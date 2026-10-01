#!/usr/bin/env python3
"""Placebo chance share per period: before the sanctions, under them, after the delisting.

Re-scores the target and decoy runs of tools/placebo_eval.py offline (see
tools/placebo_windows.py) and splits the depositors by their first deposit:
before 2022-08-08 (OFAC sanctions), 2022-08-08 to 2025-03-21 (delisting), and
after. The users of the pools changed between these periods, so a pooled chance
share could hide a difference. Counts leads of the linked-address family and of
all strong and moderate candidates under the current rule. No explorer calls.

Usage
-----
    python tools/placebo_periods.py
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from placebo_windows import OUT, narrow  # noqa: E402

from tornado_demix.heuristics import METHOD_FAMILY, ranked_candidates  # noqa: E402

SANCTIONS = 1659916800  # 2022-08-08
DELISTING = 1742515200  # 2025-03-21
PERIODS = ("before sanctions", "under sanctions", "after delisting")
HOURS = (72, 720)


def period(data):
    ts = min(v["first_ts"] for v in data["vouchers"])
    return PERIODS[0] if ts < SANCTIONS else PERIODS[1] if ts < DELISTING else PERIODS[2]


def tally(data):
    c = Counter()
    for r in ranked_candidates(data):
        if r["band"] not in ("strong", "moderate"):
            continue
        c["strong+moderate"] += 1
        if "linked address" in {METHOD_FAMILY.get(s) for s in r["signals"]}:
            c["linked address"] += 1
    exposure = sum(sum(res["counts"].values()) for res in data["denoms"].values())
    return c, exposure


def share(rows, key):
    t = sum(a[0][key] for a, _b in rows)
    d = sum(b[0][key] for _a, b in rows)
    te = sum(a[1] for a, _b in rows)
    de = sum(b[1] for _a, b in rows)
    return ((d / de) / (t / te) if t and te and de else None), t, d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--dir", default="", help="sub-directory of the placebo cache (tools/placebo_eval.py --dir)"
    )
    args = ap.parse_args(argv)
    base = os.path.join(OUT, args.dir) if args.dir else OUT
    names = sorted(
        set(os.listdir(os.path.join(base, "target"))) & set(os.listdir(os.path.join(base, "decoy")))
    )
    runs = [
        [json.load(open(os.path.join(base, k, n), encoding="utf-8")) for k in ("target", "decoy")]
        for n in names
    ]
    rng = random.Random(21)
    out = {"depositors": len(runs), "periods": {}}
    for name in PERIODS:
        sub = [r for r in runs if period(r[0]) == name]
        res = {"depositors": len(sub)}
        for hours in HOURS:
            rows = [(tally(narrow(t, hours)), tally(narrow(dc, hours))) for t, dc in sub]
            rows = [r for r in rows if r[0][1] and r[1][1]]
            for key in ("linked address", "strong+moderate"):
                v, t, d = share(rows, key)
                bs = sorted(
                    x
                    for x in (
                        share([rows[rng.randrange(len(rows))] for _ in rows], key)[0]
                        for _ in range(2000)
                    )
                    if x is not None
                )
                res[f"{key} {hours}h"] = {
                    "target": t,
                    "decoy": d,
                    "fdr": round(v, 3) if v is not None else None,
                    "fdr_95ci": [
                        round(bs[int(0.025 * len(bs))], 3),
                        round(bs[int(0.975 * len(bs)) - 1], 3),
                    ]
                    if bs
                    else None,
                }
        out["periods"][name] = res
    with open(os.path.join(base, "periods.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    for name, res in out["periods"].items():
        print(f"\n== {name} ({res['depositors']} depositors)")
        for k, r in res.items():
            if k != "depositors":
                print(
                    f"  {k:24s} target {r['target']:3d}  decoy {r['decoy']:3d}  FDR {r['fdr']}  CI {r['fdr_95ci']}"
                )


if __name__ == "__main__":
    main()
