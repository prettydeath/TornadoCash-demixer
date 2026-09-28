"""Exit groups: recipients withdrawn together in repeated bursts.

An operator who pools several deposits and pays the notes out to many fresh
addresses defeats the count match: no exit receives a voucher-sized count. What
remains is the payout rhythm. The operator's withdrawals come in bursts, and its
exits appear in the same bursts again and again. Two recipients are joined when
at least ``MIN_JOINT`` of their withdrawals fall within ``JOINT_WINDOW_S`` of
each other; the connected groups of three or more are exit groups.

A group says that its members were paid out together, not whose funds they
were. It is tied to the depositor only through an anchor: a member that is
itself a corroborated candidate (strong or moderate band) or an exit the
investigator already knows. Groups never enter a score or a band.

On the Harmony case (2022), one listed exit as the anchor gave a group of 18-22
addresses of which 76-92 % are on the investigators' list (docs/EVALUATION.md).
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict

JOINT_WINDOW_S = 600  # two withdrawals within ten minutes are one burst
MIN_JOINT = 2  # joint bursts needed to join two recipients
MIN_WITHDRAWALS = 2  # a one-off recipient cannot show a rhythm
MIN_GROUP = 3


def co_withdrawal_groups(res: dict) -> list[set[str]]:
    """Groups of recipients of one pool's analysis that withdrew in joint bursts."""
    counts, detail = res.get("counts", {}), res.get("detail", {})
    multi = {a for a, n in counts.items() if n >= MIN_WITHDRAWALS}
    events = sorted((r["ts"], a) for a in multi for r in detail.get(a, []))
    times = [t for t, _ in events]
    joint = Counter()
    for i, (t, a) in enumerate(events):
        for _, b in events[i + 1 : bisect.bisect_right(times, t + JOINT_WINDOW_S)]:
            if b != a:
                joint[(a, b) if a < b else (b, a)] += 1

    parent = {a: a for a in multi}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), n in joint.items():
        if n >= MIN_JOINT:
            parent[find(a)] = find(b)
    groups = defaultdict(set)
    for a in multi:
        groups[find(a)].add(a)
    return [g for g in groups.values() if len(g) >= MIN_GROUP]


def exit_groups(data: dict, ranked: list[dict], known_exits=()) -> list[dict]:
    """Exit groups of a run_demix() result, anchored where possible.

    ``ranked`` is heuristics.ranked_candidates(data); its strong and moderate
    rows are anchors, as is every address in ``known_exits``. Returns one dict
    per group: {pool_key, members, notes, first_ts, last_ts, anchors, anchored};
    ``members`` are sorted by withdrawals received, ``anchors`` are
    {address, why}. Anchored groups come first.
    """
    known = {a.lower() for a in known_exits}
    corroborated = {
        (r["pool_key"], r["address"]): r["band"]
        for r in ranked
        if r["band"] in ("strong", "moderate")
    }
    out = []
    for pool_key, res in data.get("denoms", {}).items():
        counts, detail = res["counts"], res["detail"]
        for group in co_withdrawal_groups(res):
            anchors = []
            for addr in sorted(group):
                if addr in known:
                    anchors.append({"address": addr, "why": "known exit"})
                elif (pool_key, addr) in corroborated:
                    band = corroborated[(pool_key, addr)]
                    anchors.append({"address": addr, "why": f"{band} candidate"})
            ts = [r["ts"] for a in group for r in detail[a]]
            out.append(
                {
                    "pool_key": pool_key,
                    "members": sorted(group, key=lambda a: (-counts[a], a)),
                    "notes": sum(counts[a] for a in group),
                    "first_ts": min(ts),
                    "last_ts": max(ts),
                    "anchors": anchors,
                    "anchored": bool(anchors),
                }
            )
    out.sort(key=lambda g: (not g["anchored"], -len(g["members"]), g["pool_key"], g["members"][0]))
    return out
