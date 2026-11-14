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

from collections import Counter, defaultdict

from .demix import (
    _log,
    _set_count_candidates,
    analyze_denom,
    analyze_denom_events,
    cluster_vouchers,
    run_demix,
)
from .errors import ApiError, ApiKeyError, BlockLookupError
from .etherscan import EtherscanClient
from .graph import WALLET_SPECIFIC_SIGNALS, cluster_wallets
from .heuristics import apply_heuristics, credit_profile_match, wallet_profile
from .honest import EXACT_K_CAVEAT, GROUP_VIEW_CAVEAT
from .incomplete import attach, lookup_failed, mark, truncated_since
from .networks import ETHEREUM, Network
from .rpc import make_contract_check, make_gas_price_gate

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
    max_voucher_span_hours: float | None = None,
    labels: dict[str, dict] | None = None,
    deposit_addresses: bool = True,
    group: bool = False,
    exact_k: bool = False,
    exact_k_windows: int | None = None,
) -> dict:
    """Run demix on every wallet and compute cross-wallet correlations.

    Returns a dict with keys: results, strong, soft, addr_detail, fingerprints,
    profile_matches, cross_profile, and ``errors`` (every wallet's failed lookups,
    each tagged with ``wallet``), ``error_count`` and ``truncated``. ``exit_window_hours`` narrows each wallet's
    withdrawal search to that many hours after its deposits (see
    :func:`tornado_demix.demix.voucher_windows`).

    ``group`` adds ``corr["group"]``: the wallets treated as one depositor
    (:func:`group_view`); the per-wallet results do not change. ``exact_k`` (with
    ``group``) adds the exploratory exact-k detector with ``exact_k_windows`` base
    windows per pool (default :data:`EXACT_K_BASE_WINDOWS`).
    """
    start_mark = mark(client)
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
            max_voucher_span_hours=max_voucher_span_hours,
            labels=labels,
            deposit_addresses=deposit_addresses,
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
    funder_errors: list[dict] = []
    corr["shared_funders"] = shared_funders(client, results, labels, funder_errors)
    # The grades are the single source of truth for whether a consolidator is a
    # window-overlap artefact; the graph clustering honours the same flag.
    corr["consolidator_grades"] = grade_consolidators(
        cross_profile, results, fingerprints, wallet_deposit_spans(results)
    )
    corr["operator_clusters"] = cluster_wallets(corr)
    group_errors: list[dict] = []
    if group:
        corr["group"] = group_view(
            client,
            results,
            window_days=window_days,
            fee_lo=fee_lo,
            fee_hi=fee_hi,
            gap_hours=gap_hours,
            mode=mode,
            network=network,
            exit_window_hours=exit_window_hours,
            max_voucher_span_hours=max_voucher_span_hours,
            exact_k=exact_k,
            exact_k_windows=exact_k_windows,
        )
        group_errors = [dict(e, wallet=GROUP_KEY) for e in corr["group"].get("errors", [])]
    # every wallet's failed lookups, tagged with the wallet (tornado_demix.incomplete)
    errors = (
        [dict(e, wallet=wallet) for wallet, data in results.items() for e in data.get("errors", [])]
        + funder_errors
        + group_errors
    )
    return attach(corr, errors, truncated_since(client, start_mark))


# A funder with this many transactions is a service or a busy wallet that pays
# many unrelated people; sharing it says nothing about common control.
BUSY_FUNDER_TXS = 200


def shared_funders(client, results, labels=None, errors=None):
    """Immediate funders shared by two or more depositor wallets.

    Returns {funder: sorted wallets}. A labelled funder (an exchange, a bridge)
    and a busy one (BUSY_FUNDER_TXS or more transactions) are left out: they fund
    many unrelated users. MixLaunder: 90.1 % of laundering deposit addresses
    share an immediate funder with another deposit address of the same case.
    A funder whose busy check fails is kept (unchecked) and, when ``errors`` is a
    list, the failure is appended to it.
    """
    labels = labels or {}
    by_funder = defaultdict(set)
    for wallet, data in results.items():
        for funder in data.get("funders", []):
            by_funder[funder].add(wallet)
    out = {}
    for funder, wallets in sorted(by_funder.items()):
        if len(wallets) < 2 or labels.get(funder):
            continue
        try:
            busy = hasattr(client, "has_at_least_txs") and client.has_at_least_txs(
                funder, BUSY_FUNDER_TXS
            )
        except ApiKeyError:
            raise
        except ApiError as exc:
            busy = False  # unchecked: kept, and listed as a failed lookup
            if errors is not None:
                errors.append(lookup_failed("funder busy check", exc, address=funder))
        if busy:
            continue
        out[funder] = sorted(wallets)
    return out


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
    out.sort(key=lambda g: (-len(g["wallets"]), g["span_seconds"], g["first_ts"], g["wallets"]))
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
    shared.sort(key=lambda entry: (-len(entry[2]), entry[0], entry[1]))
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
    by_pool = defaultdict(list)  # pool_key -> [(addr, by_wallet)], built once
    for (pool_key, addr), by_wallet in addr_detail.items():
        by_pool[pool_key].append((addr, by_wallet))

    for wallet, fingerprint in fingerprints.items():
        pool_keys = list(fingerprint.keys())
        per_pool_sets = []
        for pool_key in pool_keys:
            need = fingerprint[pool_key]
            matching = {
                addr for addr, by_wallet in by_pool[pool_key] if by_wallet.get(wallet, 0) >= need
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


# ---- group view: the given wallets treated as one depositor -----------------------

# The tag of the group view's failed lookups in ``corr["errors"]``.
GROUP_KEY = "group"

# Exact-k detector (exploratory): base windows read per pool, and the hard cap. Each
# base window costs two block-by-time lookups plus the pages of one log read of the
# pool over a window as long as the group window.
EXACT_K_BASE_WINDOWS = 6
EXACT_K_MAX_WINDOWS = 20
# The base windows of one pool together span at most this long; a longer group window
# gets fewer base windows (none when one window alone is longer), reported in the row.
EXACT_K_MAX_BASE_DAYS = 365

# A group window longer than this gets a notice: wallets that deposited months apart
# make one window of months, a huge recipient field (the count gate rarely passes) and
# a costly log read. A judgement call, not measured.
GROUP_LONG_WINDOW_DAYS = 90


def _analyze(client, pool, span, window_days, fee_lo, fee_hi, mode, exit_window_hours, notes):
    """One pool over the window of ``span`` (a voucher-shaped dict), in either mode."""
    if mode == "events":
        return analyze_denom_events(client, pool, [span], window_days, exit_window_hours, notes)
    return analyze_denom(
        client, pool, [span], window_days, fee_lo, fee_hi, exit_window_hours, notes
    )


def group_view(
    client: EtherscanClient,
    results: dict[str, dict],
    window_days: int = 30,
    fee_lo: float = 0.90,
    fee_hi: float = 0.995,
    gap_hours: float = 24,
    mode: str = "events",
    network: Network = ETHEREUM,
    exit_window_hours: float | None = None,
    max_voucher_span_hours: float | None = None,
    exact_k: bool = False,
    exact_k_windows: int | None = None,
) -> dict:
    """The wallets of ``results`` treated as ONE depositor.

    Their deposits are merged (copies, each tagged with ``wallet``) and clustered into
    vouchers as for one wallet; each pool is searched over ONE window, from the group's
    first deposit into it to the window end after its last one. Then the existing rules
    run on the merged notes: the count-candidate gate with the voucher-order check
    (:func:`tornado_demix.demix._set_count_candidates`) and every signal of
    :func:`tornado_demix.heuristics.apply_heuristics`, the early profile included. A
    direct link counts against the union of the wallets' filtered counterparties and
    the wallets themselves. Withdrawal senders and shared deposit addresses (context
    only) are not recomputed.

    Merging is the analyst's assertion that the wallets are one actor; the result is
    conditional on it (``caveat``). The per-wallet results are not touched.

    Returns a run_demix-shaped dict (``deposits``, ``vouchers``, ``denoms`` with
    signals, ``unresolved``, ``params``) plus ``wallets``, ``profile``, ``caveat``,
    ``exact_k`` (rows of :func:`exact_k_rows`, empty unless ``exact_k``) and
    ``errors`` / ``error_count`` / ``truncated`` of this step. A pool whose read fails
    is listed in ``unresolved`` and its failure in ``errors``; it is not fatal.
    """
    start_mark = mark(client)
    errors: list[dict] = []
    wallets = sorted(results)
    deposits = sorted(
        (
            dict(d, wallet=wallet)
            for wallet, data in results.items()
            for d in data.get("deposits", [])
        ),
        key=lambda d: (d["ts"], d.get("wallet", ""), str(d.get("tx") or d.get("hash") or "")),
    )
    params = {
        "window_days": window_days,
        "fee_window": [fee_lo, fee_hi],
        "gap_hours": gap_hours,
        "mode": mode,
        "network": network.name,
        "currency": network.currency,
        "exit_window_hours": exit_window_hours,
        "voucher_order": True,
        "group": True,
    }
    out = {
        "wallet": "",
        "wallets": wallets,
        "caveat": GROUP_VIEW_CAVEAT,
        "params": params,
        "deposits": deposits,
        "vouchers": [],
        "denoms": {},
        "unresolved": [],
        "profile": {},
        "exact_k": [],
        "exact_k_caveat": EXACT_K_CAVEAT,
        "notices": [],
    }
    if not deposits:
        return attach(out, errors, truncated_since(client, start_mark))
    _log("[*] Group view: {} wallets, {} deposits".format(len(wallets), len(deposits)))

    vouchers = cluster_vouchers(
        deposits, gap_hours=gap_hours, max_span_hours=max_voucher_span_hours
    )
    denoms = {}
    spans = {}
    for pool_key in sorted({v["pool_key"] for v in vouchers}):
        pool = network.by_key[pool_key]
        own = [d for d in deposits if d["pool_key"] == pool_key]
        # One window per pool: a single voucher-shaped span over every merged note.
        span = {
            "pool_key": pool_key,
            "count": len(own),
            "first_ts": own[0]["ts"],
            "last_ts": own[-1]["ts"],
        }
        try:
            denoms[pool_key] = _analyze(
                client, pool, span, window_days, fee_lo, fee_hi, mode, exit_window_hours, errors
            )
            spans[pool_key] = span
        except ApiKeyError:
            raise
        except (BlockLookupError, ApiError) as exc:
            _log("  [group {}] SKIPPED - {}".format(pool_key, exc))
            out["unresolved"].append({"pool_key": pool_key, "reason": str(exc)})
            errors.append(lookup_failed("group window ({})".format(pool_key), exc))

    for pool_key, res in denoms.items():
        for w in res.get("windows", []):
            days = (w["end_ts"] - w["first_ts"]) / 86400
            if days > GROUP_LONG_WINDOW_DAYS:
                out["notices"].append(
                    f"{pool_key}: the group window spans {days:.0f} days (more than "
                    f"{GROUP_LONG_WINDOW_DAYS}); the wallets deposited far apart, so the "
                    "recipient field is large, a count match rarely passes the gate and the "
                    "log read is costly"
                )
                _log("  [group {}] [!] window of {:.0f} days".format(pool_key, days))

    order_params = {"window_days": window_days, "exit_window_hours": exit_window_hours}
    for pool_key, res in denoms.items():
        _set_count_candidates(res, vouchers, order_params, pool_key)
    out["vouchers"] = vouchers
    out["denoms"] = denoms
    out["profile"] = wallet_profile(out)

    counterparties = set(wallets)
    for data in results.values():
        counterparties.update(data.get("counterparty_addresses", []))
    is_contract = make_contract_check(network.rpc_url)
    gas_gate = make_gas_price_gate(network.rpc_url)
    apply_heuristics(out, counterparties, gas_gate=gas_gate, is_contract=is_contract)
    errors += getattr(is_contract, "errors", [])
    errors += getattr(gas_gate, "errors", [])

    if exact_k:
        out["exact_k"] = exact_k_rows(
            client,
            out,
            spans,
            network,
            window_days=window_days,
            fee_lo=fee_lo,
            fee_hi=fee_hi,
            mode=mode,
            exit_window_hours=exit_window_hours,
            n_windows=exact_k_windows,
            errors=errors,
        )
    return attach(out, errors, truncated_since(client, start_mark))


def exact_k_rows(
    client,
    group: dict,
    spans: dict[str, dict],
    network: Network,
    window_days: int = 30,
    fee_lo: float = 0.90,
    fee_hi: float = 0.995,
    mode: str = "events",
    exit_window_hours: float | None = None,
    n_windows: int | None = None,
    errors: list | None = None,
) -> list[dict]:
    """Exploratory: "exactly k notes of a pool within the group window", with an empirical base.

    For every pool where the group deposited k >= 2 notes: ``observed`` is the number
    of recipients that received exactly k withdrawals in the group window (the events
    already read). The base is the same count in ``n_windows`` earlier windows of the
    same length in the same pool, back to back and not overlapping the group window
    (window i ends one second before window i-1 starts). A window with no withdrawal at
    all (before the pool existed, or a dead pool) is not comparable and is skipped
    (``empty_windows``); a window whose read fails is skipped and recorded in ``errors``.
    Reports ``base_counts``, ``base_mean``, ``n_ge_observed`` (base windows with at
    least ``observed`` such recipients) and ``p_ge_observed``, the smoothed empirical
    tail (n_ge_observed + 1) / (base_windows + 1), so a handful of windows never reads
    as "impossible" (None without a usable window). ``observed`` and the base counts
    both count only withdrawals timed inside their window. Context only: never scored,
    never a lead.

    Cost: up to ``n_windows`` (capped at :data:`EXACT_K_MAX_WINDOWS`) extra log reads
    per pool, each with two block-by-time lookups and as many pages as the window holds,
    and the base windows of a pool span at most :data:`EXACT_K_MAX_BASE_DAYS` days
    together (``capped_windows`` in the row says how many were dropped for that).
    """
    n = EXACT_K_BASE_WINDOWS if n_windows is None else int(n_windows)
    n = max(1, min(n, EXACT_K_MAX_WINDOWS))
    rows = []
    for pool_key, k in sorted(group.get("profile", {}).items()):
        res = group["denoms"].get(pool_key)
        span = spans.get(pool_key)
        if k < 2 or res is None or span is None:
            continue
        window = res["windows"][0]
        length = window["end_ts"] - window["first_ts"]
        if length <= 0:
            continue
        # Timed inside the window, as the base windows are (the block bounds can reach
        # one block outside it).
        inside = Counter(
            addr
            for addr, recs in res["detail"].items()
            for r in recs
            if window["first_ts"] <= r["ts"] <= window["end_ts"]
        )
        observed = sum(1 for c in inside.values() if c == k)
        pool = network.by_key[pool_key]
        n_pool = min(n, int(EXACT_K_MAX_BASE_DAYS * 86400 // length))
        counts, empty, failed = [], 0, 0
        for i in range(1, n_pool + 1):
            shifted = dict(
                span,
                first_ts=span["first_ts"] - i * length,
                last_ts=span["last_ts"] - i * length - 1,
            )
            try:
                base = _analyze(
                    client,
                    pool,
                    shifted,
                    window_days,
                    fee_lo,
                    fee_hi,
                    mode,
                    exit_window_hours,
                    None,
                )
            except ApiKeyError:
                raise
            except (BlockLookupError, ApiError) as exc:
                failed += 1
                if errors is not None:
                    errors.append(lookup_failed("exact-k base window ({})".format(pool_key), exc))
                continue
            # Adjacent windows share a boundary block; only withdrawals timed inside
            # this window count, so none is counted in two windows.
            lo, hi = shifted["first_ts"], window["first_ts"] - (i - 1) * length - 1
            per = Counter(
                addr for addr, recs in base["detail"].items() for r in recs if lo <= r["ts"] <= hi
            )
            if not per:
                empty += 1
                continue
            counts.append(sum(1 for c in per.values() if c == k))
        usable = len(counts)
        n_ge = sum(1 for c in counts if c >= observed)
        rows.append(
            {
                "pool_key": pool_key,
                "k": k,
                "window_hours": round(length / 3600, 2),
                "observed": observed,
                "requested_windows": n,
                "capped_windows": n - n_pool,
                "max_base_days": EXACT_K_MAX_BASE_DAYS,
                "base_windows": usable,
                "empty_windows": empty,
                "failed_windows": failed,
                "base_counts": counts,
                "base_mean": round(sum(counts) / usable, 4) if usable else None,
                "n_ge_observed": n_ge,
                "p_ge_observed": round((n_ge + 1) / (usable + 1), 4) if usable else None,
                "context": True,
            }
        )
    return rows


def exact_k_tail_text(row: dict) -> str:
    """The smoothed tail of an exact-k row with its window count, or "n/a"."""
    if row.get("p_ge_observed") is None:
        return "n/a"
    return "{:.2f} (smoothed, {} of n={})".format(
        row["p_ge_observed"], row.get("n_ge_observed", 0), row["base_windows"]
    )


def exact_k_skips(row: dict) -> str:
    """The base windows an exact-k row did not use, and why ("" when none)."""
    parts = []
    if row.get("capped_windows"):
        parts.append(
            "{} dropped by the {}-day base cap".format(
                row["capped_windows"], row.get("max_base_days", EXACT_K_MAX_BASE_DAYS)
            )
        )
    if row.get("empty_windows"):
        parts.append("{} empty skipped".format(row["empty_windows"]))
    if row.get("failed_windows"):
        parts.append("{} failed".format(row["failed_windows"]))
    return ", ".join(parts)
