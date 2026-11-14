"""Multi-hop FIFO tracing with a fake explorer."""

import re

from tornado_demix.trace import arrival_text, share_status, share_text, trace_funds

EXIT = "0x" + "e" * 40
HOP1 = "0x" + "1" * 40
HOP2 = "0x" + "2" * 40
OTHER = "0x" + "3" * 40
ROUTER = "0x" + "7" * 40
EXCHANGE = "0x" + "8" * 40
TOKEN = "0x" + "d" * 40
WEI = 10**18


def _tx(frm, to, eth, block, h, data="0x"):
    return {
        "from": frm,
        "to": to,
        "value": str(int(eth * WEI)),
        "blockNumber": str(block),
        "timeStamp": str(block * 12),
        "hash": h,
        "input": data,
        "isError": "0",
    }


def _ttx(frm, to, amount, block, h):
    row = _tx(frm, to, amount, block, h)
    row.update(contractAddress=TOKEN, tokenDecimal="18", tokenSymbol="DAI")
    return row


class Client:
    def __init__(self, normal=(), internal=(), tokens=()):
        self.normal, self.internal, self.tokens = list(normal), list(internal), list(tokens)

    def _touching(self, rows, addr):
        return [r for r in rows if addr in (r["from"], r["to"])]

    def outgoing_txs(self, addr):
        return self._touching(self.normal, addr)

    def internal_txs(self, addr):
        return self._touching(self.internal, addr)

    def token_transfers(self, addr, contract=None, **kw):
        rows = self._touching(self.tokens, addr)
        return [r for r in rows if not contract or r["contractAddress"] == contract]


def test_fifo_attributes_only_the_traced_amount_and_stops_at_a_labelled_service():
    client = Client(
        normal=[
            _tx(EXIT, HOP1, 3, 10, "0xa"),  # 3 of the 5 traced
            _tx(EXIT, OTHER, 4, 11, "0xb"),  # only 2 of it is the traced funds
            _tx(EXIT, OTHER, 9, 12, "0xc"),  # after the amount is spent
            _tx(HOP1, EXCHANGE, 3, 20, "0xd"),
        ]
    )
    out = trace_funds(client, EXIT, 5, labels={EXCHANGE: {"label": "Exchange"}})
    attributed = [(e["from"], e["to"], e["attributed"]) for e in out["edges"]]
    assert (EXIT, HOP1, 3) in attributed and (EXIT, OTHER, 2) in attributed
    assert all(e["tx_hash"] != "0xc" for e in out["edges"])
    reasons = {t["address"]: t["reason"] for t in out["terminals"]}
    assert reasons[EXCHANGE] == "labelled address"
    assert reasons[OTHER] == "not moved on (held or untraced)"


def test_transfers_before_the_funds_arrived_are_not_attributed():
    client = Client(normal=[_tx(EXIT, HOP1, 1, 5, "0xold"), _tx(EXIT, HOP2, 1, 50, "0xnew")])
    out = trace_funds(client, EXIT, 1, start_block=10, max_hops=1)
    assert [e["to"] for e in out["edges"]] == [HOP2]
    assert out["terminals"][0]["reason"] == "hop limit"


def test_a_swap_continues_with_the_output_asset():
    client = Client(
        normal=[_tx(EXIT, ROUTER, 2, 10, "0xswap", data="0x7ff36ab5")],
        tokens=[
            _ttx(ROUTER, EXIT, 6000, 10, "0xswap"),
            _ttx(EXIT, HOP1, 6000, 15, "0xsend"),
        ],
    )
    out = trace_funds(client, EXIT, 1, max_hops=3)
    swap = out["edges"][0]
    assert swap["kind"] == "swap" and swap["swapped_to"] == "DAI"
    send = out["edges"][1]
    # Half of the 2 ETH input is traced, so half of the output is.
    assert send["asset"] == "DAI" and send["attributed"] == 3000
    assert {t["address"] for t in out["terminals"]} == {HOP1}


def test_a_contract_that_pays_nothing_back_ends_the_trace():
    client = Client(normal=[_tx(EXIT, ROUTER, 1, 10, "0xbridge", data="0xdeadbeef")])
    out = trace_funds(client, EXIT, 1)
    assert out["terminals"] == [
        {
            "address": ROUTER,
            "asset": "ETH",
            "amount": 1.0,
            "hop": 1,
            "reason": "contract without a payment back",
            "label": None,
        }
    ]


def test_the_node_budget_bounds_the_calls():
    chain = [
        _tx("0x" + format(i, "040x"), "0x" + format(i + 1, "040x"), 1, i, f"0x{i}")
        for i in range(1, 30)
    ]
    start = "0x" + format(1, "040x")
    out = trace_funds(Client(normal=chain), start, 1, max_hops=50, max_nodes=5)
    assert out["nodes_expanded"] == 5
    assert out["terminals"][-1]["reason"] == "trace budget"


def test_an_rpc_answer_overrides_the_call_data_guess():
    client = Client(normal=[_tx(EXIT, HOP1, 1, 10, "0xa", data="0xabcdef")])
    out = trace_funds(client, EXIT, 1, max_hops=1, is_contract=lambda a: False)
    assert out["terminals"][0]["reason"] == "hop limit"


def test_edges_carry_bounds_next_to_the_fifo_amount():
    client = Client(
        normal=[
            _tx(OTHER, EXIT, 2, 1, "0xprior"),  # 2 ETH of other funds already there
            _tx(HOP2, EXIT, 5, 5, "0xin"),  # the traced 5
            _tx(EXIT, HOP1, 3, 10, "0xa"),
            _tx(EXIT, OTHER, 4, 11, "0xb"),
        ]
    )
    out = trace_funds(client, EXIT, 5, start_block=5, max_hops=1)
    first, second = out["edges"]
    assert (first["attributed"], first["lower"], first["upper"]) == (3, 1, 3)
    # balance 4 after the first out; traced in [2, 5]; other at most 2 -> lower 2
    assert (second["attributed"], second["lower"], second["upper"]) == (2, 2, 4)
    assert not first["history_incomplete"] and not second["history_incomplete"]


def test_native_funds_do_not_count_as_other_funds_of_a_token_trace():
    client = Client(
        normal=[_tx(OTHER, EXIT, 100, 1, "0xnative")],
        tokens=[_ttx(OTHER, EXIT, 5, 5, "0xin"), _ttx(EXIT, HOP1, 3, 10, "0xa")],
    )
    out = trace_funds(client, EXIT, 5, start_block=5, token=TOKEN, max_hops=1)
    edge = out["edges"][0]
    assert (edge["attributed"], edge["lower"], edge["upper"]) == (3, 3, 3)


def test_internal_inflows_count_as_other_funds():
    client = Client(
        normal=[_tx(EXIT, HOP1, 1, 10, "0xa"), _tx(HOP1, HOP2, 2, 20, "0xb")],
        internal=[_tx(OTHER, HOP1, 2, 12, "0xint")],
    )
    out = trace_funds(client, EXIT, 1, max_hops=2)
    second = out["edges"][1]
    assert (second["attributed"], second["lower"], second["upper"]) == (1, 0, 1)
    assert not second["history_incomplete"]


def test_an_outflow_beyond_the_known_history_is_flagged_per_edge():
    client = Client(normal=[_tx(EXIT, HOP1, 1, 10, "0xa"), _tx(HOP1, HOP2, 3, 20, "0xb")])
    out = trace_funds(client, EXIT, 1, max_hops=2)
    first, second = out["edges"]
    assert first["history_incomplete"] is False
    assert second["history_incomplete"] is True


def _indexed(row, index):
    row["transactionIndex"] = str(index)
    return row


def test_a_transfer_before_the_matched_arrival_carries_no_traced_funds():
    client = Client(normal=[_tx(EXIT, HOP1, 1, 6, "0xearly"), _tx(OTHER, EXIT, 1, 10, "0xin")])
    out = trace_funds(client, EXIT, 1, start_block=5, max_hops=1)
    assert out["arrival_matched"] == "exact" and out["arrival_tx"] == "0xin"
    assert out["edges"] == []
    (end,) = out["terminals"]
    assert (end["address"], end["amount"]) == (EXIT, 1.0)
    assert end["reason"] == "not moved on (held or untraced)"


def test_an_edge_from_an_older_result_without_bounds_says_so():
    edge = {"attributed": 1.0, "bounds_available": False, "history_incomplete": False}
    assert share_text(edge) == "FIFO 1.0" and share_status(edge) == "bounds unavailable"


def test_a_transfer_ordered_before_the_arrival_in_the_same_block_is_not_an_edge_on_a_later_hop():
    client = Client(
        normal=[
            _indexed(_tx(HOP1, HOP2, 1, 10, "0xb"), 2),
            _indexed(_tx(EXIT, HOP1, 1, 10, "0xa"), 5),
        ]
    )
    out = trace_funds(client, EXIT, 1, max_hops=2)
    assert [e["hop"] for e in out["edges"]] == [1]
    assert out["terminals"][0]["address"] == HOP1


def test_equal_inflows_leave_the_arrival_ambiguous_and_the_bounds_wide():
    client = Client(
        normal=[
            _tx(OTHER, EXIT, 5, 1, "0xp1"),
            _tx(OTHER, EXIT, 5, 2, "0xp2"),
            _tx(EXIT, HOP1, 8, 10, "0xa"),
        ]
    )
    out = trace_funds(client, EXIT, 5, max_hops=1)
    edge = out["edges"][0]
    assert out["arrival_matched"] == "ambiguous" and out["arrival_tx"] is None
    assert (edge["lower"], edge["upper"]) == (0, 5)  # no false "certain at least"


def test_an_explicit_arrival_transaction_overrides_the_amount_match():
    rows = [
        _tx(OTHER, EXIT, 5, 1, "0xp1"),
        _tx(OTHER, EXIT, 5, 2, "0xp2"),
        _tx(EXIT, HOP1, 8, 10, "0xa"),
    ]
    out = trace_funds(Client(normal=rows), EXIT, 5, max_hops=1, arrival_tx="0xP2")
    edge = out["edges"][0]
    assert out["arrival_matched"] == "exact" and out["arrival_tx"] == "0xp2"
    assert (edge["lower"], edge["upper"]) == (3, 5)
    unknown = trace_funds(Client(normal=rows), EXIT, 5, max_hops=1, arrival_tx="0xnone")
    assert unknown["arrival_matched"] == "explicit_not_found" and unknown["arrival_tx"] is None
    assert "given arrival transaction is not in this address's history" in arrival_text(unknown)
    assert "widest case" in arrival_text(unknown)


# --- one ledger per address, FIFO from the arrival ---------------------------

A, B, C, D, E, F, X = ("0x" + ch * 40 for ch in "abcd5f9")


def _sum_per_tx(edges):
    per = {}
    for e in edges:
        per[e["tx_hash"]] = per.get(e["tx_hash"], 0) + e["attributed"]
    return per


def test_fifo_starts_at_the_given_arrival_not_at_the_start_block():
    rows = [
        _tx(X, A, 1, 5, "0x1"),
        _tx(A, B, 1, 10, "0x2"),  # before the traced funds arrived
        _tx(OTHER, A, 1, 100, "0x3"),
        _tx(A, C, 1, 200, "0x4"),
    ]
    out = trace_funds(Client(normal=rows), A, 1, max_hops=1, arrival_tx="0x3")
    assert out["arrival_matched"] == "exact"
    assert [(e["to"], e["attributed"]) for e in out["edges"]] == [(C, 1.0)]


def test_fifo_starts_at_the_matched_arrival():
    rows = [
        _tx(X, A, 2, 5, "0x1"),
        _tx(A, B, 1, 10, "0x2"),
        _tx(OTHER, A, 1.5, 100, "0x3"),
        _tx(A, C, 1.5, 200, "0x4"),
    ]
    out = trace_funds(Client(normal=rows), A, 1.5, max_hops=1)
    assert out["arrival_matched"] == "exact"
    assert [e["to"] for e in out["edges"]] == [C]


def test_a_diamond_attributes_each_downstream_transfer_once():
    # A -> B, A -> C, B -> D, C -> D, then D -> E and D -> F
    rows = [
        _tx(X, A, 4, 1, "0xin"),
        _tx(A, B, 2, 10, "0xab"),
        _tx(A, C, 2, 11, "0xac"),
        _tx(B, D, 2, 20, "0xbd"),
        _tx(C, D, 2, 21, "0xcd"),
        _tx(D, E, 2, 30, "0xde"),
        _tx(D, F, 2, 31, "0xdf"),
    ]
    out = trace_funds(Client(normal=rows), A, 4, max_hops=4, arrival_tx="0xin")
    by_tx = {}
    for e in out["edges"]:
        by_tx.setdefault(e["tx_hash"], []).append(e)
    assert all(len(v) == 1 for v in by_tx.values()), by_tx
    de, df = by_tx["0xde"][0], by_tx["0xdf"][0]
    assert (de["attributed"], df["attributed"]) == (2, 2)
    # each outflow is split back to the branch whose funds FIFO consumed
    assert [(s["tx_hash"], s["amount"]) for s in de["sources"]] == [("0xbd", 2)]
    assert [(s["tx_hash"], s["amount"]) for s in df["sources"]] == [("0xcd", 2)]
    assert (de["lower"], de["upper"]) == (2, 2)
    assert out["nodes_expanded"] == 6  # A, B, C, D, E, F each once
    ends = {(t["address"], t["amount"]) for t in out["terminals"]}
    assert ends == {(E, 2.0), (F, 2.0)}


def test_a_diamond_merges_into_one_edge_with_the_merged_amount():
    rows = [
        _tx(X, A, 4, 1, "0xin"),
        _tx(A, B, 2, 10, "0xab"),
        _tx(A, C, 2, 11, "0xac"),
        _tx(B, D, 2, 20, "0xbd"),
        _tx(C, D, 2, 21, "0xcd"),
        _tx(D, E, 4, 30, "0xde"),
    ]
    out = trace_funds(Client(normal=rows), A, 4, max_hops=4, arrival_tx="0xin")
    (de,) = [e for e in out["edges"] if e["tx_hash"] == "0xde"]
    assert de["attributed"] == 4 and (de["lower"], de["upper"]) == (4, 4)


def test_merged_branches_are_split_proportionally_to_fifo_consumption():
    rows = [
        _tx(X, A, 4, 1, "0xin"),
        _tx(A, B, 2, 10, "0xab"),
        _tx(A, C, 2, 11, "0xac"),
        _tx(B, D, 2, 20, "0xbd"),
        _tx(C, D, 2, 21, "0xcd"),
        _tx(D, E, 3, 30, "0xde"),  # all of the B branch, half of the C branch
    ]
    out = trace_funds(Client(normal=rows), A, 4, max_hops=3, arrival_tx="0xin")
    (de,) = [e for e in out["edges"] if e["tx_hash"] == "0xde"]
    assert de["attributed"] == 3 and de["hop"] == 3
    assert [(s["tx_hash"], s["from"], s["amount"]) for s in de["sources"]] == [
        ("0xbd", B, 2),
        ("0xcd", C, 1),
    ]
    held = [t for t in out["terminals"] if t["address"] == D]
    assert held and held[0]["amount"] == 1.0


def test_two_traced_inflows_into_one_address_are_not_double_counted():
    rows = [
        _tx(X, A, 2, 1, "0x1"),
        _tx(A, B, 1, 10, "0x2"),
        _tx(A, B, 1, 20, "0x3"),
        _tx(B, C, 1, 30, "0x4"),
        _tx(B, D, 1, 40, "0x5"),
    ]
    out = trace_funds(Client(normal=rows), A, 2, max_hops=2, arrival_tx="0x1")
    per = _sum_per_tx([e for e in out["edges"] if e["hop"] == 2])
    assert per == {"0x4": 1.0, "0x5": 1.0}


def test_an_address_reached_again_later_is_rerun_with_the_merged_ledger():
    # B is reached at hop 1 (from A) and at hop 2 (A -> C -> B); its one
    # outflow to E carries both, attributed once
    rows = [
        _tx(X, A, 3, 1, "0xin"),
        _tx(A, B, 1, 10, "0xab"),
        _tx(A, C, 2, 11, "0xac"),
        _tx(C, B, 2, 12, "0xcb"),
        _tx(B, E, 3, 20, "0xbe"),
    ]
    out = trace_funds(Client(normal=rows), A, 3, max_hops=4, arrival_tx="0xin")
    (be,) = [e for e in out["edges"] if e["tx_hash"] == "0xbe"]
    assert be["attributed"] == 3 and be["hop"] == 2  # the shortest branch names the hop
    assert sorted(s["amount"] for s in be["sources"]) == [1, 2]
    assert len([e for e in out["edges"] if e["from"] == B]) == 1


def test_an_internal_outflow_of_a_contract_wallet_is_followed():
    client = Client(
        normal=[_tx(X, A, 1, 5, "0x1")],
        internal=[_tx(A, C, 1, 50, "0x2")],
    )
    out = trace_funds(client, A, 1, max_hops=1, arrival_tx="0x1", is_contract=lambda a: False)
    assert [(e["to"], e["attributed"]) for e in out["edges"]] == [(C, 1.0)]


def test_an_incomplete_history_is_never_called_certain():
    # traced 1 in; an unseen other inflow; 5 out: all 5 may be the unseen funds
    rows = [_tx(X, A, 1, 5, "0x1"), _tx(A, C, 5, 50, "0x2")]
    out = trace_funds(Client(normal=rows), A, 1, max_hops=1, arrival_tx="0x1")
    (edge,) = out["edges"]
    assert edge["history_incomplete"] and edge["lower"] == 0
    assert share_status(edge) == "possible (history incomplete)"
    assert share_status({**edge, "lower": 1}) == "possible (history incomplete)"
    assert share_status({**edge, "history_incomplete": False, "lower": 1}) == "certain at least 1"


def test_a_truncated_explorer_list_marks_the_history_incomplete():
    rows = [_tx(X, A, 5, 5, "0x1"), _tx(A, C, 5, 50, "0x2")]
    client = Client(normal=rows)
    client.truncated = [
        {"action": "txlist", "address": A, "block": 7, "limit": 10000, "reason": "truncated"}
    ]
    (edge,) = trace_funds(client, A, 5, max_hops=1, arrival_tx="0x1")["edges"]
    assert edge["history_incomplete"] is True and edge["lower"] == 0
    # a truncated token list does not touch the native history
    client.truncated[0]["action"] = "tokentx"
    (edge,) = trace_funds(client, A, 5, max_hops=1, arrival_tx="0x1")["edges"]
    assert edge["history_incomplete"] is False and edge["lower"] == 5


def test_rounded_bounds_never_cross():
    rows = [
        _tx(OTHER, A, 0.3, 1, "0xp"),
        _tx(X, A, 3.4302085, 2, "0xin"),
        _tx(A, C, 3.4302085, 3, "0xo"),
    ]
    out = trace_funds(Client(normal=rows), A, 3.4302085, max_hops=1, arrival_tx="0xin")
    for e in out["edges"]:
        assert e["lower"] <= e["attributed"] <= e["upper"]


def test_trace_invariants_on_random_transfer_graphs():
    hypothesis = __import__("pytest").importorskip("hypothesis")
    st = hypothesis.strategies
    nodes = [A, B, C, D, E]
    transfer = st.tuples(
        st.sampled_from(nodes),
        st.sampled_from(nodes + [F]),
        st.integers(1, 6),
        st.integers(2, 40),
    ).filter(lambda t: t[0] != t[1])

    @hypothesis.settings(deadline=None, max_examples=200)
    @hypothesis.given(st.lists(transfer, min_size=1, max_size=12))
    def check(transfers):
        rows = [_indexed(_tx(X, A, 8, 1, "0x" + format(0, "064x")), 0)]
        for i, (frm, to, value, block) in enumerate(transfers, start=1):
            rows.append(_indexed(_tx(frm, to, value, block, "0x" + format(i, "064x")), i))
        position = {r["hash"]: (int(r["blockNumber"]), int(r["transactionIndex"])) for r in rows}
        value = {r["hash"]: int(r["value"]) / WEI for r in rows}
        out = trace_funds(
            Client(normal=rows),
            A,
            8,
            max_hops=5,
            arrival_tx=rows[0]["hash"],
            is_contract=lambda a: False,
        )
        for tx, total in _sum_per_tx(out["edges"]).items():
            assert total <= value[tx] + 1e-6, (tx, total, value[tx])
        for e in out["edges"]:
            assert e["lower"] <= e["attributed"] <= e["upper"]
            assert abs(sum(s["amount"] for s in e["sources"]) - e["attributed"]) < 1e-5
            for s in e["sources"]:
                # no edge from an outflow earlier than the arrival that funds it
                assert position[s["tx_hash"]] < position[e["tx_hash"]], (s, e)
        held = sum(t["amount"] for t in out["terminals"])
        assert held <= 8 + 1e-5

    check()


def test_the_json_result_and_the_html_report_say_possible_for_an_incomplete_history():
    from tornado_demix.networks import get_network
    from tornado_demix.report_html import build_trace_report

    rows = [_tx(X, A, 1, 5, "0x1"), _tx(A, C, 5, 50, "0x2")]
    out = trace_funds(Client(normal=rows), A, 1, max_hops=1, arrival_tx="0x1")
    assert out["edges"][0]["status"] == "possible (history incomplete)"
    html = build_trace_report(out, get_network("ethereum"))
    assert "possible (history incomplete)" in html
    assert not re.search(r"certain at least \d", html)


def test_an_overdraft_widens_the_earlier_edges_of_the_same_address_too():
    # traced 2 in, 2 out (certain on its own), then 3 out with nothing known left:
    # an unseen inflow before the first outflow could have paid it instead
    rows = [
        _tx(X, A, 2, 5, "0x1"),
        _tx(A, C, 2, 10, "0x2"),
        _tx(A, D, 3, 20, "0x3"),
    ]
    out = trace_funds(Client(normal=rows), A, 2, max_hops=1, arrival_tx="0x1")
    first = next(e for e in out["edges"] if e["tx_hash"] == "0x2")
    assert first["history_incomplete"] is True
    assert (first["attributed"], first["lower"], first["upper"]) == (2, 0, 2)
    assert first["status"] == "possible (history incomplete)"


def test_a_row_without_a_timestamp_keeps_its_place_in_block_order():
    late = _tx(A, C, 1, 20, "0x2")
    late["timeStamp"] = ""
    client = Client(normal=[_tx(X, A, 1, 5, "0x1")], internal=[late])
    out = trace_funds(client, A, 1, max_hops=1, arrival_tx="0x1", is_contract=lambda a: False)
    assert [(e["to"], e["attributed"]) for e in out["edges"]] == [(C, 1.0)]


# --- order inside a block, and downstream certainty ---------------------------


def test_a_zero_value_trigger_gives_an_internal_payment_its_index():
    # contract wallet A pays C by an internal transfer inside a zero-value exec tx
    # (index 3); the traced 1 arrives later in the same block (index 5)
    normal = [
        _indexed(_tx(X, A, 1, 1, "0x1"), 0),
        _indexed(_tx(OTHER, A, 0, 10, "0x3"), 3),
        _indexed(_tx(X, A, 1, 10, "0x5"), 5),
    ]
    internal = [_tx(A, C, 1, 10, "0x3")]
    out = trace_funds(
        Client(normal=normal, internal=internal),
        A,
        1,
        max_hops=2,
        arrival_tx="0x5",
        is_contract=lambda a: False,
    )
    assert out["edges"] == []
    assert out["terminals"][0]["address"] == A


def test_a_late_credit_past_an_outflow_is_never_certain():
    # A pays B 2 by an internal transfer (no index anywhere) in block 5; B sends 2
    # to C at index 4 of that block, receives 2 other funds, then sends 2 to D
    normal = [
        _indexed(_tx(X, A, 2, 1, "0x1"), 0),
        _indexed(_tx(B, C, 2, 5, "0x4"), 4),
        _indexed(_tx(X, B, 2, 6, "0x6"), 0),
        _indexed(_tx(B, D, 2, 7, "0x7"), 0),
    ]
    internal = [_tx(A, B, 2, 5, "0x2")]
    out = trace_funds(
        Client(normal=normal, internal=internal),
        A,
        2,
        max_hops=3,
        arrival_tx="0x1",
        is_contract=lambda a: False,
    )
    from_b = [e for e in out["edges"] if e["from"] == B]
    assert from_b, out
    for e in from_b:
        assert not e["status"].startswith("certain"), e
        assert e["lower"] == 0 and e["history_incomplete"]


def test_a_certain_hop_needs_a_certain_upstream_edge():
    # A: 10 other, traced 1, sends 5 to B (FIFO 1, only possible); B sends 5 to C
    normal = [
        _indexed(_tx(X, A, 10, 1, "0x1"), 0),
        _indexed(_tx(OTHER, A, 1, 2, "0x2"), 0),
        _indexed(_tx(A, B, 5, 3, "0x3"), 0),
        _indexed(_tx(B, C, 5, 4, "0x4"), 0),
    ]
    out = trace_funds(Client(normal=normal), A, 1, max_hops=3, arrival_tx="0x2")
    ab, bc = out["edges"]
    assert (ab["attributed"], ab["lower"], ab["upper"]) == (1, 0, 1)
    assert (bc["attributed"], bc["lower"], bc["upper"]) == (1, 0, 1)
    assert bc["status"] == "possible"


def test_a_certain_upstream_edge_keeps_the_next_hop_certain():
    normal = [
        _indexed(_tx(X, A, 1, 1, "0x1"), 0),
        _indexed(_tx(A, B, 1, 3, "0x3"), 0),
        _indexed(_tx(B, C, 1, 4, "0x4"), 0),
    ]
    out = trace_funds(Client(normal=normal), A, 1, max_hops=3, arrival_tx="0x1")
    assert [e["status"] for e in out["edges"]] == ["certain at least 1.0"] * 2


def test_an_overdraft_downstream_of_a_credit_widens_the_credit_too():
    # A's first edge looks certain until a later overdraft at A; B, credited from
    # that edge, must not stay certain on the narrow credit
    normal = [
        _indexed(_tx(X, A, 2, 1, "0x1"), 0),
        _indexed(_tx(A, B, 2, 2, "0x2"), 0),
        _indexed(_tx(B, C, 2, 3, "0x3"), 0),
        _indexed(_tx(A, D, 3, 4, "0x4"), 0),
    ]
    out = trace_funds(Client(normal=normal), A, 2, max_hops=2, arrival_tx="0x1")
    bc = next(e for e in out["edges"] if e["tx_hash"] == "0x3")
    assert bc["lower"] == 0 and bc["status"] == "possible"


def _random_internal_case(rng):
    """Random transfers with same-block events; some internal rows carry no index."""
    nodes = [A, B, C, D, E, F]
    balance = dict.fromkeys(nodes, 0)
    txs, n, arrival = [], 0, None
    for block in range(1, rng.randint(3, 6) + 1):
        for idx in sorted(rng.sample(range(10), rng.randint(1, 4))):
            n += 1
            h = "0x" + format(n, "064x")
            rich = [a for a in nodes if balance[a] > 0]
            if arrival is None and (block >= 2 or rng.random() < 0.3):
                frm, to, value, internal, arrival = X, A, rng.randint(2, 8), False, h
            elif not rich or rng.random() < 0.25:
                frm, to, value = X, rng.choice(nodes), rng.randint(1, 5)
                internal = rng.random() < 0.3
            else:
                frm = rng.choice(rich)
                to = rng.choice([a for a in nodes if a != frm] + [X])
                value = rng.randint(1, balance[frm])
                internal = rng.random() < 0.3
            if frm in balance:
                balance[frm] -= value
            if to in balance:
                balance[to] += value
            txs.append(
                {
                    "hash": h,
                    "frm": frm,
                    "to": to,
                    "v": value,
                    "block": block,
                    "idx": idx,
                    "internal": internal,
                }
            )
    normal, internal = [], []
    for t in txs:
        row = _tx(t["frm"], t["to"], t["v"], t["block"], t["hash"])
        if t["internal"]:
            internal.append(row)
        else:
            normal.append(_indexed(row, t["idx"]))
    return txs, arrival, normal, internal


def _true_range(txs, addr, traced_at, target):
    """Exact min/max traced share of ``target`` over all allocations, true order."""
    events = sorted(
        (t for t in txs if addr in (t["frm"], t["to"])), key=lambda t: (t["block"], t["idx"])
    )
    states, balance = {0}, 0
    for t in events:
        if t["to"] == addr:
            balance += t["v"]
            states = {s + traced_at.get(t["hash"], 0) for s in states}
            continue
        o, lows, highs, nxt = t["v"], [], [], set()
        for s in states:
            lo, hi = max(0, o - (balance - s)), min(o, s)
            if lo <= hi:
                lows.append(lo)
                highs.append(hi)
                nxt.update(s - x for x in range(lo, hi + 1))
        if t["hash"] == target:
            return (min(lows), max(highs)) if lows else None
        states, balance = nxt, balance - o
    return None


def test_trace_invariants_with_unindexed_internal_rows_and_same_block_events():
    import random

    rng = random.Random(20261113)
    for _ in range(400):
        txs, arrival, normal, internal = _random_internal_case(rng)
        start = next(t["v"] for t in txs if t["hash"] == arrival)
        out = trace_funds(
            Client(normal=normal, internal=internal),
            A,
            float(start),
            max_hops=6,
            arrival_tx=arrival,
            is_contract=lambda a: False,
            labels={X: {"label": "outside"}},
        )
        by_hash = {t["hash"]: t for t in txs}
        traced_in = {A: {arrival: start}}
        for e in out["edges"]:
            got = traced_in.setdefault(e["to"], {})
            got[e["tx_hash"]] = got.get(e["tx_hash"], 0) + round(e["attributed"])
        per_tx = _sum_per_tx(out["edges"])
        for tx, total in per_tx.items():
            assert total <= by_hash[tx]["v"] + 1e-6
        for e in out["edges"]:
            me = by_hash[e["tx_hash"]]
            assert e["lower"] <= e["attributed"] <= e["upper"], e
            for s in e["sources"]:
                src = by_hash[s["tx_hash"]]
                # no edge from a transfer earlier than the arrival that funds it
                assert (src["block"], src["idx"]) < (me["block"], me["idx"]), (s, e, txs)
            truth = _true_range(txs, e["from"], traced_in[e["from"]], e["tx_hash"])
            assert truth is not None, (e, txs)
            assert e["lower"] <= truth[0] + 1e-9 and e["upper"] >= truth[1] - 1e-9, (e, truth, txs)
