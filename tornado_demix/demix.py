"""Single-wallet Tornado.Cash demixing (amount + timing correlation).

detect_deposits finds the wallet's deposits, cluster_vouchers groups them per
pool and session, analyze_denom_events (or analyze_denom) counts the recipients
of in-window withdrawals, and run_demix ties it together and attaches the
candidate matches. Results are keyed by ``pool.key`` ('1 ETH', '10 AVAX#2').
"""

from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .constants import WEI
from .deposit_addresses import (
    PAID_EXCHANGE,
    depositor_deposit_addresses,
    own_token_transfers,
    shared_deposit_hits,
)
from .errors import ApiError, ApiKeyError, BlockLookupError, PermanentApiError, ProviderBlockedError
from .etherscan import EtherscanClient
from .events import fetch_withdrawals
from .groups import exit_groups
from .heuristics import (
    apply_heuristics,
    count_in_voucher_order,
    count_is_evidence,
    first_withdrawals,
    lead_link,
    link_shift,
    ranked_candidates,
)
from .incomplete import attach, lookup_failed, mark, pool_ambiguous, truncated_since
from .networks import Network, load_networks
from .pools import Pool
from .root_exclusion import apply_root_check
from .rpc import make_contract_check, make_gas_price_gate
from .txstatus import failed as _failed

# Direct counterparties whose own transactions are read for Tornado withdrawal
# calls (linked_sender). Busier counterparties are checked first.
MAX_SENDER_COUNTERPARTIES = 25
# A counterparty with this many transactions is a service (an exchange hot
# wallet); its history is not read: it would take thousands of requests and a
# service does not broadcast a depositor's withdrawals.
BUSY_COUNTERPARTY_TXS = 5000

# A native deposit may differ from the denomination by this fraction (wallets
# and routers are not always exact to the wei).
NATIVE_DENOM_TOLERANCE = 0.005


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _log_deposit_addresses(rows: list[dict]) -> None:
    """Log the depositor's deposit addresses; a counterparty that only paid a labelled
    exchange (``paid_exchange``) is not one and is counted apart."""
    paid = sum(1 for d in rows if d.get("kind") == PAID_EXCHANGE)
    _log("[*] exchange deposit addresses of the depositor: {}".format(len(rows) - paid))
    if paid:
        _log(f"[*] counterparties that paid a labelled exchange without a sweep (context): {paid}")


def _default_network():
    """Ethereum from the registry, used when a caller passes network=None."""
    return load_networks()["ethereum"]


def detect_deposits(
    client: EtherscanClient | None,
    wallet: str,
    network: Network | None = None,
    txs: list[dict] | None = None,
    token_txs: list[dict] | None = None,
    internal_txs: list[dict] | None = None,
    errors: list | None = None,
) -> list[dict]:
    """Return the wallet's Tornado deposits, native and ERC-20.

    A native deposit matches a denomination within 0.5 % and may arrive as an
    internal transfer (a Safe or other contract wallet); a token deposit matches
    exactly. Each dict: pool_key, denom, asset, ts, block, hash, to, via,
    gas_price. Pre-fetched ``txs``, ``token_txs`` and ``internal_txs`` save calls.
    A twin-pool receipt that cannot be read leaves ``pool_ambiguous`` and, when
    ``errors`` is a list, is appended to it.
    """
    network = network or _default_network()
    wallet = wallet.lower()
    # A None client means "analyse exactly the lists I handed you".
    if txs is None:
        txs = client.outgoing_txs(wallet) if client is not None else []
    if internal_txs is None:
        internal_txs = client.internal_txs(wallet) if client is not None else []

    deposits = []
    deposits.extend(_native_deposits(wallet, txs, network))
    deposits.extend(_native_deposits(wallet, internal_txs, network, via_suffix="-internal"))

    token_pools = [p for p in network.pools if not p.is_native]
    if token_pools:
        if token_txs is None:
            token_txs = client.token_transfers(wallet) if client is not None else []
        deposits.extend(_token_deposits(wallet, token_txs, token_pools, network.routers))
    _resolve_twin_pools(client, deposits, network.pools, errors)

    deposits.sort(key=lambda d: d["ts"])
    return deposits


def _native_deposits(wallet, txs, network, via_suffix=""):
    """Deposits paid in the chain's native currency.

    ``via_suffix`` labels rows found in the internal-transaction list.
    """
    native_pools = [p for p in network.pools if p.is_native]
    found = []
    for tx in txs:
        if _failed(tx):
            continue
        if (tx.get("from") or "").lower() != wallet:
            continue
        to = (tx.get("to") or "").lower()
        value = int(tx.get("value", "0")) / WEI
        if value <= 0:
            continue

        target = network.by_address.get(to)
        is_router = to in network.routers
        if target is None and not is_router:
            continue

        if target is not None:
            # Sent straight to a pool: that pool's own denomination decides.
            # Inferring it from the value would pick the wrong one of two pools
            # sharing a denomination (Avalanche runs two 10 AVAX pools).
            if not target.is_native:
                continue
            if abs(value - target.denom) > target.denom * NATIVE_DENOM_TOLERANCE:
                continue
            matched = target
        else:
            # Sent via a router: the pool can only be inferred from the value. Twin
            # pools share a denomination (Avalanche runs two 10 and two 100 AVAX
            # pools behind both routers): such a deposit carries ``pool_ambiguous``
            # until :func:`_resolve_twin_pools` reads the receipt's Deposit emitter.
            twins = sorted(
                (
                    p
                    for p in native_pools
                    if abs(value - p.denom) <= p.denom * NATIVE_DENOM_TOLERANCE
                ),
                key=lambda p: p.key,
            )
            if not twins:
                continue
            matched = twins[0]

        deposit = _deposit(
            matched, tx, to, ("pool" if target is not None else "router") + via_suffix
        )
        if target is None and len(twins) > 1:
            deposit["pool_ambiguous"] = [p.key for p in twins]
        found.append(deposit)
    return found


def _token_deposits(wallet, token_txs, token_pools, routers=()):
    """Deposits paid in an ERC-20 token.

    * straight to the pool: matched on (pool address, token contract, exact raw
      amount). The token must be checked too: a USDC transfer to the DAI pool is
      not a DAI deposit.
    * via a router: tokentx lists the router as ``to``, so the pool is inferred
      from (token contract, exact raw amount). Twin pools share that pair; such a
      deposit carries ``pool_ambiguous`` (the twins' keys) until
      :func:`_resolve_twin_pools` reads the receipt.
    """
    by_address = {p.address: p for p in token_pools}
    by_token_amount = {}
    for p in sorted(token_pools, key=lambda p: p.key):
        by_token_amount.setdefault((p.token, p.raw_denom), []).append(p)
    routers = {r.lower() for r in routers}
    found = []
    for tx in token_txs:
        if (tx.get("from") or "").lower() != wallet:
            continue
        to = (tx.get("to") or "").lower()
        contract = (tx.get("contractAddress") or "").lower()
        try:
            raw = int(tx.get("value", "0"))
        except (ValueError, TypeError):
            # A malformed amount from the API skips the row, not the whole scan.
            continue

        pool = by_address.get(to)
        if pool is not None:
            if contract != pool.token or raw != pool.raw_denom:
                continue
            via = "token"
        elif to in routers:
            twins = by_token_amount.get((contract, raw))
            if not twins:
                continue
            pool = twins[0]
            via = "token-router"
        else:
            continue

        deposit = _deposit(pool, tx, to, via)
        if via == "token-router" and len(twins) > 1:
            deposit["pool_ambiguous"] = [p.key for p in twins]
        found.append(deposit)
    return found


def _resolve_twin_pools(client, deposits, pools, errors=None):
    """Pick the right twin for router deposits from the Deposit event's emitter.

    Native and token pools alike, on every network. One receipt per transaction hash;
    twins are rare (three pairs on Ethereum, two on Avalanche). A deposit the receipt
    cannot settle keeps ``pool_ambiguous`` (and the first twin's key, for display
    only): when ``errors`` is a list, a ``pool_ambiguous`` record (see
    :func:`tornado_demix.incomplete.pool_ambiguous`) is appended for it, and
    :func:`cluster_vouchers` leaves it out, so it is never searched as if it were the
    first twin. A refusal that retrying cannot fix (:class:`PermanentApiError`) is
    recorded once and the remaining receipts are not asked for; a blocked provider is
    raised like a rejected key.
    """
    by_key = {p.key: p for p in pools}
    receipts = {}  # tx hash -> emitters (None for an unknown hash)
    refused = False
    for deposit in deposits:
        keys = deposit.get("pool_ambiguous")
        if not keys:
            continue
        tx = deposit["hash"]
        why = "no receipt read"
        readable = client is not None and hasattr(client, "deposit_emitters")
        if readable and tx not in receipts:
            if refused:
                why = "an earlier receipt lookup was refused"
            else:
                try:
                    receipts[tx] = client.deposit_emitters(tx)
                except (ApiKeyError, ProviderBlockedError):
                    raise
                except ApiError as exc:
                    if errors is not None:
                        errors.append(lookup_failed("deposit receipt (twin pool)", exc, tx=tx))
                    refused = isinstance(exc, PermanentApiError)
                    why = "receipt lookup failed"
        if readable and tx in receipts:
            emitters = receipts[tx]
            hit = [k for k in keys if by_key[k].address in (emitters or set())]
            if len(hit) == 1:
                pool = by_key[hit[0]]
                deposit.update(pool_key=pool.key, denom=pool.denom, asset=pool.asset)
                del deposit["pool_ambiguous"]
                continue
            why = (
                "receipt of an unknown transaction"
                if emitters is None
                else "{} of the candidate pools emitted a Deposit".format(len(hit))
            )
        if errors is not None:
            errors.append(pool_ambiguous(tx, keys, why))


def _deposit(pool, tx, to, via):
    """Build one deposit record from a matched pool and its transaction."""
    return {
        "pool_key": pool.key,
        "denom": pool.denom,
        "asset": pool.asset,
        "ts": int(tx["timeStamp"]),
        "block": int(tx["blockNumber"]),
        "hash": tx["hash"],
        "to": to,
        "via": via,
        "gas_price": int(tx.get("gasPrice", "0")),
    }


def native_counterparty_rows(wallet: str, txs: list[dict]) -> list[dict]:
    """The native ``txlist`` rows that show a relationship, for the direct counterparties.

    A failed transaction (``isError`` 1 or ``txreceipt_status`` 0) moved nothing and is
    dropped in both directions. An incoming transaction with value 0 is dropped too:
    anyone can send one to any address (address poisoners do it from look-alikes) and it
    carries nothing. An incoming transaction with value, even 1 wei, is kept, and so is
    an outgoing successful transaction whatever its value: both make a counterparty, but
    since version 2.23 only a link of real value (at least ``Network.dust_floor_wei``)
    made before the first withdrawal to the counterparty can be a lead (see
    :func:`direct_links`); the others are context."""
    wallet = wallet.lower()
    out = []
    for tx in txs:
        if _failed(tx):
            continue
        incoming = (tx.get("to") or "").lower() == wallet and (
            tx.get("from") or ""
        ).lower() != wallet
        if incoming and _int_value(tx) <= 0:
            continue
        out.append(tx)
    return out


def _int_value(tx: dict) -> int:
    try:
        return int(str(tx.get("value", "0")).strip() or "0")
    except ValueError:
        return 0


def wallet_counterparties(wallet: str, txs: list[dict]) -> set[str]:
    """Return the set of addresses the wallet directly transacted with.

    Used by the linked-address heuristic: a candidate exit address that the
    depositor also transacts with directly (outside Tornado) is a strong link.
    """
    wallet = wallet.lower()
    parties = set()
    for tx in txs:
        for side in ("from", "to"):
            addr = (tx.get(side) or "").lower()
            if addr and addr != wallet:
                parties.add(addr)
    return parties


def _int_field(tx: dict, key: str) -> int:
    try:
        return int(str(tx.get(key) or "0").strip() or "0")
    except ValueError:
        return 0


def _counterparty(wallet: str, tx: dict) -> str:
    """The other side of a row of ``wallet`` ("" when it is not between the wallet and
    another address)."""
    frm = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    if frm == wallet and to and to != wallet:
        return to
    if to == wallet and frm and frm != wallet:
        return frm
    return ""


def direct_links(
    wallet: str,
    native_rows: list[dict],
    internal_rows: list[dict],
    token_rows: list[dict],
    network: Network | None = None,
) -> dict[str, list[dict]]:
    """``{counterparty: [link, ...]}``: every row that makes a direct counterparty, with
    what it moved. Each link: ``tx``, ``ts``, ``block``, ``kind`` (native, internal,
    token), ``direction`` ("out": from the depositor, "in": to it), ``asset``, ``token``
    (contract address or None), ``raw`` (integer value), ``amount`` (in whole units, None
    for an unlisted token whose decimals are its own claim), ``qualifies`` and ``why``.

    ``qualifies`` (value that can make a lead) holds for a successful native or internal
    transfer of at least the network's dust floor (``Network.dust_floor_wei``) and for a
    transfer of a listed real token (``Network.real_tokens``) at or above its floor.
    Otherwise ``why`` says what is missing: ``zero_value``, ``dust`` or
    ``unverified_token``. Failed rows make no link at all. Whether the link came before
    the first withdrawal is decided by :func:`tornado_demix.heuristics.apply_heuristics`,
    which knows the withdrawals."""
    network = network or _default_network()
    wallet = wallet.lower()
    floor = network.dust_floor_wei
    tokens = network.real_tokens
    out: dict[str, list[dict]] = defaultdict(list)

    def add(tx, kind, asset, token, raw, amount, qualifies, why, decimals=None, floor_raw=None):
        frm = (tx.get("from") or "").lower()
        to = (tx.get("to") or "").lower()
        if frm == wallet and to and to != wallet:
            other, direction = to, "out"
        elif to == wallet and frm and frm != wallet:
            other, direction = frm, "in"
        else:
            return
        out[other].append(
            {
                "tx": tx.get("hash") or "",
                "ts": _int_field(tx, "timeStamp"),
                "block": _int_field(tx, "blockNumber"),
                "kind": kind,
                "direction": direction,
                "asset": asset,
                "token": token,
                "raw": raw,
                "amount": amount,
                "decimals": decimals,
                "floor_raw": floor_raw,
                "qualifies": qualifies,
                "why": why,
            }
        )

    for kind, rows in (("native", native_rows), ("internal", internal_rows)):
        for tx in rows:
            if _failed(tx):
                continue
            raw = _int_value(tx)
            why = None if raw >= floor else ("zero_value" if raw <= 0 else "dust")
            add(tx, kind, network.currency, None, raw, raw / WEI, why is None, why, 18, floor)
    for tx in token_rows:
        if _failed(tx):
            continue
        token = (tx.get("contractAddress") or "").lower()
        raw = _int_value(tx)
        info = tokens.get(token)
        if info is None:
            # the symbol is whatever the token contract says: kept apart, never as the asset
            claimed = str(tx.get("tokenSymbol") or "").strip()[:16]
            add(tx, "token", "unverified token", token, raw, None, False, "unverified_token")
            if out.get(_counterparty(wallet, tx)):
                out[_counterparty(wallet, tx)][-1]["claimed_symbol"] = claimed
            continue
        why = None if raw >= info["min_raw"] else ("zero_value" if raw <= 0 else "dust")
        amount = raw / 10 ** info["decimals"]
        add(
            tx,
            "token",
            info["symbol"],
            token,
            raw,
            amount,
            why is None,
            why,
            info["decimals"],
            info["min_raw"],
        )
    return dict(out)


def counterparty_links(data: dict, addresses) -> dict[str, dict]:
    """``{address: link}``: the transaction the reports cite behind each direct link of
    ``addresses``, from ``data["direct_links"]`` (:func:`direct_links`). The cited link is
    the earliest one that qualifies and came before the first withdrawal to that address
    (a lead's link); without one, the earliest qualifying link; without one, the
    earliest link. Each cited link adds ``position`` ("before", "after" or "unknown"
    relative to the first withdrawal to the address; "unknown" also for a link without a
    time), ``first_withdrawal_ts``, ``lead_seconds`` (how long before that withdrawal,
    for "before") and ``links`` (how many linking rows there are). In a decoy run the
    link is compared at its time minus the deposit shift (``heuristics.link_shift``);
    ``ts`` stays the real time."""
    links_by = data.get("direct_links") or {}
    firsts = first_withdrawals(data)
    shift = link_shift(data)
    out: dict[str, dict] = {}
    for addr in {a.lower() for a in addresses}:
        links = links_by.get(addr) or []
        if not links:
            continue
        first = firsts.get(addr)
        cited = lead_link(links, first, shift)
        if cited is None:
            real = [x for x in links if x["qualifies"]]
            cited = min(real or links, key=lambda x: (x["ts"], x["tx"]))
        at = cited["ts"] - shift
        if first is None or cited["ts"] <= 0:
            position = "unknown"
        else:
            position = "before" if at < first else "after"
        out[addr] = dict(
            cited,
            position=position,
            first_withdrawal_ts=first,
            lead_seconds=first - at if position == "before" else None,
            links=len(links),
        )
    return out


def withdrawal_senders(
    client, wallet, txs, results, network, is_contract, labels=None, errors=None
):
    """{withdrawal tx hash: sender} for withdrawals sent by the wallet or a direct counterparty.

    Only non-contract, non-busy counterparties are read, at most MAX_SENDER_COUNTERPARTIES,
    and only within the searched block ranges, so the cost stays bounded. A counterparty
    whose busy check or history read fails is skipped, not fatal; when ``errors`` is a
    list the failure is appended to it (:mod:`tornado_demix.incomplete`), so "no linked
    sender" is never reported for a counterparty that was not read. Failed rows and
    zero-value incoming rows (spam) do not make a counterparty here.
    """
    wallet = wallet.lower()
    hashes = {
        (r.get("hash") or "").lower()
        for res in results.values()
        for recs in res.get("detail", {}).values()
        for r in recs
    }
    # The searched block ranges, merged: reading one span from the first to the
    # last window would cover the months between vouchers as well.
    spans = sorted(
        (w["start_block"], w["end_block"])
        for res in results.values()
        for w in res.get("windows") or []
        if w.get("start_block") is not None and w.get("end_block") is not None
    ) or sorted(tuple(res["window_blocks"]) for res in results.values() if res.get("window_blocks"))
    merged = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    if not hashes or not merged:
        return {}
    tornado = {p.address for p in network.pools} | set(network.routers)
    seen = Counter()
    for tx in txs:
        # A failed row moved nothing, and a zero-value incoming row costs a spammer
        # nothing: neither may take one of the MAX_SENDER_COUNTERPARTIES slots.
        if _failed(tx):
            continue
        if (tx.get("to") or "").lower() == wallet and _int_value(tx) <= 0:
            continue
        for side in ("from", "to"):
            addr = (tx.get(side) or "").lower()
            if addr and addr != wallet and addr not in tornado:
                seen[addr] += 1
    busy = getattr(client, "has_at_least_txs", None)

    def senders():
        """The wallet, then up to MAX_SENDER_COUNTERPARTIES non-contract, non-busy
        counterparties, busiest first. Checked one by one and stopped at the cap:
        a wallet can have thousands of counterparties, each a lookup."""
        yield wallet
        taken = 0
        for addr, _n in seen.most_common():
            if taken >= MAX_SENDER_COUNTERPARTIES:
                return
            if labels and addr in labels:
                continue  # a labelled exchange, bridge or protocol: no history read
            if is_contract(addr):
                continue
            try:
                too_busy = busy is not None and busy(addr, BUSY_COUNTERPARTY_TXS)
            except ApiKeyError:
                raise
            except ApiError as exc:
                # Some explorers refuse a page this large for a busy address;
                # an unchecked counterparty is skipped, not fatal to the run.
                _log("  [linked_sender] could not check counterparty {}, skipped".format(addr))
                if errors is not None:
                    errors.append(lookup_failed("linked_sender busy check", exc, address=addr))
                continue
            if too_busy:
                _log("  [linked_sender] skipped busy counterparty {}".format(addr))
                continue
            taken += 1
            yield addr

    found = {}
    for addr in senders():
        fetch = getattr(client, "fetch_all", None)
        try:
            if fetch:
                rows = [tx for lo, hi in merged for tx in fetch("txlist", addr, lo, hi)]
            else:
                rows = client.outgoing_txs(addr)
        except ApiKeyError:
            raise
        except ApiError as exc:
            if addr == wallet:
                raise  # the depositor's own history is required
            _log("  [linked_sender] could not read counterparty {}, skipped".format(addr))
            if errors is not None:
                errors.append(lookup_failed("linked_sender history", exc, address=addr))
            continue
        for tx in rows:
            h = (tx.get("hash") or "").lower()
            if _failed(tx):
                continue
            if (tx.get("from") or "").lower() == addr and h in hashes:
                found[h] = addr
    return found


def cluster_vouchers(deposits, gap_hours=24, max_span_hours=None):
    """Group deposits into the same pool that are close in time.

    Deposits into one pool separated by more than ``gap_hours`` start a new
    voucher; so does a deposit more than ``max_span_hours`` after the voucher's
    first one, which stops a chain of close deposits from spanning days.
    Returns a list of voucher dicts sorted by (pool_key, first_ts).
    """
    by_pool = defaultdict(list)
    for deposit in deposits:
        if deposit.get("pool_ambiguous"):
            # Pool uncertain among twins: searching twins[0] could look in the wrong
            # pool. The deposit stays listed and is recorded (incomplete.pool_ambiguous).
            continue
        by_pool[deposit["pool_key"]].append(deposit)

    vouchers = []
    for pool_key, items in by_pool.items():
        items.sort(key=lambda d: d["ts"])
        cluster = [items[0]]
        for deposit in items[1:]:
            within_span = max_span_hours is None or (
                deposit["ts"] - cluster[0]["ts"] <= max_span_hours * 3600
            )
            if deposit["ts"] - cluster[-1]["ts"] <= gap_hours * 3600 and within_span:
                cluster.append(deposit)
            else:
                vouchers.append(_make_voucher(pool_key, cluster))
                cluster = [deposit]
        vouchers.append(_make_voucher(pool_key, cluster))

    vouchers.sort(key=lambda v: (v["pool_key"], v["first_ts"]))
    return vouchers


def _make_voucher(pool_key, cluster):
    first = cluster[0]
    return {
        "pool_key": pool_key,
        "denom": first["denom"],
        "asset": first["asset"],
        "count": len(cluster),
        "first_ts": first["ts"],
        "last_ts": cluster[-1]["ts"],
        "first_block": first["block"],
        "deposits": cluster,
    }


def voucher_windows(
    vouchers: list[dict], window_days: int, exit_window_hours: float | None = None
) -> list[dict]:
    """Return one search window per voucher, merging any that overlap.

    A wallet that used a pool twice, years apart, is two events; one window
    spanning both would make every withdrawal in between a candidate.

    ``exit_window_hours``, when positive, ends each window that many hours after
    the voucher's last deposit instead of ``window_days`` days. A tight window
    cuts the recipient field so a count match discriminates, at the cost of
    missing slower exits.

    Each window: {first_ts, end_ts, voucher_count}.
    """
    forward = (
        exit_window_hours * 3600
        if exit_window_hours and exit_window_hours > 0
        else window_days * 86400
    )
    spans = sorted(
        (
            {"first_ts": v["first_ts"], "end_ts": v["last_ts"] + forward, "voucher_count": 1}
            for v in vouchers
        ),
        key=lambda w: w["first_ts"],
    )
    merged = []
    for span in spans:
        if merged and span["first_ts"] <= merged[-1]["end_ts"]:
            merged[-1]["end_ts"] = max(merged[-1]["end_ts"], span["end_ts"])
            merged[-1]["voucher_count"] += 1
        else:
            merged.append(dict(span))
    return merged


MAX_FUNDERS = 3  # earliest plain senders kept per wallet


def immediate_funders(wallet, txs, before_ts, pool_addresses=()):
    """Addresses that funded ``wallet`` by a plain native transfer before ``before_ts``.

    Only transfers without call data count, so a contract payout is not read as a
    funder. Earliest first, at most MAX_FUNDERS.
    """
    skip = {a.lower() for a in pool_addresses} | {wallet}
    funders = []
    for tx in sorted(txs, key=lambda t: int(t.get("timeStamp") or 0)):
        if int(tx.get("timeStamp") or 0) >= before_ts:
            break
        sender = (tx.get("from") or "").lower()
        if (
            (tx.get("to") or "").lower() != wallet
            or _failed(tx)
            or int(tx.get("value") or 0) <= 0
            or (tx.get("input") or "0x") not in ("", "0x")
            or sender in skip
            or sender in funders
        ):
            continue
        funders.append(sender)
        if len(funders) == MAX_FUNDERS:
            break
    return funders


# An exit address with no more than this much history before its first
# withdrawal is a fresh, disposable one (MixLaunder: 98.6 % of laundering exits).
FRESH_HISTORY_HOURS = 24
FRESH_CHECK_LIMIT = 10


def fresh_addresses(client, data, limit=FRESH_CHECK_LIMIT, errors=None):
    """Check how much history the top candidates had before their first withdrawal.

    Returns {address: {first_activity_ts, first_withdrawal_ts, history_hours,
    fresh}}. Informational only: a fresh address is typical of laundering exits
    but also of any new wallet, so it never enters the score or the band. A failed
    lookup leaves the address out and, when ``errors`` is a list, is appended to it.
    """
    if not hasattr(client, "first_activity"):
        return {}
    first_withdrawal = {}
    for row in ranked_candidates(data):
        addr = row["address"]
        first_withdrawal[addr] = min(first_withdrawal.get(addr, row["first_ts"]), row["first_ts"])
        if len(first_withdrawal) >= limit:
            break
    out = {}
    for addr, withdrawn in first_withdrawal.items():
        try:
            first = client.first_activity(addr)
        except ApiKeyError:
            raise
        except ApiError as exc:
            _log("  [!] first activity of {} not read: {}".format(addr, exc))
            if errors is not None:
                errors.append(lookup_failed("first activity", exc, address=addr))
            continue
        first = withdrawn if first is None else min(first, withdrawn)
        history = (withdrawn - first) / 3600
        out[addr] = {
            "first_activity_ts": first,
            "first_withdrawal_ts": withdrawn,
            "history_hours": round(history, 1),
            "fresh": history <= FRESH_HISTORY_HOURS,
        }
    return out


def _resolve_window_end(client, end_ts, notes=None):
    """Resolve a window's end timestamp to a block, never past the chain head.

    For a recent deposit the window ends in the future, and block-by-time with
    closest="after" rejects a future timestamp. Such an end is clamped to the
    current block instead of skipping the pool. A past end whose lookup failed falls
    back to the current block too (a wider window); when ``notes`` is a list that
    fallback is recorded there (kind ``window_end_fallback``).
    """
    if end_ts > time.time():
        return client.current_block()
    try:
        return client.block_by_time(end_ts, "after")
    except BlockLookupError as exc:
        if notes is not None:
            notes.append(
                lookup_failed(
                    "window end block (searched up to the current block instead)",
                    exc,
                    kind="window_end_fallback",
                    timestamp=end_ts,
                )
            )
        return client.current_block()


def _resolve_windows(client, pool, windows, notes=None):
    """Attach start_block / end_block to each window, logging the range."""
    for window in windows:
        window["start_block"] = client.block_by_time(window["first_ts"], "before")
        # A lagging current-block answer must not produce an empty, inverted range.
        window["end_block"] = max(
            _resolve_window_end(client, window["end_ts"], notes), window["start_block"]
        )
        _log(
            "  [{}] pool {} | blocks {}..{} ({:%Y-%m-%d} .. {:%Y-%m-%d})".format(
                pool.key,
                pool.address,
                window["start_block"],
                window["end_block"],
                datetime.fromtimestamp(window["first_ts"], tz=timezone.utc),
                datetime.fromtimestamp(window["end_ts"], tz=timezone.utc),
            )
        )


def analyze_denom(
    client: EtherscanClient,
    pool: Pool,
    vouchers_for_pool: list[dict],
    window_days: int,
    fee_lo: float,
    fee_hi: float,
    exit_window_hours: float | None = None,
    notes=None,
) -> dict:
    """Export in-window withdrawals for one pool and count recipients.

    Legacy mode that reads raw internal transfers from the pool. A withdrawal
    qualifies if the recipient received ``denom * fee_lo`` .. ``denom * fee_hi``
    (the denomination minus a relayer fee). Windows as in :func:`voucher_windows`.
    """
    low_value = pool.denom * fee_lo
    high_value = pool.denom * fee_hi

    windows = voucher_windows(vouchers_for_pool, window_days, exit_window_hours)
    _resolve_windows(client, pool, windows, notes)
    internal = []
    for window in windows:
        internal.extend(
            client.internal_from(pool.address, window["start_block"], window["end_block"])
        )
    _log(
        "  [{}] internal txs from pool across {} window(s): {}".format(
            pool.key, len(windows), len(internal)
        )
    )

    recipients = []  # (address, value, ts, hash)
    seen = set()
    for item in internal:
        if (item.get("from") or "").lower() != pool.address:
            continue
        if _failed(item):
            continue
        to = (item.get("to") or "").lower()
        value = pool.to_units(int(item.get("value", "0")))
        # Windows can share a boundary block. Raw transfers carry no nullifier,
        # so two identical transfers to one recipient in one tx still collapse.
        key = (item["hash"], to, value)
        if key in seen:
            continue
        if low_value <= value <= high_value:
            seen.add(key)
            recipients.append((to, value, int(item["timeStamp"]), item["hash"]))

    counts = Counter(address for address, *_ in recipients)
    detail = defaultdict(list)
    for address, value, ts, tx_hash in recipients:
        detail[address].append({"value": value, "ts": ts, "hash": tx_hash})

    return {
        "pool_key": pool.key,
        "denom": pool.denom,
        "asset": pool.asset,
        "decimals": pool.decimals,
        "pool": pool.address,
        "mode": "transfers",
        # The envelope is for display; the analysis used ``windows``.
        "windows": windows,
        "window_ts": [windows[0]["first_ts"], windows[-1]["end_ts"]],
        "window_blocks": [windows[0]["start_block"], windows[-1]["end_block"]],
        "fee_window": [low_value, high_value],
        "total_qualifying_withdrawals": len(recipients),
        "unique_recipients": len(counts),
        "counts": counts,
        "detail": detail,
    }


def analyze_denom_events(
    client: EtherscanClient,
    pool: Pool,
    vouchers_for_pool: list[dict],
    window_days: int,
    exit_window_hours: float | None = None,
    notes=None,
) -> dict:
    """Exact, log-based variant of :func:`analyze_denom`.

    Reads the pool's ``Withdrawal`` events, so recipient, relayer and fee come
    from the contract rather than from a value band. This also sees zero-fee
    withdrawals, which fall outside the transfers-mode fee window.
    """
    windows = voucher_windows(vouchers_for_pool, window_days, exit_window_hours)
    _resolve_windows(client, pool, windows, notes)
    withdrawals = []
    for window in windows:
        withdrawals.extend(
            fetch_withdrawals(client, pool.address, window["start_block"], window["end_block"])
        )

    # Windows can share a boundary block. The nullifier identifies a withdrawal
    # (the contract enforces uniqueness), so only a re-fetched event collapses.
    seen, unique = set(), []
    for w in withdrawals:
        key = (w["tx_hash"], w["nullifier"])
        if key not in seen:
            seen.add(key)
            unique.append(w)
    withdrawals = unique
    _log(
        "  [{}] Withdrawal events across {} window(s): {}".format(
            pool.key, len(windows), len(withdrawals)
        )
    )

    for w in withdrawals:
        w["fee"] = pool.to_units(w["fee_wei"])
        w["asset"] = pool.asset
        w["pool_key"] = pool.key

    counts = Counter(w["to"] for w in withdrawals)
    detail = defaultdict(list)
    for w in withdrawals:
        detail[w["to"]].append(
            {
                # The recipient receives the denomination minus the relayer fee.
                "value": pool.denom - w["fee"],
                "ts": w["ts"],
                "hash": w["tx_hash"],
                "block": w["block"],
                "relayer": w["relayer"],
                "fee": w["fee"],
                "asset": pool.asset,
                "self_relayed": w["self_relayed"],
                "gas_price": w.get("gas_price", 0),
            }
        )

    return {
        "pool_key": pool.key,
        "denom": pool.denom,
        "asset": pool.asset,
        "decimals": pool.decimals,
        "pool": pool.address,
        "mode": "events",
        # The envelope is for display; the analysis used ``windows``.
        "windows": windows,
        "window_ts": [windows[0]["first_ts"], windows[-1]["end_ts"]],
        "window_blocks": [windows[0]["start_block"], windows[-1]["end_block"]],
        "fee_window": [None, None],  # not applicable in event mode
        "total_qualifying_withdrawals": len(withdrawals),
        "unique_recipients": len(counts),
        "counts": counts,
        "detail": detail,
        "withdrawals": withdrawals,
    }


def _set_count_candidates(res, vouchers, order_params, pool_key):
    """Set ``target_counts`` and ``candidates_by_count`` of one pool result."""
    sizes = [v["count"] for v in vouchers if v["pool_key"] == pool_key]
    # Several sessions into one pool consolidated to one address show up as the
    # sum of the vouchers, not as any single voucher size.
    target_counts = sorted(set(sizes) | ({sum(sizes)} if len(sizes) > 1 else set()))
    res["target_counts"] = target_counts
    # A count only fits a voucher when the withdrawals fall in that voucher's
    # own window (a pool's windows are merged into one field).
    res["candidates_by_count"] = {
        n: (
            [
                addr
                for addr, c in res["counts"].items()
                if c == n
                and count_in_voucher_order(
                    vouchers,
                    order_params,
                    pool_key,
                    [r["ts"] for r in res["detail"].get(addr, [])],
                )
            ]
            if count_is_evidence(res, n)
            else []
        )
        for n in target_counts
    }


def score_collected(result: dict, counterparties: set[str], heuristics_args: dict | None = None):
    """Re-score an already collected run: the count-candidate gate, then every signal.

    Works on the data alone (no explorer calls), so it is the point where a run whose
    withdrawals changed (root exclusion, a planted lead in tools/power) is recomputed
    by the same code that scored it. ``heuristics_args`` are the keyword arguments
    of :func:`apply_heuristics` (gas gate, contract check, senders, shared deposits).
    """
    params = result.get("params", {})
    order_params = {
        "window_days": params.get("window_days", 30),
        "exit_window_hours": params.get("exit_window_hours"),
    }
    for pool_key, res in result.get("denoms", {}).items():
        _set_count_candidates(res, result.get("vouchers", []), order_params, pool_key)
    return apply_heuristics(result, counterparties, **(heuristics_args or {}))


def run_demix(
    client: EtherscanClient,
    wallet: str,
    window_days: int = 30,
    fee_lo: float = 0.90,
    fee_hi: float = 0.995,
    gap_hours: float = 24,
    mode: str = "events",
    network: Network | None = None,
    exit_window_hours: float | None = None,
    max_voucher_span_hours: float | None = None,
    known_exits=(),
    labels: dict[str, dict] | None = None,
    deposit_shift_seconds: int = 0,
    deposit_addresses: bool = True,
    root_check: bool = False,
    root_cache_dir: str | None = None,
) -> dict:
    """Full single-wallet demix. Returns a structured result dict.

    ``result['denoms']`` maps a pool key ('1 ETH') to that pool's analysis.
    ``exit_window_hours`` narrows the withdrawal search to that many hours after
    each deposit instead of ``window_days`` days (see :func:`voucher_windows`);
    ``None`` keeps the full window.
    ``deposit_shift_seconds`` moves every detected deposit that many seconds back
    in time before anything else runs: the search windows then end earlier, which
    is how a decoy window is built (see :mod:`tornado_demix.placebo`). ``0`` is a
    normal run.
    ``deposit_addresses`` switches the exchange-deposit-address lookup on or off (it
    costs up to ~50 explorer calls, see :mod:`tornado_demix.deposit_addresses`);
    off, ``result['deposit_addresses']`` is empty and the signal never fires.
    ``root_check`` reads the Merkle root of each candidate withdrawal and drops those
    whose root predates every one of the wallet's notes in the pool (see
    :mod:`tornado_demix.root_exclusion`); off by default. ``root_cache_dir`` holds the
    per-pool root indexes (default ``.cache/merkle``; the first build of a large pool
    takes about half an hour).
    Every result carries ``errors`` (failed lookups: a counterparty, busy, contract,
    base-fee or first-activity check that did not answer; see
    :mod:`tornado_demix.incomplete`), ``error_count`` and ``truncated`` (lists the
    explorer cut off during this run). A failed lookup is never "nothing found".
    """
    network = network or _default_network()
    wallet = wallet.lower()
    # A non-positive exit window means a full-window run, in params and report too.
    if exit_window_hours is not None and exit_window_hours <= 0:
        exit_window_hours = None
    _log("[*] Wallet: {} on {}".format(wallet, network.name))
    # Failed lookups and cut-off lists of THIS run (see tornado_demix.incomplete).
    errors: list[dict] = []
    start_mark = mark(client)
    read_ts = int(time.time())  # what the windows were clamped to (params.read_ts)

    all_txs = client.outgoing_txs(wallet)  # txlist returns both directions
    internal_txs = client.internal_txs(wallet)
    # Token transfers are read once, on every network: for token-pool deposits, so that
    # a direct ERC-20 transfer between the depositor and a candidate earns linked too,
    # and for USDC/USDT deposit addresses of the depositor on chains without token pools.
    # (A stub client without a token list reads none.)
    fetch_tokens = getattr(client, "token_transfers", None)
    token_txs = fetch_tokens(wallet) if fetch_tokens is not None else []
    # Only token rows the depositor really took part in make a direct counterparty: a
    # spoofed, zero-value or fake-token Transfer can name any address at any time
    # (see deposit_addresses.own_token_transfers); a 0-value or failed native tx
    # carries nothing either (native_counterparty_rows).
    native_rows = native_counterparty_rows(wallet, all_txs)
    internal_rows = [tx for tx in internal_txs if not _failed(tx)]
    token_rows = own_token_transfers(wallet, all_txs, token_txs)
    # What each link moved, and when: a lead needs real value before the first
    # withdrawal (heuristics.apply_heuristics reads result["direct_links"]). The direct
    # counterparties are exactly the addresses with a link record: a row in which the
    # depositor is neither sender nor recipient (a contract-creation internal row) links
    # nobody.
    links = direct_links(wallet, native_rows, internal_rows, token_rows, network)
    counterparties = set(links)
    deposits = detect_deposits(
        client,
        wallet,
        network=network,
        txs=all_txs,
        internal_txs=internal_txs,
        token_txs=token_txs,
        errors=errors,
    )
    if deposit_shift_seconds > 0:
        deposits = [dict(d, ts=d["ts"] - deposit_shift_seconds) for d in deposits]
    _log("[*] Tornado deposits detected: {}".format(len(deposits)))
    if not deposits:
        empty = {
            "wallet": wallet,
            "deposits": [],
            "vouchers": [],
            "denoms": {},
            "unresolved": [],
            "counterparties": len(counterparties),
            "params": {
                "network": network.name,
                "currency": network.currency,
                "mode": mode,
                "window_days": window_days,
                "gap_hours": gap_hours,
                "exit_window_hours": exit_window_hours,
                "deposit_shift_seconds": deposit_shift_seconds,
            },
        }
        return attach(empty, errors, truncated_since(client, start_mark))

    vouchers = cluster_vouchers(
        deposits, gap_hours=gap_hours, max_span_hours=max_voucher_span_hours
    )
    for voucher in vouchers:
        _log(
            "    voucher: {} x {} @ {:%Y-%m-%d %H:%M} UTC".format(
                voucher["count"],
                voucher["pool_key"],
                datetime.fromtimestamp(voucher["first_ts"], tz=timezone.utc),
            )
        )

    results = {}
    unresolved = []
    for pool_key in sorted({v["pool_key"] for v in vouchers}):
        pool = network.by_key[pool_key]
        vouchers_for_pool = [v for v in vouchers if v["pool_key"] == pool_key]
        try:
            if mode == "events":
                results[pool_key] = analyze_denom_events(
                    client, pool, vouchers_for_pool, window_days, exit_window_hours, errors
                )
            else:
                results[pool_key] = analyze_denom(
                    client,
                    pool,
                    vouchers_for_pool,
                    window_days,
                    fee_lo,
                    fee_hi,
                    exit_window_hours,
                    errors,
                )
        except BlockLookupError as exc:
            # Skipping this pool loses one lead; searching the whole chain
            # instead would produce candidates with no timing basis.
            _log("  [{}] SKIPPED - {}".format(pool_key, exc))
            unresolved.append({"pool_key": pool_key, "reason": str(exc)})

    order_params = {"window_days": window_days, "exit_window_hours": exit_window_hours}
    # Candidates: recipients whose count equals a voucher size. The gate is
    # applied once, here, so every consumer sees the same candidate set.
    for pool_key, res in results.items():
        _set_count_candidates(res, vouchers, order_params, pool_key)

    result = {
        "wallet": wallet,
        # Read by the power check (tornado_demix.power) to plant a link in a decoy run.
        "counterparty_addresses": sorted(counterparties),
        "direct_links": links,
        "funders": immediate_funders(
            wallet, all_txs, min(d["ts"] for d in deposits), [p.address for p in network.pools]
        ),
        "params": {
            "window_days": window_days,
            "fee_window": [fee_lo, fee_hi],
            "gap_hours": gap_hours,
            "mode": mode,
            "network": network.name,
            "currency": network.currency,
            "exit_window_hours": exit_window_hours,
            "read_ts": read_ts,
            "deposit_shift_seconds": deposit_shift_seconds,
            # Count matches are checked against each voucher's own window.
            "voucher_order": True,
        },
        "deposits": deposits,
        "vouchers": vouchers,
        "denoms": results,
        "unresolved": unresolved,
    }

    is_contract = make_contract_check(network.rpc_url)
    deposit_rows: list[dict] = []
    deposit_hits: dict[str, list[str]] = {}
    context_hits: dict[str, list[str]] = {}
    if deposit_addresses:
        deposit_rows = depositor_deposit_addresses(
            client,
            wallet,
            all_txs + token_txs,
            is_contract,
            labels,
            exclude={p.address for p in network.pools} | set(network.routers),
            errors=errors,
            gas_fund_wei=network.gas_fund_wei,
            floors=network.sender_floors,
        )
        _log_deposit_addresses(deposit_rows)
        recipients = {addr for res in results.values() for addr in res["detail"]}
        deposit_hits = shared_deposit_hits(deposit_rows, recipients, wallet)
        # Deposit addresses found by activity alone (no exchange label on the hot
        # wallet) were chance-level in the placebo test: their matches are context.
        context_hits = {
            r: addrs
            for r, addrs in shared_deposit_hits(
                [d for d in deposit_rows if not d.get("evidence", True)],
                recipients,
                wallet,
                evidence_only=False,
            ).items()
        }
    gas_gate = make_gas_price_gate(network.rpc_url, log=_log)
    heuristics_args = {
        "gas_gate": gas_gate,
        "is_contract": is_contract,
        "withdrawal_senders": withdrawal_senders(
            client, wallet, all_txs + internal_txs, results, network, is_contract, labels, errors
        ),
        "shared_deposits": deposit_hits,
    }
    # The cut-off lists so far: the early profile needs to know whether a pool's withdrawals are whole.
    result["truncated"] = truncated_since(client, start_mark)
    apply_heuristics(result, counterparties, **heuristics_args)

    def cite():
        # The transaction behind each direct link the evidence cites: linked and
        # context-linked candidates and the depositor-side senders of withdrawals.
        h = result["heuristics"]
        cited = (
            {a for _pk, a in h["linked_addresses"]}
            | {a for _pk, a, _s in h["linked_context"]}
            | {s for _p, _a, s, _t in h["linked_senders"]}
        )
        result["counterparty_links"] = counterparty_links(result, cited)

    cite()

    def refresh():
        score_collected(result, counterparties, heuristics_args)
        cite()

    if root_check:
        apply_root_check(client, result, network, refresh, root_cache_dir, errors=errors)
    # A counterparty whose contract status is unknown never makes a lead; when the check
    # itself recorded no failure (no RPC endpoint), the unknown status is recorded here.
    recorded = {e.get("address") for e in getattr(is_contract, "errors", [])}
    for addr in result["heuristics"].get("linked_unknown_contracts", []):
        if addr not in recorded:
            errors.append(
                lookup_failed("contract check (eth_getCode)", "no RPC endpoint", address=addr)
            )
    result["deposit_addresses"] = deposit_rows
    result["deposit_context_hits"] = context_hits
    result["fresh_addresses"] = fresh_addresses(client, result, errors=errors)
    # Exits withdrawn in joint bursts; anchored by a corroborated candidate or a
    # known exit. Context only, like the fresh mark.
    result["exit_groups"] = exit_groups(result, ranked_candidates(result), known_exits)
    errors += getattr(is_contract, "errors", [])
    errors += getattr(gas_gate, "errors", [])
    return attach(result, errors, truncated_since(client, start_mark))
