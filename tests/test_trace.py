"""Multi-hop FIFO tracing with a fake explorer."""

from tornado_demix.trace import trace_funds

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
