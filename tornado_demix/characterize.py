"""Recipient-side analysis: characterise an exit-candidate address.

The demix and multi analyses are depositor-centric: they surface a recipient
that looks like an exit. This module answers the next question, what that address
actually is. It pulls the candidate's own on-chain history and reports:

  * activity level (normal / internal tx counts): a busy address is a service or
    exchange, not a personal wallet;
  * Tornado-pool inflows: every withdrawal it received from a pool this network
    knows, with timing and pool, so a busy recipient that collects notes from
    many pools (including pools the subject never used) shows up as a
    multi-pool consolidator (a private exit or a service);
  * dominant next hops: where its funds flow, extending the trace one layer.

A classification is derived from those, with the reasoning stated, so a
multi-pool consolidator is labelled neutrally (it could be one depositor's private
exit or a service) rather than presented as either.

Every listed address (the subject and each next hop) also gets the deposit-address
check of :mod:`tornado_demix.deposit_addresses` (``deposit_check``: exchange, service
or deposit-like deposit address, forwarder, smart account, not a deposit address, or
unknown when a lookup failed). It costs two history queries per address plus one busy
check per unlabelled sweep target; ``deposit_check=False`` skips it.
:func:`run_classify` runs the same check for one given address
(``classify-address``).

Native (ETH-like) pools only: a pool withdrawal reaches the recipient as an
internal transfer from the pool contract, which ``internal_txs`` returns. ERC-20
pool withdrawals are token transfers and are out of scope for the inflow list.
"""

from .deposit_addresses import classify_address
from .incomplete import attach, mark, truncated_since
from .rpc import make_contract_check

# Classification thresholds (judgement calls).
SERVICE_MIN_TXS = 500  # above this an address is a service/exchange, not personal
AGGREGATOR_MIN_POOLS = 3  # draws from this many distinct pools -> aggregator
AGGREGATOR_MIN_INFLOWS = 10  # this many pool withdrawals -> aggregator
DISPOSABLE_HOURS = 24  # history before the first inflow of a disposable outlet

# The pool also pays the relayer's fee as a separate internal transfer. Only a
# transfer of at least this fraction of the denomination counts as a received
# note, so a relayer's collected fees do not look like withdrawals.
INFLOW_MIN_FRACTION = 0.5


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def run_classify(client, address, network, labels=None, is_contract=None):
    """The deposit-address check of one address on ``network``: the classifier result
    (always with a ``kind``, see :func:`deposit_addresses.classify_address`) plus
    ``network`` and ``errors`` / ``error_count`` / ``truncated``."""
    start_mark = mark(client)
    if is_contract is None:
        is_contract = make_contract_check(network.rpc_url)
    info, errors = classify_address(
        client, address, labels, is_contract, gas_fund_wei=network.gas_fund_wei
    )
    info = dict(info, network=network.name)
    errors += getattr(is_contract, "errors", [])
    return attach(info, errors, truncated_since(client, start_mark))


def characterize_address(
    client,
    address,
    network,
    next_hop_limit=8,
    labels=None,
    deposit_check=True,
    is_contract=None,
):
    """Return a structured characterisation of ``address`` on ``network``.

    Keys: address, label, normal_txs, internal_txs, first_activity_ts,
    last_activity_ts, pool_inflows (list of {pool_key, asset, ts, value, tx_hash,
    pool_address}), inflow_totals ({asset: summed value}), distinct_pools (sorted
    keys), top_next_hops (list of {address, count, total_value, label}),
    classification and classification_reason, and ``errors``, ``error_count`` and
    ``truncated`` (see :mod:`tornado_demix.incomplete`; both history lists are
    required, so a failed lookup raises instead of reading as "no inflows").

    ``labels`` is an optional ``{address_lower: label_dict}`` map (see
    :func:`tornado_demix.attribution.load_attribution`); when given, the subject
    and every next hop are annotated with their known attribution.

    With ``deposit_check`` (the default) the subject (``deposit_check``) and every next
    hop (``top_next_hops[i]["deposit_check"]``) carry the deposit-address check; its
    failed lookups join ``errors``. ``is_contract`` defaults to an ``eth_getCode``
    check on the network's RPC endpoint.
    """
    labels = labels or {}
    address = address.lower()
    native_pools = {p.address.lower(): p for p in network.pools if p.is_native}

    start_mark = mark(client)
    normal = client.outgoing_txs(address) or []
    internal = client.internal_txs(address) or []

    all_ts = [_to_int(t.get("timeStamp")) for t in (normal + internal) if t.get("timeStamp")]
    first_ts = min(all_ts) if all_ts else None
    last_ts = max(all_ts) if all_ts else None

    # Tornado-pool inflows: internal transfers from a known pool contract to us.
    pool_inflows = []
    inflow_totals = {}
    distinct = set()
    for t in internal:
        src = (t.get("from") or "").lower()
        dst = (t.get("to") or "").lower()
        pool = native_pools.get(src)
        if pool is None or dst != address:
            continue
        value = _to_int(t.get("value")) / (10**pool.decimals)
        if value < pool.denom * INFLOW_MIN_FRACTION:
            continue
        pool_inflows.append(
            {
                "pool_key": pool.key,
                "asset": pool.asset,
                "ts": _to_int(t.get("timeStamp")),
                "value": round(value, 6),
                "tx_hash": t.get("hash"),
                "pool_address": pool.address,
            }
        )
        inflow_totals[pool.asset] = round(inflow_totals.get(pool.asset, 0.0) + value, 6)
        distinct.add(pool.key)
    pool_inflows.sort(key=lambda r: r["ts"])

    # Next hops: normal transactions sent by this address.
    hop_count, hop_value = {}, {}
    for t in normal:
        if (t.get("from") or "").lower() != address:
            continue
        dst = (t.get("to") or "").lower()
        if not dst:
            continue  # contract creation, no destination
        hop_count[dst] = hop_count.get(dst, 0) + 1
        hop_value[dst] = hop_value.get(dst, 0) + _to_int(t.get("value"))
    top_next_hops = [
        {
            "address": dst,
            "count": n,
            "total_value": round(hop_value[dst] / 1e18, 6),
            "kind": "transfer" if hop_value[dst] else "call",
            "label": labels.get(dst),
        }
        for dst, n in sorted(hop_count.items(), key=lambda kv: (-kv[1], kv[0]))
    ][:next_hop_limit]

    classification, reason = _classify(len(normal), len(pool_inflows), len(distinct))
    # A disposable outlet: no more than a day of history before the first inflow.
    disposable = bool(
        pool_inflows and first_ts and pool_inflows[0]["ts"] - first_ts <= DISPOSABLE_HOURS * 3600
    )

    info = {
        "address": address,
        "label": labels.get(address),
        "normal_txs": len(normal),
        "internal_txs": len(internal),
        "first_activity_ts": first_ts,
        "last_activity_ts": last_ts,
        "pool_inflows": pool_inflows,
        "inflow_totals": inflow_totals,
        "distinct_pools": sorted(distinct),
        "top_next_hops": top_next_hops,
        "classification": classification,
        "classification_reason": reason,
        "disposable": disposable,
    }
    errors = []
    if deposit_check:
        if is_contract is None:
            is_contract = make_contract_check(network.rpc_url)
        listed = [(info, address)] + [(h, h["address"]) for h in top_next_hops]
        for holder, addr in listed:
            found, failed = classify_address(
                client, addr, labels, is_contract, gas_fund_wei=network.gas_fund_wei
            )
            holder["deposit_check"] = found
            errors += failed
        errors += getattr(is_contract, "errors", [])
    # Both lists are required, so a failed lookup raises; a cut-off list is recorded.
    return attach(info, errors, truncated_since(client, start_mark))


def _classify(n_normal, n_inflows, n_pools):
    """Classify an address from its activity and pool-inflow diversity."""
    if n_normal >= SERVICE_MIN_TXS:
        return (
            "high-activity service",
            "{} normal transactions - a service or exchange address, not a personal exit".format(
                n_normal
            ),
        )
    if n_pools >= AGGREGATOR_MIN_POOLS or n_inflows >= AGGREGATOR_MIN_INFLOWS:
        return (
            "multi-pool consolidator",
            "collects {} pool withdrawal(s) across {} distinct pool(s) - a "
            "multi-pool consolidator (could be a private exit or a service); "
            "withdrawals of unrelated depositors may be mixed in".format(n_inflows, n_pools),
        )
    if n_inflows == 0:
        return (
            "no pool inflows",
            "no withdrawals from a known pool of this network were found - not "
            "a Tornado exit on the pools analysed (ERC-20 pools are out of scope)",
        )
    return (
        "possible personal exit",
        "{} pool withdrawal(s) across {} pool(s) with low overall activity - "
        "consistent with a personal exit, worth corroborating".format(n_inflows, n_pools),
    )
