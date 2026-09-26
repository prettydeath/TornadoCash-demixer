#!/usr/bin/env python3
"""Discover and verify Tornado.Cash pool contracts on any EVM chain.

Never trust a pool address you have not verified: a wrong address produces
confident, meaningless output. This tool proves an address is a fixed-denomination
Tornado pool using only on-chain evidence.

Method
------
RPC verification is the primary path (``--rpc``). It reads the contract's
own getters over any public JSON-RPC endpoint, needs no API key, and costs four
calls instead of hundreds:

* ``denomination()`` - the fixed amount, in the asset's smallest unit. Absent or
  zero means this is not a Tornado pool (or the endpoint is unreachable).
* ``token()`` - zero for a native pool, otherwise the ERC-20 whose ``decimals()``
  and ``symbol()`` name the asset.
* ``levels()`` - the Merkle tree depth. 20 for every genuine deployment, and
  a rejection criterion: a different depth is a different contract.
* ``nextIndex()`` - deposits ever accepted, which separates a live pool from a
  deployed-but-unused one. Annotation only; it does not reject.

The log-sampling path (the default, no ``--rpc``) is the older, independent
cross-check, kept for chains with no usable public RPC. It queries `getLogs` for
the `Withdrawal` topic - without an address filter, for ``--discover`` - then for
each candidate decodes `fee` from the event and matches it to the pool's outgoing
internal transfer in the same transaction. For a genuine pool ``payout + fee`` is
a constant equal to the denomination; a contract whose value varies is a
clone or an unrelated protocol and is rejected. It reads no token metadata, so it
emits native-asset rows only.

Usage
-----
    python tools/verify_pools.py --chain 42161 --network-name arbitrum \
        --rpc https://arbitrum-one-rpc.publicnode.com --scan
    python tools/verify_pools.py --chain 42161 --address 0x84443CFd...
    python tools/verify_pools.py --chain 137 --discover
    python tools/verify_pools.py --chain 137 --network-name polygon \
        --currency MATIC --discover --emit-csv >> config/networks.csv
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from tornado_demix import config  # noqa: E402
from tornado_demix.constants import TOPIC_WITHDRAWAL  # noqa: E402
from tornado_demix.etherscan import MAX_BLOCK  # noqa: E402
from tornado_demix.pools import _fmt_denom  # noqa: E402
from tornado_demix.rpc import SELECTORS, RpcClient  # noqa: E402

# ERC-20 getters, for reading a token pool's asset.
ERC20_DECIMALS = "0x313ce567"  # decimals()
ERC20_SYMBOL = "0x95d89b41"  # symbol()

API = "https://api.etherscan.io/v2/api"
# Denominations considered plausible for a real pool (in whole asset units).
# Tornado deployed two families: powers of ten, and a 5x10^n family used by the
# Avalanche 500 pool and by every cDAI / cUSDC pool on mainnet. Every value here
# corresponds to a contract verified on-chain by denomination(), not to a guess.
# This is a sanity filter against clones and unrelated protocols, not a security
# boundary - the real evidence is denomination() + token() + levels().
PLAUSIBLE = [
    0.1,
    1.0,
    10.0,
    100.0,
    500.0,
    1000.0,
    5000.0,
    10000.0,
    50000.0,
    100000.0,
    500000.0,
    5000000.0,
]
# Share of sampled withdrawals that must agree on one value.
MIN_AGREEMENT = 0.8
# Merkle tree depth. Every genuine Tornado deployment uses 20; a contract that
# answers levels() with anything else is a different contract wearing the same
# interface, so this is a rejection criterion and not just an annotation.
EXPECTED_LEVELS = 20


# Overridable at runtime by --base-url / --style (for Blockscout, Routescan).
_BASE = API
_STYLE = "v2"


def _get(key, **params):
    if _STYLE == "v2":
        params["apikey"] = key
    else:
        params.pop("chainid", None)  # compat explorers are per-chain hosts
        if key:
            params["apikey"] = key
    try:
        return requests.get(_BASE, params=params, timeout=30).json().get("result")
    except (OSError, ValueError):
        return None


def discover(key, chain, sample=1000):
    """Return {address: event_count} for contracts emitting Withdrawal."""
    logs = _get(
        key,
        chainid=chain,
        module="logs",
        action="getLogs",
        topic0=TOPIC_WITHDRAWAL,
        fromBlock=0,
        toBlock=MAX_BLOCK,
        page=1,
        offset=sample,
    )
    if not isinstance(logs, list):
        raise SystemExit(f"Chain {chain} not readable with this key: {str(logs)[:120]}")
    return Counter(log["address"].lower() for log in logs)


def verify(key, chain, address, sample=100):
    """Return (denomination, agreement, n_matched) or (None, 0, 0)."""
    address = address.lower()
    logs = _get(
        key,
        chainid=chain,
        module="logs",
        action="getLogs",
        address=address,
        topic0=TOPIC_WITHDRAWAL,
        fromBlock=0,
        toBlock=MAX_BLOCK,
        page=1,
        offset=sample,
    )
    if not isinstance(logs, list) or not logs:
        return None, 0.0, 0

    # fee is the third 32-byte word of the Withdrawal data field
    fees = {log["transactionHash"]: int(log["data"][2:][128:192], 16) / 1e18 for log in logs}

    internal = _get(
        key,
        chainid=chain,
        module="account",
        action="txlistinternal",
        address=address,
        startblock=0,
        endblock=MAX_BLOCK,
        page=1,
        offset=1000,
        sort="asc",
    )

    # A withdrawal produces up to two outgoing internal transfers: the recipient
    # payout and the relayer fee. Keep the largest per transaction - that is the
    # payout - so that payout + fee reconstructs the denomination.
    largest = {}
    if isinstance(internal, list):
        for item in internal:
            tx_hash = item.get("hash")
            if tx_hash in fees and (item.get("from") or "").lower() == address:
                value = int(item.get("value", "0")) / 1e18
                if value > largest.get(tx_hash, 0):
                    largest[tx_hash] = value

    totals = Counter()
    for tx_hash, payout in largest.items():
        totals[round(payout + fees[tx_hash], 6)] += 1

    if not totals:
        return None, 0.0, 0
    value, hits = totals.most_common(1)[0]
    agreement = hits / sum(totals.values())
    return value, agreement, hits


def _read_symbol(client, token):
    """Return an ERC-20 symbol, decoding both the string and bytes32 forms."""
    if hasattr(client, "call_symbol"):  # test double
        return client.call_symbol(token)
    raw = client.eth_call(token, ERC20_SYMBOL)
    if not raw:
        return None
    body = raw[2:]
    try:
        if len(body) > 64:  # ABI-encoded dynamic string
            offset = int(body[:64], 16) * 2
            length = int(body[offset : offset + 64], 16) * 2
            start = offset + 64
            return bytes.fromhex(body[start : start + length]).decode("utf-8")
        return bytes.fromhex(body).decode("utf-8").rstrip("\x00")
    except (ValueError, UnicodeDecodeError):
        return None


def verify_via_rpc(client, address):
    """Verify one address as a Tornado pool using eth_call only.

    Returns a dict: address, denom_raw, denom, token, decimals, asset, levels,
    deposits, ok, reason. ``asset`` is None for a native pool - the caller
    supplies the chain's native ticker.

    Three checks decide ``ok``, and ``reason`` names whichever one failed:
    ``denomination()`` must answer non-zero, its value must be in
    :data:`PLAUSIBLE`, and ``levels()`` must equal :data:`EXPECTED_LEVELS`.
    ``nextIndex()`` annotates a verified pool as unused or nearly unused; it
    never rejects, because an empty pool is still a real pool.
    """
    address = address.lower()
    out = {
        "address": address,
        "denom_raw": None,
        "denom": None,
        "token": None,
        "decimals": 18,
        "asset": None,
        "levels": None,
        "deposits": None,
        "ok": False,
        "reason": "",
    }

    denom_raw = client.call_uint(address, SELECTORS["denomination"])
    if denom_raw is None or denom_raw == 0:
        out["reason"] = (
            "denomination() returned nothing - not a Tornado pool, "
            "or this RPC endpoint is unreachable"
        )
        return out
    out["denom_raw"] = denom_raw

    token = client.call_address(address, SELECTORS["token"])
    if token:
        out["token"] = token
        decimals = client.call_uint(token, ERC20_DECIMALS)
        out["decimals"] = 18 if decimals is None else int(decimals)
        out["asset"] = _read_symbol(client, token)

    # Round before the membership test: 10**22 is the largest power of ten a
    # double represents exactly, so 100000 at 18 decimals lands on
    # 99999.99999999999 and would be rejected as implausible. Eight decimals
    # matches the precision convention in tornado_demix/pools.py.
    out["denom"] = round(denom_raw / float(10 ** out["decimals"]), 8)
    out["levels"] = client.call_uint(address, SELECTORS["levels"])
    out["deposits"] = client.call_uint(address, SELECTORS["nextIndex"])

    if out["denom"] not in PLAUSIBLE:
        out["reason"] = "implausible denomination {}".format(out["denom"])
        return out

    if out["levels"] != EXPECTED_LEVELS:
        out["reason"] = "levels() returned {}, expected {} - not a Tornado pool".format(
            "nothing" if out["levels"] is None else out["levels"], EXPECTED_LEVELS
        )
        return out

    out["ok"] = True
    if out["deposits"] == 0:
        out["reason"] = "verified but unused: nextIndex() is 0, no deposits ever"
    elif out["deposits"] is not None and out["deposits"] < 10:
        out["reason"] = "verified but nearly unused: {} deposit(s)".format(out["deposits"])
    return out


def legacy_result(address, denom):
    """Shape a log-sampling verification as a :func:`csv_row` result dict.

    The log-sampling path reconstructs ``payout + fee`` in whole native units
    and reads no token metadata, so what it verifies is always a native pool at
    18 decimals. Rendering it through ``csv_row`` rather than printing five
    columns by hand is what makes the documented
    ``--discover --emit-csv >> config/networks.csv`` actually append rows the
    v2 parser accepts.
    """
    return {
        "address": address.lower(),
        "denom": denom,
        "decimals": 18,
        "token": None,
        "asset": None,
        "ok": True,
        "reason": "",
    }


def csv_row(network, chain_id, currency, result, api_base="", api_style="", rpc_url=""):
    """Render one verified pool as a schema-v2 config/networks.csv line.

    The trailing ``router_address`` field is always emitted empty: this tool
    verifies pools, not proxies, and an address it has not verified must not
    reach the registry. Fill it in by hand once a chain's router is confirmed.
    """
    asset = result["asset"] or currency
    return ",".join(
        [
            network,
            str(chain_id),
            currency,
            asset,
            _fmt_denom(result["denom"]),
            result["address"],
            str(result["decimals"]),
            result["token"] or "",
            api_base,
            api_style,
            rpc_url,
            "",
            "",
            "",
        ]
    )


def _main_rpc(args):
    """--rpc mode: verify by eth_call, optionally emitting v2 CSV rows."""
    from tornado_demix.networks import load_networks

    client = RpcClient(args.rpc)
    name = args.network_name or "chain{}".format(args.chain)

    if args.address:
        targets = [args.address.lower()]
    elif args.scan:
        networks = load_networks()
        if name not in networks:
            raise SystemExit(
                "No network '{}' in the registry to scan. Use --address, or add rows first.".format(
                    name
                )
            )
        targets = [p.address for p in networks[name].pools]
    else:
        raise SystemExit("With --rpc, pass --address or --scan")

    for address in targets:
        result = verify_via_rpc(client, address)
        status = "VERIFIED" if result["ok"] else "rejected"
        detail = ""
        if result["denom"] is not None:
            detail = "denom={:g} asset={} decimals={} deposits={}".format(
                result["denom"],
                result["asset"] or args.currency,
                result["decimals"],
                result["deposits"],
            )
        print(
            "[{}] {} {} {}".format(status, address, detail, result["reason"]).rstrip(),
            file=sys.stderr,
        )
        if result["ok"] and args.emit_csv:
            print(csv_row(name, args.chain, args.currency, result, rpc_url=args.rpc))


def main():
    ap = argparse.ArgumentParser(description="Verify Tornado pools on an EVM chain.")
    ap.add_argument("--chain", type=int, required=True, help="chain id (137, 42161, 100, ...)")
    ap.add_argument("--address", help="verify one address instead of discovering")
    ap.add_argument("--discover", action="store_true", help="scan the chain for pools")
    ap.add_argument("--network-name", default="", help="name to use in --emit-csv rows")
    ap.add_argument("--currency", default="ETH", help="native currency for --emit-csv")
    ap.add_argument(
        "--emit-csv", action="store_true", help="print verified rows in config/networks.csv format"
    )
    ap.add_argument("--top", type=int, default=8, help="how many candidates to verify")
    ap.add_argument("--api-csv", default=config.DEFAULT_API_CSV)
    ap.add_argument(
        "--base-url",
        default="",
        help="use an Etherscan-compatible explorer (Blockscout, Routescan) instead of Etherscan V2",
    )
    ap.add_argument("--style", choices=["v2", "compat"], default="v2")
    ap.add_argument(
        "--rpc",
        default="",
        help="verify via eth_call against this JSON-RPC endpoint "
        "(no API key needed); the default path samples logs",
    )
    ap.add_argument(
        "--scan",
        action="store_true",
        help="with --rpc, verify every pool already in the registry for --network-name",
    )
    args = ap.parse_args()

    if args.rpc:
        return _main_rpc(args)

    global _BASE, _STYLE
    if args.base_url:
        _BASE, _STYLE = args.base_url, "compat"
    else:
        _STYLE = args.style

    try:
        key = config.load_api_key(args.api_csv)
    except SystemExit:
        key = ""  # compat explorers need no key
        if _STYLE == "v2":
            raise
    targets = []
    if args.address:
        targets = [(args.address.lower(), None)]
    elif args.discover:
        found = discover(key, args.chain)
        print(f"# {len(found)} contract(s) emit Withdrawal on chain {args.chain}", file=sys.stderr)
        targets = found.most_common(args.top)
    else:
        raise SystemExit("Use --discover or --address")

    name = args.network_name or f"chain{args.chain}"
    for address, events in targets:
        denom, agreement, matched = verify(key, args.chain, address)
        ok = (denom in PLAUSIBLE) and agreement >= MIN_AGREEMENT and matched >= 3
        status = "VERIFIED" if ok else "rejected"
        note = "" if denom is None else f"denom={denom} agreement={agreement:.0%} n={matched}"
        print(
            f"[{status}] {address} {note}" + (f" events={events}" if events else ""),
            file=sys.stderr,
        )
        if ok and args.emit_csv:
            print(csv_row(name, args.chain, args.currency, legacy_result(address, denom)))


if __name__ == "__main__":
    main()
