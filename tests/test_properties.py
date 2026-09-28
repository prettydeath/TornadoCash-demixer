"""Property-based tests (Hypothesis) from the independent audit of v2.10.0.

Invariants of the scoring, the count discrimination, voucher clustering and
window construction over generated inputs rather than hand-picked cases.
"""

from __future__ import annotations

import random

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tornado_demix.demix import cluster_vouchers, voucher_windows  # noqa: E402
from tornado_demix.heuristics import (  # noqa: E402
    SIGNAL_WEIGHTS,
    _score,
    confidence_band,
    count_discrimination,
)
from tornado_demix.multi import deposit_synchronicity  # noqa: E402
from tornado_demix.pools import Pool, assign_keys  # noqa: E402

_settings = settings(deadline=None, max_examples=100)


# ==========================================================================
# count_discrimination
# ==========================================================================
_addresses = st.integers(min_value=0, max_value=10**9).map(lambda i: "0x%040x" % i)


@st.composite
def _counts_dicts(draw, min_size=1, max_size=25):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    addrs = draw(st.lists(_addresses, min_size=n, max_size=n, unique=True))
    hits = draw(st.lists(st.integers(min_value=0, max_value=20), min_size=n, max_size=n))
    return dict(zip(addrs, hits))


@given(_counts_dicts(), st.integers(min_value=0, max_value=25))
@_settings
def test_count_discrimination_is_always_in_the_unit_interval(counts, hits):
    res = {"counts": counts, "unique_recipients": len(counts)}
    d = count_discrimination(res, hits)
    assert 0.0 <= d <= 1.0


@given(st.integers(min_value=2, max_value=30), st.integers(min_value=0, max_value=10))
@_settings
def test_a_count_the_whole_field_shares_discriminates_nothing(n, hits):
    """0.0 "when every recipient shares the count" (METHODOLOGY.md)."""
    counts = {("0x%040x" % i): hits for i in range(n)}
    res = {"counts": counts, "unique_recipients": n}
    assert count_discrimination(res, hits) == 0.0


@given(_counts_dicts(), st.integers(min_value=0, max_value=25))
@_settings
def test_count_discrimination_does_not_depend_on_dict_insertion_order(counts, hits):
    res_a = {"counts": counts, "unique_recipients": len(counts)}
    shuffled_items = list(counts.items())
    random.Random(0).shuffle(shuffled_items)  # noqa: S311 - test-only shuffling, not crypto
    res_b = {"counts": dict(shuffled_items), "unique_recipients": len(counts)}
    assert count_discrimination(res_a, hits) == count_discrimination(res_b, hits)


def test_count_discrimination_on_an_empty_or_singleton_field_is_zero():
    """unique <= 1 is a distinct guard from "whole field shares the count"."""
    assert count_discrimination({"counts": {}, "unique_recipients": 0}, 3) == 0.0
    assert count_discrimination({"counts": {"0xa": 3}, "unique_recipients": 1}, 3) == 0.0


# ==========================================================================
# _score - noisy-OR over families, using the SHIPPED SIGNAL_WEIGHTS.
#
# (test_edge_cases.py::test_score_has_no_defensive_clamp_on_out_of_range_weights
# shows these properties do NOT hold for arbitrary weight values - only for
# the ones actually shipped, which are all in (0, 1).)
# ==========================================================================
_signal_names = sorted(
    SIGNAL_WEIGHTS
)  # count_match, gas_price, linked, profile_match, self_relayed


@given(
    st.sets(st.sampled_from(_signal_names)),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
@_settings
def test_score_is_always_in_the_unit_interval_for_the_shipped_weights(signals, disc):
    assert 0.0 <= _score(signals, disc) <= 1.0


@given(
    st.sets(st.sampled_from(_signal_names), min_size=0, max_size=len(_signal_names) - 1),
    st.sampled_from(_signal_names),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
@_settings
def test_score_is_monotone_non_decreasing_as_a_signal_is_added(signals, extra, disc):
    assume(extra not in signals)
    base = _score(signals, disc)
    grown = _score(signals | {extra}, disc)
    assert grown >= base - 1e-9


@given(
    st.sets(st.sampled_from(_signal_names), min_size=1),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
@_settings
def test_score_ignores_duplicate_signals_and_input_order(signals, disc):
    as_list = list(signals) * 2
    random.Random(0).shuffle(as_list)  # noqa: S311 - test-only shuffling, not crypto
    assert _score(as_list, disc) == _score(signals, disc)


@given(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
@_settings
def test_count_match_alone_is_scaled_exactly_by_discrimination(disc):
    expected = round(SIGNAL_WEIGHTS["count_match"] * disc, 4)
    assert _score({"count_match"}, disc) == expected


def test_score_of_no_signals_is_zero():
    assert _score(set(), 1.0) == 0.0
    assert _score([], 0.0) == 0.0


# ==========================================================================
# confidence_band
# ==========================================================================
_all_signals = ["count_match", "self_relayed", "gas_price", "linked", "profile_match"]


@given(st.sets(st.sampled_from(_all_signals)))
@_settings
def test_confidence_band_is_always_one_of_the_three_bands(signals):
    assert confidence_band(signals) in ("strong", "moderate", "weak")


@given(
    st.sets(st.sampled_from(_all_signals)),
    st.floats(min_value=0.01, max_value=0.99),
    st.floats(min_value=0.01, max_value=0.99),
    st.floats(min_value=0.01, max_value=0.99),
    st.floats(min_value=0.01, max_value=0.99),
    st.floats(min_value=0.01, max_value=0.99),
)
@_settings
def test_confidence_band_does_not_depend_on_signal_weights(signals, w1, w2, w3, w4, w5):
    """METHODOLOGY.md: "The band does not depend on the weights at all" -
    empirically re-verified by tools/sensitivity.py against saved results;
    this is the structural version: confidence_band's own source
    (heuristics.py) never reads SIGNAL_WEIGHTS, so no weight assignment can
    move it, for ANY signal subset and ANY (valid) weight assignment."""
    before = confidence_band(signals)
    saved = dict(SIGNAL_WEIGHTS)
    try:
        SIGNAL_WEIGHTS.update(
            {
                "count_match": w1,
                "self_relayed": w2,
                "gas_price": w3,
                "linked": w4,
                "profile_match": w5,
            }
        )
        after = confidence_band(signals)
    finally:
        SIGNAL_WEIGHTS.clear()
        SIGNAL_WEIGHTS.update(saved)
    assert before == after


@given(st.sets(st.sampled_from(_all_signals)))
@_settings
def test_count_match_plus_self_relayed_alone_never_reaches_strong(signals):
    """count_match and self_relayed are ONE family (amount+timing); a signal
    set drawn only from {count_match, self_relayed} (plus anything else
    filtered out) can therefore never band "strong", which needs >= 2
    families."""
    restricted = signals & {"count_match", "self_relayed"}
    assert confidence_band(restricted) != "strong"


# ==========================================================================
# cluster_vouchers
# ==========================================================================
@st.composite
def _deposit_lists(draw, max_size=15):
    n = draw(st.integers(min_value=0, max_value=max_size))
    pool_keys = draw(
        st.lists(st.sampled_from(["1 ETH", "0.1 ETH", "10 ETH"]), min_size=n, max_size=n)
    )
    timestamps = draw(
        st.lists(st.integers(min_value=0, max_value=10_000_000), min_size=n, max_size=n)
    )
    return [
        {"pool_key": pk, "denom": 1.0, "asset": "ETH", "ts": ts, "block": ts}
        for pk, ts in zip(pool_keys, timestamps)
    ]


@given(_deposit_lists(), st.integers(min_value=0, max_value=200))
@_settings
def test_cluster_vouchers_conserves_every_deposit(deposits, gap_hours):
    vouchers = cluster_vouchers(deposits, gap_hours=gap_hours)
    assert sum(v["count"] for v in vouchers) == len(deposits)


@given(_deposit_lists(), st.integers(min_value=0, max_value=200))
@_settings
def test_cluster_vouchers_is_insensitive_to_input_order(deposits, gap_hours):
    shuffled = list(deposits)
    random.Random(0).shuffle(shuffled)  # noqa: S311 - test-only shuffling, not crypto
    a = cluster_vouchers(deposits, gap_hours=gap_hours)
    b = cluster_vouchers(shuffled, gap_hours=gap_hours)

    def _key(vouchers):
        return sorted((v["pool_key"], v["first_ts"], v["last_ts"], v["count"]) for v in vouchers)

    assert _key(a) == _key(b)


@given(_deposit_lists(), st.integers(min_value=0, max_value=200))
@_settings
def test_cluster_vouchers_windows_never_start_after_they_end(deposits, gap_hours):
    for v in cluster_vouchers(deposits, gap_hours=gap_hours):
        assert v["first_ts"] <= v["last_ts"]


# ==========================================================================
# voucher_windows
# ==========================================================================
@st.composite
def _voucher_lists(draw, max_size=10):
    n = draw(st.integers(min_value=0, max_value=max_size))
    firsts = draw(st.lists(st.integers(min_value=0, max_value=10_000_000), min_size=n, max_size=n))
    spans = draw(st.lists(st.integers(min_value=0, max_value=100_000), min_size=n, max_size=n))
    return [{"first_ts": f, "last_ts": f + s, "count": 1} for f, s in zip(firsts, spans)]


@given(_voucher_lists(), st.integers(min_value=0, max_value=60))
@_settings
def test_voucher_windows_conserves_every_voucher(vouchers, window_days):
    windows = voucher_windows(vouchers, window_days=window_days)
    assert sum(w["voucher_count"] for w in windows) == len(vouchers)


@given(_voucher_lists(), st.integers(min_value=1, max_value=60))
@_settings
def test_voucher_windows_are_sorted_and_strictly_separated(vouchers, window_days):
    windows = voucher_windows(vouchers, window_days=window_days)
    for a, b in zip(windows, windows[1:]):
        assert a["first_ts"] <= b["first_ts"]
        assert b["first_ts"] > a["end_ts"]  # the merge condition is "<=", so a gap is strict


@given(_voucher_lists(), st.integers(min_value=1, max_value=60))
@_settings
def test_voucher_windows_merging_is_idempotent(vouchers, window_days):
    """Feeding a first pass's own output windows back in (as zero-width
    vouchers, via window_days=0 so nothing grows) must be a no-op: the merge
    loop guarantees consecutive output windows satisfy
    ``next.first_ts > prev.end_ts`` (see the "strictly separated" property
    above), which is precisely the condition under which the SAME loop starts
    a fresh window every time - so a second pass cannot merge anything
    further."""
    first_pass = voucher_windows(vouchers, window_days=window_days)
    synthetic = [
        {"first_ts": w["first_ts"], "last_ts": w["end_ts"], "count": w["voucher_count"]}
        for w in first_pass
    ]
    second_pass = voucher_windows(synthetic, window_days=0)
    assert [(w["first_ts"], w["end_ts"]) for w in second_pass] == [
        (w["first_ts"], w["end_ts"]) for w in first_pass
    ]


# ==========================================================================
# deposit_synchronicity
# ==========================================================================
@st.composite
def _wallet_deposit_maps(draw, max_wallets=8):
    n = draw(st.integers(min_value=0, max_value=max_wallets))
    wallets = ["0x%040x" % i for i in range(n)]
    result = {}
    for w in wallets:
        k = draw(st.integers(min_value=0, max_value=4))
        ts_list = draw(
            st.lists(st.integers(min_value=0, max_value=10_000_000), min_size=k, max_size=k)
        )
        result[w] = {"deposits": [{"ts": t} for t in ts_list], "denoms": {}}
    return result


@given(_wallet_deposit_maps(), st.floats(min_value=0.1, max_value=48))
@_settings
def test_deposit_synchronicity_groups_always_have_at_least_two_wallets(results, gap_hours):
    for g in deposit_synchronicity(results, gap_hours=gap_hours):
        assert len(g["wallets"]) >= 2


@given(_wallet_deposit_maps(), st.floats(min_value=0.1, max_value=48))
@_settings
def test_deposit_synchronicity_span_matches_first_and_last(results, gap_hours):
    for g in deposit_synchronicity(results, gap_hours=gap_hours):
        assert g["span_seconds"] == g["last_ts"] - g["first_ts"]
        assert g["span_seconds"] >= 0


@given(_wallet_deposit_maps(), st.floats(min_value=0.1, max_value=48))
@_settings
def test_deposit_synchronicity_every_wallet_appears_at_most_once(results, gap_hours):
    seen = []
    for g in deposit_synchronicity(results, gap_hours=gap_hours):
        seen.extend(g["wallets"])
    assert len(seen) == len(set(seen))


# ==========================================================================
# assign_keys
# ==========================================================================
@st.composite
def _pool_lists(draw, max_size=8):
    n = draw(st.integers(min_value=1, max_value=max_size))
    addrs = draw(
        st.lists(
            st.integers(min_value=0, max_value=2**40 - 1).map(lambda i: "0x%040x" % i),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    denom = draw(st.sampled_from([0.1, 1.0, 10.0, 100.0]))
    asset = draw(st.sampled_from(["ETH", "AVAX"]))
    return [Pool(a, denom, asset) for a in addrs]


@given(_pool_lists())
@_settings
def test_assign_keys_produces_one_unique_key_per_pool(pools):
    assign_keys(pools)
    assert len({p.key for p in pools}) == len(pools)


@given(_pool_lists())
@_settings
def test_assign_keys_the_lowest_address_keeps_the_bare_label(pools):
    assign_keys(pools)
    lowest = min(pools, key=lambda p: p.address)
    assert lowest.key == lowest.label  # no "#N" suffix


@given(_pool_lists())
@_settings
def test_assign_keys_is_independent_of_input_order(pools):
    shuffled = list(pools)
    random.Random(0).shuffle(shuffled)  # noqa: S311 - test-only shuffling, not crypto
    keys_before = {p.address: p.key for p in assign_keys(list(pools))}
    keys_after = {p.address: p.key for p in assign_keys(list(shuffled))}
    assert keys_before == keys_after
