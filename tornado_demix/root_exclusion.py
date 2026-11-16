"""Merkle-root exclusion of candidate withdrawals (exploratory, off by default).

A withdrawal proof names the pool's Merkle root it was built against. That root
holds the first ``n_root`` leaves, so a note with leaf index ``>= n_root`` cannot
have been spent by the withdrawal. If ``n_root <= min_leaf`` the tree held none of
the depositor's notes in that pool: the withdrawal is excluded, a fact about the
calldata and the tree, not a probability. If ``min_leaf < n_root <= max_leaf`` only
the later notes are excluded; that is shown as context and nothing is removed.

The root is not taken on trust from the calldata. It is the ``_root`` argument of the
decoded ``withdraw`` call to the pool (directly, through a declared router, or inside a
Multicall wrapper; see :mod:`tornado_demix.withdraw_root`) whose nullifier matches. A
transaction with no such call (``roots_undecoded``, also for calldata over the decoding
budget), with such calls only in frames that may fail silently (``roots_unverified``) or
with several different decoded roots (``roots_ambiguous``) leaves the root unknown. A decoded root counts only when the
pool still accepted it at the withdrawal block: it is in the rebuilt tree's root history
and fewer than ``ROOT_HISTORY_SIZE`` insertions (read from each pool contract; a pool whose
size cannot be read stays unchecked, with a recorded error) came after it. An unknown
root never excludes a withdrawal.

A deposit whose twin pool is uncertain (``pool_ambiguous``) is placed by its leaf: its
transaction hash is looked up in each candidate twin's tree, and the twin that holds it
counts it as a note. When it cannot be placed (no twin or several hold it, or a lookup
failed), or in a decoy run, every pool it may belong to stays unchecked, with the reason
in ``unchecked_reasons``: leaving such a note out could make the lowest leaf too high and
exclude a withdrawal that spent it.

Only the withdrawals of ranked candidates are read (one ``tx_info`` call per
transaction, at most :data:`ROOT_CHECK_LIMIT` per run). The result is not a lead
and is not scored: a candidate whose every withdrawal in a pool is excluded leaves
the ranking and is listed separately; a partly excluded one is recounted
(``candidates_by_count`` and the count match are recomputed) and carries the
``root_excluded`` context signal. A candidate with only partly excluding withdrawals
(nothing removed) carries the separate ``root_partial`` context signal.

In a decoy run (``deposit_shift_seconds`` > 0) every withdrawal predates the real
deposits, so the real leaf indices would exclude everything. The wallet's emulated
leaf for a deposit is then the number of pool insertions up to the block of the
*shifted* deposit time.
"""

from __future__ import annotations

import os
import sys

from .constants import TOPIC_DEPOSIT
from .errors import ApiError, ApiKeyError
from .events import decode_deposit
from .heuristics import ranked_candidates
from .incomplete import lookup_failed
from .merkle import cached_root_index, root_history_size, root_known_at
from .withdraw_root import known_multicalls, withdrawal_read

# tx_info calls (one per distinct withdrawal transaction) a single run may spend.
ROOT_CHECK_LIMIT = 200
DEFAULT_CACHE_DIR = os.path.join(".cache", "merkle")
# No default ROOT_HISTORY_SIZE exists: a guess below the true size would reject a real older
# root, so a pool whose size cannot be read stays unchecked and the failure is recorded.
# Forward search for the first insertion after a block: initial span and growth.
_SPAN_START = 2_000
_SPAN_GROWTH = 4


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _hex(value: int) -> str:
    return "0x{:064x}".format(value)


def classify(n_root: int, leaves: list[int]) -> str:
    """``excluded``, ``partial`` or ``kept`` for a root holding ``n_root`` leaves."""
    if n_root <= min(leaves):
        return "excluded"
    if n_root <= max(leaves):
        return "partial"
    return "kept"


def _insertions_up_to(client, pool, block: int) -> tuple[int, int] | None:
    """``(leaf, insertion_block)`` of the first pool insertion after ``block``.

    ``leaf`` is also the number of insertions in blocks ``<= block``.
    """
    head = client.current_block()
    low, span = block + 1, _SPAN_START
    while low <= head:
        high = min(low + span - 1, head)
        logs = client.get_logs(pool.address, TOPIC_DEPOSIT, low, high)
        if logs:
            first = min((decode_deposit(log) for log in logs), key=lambda r: r["leaf_index"])
            return first["leaf_index"], first["block"]
        low, span = high + 1, span * _SPAN_GROWTH
    return None


def wallet_notes(client, pool, deposits: list[dict], shifted: bool):
    """``(leaves, since_block)`` of the wallet's notes in ``pool``; None when one is not found.

    Real run: the leaf of each deposit's own ``Deposit`` event, matched by transaction
    hash; ``since_block`` is None (every withdrawal in the window is checked).
    Decoy run (``shifted``): the leaf is the number of insertions up to the block of the
    shifted deposit time, and the emulated note counts as inserted at the block of the
    pool's real insertion number ``leaf``. ``since_block`` is that block for the lowest leaf:
    a withdrawal mined before it predates the emulated note by construction (its newest
    root holds exactly ``leaf`` leaves), so it is not checked.
    """
    leaves: list[int] = []
    lowest = None
    for dep in deposits:
        if shifted:
            hit = _insertions_up_to(client, pool, client.block_by_time(dep["ts"], "before"))
            if hit is None:
                return None
            leaves.append(hit[0])
            if lowest is None or hit[0] < lowest[0]:
                lowest = hit
            continue
        logs = client.get_logs(pool.address, TOPIC_DEPOSIT, dep["block"], dep["block"])
        found = [
            row["leaf_index"]
            for row in map(decode_deposit, logs)
            if (row["tx_hash"] or "").lower() == dep["hash"].lower()
        ]
        if not found:
            return None
        leaves.extend(found)
    return sorted(leaves), (lowest[1] if lowest else None)


def place_ambiguous(client, network, deposit: dict) -> tuple[str | None, str]:
    """``(pool_key, "")`` of the twin whose tree holds ``deposit`` (a ``pool_ambiguous``
    deposit), found by its transaction hash in each candidate twin's ``Deposit`` events
    at its block; ``(None, reason)`` when no twin holds it or a lookup failed."""
    tx = (deposit.get("hash") or "").lower()
    if not tx or deposit.get("block") is None:
        return None, "the deposit has no transaction hash or block to look its leaf up by"
    found, failed = [], []
    for key in deposit.get("pool_ambiguous") or []:
        pool = network.by_key.get(key)
        if pool is None:
            failed.append(key)
            continue
        try:
            logs = client.get_logs(pool.address, TOPIC_DEPOSIT, deposit["block"], deposit["block"])
        except ApiKeyError:
            raise
        except ApiError:
            failed.append(key)
            continue
        if any((row["tx_hash"] or "").lower() == tx for row in map(decode_deposit, logs)):
            found.append(key)
    if len(found) == 1:
        return found[0], ""
    if len(found) > 1:
        return None, "its leaf was found in several twin pools"
    if failed:
        return None, "its leaf could not be looked up in " + ", ".join(failed)
    return None, "its leaf was found in no twin pool"


def wallet_leaves(client, pool, deposits: list[dict], shifted: bool) -> list[int] | None:
    """Leaf indices of the wallet's notes in ``pool`` (see :func:`wallet_notes`)."""
    notes = wallet_notes(client, pool, deposits, shifted)
    return None if notes is None else notes[0]


class _Memo:
    """``tx_info`` with memory and a call count, so a transaction is read once."""

    def __init__(self, client, errors=None):
        self._client = client
        self._seen = {}
        self.calls = 0
        self._errors = errors

    def tx_info(self, tx_hash):
        if tx_hash not in self._seen:
            self.calls += 1
            try:
                self._seen[tx_hash] = self._client.tx_info(tx_hash)
            except ApiKeyError:
                raise
            except ApiError as exc:
                _log("  [root_check] tx {} not read: {}".format(tx_hash, exc))
                if self._errors is not None:
                    self._errors.append(lookup_failed("root check transaction", exc, tx=tx_hash))
                self._seen[tx_hash] = None
        return self._seen[tx_hash]

    def known(self, tx_hash):
        return tx_hash in self._seen


def _accepted_root(candidates, index, blocks, block, history, stats) -> int | None:
    """Leaf count of the one candidate root the pool accepted at ``block``, else None."""
    valid = []
    for root in candidates:
        n_root = index.get(_hex(root))
        if n_root is not None and root_known_at(n_root, blocks, block, history):
            valid.append(n_root)
    if len(valid) > 1:
        stats["roots_ambiguous"] += 1
    elif not valid and candidates:
        stats["roots_outside_history"] += 1
    return valid[0] if len(valid) == 1 else None


def _own_withdrawals(res: dict, addr: str) -> list[tuple[int, dict]]:
    """(position in ``detail[addr]``, withdrawal) pairs, matched by transaction hash."""
    pool_withdrawals = [w for w in res.get("withdrawals", []) if w["to"] == addr]
    recs = res["detail"][addr]
    if len(pool_withdrawals) != len(recs):
        return []
    return [
        (i, w) for i, (r, w) in enumerate(zip(recs, pool_withdrawals)) if r["hash"] == w["tx_hash"]
    ]


def check_candidates(client, data: dict, network, cache_dir, limit=None, errors=None) -> dict:
    """Read the roots of the candidates' withdrawals and classify them.

    Returns ``{"removed": {(pool_key, addr): {position}}, "excluded": [...],
    "partial": [...], "stats": {...}}`` without touching ``data``. A failed lookup
    (transaction, tree, ``ROOT_HISTORY_SIZE``) is appended to ``errors`` when it is a list.
    """
    limit = ROOT_CHECK_LIMIT if limit is None else limit
    memo = _Memo(client, errors)
    shifted = bool(data.get("params", {}).get("deposit_shift_seconds"))
    leaves_by_pool: dict[str, list[int] | None] = {}
    since_by_pool: dict[str, int | None] = {}
    index_by_pool: dict[str, dict | None] = {}
    blocks_by_pool: dict[str, list[int]] = {}
    history_by_pool: dict[str, int] = {}
    unchecked = set()
    # Why a pool with an uncertain-twin deposit was not checked: {pool_key: reason}.
    reasons: dict[str, str] = {}
    placed: dict[str, tuple[str | None, str]] = {}  # deposit tx -> place_ambiguous()
    stats = {
        "candidates": 0,
        "withdrawals_checked": 0,
        "roots_found": 0,
        "roots_not_found": 0,
        "roots_ambiguous": 0,
        "roots_undecoded": 0,
        "roots_unverified": 0,
        "roots_outside_history": 0,
        "history_sizes": {},
        "tx_info_calls": 0,
        "limit": limit,
        "wallet_leaves": {},
        "decoy_gap_skipped": 0,
    }
    removed: dict[tuple[str, str], set[int]] = {}
    excluded, partial = [], []
    incomplete = False

    def note(what, exc, pool):
        if errors is not None:
            errors.append(lookup_failed(what, exc, address=pool.address))

    def history_size(pool_key, pool):
        """ROOT_HISTORY_SIZE of the pool read from its contract, or None (pool unchecked)."""
        try:
            size = root_history_size(client, pool, cache_dir)
        except ApiKeyError:
            raise
        except ApiError as exc:
            note("ROOT_HISTORY_SIZE", exc, pool)
            _log(
                "  [root_check] {} ROOT_HISTORY_SIZE not read ({}); pool skipped".format(
                    pool_key, exc
                )
            )
            return None
        stats["history_sizes"][pool_key] = size
        return size

    def pool_ready(pool_key):
        if pool_key in leaves_by_pool:
            return leaves_by_pool[pool_key] is not None and index_by_pool[pool_key] is not None
        pool = network.by_key[pool_key]
        res = data["denoms"][pool_key]
        deposits = [
            d
            for d in data.get("deposits", [])
            if d.get("pool_key") == pool_key and not d.get("pool_ambiguous")
        ]
        # A deposit whose twin pool is uncertain may be a note of this pool. Leaving it
        # out could make the lowest leaf too high and exclude a withdrawal that spent it,
        # so a real run places it by its leaf (looked up by transaction hash in each twin)
        # and a pool it cannot be placed for, or any such pool in a decoy run (no real
        # leaf to place by), stays unchecked with the reason recorded.
        for d in data.get("deposits", []):
            if pool_key not in (d.get("pool_ambiguous") or []):
                continue
            if shifted:
                why = "a deposit whose twin pool is uncertain (decoy run: not placed)"
            else:
                tx = (d.get("hash") or "").lower()
                if tx not in placed:
                    placed[tx] = place_ambiguous(client, network, d)
                where, why = placed[tx]
                if where == pool_key:
                    deposits.append(d)
                    continue
                if where is not None:
                    continue  # the note is in the other twin
                why = "a deposit whose twin pool is uncertain: " + why
            reasons[pool_key] = why
            note("wallet leaves", why, pool)
            _log("  [root_check] {} skipped: {}".format(pool_key, why))
            leaves_by_pool[pool_key], index_by_pool[pool_key] = None, None
            unchecked.add(pool_key)
            return False
        leaves = index = None
        try:
            notes = wallet_notes(client, pool, deposits, shifted)
            leaves = None if notes is None else notes[0]
            since_by_pool[pool_key] = None if notes is None else notes[1]
            if notes is None:
                note("wallet leaves", "a deposit's leaf was not found", pool)
            if leaves is not None:
                tree = cached_root_index(client, pool, res["window_blocks"][1], cache_dir)
                if tree.get("blocks") is None:
                    raise ApiError("the merkle cache holds no insertion blocks")
                size = history_size(pool_key, pool)
                if size is not None:
                    index = tree["roots"]
                    blocks_by_pool[pool_key] = tree["blocks"]
                    history_by_pool[pool_key] = size
        except ApiKeyError:
            raise
        except ApiError as exc:
            _log("  [root_check] {} skipped: {}".format(pool_key, exc))
            note("merkle tree", exc, pool)
        leaves_by_pool[pool_key], index_by_pool[pool_key] = leaves, index
        if leaves is None or index is None:
            unchecked.add(pool_key)
            return False
        stats["wallet_leaves"][pool_key] = leaves
        return True

    for row in ranked_candidates(data):
        pool_key, addr = row["pool_key"], row["address"]
        res = data["denoms"][pool_key]
        if "withdrawals" not in res:  # transfers mode carries no nullifier to read a root by
            unchecked.add(pool_key)
            continue
        if not pool_ready(pool_key):
            continue
        pairs = _own_withdrawals(res, addr)
        if not pairs:
            unchecked.add(pool_key)
            continue
        # Decoy: a withdrawal mined before the emulated note's insertion block is not checked.
        since = since_by_pool.get(pool_key)
        gap = [(i, w) for i, w in pairs if since is not None and w["block"] < since]
        stats["decoy_gap_skipped"] += len(gap)
        pairs = [p for p in pairs if p not in gap]
        fresh = {w["tx_hash"] for _i, w in pairs if not memo.known(w["tx_hash"])}
        if memo.calls + len(fresh) > limit:
            incomplete = True
            _log(
                "  [root_check] call limit {} reached; remaining candidates not checked".format(
                    limit
                )
            )
            break
        stats["candidates"] += 1
        leaves, index = leaves_by_pool[pool_key], index_by_pool[pool_key]
        pool_address = network.by_key[pool_key].address
        for pos, w in pairs:
            stats["withdrawals_checked"] += 1
            read = withdrawal_read(
                memo, w, pool_address, network.routers, known_multicalls(network.chain_id)
            )
            if read["root"] is None:
                stats["roots_not_found"] += 1
                if read["source"] == "ambiguous":
                    stats["roots_ambiguous"] += 1
                elif read["source"] == "unverified":
                    stats["roots_unverified"] += 1
                else:
                    stats["roots_undecoded"] += 1
                continue
            n_root = _accepted_root(
                [read["root"]],
                index,
                blocks_by_pool[pool_key],
                w["block"],
                history_by_pool[pool_key],
                stats,
            )
            if n_root is None:
                stats["roots_not_found"] += 1
                continue
            stats["roots_found"] += 1
            kind = classify(n_root, leaves)
            entry = (pool_key, addr, w["tx_hash"], n_root, max(leaves))
            if kind == "excluded":
                excluded.append(entry)
                removed.setdefault((pool_key, addr), set()).add(pos)
            elif kind == "partial":
                partial.append(entry)
    if unchecked:
        incomplete = True
    stats["tx_info_calls"] = memo.calls
    stats["incomplete"] = incomplete
    stats["unchecked_pools"] = sorted(unchecked)
    stats["unchecked_reasons"] = dict(sorted(reasons.items()))
    return {"removed": removed, "excluded": excluded, "partial": partial, "stats": stats}


def _remove(res: dict, addr: str, positions: set[int]) -> None:
    """Drop the excluded withdrawals of ``addr`` and recount that candidate's own hits.

    The field statistics other candidates are judged against stay at their pre-removal
    values: an excluded withdrawal is still a real withdrawal of someone else.
    ``field_counts`` keeps the original recipient counts for the discrimination,
    ``root_excluded_detail`` the removed records (the gas-price share still counts them);
    ``unique_recipients`` and ``total_qualifying_withdrawals`` are left as they were.
    """
    res.setdefault("field_counts", dict(res["counts"]))
    recs = res["detail"][addr]
    res.setdefault("root_excluded_detail", {}).setdefault(addr, []).extend(
        r for i, r in enumerate(recs) if i in positions
    )
    pairs = _own_withdrawals(res, addr)
    gone = {w["tx_hash"] for pos, w in pairs if pos in positions}
    res["withdrawals"] = [
        w for w in res["withdrawals"] if not (w["to"] == addr and w["tx_hash"] in gone)
    ]
    kept = [r for i, r in enumerate(recs) if i not in positions]
    if kept:
        res["detail"][addr] = kept
        res["counts"][addr] = len(kept)
    else:
        del res["detail"][addr]
        del res["counts"][addr]


def apply_root_check(
    client, data: dict, network, refresh, cache_dir=None, limit=None, errors=None
) -> dict:
    """Run the Merkle-root check on ``data`` (a ``run_demix`` result) in place.

    ``refresh()`` recomputes ``candidates_by_count`` and the signals after
    withdrawals were removed. Adds ``data["root_check"]`` (counts, the fully excluded
    candidates, ``incomplete``), ``data["root_check_incomplete"]`` and, under
    ``data["heuristics"]``, ``root_excluded`` and ``root_partial``: lists of
    ``(pool_key, address, tx_hash, n_root, max_wallet_leaf)``.
    """
    out = check_candidates(client, data, network, cache_dir or DEFAULT_CACHE_DIR, limit, errors)
    full = []
    for (pool_key, addr), positions in out["removed"].items():
        res = data["denoms"][pool_key]
        everything = len(positions) == len(res["detail"][addr])
        _remove(res, addr, positions)
        if everything:
            full.append((pool_key, addr))
    if out["removed"]:
        refresh()
    for (pool_key, addr), _ in out["removed"].items():
        res = data["denoms"][pool_key]
        if addr in res.get("signals", {}):
            _note(res, addr, "root_excluded")
    for entry in out["partial"]:
        res = data["denoms"][entry[0]]
        if entry[1] in res.get("signals", {}):
            _note(res, entry[1], "root_partial")
    data["heuristics"]["root_excluded"] = out["excluded"]
    data["heuristics"]["root_partial"] = out["partial"]
    # A partly excluded candidate whose recount no longer qualifies it (it lost its count
    # match) is not in the ranking any more; it is listed instead of vanishing.
    ranked = {(r["pool_key"], r["address"]) for r in ranked_candidates(data)}
    dropped = []
    for pool_key, addr in out["removed"]:
        if (pool_key, addr) in full or (pool_key, addr) in ranked:
            continue
        dropped.append(
            {
                "pool_key": pool_key,
                "address": addr,
                "remaining": len(data["denoms"][pool_key]["detail"].get(addr, [])),
                "withdrawals": [
                    {"tx_hash": e[2], "n_root": e[3]}
                    for e in out["excluded"]
                    if e[0] == pool_key and e[1] == addr
                ],
            }
        )
    blocks = []
    for pool_key, addr in full:
        txs = [(e[2], e[3]) for e in out["excluded"] if e[0] == pool_key and e[1] == addr]
        blocks.append(
            {
                "pool_key": pool_key,
                "address": addr,
                "withdrawals": [{"tx_hash": t, "n_root": n} for t, n in txs],
            }
        )
    stats = out["stats"]
    stats["excluded_candidates"] = blocks
    stats["dropped_after_recount"] = dropped
    data["root_check"] = stats
    data["root_check_incomplete"] = stats["incomplete"]
    _log(
        "[*] Merkle-root check: {} withdrawal(s) read, {} excluded, {} candidate(s) removed{}".format(
            stats["withdrawals_checked"],
            len(out["excluded"]),
            len(blocks),
            "; INCOMPLETE" if stats["incomplete"] else "",
        )
    )
    return data


def _note(res: dict, addr: str, signal: str) -> None:
    """Add a context signal; the score is not recomputed (the signals have no weight)."""
    signals = set(res["signals"][addr])
    signals.add(signal)
    res["signals"][addr] = sorted(signals)
