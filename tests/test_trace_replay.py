"""Trace replay internals: lazy replay, unordered outflows, possible rows, claim order."""

import random

from tests.test_trace import (
    A,
    B,
    C,
    Client,
    D,
    X,
    _indexed,
    _random_internal_case,
    _sum_per_tx,
    _true_range,
    _tx,
)
from tornado_demix import trace as trace_mod
from tornado_demix.trace import trace_funds


def _run(client, start, amount, **kw):
    kw.setdefault("is_contract", lambda a: False)
    kw.setdefault("labels", {X: {"label": "outside"}})
    return trace_funds(client, start, amount, **kw)


class _Truncated(Client):
    """A client that reports some address lists as cut off."""

    def __init__(self, cut, **kw):
        super().__init__(**kw)
        self.truncated = [{"address": a, "action": "txlist"} for a in cut]


def _random_client(rng):
    txs, arrival, normal, internal = _random_internal_case(rng)
    cut = [a for a in (A, B, C, D) if rng.random() < 0.25]
    return txs, arrival, _Truncated(cut, normal=normal, internal=internal)


def test_the_lazy_replay_gives_the_same_result_as_queueing_every_event(monkeypatch):
    rng = random.Random(20261114)
    for _ in range(300):
        txs, arrival, client = _random_client(rng)
        start = float(next(t["v"] for t in txs if t["hash"] == arrival))
        hops = rng.choice([2, 3, 6])
        monkeypatch.setattr(trace_mod, "_LAZY", False)
        reference = _run(client, A, start, max_hops=hops, arrival_tx=arrival)
        monkeypatch.setattr(trace_mod, "_LAZY", True)
        lazy = _run(client, A, start, max_hops=hops, arrival_tx=arrival)
        assert lazy == reference, txs


def _chain(n_addr, noise):
    """A traced 5 ETH hops along ``n_addr`` addresses early on; each address then has
    ``noise`` unrelated in/out transfers of 1 ETH."""
    addrs = ["0x" + format(i + 1, "040x") for i in range(n_addr)]
    rows, n = [], 0

    def add(frm, to, value, block, index):
        nonlocal n
        n += 1
        rows.append(_indexed(_tx(frm, to, value, block, "0x" + format(n, "064x")), index))

    add(X, addrs[0], 5, 1, 0)
    for i in range(n_addr - 1):
        add(addrs[i], addrs[i + 1], 5, 2 + i, 2)
    block = n_addr + 2
    for a in addrs:
        for _ in range(noise // 2):
            block += 1
            add(X, a, 1, block, 0)
            add(a, X, 1, block, 1)
    return addrs, rows


def test_a_replay_queues_only_the_events_of_addresses_holding_traced_funds(monkeypatch):
    addrs, rows = _chain(8, 400)
    counted = {"queued": 0}
    real = trace_mod._Replay.event

    def event(self, key, i):
        counted["queued"] += 1
        return real(self, key, i)

    monkeypatch.setattr(trace_mod._Replay, "event", event)
    out = _run(Client(normal=rows), addrs[0], 5, max_hops=10, max_nodes=8)
    assert sum(1 for e in out["edges"] if e["value"] == 5) == 7  # the last one spends it
    # every replay used to queue all 8 x 401 events; now only the traced hops are
    assert counted["queued"] < 200, counted


def test_invariants_hold_with_truncated_histories_and_unordered_rows():
    rng = random.Random(7)
    for _ in range(300):
        txs, arrival, client = _random_client(rng)
        start = next(t["v"] for t in txs if t["hash"] == arrival)
        out = _run(client, A, float(start), max_hops=60, arrival_tx=arrival)
        by_hash = {t["hash"]: t for t in txs}
        for tx, total in _sum_per_tx(out["edges"]).items():
            assert total <= by_hash[tx]["v"] + 1e-6
        for e in out["edges"]:
            me = by_hash[e["tx_hash"]]
            assert e["lower"] <= e["attributed"] <= e["upper"], e
            for s in e["sources"]:
                src = by_hash[s["tx_hash"]]
                assert (src["block"], src["idx"]) < (me["block"], me["idx"]), (s, e, txs)
        # every traced unit ends exactly once (no swap or bridge here)
        ends = sum(t["amount"] for t in out["terminals"])
        assert abs(ends - start) < 1e-5, (ends, start, out["terminals"], txs)


def test_bounds_stay_sound_against_the_true_order():
    rng = random.Random(11)
    for _ in range(300):
        txs, arrival, _normal, _internal = _random_internal_case(rng)
        client = Client(normal=_normal, internal=_internal)
        start = next(t["v"] for t in txs if t["hash"] == arrival)
        out = _run(client, A, float(start), max_hops=60, arrival_tx=arrival)
        traced_in = {A: {arrival: start}}
        for e in out["edges"]:
            got = traced_in.setdefault(e["to"], {})
            got[e["tx_hash"]] = got.get(e["tx_hash"], 0) + round(e["attributed"])
        for e in out["edges"]:
            truth = _true_range(txs, e["from"], traced_in[e["from"]], e["tx_hash"])
            assert truth is not None, (e, txs)
            assert e["lower"] <= truth[0] + 1e-9 and e["upper"] >= truth[1] - 1e-9, (e, truth)
            if e["status"].startswith("certain"):
                assert truth[0] > 0, (e, truth, txs)


# --- outflows of unknown order and possible rows on an incomplete history -------------


def test_an_outflow_of_unknown_order_next_to_the_arrival_ends_as_order_unknown():
    # the traced 2 arrive at index 3 of block 5; A pays B 2 by an internal transfer in
    # the same block whose index is not known: it may have come before the arrival
    normal = [_indexed(_tx(X, A, 2, 5, "0x1"), 3)]
    internal = [_tx(A, B, 2, 5, "0x2")]
    out = _run(Client(normal=normal, internal=internal), A, 2, max_hops=3, arrival_tx="0x1")
    assert out["edges"] == []
    (end,) = out["terminals"]
    assert (end["address"], end["amount"], end["reason"]) == (A, 2, trace_mod.ORDER_UNKNOWN)
    assert end["outflows"] == [{"to": B, "tx_hash": "0x2", "value": 2.0}]
    (row,) = out["possible"]
    assert (row["from"], row["to"], row["tx_hash"]) == (A, B, "0x2")
    assert (row["attributed"], row["lower"], row["upper"]) == (0, 0, 2)
    assert row["status"] == "possible (history incomplete)"


def test_only_what_an_unordered_outflow_can_hold_ends_as_order_unknown():
    normal = [_indexed(_tx(X, A, 3, 5, "0x1"), 3)]
    internal = [_tx(A, B, 1, 5, "0x2")]
    out = _run(Client(normal=normal, internal=internal), A, 3, max_hops=3, arrival_tx="0x1")
    ends = {t["reason"]: t["amount"] for t in out["terminals"]}
    assert ends == {trace_mod.ORDER_UNKNOWN: 1, "not moved on (held or untraced)": 2}


def test_an_outflow_with_no_fifo_share_on_an_incomplete_history_is_a_possible_row():
    normal = [
        _indexed(_tx(X, A, 1, 1, "0x1"), 0),
        _indexed(_tx(A, B, 1, 2, "0x2"), 0),
        _indexed(_tx(A, C, 3, 3, "0x3"), 0),  # more than A holds: history incomplete
    ]
    out = _run(Client(normal=normal), A, 1, max_hops=1, arrival_tx="0x1")
    assert [e["to"] for e in out["edges"]] == [B]
    (row,) = out["possible"]
    assert (row["from"], row["to"], row["hop"], row["value"]) == (A, C, 1, 3)
    assert (row["lower"], row["upper"]) == (0, 1)
    assert row["kind"] == "possible" and row["history_incomplete"]
    assert "possible_more" not in out
    assert abs(sum(t["amount"] for t in out["terminals"]) - 1) < 1e-9


def test_a_complete_history_has_no_possible_rows():
    normal = [_indexed(_tx(X, A, 1, 1, "0x1"), 0), _indexed(_tx(A, B, 1, 2, "0x2"), 0)]
    out = _run(Client(normal=normal), A, 1, max_hops=1, arrival_tx="0x1")
    assert out["possible"] == []


def test_possible_rows_are_capped_and_the_rest_summarised():
    normal = [_indexed(_tx(X, A, 1, 1, "0x1"), 0)]
    for i in range(trace_mod.POSSIBLE_ROWS + 5):
        to = "0x" + format(0x100 + i, "040x")
        normal.append(_indexed(_tx(A, to, 2, 2 + i, "0x" + format(0x100 + i, "x")), 0))
    out = _run(Client(normal=normal), A, 1, max_hops=1, arrival_tx="0x1")
    assert len(out["edges"]) == 1  # the first outflow takes the traced 1 under FIFO
    assert len(out["possible"]) == trace_mod.POSSIBLE_ROWS
    assert out["possible_more"] == {"count": 4, "value": 8.0}


def test_possible_rows_and_order_unknown_reach_every_view():
    from tornado_demix.networks import get_network
    from tornado_demix.report_html import build_trace_report, end_state

    normal = [_indexed(_tx(X, A, 2, 5, "0x1"), 3)]
    internal = [_tx(A, B, 2, 5, "0x2")]
    out = _run(Client(normal=normal, internal=internal), A, 2, max_hops=3, arrival_tx="0x1")
    assert end_state(trace_mod.ORDER_UNKNOWN) == ("chance", "order unknown")
    html = build_trace_report(out, get_network("ethereum"))
    assert "Possible outflows (history incomplete)" in html
    assert "order unknown" in html and B in html
    assert "order_unknown" in trace_mod.END_STATES_NOTE
    assert "1 possible outflow on incomplete histories" in html  # page 1 check


# --- stale arrival index, rows before the arrival, incremental re-sort -----------------


def test_the_arrival_index_follows_a_re_sort_of_the_start_history():
    # B pays A back by an internal transfer of tx 0x3, whose index (11) is known only from
    # C's zero-value call of B in B's list, read one level later: A's history is re-sorted
    # and the arrival must still be found at its new place
    normal = [
        _indexed(_tx(X, A, 9, 1, "0x1"), 0),
        _indexed(_tx(A, B, 9, 1, "0x2"), 6),
        _indexed(_tx(C, B, 0, 1, "0x3"), 11),
    ]
    internal = [_tx(B, A, 8, 1, "0x3")]
    out = _run(Client(normal=normal, internal=internal), A, 9, max_hops=4, arrival_tx="0x1")
    assert out["edges"], out
    assert abs(sum(t["amount"] for t in out["terminals"]) - 9) < 1e-9, out["terminals"]


def test_an_outflow_known_to_precede_every_traced_arrival_is_no_possible_row():
    normal = [
        _indexed(_tx(X, A, 1, 4, "0x0"), 0),
        _indexed(_tx(A, C, 1, 5, "0x1"), 0),  # index 0 of block 5: before the arrival
        _indexed(_tx(X, A, 2, 5, "0x2"), 3),  # the traced 2
        _indexed(_tx(A, B, 5, 6, "0x3"), 0),  # overdraft: history incomplete
    ]
    out = _run(Client(normal=normal), A, 2, max_hops=2, arrival_tx="0x2")
    assert "0x1" not in {r["tx_hash"] for r in out["possible"]}, out["possible"]


def test_an_outflow_with_a_fifo_share_below_the_threshold_is_no_possible_row():
    normal = [
        _indexed(_tx(X, A, 100, 1, "0x1"), 0),
        _indexed(_tx(A, B, 99.5, 2, "0x2"), 0),
        _indexed(_tx(A, C, 1, 3, "0x3"), 0),  # FIFO 0.5: below the threshold
        _indexed(_tx(A, D, 5, 4, "0x4"), 0),  # overdraft: history incomplete
    ]
    out = _run(Client(normal=normal), A, 100, max_hops=1, arrival_tx="0x1")
    ends = {(t["address"], t["reason"]) for t in out["terminals"]}
    assert (C, "below_threshold") in ends
    assert "0x3" not in {r["tx_hash"] for r in out["possible"]}


def _scratch_order(tracer, order_all):
    """The histories of ``tracer`` sorted from scratch (a fresh order_all on copies)."""
    fresh = trace_mod._Tracer()
    fresh.histories = {
        key: {
            "events": [dict(e) for e in sorted(h["events"], key=lambda e: e["hash"])],
            "complete": h["complete"],
            "index": dict(h["index"]),
        }
        for key, h in tracer.histories.items()
    }
    order_all(fresh)
    return fresh.histories


def _check_incremental_order(monkeypatch, client, start, amount, **kw):
    real = trace_mod._Tracer.order_all
    seen = {"resorted": 0}

    def order_all(self):
        before = {k: list(h.get("positions") or []) for k, h in self.histories.items()}
        real(self)
        expect = _scratch_order(self, real)
        for key, hist in self.histories.items():
            if before.get(key) and before[key] != hist["positions"]:
                seen["resorted"] += 1
            want = expect[key]
            assert hist["positions"] == want["positions"], key
            assert [e["at"] for e in hist["events"]] == [e["at"] for e in want["events"]]
            assert hist["ambiguous"] == want["ambiguous"] and hist["inflows"] == want["inflows"]
            assert hist["keys"] == want["keys"]

    monkeypatch.setattr(trace_mod._Tracer, "order_all", order_all)
    out = _run(client, start, amount, **kw)
    return out, seen["resorted"]


def test_a_later_history_giving_a_row_its_index_re_sorts_like_from_scratch(monkeypatch):
    normal = [
        _indexed(_tx(X, A, 9, 1, "0x1"), 0),
        _indexed(_tx(A, B, 9, 1, "0x2"), 6),
        _indexed(_tx(C, B, 0, 1, "0x3"), 11),
    ]
    internal = [_tx(B, A, 8, 1, "0x3")]
    client = Client(normal=normal, internal=internal)
    _out, resorted = _check_incremental_order(monkeypatch, client, A, 9, arrival_tx="0x1")
    assert resorted >= 1


def test_a_later_history_raising_a_block_time_re_sorts_like_from_scratch(monkeypatch):
    # A's row of block 3 carries no timestamp; B's row of block 3 (read later) does, and
    # it is later than A's block-4 row's stamp... the running maximum moves A's events
    late = _indexed(_tx(A, B, 2, 3, "0x2"), 0)
    late["timeStamp"] = "0"
    early = _indexed(_tx(X, A, 2, 1, "0x1"), 0)
    other = _indexed(_tx(A, C, 1, 4, "0x4"), 0)
    other["timeStamp"] = "40"
    seen_by_b = _indexed(_tx(D, B, 1, 3, "0x3"), 1)
    seen_by_b["timeStamp"] = "60"  # block 3 is later than block 4's stamp: clock rises
    client = Client(normal=[early, late, other, seen_by_b])
    _out, resorted = _check_incremental_order(monkeypatch, client, A, 2, arrival_tx="0x1")
    assert resorted >= 1


def test_random_graphs_re_sort_incrementally_like_from_scratch(monkeypatch):
    rng = random.Random(5)
    for _ in range(60):
        txs, arrival, client = _random_client(rng)
        for row in client.normal:
            if rng.random() < 0.2:
                row["timeStamp"] = "0"
        start = float(next(t["v"] for t in txs if t["hash"] == arrival))
        _check_incremental_order(
            monkeypatch, client, A, start, arrival_tx=arrival, max_nodes=rng.choice([2, 3, 40])
        )
        monkeypatch.undo()
