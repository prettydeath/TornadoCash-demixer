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
    ("shared deposit address", "exchange label"),
}


def load(name):
    p = os.path.join(BASE, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def ci(c):
    return "" if not c else f"{c[0]:.3f}–{c[1]:.3f}"


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
        for sample, hs in t.items():
            if not isinstance(hs, dict):
                continue
            for h, v in hs.items():
                if isinstance(v, dict) and "target" in v:
                    out.append(
                        (
                            "published heuristics",
                            f"{h}, {sample}",
                            v.get("depositors"),
                            v["target"],
                            v["decoy"],
                            v.get("R"),
                            v.get("boot_ci") or v.get("exact_ci"),
                            False,
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
        "# Supplementary Table S1. Exploratory cells examined before the rule was frozen",
        "",
        "R = (decoy leads / decoy withdrawals) / (target leads / target withdrawals) as stored by each analysis "
        "(named `fdr` in older outputs). ★ = cell that entered the rule. Values are those of the rule version in "
        "force when each analysis was run.",
        "",
        "| Analysis | Cell | Depositors | Target | Decoy | R | 95% interval | |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for an, cell, n, tt, dd, rr, cc, sel in rs:
        md.append(
            f"| {an} | {cell} | {n or ''} | {tt} | {dd} | {r(rr)} | {ci(cc)} | {'★' if sel else ''} |"
        )
    md.append("")
    md.append(f"Cells: {len(rs)}.")
    open(a.out, "w", encoding="utf-8").write("\n".join(md))
    print(a.out, len(rs), "cells;", sum(x[-1] for x in rs), "selected")


if __name__ == "__main__":
    main()
