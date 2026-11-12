import os
import sys

from tornado_demix.bridges import classify, load_bridges

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
import bridge_exits  # noqa: E402

SPOKE = "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5"
EXIT = "0x" + "11" * 20
MID = "0x" + "22" * 20


def test_table_rows_have_sources_and_unknown_is_other():
    table = load_bridges()
    assert len(table) >= 20
    assert all(r["source_url"].startswith("https://") for r in table.values())
    assert classify(table, 1, SPOKE.upper().replace("0X", "0x")) == "across"
    assert classify(table, 1, "0x" + "00" * 20) == "other"
    assert classify(table, 137, SPOKE) == "other"  # same address on another chain is not listed


class FakeClient:
    def __init__(self, txs):
        self.txs = txs

    def fetch_all(self, action, address, start_block=0, end_block=0, extra=None):
        assert action == "txlist"
        return [
            t
            for t in self.txs.get(address, [])
            if start_block <= int(t["blockNumber"]) <= end_block
        ]

    def first_txs(self, address, start_block, end_block, n):
        return self.fetch_all("txlist", address, start_block, end_block)[:n]


def tx(frm, to, value, block, inp="0x"):
    return {
        "from": frm, "to": to, "value": str(int(value * 10**18)), "blockNumber": str(block),
        "isError": "0", "input": inp, "hash": "0x" + hex(block)[2:].zfill(64),
    }  # fmt: skip


def test_direct_bridge_call_is_hop_one_even_with_zero_value():
    c = FakeClient({EXIT: [tx(EXIT, SPOKE, 0, 105, "0xabcdef01")]})
    hits = bridge_exits.bridge_hops(c, load_bridges(), EXIT, 1.0, 100)
    assert [(h["hop"], h["protocol"]) for h in hits] == [(1, "across")]


def test_second_hop_follows_plain_transfer_and_ignores_dust():
    c = FakeClient(
        {
            EXIT: [tx(EXIT, MID, 0.9, 101), tx(EXIT, "0x" + "33" * 20, 0.001, 102)],
            MID: [tx(MID, SPOKE, 0.8, 110, "0xabcdef01")],
            "0x" + "33" * 20: [tx("0x" + "33" * 20, SPOKE, 0.001, 111, "0x01")],
        }
    )
    hits = bridge_exits.bridge_hops(c, load_bridges(), EXIT, 1.0, 100)
    assert [(h["hop"], h["protocol"], h["via"]) for h in hits] == [(2, "across", MID)]


def test_failed_tx_and_old_tx_do_not_count():
    bad = tx(EXIT, SPOKE, 0, 105, "0x01")
    bad["isError"] = "1"
    c = FakeClient({EXIT: [bad, tx(EXIT, SPOKE, 0, 50, "0x01")]})
    assert bridge_exits.bridge_hops(c, load_bridges(), EXIT, 1.0, 100) == []


def test_gate_uses_share_among_bridge_exits():
    per_exit = [{"protocols": ["across"]}] * 3 + [{"protocols": []}] * 7 + [{"protocols": ["hop"]}]
    s = bridge_exits.summarise(per_exit)
    assert s["exits"] == 11 and s["bridge_exits"] == 4
    assert s["by_protocol"]["across"] == 3
    assert s["top"] == "across" and s["gate"] is True


def test_busy_second_hop_recipient_is_skipped():
    busy = [tx(MID, "0x" + "44" * 20, 0.0001, 110 + i) for i in range(bridge_exits.BUSY_ROWS)]
    c = FakeClient(
        {EXIT: [tx(EXIT, MID, 0.9, 101)], MID: busy + [tx(MID, SPOKE, 0.5, 400, "0x01")]}
    )
    assert bridge_exits.bridge_hops(c, load_bridges(), EXIT, 1.0, 100) == []


def test_every_row_has_a_kind_and_the_liquidity_pool_is_not_a_bridge_row():
    table = load_bridges()
    assert {r["kind"] for r in table.values()} <= {"contract", "maker_eoa", "aggregator"}
    assert any(r["kind"] == "aggregator" for r in table.values())
    assert not any("HubPool" in r["role"] for r in table.values())


def test_unrecognised_recipients_are_collected_per_hop():
    c = FakeClient(
        {
            EXIT: [tx(EXIT, MID, 0.9, 101), tx(EXIT, "0x" + "55" * 20, 0, 102, "0xdeadbeef")],
            MID: [tx(MID, "0x" + "66" * 20, 0.1, 110, "0x1234")],
        }
    )
    unknown = []
    bridge_exits.bridge_hops(c, load_bridges(), EXIT, 1.0, 100, unknown=unknown)
    assert {(u["address"], u["hop"], u["kind"]) for u in unknown} == {
        ("0x" + "55" * 20, 1, "contract"),
        ("0x" + "66" * 20, 2, "contract"),
    }
