#!/usr/bin/env python3
"""Upper bound on how often the Merkle-root exclusion can fire (feasibility gate).

A withdrawal proof names a root from the pool's ring buffer of the last
ROOT_HISTORY_SIZE roots. It can exclude a deposit with leaf index L only if some
root still accepted at withdrawal time was computed before leaf L was inserted.

ROOT_HISTORY_SIZE, read from the verified source (explorer ``getsourcecode``)
of the deployed pools on 2026-11-12:

* Ethereum (solc 0.5.11): 0.1 ETH 0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc = 100;
  1 ETH 0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936 = 100;
  10 ETH 0x910cbd523d972eb0a6f4cae4618ad62622b39dbf = 100;
  100 ETH 0xa160cdab225685da1d56aa342ad8841c3b53f291 = 100.
* Polygon (solc 0.7.6): 100 MATIC 0x1e34a77868e19a6647b1f2f47b51ed72dede95dd = 30;
  1000 MATIC 0xdf231d99ff8b6c6cbf4e9b9a945cbacef9339178 = 30.

The ceiling counts every Withdrawal event in the real search window (no fee
filter) and tests only the newest of the wallet's notes in the pool, so it is an
upper bound on the share of withdrawals a root rule could ever exclude.

Usage
-----
    python tools/root_ceiling.py --network ethereum --dir holdout2 --history 100
    python tools/root_ceiling.py --network polygon --dir polygon2 --history 30
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tornado_demix.constants import TOPIC_DEPOSIT, TOPIC_WITHDRAWAL  # noqa: E402
from tornado_demix.events import decode_deposit  # noqa: E402
from tornado_demix.registry import record_safe as record  # noqa: E402


def fetch_deposit_index(client, pool_address, start_block, end_block):
    """``(block, leaf_index, tx_hash)`` of every Deposit event, by leaf index."""
    logs = client.get_logs(pool_address, TOPIC_DEPOSIT, start_block, end_block)
    rows = [decode_deposit(log) for log in logs]
    return sorted((r["block"], r["leaf_index"], r["tx_hash"]) for r in rows)


def leaves_before(index, block):
    """Insertions in the pool up to the end of ``block``.

    An index that starts mid-history is offset by its first leaf index.
    """
    return _leaves_before(index, [b for b, _leaf, _tx in index], block)


def _leaves_before(index, blocks, block):
    if not index:
        return 0
    return index[0][1] + bisect.bisect_right(blocks, block)


def ceiling_hits(deposit_leaves, withdrawal_blocks, index, history):
    """``(withdrawals that could be excluded, withdrawals)``."""
    if not withdrawal_blocks or not deposit_leaves:
        return 0, len(withdrawal_blocks)
    newest = max(deposit_leaves)
    blocks = [b for b, _leaf, _tx in index]
    hits = 0
    for wb in withdrawal_blocks:
        n_w = _leaves_before(index, blocks, wb)
        oldest_accepted = max(0, n_w - history + 1)
        if oldest_accepted <= newest:
            hits += 1
    return hits, len(withdrawal_blocks)


def ceiling_share(deposit_leaves, withdrawal_blocks, index, history):
    hits, total = ceiling_hits(deposit_leaves, withdrawal_blocks, index, history)
    return hits / total if total else 0.0


def _withdrawal_blocks(client, pool_address, start_block, end_block):
    logs = client.get_logs(pool_address, TOPIC_WITHDRAWAL, start_block, end_block)
    return sorted(int(log.get("blockNumber", "0x0"), 16) for log in logs)


def _collect(runs_dir, pools, limit):
    """Per pool: windows and deposits of the first ``limit`` depositors (sorted order)."""
    files = sorted(glob.glob(os.path.join(runs_dir, "target", "*.json")))
    if limit:
        files = files[:limit]
    per_pool = defaultdict(lambda: {"windows": [], "deposits": defaultdict(list)})
    for path in files:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        wallet = data["wallet"]
        for dep in data["deposits"]:
            if dep["pool_key"] in pools:
                per_pool[dep["pool_key"]]["deposits"][wallet].append((dep["block"], dep["hash"]))
        for key, res in data["denoms"].items():
            if key in pools:
                for w in res.get("windows", []):
                    per_pool[key]["windows"].append((wallet, w["start_block"], w["end_block"]))
    return len(files), per_pool


def _record_summary(summary):
    record(
        "root_ceiling.share",
        __file__,
        {k: summary.get(k) for k in ("network", "dir", "history", "limit")},
        summary,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--network", default="ethereum")
    ap.add_argument("--dir", required=True, help="run directory under .cache/labels/placebo")
    ap.add_argument("--history", type=int, required=True)
    ap.add_argument("--pools", default=None, help="comma-separated pool keys")
    ap.add_argument("--limit", type=int, default=0, help="first N depositors in sample order")
    args = ap.parse_args(argv)

    from ens_labels import CACHE, api_keys

    from tornado_demix.etherscan import EtherscanClient
    from tornado_demix.networks import get_network

    network = get_network(args.network)
    native = {"ethereum": ("0.1 ETH", "1 ETH", "10 ETH", "100 ETH")}.get(args.network)
    keys = (
        set(args.pools.split(",")) if args.pools else set(native or (p.key for p in network.pools))
    )
    addresses = {p.key: p.address for p in network.pools if p.key in keys}
    runs_dir = os.path.join(CACHE, "placebo", args.dir)
    out_dir = os.path.join(CACHE, "placebo", "root_ceiling")
    os.makedirs(out_dir, exist_ok=True)
    n_dep, per_pool = _collect(runs_dir, keys, args.limit)

    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]

    def load(i, pool_key):
        info = per_pool[pool_key]
        lo = min(w[1] for w in info["windows"])
        hi = max(w[2] for w in info["windows"])
        path = os.path.join(out_dir, f"{args.network}_{pool_key.replace(' ', '_')}_{lo}_{hi}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
            return pool_key, [tuple(r) for r in cached["index"]], cached["withdrawals"]
        client = clients[i % len(clients)]
        print(f"fetching {pool_key} blocks {lo}-{hi}", file=sys.stderr, flush=True)
        index = fetch_deposit_index(client, addresses[pool_key], lo, hi)
        wblocks = _withdrawal_blocks(client, addresses[pool_key], lo, hi)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"index": index, "withdrawals": wblocks}, fh)
        return pool_key, index, wblocks

    active = [k for k in sorted(keys) if per_pool[k]["windows"]]
    with ThreadPoolExecutor(max_workers=len(clients)) as ex:
        loaded = {k: (idx, wb) for k, idx, wb in ex.map(load, range(len(active)), active)}

    rows = {}
    missing = 0
    for key in active:
        index, wblocks = loaded[key]
        leaf_by_tx = defaultdict(list)
        for _b, leaf, tx in index:
            leaf_by_tx[tx].append(leaf)
        hits = total = windows = 0
        for wallet, sb, eb in per_pool[key]["windows"]:
            leaves = []
            for blk, h in per_pool[key]["deposits"][wallet]:
                if sb <= blk <= eb:
                    found = leaf_by_tx.get(h)
                    if found:
                        leaves.extend(found)
                    else:
                        missing += 1
            lo = bisect.bisect_left(wblocks, sb)
            hi = bisect.bisect_right(wblocks, eb)
            h_, t_ = ceiling_hits(leaves, wblocks[lo:hi], index, args.history)
            hits += h_
            total += t_
            windows += 1
        rows[key] = {"windows": windows, "withdrawals": total, "excludable": hits}

    all_h = sum(r["excludable"] for r in rows.values())
    all_t = sum(r["withdrawals"] for r in rows.values())
    summary = {
        "network": args.network,
        "dir": args.dir,
        "history": args.history,
        "depositors": n_dep,
        "limit": args.limit or None,
        "deposits_without_leaf": missing,
        "pools": rows,
        "withdrawals": all_t,
        "excludable": all_h,
        "ceiling_share": (all_h / all_t) if all_t else 0.0,
    }
    print(json.dumps(summary, indent=1))
    with open(os.path.join(out_dir, f"summary_{args.network}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    _record_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
