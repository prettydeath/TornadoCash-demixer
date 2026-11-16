"""CoW Protocol settlements: a transfer into GPv2Settlement is tied to the owner's Trade
event (owner, sell token and sell amount) and continued as a swap from the buy-token
payout to the order receiver in the same transaction.

Fixture (tests/fixtures/bridges/cow_settlement.json): the first ``Trade`` at or after
Ethereum block 26,300,000 whose sell and buy tokens are ERC-20 tokens and whose owner is
an EOA, skipping any whose addresses (sender, log and calldata words) appear in the
project's private data other than protocol or token contracts (the first EOA candidate
carried a solver contract that did). ``payout_rows`` is the owner's buy-token transfer
in that transaction, as the explorer lists it.
"""

import json
import os

from tornado_demix import swaps_cow as cow
from tornado_demix.errors import ApiError
from tornado_demix.keccak import keccak256
from tornado_demix.networks import get_network, load_networks
from tornado_demix.report_html import build_trace_report
from tornado_demix.trace import END_STATES_NOTE, trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
OWNER = "0xed8db5de8e7f3f14114bb2e2d57f08182537350f"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TGC = "0x25b4f5d4c314bcd5d7962734936c957b947cb7cf"
SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
BUY = 312736731666053128285


def _fx():
    with open(os.path.join(FIXTURES, "cow_settlement.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _topic(sig):
    return "0x" + keccak256(sig.encode()).hex()


def _w(n):
    return "{:064x}".format(n)


def test_topics_and_settlement_chains():
    assert cow.TOPIC_TRADE == _topic("Trade(address,address,address,uint256,uint256,uint256,bytes)")
    assert cow.TOPIC_TRANSFER == _topic("Transfer(address,address,uint256)")
    registry = {n.chain_id for n in load_networks().values()}
    assert registry <= cow.CHAINS  # every registry chain has the settlement contract
    assert cow.is_settlement(1, SETTLEMENT.upper().replace("0X", "0x"))
    assert not cow.is_settlement(None, SETTLEMENT) and not cow.is_settlement(1, USDT)
    assert cow.SOURCE_URL.startswith("https://docs.cow.fi/")


def test_decode_trades_and_payouts_on_the_fixture():
    fx = _fx()
    (t,) = cow.decode_trades(fx["receipt"], SETTLEMENT)
    assert t["owner"] == OWNER and t["sell_token"] == USDT and t["buy_token"] == TGC
    assert t["sell_amount"] == 46180200 and t["buy_amount"] == BUY and t["fee_amount"] == 0
    assert len(t["order_uid"]) == 2 + 56 * 2 and t["order_uid"][2 + 64 : 2 + 104] == OWNER[2:]
    pays = cow.payouts(fx["receipt"], SETTLEMENT, TGC)
    assert [(p["to"], p["amount"]) for p in pays] == [(OWNER, BUY)]
    assert cow.decode_trades(dict(fx["receipt"], status="0x0"), SETTLEMENT) == []
    assert cow.decode_trades(fx["receipt"], USDT) == []  # only the settlement's own logs


class Client:
    def __init__(self, fx, rows, receipt=None, others=None, internal=None, receipts=None):
        self.fx, self.rows = fx, rows
        self.receipt = fx["receipt"] if receipt is None else receipt
        self.others = others or {}
        self.internal = internal or {}
        self.receipts = receipts or {}

    def token_transfers(self, addr, contract=None, **kw):
        rows = self.rows if addr == OWNER else self.others.get(addr, [])
        return [
            r
            for r in rows
            if addr in (r["from"], r["to"]) and contract in (None, r["contractAddress"])
        ]

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return list(self.internal.get(addr, []))

    def tx_receipt(self, tx_hash):
        if isinstance(self.receipt, Exception):
            raise self.receipt
        if tx_hash in self.receipts:
            return self.receipts[tx_hash]
        return self.receipt if tx_hash == self.fx["tx_hash"] else None


def _run(fx=None, rows=None, receipt=None, others=None, amount=None, internal=None, receipts=None):
    fx = fx or _fx()
    rows = fx["l1_rows"] + fx["payout_rows"] if rows is None else rows
    return trace_funds(
        Client(fx, rows, receipt, others, internal, receipts),
        OWNER,
        amount or int(rows[0]["value"]) / 10**6,
        start_block=26_300_000,
        token=USDT,
        chain_id=1,
    )


def test_trade_is_followed_as_a_swap_into_the_buy_token():
    result = _run()
    (edge,) = result["edges"]
    assert edge["kind"] == "swap" and edge["swapped_to"] == "TGC"
    assert edge["swapped_to_address"] == TGC and "paid_to" not in edge
    info = edge["swap"]
    assert info["protocol"] == "cow" and info["state"] == cow.SETTLED
    assert info["sell_amount"] == 46180200 and info["buy_amount"] == BUY
    assert info["trades"] == 1 and info["receiver"] == OWNER
    (end,) = result["terminals"]
    assert end["address"] == OWNER and end["asset_address"] == TGC
    assert abs(end["amount"] - BUY / 10**18) < 1e-6


def _payout_to(fx, receiver, amount=BUY, extra=()):
    receipt = json.loads(json.dumps(fx["receipt"]))
    logs = []
    for log in receipt["logs"]:
        if log["address"] == TGC and log["topics"][1].endswith(SETTLEMENT[2:]):
            log["topics"][2] = "0x" + _w(int(receiver, 16))
            log["data"] = "0x" + _w(amount)
        logs.append(log)
    logs += list(extra)
    receipt["logs"] = logs
    return receipt


def test_payout_to_another_receiver_continues_there():
    fx = _fx()
    receiver = "0x" + "c3" * 20
    row = dict(fx["payout_rows"][0], to=receiver)
    result = _run(receipt=_payout_to(fx, receiver), rows=fx["l1_rows"], others={receiver: [row]})
    (edge,) = [e for e in result["edges"] if e["from"] == OWNER]
    assert edge["kind"] == "swap" and edge["paid_to"] == receiver
    assert edge["swap"]["receiver"] == receiver
    (end,) = result["terminals"]
    assert end["address"] == receiver and end["asset_address"] == TGC


def test_trade_of_another_amount_is_not_tied():
    fx = _fx()
    row = dict(fx["l1_rows"][0], value=str(int(fx["l1_rows"][0]["value"]) - 1))
    result = _run(rows=[row])
    (end,) = result["terminals"]
    assert end["reason"] == "swap cow (trade not tied to this transfer)"
    assert end["address"] == SETTLEMENT
    assert result["edges"][0]["swap"]["state"] == cow.NOT_TIED


def test_trade_of_another_owner_is_not_tied():
    fx = _fx()
    receipt = json.loads(json.dumps(fx["receipt"]))
    for log in receipt["logs"]:
        if log["topics"][0] == cow.TOPIC_TRADE:
            log["topics"][1] = "0x" + _w(int("44" * 20, 16))
    result = _run(receipt=receipt)
    assert result["terminals"][0]["reason"] == "swap cow (trade not tied to this transfer)"


def test_missing_or_ambiguous_payout_ends_the_branch():
    fx = _fx()
    receipt = json.loads(json.dumps(fx["receipt"]))
    receipt["logs"] = [
        x
        for x in receipt["logs"]
        if not (x["address"] == TGC and x["topics"][1].endswith(SETTLEMENT[2:]))
    ]
    result = _run(receipt=receipt, rows=fx["l1_rows"])
    (end,) = result["terminals"]
    uid = result["edges"][0]["swap"]["order_uid"]
    assert end["reason"] == f"swap cow (order {uid}, payout not found)"
    assert result["edges"][0]["swap"]["state"] == cow.PAYOUT_NOT_FOUND
    # two equal payouts to two other addresses: which is the receiver is not known
    a, b = "0x" + "a5" * 20, "0x" + "b6" * 20
    second = {
        "address": TGC,
        "topics": [cow.TOPIC_TRANSFER, "0x" + _w(int(SETTLEMENT, 16)), "0x" + _w(int(b, 16))],
        "data": "0x" + _w(BUY),
        "logIndex": "0x200",
    }
    result = _run(receipt=_payout_to(fx, a, extra=[second]), rows=fx["l1_rows"])
    assert result["terminals"][0]["reason"].endswith("payout not found)")


def test_receipt_failure_is_lookup_failed():
    result = _run(receipt=ApiError("down"))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed"


def test_each_trade_is_taken_by_one_transfer():
    fx = _fx()
    row = fx["l1_rows"][0]
    result = _run(rows=[row, dict(row)] + fx["payout_rows"], amount=2 * int(row["value"]) / 10**6)
    reasons = sorted(t["reason"] for t in result["terminals"])
    assert "swap cow (trade not tied to this transfer)" in reasons
    assert len([e for e in result["edges"] if e["kind"] == "swap"]) == 1


def test_batch_caveat_is_stated_and_the_report_names_the_receiver():
    assert "CoW" in END_STATES_NOTE and "batch" in END_STATES_NOTE
    fx = _fx()
    receiver = "0x" + "c3" * 20
    row = dict(fx["payout_rows"][0], to=receiver)
    result = _run(receipt=_payout_to(fx, receiver), rows=fx["l1_rows"], others={receiver: [row]})
    html = build_trace_report(result, get_network("ethereum"))
    assert "paid out by cow to" in html and receiver in html


# --- payouts that could be another order's ----------------------------------------------

OTHER = "0x" + "d4" * 20
THIRD = "0x" + "c3" * 20
WEI_BUY = 3 * 10**16


def _trade_log(owner, sell, buy, sell_amount, buy_amount, index, uid_byte="aa"):
    uid = uid_byte * 32 + owner[2:] + "ffffffff"
    data = (
        _w(int(sell, 16))
        + _w(int(buy, 16))
        + _w(sell_amount)
        + _w(buy_amount)
        + _w(0)
        + _w(6 * 32)
        + _w(56)
        + uid.ljust(128, "0")
    )
    return {
        "address": SETTLEMENT,
        "topics": [cow.TOPIC_TRADE, "0x" + _w(int(owner, 16))],
        "data": "0x" + data,
        "logIndex": hex(index),
    }


def _transfer_log(token, frm, to, amount, index):
    return {
        "address": token,
        "topics": [cow.TOPIC_TRANSFER, "0x" + _w(int(frm, 16)), "0x" + _w(int(to, 16))],
        "data": "0x" + _w(amount),
        "logIndex": hex(index),
    }


def _receipt(logs):
    return {"status": "0x1", "from": "0x" + "99" * 20, "to": SETTLEMENT, "logs": logs}


def test_single_other_payout_is_not_taken_when_another_trade_has_the_same_buy():
    fx = _fx()
    sell = int(fx["l1_rows"][0]["value"])
    # our trade and another user's with the same buy token and amount, and one payout to a
    # third address: it may be the other order's receiver, so it is not taken
    receipt = _receipt(
        [
            _trade_log(OWNER, USDT, TGC, sell, BUY, 1),
            _trade_log(OTHER, USDT, TGC, 5, BUY, 2, "bb"),
            _transfer_log(USDT, OWNER, SETTLEMENT, sell, 3),
            _transfer_log(TGC, SETTLEMENT, THIRD, BUY, 4),
        ]
    )
    row = dict(fx["payout_rows"][0], to=THIRD)
    result = _run(receipt=receipt, rows=fx["l1_rows"], others={THIRD: [row]})
    (end,) = result["terminals"]
    assert end["reason"].endswith("payout not found)")
    # the same payout with our trade alone in the receipt is ours
    receipt["logs"].pop(1)
    result = _run(receipt=receipt, rows=fx["l1_rows"], others={THIRD: [row]})
    (end,) = result["terminals"]
    assert end["address"] == THIRD and end["asset_address"] == TGC


def test_owner_payout_needs_one_payout_per_owner_trade_of_that_buy():
    fx = _fx()
    sell = int(fx["l1_rows"][0]["value"])
    # two orders of the owner, same buy token and amount, one payout to the owner and one
    # to another receiver: which order the owner's payout belongs to is not known
    logs = [
        _trade_log(OWNER, USDT, TGC, sell, BUY, 1),
        _trade_log(OWNER, USDT, TGC, sell, BUY, 2, "bb"),
        _transfer_log(USDT, OWNER, SETTLEMENT, sell, 3),
        _transfer_log(USDT, OWNER, SETTLEMENT, sell, 4),
        _transfer_log(TGC, SETTLEMENT, OWNER, BUY, 5),
        _transfer_log(TGC, SETTLEMENT, THIRD, BUY, 6),
    ]
    row = fx["l1_rows"][0]
    rows = [row, dict(row)] + fx["payout_rows"]
    result = _run(receipt=_receipt(logs), rows=rows, amount=2 * sell / 10**6)
    assert result["terminals"]
    assert all(t["reason"].endswith("payout not found)") for t in result["terminals"])
    # with both payouts to the owner each order takes one
    logs[5] = _transfer_log(TGC, SETTLEMENT, OWNER, BUY, 6)
    pay = fx["payout_rows"][0]
    rows = [row, dict(row), pay, dict(pay)]
    result = _run(receipt=_receipt(logs), rows=rows, amount=2 * sell / 10**6)
    assert len([e for e in result["edges"] if e["kind"] == "swap"]) == 2
    (end,) = result["terminals"]
    assert end["address"] == OWNER and abs(end["amount"] - 2 * BUY / 10**18) < 1e-6


def _native_buy(fx, payers):
    sell = int(fx["l1_rows"][0]["value"])
    receipt = _receipt(
        [
            _trade_log(OWNER, USDT, cow.NATIVE, sell, WEI_BUY, 1),
            _transfer_log(USDT, OWNER, SETTLEMENT, sell, 2),
        ]
    )
    rows = [
        {
            "from": payer,
            "to": OWNER,
            "value": str(WEI_BUY),
            "hash": fx["tx_hash"],
            "blockNumber": fx["l1_rows"][0]["blockNumber"],
            "timeStamp": fx["l1_rows"][0]["timeStamp"],
            "isError": "0",
        }
        for payer in payers
    ]
    return receipt, {OWNER: rows}


def test_native_buy_paid_to_the_owner_by_the_settlement_is_followed():
    fx = _fx()
    receipt, internal = _native_buy(fx, [SETTLEMENT])
    result = _run(receipt=receipt, rows=fx["l1_rows"], internal=internal)
    (edge,) = result["edges"]
    assert edge["kind"] == "swap" and edge["swapped_to_address"] is None
    (end,) = result["terminals"]
    assert end["asset_address"] is None and abs(end["amount"] - 0.03) < 1e-9


def test_native_buy_not_paid_by_the_settlement_is_not_found():
    fx = _fx()
    receipt, internal = _native_buy(fx, ["0x" + "77" * 20])
    result = _run(receipt=receipt, rows=fx["l1_rows"], internal=internal)
    assert result["terminals"][0]["reason"].endswith("payout not found)")
    receipt, _none = _native_buy(fx, [])
    result = _run(receipt=receipt, rows=fx["l1_rows"])
    assert result["terminals"][0]["reason"].endswith("payout not found)")


def test_two_native_trades_cannot_share_one_payment():
    fx = _fx()
    sell = int(fx["l1_rows"][0]["value"])
    receipt, internal = _native_buy(fx, [SETTLEMENT])
    receipt["logs"].insert(1, _trade_log(OWNER, USDT, cow.NATIVE, sell, WEI_BUY, 5, "bb"))
    receipt["logs"].append(_transfer_log(USDT, OWNER, SETTLEMENT, sell, 6))
    row = fx["l1_rows"][0]
    result = _run(
        receipt=receipt, rows=[row, dict(row)], internal=internal, amount=2 * sell / 10**6
    )
    assert len([e for e in result["edges"] if e["kind"] == "swap"]) == 1
    assert any(t["reason"].endswith("payout not found)") for t in result["terminals"])


def test_partial_fills_in_two_settlements_are_each_followed():
    fx = _fx()
    half = int(fx["l1_rows"][0]["value"]) // 2
    receipts, rows = {}, []
    for n, tx in enumerate(("0x" + "e1" * 32, "0x" + "e2" * 32)):
        # the same order (same uid) filled in two settlements, half each time
        receipts[tx] = _receipt(
            [
                _trade_log(OWNER, USDT, TGC, half, BUY // 2, 1),
                _transfer_log(USDT, OWNER, SETTLEMENT, half, 2),
                _transfer_log(TGC, SETTLEMENT, OWNER, BUY // 2, 3),
            ]
        )
        base = {
            "blockNumber": str(26_300_010 + n),
            "timeStamp": str(1793296200 + 12 * n),
            "hash": tx,
            "transactionIndex": "5",
        }
        rows.append(dict(fx["l1_rows"][0], value=str(half), **base))
        rows.append(dict(fx["payout_rows"][0], value=str(BUY // 2), **base))
    result = _run(rows=rows, receipts=receipts, amount=2 * half / 10**6)
    swaps = [e for e in result["edges"] if e["kind"] == "swap"]
    assert len(swaps) == 2
    assert swaps[0]["swap"]["order_uid"] == swaps[1]["swap"]["order_uid"]
    (end,) = result["terminals"]
    assert end["address"] == OWNER and abs(end["amount"] - (BUY // 2) * 2 / 10**18) < 1e-6


def test_two_owner_trades_with_different_buy_tokens_each_take_their_own_payout():
    fx = _fx()
    sell = int(fx["l1_rows"][0]["value"])
    dai, buy2 = "0x6b175474e89094c44da98b954eedeac495271d0f", 7 * 10**18
    logs = [
        _trade_log(OWNER, USDT, TGC, sell, BUY, 1),
        _trade_log(OWNER, USDT, dai, sell + 1, buy2, 2, "bb"),
        _transfer_log(USDT, OWNER, SETTLEMENT, sell, 3),
        _transfer_log(USDT, OWNER, SETTLEMENT, sell + 1, 4),
        _transfer_log(dai, SETTLEMENT, OWNER, buy2, 5),
        _transfer_log(TGC, SETTLEMENT, OWNER, BUY, 6),
    ]
    row = fx["l1_rows"][0]
    pay = fx["payout_rows"][0]
    rows = [
        row,
        dict(row, value=str(sell + 1)),
        pay,
        dict(pay, contractAddress=dai, value=str(buy2), tokenSymbol="DAI"),
    ]
    result = _run(receipt=_receipt(logs), rows=rows, amount=(2 * sell + 1) / 10**6)
    swaps = sorted(
        (e["swap"]["sell_amount"], e["swapped_to_address"])
        for e in result["edges"]
        if e["kind"] == "swap"
    )
    assert swaps == [(sell, TGC), (sell + 1, dai)]
    ends = {t["asset_address"]: t for t in result["terminals"]}
    assert set(ends) == {TGC, dai}
    assert abs(ends[TGC]["amount"] - BUY / 10**18) < 1e-6
    assert abs(ends[dai]["amount"] - buy2 / 10**18) < 1e-6
