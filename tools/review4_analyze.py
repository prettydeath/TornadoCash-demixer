#!/usr/bin/env python3
"""review4: strong class (C1), signal co-occurrence (C2) and the summary table (3), offline.

Reads review4/compact.json (tools/review4_compute.py), review4/ens_strong.json,
review4/universe_combos.json and the cached placebo summary jsons; writes
review4/results.json (RESULTS.md is rendered by tools/review4_report.py).
"""

from __future__ import annotations

import itertools
import json
import math
import os
import random
import sys
from collections import Counter

from scipy.stats import beta, chi2, fisher_exact

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from review4_common import OUT, R4, boot_ratio  # noqa: E402

compact = json.load(open(os.path.join(R4, "compact.json"), encoding="utf-8"))
ens = json.load(open(os.path.join(R4, "ens_strong.json"), encoding="utf-8"))
ucomb = json.load(open(os.path.join(R4, "universe_combos.json"), encoding="utf-8"))
rng = random.Random(17)


def J(*p):
    return json.load(open(os.path.join(OUT, *p), encoding="utf-8"))


def variant(rec, h, basis):
    if rec["set"] == "old152" and basis == "inj":
        return rec["inj"][str(h)]
    return rec[str(h)]


def metric(v, kind, key):
    """Count of a metric in one run tally: a band, 'strong+moderate' or 'sig:<signal>'."""
    t = v[kind]
    if key == "strong+moderate":
        return t["bands"].get("strong", 0) + t["bands"].get("moderate", 0)
    if key.startswith("sig:"):
        return t["signals_in_candidates"].get(key[4:], 0)
    return t["bands"].get(key, 0)


def cp_ratio_ci(t, te, d, de):
    """Exact (Clopper-Pearson) interval for R given the counts, treating them as Poisson:
    conditional on t+d, d ~ Binomial(t+d, p) with p/(1-p) = R*(De/Te)."""
    n = t + d
    if n == 0:
        return None
    lo = 0.0 if d == 0 else beta.ppf(0.025, d, n - d + 1)
    hi = 1.0 if d == n else beta.ppf(0.975, d + 1, n - d)

    def f(p):
        return None if p >= 1 else round((p / (1 - p)) * (te / de), 3)

    return [f(lo), f(hi)]


def row(recs, basis, h, key):
    per = []
    for r in recs:
        v = variant(r, h, basis)
        per.append(
            (
                metric(v, "target", key),
                v["target"]["exposure"],
                metric(v, "decoy", key),
                v["decoy"]["exposure"],
            )
        )
    per = [p for p in per if p[1] and p[3]]  # as the tools: both windows need withdrawals
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
    }
    if t:
        v, ci = boot_ratio(per, rng)
        out["R"] = None if v is None else round(v, 3)
        out["R_boot95"] = ci
        out["R_exact95"] = cp_ratio_ci(t, te, d, de)
    else:
        out["R"] = None
    out["flag"] = "too few (<5 on a side)" if min(t, d) < 5 else ""
    # one-sided exact 95 % upper bound of the Poisson mean of the decoy count, given d observed
    out["decoy_poisson_upper95_count"] = round(chi2.ppf(0.95, 2 * (d + 1)) / 2, 2)
    return out


results = {}
sets302 = list(compact)
# ---------------------------------------------------------------- C1
c1 = {}
for basis, label in (
    ("asis", "as_cached"),
    ("inj", "shared_injected"),
):
    c1[label] = {}
    for h in (72, 720):
        blk = {k: row(sets302, basis, h, k) for k in ("strong", "moderate", "strong+moderate")}
        for sname in ("old152", "post150"):
            sub = [r for r in compact if r["set"] == sname]
            blk[f"strong [{sname}]"] = row(sub, basis, h, "strong")
            blk[f"strong+moderate [{sname}]"] = row(sub, basis, h, "strong+moderate")
        c1[label][f"{h}h"] = blk
    combos = {}
    for h in (72, 720):
        for kind in ("target", "decoy"):
            cnt = Counter()
            rows_ = []
            for r in compact:
                v = variant(r, h, basis)
                cnt.update(v[kind]["strong_combos"])
                for pk, addr, sig in v[kind]["strong_rows"]:
                    rows_.append([r["set"], r["wallet"], pk, addr, sig])
            combos[f"{h}h {kind}"] = {"combos": dict(cnt), "rows": rows_}
    c1[label]["strong_source_combinations"] = combos
results["C1b"] = c1
results["C1a"] = {
    "dar_universe_json_summary": J("dar_universe.json")["summary"],
    "universe_combos": ucomb,
    "direct_link_plus_shared": "not computable offline: the 1,000-universe has no counterparty history (explorer data)",
    "poisson_upper95_for_zero_events": round(chi2.ppf(0.95, 2) / 2, 3),
}
results["C1c"] = ens

# ---------------------------------------------------------------- post-2022 (210) = 150 + the 60 old depositors whose first deposit is on/after the sanctions
SPLIT = 1659916800  # 2022-08-08 UTC
late_old = set()
for r in compact:
    if r["set"] == "old152":
        d = J("target", r["wallet"] + ".json")
        if min(x["ts"] for x in d["deposits"]) >= SPLIT:
            late_old.add(r["wallet"])
sub210 = [r for r in compact if r["set"] == "post150" or r["wallet"] in late_old]
results["post2022_210"] = {
    "n": len(sub210),
    "old_after_sanctions": len(late_old),
    **{
        f"{h}h": {
            k: row(sub210, "asis", h, k)
            for k in ("strong+moderate", "sig:linked", "sig:linked_late")
        }
        for h in (72, 720)
    },
}

# ---------------------------------------------------------------- C2
SIGS = [
    "count_match",
    "self_relayed",
    "gas_price",
    "linked",
    "linked_late",
    "linked_sender",
    "shared_deposit",
    "early_profile",
]


def cooc(counter):
    rows = [(set(k.split(",")) - {""}, n) for k, n in counter.items()]
    n = sum(c for _s, c in rows)
    fire = {s: sum(c for st, c in rows if s in st) for s in SIGS}
    pairs = []
    for a, b in itertools.combinations(SIGS, 2):
        pop, note = rows, "all rows"
        if "gas_price" in (a, b):  # the gas gate applies only to self-relayed exits
            pop, note = (
                [(st, c) for st, c in rows if "self_relayed" in st],
                "self-relayed rows only",
            )
        N = sum(c for _s, c in pop)
        na = sum(c for st, c in pop if a in st)
        nb = sum(c for st, c in pop if b in st)
        ab = sum(c for st, c in pop if a in st and b in st)
        exp = na * nb / N if N else 0
        table = [[ab, na - ab], [nb - ab, N - na - nb + ab]]
        den = math.sqrt(na * (N - na) * nb * (N - nb)) if N else 0
        phi = (ab * (N - na - nb + ab) - (na - ab) * (nb - ab)) / den if den else None
        p = fisher_exact(table)[1] if N and na and nb else None
        struct = ""
        if {a, b} == {"linked", "linked_late"}:
            struct = "mutually exclusive by construction"
        if {a, b} == {"gas_price", "self_relayed"}:
            struct = "gas_price requires self_relayed"
        pairs.append(
            {
                "a": a,
                "b": b,
                "population": note,
                "n_population": N,
                "n_a": na,
                "n_b": nb,
                "observed": ab,
                "expected": round(exp, 4),
                "phi": None if phi is None else round(phi, 5),
                "fisher_p": p,
                "underpowered": exp < 1,
                "structural": struct,
            }
        )
    return {"n_rows": n, "fires": fire, "pairs": pairs}


c2 = {}
for h in (720, 72):
    for kind in ("target", "decoy", "pooled"):
        total = Counter()
        for r in compact:
            v = variant(r, h, "inj")
            for k in ("target", "decoy") if kind == "pooled" else (kind,):
                total.update(v[k]["row_signal_sets"])
        c2[f"{h}h {kind}"] = cooc(total)
results["C2"] = c2

# ---------------------------------------------------------------- 3 summary
S = {}
KEYS = (
    ("early direct link (linked)", "sig:linked"),
    ("late direct link (linked_late, context)", "sig:linked_late"),
    ("linked_sender", "sig:linked_sender"),
    ("shared_deposit", "sig:shared_deposit"),
    ("early_profile", "sig:early_profile"),
    ("count match (count_match)", "sig:count_match"),
    ("self_relayed", "sig:self_relayed"),
    ("gas price", "sig:gas_price"),
    ("class moderate", "moderate"),
    ("class strong", "strong"),
    ("strong+moderate", "strong+moderate"),
)
for h in (72, 720):
    S[f"{h}h"] = {
        label: {
            "as_cached": row(sets302, "asis", h, key),
            "shared_injected": row(sets302, "inj", h, key),
        }
        for label, key in KEYS
    }
results["summary_302"] = S
du, dn = J("dar_universe.json")["summary"], J("dar_universe_nolabels.json")["summary"]


def cell(d):
    return next(
        c for c in d["cells"] if c["min_notes"] == 10 and c["min_pools"] == 2 and c["hours"] == 72
    )


results["external"] = {
    "shared_deposit_labelled_1000": {
        "source": "dar_universe.json summary.evidence",
        **du["evidence"],
        "depositors": du["depositors"],
    },
    "shared_deposit_nolabels_1000": {
        "source": "dar_universe_nolabels.json summary.loose",
        **dn["loose"],
        "depositors": dn["depositors"],
    },
    "shared_deposit_loose_1000": {"source": "dar_universe.json summary.loose", **du["loose"]},
    "early_profile_4194": {
        "source": "profile.json cell notes>=10 pools>=2 72h",
        **cell(J("profile.json")),
    },
    "early_profile_before_sanctions": {
        "source": "profile_before.json same cell",
        **cell(J("profile_before.json")),
    },
    "early_profile_after_sanctions": {
        "source": "profile_after.json same cell",
        **cell(J("profile_after.json")),
    },
    "combined_v217": {
        "source": "post2022/combined_v217.json",
        **J("post2022", "combined_v217.json"),
    },
}
json.dump(
    results, open(os.path.join(R4, "results.json"), "w", encoding="utf-8"), indent=1, default=str
)
print("saved")
