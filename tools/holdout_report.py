#!/usr/bin/env python3
"""Pre-registered hold-out metrics from the cached target/decoy runs of a sample directory.

Offline: every run is re-scored with the CURRENT heuristics (tools/placebo_windows.narrow,
no explorer calls) for the 30-day (720 h) and 72-hour exit windows. Per signal and per class
the report gives the target and decoy lead counts, the withdrawals examined and the ratio
R = (Lc / Nc) / (Lk / Nk) (c = decoy, k = target), the false-lead share, with a percentile
bootstrap 95 % CI over depositors (2000 resamples, fixed seed) and an exact
(Clopper-Pearson-based) 95 % CI. The exact interval is the one to read when a side has
fewer than 5 leads.

Works for any network directory (ethereum, polygon, arbitrum): it only reads
<dir>/target/*.json and <dir>/decoy/*.json.

Writes <dir>/holdout_summary.json and <dir>/HOLDOUT.md.

Usage
-----
    python tools/holdout_report.py holdout_eth
    python tools/holdout_report.py polygon --boot 2000 --seed 20260801
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter

from scipy.stats import beta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from placebo_windows import OUT, narrow  # noqa: E402

from tornado_demix.deposit_addresses import shared_deposit_hits  # noqa: E402
from tornado_demix.heuristics import LEAD_SOURCE, ranked_candidates  # noqa: E402

WINDOWS = {"30d": 720, "72h": 72}
SEED = 20260801
MIN_SIDE = 5  # fewer leads than this on a side: the exact interval is the one to use

SIGNALS = (
    ("early direct link (linked, <=72h)", "sig:linked"),
    ("late direct link (linked_late, context)", "sig:linked_late"),
    ("linked_sender", "sig:linked_sender"),
    ("shared_deposit (labelled)", "sig:shared_deposit"),
    ("shared deposit, unlabelled (context)", "ctx:shared_deposit"),
    ("early_profile", "sig:early_profile"),
    ("count_match", "sig:count_match"),
    ("gas_price", "sig:gas_price"),
)
CLASSES = (
    ("class moderate", "moderate"),
    ("class strong", "strong"),
    ("class strong+moderate", "strong+moderate"),
)


def tally(data):
    """Compact per-run counts from an already narrowed run."""
    bands, sig, combos = Counter(), Counter(), Counter()
    for r in ranked_candidates(data):
        bands[r["band"]] += 1
        for s in r["signals"]:
            sig[s] += 1
        if r["band"] == "strong":
            src = sorted({LEAD_SOURCE[s] for s in r["signals"] if s in LEAD_SOURCE})
            combos[" + ".join(src)] += 1
    exposure = sum(sum(res.get("counts", {}).values()) for res in data.get("denoms", {}).values())
    rows = data.get("deposit_addresses")
    ctx = None
    if rows is not None:
        recips = {a for res in data.get("denoms", {}).values() for a in res.get("counts", {})}
        ctx = len(
            shared_deposit_hits(
                [d for d in rows if not d.get("evidence", True)],
                recips,
                data.get("wallet", ""),
                evidence_only=False,
            )
        )
    return {
        "exposure": exposure,
        "bands": dict(bands),
        "signals": dict(sig),
        "combos": dict(combos),
        "ctx_shared": ctx,
    }


def metric(t, key):
    if key == "strong+moderate":
        return t["bands"].get("strong", 0) + t["bands"].get("moderate", 0)
    if key.startswith("sig:"):
        return t["signals"].get(key[4:], 0)
    if key == "ctx:shared_deposit":
        return t["ctx_shared"] or 0
    return t["bands"].get(key, 0)


def exact_ratio_ci(t, te, d, de, alpha=0.05):
    """Clopper-Pearson-based interval for R = (d/de)/(t/te): given t+d, d is Binomial(t+d, p)
    with p/(1-p) = R * de/te; the exact binomial limits for p are mapped back to R."""
    n = t + d
    if n == 0 or not te or not de:
        return None
    lo = 0.0 if d == 0 else beta.ppf(alpha / 2, d, n - d + 1)
    hi = 1.0 if d == n else beta.ppf(1 - alpha / 2, d + 1, n - d)

    def f(p):
        return None if p >= 1 else float((p / (1 - p)) * (te / de))

    return [f(lo), f(hi)]


def _ratio(rs):
    t = sum(r[0] for r in rs)
    te = sum(r[1] for r in rs)
    d = sum(r[2] for r in rs)
    de = sum(r[3] for r in rs)
    return (d / de) / (t / te) if t and te and de else None


def boot_ci(per, rng, n=2000):
    bs = sorted(
        x
        for x in (_ratio([per[rng.randrange(len(per))] for _ in per]) for _ in range(n))
        if x is not None
    )
    return [bs[int(0.025 * len(bs))], bs[max(int(0.975 * len(bs)) - 1, 0)]] if bs else None


def _r(x):
    return None if x is None else round(float(x), 3)


def ratio_stats(per, rng, boot=2000):
    """per: [(target_leads, target_withdrawals, decoy_leads, decoy_withdrawals)] per depositor.
    Depositors with no withdrawals on either side are dropped, as in the placebo tools."""
    per = [p for p in per if p[1] and p[3]]
    t = sum(p[0] for p in per)
    d = sum(p[2] for p in per)
    te = sum(p[1] for p in per)
    de = sum(p[3] for p in per)
    out = {
        "depositors": len(per),
        "target": t,
        "decoy": d,
        "target_withdrawals": te,
        "decoy_withdrawals": de,
        "R": _r(_ratio(per)),
        "R_boot95": None,
        "R_exact95": None,
        "ci_used": None,
        "ci_type": None,
    }
    if not per or not t:
        out["note"] = "no target leads: R undefined"
        return out
    boot_c = boot_ci(per, rng, boot)
    exact = exact_ratio_ci(t, te, d, de)
    out["R_boot95"] = None if boot_c is None else [_r(x) for x in boot_c]
    out["R_exact95"] = None if exact is None else [_r(x) for x in exact]
    few = min(t, d) < MIN_SIDE
    out["ci_type"] = "exact" if few else "bootstrap"
    out["ci_used"] = out["R_exact95"] if few else out["R_boot95"]
    out["few_leads"] = few
    return out


def analyze(pairs, boot=2000, seed=SEED):
    """pairs: [(target_tally, decoy_tally)] for one window -> metric table."""
    rng = random.Random(seed)
    out = {}
    have_ctx = any(p[0]["ctx_shared"] is not None or p[1]["ctx_shared"] is not None for p in pairs)
    for label, key in SIGNALS + CLASSES:
        if key == "ctx:shared_deposit" and not have_ctx:
            out[label] = {"note": "not available: runs carry no deposit_addresses"}
            continue
        per = [(metric(t, key), t["exposure"], metric(d, key), d["exposure"]) for t, d in pairs]
        out[label] = ratio_stats(per, rng, boot)
    combos = {"target": Counter(), "decoy": Counter()}
    for t, d in pairs:
        if t["exposure"] and d["exposure"]:
            combos["target"].update(t["combos"])
            combos["decoy"].update(d["combos"])
    out["strong_source_combinations"] = {k: dict(v) for k, v in combos.items()}
    return out


def resolve_dir(arg):
    if os.path.isabs(arg) and os.path.isdir(arg):
        return arg
    for cand in (os.path.join(OUT, arg), arg):
        if os.path.isdir(os.path.join(cand, "target")):
            return os.path.abspath(cand)
    raise SystemExit(
        f"no target/ and decoy/ runs under {arg} (also tried {os.path.join(OUT, arg)})"
    )


def load_runs(base):
    names = sorted(
        set(os.listdir(os.path.join(base, "target"))) & set(os.listdir(os.path.join(base, "decoy")))
    )
    runs = []
    for n in (n for n in names if n.endswith(".json")):
        pair = []
        for k in ("target", "decoy"):
            with open(os.path.join(base, k, n), encoding="utf-8") as fh:
                pair.append(json.load(fh))
        runs.append(tuple(pair))
    return runs


def build(runs, boot=2000, seed=SEED, windows=None):
    windows = windows or WINDOWS
    out = {"depositors": len(runs), "boot": boot, "seed": seed, "windows": {}}
    for wname, hours in windows.items():
        pairs = [(tally(narrow(t, hours)), tally(narrow(d, hours))) for t, d in runs]
        res = analyze(pairs, boot, seed)
        res["hours"] = hours
        res["depositors_with_withdrawals"] = sum(
            1 for t, d in pairs if t["exposure"] and d["exposure"]
        )
        out["windows"][wname] = res
    return out


def _fmt(x):
    return "n/a" if x is None else f"{x:g}"


def render(summary, title="Hold-out placebo"):
    lines = [f"# {title}", ""]
    lines.append(
        f"Depositors: {summary['depositors']}. R = (decoy leads / decoy withdrawals) / "
        f"(target leads / target withdrawals); bootstrap {summary['boot']} resamples over "
        f"depositors, seed {summary['seed']}; the exact CI is used instead when a side has "
        f"fewer than {MIN_SIDE} leads."
    )
    for wname, res in summary["windows"].items():
        lines += [
            "",
            f"## {wname} ({res['hours']} h), depositors with withdrawals on both sides: "
            f"{res['depositors_with_withdrawals']}",
            "",
            "| metric | target | decoy | N target | N decoy | R | 95% CI | CI type | bootstrap | exact |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for label, _k in SIGNALS + CLASSES:
            r = res[label]
            if "target" not in r:
                lines.append(f"| {label} | {r['note']} |  |  |  |  |  |  |  |  |")
                continue
            ci = r["ci_used"]
            span = "n/a" if ci is None else f"{_fmt(ci[0])} - {_fmt(ci[1])}"
            lines.append(
                f"| {label} | {r['target']} | {r['decoy']} | {r['target_withdrawals']} | "
                f"{r['decoy_withdrawals']} | {_fmt(r['R'])} | {span} | {r['ci_type'] or 'n/a'} | "
                f"{r['R_boot95']} | {r['R_exact95']} |"
            )
        lines += ["", "Strong source combinations:", ""]
        combos = res["strong_source_combinations"]
        names = sorted(set(combos["target"]) | set(combos["decoy"]))
        if not names:
            lines.append("none")
        else:
            lines += ["| combination | target | decoy |", "|---|---|---|"]
            lines += [
                f"| {n} | {combos['target'].get(n, 0)} | {combos['decoy'].get(n, 0)} |"
                for n in names
            ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", help="sample dir (absolute, or relative to .cache/labels/placebo)")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    base = resolve_dir(args.dir)
    runs = load_runs(base)
    if not runs:
        raise SystemExit(f"no wallets with both target and decoy runs in {base}")
    summary = build(runs, args.boot, args.seed)
    samp = os.path.join(base, "summary.json")
    if os.path.exists(samp):
        with open(samp, encoding="utf-8") as fh:
            summary["sampling"] = json.load(fh).get("sampling")
    text = render(summary, f"Hold-out placebo: {os.path.basename(base)}")
    with open(os.path.join(base, "holdout_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    with open(os.path.join(base, "HOLDOUT.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
