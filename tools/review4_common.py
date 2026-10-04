"""Shared offline loaders for the review4 scripts (reuses tools/placebo_windows.narrow)."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from placebo_dar import DAR  # noqa: E402
from placebo_windows import OUT  # noqa: E402

from tornado_demix.deposit_addresses import shared_deposit_hits  # noqa: E402

R4 = os.path.join(OUT, "review4")


def recipients(data):
    return {a for res in data.get("denoms", {}).values() for a in res.get("counts", {})}


def _inject_shared(data, wallet):
    """The 152-depositor runs predate the shared-deposit signal: add the labelled
    (evidence-only) hits from the cached deposit-address lookups, as demix.py does."""
    path = os.path.join(DAR, wallet + ".json")
    if "shared_deposits" in data.get("heuristics", {}) or not os.path.exists(path):
        return False
    dep = json.load(open(path, encoding="utf-8"))
    hits = shared_deposit_hits(dep, recipients(data), wallet)  # evidence_only=True
    data["heuristics"]["shared_deposits"] = [("*", a, addrs) for a, addrs in hits.items()]
    return True


def load_sets():
    """{set_name: [(wallet, target, decoy)]} ; 'old152' and 'post150'."""
    sets = {}
    for name, base in (("old152", OUT), ("post150", os.path.join(OUT, "post2022"))):
        names = sorted(
            set(os.listdir(os.path.join(base, "target")))
            & set(os.listdir(os.path.join(base, "decoy")))
        )
        rows = []
        for n in names:
            t, d = (
                json.load(open(os.path.join(base, k, n), encoding="utf-8"))
                for k in ("target", "decoy")
            )
            if name == "old152":
                _inject_shared(t, t["wallet"])
                _inject_shared(d, t["wallet"])
            rows.append((t["wallet"], t, d))
        sets[name] = rows
    return sets


def exposure(data):
    return sum(sum(res["counts"].values()) for res in data["denoms"].values())


def boot_ratio(rows, rng, n=2000):
    """rows: [(t, te, d, de)] per depositor. R=(D/De)/(T/Te); percentile bootstrap over depositors."""

    def est(rs):
        t = sum(r[0] for r in rs)
        te = sum(r[1] for r in rs)
        d = sum(r[2] for r in rs)
        de = sum(r[3] for r in rs)
        return (d / de) / (t / te) if t and te and de else None

    v = est(rows)
    bs = sorted(
        x
        for x in (est([rows[rng.randrange(len(rows))] for _ in rows]) for _ in range(n))
        if x is not None
    )
    ci = (
        [round(bs[int(0.025 * len(bs))], 3), round(bs[int(0.975 * len(bs)) - 1], 3)] if bs else None
    )
    return v, ci
