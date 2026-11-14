"""Signals, score and evidence bands for demix candidates.

Besides the amount+timing count match: a withdrawal that reuses a rare deposit
gas price (only where the sender chose the price, i.e. no EIP-1559 base fee),
and a candidate that transacts directly with the depositor outside Tornado.
The score orders candidates; the band describes the structure of the evidence.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from .attribution import format_label, label_of

# A deposit gas price reused by more withdrawals than this in one window is a
# common wallet default, not a fingerprint.
MAX_GAS_PRICE_SHARE = 3

# Signal weights (documented, tunable). Higher = stronger evidence.
SIGNAL_WEIGHTS = {
    "count_match": 0.30,  # received exactly a voucher's worth of notes
    "self_relayed": 0.25,  # paid own gas -> funded address tied to exit
    "gas_price": 0.25,  # exact deposit gas price reused on withdrawal
    "linked": 0.40,  # directly transacts with the depositor
    "profile_match": 0.35,  # received the full pool fingerprint
    "early_profile": 0.35,  # received the wallet's whole note profile within 72 h
}
# Noisy-OR keeps the score in [0, 1] only for weights inside (0, 1).
if not all(0.0 < w < 1.0 for w in SIGNAL_WEIGHTS.values()):
    raise ValueError("every signal weight must lie strictly between 0 and 1")


# Below this discrimination a count match is not a signal at all (no score, no
# band, no candidate); above it the weight scales with disc. A judgement call.
MIN_COUNT_DISCRIMINATION = 0.5

# disc is relative: with two or three recipients in the window a unique count
# reaches 1.0 although a chance match is still likely. Below this field size a
# count match is not evidence at all.
MIN_FIELD_SIZE = 5

# Early multi-pool profile match (tools/placebo_profile.py): a recipient that, in
# every pool the wallet used, received exactly the wallet's note count inside
# [first deposit, last deposit + EARLY_PROFILE_HOURS]. One pool or a handful of
# notes is chance-level; over >= 2 pools and >= 10 notes the chance share was 0.12
# on 28,739 real depositors (a direct linked address: 0.25). The recipient's
# withdrawals come from the search windows, so an exit window under
# EARLY_PROFILE_HOURS hours cannot hold the full window and nobody qualifies.
EARLY_PROFILE_MIN_POOLS = 2
EARLY_PROFILE_MIN_NOTES = 10
EARLY_PROFILE_HOURS = 72


def count_discrimination(res, hits):
    """How much of the field a hit count of ``hits`` eliminates, in [0, 1].

    0.0 when every recipient shares the count (a single-note voucher), 1.0 when
    the count is unique.
    """
    unique = res.get("unique_recipients") or len(res.get("counts", {}))
    if unique <= 1:
        return 0.0
    # The field as it was before a Merkle-root removal (root_exclusion.py), when there was one.
    sharing = sum(1 for c in res.get("field_counts", res["counts"]).values() if c == hits)
    if sharing >= unique:
        return 0.0
    if sharing <= 0:
        # No recipient has this count. Unreachable from the pipeline, where
        # hits comes from counts[addr]; without it the formula would exceed 1.
        return 1.0
    return 1.0 - (float(sharing) - 1.0) / (float(unique) - 1.0)


def count_is_evidence(res: dict, hits: int) -> bool:
    """True when a hit count narrows a large enough field to count as a signal."""
    field = res.get("unique_recipients") or len(res.get("counts", {}))
    return field >= MIN_FIELD_SIZE and count_discrimination(res, hits) >= MIN_COUNT_DISCRIMINATION


def voucher_forward_seconds(params: dict) -> float:
    """Length of a voucher's search window after its last deposit, in seconds."""
    hours = params.get("exit_window_hours")
    if hours and hours > 0:
        return hours * 3600
    return params.get("window_days", 30) * 86400


def count_in_voucher_order(vouchers: list[dict], params: dict, pool_key: str, times: list) -> bool:
    """True when withdrawals at ``times`` can be the notes of a voucher by time.

    A pool's windows are merged into one field, so a recipient's count is taken
    over all of them. A count equal to a voucher's size only fits that voucher
    when every withdrawal falls inside that voucher's own window; a count equal
    to the sum of the pool's vouchers fits when no withdrawal precedes the
    pool's first deposit. Without this, withdrawals made before a voucher's
    deposits could match it.
    """
    own = [v for v in vouchers if v.get("pool_key") == pool_key]
    if not own or not times:
        return True
    hits, forward = len(times), voucher_forward_seconds(params)
    for v in own:
        if v["count"] == hits and all(v["first_ts"] <= t <= v["last_ts"] + forward for t in times):
            return True
    return (
        len(own) > 1
        and hits == sum(v["count"] for v in own)
        and min(times) >= min(v["first_ts"] for v in own)
    )


def count_match_in_order(data: dict, pool_key: str, recs: list[dict]) -> bool:
    """:func:`count_in_voucher_order` for one recipient's withdrawals.

    Applied only to runs made with the check (``params.voucher_order``), so cached
    runs from the evaluation re-score as they were measured.
    """
    params = data.get("params", {})
    if not params.get("voucher_order"):
        return True
    return count_in_voucher_order(
        data.get("vouchers", []), params, pool_key, [r["ts"] for r in recs]
    )


def wallet_profile(data: dict) -> dict[str, int]:
    """Notes deposited per pool over all of the wallet's deposits: {pool_key: count}."""
    return dict(Counter(d["pool_key"] for d in data.get("deposits", []) if d.get("pool_key")))


def early_profile_applies(profile: dict[str, int]) -> bool:
    """True when a profile is wide and large enough to be a fingerprint."""
    return (
        len(profile) >= EARLY_PROFILE_MIN_POOLS and sum(profile.values()) >= EARLY_PROFILE_MIN_NOTES
    )


def _early_profile_hits(data: dict, profile: dict[str, int]) -> list[str]:
    """Recipients holding exactly ``profile`` inside every pool's early window."""
    deposits = data.get("deposits", [])
    windows = {}
    for pool_key in profile:
        stamps = [d["ts"] for d in deposits if d.get("pool_key") == pool_key and d.get("ts")]
        if not stamps or pool_key not in data.get("denoms", {}):
            return []
        windows[pool_key] = (min(stamps), max(stamps) + EARLY_PROFILE_HOURS * 3600)
    first, *rest = profile
    candidates = set(data["denoms"][first]["detail"])
    for pool_key in rest:
        candidates &= set(data["denoms"][pool_key]["detail"])
    hits = []
    for addr in sorted(candidates):
        for pool_key, need in profile.items():
            lo, hi = windows[pool_key]
            got = sum(1 for r in data["denoms"][pool_key]["detail"][addr] if lo <= r["ts"] <= hi)
            if got != need:
                break
        else:
            hits.append(addr)
    return hits


def apply_heuristics(
    data: dict,
    counterparties: set[str],
    gas_gate: Callable[[int], bool] | None = None,
    is_contract: Callable[[str], bool | None] | None = None,
    withdrawal_senders: dict[str, str] | None = None,
    shared_deposits: dict[str, list[str]] | None = None,
) -> dict:
    """Add ``signals`` and ``confidence`` to every recipient of a run_demix() result.

    Signals are keyed by (pool_key, address), so a signal earned in one pool does
    not follow the address into another. ``gas_gate(block)`` says whether the
    sender chose that block's gas price (None: ungated). ``is_contract(address)``
    returns True, False or None; a contract counterparty does not earn ``linked``.
    A direct link (the depositor itself or a non-contract counterparty) earns
    ``linked`` only when the recipient's first withdrawal in the pool is within
    :data:`EARLY_EXIT_HOURS` after a voucher's last deposit there; a later one earns
    ``linked_late``, which is context: no score, no band (placebo test,
    docs/EVALUATION.md).
    ``withdrawal_senders`` maps a withdrawal tx hash to its sender when that sender
    is the depositor or one of its direct counterparties (``linked_sender``).
    ``shared_deposits`` maps a recipient to the exchange deposit addresses it shares
    with the depositor (``shared_deposit``, see :mod:`tornado_demix.deposit_addresses`).
    ``early_profile`` needs no input: it reads ``data["deposits"]`` and the recipients'
    withdrawals (see :data:`EARLY_PROFILE_HOURS`).
    """
    wallet = (data.get("wallet") or "").lower()
    withdrawal_senders = withdrawal_senders or {}
    shared_deposits = shared_deposits or {}
    deposits = data.get("deposits", [])
    deposit_gas = {d["gas_price"] for d in deposits if d.get("gas_price")}

    gas_matches = []  # (pool_key, recipient, gas_price, tx_hash)
    linked_hits = []  # (pool_key, recipient): every direct link, early or late
    late_hits = []  # (pool_key, recipient, hours after the last deposit or None)
    linked_contracts = set()  # direct counterparties that are contracts
    sender_hits = []  # (pool_key, recipient, sender, tx_hash)
    deposit_hits = []  # (pool_key, recipient, [deposit addresses])
    signals = {}  # (pool_key, address) -> set of signal names
    discriminations = {}  # (pool_key, address) -> count discrimination in [0, 1]

    for pool_key, res in data.get("denoms", {}).items():
        gp_counts = Counter()
        removed = res.get("root_excluded_detail", {}).values()
        for recs in [*res["detail"].values(), *removed]:
            for r in recs:
                if r.get("gas_price"):
                    gp_counts[r["gas_price"]] += 1

        target_counts = set(res.get("target_counts", []))
        for addr, recs in res["detail"].items():
            sig = signals.setdefault((pool_key, addr), set())
            hits = res["counts"][addr]

            discrimination = count_discrimination(res, hits)
            if (
                hits in target_counts
                and count_is_evidence(res, hits)
                and count_match_in_order(data, pool_key, recs)
            ):
                sig.add("count_match")
            discriminations[(pool_key, addr)] = discrimination
            if any(r.get("self_relayed") for r in recs):
                sig.add("self_relayed")

            # A withdrawal reusing one of the wallet's deposit gas prices, where
            # that price is rare in the window (not a common auto-fee value).
            # Only a withdrawal the user sent without a relayer counts: a relayer
            # chooses the gas price of the transactions it sends (on real data 44 of
            # 45 such matches were relayed). gas_gate rejects blocks whose price the
            # sender did not choose.
            for r in recs:
                if not r.get("self_relayed"):
                    continue
                gp = r.get("gas_price")
                if gp and gp in deposit_gas and gp_counts[gp] <= MAX_GAS_PRICE_SHARE:
                    if gas_gate is not None and not gas_gate(r.get("block")):
                        continue
                    sig.add("gas_price")
                    gas_matches.append((pool_key, addr, gp, r["hash"]))
                    break

            # Every withdrawal the depositor's side sent is recorded, so the evidence
            # can say how many of them it sent, not just that one was.
            for r in recs:
                sender = withdrawal_senders.get((r.get("hash") or "").lower())
                if sender:
                    sig.add("linked_sender")
                    sender_hits.append((pool_key, addr, sender, r["hash"]))

            if addr != wallet and shared_deposits.get(addr):
                sig.add("shared_deposit")
                deposit_hits.append((pool_key, addr, list(shared_deposits[addr])))

            # A withdrawal back to the depositor itself, or to a direct
            # non-contract counterparty. Only an early one is a lead.
            direct = addr == wallet
            if not direct and addr in counterparties:
                if is_contract is not None and is_contract(addr):
                    linked_contracts.add(addr)
                else:
                    direct = True
            if direct:
                linked_hits.append((pool_key, addr))
                stamps = [r["ts"] for r in recs if r.get("ts") is not None]
                if stamps and early_exit(data, pool_key, min(stamps)):
                    sig.add("linked")
                else:
                    sig.add("linked_late")
                    late_hits.append(
                        (pool_key, addr, _withdrawal_delay_hours(data, pool_key, recs))
                    )

    profile = wallet_profile(data)
    profile_hits = []  # (pool_key, recipient, {pool_key: count})
    if early_profile_applies(profile):
        for addr in _early_profile_hits(data, profile):
            for pool_key in profile:
                signals.setdefault((pool_key, addr), set()).add("early_profile")
                profile_hits.append((pool_key, addr, dict(profile)))

    for pool_key, res in data.get("denoms", {}).items():
        res["signals"] = {}
        res["confidence"] = {}
        res["discrimination"] = {}
        for addr in res["detail"]:
            sig = signals.get((pool_key, addr), set())
            disc = discriminations.get((pool_key, addr), 0.0)
            res["signals"][addr] = sorted(sig)
            res["discrimination"][addr] = round(disc, 4)
            res["confidence"][addr] = _score(sig, disc)

    data["heuristics"] = {
        "deposit_gas_prices": sorted(deposit_gas),
        "gas_price_matches": gas_matches,
        "linked_addresses": linked_hits,
        "linked_late": late_hits,
        "linked_contracts": sorted(linked_contracts),
        "linked_senders": sender_hits,
        "shared_deposits": deposit_hits,
        "early_profiles": profile_hits,
    }
    return data


SIGNAL_LABEL = {
    "count_match": "Amount + timing (count match)",
    "self_relayed": "Self-relayed exit (paid own gas)",
    "gas_price": "Unique gas-price reuse",
    "linked": "Linked address (direct counterparty)",
    "linked_late": "Late direct link (context)",
    "linked_sender": "Withdrawal sent by the depositor or its counterparty",
    "shared_deposit": "Shared exchange deposit address",
    "profile_match": "Denomination-profile match",
    "early_profile": "Early multi-pool profile match",
    "root_excluded": "Withdrawal excluded by Merkle root (context)",
    "root_partial": "Some notes not yet in the Merkle tree (context)",
}


def credit_profile_match(data: dict, pool_keys: list[str], address: str) -> None:
    """Record that ``address`` received this wallet's whole fingerprint.

    Called from :func:`tornado_demix.multi.correlate`. A single pool is not a
    fingerprint (it is the count match again), so it is not credited.
    """
    if len(pool_keys) < 2:
        return
    for pool_key in pool_keys:
        res = data.get("denoms", {}).get(pool_key)
        if not res or address not in res.get("signals", {}):
            continue
        signals = set(res["signals"][address])
        if "profile_match" in signals:
            continue
        signals.add("profile_match")
        res["signals"][address] = sorted(signals)
        res["confidence"][address] = _score(
            signals, res.get("discrimination", {}).get(address, 0.0)
        )


def _score(signals, discrimination):
    """Noisy-OR across evidence families; within a family only the strongest
    signal counts, so dependent signals are not added twice. count_match is
    scaled by how much the count actually narrowed the field."""
    strongest = {}
    for signal in signals:
        weight = SIGNAL_WEIGHTS.get(signal, 0.0)
        if signal == "count_match":
            weight *= discrimination
        family = METHOD_FAMILY.get(signal, signal)
        strongest[family] = max(strongest.get(family, 0.0), weight)
    product = 1.0
    for _family, weight in sorted(strongest.items()):
        product *= 1 - weight
    return round(1 - product, 4)


def _plural(count: int, noun: str) -> str:
    """ "1 withdrawal", "2 withdrawals"."""
    return f"{count} {noun if count == 1 else noun + 's'}"


def _duration(seconds: float) -> str:
    """A delay in words: "5 min", "3.2 h", "100 h (4.2 days)"."""
    if seconds < 3600:
        return f"{max(round(seconds / 60), 0)} min"
    hours = seconds / 3600
    if hours < 10:
        return f"{hours:.1f} h"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours:.0f} h ({hours / 24:.1f} days)"


def numbered_deposits(data: dict) -> list[tuple[int, dict]]:
    """The depositor's deposits as ``[(n, deposit)]``, numbered 1.. in time order.

    The one numbering every report uses: the deposits table lists them as D1, D2, ...
    and the evidence of each candidate refers to them by these numbers."""
    deps = sorted(enumerate(data.get("deposits") or []), key=lambda x: (x[1].get("ts") or 0, x[0]))
    return [(n, dep) for n, (_i, dep) in enumerate(deps, 1)]


def _numbers_text(numbers: list[int]) -> str:
    """[1, 2, 3, 7] as "D1–D3, D7"."""
    out, run = [], []
    for n in sorted(numbers):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"D{run[0]}" if len(run) == 1 else f"D{run[0]}–D{run[-1]}")
        run = [n]
    if run:
        out.append(f"D{run[0]}" if len(run) == 1 else f"D{run[0]}–D{run[-1]}")
    return ", ".join(out)


def voucher_deposit_numbers(data: dict, voucher: dict) -> list[int]:
    """The deposit numbers (see :func:`numbered_deposits`) of one voucher."""
    hashes = {d.get("hash") for d in voucher.get("deposits") or [] if d.get("hash")}
    first = voucher.get("first_ts")
    last = voucher.get("last_ts", first)
    out = []
    for n, dep in numbered_deposits(data):
        if dep.get("pool_key") != voucher.get("pool_key"):
            continue
        if hashes:
            if dep.get("hash") in hashes:
                out.append(n)
        elif first is not None and first <= (dep.get("ts") or 0) <= last:
            out.append(n)
    return out


def _voucher_text(data: dict, voucher: dict) -> str:
    """ "the 24-note voucher of the 100 ETH pool (deposits D1–D24)"."""
    nums = voucher_deposit_numbers(data, voucher)
    where = f" (deposits {_numbers_text(nums)})" if nums else ""
    return f"the {voucher['count']}-note voucher of the {voucher['pool_key']} pool{where}"


def _link_text(data: dict, address: str) -> str:
    """ "tx 0x…" of the transaction that makes ``address`` a direct counterparty, or a
    plain statement that the run did not record it (never an invented hash)."""
    link = (data.get("counterparty_links") or {}).get(address)
    if link and link.get("tx"):
        if link.get("ts"):
            when = datetime.fromtimestamp(link["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
            return f"tx {link['tx']}, first transaction between them on {when}"
        return f"tx {link['tx']}"
    return "linking transaction not recorded in this run"


def _last_deposit_before(data: dict, pool_key: str, ts: int):
    """``(last deposit ts, its deposit number)`` of the latest voucher of the pool that
    ended before ``ts``; ``(None, None)`` when no voucher did."""
    best = None
    for v in data.get("vouchers", []):
        last = v.get("last_ts", v.get("first_ts"))
        if v.get("pool_key") == pool_key and last is not None and last <= ts:
            if best is None or last > best.get("last_ts", best.get("first_ts")):
                best = v
    if best is None:
        return None, None
    last = best.get("last_ts", best.get("first_ts"))
    nums = voucher_deposit_numbers(data, best)
    return last, (nums[-1] if nums else None)


def _delay_text(data: dict, pool_key: str, recs: list[dict]) -> str:
    """ "first withdrawal 5 min after the voucher's last deposit (D2)", or ""."""
    stamps = sorted(r["ts"] for r in recs if r.get("ts"))
    if not stamps:
        return ""
    last, num = _last_deposit_before(data, pool_key, stamps[0])
    if last is None:
        return "first withdrawal before any of the pool's vouchers ended"
    ref = f" (D{num})" if num else ""
    return f"first withdrawal {_duration(stamps[0] - last)} after the voucher's last deposit{ref}"


# How each signal counts, appended to its line of the "why" text.
_REASON_STATUS = {
    "linked": "lead signal",
    "early_profile": "lead signal",
    "count_match": "scored for ordering only, never raises the band",
    "self_relayed": "scored for ordering only, never raises the band",
    "gas_price": "scored for ordering only, never raises the band",
    "profile_match": "scored for ordering only, never raises the band",
    "linked_late": "context only, not scored",
    "linked_sender": "context only, not scored",
    "shared_deposit": "context only, not scored",
    "root_excluded": "note, context only",
    "root_partial": "note, context only",
}


def candidate_reason(row: dict) -> str:
    """Why an address was chosen: every signal that fired for it, each with its concrete
    evidence (sender, transactions, counts) and how it counts. Built from the row's
    evidence lines (:func:`candidate_evidence`), so it never names a signal that did not
    fire and never falls back to generic wording."""
    held = [e for e in row.get("evidence") or [] if e["holds"]]
    if not held:
        return "No signal fired for this address."
    parts = []
    for e in held:
        detail = e["detail"]
        status = _REASON_STATUS.get(e["signal"], "context only, not scored")
        parts.append(f"{e['label'][0].upper() + e['label'][1:]}: {detail} ({status}).")
    if not set(row["signals"]) & LEAD_SIGNALS:
        parts.append("No lead signal.")
    return " ".join(parts)


EVIDENCE_LABEL = {
    "count_match": "amount + timing",
    "self_relayed": "self-relay (same family as amount + timing)",
    "gas_price": "gas price",
    "linked": "linked address",
    "linked_late": "late direct link (context)",
    "linked_sender": "linked withdrawal sender",
    "shared_deposit": "shared exchange deposit address",
    "profile_match": "denomination profile",
    "early_profile": "early multi-pool profile (same family as amount + timing)",
    "root_excluded": "Merkle-root exclusion (context)",
    "root_partial": "Merkle-root partial overlap (context)",
}


def _linked_detail(data, signals, address, pool_key, recs):
    own = address == (data.get("wallet") or "").lower()
    who = (
        "is the depositor's own address"
        if own
        else f"is a direct counterparty of the depositor ({_link_text(data, address)}): it "
        "transacts directly with the depositor outside Tornado"
    )
    if "linked" in signals:
        delay = _delay_text(data, pool_key, recs)
        return f"{who}; {delay}, within {EARLY_EXIT_HOURS} h"
    if "linked_late" in signals:
        return "a direct link, but withdrawn late: see the next line"
    if own:
        return "is the depositor's own address"
    if address in data.get("heuristics", {}).get("linked_contracts", []):
        return "transacts with the depositor, but is a contract (router, DEX, service): not counted"
    return "no direct transaction with the depositor"


def _late_detail(data, pool_key, address):
    hits = [
        hours
        for pk, addr, hours in data.get("heuristics", {}).get("linked_late", [])
        if pk == pool_key and addr == address
    ]
    own = address == (data.get("wallet") or "").lower()
    who = (
        "is the depositor's own address"
        if own
        else f"is a direct counterparty of the depositor ({_link_text(data, address)})"
    )
    hours = hits[0] if hits else None
    when = (
        f"its first withdrawal came {hours:.0f} h after the last deposit"
        if hours is not None
        else f"its first withdrawal did not come within {EARLY_EXIT_HOURS} h of a deposit"
    )
    return (
        f"{who}, but {when}: a direct link this late is chance-level on real "
        "depositors (placebo test), so it is context, not a lead"
    )


def _sender_detail(data, pool_key, address, hits_total):
    hits = [
        (sender, tx)
        for pk, addr, sender, tx in data.get("heuristics", {}).get("linked_senders", [])
        if pk == pool_key and addr == address
    ]
    if not hits:
        return "no withdrawal to it was sent by the depositor or one of its direct counterparties"
    wallet = (data.get("wallet") or "").lower()
    by_sender: dict[str, list[str]] = {}
    for sender, tx in hits:
        by_sender.setdefault(sender, []).append(tx)
    parts = []
    for sender, txs in by_sender.items():
        n = len(txs)
        who = (
            f"the depositor itself ({sender})"
            if sender == wallet
            else f"{sender}, which is a direct counterparty of the depositor "
            f"({_link_text(data, sender)})"
        )
        first = f"tx {txs[0]}" if n == 1 else f"first tx {txs[0]}"
        parts.append(
            f"{n} of {_plural(hits_total, 'withdrawal')} to this address "
            f"{'was' if n == 1 else 'were'} sent by {who}: that address signed the "
            f"withdrawal {'transaction' if n == 1 else 'transactions'} and paid the gas, "
            f"no relayer service did ({first})"
        )
    return "; ".join(parts)


def _deposit_detail(data, pool_key, address):
    hits = [
        addrs
        for pk, addr, addrs in data.get("heuristics", {}).get("shared_deposits", [])
        if pk == pool_key and addr == address
    ]
    if not hits:
        return "did not send funds to an exchange deposit address the depositor also used"
    info = {d["address"]: d for d in data.get("deposit_addresses") or []}
    parts = []
    for dep in hits[0]:
        row = info.get(dep, {})
        target = row.get("exchange") or "a hot wallet"
        parts.append(f"{dep}, swept to {target}")
    return (
        "sent funds to the same exchange deposit address as the depositor ("
        + "; ".join(parts)
        + ")"
    )


def _profile_text(data, profile):
    denom = {d["pool_key"]: d.get("denom", 0) for d in data.get("deposits", [])}
    ordered = sorted(profile.items(), key=lambda kv: denom.get(kv[0], 0))
    return " + ".join(f"{n}×{pk}" for pk, n in ordered)


def _root_detail(data, pool_key, address, kind):
    h = data.get("heuristics", {})
    gone = [e for e in h.get("root_excluded", []) if e[0] == pool_key and e[1] == address]
    part = [e for e in h.get("root_partial", []) if e[0] == pool_key and e[1] == address]
    if kind == "root_excluded" and gone:
        return (
            f"{_plural(len(gone), 'withdrawal')} excluded by their Merkle root (the tree held "
            "none of the wallet's notes in this pool) and not counted"
        )
    if kind == "root_partial" and part:
        n_root = min(e[3] for e in part)
        return (
            f"{_plural(len(part), 'withdrawal')} {'proves' if len(part) == 1 else 'prove'} against a root that was missing some "
            f"of the wallet's notes (leaf >= {n_root}) at that time; nothing removed, all "
            "still counted"
        )
    return ""


def _early_profile_detail(data, signals):
    profile = wallet_profile(data)
    if not early_profile_applies(profile):
        return (
            f"does not apply: the wallet's profile ({_profile_text(data, profile) or 'no notes'}) "
            f"needs at least {EARLY_PROFILE_MIN_POOLS} pools and {EARLY_PROFILE_MIN_NOTES} notes"
        )
    text = _profile_text(data, profile)
    if "early_profile" in signals:
        return (
            f"received the wallet's full note profile within {EARLY_PROFILE_HOURS} h "
            f"of the last deposit in each pool ({text})"
        )
    return (
        f"did not receive the wallet's full profile {text} within {EARLY_PROFILE_HOURS} h "
        "of the last deposit in each pool"
    )


def _withdrawal_delay_hours(data, pool_key, recs):
    """Hours from the pool's last deposit before the first withdrawal to that withdrawal."""
    stamps = sorted(r["ts"] for r in recs if r.get("ts"))
    if not stamps:
        return None
    last = [
        v["last_ts"]
        for v in data.get("vouchers", [])
        if v["pool_key"] == pool_key and v.get("last_ts") and v["last_ts"] <= stamps[0]
    ]
    return (stamps[0] - max(last)) / 3600.0 if last else None


def _tx_list(hashes: list[str], limit: int = 3) -> str:
    """ "0xa, 0xb, 0xc and 4 more" (full hashes)."""
    shown = ", ".join(hashes[:limit])
    more = len(hashes) - limit
    return shown + (f" and {more} more" if more > 0 else "")


def _gas_detail(data, pool_key, address):
    gas = [
        (gp, tx)
        for pk, addr, gp, tx in data.get("heuristics", {}).get("gas_price_matches", [])
        if pk == pool_key and addr == address
    ]
    if not gas:
        return "no deposit gas price reused (or not checkable after EIP-1559)"
    gp, tx = gas[0]
    same = [(n, d) for n, d in numbered_deposits(data) if d.get("gas_price") == gp]
    same.sort(key=lambda x: (x[1].get("pool_key") != pool_key, x[0]))
    text = f"self-relayed withdrawal {tx} paid {gp / 1e9:.3f} gwei"
    if same:
        n, dep = same[0]
        ref = f"deposit D{n} (tx {dep['hash']})" if dep.get("hash") else f"deposit D{n}"
        text += f", the gas price of {ref}"
        if len(same) > 1:
            text += f"; also used by {_numbers_text([m for m, _d in same[1:]])}"
    return text + f"; a price used by at most {MAX_GAS_PRICE_SHARE} withdrawals in the window"


def candidate_evidence(data: dict, pool_key: str, address: str) -> list[dict]:
    """Every evidence line for one candidate, including those that do not hold.

    Each item: {signal, label, family, holds, detail}. Attribution is not evidence.
    A line that holds names its concrete evidence (counts, transactions, addresses,
    deposit numbers of :func:`numbered_deposits`).
    """
    res = data["denoms"][pool_key]
    signals = set(res.get("signals", {}).get(address, []))
    recs = res["detail"][address]
    hits = res["counts"][address]
    targets = sorted(res.get("target_counts", []))
    field = res.get("unique_recipients") or len(res.get("counts", {}))
    sharing = sum(1 for c in res.get("field_counts", res["counts"]).values() if c == hits)
    disc = res.get("discrimination", {}).get(address, 0.0)

    sized = [
        v for v in data.get("vouchers", []) if v["pool_key"] == pool_key and v["count"] == hits
    ]
    if sized:
        what = " or ".join(_voucher_text(data, v) for v in sized)
    else:
        what = f"all {hits} notes of the pool's vouchers"
    if hits in targets:
        count_detail = (
            f"received {_plural(hits, 'withdrawal')} = {what} within its window; {sharing} of "
            f"{field} recipients in the window share this count; discrimination {disc:.2f} "
            f"(counted from {MIN_COUNT_DISCRIMINATION:.2f})"
        )
        delay = _withdrawal_delay_hours(data, pool_key, recs)
        if delay is not None:
            count_detail += f"; first withdrawal {delay:.0f} h after the last deposit"
        if field < MIN_FIELD_SIZE:
            count_detail += f"; a field of {field} is too small to count (needs {MIN_FIELD_SIZE})"
        if not count_match_in_order(data, pool_key, recs):
            count_detail += (
                "; not counted: the withdrawals fall outside the window of every voucher of "
                "this size (some precede its deposits)"
            )
    else:
        sizes = ", ".join(map(str, targets)) or "none"
        count_detail = (
            f"{_plural(hits, 'withdrawal')}, no voucher of that size "
            f"(voucher {'size' if len(targets) == 1 else 'sizes'}: {sizes})"
        )

    selfs = [
        r["hash"] for r in sorted(recs, key=lambda r: r.get("ts") or 0) if r.get("self_relayed")
    ]
    lines = [
        ("count_match", count_detail),
        (
            "self_relayed",
            f"{len(selfs)} of {_plural(hits, 'withdrawal')} sent without a relayer (the "
            f"recipient paid the gas): {_tx_list(selfs)}"
            if selfs
            else "no withdrawal was self-relayed (sent by the recipient itself)",
        ),
        ("gas_price", _gas_detail(data, pool_key, address)),
        ("linked", _linked_detail(data, signals, address, pool_key, recs)),
        ("linked_sender", _sender_detail(data, pool_key, address, hits)),
        ("shared_deposit", _deposit_detail(data, pool_key, address)),
        ("early_profile", _early_profile_detail(data, signals)),
    ]
    if "linked_late" in signals:
        lines.insert(4, ("linked_late", _late_detail(data, pool_key, address)))
    if "profile_match" in signals:
        lines.append(("profile_match", "received the wallet's full multi-pool fingerprint"))
    for kind in ("root_excluded", "root_partial"):
        if kind in signals:
            lines.append((kind, _root_detail(data, pool_key, address, kind)))
    return [
        {
            "signal": signal,
            "label": EVIDENCE_LABEL[signal],
            "family": METHOD_FAMILY[signal],
            "holds": signal in signals,
            "detail": detail,
            "short": _short_evidence(data, signal, pool_key, address, recs, hits, disc)
            if signal in signals
            else "",
        }
        for signal, detail in lines
    ]


def _short_evidence(data, signal, pool_key, address, recs, hits, disc) -> str:
    """One short phrase for a fired signal (the one-line "why", see
    :func:`candidate_reason_short`); still concrete: counts, deposit numbers, addresses."""
    if signal == "count_match":
        return f"{_plural(hits, 'withdrawal')} = a {hits}-note voucher, discrimination {disc:.2f}"
    if signal == "self_relayed":
        n = sum(1 for r in recs if r.get("self_relayed"))
        return f"{n} of {_plural(hits, 'withdrawal')} self-relayed"
    if signal == "gas_price":
        gps = [
            gp
            for pk, a, gp, _tx in data.get("heuristics", {}).get("gas_price_matches", [])
            if pk == pool_key and a == address
        ]
        nums = [n for n, d in numbered_deposits(data) if gps and d.get("gas_price") == gps[0]]
        return f"gas price of deposit D{nums[0]}" if nums else "gas price of a deposit"
    if signal in ("linked", "linked_late"):
        own = address == (data.get("wallet") or "").lower()
        who = "the depositor's own address" if own else "direct counterparty of the depositor"
        delay = _delay_text(data, pool_key, recs).replace("first withdrawal ", "")
        delay = delay.replace(" after the voucher's last deposit", " after the last deposit")
        return f"{who}, first withdrawal {delay}" + (" (late)" if signal == "linked_late" else "")
    if signal == "linked_sender":
        return _sender_summary(data, pool_key, address, hits)
    if signal == "shared_deposit":
        hits_ = [
            addrs
            for pk, a, addrs in data.get("heuristics", {}).get("shared_deposits", [])
            if pk == pool_key and a == address
        ]
        deps = ", ".join(hits_[0]) if hits_ else ""
        return f"shared exchange deposit address {deps}".strip()
    if signal == "early_profile":
        return f"full note profile within {EARLY_PROFILE_HOURS} h"
    if signal == "profile_match":
        return "full multi-pool fingerprint"
    return "Merkle-root note"


def candidate_reason_short(row: dict) -> str:
    """The "why" in one line: each fired signal as a short concrete phrase, context
    signals marked. For summaries (the report's first page); the full text is
    :func:`candidate_reason`."""
    held = [e for e in row.get("evidence") or [] if e["holds"]]
    if not held:
        return "No signal fired."
    parts = []
    for e in held:
        text = e.get("short") or e["label"]
        if e["signal"] in CONTEXT_SIGNALS | NOTE_SIGNALS:
            text += " (context)"
        elif e["signal"] in LEAD_SIGNALS:
            text += " (lead)"
        parts.append(text)
    return "; ".join(parts)


def _sender_summary(data: dict, pool_key: str, address: str, hits: int) -> str:
    """ "29 of 29 withdrawals sent by 0x…" for the method breakdown table."""
    wallet = (data.get("wallet") or "").lower()
    by_sender = Counter(
        sender
        for pk, addr, sender, _tx in data.get("heuristics", {}).get("linked_senders", [])
        if pk == pool_key and addr == address
    )
    return "; ".join(
        f"{n} of {_plural(hits, 'withdrawal')} sent by "
        + ("the depositor itself" if sender == wallet else f"direct counterparty {sender}")
        for sender, n in by_sender.items()
    )


def method_breakdown(data: dict) -> list[dict]:
    """Group candidate addresses by the signal that flagged them: [{method, label, rows}]."""
    order = [
        "count_match",
        "self_relayed",
        "gas_price",
        "linked",
        "linked_sender",
        "shared_deposit",
        "early_profile",
    ]
    per = {s: [] for s in order}
    for pool_key, res in data.get("denoms", {}).items():
        target = set(res.get("target_counts", []))
        for addr, sig in res.get("signals", {}).items():
            recs = res["detail"][addr]
            hits = res["counts"][addr]
            n_self = sum(1 for r in recs if r.get("self_relayed"))
            is_candidate = hits in target
            include = {
                "count_match": "count_match" in sig and is_candidate,
                "self_relayed": "self_relayed" in sig and "count_match" in sig,
                "gas_price": "gas_price" in sig,
                "linked": "linked" in sig,
                "linked_sender": "linked_sender" in sig,
                "shared_deposit": "shared_deposit" in sig,
                "early_profile": "early_profile" in sig,
            }
            for s in order:
                if not include[s]:
                    continue
                reason = {
                    "count_match": f"hit {hits}x = voucher size",
                    "self_relayed": f"{n_self}/{hits} withdrawals self-relayed",
                    "gas_price": "gas price equals a deposit's",
                    "linked": "direct counterparty of the depositor",
                    "linked_sender": _sender_summary(data, pool_key, addr, hits),
                    "shared_deposit": "sent to an exchange deposit address the depositor also used",
                    "early_profile": "received the wallet's full profile within 72 h",
                }[s]
                per[s].append(
                    {
                        "address": addr,
                        "pool_key": pool_key,
                        "denom": res["denom"],
                        "asset": res["asset"],
                        "hits": hits,
                        "reason": reason,
                        "confidence": res.get("confidence", {}).get(addr, 0.0),
                    }
                )
    out = []
    for s in order:
        rows = sorted(per[s], key=lambda r: (-r["confidence"], -r["hits"], r["address"]))
        out.append({"method": s, "label": SIGNAL_LABEL[s], "rows": rows})
    return out


# Evidence families. count_match, self_relayed, profile_match and early_profile
# read the same withdrawals, so they are one family.
METHOD_FAMILY = {
    "count_match": "amount+timing",
    "self_relayed": "amount+timing",
    "profile_match": "amount+timing",
    "early_profile": "amount+timing",
    "gas_price": "gas price",
    "linked": "linked address",
    "linked_late": "context",
    "linked_sender": "context",
    "shared_deposit": "context",
    "root_excluded": "context",
    "root_partial": "context",
}

# Shown as evidence, never scored, never a lead, never an independent family:
# a direct link first withdrawn later than EARLY_EXIT_HOURS (since version 2.17),
# and a withdrawal sent by the depositor's side (since version 2.19: in no sample
# did it separate from chance - 8 / 2, 14 / 11, 9 / 2 - and in the 2026 hold-out
# it was 11 of the 15 decoy moderate leads), and a shared exchange deposit address
# (since version 2.21: in the second hold-out, labelled shared deposits at 30 days
# were 18 target against 30 decoy leads on Ethereum, R 1.77 (0.16-3.91), and 23
# against 13 on Polygon, R 0.58 (0.00-3.16); not above chance).
CONTEXT_SIGNALS = frozenset({"linked_late", "linked_sender", "shared_deposit"})

# What is known about each context signal, in running text. The one source for every
# generated "how to read" text (a test pins that the keys equal CONTEXT_SIGNALS).
CONTEXT_VERDICT = {
    "linked_late": "did not beat chance on real depositors",
    "linked_sender": "did not beat chance on real depositors",
    "shared_deposit": (
        "not shown above chance at 30 days; post-hoc, untested "
        "(at 72 h: Ethereum 14/2, Polygon 23/0)"
    ),
}

# A note on a candidate, not evidence for it: some of its withdrawals were excluded by
# their Merkle root (tornado_demix.root_exclusion). Never scored, never a lead, never
# admits a candidate by itself.
NOTE_SIGNALS = frozenset({"root_excluded", "root_partial"})


# The score is uncalibrated and a percentage reads as a probability, so results
# are presented by band. A placebo test on real depositors (docs/EVALUATION.md:
# the same pipeline run on decoy windows that cannot hold the wallet's notes)
# found amount+timing and gas-price leads as frequent in decoy windows as in real
# ones; only a direct link within 72 h stood above chance, and so did the early
# multi-pool profile match (chance share 0.12, tools/placebo_profile.py). A band
# therefore needs one of these lead signals. Since version 2.16 only lead signals
# corroborate each other: among candidates with a lead, a count match did not
# lower the chance share (72 h: 4 real against 1 decoy; tools/placebo_strong.py),
# so amount+timing and gas price are shown as context and never raise the band.
BAND_ORDER = {"strong": 3, "moderate": 2, "weak": 1}

LINKED_SIGNALS = frozenset({"linked", "linked_sender", "shared_deposit"})
LEAD_SIGNALS = frozenset({"linked", "early_profile"})

# Independent sources of lead evidence; "strong" needs two of them.
LEAD_SOURCE = {
    "linked": "direct link",
    "early_profile": "early multi-pool profile",
}

# A linked exit this soon after the voucher's last deposit. Since version 2.17 a
# direct link (``linked``) is a lead only this early: on 302 random depositors a
# direct link first withdrawn within 72 h was a lead 26 times in real windows
# against 1 in decoy windows, a later one 8 against 13 (chance level), so a later
# one is ``linked_late``, context only. The early-exit mark on a row of a context-only
# linked signal (linked_sender, shared_deposit) shows when it is early.
EARLY_EXIT_HOURS = 72


def confidence_band(signals):
    """Return "strong", "moderate" or "weak" from the candidate's signal set.

    strong: lead signals from two independent sources (an early direct link, an
    early multi-pool profile match); moderate: a lead signal from one source;
    weak: amount+timing, gas price and/or context-only signals (late direct link,
    withdrawal sender, shared deposit address).
    Amount+timing and gas price never raise the band: on real depositors they
    are chance-level; next to a lead signal there are too few cases to judge,
    so they are not counted.
    """
    sources = {LEAD_SOURCE[s] for s in signals if s in LEAD_SOURCE}
    if len(sources) >= 2:
        return "strong"
    if sources:
        return "moderate"
    return "weak"


def band_rationale(band: str, signals: set[str]) -> str:
    """Plain-language reason for the band; never claims a signal the candidate lacks."""
    sources = sorted({LEAD_SOURCE[s] for s in signals if s in LEAD_SOURCE})
    if band == "strong":
        return f"two independent lead signals ({' and '.join(sources)})"
    if band == "moderate":
        return f"one lead signal ({sources[0]}); other evidence is context only"
    if "linked_sender" in signals:
        return (
            "no lead signal; a withdrawal sent by the depositor's side did not beat chance "
            "on real data, so it is context only"
        )
    if "shared_deposit" in signals:
        return (
            "no lead signal; a shared exchange deposit address is "
            f"{CONTEXT_VERDICT['shared_deposit']}; at 30 days 18 against 30 decoy leads on "
            "Ethereum and 23 against 13 on Polygon, so it is context only"
        )
    if "linked_late" in signals:
        return (
            f"no lead signal; a direct link first withdrawn more than {EARLY_EXIT_HOURS} h "
            "after the last deposit is chance-level on real data, so it is context only"
        )
    if "gas_price" in signals:
        return "gas price or amount+timing without a lead signal - chance-level on real data"
    return "amount+timing only - chance-level on real data"


def early_exit(data: dict, pool_key: str, first_ts: int) -> bool:
    """True when the exit's first withdrawal is within EARLY_EXIT_HOURS after a
    voucher's last deposit into the same pool."""
    limit = EARLY_EXIT_HOURS * 3600
    for v in data.get("vouchers", []):
        last = v.get("last_ts", v.get("first_ts"))
        if v.get("pool_key") == pool_key and last is not None and 0 <= first_ts - last <= limit:
            return True
    return False


def cross_method(data: dict, min_methods: int = 2) -> list[dict]:
    """Addresses selected by two or more *independent* methods.

    Independence is by family (:data:`METHOD_FAMILY`), not by signal name.
    ``n_methods`` counts families; ``methods`` lists every individual method of
    those families that fired. Context-only and note signals (family "context":
    a late link, the withdrawal sender, a shared deposit address, a Merkle-root
    note) are neither evidence nor independent, so they are left out of both.
    """
    agg = {}
    for m in method_breakdown(data):
        if METHOD_FAMILY.get(m["method"], m["method"]) == "context":
            continue
        for r in m["rows"]:
            key = (r["address"], r["pool_key"])
            e = agg.setdefault(
                key,
                {
                    "methods": [],
                    "reasons": [],
                    "families": set(),
                    "conf": 0.0,
                    "hits": r["hits"],
                    "denom": r["denom"],
                    "asset": r["asset"],
                },
            )
            e["methods"].append(m["label"])
            e["families"].add(METHOD_FAMILY.get(m["method"], m["method"]))
            e["reasons"].append(f"{m['label']}: {r['reason']}")
            e["conf"] = max(e["conf"], r.get("confidence", 0.0))
    out = []
    for (addr, pool_key), e in agg.items():
        if len(e["families"]) >= min_methods:
            out.append(
                {
                    "address": addr,
                    "pool_key": pool_key,
                    "denom": e["denom"],
                    "asset": e["asset"],
                    "methods": e["methods"],
                    "reasons": e["reasons"],
                    "n_methods": len(e["families"]),
                    "families": sorted(e["families"]),
                    "hits": e["hits"],
                    "confidence": e["conf"],
                }
            )
    out.sort(key=lambda r: (-r["n_methods"], -r["confidence"], r["address"]))
    return out


def candidate_kind(row: dict) -> str:
    """Which kind a ranked candidate is, the one classification every output uses (CLI
    summary, HTML report, web UI): ``lead`` (strong or moderate), ``context`` (weak with
    a context-only signal) or ``chance`` (weak on amount+timing or gas price alone)."""
    if row["band"] in ("strong", "moderate"):
        return "lead"
    if set(row["signals"]) & CONTEXT_SIGNALS:
        return "context"
    return "chance"


def profile_match_kind(fingerprint: dict) -> str:
    """Kind of a multi-wallet profile match, on the same scale as :func:`candidate_kind`:
    a wallet's full fingerprint over two or more pools is ``context``; a single-pool
    "fingerprint" is just the count match, so ``chance``."""
    return "context" if len(fingerprint or {}) >= 2 else "chance"


_KIND_PHRASE = {"context": "context-only", "chance": "chance-level"}


def conclusion(data: dict) -> str:
    """Summarise the outcome as labelled lines: the top candidate (or, with no lead,
    the tied weak candidates, named context-only or chance-level by
    :func:`candidate_kind`), the evidence families behind it, the
    candidate count per band, and the limits."""
    cands = ranked_candidates(data)
    lines = []
    if cands and cands[0]["band"] == "weak":
        # No lead signal anywhere: every candidate is weak (context only or
        # chance-level), and the score only orders signals that real depositors
        # and decoys show equally often. Say so instead of naming a "top" one.
        best = f"{cands[0]['confidence']:.2f}"
        tied = sorted(
            (c for c in cands if f"{c['confidence']:.2f}" == best),
            key=lambda c: (-c["hits"], c["address"]),
        )
        kinds = {candidate_kind(c) for c in tied}
        phrase = _KIND_PHRASE[kinds.pop()] if len(kinds) == 1 else "weak"
        lines.append(
            f"No lead. Highest-scoring {phrase} candidates ({len(tied)} tied at score {best}): "
            + ", ".join(f"{c['address']} ({c['pool_key']})" for c in tied[:5])
            + (f" and {len(tied) - 5} more" if len(tied) > 5 else "")
            + "."
        )
        lines.append(
            "Order within a tie: more withdrawals first, then address (ascending); "
            "the order carries no meaning."
        )
        lines.append(f"Basis of the first: {candidate_reason(tied[0])}")
        n_ctx = sum(1 for c in cands if candidate_kind(c) == "context")
        lines.append(
            f"Candidates: 0 strong, 0 moderate, {len(cands)} weak ({n_ctx} context only, "
            f"{len(cands) - n_ctx} chance-level)."
        )
    elif cands:
        top = cands[0]
        families = sorted(
            {
                METHOD_FAMILY.get(s, s)
                for s in top["signals"]
                if s not in CONTEXT_SIGNALS | NOTE_SIGNALS
            }
        )
        lines.append(
            f"Top candidate: {top['address']} ({top['pool_key']}), band {top['band']}, "
            f"score {top['confidence']:.2f}."
        )
        lines.append(f"Evidence families: {', '.join(families)} ({len(families)} independent).")
        lines.append(f"Basis: {candidate_reason(top)}")
        per_band = [
            f"{sum(c['band'] == band for c in cands)} {band}"
            for band in sorted(BAND_ORDER, key=BAND_ORDER.get, reverse=True)
        ]
        lines.append(f"Candidates: {', '.join(per_band)}.")
    else:
        lines.append("Top candidate: none; no count match narrows its field enough to count.")
    single = [v for v in data.get("vouchers", []) if v["count"] == 1]
    if single:
        lines.append(
            f"Single-note vouchers: {len(single)}. A count of one does not narrow the "
            f"field; trace the deposit funding source instead."
        )
    lines.append(
        "Limitations: leads, not proof. The score is uncalibrated and only orders "
        "candidates within a band; confirm any address with independent evidence "
        "(funding source, exchange KYC, further hops)."
    )
    return "\n".join(lines)


def ranked_candidates(
    data: dict,
    min_confidence: float = 0.0,
    limit: int | None = None,
    attribution: dict[str, dict] | None = None,
) -> list[dict]:
    """Return exit-address candidates, strongest band first, then by score.

    Each item: {address, pool_key, denom, asset, hits, confidence, signals,
    band, self_relayed, total_value, first_ts, last_ts, discrimination,
    field_size, evidence, attribution}. ``confidence`` is the noisy-OR score
    used for ordering; ``band`` and ``evidence`` (see
    :func:`candidate_evidence`) describe the evidence.

    ``attribution`` is an optional ``{address_lower: label_dict}`` map (see
    :func:`tornado_demix.attribution.load_attribution`). When given, each row's
    ``attribution`` is the one-line label of a known address (exchange, bridge,
    mixer, sanctioned...) and empty when the address is not in the set. Without a
    set every row's ``attribution`` is empty, so the field is always present and
    consumers need no special-casing.
    """
    rows = []
    for pool_key, res in data.get("denoms", {}).items():
        for addr, recs in res["detail"].items():
            hits = res["counts"][addr]
            sig = res.get("signals", {}).get(addr, [])
            conf = res.get("confidence", {}).get(addr, 0.0)
            # Admit an address on a wallet-specific signal, a voucher-sized count
            # that narrows the field, or the wallet's full multi-pool fingerprint.
            # Self-relaying is a property of the withdrawal, so it never admits alone.
            # A late direct link admits (it is about this wallet) but stays weak.
            disc = res.get("discrimination", {}).get(addr, 0.0)
            discriminating = bool(
                set(sig)
                & (LEAD_SIGNALS | CONTEXT_SIGNALS | {"gas_price", "count_match", "profile_match"})
            )
            if not discriminating or conf < min_confidence:
                continue
            rows.append(
                {
                    "address": addr,
                    "pool_key": pool_key,
                    "denom": res["denom"],
                    "asset": res["asset"],
                    "hits": hits,
                    "confidence": conf,
                    "signals": sig,
                    "band": confidence_band(set(sig)),
                    "self_relayed": sum(1 for r in recs if r.get("self_relayed")),
                    "total_value": round(sum(r["value"] for r in recs), 6),
                    "first_ts": min(r["ts"] for r in recs),
                    "last_ts": max(r["ts"] for r in recs),
                    "discrimination": round(disc, 4),
                    "field_size": res.get("unique_recipients") or len(res["counts"]),
                    "evidence": candidate_evidence(data, pool_key, addr),
                    "attribution": format_label(label_of(attribution, addr)) if attribution else "",
                    "fresh": data.get("fresh_addresses", {}).get(addr),
                    "early_exit": bool(set(sig) & LINKED_SIGNALS)
                    and early_exit(data, pool_key, min(r["ts"] for r in recs)),
                }
            )
    # Band first: the score orders leads within a band but cannot lift a
    # single-family lead above a corroborated one.
    rows.sort(key=lambda r: (-BAND_ORDER[r["band"]], -r["confidence"], -r["hits"], r["address"]))
    return rows[:limit] if limit else rows
