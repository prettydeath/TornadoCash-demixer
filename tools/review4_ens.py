#!/usr/bin/env python3
"""Strong candidates of the ENS evaluation under the current rule (v2.17), from cached runs."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE  # noqa: E402
from review4_common import R4, narrow  # noqa: E402

from tornado_demix.heuristics import LEAD_SOURCE, ranked_candidates  # noqa: E402

RUNS = os.path.join(CACHE, "demix")
pairs = json.load(open(os.path.join(CACHE, "pairs.json"), encoding="utf-8"))
pairs = pairs["pairs"] if isinstance(pairs, dict) else pairs
partners = {(p["depositor"], p["pool_key"], p["recipient"]) for p in pairs}
out = {"runs": 0, "bands": Counter(), "strong": [], "strong_combos": Counter()}
for n in sorted(os.listdir(RUNS)):
    d = json.load(open(os.path.join(RUNS, n), encoding="utf-8"))
    out["runs"] += 1
    for r in ranked_candidates(narrow(d, 720)):
        out["bands"][r["band"]] += 1
        if r["band"] == "strong":
            src = " + ".join(sorted({LEAD_SOURCE[s] for s in r["signals"] if s in LEAD_SOURCE}))
            out["strong_combos"][src] += 1
            out["strong"].append(
                {
                    "depositor": d["wallet"],
                    "pool": r["pool_key"],
                    "address": r["address"],
                    "signals": r["signals"],
                    "labelled_pair": (d["wallet"], r["pool_key"], r["address"]) in partners,
                }
            )
out["bands"] = dict(out["bands"])
out["strong_combos"] = dict(out["strong_combos"])
json.dump(out, open(os.path.join(R4, "ens_strong.json"), "w", encoding="utf-8"), indent=1)
print(json.dumps(out, indent=1))
