#!/usr/bin/env python3
"""Post-hoc (exploratory): shared-deposit leads by the number of distinct senders of the deposit
address. The rule allows up to MAX_DEPOSIT_SENDERS = 50; this re-counts target / decoy leads for
lower caps from the cached runs, without changing the rule.

Usage: python tools/shared_deposit_cap.py holdout1 polygon1 holdout2 polygon2 [--caps 5 10 20 50]
"""

from __future__ import annotations

import argparse
import glob
import json
import os

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "labels", "placebo"
)


def leads(data, cap):
    senders = {
        d["address"].lower(): len(d.get("senders", [])) for d in data.get("deposit_addresses", [])
    }
    out = set()
    for row in data.get("heuristics", {}).get("shared_deposits", []):
        pool, addr, deps = row[0], row[1], row[2]
        if any(senders.get(x.lower(), 0) <= cap for x in deps):
            out.add((pool, addr))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--caps", nargs="+", type=int, default=[5, 10, 20, 50])
    a = ap.parse_args()
    print("| sample | sender cap | target / decoy leads | depositors with decoy lead |")
    print("|---|---|---|---|")
    for sub in a.dirs:
        for cap in a.caps:
            t = dd = wal = 0
            for ft in glob.glob(os.path.join(BASE, sub, "target", "*.json")):
                fd = ft.replace(os.sep + "target" + os.sep, os.sep + "decoy" + os.sep)
                if not os.path.exists(fd):
                    continue
                lt = leads(json.load(open(ft, encoding="utf-8")), cap)
                ld = leads(json.load(open(fd, encoding="utf-8")), cap)
                t += len(lt)
                dd += len(ld)
                wal += bool(ld)
            print(f"| {sub} | {cap} | {t} / {dd} | {wal} |")


if __name__ == "__main__":
    main()
