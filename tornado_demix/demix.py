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
from .errors import BlockLookupError
from .etherscan import EtherscanClient
from .events import fetch_withdrawals
from .heuristics import apply_heuristics, count_is_evidence
from .networks import Network, load_networks
from .pools import Pool
from .rpc import make_contract_check, make_gas_price_gate

# A native deposit may differ from the denomination by this fraction (wallets
# and routers are not always exact to the wei).
NATIVE_DENOM_TOLERANCE = 0.005


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


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
) -> list[dict]:
    """Return the wallet's Tornado deposits, native and ERC-20.

    A native deposit matches a denomination within 0.5 % and may arrive as an
    internal transfer (a Safe or other contract wallet); a token deposit matches
    exactly. Each dict: pool_key, denom, asset, ts, block, hash, to, via,
    gas_price. Pre-fetched ``txs``, ``token_txs`` and ``internal_txs`` save calls.
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
            token_txs = client.token_transfers(wallet)
        deposits.extend(_token_deposits(wallet, token_txs, token_pools, network.routers))

    deposits.sort(key=lambda d: d["ts"])
    return deposits


def _native_deposits(wallet, txs, network, via_suffix=""):
    """Deposits paid in the chain's native currency.

    ``via_suffix`` labels rows found in the internal-transaction list.
    """
    native_pools = [p for p in network.pools if p.is_native]
    found = []
    for tx in txs:
        if tx.get("isError") == "1":
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
            # Sent via a router: the pool can only be inferred from the value.
            matched = None
            for pool in native_pools:
                if abs(value - pool.denom) <= pool.denom * NATIVE_DENOM_TOLERANCE:
                    matched = pool
                    break
            if matched is None:
                continue

        found.append(
            _deposit(matched, tx, to, ("pool" if target is not None else "router") + via_suffix)
        )
    return found


def _token_deposits(wallet, token_txs, token_pools, routers=()):
    """Deposits paid in an ERC-20 token.

    * straight to the pool: matched on (pool address, token contract, exact raw
      amount). The token must be checked too: a USDC transfer to the DAI pool is
      not a DAI deposit.
    * via a router: tokentx lists the router as ``to``, so the pool is inferred
      from (token contract, exact raw amount), which is unique among token pools.
    """
    by_address = {p.address: p for p in token_pools}
    by_token_amount = {(p.token, p.raw_denom): p for p in token_pools}
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
            pool = by_token_amount.get((contract, raw))
            if pool is None:
                continue
            via = "token-router"
        else:
            continue

        found.append(_deposit(pool, tx, to, via))
    return found


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


def cluster_vouchers(deposits, gap_hours=24):
    """Group deposits into the same pool that are close in time.

    Deposits into one pool separated by more than ``gap_hours`` start a new
    voucher. Returns a list of voucher dicts sorted by (pool_key, first_ts).
    """
    by_pool = defaultdict(list)
    for deposit in deposits:
        by_pool[deposit["pool_key"]].append(deposit)

    vouchers = []
    for pool_key, items in by_pool.items():
        items.sort(key=lambda d: d["ts"])
        cluster = [items[0]]
        for deposit in items[1:]:
            if deposit["ts"] - cluster[-1]["ts"] <= gap_hours * 3600:
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


def _resolve_window_end(client, end_ts):
    """Resolve a window's end timestamp to a block, never past the chain head.

    For a recent deposit the window ends in the future, and block-by-time with
    closest="after" rejects a future timestamp. Such an end is clamped to the
    current block instead of skipping the pool.
    """
    if end_ts > time.time():
        return client.current_block()
    try:
        return client.block_by_time(end_ts, "after")
    except BlockLookupError:
        return client.current_block()


def _resolve_windows(client, pool, windows):
    """Attach start_block / end_block to each window, logging the range."""
    for window in windows:
        window["start_block"] = client.block_by_time(window["first_ts"], "before")
        window["end_block"] = _resolve_window_end(client, window["end_ts"])
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
) -> dict:
    """Export in-window withdrawals for one pool and count recipients.

    Legacy mode that reads raw internal transfers from the pool. A withdrawal
    qualifies if the recipient received ``denom * fee_lo`` .. ``denom * fee_hi``
    (the denomination minus a relayer fee). Windows as in :func:`voucher_windows`.
    """
    low_value = pool.denom * fee_lo
    high_value = pool.denom * fee_hi

    windows = voucher_windows(vouchers_for_pool, window_days, exit_window_hours)
    _resolve_windows(client, pool, windows)
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
        if item.get("isError") == "1":
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
) -> dict:
    """Exact, log-based variant of :func:`analyze_denom`.

    Reads the pool's ``Withdrawal`` events, so recipient, relayer and fee come
    from the contract rather than from a value band. This also sees zero-fee
    withdrawals, which fall outside the transfers-mode fee window.
    """
    windows = voucher_windows(vouchers_for_pool, window_days, exit_window_hours)
    _resolve_windows(client, pool, windows)
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
) -> dict:
    """Full single-wallet demix. Returns a structured result dict.

    ``result['denoms']`` maps a pool key ('1 ETH') to that pool's analysis.
    ``exit_window_hours`` narrows the withdrawal search to that many hours after
    each deposit instead of ``window_days`` days (see :func:`voucher_windows`);
    ``None`` keeps the full window.
    """
    network = network or _default_network()
    wallet = wallet.lower()
    # A non-positive exit window means a full-window run, in params and report too.
    if exit_window_hours is not None and exit_window_hours <= 0:
        exit_window_hours = None
    _log("[*] Wallet: {} on {}".format(wallet, network.name))

    all_txs = client.outgoing_txs(wallet)  # txlist returns both directions
    internal_txs = client.internal_txs(wallet)
    counterparties = wallet_counterparties(wallet, all_txs + internal_txs)
    deposits = detect_deposits(
        client, wallet, network=network, txs=all_txs, internal_txs=internal_txs
    )
    _log("[*] Tornado deposits detected: {}".format(len(deposits)))
    if not deposits:
        return {
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
            },
        }

    vouchers = cluster_vouchers(deposits, gap_hours=gap_hours)
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
                    client, pool, vouchers_for_pool, window_days, exit_window_hours
                )
            else:
                results[pool_key] = analyze_denom(
                    client, pool, vouchers_for_pool, window_days, fee_lo, fee_hi, exit_window_hours
                )
        except BlockLookupError as exc:
            # Skipping this pool loses one lead; searching the whole chain
            # instead would produce candidates with no timing basis.
            _log("  [{}] SKIPPED - {}".format(pool_key, exc))
            unresolved.append({"pool_key": pool_key, "reason": str(exc)})

    # Candidates: recipients whose count equals a voucher size. The gate is
    # applied once, here, so every consumer sees the same candidate set.
    for pool_key, res in results.items():
        target_counts = sorted({v["count"] for v in vouchers if v["pool_key"] == pool_key})
        res["target_counts"] = target_counts
        res["candidates_by_count"] = {
            n: (
                [addr for addr, c in res["counts"].items() if c == n]
                if count_is_evidence(res, n)
                else []
            )
            for n in target_counts
        }

    result = {
        "wallet": wallet,
        "params": {
            "window_days": window_days,
            "fee_window": [fee_lo, fee_hi],
            "gap_hours": gap_hours,
            "mode": mode,
            "network": network.name,
            "currency": network.currency,
            "exit_window_hours": exit_window_hours,
        },
        "deposits": deposits,
        "vouchers": vouchers,
        "denoms": results,
        "unresolved": unresolved,
    }

    apply_heuristics(
        result,
        counterparties,
        gas_gate=make_gas_price_gate(network.rpc_url, log=_log),
        is_contract=make_contract_check(network.rpc_url),
    )
    return result
