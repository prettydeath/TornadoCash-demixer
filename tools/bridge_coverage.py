#!/usr/bin/env python3
"""Where do Tornado ETH-pool exits go within two hops? Measured on a seeded sample.

Draws a reproducible random sample (fixed seed) of distinct recipients of Tornado
Cash ETH-pool withdrawals in a block range of Ethereum (the recipient's first
withdrawal in the range is the arrival). For each recipient it runs the tool's own
trace (``tornado_demix.trace.trace_funds``) over the withdrawn amount with
``max_hops=2``, the same code the ``trace`` command runs, including bridge decoding
and the following of bridges across chains. Nothing is guessed here: the class of a
recipient comes from the edges and end states of that trace.

Classes (one per recipient, the first that applies; counts add up to n):

* followed: a hop that the trace followed to the other side (an Across fill, an
  Arbitrum credit, a CCTP mint, a Mayan same-chain payout)
* named: a hop that ended at a recognised bridge or listed aggregator with a named
  state (Relay, Mayan, THORChain, Maya, Layerswap, NEAR Intents, Unit, Hyperliquid,
  ZKsync, an Across deposit whose fill was not found, and so on)
* swap: a swap resolved (DEX router, CoW Protocol) with no bridge before it
* exchange: the branch ended at an address labelled as an exchange
* other_labelled: the branch ended at an address with another label
* tornado_redeposit: sent into a Tornado pool or router again
* unknown_contract: a contract that pays nothing back and is not in any table
* unread: a lookup failed and nothing else was recognised
* eoa: everything else (the funds stayed, went to a plain address, or the hop limit
  was reached)

The per-protocol table counts a recipient once per protocol, so its rows overlap.
Intervals are 95 % Wilson intervals over the recipients. Exchange shares depend on the
label set in ``config/attribution/ethereum.csv`` (``tornado-demix labels fetch``); the
result states how many labels were loaded, and without labels the exchange share is
zero by construction.

A full trace per recipient is more expensive than a dry look at the first
transactions, and it is the only way to tell "followed" from "named": a bridge call
that is not tied to the traced transfer is not followed. The sample is small and the
exits of one window are not independent; the figures are exploratory.

Usage
-----
    # pilot: 60 recipients, the 2026-11-14 window of the evaluation document
    python tools/bridge_coverage.py --n 60 --seed 20261114 --pause 0.4

    # default size: 150 recipients (the pilot above passes --n 60 explicitly)
    python tools/bridge_coverage.py --seed 20261114 --pause 0.4

    # full run: 300 recipients over a wider window
    python tools/bridge_coverage.py --n 300 --seed 20261114 --pause 0.4 \
        --start-block 26000000 --end-block 26300000

The API key is read by the client from the configuration and never printed. The run
is resumable: answered recipients are kept in ``<out>/per_recipient.json``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tornado_demix import (  # noqa: E402
    bridges_canonical,
    bridges_cctp,
    bridges_named,
    config,  # noqa: E402
)
from tornado_demix.attribution import default_attribution_dir, load_attribution  # noqa: E402
from tornado_demix.bridges import FILLED, Destinations, load_bridges  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.events import fetch_withdrawals  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.rpc import make_contract_check  # noqa: E402
from tornado_demix.trace import trace_funds  # noqa: E402

OUT = os.path.join(ROOT, ".cache", "bridge_coverage")
DEFAULT_START = 26_100_000
DEFAULT_END = 26_300_000
DEFAULT_SEED = 20261114
MAX_HOPS = 2

# Bridge states in which the trace went on to the other side of the bridge.
FOLLOWED_STATES = frozenset(
    {
        FILLED,
        bridges_canonical.CREDITED,
        bridges_cctp.MINTED,
        bridges_named.SAME_CHAIN,
    }
)
CATEGORIES = (
    "followed",
    "named",
    "swap",
    "exchange",
    "other_labelled",
    "tornado_redeposit",
    "unknown_contract",
    "unread",
    "eoa",
)
CATEGORY_TEXT = {
    "followed": "bridge followed to the other side",
    "named": "bridge or aggregator named, not followed",
    "swap": "swap resolved (DEX, CoW Protocol)",
    "exchange": "ended at an exchange-labelled address",
    "other_labelled": "ended at another labelled address",
    "tornado_redeposit": "sent into Tornado again",
    "unknown_contract": "unknown contract (pays nothing back, in no table)",
    "unread": "a lookup failed, nothing recognised",
    "eoa": "plain address, funds held, or hop limit",
}


def wilson(k, n, z=1.96):
    """95 % Wilson score interval of k of n, as [low, high] (None when n is 0)."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def sample_recipients(client, net, start_block, end_block, n, seed):
    """A seeded random sample of distinct recipients of the native pools' withdrawals.

    A recipient is kept once, with its first withdrawal of the range (block, then
    transaction hash). Returns [{address, block, tx_hash, pool, amount}] in block order;
    ``amount`` is the pool's denomination less the relayer fee, in whole coins.
    """
    first = {}
    for pool in net.pools:
        if not pool.is_native:
            continue
        for w in fetch_withdrawals(client, pool.address, start_block, end_block):
            addr = w["to"].lower()
            row = {
                "address": addr,
                "block": int(w["block"]),
                "tx_hash": w["tx_hash"],
                "pool": pool.key,
                "amount": pool.denom - pool.to_units(w.get("fee_wei", 0)),
            }
            cur = first.get(addr)
            if cur is None or (row["block"], row["tx_hash"]) < (cur["block"], cur["tx_hash"]):
                first[addr] = row
    universe = sorted(first.values(), key=lambda r: (r["block"], r["tx_hash"], r["address"]))
    rng = random.Random(seed)
    picked = rng.sample(universe, min(n, len(universe)))
    return sorted(picked, key=lambda r: (r["block"], r["tx_hash"], r["address"]))


def _is_exchange(label):
    return ((label or {}).get("category") or "").strip().lower() == "exchange"


def classify_trace(result, table):
    """Class of one recipient from its trace result (see the module text).

    Returns {category, protocols, swap_protocols, labels}: ``protocols`` maps each
    bridge protocol seen in the first two hops to "followed" or "named" (followed when
    any of its hops was followed).
    """
    protocols, swaps, labels = {}, [], []
    plain = set()

    def bridge(info):
        proto = info.get("protocol") or "unknown"
        state = FOLLOWED_STATES & {info.get("state")}
        if state:
            protocols[proto] = "followed"
        else:
            protocols.setdefault(proto, "named")

    swap_seen = False
    for e in result.get("edges", []):
        if e.get("bridge"):
            bridge(e["bridge"])
        elif e.get("swap"):
            swap_seen = True
            swaps.append(e["swap"].get("protocol") or "dex")
        elif e.get("kind") == "swap":
            swap_seen = True
            swaps.append("dex")
    for t in result.get("terminals", []):
        reason = t.get("reason") or ""
        if t.get("bridge"):
            bridge(t["bridge"])
        for extra in t.get("bridges") or []:
            bridge(extra)
        if reason == "labelled address":
            labels.append(t.get("label") or {})
            plain.add("exchange" if _is_exchange(t.get("label")) else "other_labelled")
        elif reason == "tornado_redeposit":
            plain.add("tornado_redeposit")
        elif reason == "contract without a payment back":
            plain.add("unknown_contract")
        elif reason == "lookup_failed":
            plain.add("unread")
        elif not t.get("bridge"):
            plain.add("eoa")
    if "followed" in protocols.values():
        category = "followed"
    elif protocols:
        category = "named"
    elif swap_seen:
        category = "swap"
    else:
        category = next((c for c in CATEGORIES[3:] if c in plain), "eoa")
    return {
        "category": category,
        "protocols": protocols,
        "swap_protocols": sorted(set(swaps)),
        "labels": [x.get("label") or x.get("entity") or "" for x in labels],
        "label_categories": sorted({(x.get("category") or "none") for x in labels}),
    }


def summarise(rows):
    """Shares per class and per protocol with Wilson intervals."""
    n = len(rows)
    by_category = {}
    for c in CATEGORIES:
        k = sum(1 for r in rows if r["category"] == c)
        by_category[c] = {"count": k, "share": (k / n if n else 0.0), "ci": wilson(k, n)}
    bridged = sum(1 for r in rows if r.get("protocols"))
    by_protocol = {}
    for r in rows:
        for proto, how in (r.get("protocols") or {}).items():
            row = by_protocol.setdefault(proto, {"followed": 0, "named": 0, "any": 0})
            row[how] += 1
            row["any"] += 1
    for row in by_protocol.values():
        row["ci"] = wilson(row["any"], n)
    other = {}
    for r in rows:
        if r["category"] == "other_labelled":
            for c in r.get("label_categories") or ["none"]:
                other[c] = other.get(c, 0) + 1
    return {
        "n": n,
        "by_category": by_category,
        "other_label_categories": other,
        "any_bridge": {
            "count": bridged,
            "share": (bridged / n if n else 0.0),
            "ci": wilson(bridged, n),
        },
        "by_protocol": by_protocol,
    }


def _pct(x):
    return f"{100 * x:.1f} %"


def _ci(ci):
    return "" if ci is None else f"{100 * ci[0]:.1f}-{100 * ci[1]:.1f} %"


def render(summ, meta):
    n = summ["n"]
    lines = [
        "# Bridge coverage of Tornado ETH-pool exits (exploratory)",
        "",
        f"Sample: {n} distinct recipients of Tornado ETH-pool withdrawals in Ethereum blocks "
        f"{meta.get('start_block'):,} to {meta.get('end_block'):,} (seed {meta.get('seed')}), "
        f"run on {meta.get('date')}. Each recipient was traced for {MAX_HOPS} hops with "
        "`trace_funds` (bridges followed across chains where the trace follows them). "
        f"Labels loaded: {meta.get('labels', 'not stated')}. "
        "Intervals are 95 % Wilson intervals; recipients of one window are not independent, "
        "so the intervals understate the uncertainty.",
        "",
        "| class (one per recipient) | recipients | share | 95 % CI |",
        "|---|---:|---:|---|",
    ]
    for c in CATEGORIES:
        r = summ["by_category"][c]
        lines.append(f"| {CATEGORY_TEXT[c]} | {r['count']} | {_pct(r['share'])} | {_ci(r['ci'])} |")
    a = summ["any_bridge"]
    if summ.get("other_label_categories"):
        kinds = ", ".join(f"{c} {k}" for c, k in sorted(summ["other_label_categories"].items()))
        lines += ["", f"Label categories of the other labelled ends: {kinds}."]
    lines += [
        "",
        f"Recipients with any bridge hop in two hops: {a['count']} of {n} "
        f"({_pct(a['share'])}, {_ci(a['ci'])}).",
        "",
        "| protocol (a recipient counts once per protocol) | followed | named, not followed "
        "| share of recipients | 95 % CI |",
        "|---|---:|---:|---:|---|",
    ]
    ranked = sorted(summ["by_protocol"].items(), key=lambda kv: (-kv[1]["any"], kv[0]))
    for proto, r in ranked:
        share = r["any"] / n if n else 0.0
        lines.append(
            f"| {proto} | {r['followed']} | {r['named']} | {_pct(share)} | {_ci(r['ci'])} |"
        )
    if not ranked:
        lines.append("| (none) | 0 | 0 | 0.0 % | |")
    lines += [
        "",
        "Limits: the sample is one block window; a bridge the code does not recognise "
        "falls under unknown contract or plain address; a hop that is not tied to the "
        "traced transfer is named, not followed; exchange labels come from the fetched "
        "label set and are leads, not attribution; the figures are exploratory.",
    ]
    return "\n".join(lines) + "\n"


class _PacedDestinations(Destinations):
    """Destination readers with the same pause and response cache as the main client."""

    def __init__(self, api_key, pause, cache_dir, **kw):
        super().__init__(api_key, **kw)
        self.pause = pause
        self.cache_dir = cache_dir

    def __call__(self, chain_id):
        d = super().__call__(chain_id)
        if d is not None:
            d["client"].pause = self.pause
            if self.cache_dir:
                d["client"].cache_dir = self.cache_dir
        return d


def _load_done(path):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--n",
        type=int,
        default=150,
        help="recipients to draw (default 150; the documented pilot used --n 60)",
    )
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--start-block", type=int, default=DEFAULT_START)
    ap.add_argument("--end-block", type=int, default=DEFAULT_END)
    ap.add_argument("--pause", type=float, default=0.4, help="seconds before every API call")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    if args.n < 1 or args.start_block > args.end_block:
        ap.error("--n must be at least 1 and --start-block at most --end-block")
    net = get_network("ethereum")
    key = config.load_api_key()
    http_cache = os.path.join(args.out, "http")
    os.makedirs(http_cache, exist_ok=True)
    client = EtherscanClient(
        key,
        pause=args.pause,
        cache_dir=http_cache,
        **{k: v for k, v in net.client_kwargs().items() if k != "chain_id"},
    )
    destinations = _PacedDestinations(key, args.pause, http_cache)
    attribution = default_attribution_dir()
    labels = load_attribution("ethereum", attribution)
    table = load_bridges()
    is_contract = make_contract_check(net.rpc_url)
    sample = sample_recipients(client, net, args.start_block, args.end_block, args.n, args.seed)
    print(f"{len(sample)} recipients drawn (seed {args.seed})", file=sys.stderr)
    done_path = os.path.join(args.out, "per_recipient.json")
    done = _load_done(done_path)
    for i, rec in enumerate(sample, 1):
        slot = f"{rec['address']}@{rec['tx_hash']}"
        if slot in done:
            continue
        result = trace_funds(
            client,
            rec["address"],
            rec["amount"],
            start_block=rec["block"],
            currency=net.currency,
            max_hops=MAX_HOPS,
            labels=labels,
            is_contract=is_contract,
            chain_id=net.chain_id,
            destinations=destinations,
            arrival_tx=rec["tx_hash"],
        )
        row = dict(rec, **classify_trace(result, table))
        row["error_count"] = result.get("error_count", 0)
        done[slot] = row
        with open(done_path, "w", encoding="utf-8") as fh:
            json.dump(done, fh, indent=1, sort_keys=True)
        print(f"[{i}/{len(sample)}] {rec['address']} -> {row['category']}", file=sys.stderr)
    rows = [done[f"{r['address']}@{r['tx_hash']}"] for r in sample]
    summ = summarise(rows)
    meta = {
        "n": len(rows),
        "seed": args.seed,
        "start_block": args.start_block,
        "end_block": args.end_block,
        "date": datetime.date.today().isoformat(),
        "labels": f"{len(labels)} Ethereum addresses",
    }
    text = render(summ, meta)
    with open(os.path.join(args.out, "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "summary": summ}, fh, indent=1, sort_keys=True)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
