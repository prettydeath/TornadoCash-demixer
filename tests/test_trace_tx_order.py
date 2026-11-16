"""Order of the transfers inside one transaction (internal traceId, token logIndex)."""

import random

from tests.test_trace import A, B, C, Client, D, X, _indexed, _ttx, _tx
from tornado_demix import allocation as alloc
from tornado_demix import trace as trace_mod
from tornado_demix.trace import trace_funds

H = "0x" + "f" * 64  # the transaction holding both transfers (synthetic)
HELD = "not moved on (held or untraced)"


def _internal(frm, to, eth, block, h, trace_id=None):
    row = _tx(frm, to, eth, block, h)
    if trace_id is not None:
        row["traceId"] = trace_id
    return row


def _logged(row, log_index=None):
    if log_index is not None:
        row["logIndex"] = str(log_index)
    return row


def _run(client, start, amount, **kw):
    kw.setdefault("is_contract", lambda a: False)
    kw.setdefault("labels", {X: {"label": "outside"}})
    kw.setdefault("max_hops", 3)
    return trace_funds(client, start, amount, **kw)


def _contract_case(out_id, in_id):
    """A holds 1 older ETH; in H (a zero-value call, index 0) A pays B 1 ETH and receives
    the traced 1 ETH from C, both internal, at the given traceIds."""
    normal = [_indexed(_tx(C, A, 1, 1, "0xa1"), 0), _indexed(_tx(X, A, 0, 2, H), 0)]
    internal = [_internal(A, B, 1, 2, H, out_id), _internal(C, A, 1, 2, H, in_id)]
    return Client(normal=normal, internal=internal)


def _ends(out):
    return {(t["address"], t["reason"]): t["amount"] for t in out["terminals"]}


# --- the position helpers --------------------------------------------------------------


def test_trace_ids_parse_component_wise_and_bad_ones_are_unknown():
    assert alloc.trace_path("0") == (0,)
    assert alloc.trace_path("0_1_12") == (0, 1, 12)
    assert alloc.trace_path("call_0_1") == (0, 1)
    for bad in (None, "", "x", "0_a", "-1", "1__2"):
        assert alloc.trace_path(bad) is None, bad


def test_inside_one_transaction_only_comparable_positions_order_events():
    top, unknown_internal = (0, ()), (1, None)
    first, second, child = (1, (0,)), (1, (1,)), (1, (0, 5))
    assert alloc.sub_order(top, first) == -1 and alloc.sub_order(first, top) == 1
    assert alloc.sub_order(top, unknown_internal) == -1  # the call's value moves first
    assert alloc.sub_order(first, second) == -1 and alloc.sub_order(second, child) == 1
    assert alloc.sub_order(first, child) == -1  # a call's value moves before its sub-calls
    assert alloc.sub_order(first, unknown_internal) is None
    assert alloc.sub_order(first, first) is None
    assert alloc.sub_order((2, 3), (2, 7)) == -1 and alloc.sub_order((2, 7), (2, 3)) == 1
    assert alloc.sub_order((2, 3), (2, None)) is None
    assert alloc.sub_order(top, (2, 3)) is None and alloc.sub_order((2, 3), first) is None
    assert alloc.sub_order(None, first) is None


def test_an_equal_transaction_index_alone_no_longer_means_arrived_before():
    # (block, transaction index): the same transaction, no position inside it
    assert not alloc._known_before((5, 3), (5, 3))
    assert alloc._order_unknown((5, 3), (5, 3))
    lot, out = (5, 3, "0xh", (1, (1,))), (5, 3, "0xh", (1, (0,)))
    assert not alloc._known_before(lot, out) and not alloc._order_unknown(lot, out)
    assert alloc._known_before(out, lot) and not alloc._order_unknown(out, lot)
    # the same transaction found by hash when the index is not known
    lot, out = (5, None, "0xh", (2, 1)), (5, None, "0xh", (2, 4))
    assert alloc._known_before(lot, out)
    # different transactions keep the index rule
    assert alloc._known_before((5, 1, "0xa", None), (5, 2, "0xb", None))
    assert alloc._order_unknown((5, None, "0xa", None), (5, 2, "0xb", None))


def test_a_ledger_does_not_spend_a_lot_that_arrived_later_in_the_same_transaction():
    ledger = alloc.Ledger()
    ledger.inflow(1.0)  # older funds
    ledger.inflow(1.0)
    ledger.add_traced(1.0, "lot", at=(2, 0, H, (1, (1,))))
    row = ledger.outflow(1.0, at=(2, 0, H, (1, (0,))), ref=7)
    assert row["fifo"] == 0 and ledger.traced_left == 1.0
    assert ledger.unordered() == (0.0, [])  # known to come after: not order_unknown


# --- the replay ------------------------------------------------------------------------


def test_an_internal_outflow_before_the_traced_internal_inflow_is_no_edge():
    out = _run(_contract_case("0", "1"), A, 1, start_block=2, arrival_tx=H)
    assert out["edges"] == []
    assert _ends(out) == {(A, HELD): 1.0}


def test_an_internal_outflow_after_the_traced_internal_inflow_is_an_edge():
    out = _run(_contract_case("1", "0"), A, 1, start_block=2, arrival_tx=H)
    (edge,) = out["edges"]
    assert (edge["from"], edge["to"], edge["tx_hash"], edge["attributed"]) == (A, B, H, 1.0)
    assert not edge["history_incomplete"]


def test_trace_paths_compare_component_wise():
    out = _run(_contract_case("0_1", "0_0_5"), A, 1, start_block=2, arrival_tx=H)
    assert [e["to"] for e in out["edges"]] == [B]
    out = _run(_contract_case("0_1_3", "0_2"), A, 1, start_block=2, arrival_tx=H)
    assert out["edges"] == [] and _ends(out) == {(A, HELD): 1.0}


def test_a_missing_or_unreadable_trace_id_ends_as_order_unknown_inside_the_transaction():
    for out_id, in_id in ((None, "1"), ("0", None), ("0", "x"), (None, None)):
        out = _run(_contract_case(out_id, in_id), A, 1, start_block=2, arrival_tx=H)
        assert out["edges"] == [], (out_id, in_id)
        (end,) = out["terminals"]
        assert (end["address"], end["amount"], end["reason"]) == (A, 1, trace_mod.ORDER_UNKNOWN)
        assert end["order_note"] == trace_mod.ORDER_SAME_TX
        assert end["outflows"] == [{"to": B, "tx_hash": H, "value": 1.0, "same_tx": True}]
        total = sum(t["amount"] for t in out["terminals"])
        assert abs(total - 1) < 1e-9


def test_a_top_level_value_moves_before_every_internal_transfer_of_its_transaction():
    # A (a contract) receives the traced 1 ETH as the call's value and pays B internally
    # (no traceId needed): the inflow came first
    normal = [_indexed(_tx(X, A, 1, 2, H), 0)]
    internal = [_internal(A, B, 1, 2, H)]
    out = _run(Client(normal=normal, internal=internal), A, 1, arrival_tx=H)
    assert [(e["to"], e["attributed"]) for e in out["edges"]] == [(B, 1.0)]
    # A sends 1 ETH as the call's value and receives the traced 1 ETH back internally:
    # the outflow came first and cannot have taken it
    normal = [_indexed(_tx(C, A, 1, 1, "0xa1"), 0), _indexed(_tx(A, B, 1, 2, H), 0)]
    internal = [_internal(C, A, 1, 2, H)]
    out = _run(Client(normal=normal, internal=internal), A, 1, arrival_tx=H)
    assert out["edges"] == [] and _ends(out) == {(A, HELD): 1.0}


def test_token_transfers_inside_one_transaction_order_by_log_index():
    def case(out_log, in_log):
        tokens = [
            _logged(_indexed(_ttx(C, A, 1, 1, "0xa1"), 0), 0),
            _logged(_indexed(_ttx(A, B, 1, 2, H), 0), out_log),
            _logged(_indexed(_ttx(C, A, 1, 2, H), 0), in_log),
        ]
        token = tokens[0]["contractAddress"]
        return _run(Client(tokens=tokens), A, 1, token=token, arrival_tx=H)

    assert [e["to"] for e in case(7, 3)["edges"]] == [B]
    out = case(3, 7)
    assert out["edges"] == [] and [t["reason"] for t in out["terminals"]] == [HELD]
    out = case(None, 7)
    assert out["edges"] == []
    assert [t["reason"] for t in out["terminals"]] == [trace_mod.ORDER_UNKNOWN]


def test_a_forwarding_contract_passes_the_traced_funds_on_in_the_same_transaction():
    # X calls A (index 0); A receives 1 ETH from C (traceId 0) and forwards it to B
    # (traceId 0_1), B forwards to D (traceId 0_1_0): one transaction, two hops
    normal = [_indexed(_tx(X, A, 0, 2, H), 0)]
    internal = [
        _internal(C, A, 1, 2, H, "0"),
        _internal(A, B, 1, 2, H, "0_1"),
        _internal(B, D, 1, 2, H, "0_1_0"),
    ]
    out = _run(Client(normal=normal, internal=internal), A, 1, arrival_tx=H)
    assert [(e["from"], e["to"]) for e in out["edges"]] == [(A, B), (B, D)]
    assert [t["address"] for t in out["terminals"]] == [D]


# --- every view ------------------------------------------------------------------------


def test_order_unknown_inside_one_transaction_reaches_every_view(tmp_path):
    import json

    from tornado_demix.networks import get_network
    from tornado_demix.report_html import build_trace_report

    out = _run(_contract_case(None, "1"), A, 1, start_block=2, arrival_tx=H)
    html = build_trace_report(out, get_network("ethereum"))
    assert "order inside one transaction unknown" in html
    from tests.test_report_page1 import _page1

    p1 = _page1(html)
    assert "order inside one transaction unknown" in p1  # page 1 badge row
    data = json.loads(json.dumps(out))
    assert data["terminals"][0]["order_note"] == "order inside one transaction unknown"
    assert "inside one transaction" in trace_mod.END_STATES_NOTE


# --- random same-transaction cases: lazy == reference, conservation --------------------


def _random_same_tx_case(rng):
    """Contracts that receive and pay out inside one transaction, traceIds random,
    sometimes missing, after older balances (a history can still be overdrawn)."""
    nodes = [A, B, C, D]
    normal, internal, n = [], [], 0
    for i, a in enumerate(nodes):
        normal.append(_indexed(_tx(X, a, 5, 1, "0x" + format(0x900 + i, "064x")), i))
    arrival = None
    for block in range(2, rng.randint(3, 6) + 2):
        for idx in range(rng.randint(1, 3)):
            n += 1
            h = "0x" + format(n, "064x")
            normal.append(_indexed(_tx(X, rng.choice(nodes), 0, block, h), idx))
            flat = rng.random() < 0.3  # a Blockscout-style transaction: flat indexes
            for _ in range(rng.randint(1, 4)):
                frm = rng.choice([X] + nodes) if arrival else X
                to = rng.choice([a for a in nodes if a != frm] + ([X] if frm != X else []))
                if arrival is None:
                    to = A
                value = rng.randint(1, 2)
                if flat:
                    row = _tx(frm, to, value, block, h)
                    if rng.random() < 0.8:
                        row["index"] = str(rng.randint(0, 6))
                    internal.append(row)
                else:
                    path = "_".join(str(rng.randint(0, 2)) for _ in range(rng.randint(1, 3)))
                    tid = path if rng.random() < 0.8 else None
                    internal.append(_internal(frm, to, value, block, h, tid))
                if arrival is None:
                    arrival = h
    return arrival, Client(normal=normal, internal=internal), internal


def _funded_in_order(internal, edge, source):
    """Whether an edge funded by an inflow of its own transaction has an inflow row known
    to come before its outflow row (both traceIds, or both flat indexes, readable and the
    inflow's smaller)."""
    tx = edge["tx_hash"]
    ins = [
        r for r in internal if r["hash"] == tx and (r["from"], r["to"]) == (source, edge["from"])
    ]
    outs = [
        r
        for r in internal
        if r["hash"] == tx and (r["from"], r["to"]) == (edge["from"], edge["to"])
    ]

    def place(row):
        path = alloc.trace_path(row.get("traceId"))
        if path is not None:
            return ("path", path)
        return ("flat", (int(row["index"]),)) if row.get("index") else None

    return any(
        place(i) is not None
        and place(o) is not None
        and place(i)[0] == place(o)[0]
        and place(i)[1] < place(o)[1]
        for i in ins
        for o in outs
    )


def test_random_same_transaction_cases_conserve_and_replay_lazily_alike(monkeypatch):
    rng = random.Random(20261116)
    for _ in range(300):
        arrival, client, internal = _random_same_tx_case(rng)
        amount = float(
            sum(int(r["value"]) for r in internal if r["hash"] == arrival and r["to"] == A) / 10**18
        )
        hops = rng.choice([2, 4, 8])
        monkeypatch.setattr(trace_mod, "_LAZY", False)
        reference = _run(client, A, amount, max_hops=hops, arrival_tx=arrival)
        monkeypatch.setattr(trace_mod, "_LAZY", True)
        lazy = _run(client, A, amount, max_hops=hops, arrival_tx=arrival)
        assert lazy == reference
        ends = sum(t["amount"] for t in lazy["terminals"])
        assert abs(ends - amount) < 1e-6, (ends, amount, lazy["terminals"])
        for e in lazy["edges"]:
            assert e["lower"] <= e["attributed"] + 1e-9 <= e["upper"] + 2e-9, e
            for src in e["sources"]:
                if src["tx_hash"] == e["tx_hash"]:  # no edge before the inflow funding it
                    assert _funded_in_order(internal, e, src["from"]), (e, internal)


def test_order_unknown_inside_one_transaction_reaches_the_web_ui(monkeypatch):
    import os
    import sys

    import pytest

    pytest.importorskip("flask")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "webui"))
    import app as webapp

    from tornado_demix.networks import get_network

    out = _run(_contract_case(None, "1"), A, 1, start_block=2, arrival_tx=H)
    monkeypatch.setattr(webapp, "trace_funds", lambda *a, **kw: out)
    monkeypatch.setattr(webapp, "load_attribution", lambda name: {})
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: lambda a: None)
    params = {"amount": 1.0, "start_block": 2, "max_hops": 3, "token": None}
    view, _table, reports = webapp._run_trace(object(), A, get_network("ethereum"), params)
    assert view["terminals"][0]["order_note"] == trace_mod.ORDER_SAME_TX
    assert f"{trace_mod.ORDER_SAME_TX}: 1.0 to" in view["report_body"]
    assert '"order_note": "order inside one transaction unknown"' in reports[A + ".json"]


# --- several inflows of the arrival transaction --------------------------------------


def _two_inflows(first=1, second=1, sender=D):
    """In H (a zero-value call): C pays A ``first`` (traceId 0), A pays B 1 (traceId 1),
    ``sender`` pays A ``second`` (traceId 2)."""
    normal = [_indexed(_tx(X, A, 0, 2, H), 0)]
    internal = [
        _internal(C, A, first, 2, H, "0"),
        _internal(A, B, 1, 2, H, "1"),
        _internal(sender, A, second, 2, H, "2"),
    ]
    return Client(normal=normal, internal=internal)


def test_an_arrival_transaction_with_two_equal_inflows_gives_no_certain_edge():
    out = _run(_two_inflows(), A, 1, arrival_tx=H)
    assert out["arrival_matched"] == "ambiguous_in_tx"
    assert "which one is the traced" in trace_mod.arrival_text(out)
    for e in out["edges"]:
        assert e["lower"] == 0 and not e["status"].startswith("certain"), e
    assert [e for e in out["edges"] if e["to"] == B] == []
    (end,) = [t for t in out["terminals"] if t["reason"] == trace_mod.ORDER_UNKNOWN]
    assert end["order_note"] == trace_mod.ORDER_SAME_TX
    assert abs(sum(t["amount"] for t in out["terminals"]) - 1) < 1e-9


def test_the_traced_inflow_singled_out_by_amount_is_used():
    # the traced 2 ETH came at traceId 2, after A paid B: no edge, the 2 stay
    out = _run(_two_inflows(first=1, second=2), A, 2, arrival_tx=H)
    assert out["arrival_matched"] == "exact"
    assert out["edges"] == [] and _ends(out) == {(A, HELD): 2.0}
    # the traced 1 ETH came at traceId 0, before A paid B: a certain edge
    out = _run(_two_inflows(first=1, second=2), A, 1, arrival_tx=H)
    (edge,) = out["edges"]
    assert (edge["to"], edge["attributed"], edge["lower"]) == (B, 1.0, 1.0)


def test_a_later_hop_singles_out_its_inflow_by_sender_and_place():
    # A pays E 1 in H2; in the same transaction E also gets 1 from X first and pays B 1
    # in between: the traced 1 is A's (traceId 2), after E paid B
    E = "0x" + "e" * 40
    h2 = "0x" + "c" * 64
    normal = [_indexed(_tx(C, A, 1, 1, "0xa1"), 0), _indexed(_tx(X, E, 0, 2, h2), 0)]
    internal = [
        _internal(X, E, 1, 2, h2, "0"),
        _internal(E, B, 1, 2, h2, "1"),
        _internal(A, E, 1, 2, h2, "2"),
    ]
    out = _run(Client(normal=normal, internal=internal), A, 1, arrival_tx="0xa1")
    assert [(e["from"], e["to"]) for e in out["edges"]] == [(A, E)]
    assert _ends(out) == {(E, HELD): 1.0}


def test_the_ambiguous_arrival_reaches_every_view():
    from tests.test_report_page1 import _page1
    from tornado_demix.networks import get_network
    from tornado_demix.report_html import build_trace_report

    out = _run(_two_inflows(), A, 1, arrival_tx=H)
    p1 = _page1(build_trace_report(out, get_network("ethereum")))
    assert "holds several inflows that can carry the traced amount" in p1


# --- Blockscout: internal calls numbered by a flat trace index -------------------------


def _blockscout(frm, to, eth, block, h, index):
    """An internal row as Blockscout's account API gives it (no traceId, ``index``)."""
    row = _tx(frm, to, eth, block, h)
    row.update(transactionHash=h, index=str(index), type="call", callType="call")
    return row


def _blockscout_case(out_index, in_index):
    normal = [_indexed(_tx(C, A, 1, 1, "0xa1"), 0), _indexed(_tx(X, A, 0, 2, H), 0)]
    internal = [
        _blockscout(A, B, 1, 2, H, out_index),
        _blockscout(C, A, 1, 2, H, in_index),
    ]
    return Client(normal=normal, internal=internal)


def test_flat_trace_indexes_order_internal_transfers_without_a_trace_id():
    assert alloc.sub_order((alloc.FLAT, 1), (alloc.FLAT, 2)) == -1
    assert alloc.sub_order((alloc.TOP, ()), (alloc.FLAT, 0)) == -1
    assert alloc.sub_order((alloc.FLAT, 1), (alloc.INTERNAL, (2,))) is None
    out = _run(_blockscout_case(1, 2), A, 1, start_block=2, arrival_tx=H)
    assert out["edges"] == [] and _ends(out) == {(A, HELD): 1.0}
    out = _run(_blockscout_case(2, 1), A, 1, start_block=2, arrival_tx=H)
    assert [(e["to"], e["attributed"]) for e in out["edges"]] == [(B, 1.0)]


def test_a_flat_index_and_a_trace_id_in_one_transaction_are_not_compared():
    normal = [_indexed(_tx(C, A, 1, 1, "0xa1"), 0), _indexed(_tx(X, A, 0, 2, H), 0)]
    internal = [_internal(A, B, 1, 2, H, "5"), _blockscout(C, A, 1, 2, H, 1)]
    out = _run(Client(normal=normal, internal=internal), A, 1, start_block=2, arrival_tx=H)
    assert out["edges"] == []
    assert [t["reason"] for t in out["terminals"]] == [trace_mod.ORDER_UNKNOWN]


def test_an_unplaced_internal_outflow_next_to_an_indexed_inflow_is_order_unknown():
    # A pays B by an internal row with neither traceId nor index; the traced 1 ETH comes in
    # by a Blockscout row with an index: the order is unknown, the inflow sorts first
    normal = [_indexed(_tx(C, A, 1, 1, "0xa1"), 0), _indexed(_tx(X, A, 0, 2, H), 0)]
    internal = [_internal(A, B, 1, 2, H), _blockscout(C, A, 1, 2, H, 3)]
    out = _run(Client(normal=normal, internal=internal), A, 1, start_block=2, arrival_tx=H)
    assert out["edges"] == []
    (end,) = out["terminals"]
    assert (end["reason"], end["amount"], end["order_note"]) == (
        trace_mod.ORDER_UNKNOWN,
        1.0,
        trace_mod.ORDER_SAME_TX,
    )
    # the outflow was replayed with the traced lot present: all of it is what it can hold
    assert end["outflows"] == [{"to": B, "tx_hash": H, "value": 1.0, "same_tx": True}]
    events = trace_mod._history(
        {"incoming": trace_mod._Incoming(Client(internal=internal)), "client": Client()},
        A,
        None,
        [],
    )[0]
    assert {e["sub"] for e in events} == {(alloc.INTERNAL, None)}
