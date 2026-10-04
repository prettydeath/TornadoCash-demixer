#!/usr/bin/env python3
"""Two-source combinations on the 1,000-depositor shared-deposit universe, offline.

The direct-link source needs the depositor's counterparties (explorer transaction
history), which is not cached for the universe, so "early direct link + shared deposit"
cannot be formed there. The early multi-pool profile source can be computed from the
universe itself (same rule as tools/placebo_profile.py, 10 notes, 2 pools, 72 h), so
"early profile + labelled shared deposit" is counted. Output: review4/universe_combos.json.
"""

from __future__ import annotations

import bisect
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import _load  # noqa: E402
from placebo_dar_universe import WINDOW  # noqa: E402
from review4_common import OUT, R4  # noqa: E402

DAY = 86400
uni = _load("universe.json")
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

dar = json.load(open(os.path.join(OUT, "dar_universe.json"), encoding="utf-8"))["rows"]


def profile_hits(deps, shift, hours=72):
    per = defaultdict(list)
    for p, t in deps:
        per[p].append(t)
    counts = {}
    for p, stamps in per.items():
        ts, addrs = index[p]
        lo, hi = min(stamps) - shift, max(stamps) + hours * 3600 - shift
        i, j = bisect.bisect_left(ts, lo), bisect.bisect_right(ts, hi)
        counts[p] = Counter(addrs[i:j])
    need = {p: len(s) for p, s in per.items()}
    pools = list(need)
    cands = set(counts[pools[0]])
    for p in pools[1:]:
        cands &= set(counts[p])
    return {a for a in cands if all(counts[p][a] == need[p] for p in pools)}


res = Counter()
detail = []
for r in dar:
    w = r["wallet"]
    deps = by_dep[w]
    stamps = [t for _p, t in deps]
    span = max(stamps) - min(stamps)
    res["depositors"] += 1
    ev_t, ev_d = set(r["evidence"]["target"]), set(r["evidence"]["decoy"])
    res["shared_labelled_target"] += len(ev_t)
    res["shared_labelled_decoy"] += len(ev_d)
    profile = Counter(p for p, _t in deps)
    if len(profile) < 2 or sum(profile.values()) < 10:
        continue
    offset = span + WINDOW + DAY  # same decoy shift as the shared-deposit universe run
    res["profile_eligible_depositors"] += 1
    th, dh = profile_hits(deps, 0), profile_hits(deps, offset)
    res["profile_target_hits"] += len(th)
    res["profile_decoy_hits"] += len(dh)
    both_t, both_d = th & ev_t, dh & ev_d
    res["profile_and_shared_target"] += len(both_t)
    res["profile_and_shared_decoy"] += len(both_d)
    if both_t or both_d:
        detail.append({"wallet": w, "target": sorted(both_t), "decoy": sorted(both_d)})
out = dict(res)
out["detail"] = detail
json.dump(out, open(os.path.join(R4, "universe_combos.json"), "w", encoding="utf-8"), indent=1)
print(json.dumps(out, indent=1))
