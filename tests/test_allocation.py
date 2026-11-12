"""Allocation bounds of a traced amount, checked against hand-computed cases."""

from tornado_demix.allocation import bounds


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
    # the 2 missing units are treated as other funds, not clamped away
    row = _row(res, "o")
    assert (row["lower"], row["upper"]) == (1, 1)


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
