"""Allocation bounds of a traced amount, checked against hand-computed cases."""

import pytest

from tornado_demix.allocation import Ledger, bounds, rounded_bounds


def _in(h, block, value, order=None):
    return {"kind": "in", "hash": h, "block": block, "order": order, "value": value, "from": "0xa"}


def _out(h, block, value, order=None):
    return {"kind": "out", "hash": h, "block": block, "order": order, "value": value, "to": "0xb"}


def _row(res, h):
    return next(r for r in res["outflows"] if r["hash"] == h)


def test_a_single_inflow_and_outflow_are_certain():
    res = bounds([_in("t", 1, 10), _out("o", 2, 4)], "t")
    assert (_row(res, "o")["lower"], _row(res, "o")["upper"], _row(res, "o")["fifo"]) == (4, 4, 4)
    assert res["history_incomplete"] is False and res["traced_found"] is True


def test_a_prior_balance_makes_the_lower_bound_zero():
    # balance 5 + traced 3 = 8; an outflow of 2 can be entirely the 5 of other funds
    res = bounds([_in("p", 1, 5), _in("t", 2, 3), _out("o", 3, 2)], "t")
    row = _row(res, "o")
    assert (row["lower"], row["upper"], row["fifo"]) == (0, 2, 2)


def test_lower_bound_is_positive_only_for_the_second_outflow():
    # prior 2, traced 3. First out 2: other can cover it (lower 0, upper 2).
    # Afterwards the traced pool is in [1, 3] and the balance is 3, so the other
    # pool is at most 2 and an outflow of 3 holds at least 1 of the traced funds.
    res = bounds([_in("p", 1, 2), _in("t", 2, 3), _out("o1", 3, 2), _out("o2", 4, 3)], "t")
    first, second = _row(res, "o1"), _row(res, "o2")
    assert (first["lower"], first["upper"], first["fifo"]) == (0, 2, 2)
    assert (second["lower"], second["upper"], second["fifo"]) == (1, 3, 1)


def test_fifo_always_lies_inside_the_bounds():
    events = [_in("p", 1, 2), _in("t", 2, 3), _out("o1", 3, 2), _out("o2", 4, 3)]
    for row in bounds(events, "t")["outflows"]:
        assert row["lower"] <= row["fifo"] <= row["upper"]


def test_an_outflow_larger_than_everything_known_marks_the_history_incomplete():
    res = bounds([_in("t", 1, 1), _out("o", 2, 3)], "t")
    assert res["history_incomplete"] is True
    # the missing units are unknown other funds: none of the outflow is certain
    row = _row(res, "o")
    assert (row["lower"], row["upper"], row["fifo"]) == (0, 1, 1)
    assert row["incomplete"] is True


def test_only_part_of_the_arriving_inflow_is_traced():
    res = bounds([_in("t", 1, 10), _out("o", 2, 5)], "t", traced_amount=4)
    row = _row(res, "o")
    assert (row["lower"], row["upper"], row["fifo"]) == (0, 4, 4)


def test_outflows_before_the_arrival_spend_other_funds_and_are_not_reported():
    events = [_in("p", 1, 5), _out("early", 2, 2), _in("t", 3, 3), _out("o", 4, 4)]
    res = bounds(events, "t")
    assert [r["hash"] for r in res["outflows"]] == ["o"]
    row = res["outflows"][0]
    assert (row["lower"], row["upper"]) == (1, 3)  # balance 3 + 3, other at most 3


def test_a_known_transaction_index_orders_events_inside_a_block():
    funded = [_in("p", 5, 2, order=0), _out("o", 5, 2, order=1), _in("t", 5, 2, order=2)]
    res = bounds(funded, "t")
    assert res["history_incomplete"] is False and res["outflows"] == []
    unfunded = [_in("p", 5, 1, order=0), _out("o", 5, 2, order=1), _in("t", 5, 2, order=2)]
    assert bounds(unfunded, "t")["history_incomplete"] is True


def test_without_an_index_an_inflow_counts_before_an_outflow_of_the_same_block():
    res = bounds([_out("o", 5, 2), _in("t", 5, 2)], "t")
    row = _row(res, "o")
    assert res["history_incomplete"] is False
    assert (row["lower"], row["upper"]) == (2, 2)  # nothing else was there: all traced


def test_missing_traced_inflow_is_reported_and_placed_at_the_start_block():
    res = bounds([_in("p", 1, 5), _out("o", 9, 3)], None, traced_amount=2, start_block=5)
    assert res["traced_found"] is False
    assert (_row(res, "o")["lower"], _row(res, "o")["upper"]) == (0, 2)


def test_zero_value_events_are_ignored():
    res = bounds([_in("t", 1, 2), _out("z", 2, 0), _out("o", 3, 1)], "t")
    assert [r["hash"] for r in res["outflows"]] == ["o"]


def test_an_unseen_inflow_before_an_overdraft_leaves_nothing_certain():
    # traced 1 in; an unseen other inflow of 10; out 5: all of it may be the unseen funds
    res = bounds([_in("t", 1, 1), _out("o", 3, 5)], "t", traced_amount=1)
    row = _row(res, "o")
    assert res["history_incomplete"] is True
    assert (row["lower"], row["upper"]) == (0, 1)


def test_after_an_overdraft_lower_stays_zero_and_upper_is_not_capped_by_the_balance():
    # in 1 traced, out 1 (balance would cap the traced pool at 0), out 1 overdraws
    events = [_in("t", 1, 1), _out("o1", 2, 1), _out("o2", 3, 1), _out("o3", 4, 1)]
    res = bounds(events, "t")
    o2, o3 = _row(res, "o2"), _row(res, "o3")
    assert res["history_incomplete"] is True
    # unseen funds may have paid o1, so all traced funds may still be there
    assert (o2["lower"], o2["upper"]) == (0, 1) and o2["incomplete"] is True
    assert (o3["lower"], o3["upper"]) == (0, 1)


def test_a_ledger_merges_several_traced_inflows():
    ledger = Ledger()
    ledger.inflow(2, traced=2, tag="b")
    ledger.inflow(3)  # other funds
    ledger.inflow(2, traced=2, tag="c")
    first = ledger.outflow(3)
    # FIFO takes the first traced lot, then one unit of the second
    assert first["fifo"] == 3 and first["taken"] == [("b", 2), ("c", 1)]
    assert (first["lower"], first["upper"]) == (0, 3)
    second = ledger.outflow(4)
    assert second["fifo"] == 1 and second["taken"] == [("c", 1)]
    # balance 4, traced in [1, 4]: other at most 3
    assert (second["lower"], second["upper"]) == (1, 4)


def test_an_incomplete_ledger_never_reports_a_positive_lower_bound():
    ledger = Ledger(complete=False)
    ledger.inflow(5, traced=5)
    row = ledger.outflow(5)
    assert (row["lower"], row["upper"], row["fifo"]) == (0, 5, 5)
    assert row["incomplete"] is True


def test_rounding_never_puts_the_lower_bound_above_the_upper():
    assert rounded_bounds(3.4302085, 3.43020849999) == (3.430208, 3.430209)
    assert rounded_bounds(3.0, 3.0) == (3.0, 3.0)
    assert rounded_bounds(1.0000004, 1.0000001) == (1.0, 1.000001)
    assert rounded_bounds(0.1 + 0.2, 0.3) == (0.3, 0.3)


def test_rounding_property():
    hypothesis = pytest.importorskip("hypothesis")
    st = hypothesis.strategies

    @hypothesis.settings(deadline=None, max_examples=300)
    @hypothesis.given(st.floats(0, 1e6, allow_nan=False), st.floats(0, 1e-6), st.floats(-1e-9, 1e6))
    def check(lower, noise, width):
        upper = lower + max(width, 0.0) - noise * (width < 0)
        lo, hi = rounded_bounds(lower, upper)
        assert lo <= hi
        assert lo <= max(lower, 0.0) + 1e-9 * max(1.0, lower)
        assert hi >= upper - 1e-9 * max(1.0, upper)

    check()


# --- brute force: the bounds against every feasible allocation ---------------
#
# Truth model: integer coins, each traced or other. An outflow o takes x traced
# coins with 0 <= x <= T and o - x <= O; every sequence of such choices is one
# ordering convention. Hidden events are in the truth but not shown to the ledger.


def _truth(seq):
    """{index of a visible outflow: (min x, max x)} over all feasible paths."""
    res = {}

    def walk(i, traced, other, chosen):
        if i == len(seq):
            for k, x in chosen:
                lo, hi = res.get(k, (10**9, -1))
                res[k] = (min(lo, x), max(hi, x))
            return
        kind, value, share, hidden = seq[i]
        if kind == "in":
            walk(i + 1, traced + share, other + value - share, chosen)
            return
        lo, hi = max(0, value - other), min(value, traced)
        for x in range(lo, hi + 1):
            mark = [] if hidden else [(i, x)]
            walk(i + 1, traced - x, other - (value - x), chosen + mark)

    walk(0, 0, 0, [])
    return res


def _feasible(seq):
    balance = 0
    for kind, value, _share, _hidden in seq:
        balance += value if kind == "in" else -value
        if balance < 0:
            return False
    return True


def _ledger_rows(seq):
    ledger, rows = Ledger(), {}
    for i, (kind, value, share, hidden) in enumerate(seq):
        if hidden:
            continue
        if kind == "in":
            ledger.inflow(float(value), traced=float(share))
        else:
            rows[i] = ledger.outflow(float(value))
    return rows


def _sequences(length):
    steps = [("out", v, 0, False) for v in (1, 2, 3)]
    steps += [("in", v, 0, False) for v in (1, 2)]
    steps += [("in", v, a, False) for v in (1, 2) for a in range(1, v + 1)]
    seqs = [[]]
    for _ in range(length):
        seqs = [s + [step] for s in seqs for step in steps]
    return seqs


def test_bounds_are_sound_and_tight_on_every_short_complete_history():
    checked = 0
    for length in range(1, 5):
        for seq in _sequences(length):
            if not _feasible(seq):
                continue
            rows = _ledger_rows(seq)
            for i, (lo, hi) in _truth(seq).items():
                row = rows[i]
                assert row["incomplete"] is False
                assert abs(row["lower"] - lo) < 1e-9 and abs(row["upper"] - hi) < 1e-9, (seq, i)
                assert row["lower"] - 1e-9 <= row["fifo"] <= row["upper"] + 1e-9
                checked += 1
    assert checked > 1000


def test_bounds_stay_sound_after_an_overdraft_from_unseen_inflows():
    hypothesis = pytest.importorskip("hypothesis")
    st = hypothesis.strategies
    step = st.one_of(
        st.tuples(st.just("out"), st.integers(1, 4), st.just(0), st.just(False)),
        st.tuples(st.just("in"), st.integers(1, 4), st.just(0), st.booleans()),
        st.integers(1, 3).flatmap(
            lambda v: st.tuples(st.just("in"), st.just(v), st.integers(1, v), st.just(False))
        ),
    )

    @hypothesis.settings(deadline=None, max_examples=500)
    @hypothesis.given(st.lists(step, min_size=1, max_size=7))
    def check(seq):
        hypothesis.assume(_feasible(seq))
        rows = _ledger_rows(seq)
        for i, (lo, hi) in _truth(seq).items():
            row = rows[i]
            if row["incomplete"]:  # from the overdraft onwards the bounds must hold
                assert row["lower"] <= lo + 1e-9, (seq, i, row)
                assert row["upper"] >= hi - 1e-9, (seq, i, row)

    check()


def test_bounds_widens_the_rows_before_an_overdraft_too():
    # o1 alone looks certain; the later overdraft shows the history misses inflows,
    # which may have paid o1 instead
    events = [_in("t", 1, 2), _out("o1", 2, 2), _out("o2", 3, 3)]
    res = bounds(events, "t")
    o1 = _row(res, "o1")
    assert res["history_incomplete"] is True
    assert (o1["lower"], o1["upper"], o1["fifo"]) == (0, 2, 2)


def test_a_traced_inflow_can_carry_an_interval_instead_of_an_exact_amount():
    # upstream: FIFO 1, but anything in [0, 1] may be the traced funds
    ledger = Ledger()
    ledger.inflow(5)
    ledger.add_traced(1, "b", lo=0, hi=1)
    row = ledger.outflow(5)
    assert (row["lower"], row["upper"], row["fifo"]) == (0, 1, 1)
    assert ledger.traced_total == 1


def test_crossed_bounds_mark_the_ledger_incomplete_instead_of_clamping():
    ledger = Ledger()
    ledger.inflow(1)
    ledger.add_traced(3, "x")  # more traced than the balance: order or data is off
    row = ledger.outflow(1)
    assert row["incomplete"] is True and row["lower"] <= row["upper"]
    assert row["lower"] <= row["fifo"] <= row["upper"]


def test_fifo_skips_a_lot_whose_order_in_the_block_is_unknown():
    ledger = Ledger()
    ledger.inflow(1)
    ledger.add_traced(1, "early", at=(1, 0))
    ledger.inflow(1)
    ledger.add_traced(1, "same_block_unindexed", at=(5, None))
    row = ledger.outflow(2, at=(5, 3))
    assert row["taken"] == [("early", 1)] and row["fifo"] == 1
    later = ledger.outflow(0.5, at=(6, 0))
    assert later["taken"] == [("same_block_unindexed", 0.5)]


def test_rounding_keeps_large_amounts_on_their_own_grid_point():
    assert rounded_bounds(3261.597488, 3261.597488) == (3261.597488, 3261.597488)
    assert rounded_bounds(123456789.123456, 123456789.123456) == (123456789.123456,) * 2
