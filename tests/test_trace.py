"""Multi-hop FIFO tracing with a fake explorer."""

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


def test_a_transfer_before_the_matched_arrival_has_no_bounds_not_zero_bounds():
    client = Client(normal=[_tx(EXIT, HOP1, 1, 6, "0xearly"), _tx(OTHER, EXIT, 1, 10, "0xin")])
    out = trace_funds(client, EXIT, 1, start_block=5, max_hops=1)
    edge = out["edges"][0]
    assert out["arrival_matched"] == "exact" and out["arrival_tx"] == "0xin"
    assert edge["attributed"] == 1 and edge["bounds_available"] is False
    assert "lower" not in edge and "upper" not in edge
    assert share_text(edge) == "FIFO 1.0" and share_status(edge) == "bounds unavailable"


def test_a_transfer_ordered_before_the_arrival_in_the_same_block_has_no_bounds_on_a_later_hop():
    client = Client(
        normal=[
            _indexed(_tx(HOP1, HOP2, 1, 10, "0xb"), 2),
            _indexed(_tx(EXIT, HOP1, 1, 10, "0xa"), 5),
        ]
    )
    out = trace_funds(client, EXIT, 1, max_hops=2)
    second = [e for e in out["edges"] if e["hop"] == 2][0]
    assert second["bounds_available"] is False and "lower" not in second


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
