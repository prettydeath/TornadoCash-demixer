"""Group view of ``multi``: the given wallets treated as one depositor.

Synthetic pools only. The group view merges the wallets' deposits, searches one
window per pool (first deposit to window end) and runs the existing count-match,
voucher-order and early-profile rules on the merged notes. The per-wallet results
must not change, and the exact-k detector is context with an empirical base.
"""

import json

import pytest

from tests.test_multi import ALICE, BOB, EXIT_A, POOL_1, POOL_10, T0, StubClient, network
from tornado_demix import multi
from tornado_demix.errors import ApiError
from tornado_demix.heuristics import ranked_candidates
from tornado_demix.multi import (
    EXACT_K_BASE_WINDOWS,
    EXACT_K_MAX_BASE_DAYS,
    EXACT_K_MAX_WINDOWS,
    correlate,
    group_view,
)

DAY = 86400


def _bg(prefix, n, ts, pool_hits=1, step=60):
    """``n`` background recipients with ``pool_hits`` withdrawals each, from ``ts`` on."""
    out = []
    for i in range(n):
        addr = "0x%s%036x" % (prefix, i)
        out += [(addr, ts + i * step + j) for j in range(pool_hits)]
    return out


def _split_deposits():
    """ALICE deposits 3 x 1 ETH, BOB 2 x 1 ETH an hour later: one 5-note group voucher."""
    return {
        ALICE: [(POOL_1, T0), (POOL_1, T0 + 60), (POOL_1, T0 + 120)],
        BOB: [(POOL_1, T0 + 3600), (POOL_1, T0 + 3660)],
    }


def _split_withdrawals():
    return {
        POOL_1: [(EXIT_A, T0 + DAY * i) for i in range(1, 6)]  # the merged 5 notes
        + _bg("b0", 6, T0 + DAY * 6)  # one withdrawal each
        + _bg("c0", 2, T0 + DAY * 7, pool_hits=3)  # three each: ALICE-sized
        + _bg("d0", 2, T0 + DAY * 8, pool_hits=2)  # two each: BOB-sized
    }


def _correlate(deposits, withdrawals, **kw):
    client = StubClient(deposits, withdrawals)
    return correlate(client, [ALICE, BOB], window_days=30, network=network(), **kw)


def _strip_volatile(results):
    """Per-wallet results as comparable JSON (sets and Counters normalised)."""
    return json.loads(json.dumps(results, sort_keys=True, default=sorted))


def test_the_recipient_of_the_merged_notes_ranks_first_in_the_group_view():
    corr = _correlate(_split_deposits(), _split_withdrawals(), group=True)
    group = corr["group"]
    ranked = ranked_candidates(group)
    assert ranked and ranked[0]["address"] == EXIT_A
    assert "count_match" in ranked[0]["signals"]
    assert group["profile"] == {"1 ETH": 5}
    assert [v["count"] for v in group["vouchers"]] == [5]
    # per-wallet: EXIT_A received 5, neither wallet's voucher size, so it is not ranked
    for wallet in (ALICE, BOB):
        assert EXIT_A not in {r["address"] for r in ranked_candidates(corr["results"][wallet])}


def test_the_group_view_does_not_change_the_per_wallet_output():
    plain = _correlate(_split_deposits(), _split_withdrawals())
    grouped = _correlate(_split_deposits(), _split_withdrawals(), group=True)
    assert "group" not in plain
    assert _strip_volatile(plain["results"]) == _strip_volatile(grouped["results"])
    for key in ("strong", "soft", "cross_profile", "operator_clusters", "consolidator_grades"):
        assert _strip_volatile(plain[key]) == _strip_volatile(grouped[key])


def test_the_group_window_runs_from_the_first_deposit_to_the_window_end():
    corr = _correlate(_split_deposits(), _split_withdrawals(), group=True)
    windows = corr["group"]["denoms"]["1 ETH"]["windows"]
    assert len(windows) == 1
    assert windows[0]["first_ts"] == T0
    assert windows[0]["end_ts"] == T0 + 3660 + 30 * DAY


def test_the_group_view_carries_the_conditional_caveat_and_the_wallets():
    corr = _correlate(_split_deposits(), _split_withdrawals(), group=True)
    group = corr["group"]
    assert group["wallets"] == sorted([ALICE, BOB])
    assert "assertion" in group["caveat"] and "one actor" in group["caveat"]
    assert group["params"]["group"] is True
    assert group["params"]["voucher_order"] is True
    # every merged deposit names the wallet it came from
    assert {d["wallet"] for d in group["deposits"]} == {ALICE, BOB}


def test_the_merged_deposits_are_copies_not_the_per_wallet_records():
    corr = _correlate(_split_deposits(), _split_withdrawals(), group=True)
    assert all("wallet" not in d for d in corr["results"][ALICE]["deposits"])


def test_early_profile_on_the_merged_notes_raises_the_group_band_only():
    """ALICE 6 x 1 ETH, BOB 4 x 10 ETH: each wallet has one pool (no profile), the
    group has a 10-note two-pool profile that one address received within 72 h."""
    deposits = {
        ALICE: [(POOL_1, T0 + 60 * i) for i in range(6)],
        BOB: [(POOL_10, T0 + 600 + 60 * i) for i in range(4)],
    }
    withdrawals = {
        POOL_1: [(EXIT_A, T0 + 3600 * (i + 1)) for i in range(6)] + _bg("b1", 8, T0 + DAY * 5),
        POOL_10: [(EXIT_A, T0 + 3600 * (i + 7)) for i in range(4)] + _bg("b2", 8, T0 + DAY * 5),
    }
    corr = _correlate(deposits, withdrawals, group=True)
    group_rows = [r for r in ranked_candidates(corr["group"]) if r["address"] == EXIT_A]
    assert group_rows and all("early_profile" in r["signals"] for r in group_rows)
    assert group_rows[0]["band"] == "moderate"
    for wallet in (ALICE, BOB):
        for res in corr["results"][wallet]["denoms"].values():
            assert "early_profile" not in res["signals"].get(EXIT_A, [])


def test_a_group_with_no_deposits_is_empty_not_an_error():
    corr = _correlate({}, {}, group=True)
    assert corr["group"]["denoms"] == {}
    assert corr["group"]["deposits"] == []


def test_a_failed_group_fetch_is_recorded_and_per_wallet_results_survive(monkeypatch):
    class FailingGroupClient(StubClient):
        """Answers the per-wallet runs, then refuses every log read."""

        fail = False

        def get_logs(self, *a, **kw):
            if self.fail:
                raise ApiError("NOTOK: rate limit")
            return super().get_logs(*a, **kw)

    from tornado_demix import multi

    real = multi.group_view

    def flip_then_group(cl, *a, **kw):
        cl.fail = True
        return real(cl, *a, **kw)

    monkeypatch.setattr(multi, "group_view", flip_then_group)
    client = FailingGroupClient(_split_deposits(), _split_withdrawals())
    corr = correlate(client, [ALICE, BOB], window_days=30, network=network(), group=True)
    assert corr["group"]["denoms"] == {}
    assert corr["group"]["unresolved"] and corr["group"]["unresolved"][0]["pool_key"] == "1 ETH"
    assert any(e.get("wallet") == "group" for e in corr["errors"])
    assert corr["results"][ALICE]["denoms"]


# ---- exact-k detector: empirical base from comparable earlier windows ------------


class CountingClient(StubClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.log_calls = 0

    def get_logs(self, *a, **kw):
        self.log_calls += 1
        return super().get_logs(*a, **kw)


def _group_only(withdrawals, deposits=None, **kw):
    """Run only the group view over the stub (the per-wallet runs are not needed)."""
    deposits = deposits or _split_deposits()
    client = CountingClient(deposits, withdrawals)
    from tornado_demix.demix import detect_deposits

    results = {
        w: {"wallet": w, "deposits": detect_deposits(client, w, network=network())}
        for w in (ALICE, BOB)
    }
    group = group_view(
        client,
        results,
        window_days=30,
        network=network(),
        **kw,
    )
    return group, client


def test_exact_k_reports_observed_base_mean_and_empirical_tail():
    # group window: T0 .. T0 + 3660 + 30 days; length L
    length = 3660 + 30 * DAY
    base = []
    # window 1 (just before T0): one recipient with exactly 5
    mid1 = T0 - length + length // 2
    base += [("0x" + "51" * 20, mid1 + i) for i in range(5)] + _bg("e1", 3, mid1 + 100)
    # window 2: two recipients with exactly 5
    mid2 = T0 - 2 * length + length // 2
    base += [("0x" + "52" * 20, mid2 + i) for i in range(5)]
    base += [("0x" + "53" * 20, mid2 + 10 + i) for i in range(5)]
    # windows 3 and 4: activity, but nobody with exactly 5
    for k in (3, 4):
        base += _bg("f%d" % k, 4, T0 - k * length + length // 2)
    withdrawals = {POOL_1: _split_withdrawals()[POOL_1] + base}
    group, client = _group_only(withdrawals, exact_k=True, exact_k_windows=4)
    (row,) = group["exact_k"]
    assert row["pool_key"] == "1 ETH" and row["k"] == 5
    assert row["observed"] == 1
    assert row["base_counts"] == [1, 2, 0, 0]
    assert row["base_mean"] == pytest.approx(0.75)
    # two of four base windows hold >= 1 such recipient: smoothed (2 + 1) / (4 + 1)
    assert row["n_ge_observed"] == 2
    assert row["p_ge_observed"] == pytest.approx(0.6)
    assert row["window_hours"] == pytest.approx(length / 3600, abs=0.01)
    assert row["context"] is True
    # one read for the group window, one per base window
    assert client.log_calls == 1 + 4


def test_exact_k_never_touches_the_scores():
    withdrawals = _split_withdrawals()
    plain, _ = _group_only(withdrawals)
    with_k, _ = _group_only(withdrawals, exact_k=True, exact_k_windows=2)
    assert plain["exact_k"] == []
    a, b = plain["denoms"]["1 ETH"], with_k["denoms"]["1 ETH"]
    assert a["signals"] == b["signals"] and a["confidence"] == b["confidence"]


def test_exact_k_skips_windows_without_any_withdrawal():
    """A window before the pool existed holds nothing and is not a comparable base."""
    group, _ = _group_only(_split_withdrawals(), exact_k=True, exact_k_windows=3)
    (row,) = group["exact_k"]
    assert row["base_windows"] == 0 and row["empty_windows"] == 3
    assert row["base_mean"] is None and row["p_ge_observed"] is None


def test_exact_k_window_count_is_bounded():
    # a 25-hour group window (exit window 24 h): the count cap binds, not the span cap
    group, client = _group_only(
        _split_withdrawals(), exact_k=True, exact_k_windows=500, exit_window_hours=24
    )
    row = group["exact_k"][0]
    assert row["requested_windows"] == EXACT_K_MAX_WINDOWS and row["capped_windows"] == 0
    assert client.log_calls == 1 + EXACT_K_MAX_WINDOWS
    assert 1 <= EXACT_K_BASE_WINDOWS <= EXACT_K_MAX_WINDOWS


def test_exact_k_base_windows_span_at_most_a_year():
    # a ~30-day group window: at most 12 base windows fit in 365 days
    group, client = _group_only(_split_withdrawals(), exact_k=True, exact_k_windows=20)
    row = group["exact_k"][0]
    assert row["max_base_days"] == EXACT_K_MAX_BASE_DAYS == 365
    assert row["capped_windows"] == 20 - 12
    assert client.log_calls == 1 + 12


def test_a_group_window_longer_than_a_year_gets_no_base_and_a_notice():
    deposits = {ALICE: [(POOL_1, T0), (POOL_1, T0 + 60)], BOB: [(POOL_1, T0 + 400 * DAY)]}
    group, client = _group_only(_split_withdrawals(), deposits=deposits, exact_k=True)
    (row,) = group["exact_k"]
    assert row["base_windows"] == 0 and row["capped_windows"] == EXACT_K_BASE_WINDOWS
    assert row["p_ge_observed"] is None
    assert client.log_calls == 1
    assert group["notices"] and "430 days" in group["notices"][0]


def test_a_group_window_of_90_days_or_less_has_no_notice():
    group, _ = _group_only(_split_withdrawals())
    assert group["notices"] == []
    deposits = {ALICE: [(POOL_1, T0)], BOB: [(POOL_1, T0 + 61 * DAY)]}  # 61 + 30 = 91 days
    group, _ = _group_only(_split_withdrawals(), deposits=deposits)
    assert len(group["notices"]) == 1 and "more than 90" in group["notices"][0]


def test_exact_k_observed_counts_only_withdrawals_timed_inside_the_group_window():
    """A withdrawal in the group window's first block but timed before it is not observed."""
    early = [("0x" + "66" * 20, T0 - 7 + i % 2) for i in range(5)]  # same block as T0, before it
    withdrawals = {POOL_1: _split_withdrawals()[POOL_1] + early}
    group, _ = _group_only(withdrawals, exact_k=True, exact_k_windows=1)
    assert group["exact_k"][0]["observed"] == 1  # EXIT_A only


# ---- the direct link inside a group ----------------------------------------------


def test_a_group_member_receiving_a_withdrawal_is_a_linked_candidate():
    """A withdrawal back to one of the merged wallets, early: a direct link to the actor."""
    withdrawals = {POOL_1: _split_withdrawals()[POOL_1] + [(BOB, T0 + DAY)]}
    corr = _correlate(_split_deposits(), withdrawals, group=True)
    rows = [r for r in ranked_candidates(corr["group"]) if r["address"] == BOB]
    assert rows and "linked" in rows[0]["signals"] and rows[0]["band"] == "moderate"


def test_a_transfer_between_members_makes_no_third_party_a_lead():
    """ALICE pays BOB directly: only the members become counterparties, so no other
    recipient of the group window earns a direct link."""

    class TransferClient(StubClient):
        def outgoing_txs(self, wallet):
            rows = super().outgoing_txs(wallet)
            if wallet.lower() in (ALICE, BOB):
                rows.append(
                    {
                        "from": ALICE,
                        "to": BOB,
                        "value": str(10**17),
                        "timeStamp": str(T0 - DAY),
                        "blockNumber": str((T0 - DAY) // 12),
                        "hash": "0xab01",
                        "gasPrice": "1",
                        "isError": "0",
                    }
                )
            return rows

    client = TransferClient(_split_deposits(), _split_withdrawals())
    corr = correlate(client, [ALICE, BOB], window_days=30, network=network(), group=True)
    assert BOB in corr["results"][ALICE]["counterparty_addresses"]
    linked = [
        r["address"]
        for r in ranked_candidates(corr["group"])
        if {"linked", "linked_late"} & set(r["signals"])
    ]
    assert linked == []


def test_exact_k_skips_single_note_pools():
    deposits = {ALICE: [(POOL_1, T0)], BOB: []}
    group, client = _group_only(_split_withdrawals(), deposits=deposits, exact_k=True)
    assert group["exact_k"] == []
    assert client.log_calls == 1


def test_exact_k_does_not_count_a_boundary_block_withdrawal_twice():
    """The base window ending just before the group window shares its boundary block;
    a group-window withdrawal read with that block is not counted in the base."""
    length = 3660 + 30 * DAY
    inside = [("0x" + "77" * 20, T0 + 1 + i % 3) for i in range(5)]  # boundary block, after T0
    activity = _bg("e9", 3, T0 - length // 2)
    withdrawals = {POOL_1: _split_withdrawals()[POOL_1] + inside + activity}
    group, _ = _group_only(withdrawals, exact_k=True, exact_k_windows=1)
    (row,) = group["exact_k"]
    assert row["observed"] == 2  # EXIT_A and the boundary recipient
    assert row["base_counts"] == [0]


def test_group_view_records_its_own_cut_off_lists(monkeypatch):
    client = StubClient(_split_deposits(), _split_withdrawals())
    corr = correlate(client, [ALICE, BOB], window_days=30, network=network())
    note = {"what": "tokentx", "address": ALICE, "reason": "row_cap", "limit": 1000}
    # a list cut off while the group step reads: the group reports it in its own record
    monkeypatch.setattr(multi, "mark", lambda c: 0)
    client.truncated = [dict(note)]
    out = group_view(client, corr["results"], window_days=30, network=network())
    assert out["truncated"] == [note]
