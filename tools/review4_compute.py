#!/usr/bin/env python3
"""Offline per-depositor tallies under the current rule (v2.17) for the review4 analyses.

Re-applies the heuristics with tools/placebo_windows.narrow (no explorer calls) to every
cached target/decoy run of the 152 old and 150 post-2022 depositors, for the 72 h and 720 h
windows, and stores compact per-depositor tallies in .cache/labels/placebo/review4/compact.json.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from review4_common import OUT, R4, _inject_shared, exposure, narrow  # noqa: E402

from tornado_demix.heuristics import LEAD_SOURCE, ranked_candidates  # noqa: E402

HOURS = (72, 720)
SETS = {"old152": OUT, "post150": os.path.join(OUT, "post2022")}


def tally(data):
    cands = ranked_candidates(data)
    bands, sig, combos, strong = Counter(), Counter(), Counter(), []
    for r in cands:
        bands[r["band"]] += 1
        for s in r["signals"]:
            sig[s] += 1
        if r["band"] == "strong":
            src = sorted({LEAD_SOURCE[s] for s in r["signals"] if s in LEAD_SOURCE})
            combos[" + ".join(src)] += 1
            strong.append([r["pool_key"], r["address"], r["signals"]])
    rowsig = Counter()
    for res in data["denoms"].values():
        for addr in res["detail"]:
            rowsig[",".join(res["signals"].get(addr, []))] += 1
    return {
        "exposure": exposure(data),
        "bands": dict(bands),
        "signals_in_candidates": dict(sig),
        "strong_combos": dict(combos),
        "strong_rows": strong,
        "row_signal_sets": dict(rowsig),
    }


def work(args):
    sname, name = args
    base = SETS[sname]
    t, d = (
        json.load(open(os.path.join(base, k, name), encoding="utf-8")) for k in ("target", "decoy")
    )
    out = {"set": sname, "wallet": t["wallet"]}
    # "asis": the cached runs exactly as the thesis counted them (the 152 old runs predate
    # the shared-deposit signal). "inj": labelled shared-deposit hits from the cached
    # deposit-address lookups injected, i.e. the full v2.17 signal set.
    for h in HOURS:
        out[str(h)] = {"target": tally(narrow(t, h)), "decoy": tally(narrow(d, h))}
    if sname == "old152":
        out["shared_injected"] = _inject_shared(t, t["wallet"]) | _inject_shared(d, t["wallet"])
        out["inj"] = {
            str(h): {"target": tally(narrow(t, h)), "decoy": tally(narrow(d, h))} for h in HOURS
        }
    return out


def main():
    jobs = []
    for sname, base in SETS.items():
        names = sorted(
            set(os.listdir(os.path.join(base, "target")))
            & set(os.listdir(os.path.join(base, "decoy")))
        )
        jobs += [(sname, n) for n in names]
    with ProcessPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(work, jobs, chunksize=4))
    os.makedirs(R4, exist_ok=True)
    with open(os.path.join(R4, "compact.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    print("depositors", len(res))


if __name__ == "__main__":
    main()
