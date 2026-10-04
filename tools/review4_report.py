#!/usr/bin/env python3
"""Render review4/results.json into review4/RESULTS.md."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from review4_common import R4  # noqa: E402

r = json.load(open(os.path.join(R4, "results.json"), encoding="utf-8"))
L = []


def w(s=""):
    L.append(s)


def fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def ci(v):
    """Bootstrap CI shown only when the target count allows it; exact CI always."""
    boot = (
        "n/a"
        if (v["target"] < 5 or v.get("R_boot95") is None)
        else f"{v['R_boot95'][0]:.3f}-{v['R_boot95'][1]:.3f}"
    )
    ex = v.get("R_exact95")
    exs = "n/a" if not ex else f"{ex[0]:.3f}-{'inf' if ex[1] is None else format(ex[1], '.3f')}"
    return boot, exs


def line(label, v, extra=""):
    b, e = ci(v)
    flag = " (too few)" if v["flag"] else ""
    return f"| {label} | {v['target']} / {v['decoy']}{flag} | {v['target_withdrawals']:,} / {v['decoy_withdrawals']:,} | {fmt(v.get('R'))} | {b} | {e} |{extra}"


w("# review4: strong class, signal co-occurrence, summary of the v2.17 rule")
w()
w(
    "All numbers computed offline from cached data (no network). R = (L_decoy/N_decoy)/(L_target/N_target), N = withdrawals examined in that window type."
)
w(
    "`boot` = percentile bootstrap over depositors (2,000 resamples, as in tools/placebo_*.py); `exact` = Clopper-Pearson interval for R treating the two counts as Poisson (conditional binomial) - use it when counts are < 5, where the bootstrap is degenerate."
)
w(
    "Scripts: tools/review4_compute.py -> compact.json; review4_ens.py; review4_universe.py; review4_analyze.py -> results.json; review4_report.py -> this file."
)
w()
w("## 0. Two bases for the 302 depositors")
w(
    "The 152 first-sample runs predate the shared-deposit signal; the thesis numbers (31/1, 35/3) are computed on them as cached. "
    "`as cached` reproduces those numbers exactly. `shared injected` adds the labelled (evidence-only) shared-deposit hits "
    "from the cached deposit-address lookups `.cache/labels/placebo/dar/<wallet>.json` (as demix.py would) to the 152 runs, i.e. the full v2.17 signal set. "
    "The 150 post-2022 runs already contain shared_deposit."
)
w()
w("## C1. Strong class")
w()
w("### C1a. 1,000-depositor shared-deposit universe (dar_universe.json)")
cu = r["C1a"]["universe_combos"]
du = r["C1a"]["dar_universe_json_summary"]
w(
    f"- Labelled shared deposit alone (dar_universe.json summary.evidence): target {du['evidence']['target_hits']} / decoy {du['evidence']['decoy_hits']}, R {du['evidence']['fdr']} (boot {du['evidence']['fdr_95ci'][0]}-{du['evidence']['fdr_95ci'][1]}), N {du['evidence']['target_withdrawals']:,} / {du['evidence']['decoy_withdrawals']:,}."
)
w(
    "- **Early direct link + labelled shared deposit: NOT computable on this universe.** The universe holds only pool deposits/withdrawals; a direct link needs each depositor's counterparties (explorer transaction history), which is not cached for these 1,000 wallets. Only 2 of the 1,000 wallets are among the 150 full runs. The only full runs that carry both sources are the 302 (see C1b)."
)
w(
    f"- Other 2-source combination computable from the universe: **early multi-pool profile + labelled shared deposit** (profile rule 10 notes / 2 pools / 72 h, hits recomputed from universe.json; decoy shift as in the dar run). "
    f"Profile-eligible depositors in the 1,000: {cu['profile_eligible_depositors']}; profile hits target {cu['profile_target_hits']} / decoy {cu['profile_decoy_hits']}; "
    f"hits that are also labelled shared-deposit hits: **target {cu['profile_and_shared_target']} / decoy {cu['profile_and_shared_decoy']}**. 0 observed, so no R can be estimated (too few). "
    f"Exact 95% one-sided upper bound for the Poisson mean given 0 observed: {r['C1a']['poisson_upper95_for_zero_events']} events (so in this sample strong via this combination is < ~3 in either window type)."
)
w(
    "- Shared deposit is rare enough that a decoy strong lead needs two independent rare events: the 4 labelled decoy shared-deposit hits (of ~1.98M decoy withdrawals) would each additionally need an early direct link."
)
w()
w("### C1b. 302 depositors (152 old + 150 post-2022), v2.17")
for basis, title in (
    ("as_cached", "as cached (thesis basis)"),
    ("shared_injected", "shared deposit injected into the 152"),
):
    w()
    w(f"**{title}**")
    w()
    w("| window / class | real / decoy leads | N real / N decoy | R | boot 95% CI | exact 95% CI |")
    w("|---|---|---|---|---|---|")
    for h in ("72h", "720h"):
        blk = r["C1b"][basis][h]
        for k in ("strong", "moderate", "strong+moderate"):
            w(line(f"{h} {k}", blk[k]))
    w()
    w("Strong-candidate source combinations (all strong rows, both windows identical):")
    for key, v in r["C1b"][basis]["strong_source_combinations"].items():
        if key.startswith("720h"):
            w(f"- {key}: {v['combos'] or 'none'}")
            for x in v["rows"]:
                w(f"  - {x[0]} depositor {x[1][:10]}.. {x[2]} {x[3][:10]}.. signals {x[4]}")
w()
p = r["post2022_210"]
w(
    "Sub-sample cross-check, post-2022 (210) = 150 post-2022 + the 60 old depositors with first deposit on/after 2022-08-08 (as cached):"
)
w()
w("| | real / decoy | R | boot 95% CI |")
w("|---|---|---|---|")
for h in ("72h", "720h"):
    for k, lab in (
        ("strong+moderate", "strong+moderate"),
        ("sig:linked", "early direct link"),
        ("sig:linked_late", "late direct link"),
    ):
        v = p[h][k]
        w(
            f"| {h} {lab} | {v['target']} / {v['decoy']} | {fmt(v.get('R'))} | {v['R_boot95'][0] if v.get('R_boot95') else 'n/a'}-{v['R_boot95'][1] if v.get('R_boot95') else 'n/a'} |"
        )
w()
w(
    "### C1c. ENS evaluation under v2.17 (tools/review4_ens.py; 27 cached demix runs re-scored with narrow(., 720 h))"
)
e = r["C1c"]
w(f"- Bands over all candidates: {e['bands']} (28 strong+moderate, matching docs/EVALUATION.md).")
w(
    f"- Strong = {e['bands'].get('strong', 0)}; source combination: {e['strong_combos']}. Both are labelled pairs (signals linked + shared_deposit): "
    + "; ".join(
        f"{x['depositor'][:10]}.. {x['pool']} {x['address'][:10]}.. labelled_pair={x['labelled_pair']}"
        for x in e["strong"]
    )
    + "."
)
w()
w("## C2. Co-occurrence of the current signal set")
w(
    "Unit = recipient-pool row (every recipient of every pool in the window, 302 depositors, shared injected). Window = 30 days (720 h; all signals can fire); the 72 h window is in results.json. "
    "Expected = n_a*n_b/n. phi from the 2x2 table; Fisher exact two-sided (scipy 1.18.1). Pairs with gas_price use the population of self-relayed rows (the gas gate applies only to self-relayed exits). "
    "Underpowered = expected < 1. Pairs with 0 observed and expected < 1 are omitted from the tables (listed as a count)."
)
for key in ("720h target", "720h decoy", "720h pooled", "72h target", "72h decoy"):
    c = r["C2"][key]
    w()
    w(f"### {key}: n rows {c['n_rows']:,}")
    w("Fires: " + ", ".join(f"{k} {v}" for k, v in c["fires"].items()))
    w()
    w("| pair | observed | expected | phi | Fisher p | note |")
    w("|---|---|---|---|---|---|")
    omitted = 0
    for q in c["pairs"]:
        if q["observed"] == 0 and q["expected"] < 1:
            omitted += 1
            continue
        note = []
        if q["underpowered"]:
            note.append("underpowered (exp<1)")
        if q["structural"]:
            note.append(q["structural"])
        if q["population"] != "all rows":
            note.append(q["population"])
        phi = "n/a" if q["phi"] is None else f"{q['phi']:.4f}"
        pv = "n/a" if q["fisher_p"] is None else f"{q['fisher_p']:.2g}"
        w(
            f"| {q['a']} x {q['b']} | {q['observed']} | {q['expected']} | {phi} | {pv} | {'; '.join(note)} |"
        )
    w(f"({omitted} of 28 pairs: 0 observed, expected < 1 - underpowered, not informative)")
w()
w(
    "Reading: the only adequately powered pair (expected >= 1) in every table is count_match x self_relayed, phi about 0.03-0.05 (p < 1e-9): both read the same withdrawals, so they are one family (METHOD_FAMILY); this is larger than the old-set |phi| <= 0.006. "
    "count_match x linked is borderline (expected 0.85-0.96). Every other pair has expected < 1: the lead-signal pairs (linked x shared_deposit: 2 observed vs 0.0005 expected; linked x linked_sender: 1 vs 0.0007) show the expected positive association by construction (those are the strong candidates) but are underpowered. "
    "Rows are (pool, address), so one address in several pools counts several times; rows are not independent."
)
w()
w("## 3. Summary table for the v2.17 rule")
w(
    "Lead counts = rows (pool, address) among the candidates admitted by ranked_candidates carrying the signal/band (the thesis's L); N = withdrawals examined. Window: 30 d = 720 h."
)
w()
w(
    "| signal / class | sample | window | real / decoy | N real / N decoy | R | boot 95% CI | exact 95% CI | source |"
)
w("|---|---|---|---|---|---|---|---|---|")


def srow(label, sample, win, v, src):
    b, e = ci(v)
    flag = " (too few)" if v["flag"] else ""
    w(
        f"| {label} | {sample} | {win} | {v['target']} / {v['decoy']}{flag} | {v['target_withdrawals']:,} / {v['decoy_withdrawals']:,} | {fmt(v.get('R'))} | {b} | {e} | {src} |"
    )


def jrow(label, sample, win, d, src):
    lo, hi = d["fdr_95ci"]
    t, dd = d.get("target_hits", d.get("target")), d.get("decoy_hits", d.get("decoy"))
    n = (
        f"{d['target_withdrawals']:,} / {d['decoy_withdrawals']:,}"
        if "target_withdrawals" in d
        else "not stored in json"
    )
    w(f"| {label} | {sample} | {win} | {t} / {dd} | {n} | {d['fdr']} | {lo}-{hi} | - | {src} |")


S = r["summary_302"]
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow(
        "early direct link (linked)",
        "302",
        hn,
        S[h]["early direct link (linked)"]["as_cached"],
        "review4 compact.json (re-scored v2.17)",
    )
srow(
    "late direct link (linked_late, context)",
    "302",
    "30 d",
    S["720h"]["late direct link (linked_late, context)"]["as_cached"],
    "review4 compact.json",
)
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow("linked_sender", "302", hn, S[h]["linked_sender"]["as_cached"], "review4 compact.json")
ex = r["external"]
jrow(
    "shared deposit, labelled",
    "1,000 (universe)",
    "30 d",
    ex["shared_deposit_labelled_1000"],
    ex["shared_deposit_labelled_1000"]["source"],
)
jrow(
    "shared deposit, unlabelled/activity-only (context)",
    "1,000 (universe)",
    "30 d",
    ex["shared_deposit_nolabels_1000"],
    ex["shared_deposit_nolabels_1000"]["source"] + " (attribution set ignored)",
)
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow(
        "shared deposit, labelled (injected)",
        "302",
        hn,
        S[h]["shared_deposit"]["shared_injected"],
        "review4 compact.json (shared injected)",
    )
jrow(
    "early profile",
    "4,194 (universe, >=10 notes, >=2 pools)",
    "72 h",
    ex["early_profile_4194"],
    ex["early_profile_4194"]["source"],
)
jrow(
    "early profile, before sanctions",
    f"{ex['early_profile_before_sanctions']['depositors']:,}",
    "72 h",
    ex["early_profile_before_sanctions"],
    ex["early_profile_before_sanctions"]["source"],
)
jrow(
    "early profile, after sanctions",
    f"{ex['early_profile_after_sanctions']['depositors']:,}",
    "72 h",
    ex["early_profile_after_sanctions"],
    ex["early_profile_after_sanctions"]["source"],
)
srow(
    "early profile (in the 302 full runs)",
    "302",
    "30 d",
    S["720h"]["early_profile"]["as_cached"],
    "review4 compact.json",
)
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow(
        "count match (count_match)",
        "302",
        hn,
        S[h]["count match (count_match)"]["as_cached"],
        "review4 compact.json",
    )
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow("gas price", "302", hn, S[h]["gas price"]["as_cached"], "review4 compact.json")
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow(
        "class moderate",
        "302 as cached",
        hn,
        S[h]["class moderate"]["as_cached"],
        "review4 compact.json",
    )
    srow(
        "class strong",
        "302 as cached",
        hn,
        S[h]["class strong"]["as_cached"],
        "review4 compact.json",
    )
    srow(
        "strong+moderate",
        "302 as cached",
        hn,
        S[h]["strong+moderate"]["as_cached"],
        "review4 compact.json; = post2022/combined_v217.json",
    )
    srow(
        "class moderate",
        "302 shared injected",
        hn,
        S[h]["class moderate"]["shared_injected"],
        "review4 compact.json",
    )
    srow(
        "class strong",
        "302 shared injected",
        hn,
        S[h]["class strong"]["shared_injected"],
        "review4 compact.json",
    )
    srow(
        "strong+moderate",
        "302 shared injected",
        hn,
        S[h]["strong+moderate"]["shared_injected"],
        "review4 compact.json",
    )
for h, hn in (("72h", "72 h"), ("720h", "30 d")):
    srow(
        "strong+moderate",
        "post-2022 (210)",
        hn,
        p[h]["strong+moderate"],
        "review4 compact.json; = combined_v217.json",
    )
w()
w("### Cross-check against the known values")
cv = ex["combined_v217"]
w(f"- combined_v217.json: {json.dumps({k: v for k, v in cv.items() if k != 'source'})}")
w("- early direct link 302: 26 vs 1 - REPRODUCED (26 / 1, R 0.042-0.045).")
w("- late direct link 302: 8 vs 13 - REPRODUCED (8 / 13 at 30 d).")
w(
    "- strong+moderate 302: 31/1 (0.04) at 72 h, 35/3 (0.09) at 30 d - REPRODUCED on the as-cached basis (31/1 R 0.038; 35/3 R 0.093). With the labelled shared deposit injected into the 152 old runs the full-v2.17 counts are 33/1 and 38/3 (R 0.036, 0.086): the thesis counts exclude shared-deposit leads on 152 of the 302 depositors."
)
w("- post-2022 210: 12/1 and 12/3 - REPRODUCED (R 0.107, 0.276).")
w(
    "- shared deposit labelled 1,000: 27 vs 4 (0.16) - REPRODUCED from dar_universe.json; unlabelled 16 vs 12 (0.80) - REPRODUCED from dar_universe_nolabels.json (R 0.798; this is the run that ignores the attribution set, not 'loose minus labelled' which would be 11 vs 9)."
)
w(
    "- early profile 4,194: 318 vs 34 (0.12) - REPRODUCED (R 0.117); before/after sanctions 0.037 / 0.19 (stated 0.04 / 0.19) - REPRODUCED."
)
w()
w("## Caveats / not computed")
w(
    "- Early direct link + labelled shared deposit on the 1,000 universe: not computable (no counterparty history cached). The 2 strong leads (302 and ENS alike) are all this combination."
)
w(
    "- N for the 4,194 early-profile cells is not stored in profile*.json (re-running tools/placebo_profile.py would overwrite them, so it was not re-run); R in those rows is the stored fdr."
)
w(
    "- Bootstrap CIs are degenerate when a side has < 5 leads (all resamples that drop the single lead give undefined R); use the exact column. gas price in particular (1 / 0 at 72 h, 1 / 2 at 30 d) cannot be estimated."
)
w(
    "- The 'gas price' lead count uses only gas-price matches that passed the gate in the original full run (narrow() keeps those only), same as tools/placebo_windows.py."
)
w(
    "- The 152 as-cached 'shared_deposit' count is 1 (post-2022 set only); injected labelled hits come from dar/ lookups made with the current label set, not necessarily the labels in force when the post-2022 runs were made."
)
open(os.path.join(R4, "RESULTS.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
print("ok", len(L))
