#!/usr/bin/env python3
"""Placebo false-lead rate as a function of the exit window, from cached runs.

Re-scores the target and decoy runs of tools/placebo_eval.py offline for exit
windows of N hours after each voucher's last deposit: withdrawals outside the
window are dropped, counts and the count gate are recomputed, and the heuristics
are re-applied with the evidence the full run already gathered (linked
addresses, linked senders, gated gas-price matches). No explorer calls.

The gas-price signal is kept only for withdrawals whose match passed the gate in
the full run, so a price that was too common in the 30-day window but rare in a
narrow one is not re-credited; this affects target and decoy alike.

The full window (720 h) must reproduce the cached bands; the script checks it.

Usage
-----
    python tools/placebo_windows.py --hours 6 24 72 720
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE  # noqa: E402

from tornado_demix.heuristics import (  # noqa: E402
    METHOD_FAMILY,
    apply_heuristics,
    count_is_evidence,
    ranked_candidates,
)

OUT = os.path.join(CACHE, "placebo")
FAMILIES = ("linked address", "amount+timing", "gas price")


def narrow(data, hours):
    d = copy.deepcopy(data)
    h = d.get("heuristics", {})
    gas_ok = {m[3].lower() for m in h.get("gas_price_matches", [])}
    senders = {s[3].lower(): s[2] for s in h.get("linked_senders", [])}
    linked = {a for _p, a in h.get("linked_addresses", [])}
    for pool_key, res in d.get("denoms", {}).items():
        spans = [
            (v["first_ts"], v["last_ts"] + hours * 3600)
            for v in d["vouchers"]
            if v["pool_key"] == pool_key
        ]
        detail = {}
        for addr, recs in res["detail"].items():
            keep = [
                dict(
                    r,
                    gas_price=r.get("gas_price")
                    if (r.get("hash") or "").lower() in gas_ok
                    else None,
                )
                for r in recs
                if any(lo <= r["ts"] <= hi for lo, hi in spans)
            ]
            if keep:
                detail[addr] = keep
        res["detail"] = detail
        res["counts"] = {a: len(r) for a, r in detail.items()}
        res["unique_recipients"] = len(detail)
        sizes = [v["count"] for v in d["vouchers"] if v["pool_key"] == pool_key]
        res["target_counts"] = sorted(set(sizes) | ({sum(sizes)} if len(sizes) > 1 else set()))
        res["candidates_by_count"] = {
            n: [a for a, c in res["counts"].items() if c == n] if count_is_evidence(res, n) else []
            for n in res["target_counts"]
        }
    apply_heuristics(d, linked, gas_gate=None, is_contract=None, withdrawal_senders=senders)
    return d


def tally(data):
    fam = Counter()
    bands = Counter()
    for r in ranked_candidates(data):
        bands[r["band"]] += 1
        if r["band"] in ("strong", "moderate"):
            for f in {METHOD_FAMILY.get(s) for s in r["signals"]} - {None}:
                fam[f] += 1
    fam["strong+moderate"] = bands["strong"] + bands["moderate"]
    exposure = sum(sum(res["counts"].values()) for res in data["denoms"].values())
    return fam, exposure


def fdr(rows, key):
    t = sum(r[0][0][key] for r in rows)
    d = sum(r[1][0][key] for r in rows)
    te = sum(r[0][1] for r in rows)
    de = sum(r[1][1] for r in rows)
    return ((d / de) / (t / te) if t and te and de else None), t, d, te, de


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, nargs="+", default=[6, 24, 72, 720])
    ap.add_argument("--boot", type=int, default=2000)
    args = ap.parse_args(argv)
    names = sorted(
        set(os.listdir(os.path.join(OUT, "target"))) & set(os.listdir(os.path.join(OUT, "decoy")))
    )
    runs = []
    for n in names:
        pair = [
            json.load(open(os.path.join(OUT, k, n), encoding="utf-8")) for k in ("target", "decoy")
        ]
        runs.append(pair)

    # Sanity: the full window must reproduce the cached bands.
    mismatch = 0
    for pair in runs:
        for data in pair:
            a = sorted((r["pool_key"], r["address"], r["band"]) for r in ranked_candidates(data))
            b = sorted(
                (r["pool_key"], r["address"], r["band"])
                for r in ranked_candidates(narrow(data, 30 * 24))
            )
            mismatch += a != b
    print(f"full-window reproduction mismatches: {mismatch} of {2 * len(runs)} runs")

    rng = random.Random(5)
    out = {"depositors": len(runs), "reproduction_mismatches": mismatch, "windows": {}}
    for hours in args.hours:
        rows = [(tally(narrow(t, hours)), tally(narrow(dc, hours))) for t, dc in runs]
        rows = [r for r in rows if r[0][1] and r[1][1]]
        res = {"depositors_with_withdrawals": len(rows)}
        for key in FAMILIES + ("strong+moderate",):
            v, t, d, te, de = fdr(rows, key)
            bs = sorted(
                x
                for x in (
                    fdr([rows[rng.randrange(len(rows))] for _ in rows], key)[0]
                    for _ in range(args.boot)
                )
                if x is not None
            )
            res[key] = {
                "target": t,
                "decoy": d,
                "target_withdrawals": te,
                "decoy_withdrawals": de,
                "fdr": round(v, 3) if v is not None else None,
                "fdr_95ci": [
                    round(bs[int(0.025 * len(bs))], 3),
                    round(bs[int(0.975 * len(bs)) - 1], 3),
                ]
                if bs
                else None,
            }
        out["windows"][f"{hours:g}h"] = res
    with open(os.path.join(OUT, "windows.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    for w, res in out["windows"].items():
        print(f"\n== {w} (depositors {res['depositors_with_withdrawals']})")
        for k in FAMILIES + ("strong+moderate",):
            r = res[k]
            print(
                f"  {k:16s} target {r['target']:4d}  decoy {r['decoy']:4d}  FDR {r['fdr']}  CI {r['fdr_95ci']}"
            )


if __name__ == "__main__":
    main()
