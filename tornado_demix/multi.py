"""Multi-wallet correlation: intersections and pool-profile matching.

Given several depositor wallets, this module finds:
  * strong links   - a count-matched candidate shared by 2+ wallets;
  * soft overlaps  - any qualifying recipient shared by 2+ wallets;
  * profile match  - one address that received a wallet's full pool
                     fingerprint;
  * cross profile  - one address that is a full consolidator for 2+ wallets.

Caveat, also stated in the report: wallets that deposit at similar times with
the same voucher size have overlapping search windows, so their count-matched
sets are nearly identical by construction. Raw intersections are then an
artefact of the shared window, not evidence of a link.
"""

from __future__ import annotations

from collections import defaultdict

from .demix import run_demix
from .etherscan import EtherscanClient
from .graph import WALLET_SPECIFIC_SIGNALS, cluster_wallets
from .heuristics import credit_profile_match
from .networks import ETHEREUM, Network

# Wallets whose deposits chain together within this gap form one synchronised
# batch. That is a coordination lead in its own right, and it is also why a
# distinct-fingerprint consolidator inside the batch cannot be trusted: one
# busy-pool recipient satisfies the different note-patterns by shared window.
# A judgement call, like MIN_COUNT_DISCRIMINATION.
SYNC_GAP_HOURS = 6.0

# Single-linkage batches can chain transitively into a group spanning days, so
# the artefact downgrade gates on the actual span of the matched wallets instead.
SYNC_MAX_SPAN_HOURS = 12.0


def correlate(
    client: EtherscanClient,
    wallets: list[str],
    window_days: int = 30,
    fee_lo: float = 0.90,
    fee_hi: float = 0.995,
    gap_hours: float = 24,
    mode: str = "events",
    network: Network = ETHEREUM,
    exit_window_hours: float | None = None,
) -> dict:
    """Run demix on every wallet and compute cross-wallet correlations.

    Returns a dict with keys: results, strong, soft, addr_detail, fingerprints,
    profile_matches, cross_profile. ``exit_window_hours`` narrows each wallet's
    withdrawal search to that many hours after its deposits (see
    :func:`tornado_demix.demix.voucher_windows`).
    """
    results = {}  # wallet -> run_demix output
    cand_members = defaultdict(lambda: defaultdict(set))  # pool_key -> addr -> wallets
    qual_members = defaultdict(lambda: defaultdict(set))  # pool_key -> addr -> wallets
    addr_detail = defaultdict(dict)  # (pool_key, addr) -> wallet -> hits

    for wallet in wallets:
        wallet = wallet.lower()
        data = run_demix(
            client,
            wallet,
            window_days,
            fee_lo,
            fee_hi,
            gap_hours,
            mode=mode,
            network=network,
            exit_window_hours=exit_window_hours,
        )
        results[wallet] = data
        for pool_key, res in data["denoms"].items():
            candidates = set()
            for n in res.get("target_counts", []):
                candidates.update(res["candidates_by_count"].get(n, []))
            for addr in candidates:
                cand_members[pool_key][addr].add(wallet)
            for addr, hits in res["counts"].items():
                qual_members[pool_key][addr].add(wallet)
                addr_detail[(pool_key, addr)][wallet] = hits

    strong = _shared(cand_members)
    strong_keys = {(pool_key, addr) for pool_key, addr, _ in strong}
    soft = [entry for entry in _shared(qual_members) if (entry[0], entry[1]) not in strong_keys]

    fingerprints = _fingerprints(results)
    profile_matches, cross_profile = _profile_match(fingerprints, addr_detail)

    # Feed the profile match back into the per-wallet scores. Only an exact match
    # is credited, and credit_profile_match itself refuses anything under two
    # pools: a single-pool "fingerprint" is just the count match again.
    for wallet, matches in profile_matches.items():
        pool_keys = list(fingerprints.get(wallet, {}))
        for addr, _detail, exact in matches:
            if exact:
                credit_profile_match(results[wallet], pool_keys, addr)

    corr = {
        "results": results,
        "strong": strong,
        "soft": soft,
        "addr_detail": addr_detail,
        "fingerprints": fingerprints,
        "profile_matches": profile_matches,
        "cross_profile": cross_profile,
    }
    corr["sync_groups"] = deposit_synchronicity(results)
    # The grades are the single source of truth for whether a consolidator is a
    # window-overlap artefact; the graph clustering honours the same flag.
    corr["consolidator_grades"] = grade_consolidators(
        cross_profile, results, fingerprints, wallet_deposit_spans(results)
    )
    corr["operator_clusters"] = cluster_wallets(corr)
    return corr


def _fp_key(fingerprint):
    """Hashable canonical form of a wallet fingerprint for distinctness tests."""
    return tuple(sorted(fingerprint.items()))


def wallet_deposit_spans(results: dict[str, dict]) -> dict[str, tuple[int, int]]:
    """wallet -> (first_deposit_ts, last_deposit_ts) for wallets with deposits."""
    spans = {}
    for wallet, data in results.items():
        ts = sorted(d["ts"] for d in data.get("deposits", []) if "ts" in d)
        if ts:
            spans[wallet] = (ts[0], ts[-1])
    return spans


def deposit_synchronicity(results, gap_hours=SYNC_GAP_HOURS):
    """Cluster wallets into synchronised deposit batches.

    Two wallets fall in the same batch when their deposit activity chains within
    ``gap_hours`` - single-linkage on the 1-D deposit timeline. Returns a list of
    ``{wallets, first_ts, last_ts, span_seconds}`` for every batch of two or more
    wallets, widest and tightest first. Single-linkage chains transitively, so a
    caller that needs a tight batch must check ``span_seconds`` itself.
    """
    gap = gap_hours * 3600.0
    spans = wallet_deposit_spans(results)
    if not spans:
        return []

    ordered = sorted(spans.items(), key=lambda kv: kv[1][0])
    groups = []
    cur = [ordered[0][0]]
    cur_first, cur_maxlast = ordered[0][1]
    for wallet, (first, last) in ordered[1:]:
        if first - cur_maxlast <= gap:  # chains onto the batch
            cur.append(wallet)
            cur_maxlast = max(cur_maxlast, last)
        else:
            groups.append((cur, cur_first, cur_maxlast))
            cur, cur_first, cur_maxlast = [wallet], first, last
    groups.append((cur, cur_first, cur_maxlast))

    out = [
        {"wallets": sorted(ws), "first_ts": first, "last_ts": last, "span_seconds": last - first}
        for ws, first, last in groups
        if len(ws) >= 2
    ]
    out.sort(key=lambda g: (-len(g["wallets"]), g["span_seconds"]))
    return out


def _matched_within_span(wallets, deposit_spans, max_span_seconds):
    """True if all matched wallets deposited within ``max_span_seconds``.

    A wallet without deposit times makes the answer False: no downgrade on
    incomplete evidence.
    """
    firsts, lasts = [], []
    for w in wallets:
        span = deposit_spans.get(w)
        if span is None:
            return False
        firsts.append(span[0])
        lasts.append(span[1])
    if not firsts:
        return False
    return (max(lasts) - min(firsts)) <= max_span_seconds


def grade_consolidators(
    cross_profile: dict[str, list[str]],
    results: dict[str, dict],
    fingerprints: dict[str, dict],
    deposit_spans: dict[str, tuple[int, int]] | None = None,
    max_span_seconds: float | None = None,
) -> dict[str, dict]:
    """Grade each cross-wallet consolidator by how much it discriminates.

    * ``strong`` - an independent signal (gas_price / linked) on the address for
      one of the matched wallets: evidence about that wallet, not the window.
    * ``moderate`` - the matched wallets have distinct fingerprints and did not
      all deposit inside one tight window.
    * ``weak`` with ``artefact`` True - the window-overlap artefact, either
      ``"identical"`` (one shared fingerprint, no signal) or ``"synchronized"``
      (distinct fingerprints, but every matched wallet deposited within
      ``max_span_seconds``, default :data:`SYNC_MAX_SPAN_HOURS`).

    Returns ``{addr: grade}`` where grade carries: wallets, band, independent
    (sorted signals), self_relayed (bool), distinct_fingerprints (int),
    synchronized (bool), artefact (bool), artefact_kind (str|None) and edge_reason
    (why the graph may merge on it, or None if it may not).
    """
    if max_span_seconds is None:
        max_span_seconds = SYNC_MAX_SPAN_HOURS * 3600.0
    deposit_spans = deposit_spans or {}
    grades = {}
    for addr, wallets in cross_profile.items():
        independent = set()
        self_relayed = False
        for w in wallets:
            for _pk, res in results.get(w, {}).get("denoms", {}).items():
                sig = set(res.get("signals", {}).get(addr, []))
                independent |= sig & WALLET_SPECIFIC_SIGNALS
                if "self_relayed" in sig:
                    self_relayed = True
        distinct = len({_fp_key(fingerprints.get(w, {})) for w in wallets})
        synchronized = _matched_within_span(wallets, deposit_spans, max_span_seconds)
        artefact_kind = None
        if independent:
            band, artefact = "strong", False
            edge_reason = (
                "shared full-fingerprint consolidator (+" + "/".join(sorted(independent)) + ")"
            )
        elif distinct >= 2 and not synchronized:
            band, artefact = "moderate", False
            edge_reason = "shared full-fingerprint consolidator (distinct fingerprints)"
        elif distinct >= 2:  # distinct patterns, but one synchronised batch
            band, artefact = "weak", True
            edge_reason = None
            artefact_kind = "synchronized"
        else:  # one identical fingerprint, no signal
            band, artefact = "weak", True
            edge_reason = None
            artefact_kind = "identical"
        grades[addr] = {
            "wallets": sorted(wallets),
            "band": band,
            "independent": sorted(independent),
            "self_relayed": self_relayed,
            "distinct_fingerprints": distinct,
            "synchronized": synchronized,
            "artefact": artefact,
            "artefact_kind": artefact_kind,
            "edge_reason": edge_reason,
        }
    return grades


def _shared(membership):
    """Return [(pool_key, address, sorted_wallets)] for addresses in 2+ wallets."""
    shared = []
    for pool_key, addr_map in membership.items():
        for addr, wallets in addr_map.items():
            if len(wallets) >= 2:
                shared.append((pool_key, addr, sorted(wallets)))
    shared.sort(key=lambda entry: (-len(entry[2]), entry[0]))
    return shared


def _fingerprints(results):
    """wallet -> {pool_key: total notes deposited across all its vouchers}."""
    return {wallet: wallet_fingerprint(data["vouchers"]) for wallet, data in results.items()}


def wallet_fingerprint(vouchers: list[dict]) -> dict[str, int]:
    """{pool_key: total notes deposited} over all of a wallet's vouchers."""
    fingerprint = defaultdict(int)
    for voucher in vouchers:
        fingerprint[voucher["pool_key"]] += voucher["count"]
    return dict(fingerprint)


def _profile_match(fingerprints, addr_detail):
    """Find addresses that received a wallet's full pool fingerprint.

    For wallet W with fingerprint {p: N_p}, a consolidation candidate A must
    have received at least N_p withdrawals from every pool p in W's window.
    Multi-pool fingerprints are far more discriminating than a single count, so
    those matches are highlighted as strong.
    """
    profile_matches = defaultdict(list)  # wallet -> [(addr, detail, exact)]
    addr_profile_wallets = defaultdict(set)  # addr -> wallets it fully matches

    for wallet, fingerprint in fingerprints.items():
        pool_keys = list(fingerprint.keys())
        per_pool_sets = []
        for pool_key in pool_keys:
            need = fingerprint[pool_key]
            matching = {
                addr
                for (p, addr), by_wallet in addr_detail.items()
                if p == pool_key and by_wallet.get(wallet, 0) >= need
            }
            per_pool_sets.append(matching)

        common = set.intersection(*per_pool_sets) if per_pool_sets else set()
        for addr in common:
            detail, exact = {}, True
            for pool_key in pool_keys:
                received = addr_detail[(pool_key, addr)].get(wallet, 0)
                need = fingerprint[pool_key]
                detail[pool_key] = (received, need)
                if received != need:
                    exact = False
            profile_matches[wallet].append((addr, detail, exact))
            if len(pool_keys) >= 2:
                addr_profile_wallets[addr].add(wallet)
        profile_matches[wallet].sort(key=lambda entry: (not entry[2], entry[0]))

    cross_profile = {
        addr: sorted(wallets) for addr, wallets in addr_profile_wallets.items() if len(wallets) >= 2
    }
    return profile_matches, cross_profile
