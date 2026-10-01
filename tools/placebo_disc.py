#!/usr/bin/env python3
"""Does the discrimination gate remove chance count matches? From cached runs.

For every target and decoy run of tools/placebo_eval.py (re-scored offline for
the window, see tools/placebo_windows.py), every recipient whose withdrawal count
equals a voucher size is a count match, gated or not. The matches are binned by
the discrimination of their count (count_discrimination) and by field size, and
the chance share (decoy matches per withdrawal searched over target matches per
withdrawal searched) is reported per bin, with a bootstrap 95 % interval over
depositors. If the gate removed chance matches, the share would fall with
discrimination. It also compares the observed number of recipients sharing a
count with a null model that draws each recipient's count from the window's own
count distribution. No explorer calls.

Usage
-----
    python tools/placebo_disc.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from placebo_windows import OUT, narrow  # noqa: E402

from tornado_demix.heuristics import MIN_FIELD_SIZE, count_discrimination  # noqa: E402

BINS = (
    (0.0, 0.5, "disc < 0.5 (gated out)"),
    (0.5, 0.8, "0.5 <= disc < 0.8"),
    (0.8, 0.95, "0.8 <= disc < 0.95"),
    (0.95, 1.01, "disc >= 0.95"),
)
HOURS = (72, 720)


def tally(data):
    c = Counter()
    exposure = 0
    for _pool_key, res in data["denoms"].items():
        counts = res["counts"]
        exposure += sum(counts.values())
        field = len(counts)
        if field < MIN_FIELD_SIZE:
            continue
        for n in res["target_counts"]:
            sharing = sum(1 for v in counts.values() if v == n)
            if not sharing:
                continue
            disc = count_discrimination(res, n)
            label = next(lab for lo, hi, lab in BINS if lo <= disc < hi)
            c[label] += sharing
            c["all"] += sharing
            if disc >= 0.5:
                c["gated in (disc >= 0.5)"] += sharing
            # Null model: each recipient's count drawn from the window's distribution.
            p = sharing / field
            c["null expected"] += field * p
    return c, exposure


def share(rows, key):
    t = sum(a[0][key] for a, _b in rows)
    d = sum(b[0][key] for _a, b in rows)
    te = sum(a[1] for a, _b in rows)
    de = sum(b[1] for _a, b in rows)
    return ((d / de) / (t / te) if t and te and de else None), t, d


def main():
    names = sorted(
        set(os.listdir(os.path.join(OUT, "target"))) & set(os.listdir(os.path.join(OUT, "decoy")))
    )
    runs = [
        [json.load(open(os.path.join(OUT, k, n), encoding="utf-8")) for k in ("target", "decoy")]
        for n in names
    ]
    keys = ["all", "gated in (disc >= 0.5)"] + [lab for _lo, _hi, lab in BINS]
    rng = random.Random(13)
    out = {"depositors": len(runs), "windows": {}}
    for hours in HOURS:
        rows = [(tally(narrow(t, hours)), tally(narrow(dc, hours))) for t, dc in runs]
        rows = [r for r in rows if r[0][1] and r[1][1]]
        res = {}
        for key in keys:
            v, t, d = share(rows, key)
            bs = sorted(
                x
                for x in (
                    share([rows[rng.randrange(len(rows))] for _ in rows], key)[0]
                    for _ in range(2000)
                )
                if x is not None
            )
            res[key] = {
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
        out["windows"][f"{hours}h"] = res
    with open(os.path.join(OUT, "disc.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    for w, res in out["windows"].items():
        print(f"\n== {w}")
        for k in keys:
            r = res[k]
            print(
                f"  {k:24s} target {r['target']:6d}  decoy {r['decoy']:6d}  FDR {r['fdr']}  CI {r['fdr_95ci']}"
            )


if __name__ == "__main__":
    main()
