"""Graph clustering of depositor wallets from correlation evidence.

Heuristics are reduced to edges between wallets and connected components are
taken with a small union-find. Two wallets are linked only on discriminating
evidence: a shared exit candidate carrying a wallet-specific signal, or a shared
multi-pool consolidator that is not a window-overlap artefact.

``gas_price`` is computed against one wallet's own deposit gas prices and
``linked`` against its own counterparties, so either holding for two wallets at
one address says something about the pair. ``self_relayed`` does not: it is a
property of the withdrawal, identical for every wallet whose window contains it,
and crediting it would merge unrelated depositors that merely deposited the same
note count at around the same time.
"""

from __future__ import annotations

from collections import defaultdict

# Signals that say something about the wallet, not merely about the withdrawal.
# See the module docstring for why self_relayed is not among them.
WALLET_SPECIFIC_SIGNALS = frozenset({"gas_price", "linked"})


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def cluster_wallets(corr: dict) -> list[dict]:
    """Return operator clusters of depositor wallets with linking evidence.

    ``corr`` is the dict returned by :func:`tornado_demix.multi.correlate`.
    Returns a list of {wallets, edges} where edges justify each link.
    """
    results = corr["results"]
    wallets = list(results)
    uf = _UnionFind()
    for w in wallets:
        uf.find(w)

    edges = []  # (wallet_a, wallet_b, reason, address)

    # Shared exit candidates carrying a wallet-specific signal:
    # addr -> {wallet: signal set}.
    addr_wallets = defaultdict(dict)
    for wallet, data in results.items():
        for _pool_key, res in data.get("denoms", {}).items():
            for addr, sig in res.get("signals", {}).items():
                strong = set(sig) & WALLET_SPECIFIC_SIGNALS
                if strong and res["counts"][addr] in set(res.get("target_counts", [])):
                    addr_wallets[addr][wallet] = strong

    for addr, wmap in addr_wallets.items():
        ws = sorted(wmap)
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                reason = "shared " + "/".join(sorted(wmap[ws[i]] | wmap[ws[j]])) + " exit"
                uf.union(ws[i], ws[j])
                edges.append((ws[i], ws[j], reason, addr))

    # Shared multi-pool consolidators, skipping those graded as a window-overlap
    # artefact. Grades missing from ``corr`` (a direct caller) are computed here,
    # without deposit spans, so the synchronised-batch downgrade does not apply.
    cross_profile = corr.get("cross_profile", {})
    grades = dict(corr.get("consolidator_grades", {}))
    ungraded = {addr: ws for addr, ws in cross_profile.items() if addr not in grades}
    if ungraded:
        from .multi import grade_consolidators  # multi imports this module

        grades.update(grade_consolidators(ungraded, results, corr.get("fingerprints", {})))
    for addr, ws in cross_profile.items():
        grade = grades[addr]
        if grade["artefact"]:
            continue
        reason = grade["edge_reason"]
        ws = sorted(ws)
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                uf.union(ws[i], ws[j])
                edges.append((ws[i], ws[j], reason, addr))

    groups = defaultdict(list)
    for w in wallets:
        groups[uf.find(w)].append(w)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        clusters.append(
            {
                "wallets": sorted(members),
                "edges": [e for e in edges if e[0] in member_set and e[1] in member_set],
            }
        )
    clusters.sort(key=lambda c: -len(c["wallets"]))
    return clusters
