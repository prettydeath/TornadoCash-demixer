#!/usr/bin/env python3
"""Decoy rates, cluster-aware rate ratios and lead decomposition for placebo samples.

For each sample directory under .cache/labels/placebo (target/ and decoy/ runs), with the
current rule (tornado_demix.heuristics.ranked_candidates):

* decoy rate per wallet: share of depositors with at least one decoy lead of a kind, with a
  Clopper–Pearson 95 % upper bound (an empirical false-positive rate per investigated wallet);
* decoy rate per candidate: decoy leads / distinct recipients examined in decoy windows;
* rate ratio R from a Poisson GLM with log-exposure offset and depositor-clustered
  (sandwich) standard errors, next to the plain ratio;
* decomposition of leads: the candidate is the depositor itself, one of its funders, or other;
* overlap with the early-profile universe U (depositors with >= 10 notes in >= 2 pools).

Usage: python tools/decoy_rates.py holdout1 polygon1 [. post2022 ...] [--out article]
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import Counter

import numpy as np
from scipy.stats import beta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from placebo_windows import narrow  # noqa: E402

from tornado_demix.heuristics import ranked_candidates  # noqa: E402

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "labels", "placebo"
)
KINDS = [
    "moderate+strong",
    "moderate",
    "strong",
    "linked",
    "shared_deposit",
    "early_profile",
    "linked_sender",
    "linked_late",
    "count_match",
]


def cp_upper(k, n, alpha=0.05):
    return 1.0 if k >= n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))


def leads(data):
    """Per kind: set of (pool, address) leads; plus decomposition for class leads."""
    out = {k: set() for k in KINDS}
    decomp = Counter()
    wallet = (data.get("wallet") or "").lower()
    funders = {
        (f.get("address") if isinstance(f, dict) else f or "").lower()
        for f in data.get("funders", [])
    }
    for r in ranked_candidates(data):
        key = (r["pool_key"], r["address"])
        sig = set(r["signals"])
        if r["band"] in ("moderate", "strong"):
            out["moderate+strong"].add(key)
            out[r["band"]].add(key)
            who = (
                "self"
                if r["address"].lower() == wallet
                else "funder"
                if r["address"].lower() in funders
                else "other"
            )
            decomp[who] += 1
        for s in (
            "linked",
            "shared_deposit",
            "early_profile",
            "linked_sender",
            "linked_late",
            "count_match",
        ):
            if s in sig:
                out[s].add(key)
    return out, decomp


def exposure(data):
    n_w = sum(sum(res.get("counts", {}).values()) for res in data.get("denoms", {}).values())
    n_r = sum(len(res.get("counts", {})) for res in data.get("denoms", {}).values())
    return n_w, n_r


def profile_universe_member(data):
    notes = len(data.get("deposits", []))
    pools = len({d.get("pool_key") for d in data.get("deposits", [])})
    return notes >= 10 and pools >= 2


def cluster_rr(rows):
    """rows: (depositor_index, is_decoy, y, exposure). Poisson GLM, clustered SE."""
    import statsmodels.api as sm

    y = np.array([r[2] for r in rows], float)
    if y[[r[1] == 1 for r in rows]].sum() == 0 or y[[r[1] == 0 for r in rows]].sum() == 0:
        return None
    X = sm.add_constant(np.array([r[1] for r in rows], float))
    off = np.log(np.array([max(r[3], 1) for r in rows], float))
    groups = np.array([r[0] for r in rows])
    try:
        fit = sm.GLM(y, X, family=sm.families.Poisson(), offset=off).fit(
            cov_type="cluster", cov_kwds={"groups": groups}
        )
        b, se = fit.params[1], fit.bse[1]
        return {"rr": math.exp(b), "lo": math.exp(b - 1.96 * se), "hi": math.exp(b + 1.96 * se)}
    except Exception as exc:  # pragma: no cover - reported, not hidden
        return {"error": str(exc)}


def analyse(sub):
    d0 = os.path.join(BASE, sub) if sub != "." else BASE
    rows, per_kind = [], {k: {"t": 0, "d": 0, "wal_t": 0, "wal_d": 0} for k in KINDS}
    exp_t = exp_d = rec_d = 0
    decomp = {"target": Counter(), "decoy": Counter()}
    n = u_overlap = 0
    for i, ft in enumerate(sorted(glob.glob(os.path.join(d0, "target", "*.json")))):
        fd = ft.replace(os.sep + "target" + os.sep, os.sep + "decoy" + os.sep)
        if not os.path.exists(fd):
            continue
        # re-apply the current heuristics over the full 30-day window (as tools/review4_*.py)
        t, d = (narrow(json.load(open(f, encoding="utf-8")), 720) for f in (ft, fd))
        n += 1
        u_overlap += profile_universe_member(t)
        lt, dt = leads(t)
        ld, dd = leads(d)
        decomp["target"] += dt
        decomp["decoy"] += dd
        (wt, _), (wd, rd) = exposure(t), exposure(d)
        exp_t += wt
        exp_d += wd
        rec_d += rd
        for k in KINDS:
            per_kind[k]["t"] += len(lt[k])
            per_kind[k]["d"] += len(ld[k])
            per_kind[k]["wal_t"] += bool(lt[k])
            per_kind[k]["wal_d"] += bool(ld[k])
        rows.append((i, 0, {k: len(lt[k]) for k in KINDS}, wt))
        rows.append((i, 1, {k: len(ld[k]) for k in KINDS}, wd))
    res = {
        "sample": sub,
        "depositors": n,
        "withdrawals_target": exp_t,
        "withdrawals_decoy": exp_d,
        "recipients_decoy": rec_d,
        "profile_universe_overlap": u_overlap,
        "decomposition": {k: dict(v) for k, v in decomp.items()},
        "kinds": {},
    }
    for k in KINDS:
        p = per_kind[k]
        r_plain = (p["d"] / exp_d) / (p["t"] / exp_t) if p["t"] and exp_d else None
        res["kinds"][k] = {
            "target_leads": p["t"],
            "decoy_leads": p["d"],
            "wallets_with_target_lead": p["wal_t"],
            "wallets_with_decoy_lead": p["wal_d"],
            "decoy_rate_per_wallet": p["wal_d"] / n if n else None,
            "decoy_rate_per_wallet_upper95": cp_upper(p["wal_d"], n) if n else None,
            "decoy_rate_per_candidate": p["d"] / rec_d if rec_d else None,
            "R_plain": r_plain,
            "R_cluster_poisson": cluster_rr([(a, b, y[k], e) for a, b, y, e in rows]),
        }
    return res


def fmt(x, nd=3):
    return "–" if x is None else f"{x:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--out", default="article")
    a = ap.parse_args()
    out = os.path.join(BASE, a.out)
    os.makedirs(out, exist_ok=True)
    allres = [analyse(s) for s in a.dirs]
    json.dump(allres, open(os.path.join(out, "decoy_rates.json"), "w", encoding="utf-8"), indent=1)
    md = [
        "# Decoy rates (current rule)",
        "",
        "Per wallet: share of depositors with ≥ 1 decoy lead (Clopper–Pearson 95 % upper bound). Per candidate: "
        "decoy leads / distinct recipients examined in decoy windows. R: plain ratio of lead rates per withdrawal, "
        "and Poisson GLM with log-exposure offset and depositor-clustered SE.",
        "",
    ]
    for r in allres:
        md += [
            f"## {r['sample']} — {r['depositors']} depositors (in profile universe U: {r['profile_universe_overlap']})",
            "",
            f"Decomposition of moderate/strong leads: target {r['decomposition']['target']}, decoy {r['decomposition']['decoy']}",
            "",
            "| kind | target / decoy leads | wallets with decoy lead | per-wallet rate (upper 95 %) | per-candidate rate | R plain | R cluster (95 %) |",
            "|---|---|---|---|---|---|---|",
        ]
        for k, v in r["kinds"].items():
            c = v["R_cluster_poisson"]
            cs = "–" if not c or "rr" not in c else f"{c['rr']:.3f} ({c['lo']:.3f}–{c['hi']:.3f})"
            md.append(
                f"| {k} | {v['target_leads']} / {v['decoy_leads']} | {v['wallets_with_decoy_lead']} | "
                f"{fmt(v['decoy_rate_per_wallet'])} ({fmt(v['decoy_rate_per_wallet_upper95'])}) | "
                f"{fmt(v['decoy_rate_per_candidate'], 6)} | {fmt(v['R_plain'])} | {cs} |"
            )
        md.append("")
    open(os.path.join(out, "DECOY_RATES.md"), "w", encoding="utf-8").write("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
