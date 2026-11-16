"""Cluster diagnostics for the hold-out intervals (offline, no dependencies).

The exact interval treats the leads as independent withdrawals; the bootstrap resamples
depositors. Neither is reliable when the leads of a side come from a handful of depositors.
"""

from __future__ import annotations

MIN_DEPOSITORS = 3  # leads from fewer depositors than this on a side: not reliable
MAX_SHARE = 0.5  # one depositor with more than this share of a side's leads: not reliable
ASSUMPTION = "assumes independent withdrawals"


def diagnostics(target_leads, decoy_leads, dropped):
    """target_leads / decoy_leads: lead count per depositor; dropped: bootstrap replicates
    discarded as undefined. Returns the counts, the largest single depositor's share and,
    when the interval cannot be read as reliable, the warning text."""
    out = {"bootstrap_dropped": dropped, "ci_assumption": ASSUMPTION}
    triggers = []  # (side, depositors the leads are concentrated in); only sides with leads
    for side, leads in (("target", target_leads), ("decoy", decoy_leads)):
        have = [x for x in leads if x]
        total = sum(have)
        out[f"depositors_with_{side}_leads"] = len(have)
        out[f"max_{side}_lead_share"] = round(max(have) / total, 3) if total else None
        if not have:
            continue  # a side without leads keeps its interval; it cannot be concentrated
        if len(have) < MIN_DEPOSITORS:
            triggers.append((side, len(have)))
        elif max(have) / total > MAX_SHARE:
            triggers.append((side, 1))  # one depositor holds most of the leads
    out["reliable"] = not triggers
    if triggers:
        out["warning_side"] = ", ".join(side for side, _n in triggers)
        out["warning"] = (
            "insufficient information (leads concentrated in "
            f"{min(n for _s, n in triggers)} depositor(s))"
        )
    if not out["depositors_with_target_leads"] and not out["depositors_with_decoy_leads"]:
        out["reliable"] = False
        out["lead_note"] = "no leads on either side"
    return out
