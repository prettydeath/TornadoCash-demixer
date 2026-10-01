#!/usr/bin/env python3
"""Does a second family make a lead stronger? Chance share per band, from cached runs.

Re-scores the target and decoy runs of tools/placebo_eval.py offline (see
tools/placebo_windows.py) and answers two questions:

1. Among candidates with a lead signal (a linked address or an early multi-pool
   profile match), is the chance share lower when an amount+timing or gas-price
   signal comes on top? Before version 2.16 such a candidate was ``strong``.
2. What is the chance share of each band under the current rule?

The chance share is decoy leads per withdrawal searched over target leads per
withdrawal searched; 95 % intervals come from a bootstrap over depositors. The
cached runs predate the shared-deposit signal, so ``strong`` here can only come
from a direct link plus an early multi-pool profile. No explorer calls.

Usage
-----
    python tools/placebo_strong.py
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

from tornado_demix.heuristics import LEAD_SIGNALS, ranked_candidates  # noqa: E402

AMOUNT_TIMING = {"count_match", "self_relayed", "profile_match"}
LINKED = {"linked", "linked_sender", "shared_deposit"}
KEYS = (
    "lead, nothing else",
    "lead + amount+timing",
    "lead + gas price",
    "strong",
    "moderate",
    "strong+moderate",
)
HOURS = (72, 720)


def tally(data):
    c = Counter()
    for r in ranked_candidates(data):
        s = set(r["signals"])
        c[r["band"]] += 1
        if not s & LEAD_SIGNALS:
            continue
        # early_profile is in the amount+timing family; only other signals corroborate
        at = bool(s & AMOUNT_TIMING) and bool(s & LINKED)
        if "gas_price" in s:
            c["lead + gas price"] += 1
        if at:
            c["lead + amount+timing"] += 1
        if not at and "gas_price" not in s:
            c["lead, nothing else"] += 1
    c["strong+moderate"] = c["strong"] + c["moderate"]
    exposure = sum(sum(res["counts"].values()) for res in data["denoms"].values())
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
    rng = random.Random(11)
    out = {"depositors": len(runs), "windows": {}}
    for hours in HOURS:
        rows = [(tally(narrow(t, hours)), tally(narrow(dc, hours))) for t, dc in runs]
        rows = [r for r in rows if r[0][1] and r[1][1]]
        res = {}
        for key in KEYS:
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
    with open(os.path.join(OUT, "strong.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    for w, res in out["windows"].items():
        print(f"\n== {w}")
        for k in KEYS:
            r = res[k]
            print(
                f"  {k:22s} target {r['target']:4d}  decoy {r['decoy']:4d}  FDR {r['fdr']}  CI {r['fdr_95ci']}"
            )


if __name__ == "__main__":
    main()
