#!/usr/bin/env python3
"""Permutation control for the early multi-pool profile, offline.

The placebo test of tools/placebo_profile.py moves the window back in time. This
control keeps the real windows and swaps the profile instead: each eligible
depositor (at least 10 notes over two or more pools) is matched, in its own real
72-hour windows, against the profile of another depositor that used the same set
of pools. If recipients that receive the wallet's own profile were chance, a
borrowed profile would find as many. Repeated over several random pairings; no
explorer calls.

Usage
-----
    python tools/placebo_profile_perm.py
"""

from __future__ import annotations

import argparse
import bisect
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
HOURS = 72
MIN_NOTES = 10
ROUNDS = 20


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--universe", default="universe.json", help="universe file (tools/ens_labels.py)"
    )
    ap.add_argument("--out", default="profile_perm.json")
    args = ap.parse_args(argv)
    uni = _load(args.universe)
    wd = defaultdict(list)
    for pool, addr, ts, _h in uni["withdrawals"]:
        wd[pool].append((ts, addr))
    index = {}
    for pool, rows in wd.items():
        rows.sort()
        index[pool] = ([t for t, _a in rows], [a for _t, a in rows])
    by_dep = defaultdict(list)
    for pool, addr, ts, _h in uni["deposits"]:
        by_dep[addr].append((pool, ts))

    deps = []
    for _d, items in by_dep.items():
        stamps = [t for _p, t in items]
        if max(stamps) - min(stamps) > MAX_SPAN_DAYS * DAY or max(stamps) + 30 * DAY > END_TS:
            continue
        profile = Counter(p for p, _t in items)
        if sum(profile.values()) < MIN_NOTES or len(profile) < 2:
            continue
        per_pool = defaultdict(list)
        for p, t in items:
            per_pool[p].append(t)
        counts = {}
        for p, st in per_pool.items():
            ts, addrs = index[p]
            i = bisect.bisect_left(ts, min(st))
            j = bisect.bisect_right(ts, max(st) + HOURS * 3600)
            counts[p] = Counter(addrs[i:j])
        deps.append((frozenset(profile), dict(profile), counts))

    def matches(profile, counts):
        pools = list(profile)
        cands = set(counts[pools[0]])
        for p in pools[1:]:
            cands &= set(counts[p])
        return sum(1 for a in cands if all(counts[p][a] == profile[p] for p in pools))

    own = sum(matches(prof, cnt) for _k, prof, cnt in deps)
    groups = defaultdict(list)
    for i, (key, _prof, _cnt) in enumerate(deps):
        groups[key].append(i)
    rng = random.Random(17)
    borrowed = []
    usable = [i for i, (key, _p, _c) in enumerate(deps) if len(groups[key]) > 1]
    own_usable = sum(matches(deps[i][1], deps[i][2]) for i in usable)
    for _ in range(ROUNDS):
        total = 0
        for i in usable:
            key = deps[i][0]
            j = i
            while j == i:
                j = rng.choice(groups[key])
            if deps[j][1] == deps[i][1]:
                continue  # identical profile: not a swap
            total += matches(deps[j][1], deps[i][2])
        borrowed.append(total)
    borrowed.sort()
    out = {
        "depositors": len(deps),
        "with_same_pool_set_partner": len(usable),
        "own_profile_hits": own,
        "own_profile_hits_usable": own_usable,
        "borrowed_profile_hits": {
            "rounds": ROUNDS,
            "mean": round(sum(borrowed) / len(borrowed), 1),
            "min": borrowed[0],
            "max": borrowed[-1],
        },
    }
    with open(os.path.join(CACHE, "placebo", args.out), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
