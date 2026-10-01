#!/usr/bin/env python3
"""Placebo test of a single-wallet denomination-profile match, offline.

For every eligible depositor in the ENS universe (the four Ethereum ETH pools; see
tools/ens_labels.py), the profile is its note count per pool. A recipient matches
when, inside the depositor's windows (per pool: first deposit to last deposit plus
``hours``), it received exactly that many withdrawals in every pool the depositor
used. The same count is made on decoy windows shifted back to end a day before
the first deposit. The chance share is decoy hits per withdrawal searched over
target hits per withdrawal searched, as in tools/placebo_eval.py; 95 % intervals
come from a bootstrap over depositors. No explorer calls.

Usage
-----
    python tools/placebo_profile.py
    python tools/placebo_profile.py --period before --out profile_before.json
    python tools/placebo_profile.py --notes 6 10 15 --hours 24 72 168 --out profile_grid.json

``--period before|after`` keeps depositors whose first deposit is before or after
``--split`` (default 2022-08-08, the sanctions), for a hold-out check of the
thresholds chosen on the whole set.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import random
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE, _load  # noqa: E402
from placebo_eval import END_TS, MAX_SPAN_DAYS  # noqa: E402

DAY = 86400
HOURS = (24, 72, 720)
MIN_NOTES = (2, 6, 10)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--notes", type=int, nargs="+", default=list(MIN_NOTES))
    ap.add_argument("--hours", type=int, nargs="+", default=list(HOURS))
    ap.add_argument("--period", choices=("all", "before", "after"), default="all")
    ap.add_argument("--split", default="2022-08-08")
    ap.add_argument("--out", default="profile.json")
    ap.add_argument(
        "--universe", default="universe.json", help="universe file (tools/ens_labels.py)"
    )
    args = ap.parse_args(argv)
    split_ts = int(
        dt.datetime.fromisoformat(args.split).replace(tzinfo=dt.timezone.utc).timestamp()
    )
    uni = _load(args.universe)
    wd = defaultdict(list)
    first_w = {}
    for pool, addr, ts, _h in uni["withdrawals"]:
        wd[pool].append((ts, addr))
        first_w[pool] = min(first_w.get(pool, ts), ts)
    index = {}
    for pool, rows in wd.items():
        rows.sort()
        index[pool] = ([t for t, _a in rows], [a for _t, a in rows])
    by_dep = defaultdict(list)
    for pool, addr, ts, _h in uni["deposits"]:
        by_dep[addr].append((pool, ts))

    def window_counts(deps, hours, shift):
        per_pool = defaultdict(list)
        for pool, ts in deps:
            per_pool[pool].append(ts)
        counts, searched = {}, 0
        for pool, stamps in per_pool.items():
            ts, addrs = index[pool]
            lo, hi = min(stamps) - shift, max(stamps) + hours * 3600 - shift
            i, j = bisect.bisect_left(ts, lo), bisect.bisect_right(ts, hi)
            counts[pool] = Counter(addrs[i:j])
            searched += j - i
        return counts, searched

    rows = []
    for _dep, deps in by_dep.items():
        stamps = [t for _p, t in deps]
        span = max(stamps) - min(stamps)
        if span > MAX_SPAN_DAYS * DAY or max(stamps) + 30 * DAY > END_TS:
            continue
        profile = Counter(p for p, _t in deps)
        notes = sum(profile.values())
        if notes < min(args.notes):
            continue
        if args.period == "before" and min(stamps) >= split_ts:
            continue
        if args.period == "after" and min(stamps) < split_ts:
            continue
        row = {"notes": notes, "pools": len(profile)}
        ok = True
        for hours in args.hours:
            offset = span + hours * 3600 + DAY
            if any(t - offset < first_w.get(p, 1 << 62) + DAY for p, t in deps):
                ok = False
                break
            for kind, shift in (("t", 0), ("d", offset)):
                counts, searched = window_counts(deps, hours, shift)
                pools = list(profile)
                cands = set(counts[pools[0]])
                for p in pools[1:]:
                    cands &= set(counts[p])
                hits = sum(1 for a in cands if all(counts[p][a] == profile[p] for p in pools))
                row[f"{kind}{hours}"] = hits
                row[f"{kind}{hours}_n"] = searched
        if ok:
            rows.append(row)

    def est(rs, hours):
        t = sum(r[f"t{hours}"] for r in rs)
        d = sum(r[f"d{hours}"] for r in rs)
        tn = sum(r[f"t{hours}_n"] for r in rs)
        dn = sum(r[f"d{hours}_n"] for r in rs)
        return ((d / dn) / (t / tn) if t and tn and dn else None), t, d

    rng = random.Random(8)
    out = {"depositors": len(rows), "period": args.period, "split": args.split, "cells": []}
    for m in args.notes:
        for min_pools in (1, 2):
            sub = [r for r in rows if r["notes"] >= m and r["pools"] >= min_pools]
            for hours in args.hours:
                v, t, d = est(sub, hours)
                bs = (
                    sorted(
                        x
                        for x in (
                            est([sub[rng.randrange(len(sub))] for _ in sub], hours)[0]
                            for _ in range(1000)
                        )
                        if x is not None
                    )
                    if sub
                    else []
                )
                out["cells"].append(
                    {
                        "min_notes": m,
                        "min_pools": min_pools,
                        "hours": hours,
                        "depositors": len(sub),
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
                )
    with open(os.path.join(CACHE, "placebo", args.out), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"depositors {out['depositors']}")
    for c in out["cells"]:
        print(
            f"notes>={c['min_notes']:2d} pools>={c['min_pools']} {c['hours']:4d}h  dep {c['depositors']:6d}  "
            f"target {c['target']:5d} decoy {c['decoy']:5d}  FDR {c['fdr']}  CI {c['fdr_95ci']}"
        )


if __name__ == "__main__":
    main()
