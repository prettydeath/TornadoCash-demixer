#!/usr/bin/env python3
"""Supplementary Table S1: every exploratory cell examined before the rule was frozen.

Flattens the cached exploratory outputs under .cache/labels/placebo (window sweep, discrimination
bins, periods, early-profile grids, shared-deposit label variants, published heuristics) into one
table: analysis, cell, depositors, target leads, decoy leads, R and its 95 % interval as stored.
Cells that entered the rule are marked. The values are those of the rule version in force when
each analysis was run (see docs/EVALUATION.md); they document the search, not current estimates.

Usage: python tools/supplementary_s1.py [--out PATH.md]
"""

from __future__ import annotations

import argparse
import json
import os

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "labels", "placebo"
)
SELECTED = {
    ("window sweep", "linked address", "72h"),
    ("discrimination bins", "gated in (disc >= 0.5)", "720h"),
    ("early profile", "notes>=10, pools>=2, 72h"),
}


def load(name):
    p = os.path.join(BASE, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def ci(c):
    return "" if not c else f"{c[0]:.3f}–{c[1]:.3f}"


def exact_ci(t, d, nt, nd):
    """Clopper-Pearson interval of d given t + d, mapped to R (the paper's policy for < 5 leads)."""
    from scipy.stats import beta

    n = t + d
    lo = 0.0 if d == 0 else beta.ppf(0.025, d, n - d + 1)
    hi = 1.0 if d == n else beta.ppf(0.975, d + 1, n - d)
    k = nt / nd

    def to_r(p):
        return float("inf") if p >= 1 else p / (1 - p) * k

    return [to_r(lo), to_r(hi)]


def policy(t, d, stored, nt=None, nd=None):
    """Bootstrap as stored when both sides have >= 5 leads, otherwise exact (needs exposures)."""
    if t is None or d is None or t + d == 0:
        return "", ""
    if min(t, d) >= 5:
        return ci(stored), ""
    if nt and nd:
        return ci(exact_ci(t, d, nt, nd)), "ᵉ"
    return "", "ⁿ"


def r(x):
    return "" if x is None else f"{x:.3f}"


def rows():
    out = []
    w = load("windows.json")
    if w:
        for win, fams in w["windows"].items():
            for fam, v in fams.items():
                if isinstance(v, dict) and "target" in v:
                    out.append(
                        (
                            "window sweep",
                            f"{fam}, {win}",
                            w["depositors"],
                            v["target"],
                            v["decoy"],
                            v.get("fdr"),
                            v.get("fdr_95ci"),
                            ("window sweep", fam, win) in SELECTED,
                            v.get("target_withdrawals"),
                            v.get("decoy_withdrawals"),
                        )
                    )
    d = load("disc.json")
    if d:
        for win, bins in d["windows"].items():
            for b, v in bins.items():
                out.append(
                    (
                        "discrimination bins",
                        f"{b}, {win}",
                        d["depositors"],
                        v["target"],
                        v["decoy"],
                        v.get("fdr"),
                        v.get("fdr_95ci"),
                        ("discrimination bins", b, win) in SELECTED,
                    )
                )
    pe = load("periods.json")
    if pe:
        for per, v in pe["periods"].items():
            for k, x in v.items():
                if isinstance(x, dict):
                    out.append(
                        (
                            "periods",
                            f"{per}: {k}",
                            v.get("depositors"),
                            x["target"],
                            x["decoy"],
                            x.get("fdr"),
                            x.get("fdr_95ci"),
                            False,
                        )
                    )
    for fname, label in (
        ("profile.json", "early profile (universe)"),
        ("profile_grid.json", "early profile grid"),
        ("profile_before.json", "early profile, before 2022-08-08"),
        ("profile_after.json", "early profile, after 2022-08-08"),
        ("profile_arbitrum.json", "early profile, Arbitrum"),
        ("profile_polygon.json", "early profile, Polygon"),
    ):
        g = load(fname)
        if not g:
            continue
        for c in g.get("cells", []):
            cell = f"notes>={c['min_notes']}, pools>={c['min_pools']}, {c['hours']}h"
            sel = fname == "profile.json" and ("early profile", cell) in SELECTED
            out.append(
                (
                    label,
                    cell,
                    c.get("depositors"),
                    c["target"],
                    c["decoy"],
                    c.get("fdr"),
                    c.get("fdr_95ci"),
                    sel,
                )
            )
    for fname, label in (
        ("dar_universe.json", "S (1,000): labels or activity"),
        ("dar_universe_labels_only.json", "S (1,000): labels only"),
        ("dar_universe_nolabels.json", "S (1,000): no labels (activity only)"),
    ):
        s = load(fname)
        if not s:
            continue
        for var, v in s["summary"].items():
            if isinstance(v, dict) and "target_hits" in v:
                out.append(
                    (
                        "shared deposit address",
                        f"{label}, {var}",
                        s["summary"].get("depositors"),
                        v["target_hits"],
                        v["decoy_hits"],
                        v.get("fdr"),
                        v.get("fdr_95ci"),
                        fname == "dar_universe.json" and var == "evidence",
                        v.get("target_withdrawals"),
                        v.get("decoy_withdrawals"),
                    )
                )
    # Early versus late direct link on P1 + P2 (re-scored, review4 compact.json; source of the 72 h cut).
    out.append(
        (
            "direct link by delay",
            "early (<= 72 h after last deposit), 30 d",
            302,
            27,
            1,
            0.040,
            None,
            True,
            483224,
            446004,
        )
    )
    out.append(
        (
            "direct link by delay",
            "late (> 72 h), 30 d",
            302,
            8,
            13,
            1.76,
            [0.69, 6.4],
            False,
            483224,
            446004,
        )
    )
    for fname, label in (
        ("dar_summary.json", "label or activity"),
        ("dar_summary_labels_only.json", "exchange label"),
    ):
        s = load(fname)
        if s and "summary" in s:
            v = s["summary"]
            out.append(
                (
                    "shared deposit address",
                    label,
                    v.get("depositors"),
                    v.get("target_hits"),
                    v.get("decoy_hits"),
                    v.get("fdr"),
                    v.get("fdr_95ci"),
                    ("shared deposit address", label) in SELECTED,
                )
            )
    t = load(os.path.join("tutela", "summary.json"))
    if t:
        for sample, hs in t.get("sets", {}).items():
            for h, v in hs.items():
                if isinstance(v, dict) and "target_leads" in v:
                    out.append(
                        (
                            "published heuristics",
                            f"{h}, {sample}",
                            v.get("depositors"),
                            v["target_leads"],
                            v["decoy_leads"],
                            v.get("R"),
                            v.get("R_boot95"),
                            False,
                            v.get("target_withdrawals"),
                            v.get("decoy_withdrawals"),
                        )
                    )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BASE, "article", "S1.md"))
    a = ap.parse_args()
    rs = rows()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    md = [
        "# Supplementary Table S1. Exploratory cells from the window, discrimination, period, profile, "
        "shared-deposit, delay and published-heuristic analyses",
        "",
        "R = (decoy leads / decoy withdrawals) / (target leads / target withdrawals) as stored by each analysis "
        "(named `fdr` in older outputs). ★ = cell that entered the rule. Values are those of the rule version in "
        "force when each analysis was run.",
        "",
        "| Analysis | Cell | Depositors | Target | Decoy | R | 95% interval | |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rs = [tuple(x) + (None,) * (10 - len(x)) for x in rs]
    for an, cell, n, tt, dd, rr, cc, sel, nt, nd in rs:
        iv, mark = policy(tt, dd, cc, nt, nd)
        md.append(
            f"| {an} | {cell} | {n or ''} | {tt} | {dd} | {r(rr)} | {iv}{mark} | {'★' if sel else ''} |"
        )
    md.append("")
    md.append(
        f"Cells: {len(rs)}. Interval policy as in the paper: bootstrap over depositors (as stored) when both "
        "sides have at least five leads; ᵉ exact (Clopper–Pearson, mapped to R) otherwise; ⁿ fewer than five "
        "leads and the analysis did not store withdrawal counts, so no interval is given. Not tabulated here "
        "but documented in docs/EVALUATION.md: the priority-tip rarity rules, the gas-price band variants and "
        "the count-match/strong conditional (k2_conditional.json)."
    )
    open(a.out, "w", encoding="utf-8").write("\n".join(md))
    print(a.out, len(rs), "cells;", sum(bool(x[7]) for x in rs), "selected")


if __name__ == "__main__":
    main()
