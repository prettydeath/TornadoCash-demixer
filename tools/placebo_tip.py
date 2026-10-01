#!/usr/bin/env python3
"""Placebo check of a post-EIP-1559 priority-tip signal (rejected: chance level).

tip = effective gas price - the block's base fee. A self-relayed withdrawal whose
tip equals one of the depositor's deposit tips, and is rare among the
self-relayed withdrawals of that pool window (``--rare``, default 3), is a hit.
Hits are counted in real and decoy windows of the cached placebo runs (the first
sample and ``post2022``); base fees come from a public JSON-RPC node and are
cached in .cache/labels/placebo/basefee.json.

Result (302 depositors): chance share 1.57 at 72 h and 1.26 over 30 days with
``--rare 3``; tips cluster on wallet defaults (3, 0.5, 1, 2 gwei), so a shared tip
says nothing about who withdrew. The signal is not used.

Usage
-----
    python tools/placebo_tip.py [--rare 3]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from placebo_windows import OUT, narrow  # noqa: E402

LONDON_BLOCK = 12_965_000
RPC = "https://ethereum-rpc.publicnode.com"
FEE_CACHE = os.path.join(OUT, "basefee.json")
_fetch = urllib.request.urlopen  # JSON-RPC over HTTP, not a file


def load_runs():
    runs = []
    for d in (OUT, os.path.join(OUT, "post2022")):
        names = sorted(
            set(os.listdir(os.path.join(d, "target"))) & set(os.listdir(os.path.join(d, "decoy")))
        )
        for n in names:
            runs.append(
                [
                    json.load(open(os.path.join(d, k, n), encoding="utf-8"))
                    for k in ("target", "decoy")
                ]
            )
    return runs


def base_fees(runs):
    """{block: base fee} for every post-London deposit and self-relayed withdrawal."""
    blocks = set()
    for pair in runs:
        for data in pair:
            for dep in data["deposits"]:
                if dep.get("block", 0) >= LONDON_BLOCK and dep.get("gas_price"):
                    blocks.add(int(dep["block"]))
            for res in data["denoms"].values():
                for recs in res["detail"].values():
                    for r in recs:
                        if r.get("self_relayed") and r.get("block", 0) >= LONDON_BLOCK:
                            blocks.add(int(r["block"]))
    fee = {}
    if os.path.exists(FEE_CACHE):
        with open(FEE_CACHE, encoding="utf-8") as fh:
            fee = json.load(fh)
    need = sorted(b for b in blocks if str(b) not in fee)
    for i in range(0, len(need), 50):
        batch = [
            {"jsonrpc": "2.0", "id": b, "method": "eth_getBlockByNumber", "params": [hex(b), False]}
            for b in need[i : i + 50]
        ]
        for attempt in range(6):
            try:
                req = urllib.request.Request(
                    RPC, json.dumps(batch).encode(), {"content-type": "application/json"}
                )
                with _fetch(req, timeout=60) as resp:
                    for o in json.load(resp):
                        if o.get("result") and o["result"].get("baseFeePerGas"):
                            fee[str(o["id"])] = int(o["result"]["baseFeePerGas"], 16)
                break
            except (OSError, ValueError):
                time.sleep(2 * (attempt + 1))
        time.sleep(0.15)
    with open(FEE_CACHE, "w", encoding="utf-8") as fh:
        json.dump(fee, fh)
    return fee


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rare", type=int, default=3)
    args = ap.parse_args(argv)

    runs = load_runs()
    fee = base_fees(runs)
    # narrow() keeps a gas price only for matches gated in the full run, so the
    # raw prices are looked up by transaction hash.
    gas = {}
    for pair in runs:
        for data in pair:
            for res in data["denoms"].values():
                for recs in res["detail"].values():
                    for r in recs:
                        if r.get("gas_price"):
                            gas[(r.get("hash") or "").lower()] = r["gas_price"]

    def tip(block, price):
        bf = fee.get(str(int(block)))
        return None if bf is None or not price else int(price) - bf

    def tally(data):
        dep_tips = {
            tip(d["block"], d["gas_price"])
            for d in data["deposits"]
            if d.get("block", 0) >= LONDON_BLOCK
        }
        dep_tips.discard(None)
        hits = 0
        for res in data["denoms"].values():
            seen = Counter()
            per_addr = {}
            for addr, recs in res["detail"].items():
                tips = [
                    tip(r["block"], gas.get((r.get("hash") or "").lower()))
                    for r in recs
                    if r.get("self_relayed") and r.get("block", 0) >= LONDON_BLOCK
                ]
                per_addr[addr] = [t for t in tips if t is not None]
                seen.update(per_addr[addr])
            for addr, tips in per_addr.items():
                if addr != data["wallet"] and any(
                    t in dep_tips and seen[t] <= args.rare for t in tips
                ):
                    hits += 1
        exposure = sum(sum(res["counts"].values()) for res in data["denoms"].values())
        return hits, exposure

    def share(rows):
        t = sum(a[0] for a, _b in rows)
        d = sum(b[0] for _a, b in rows)
        te = sum(a[1] for a, _b in rows)
        de = sum(b[1] for _a, b in rows)
        return ((d / de) / (t / te) if t and de else None), t, d

    rng = random.Random(9)
    for hours in (72, 720):
        rows = [(tally(narrow(t, hours)), tally(narrow(dc, hours))) for t, dc in runs]
        rows = [r for r in rows if r[0][1] and r[1][1]]
        v, t, d = share(rows)
        bs = sorted(
            x
            for x in (share([rows[rng.randrange(len(rows))] for _ in rows])[0] for _ in range(2000))
            if x is not None
        )
        ci = (
            [round(bs[int(0.025 * len(bs))], 3), round(bs[int(0.975 * len(bs)) - 1], 3)]
            if bs
            else None
        )
        print(
            f"{hours} h: target {t}  decoy {d}  chance share {None if v is None else round(v, 3)}  CI {ci}"
        )


if __name__ == "__main__":
    main()
