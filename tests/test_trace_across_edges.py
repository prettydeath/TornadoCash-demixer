"""Across edge cases in a trace: a swap before the deposit, a fill paid to the Across
MulticallHandler, ERC-4337 deposits, a router depositing in its own name and a listed
aggregator whose only payment back is a native refund.

Fixture (tests/fixtures/bridges/across_multicall_handler.json): the first Ethereum ->
Arbitrum deposit whose fill on Arbitrum at or after block 520,000,000 is paid to the
MulticallHandler and whose deposit transaction was sent by the depositor straight to the
Ethereum SpokePool, skipping candidates whose addresses appear in the project's private
data other than protocol or token contracts. Deposit tx
0x9ef4a500204eb8357469f572c7894499759c00e8781904f9bc367647b029e4e7 (0.005 ETH, deposit
id 4878172); fill tx 0xb886ca4b2b048750e8dfafb65828caaf0b0d1ff78de214400cdf4fa6371f5f9b,
where the handler unwraps the WETH and pays 0.0049793917231625 ETH back to the depositor
and the rest to a fee recipient. Every candidate fill carried the same relayer and fee
recipient, which do appear in that data, so wherever they occur in the stored receipts
(the fill transaction, and the fee recipient inside the deposit's message calldata) the
relayer contract, its sender and the fee recipient are replaced by the synthetic
addresses 0xe1e1..., 0xe2e2... and 0xe3e3.... Everything else is as read on chain,
including the transaction hashes and the fill's message hash, which are those of the
original transactions and so no longer match the edited content (nothing here checks
them).

The swap-before-deposit, ERC-4337 and router receipts are synthetic: real
swap-and-bridge receipts through the SpokePoolPeriphery at or after Ethereum block
26,300,000 and 26,310,000 all held swap-path contracts or relayers that appear in that
private data. They follow the layout of those receipts (the sender's token into the
periphery, the swap, the periphery's deposit of the other token).
"""

import json
import os

import pytest

from tornado_demix import bridges as B
from tornado_demix.errors import ApiError
from tornado_demix.report_html import end_state
from tornado_demix.trace import END_STATES_NOTE, bridge_text, trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
SPOKE = "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5"
ARB_SPOKE = "0xe35e9842fceaca96570b734083f4a58e8f7c5f2a"
PERIPHERY = "0x97ccdbea4632140639ad5ea9b944aa034eb15fd4"
LIFI = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ARB_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
ARB_WETH = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
HANDLER = "0x924a9f036260ddd5808007e1aa95f08ed08aa569"
EP7 = "0x0000000071727de22e5e9d8baf0edac6f37da032"
USER = "0x" + "a1" * 20
OTHER = "0x" + "b2" * 20
RECIPIENT = "0x" + "c3" * 20
RELAYER = "0x" + "d4" * 20
BUNDLER = "0x" + "e5" * 20
SWAPPER = "0x" + "f6" * 20
EXCHANGE = "0x" + "8" * 40
NOT_TIED = "bridge across (deposit not tied to this transfer)"
BLOCK = 26_320_000
TS = 1_793_300_000
TX = "0x" + "5e" * 32
FILL_TX = "0x" + "f1" * 32
DEP_ID = 4_900_000


def _w(n):
    return "{:064x}".format(n)


def _a(addr):
    return _w(int(addr, 16))


def _transfer(token, frm, to, amount, index):
    return {
        "address": token,
        "topics": [B.TOPIC_TRANSFER, "0x" + _a(frm), "0x" + _a(to)],
        "data": "0x" + _w(amount),
        "logIndex": hex(index),
    }


def _deposit(
    depositor,
    in_tok,
    in_amt,
    out_tok,
    out_amt,
    index,
    dest=42161,
    dep_id=DEP_ID,
    recipient=None,
):
    words = [
        _a(in_tok),
        _a(out_tok),
        _w(in_amt),
        _w(out_amt),
        _w(TS),
        _w(TS + 3600),
        _w(0),
        _a(recipient or RECIPIENT),
        _w(0),
        _w(10 * 32),
        _w(0),
    ]
    return {
        "address": SPOKE,
        "topics": [
            B.TOPIC_ACROSS_DEPOSIT,
            "0x" + _w(dest),
            "0x" + _w(dep_id),
            "0x" + _a(depositor),
        ],
        "data": "0x" + "".join(words),
        "logIndex": hex(index),
    }


def _fill(depositor, in_tok, in_amt, out_tok, out_amt, recipient=RECIPIENT, dep_id=DEP_ID):
    words = [
        _a(in_tok),
        _a(out_tok),
        _w(in_amt),
        _w(out_amt),
        _w(1),
        _w(TS + 3600),
        _w(0),
        _w(0),
        _a(depositor),
        _a(recipient),
        _w(0),
        _a(recipient),
        _w(0),
        _w(out_amt),
        _w(0),
    ]
    return {
        "address": ARB_SPOKE,
        "topics": [B.TOPIC_ACROSS_FILL, "0x" + _w(1), "0x" + _w(dep_id), "0x" + _a(RELAYER)],
        "data": "0x" + "".join(words),
        "blockNumber": hex(500_000_100),
        "timeStamp": hex(TS + 30),
        "transactionHash": FILL_TX,
        "logIndex": hex(3),
    }


def _uop(sender, index, success=1, entry_point=EP7):
    return {
        "address": entry_point,
        "topics": [B.TOPIC_USER_OPERATION, "0x" + "ab" * 32, "0x" + _a(sender), "0x" + _w(0)],
        "data": "0x" + _w(7) + _w(success) + _w(10**15) + _w(100000),
        "logIndex": hex(index),
    }


def _receipt(logs, frm=USER, to=PERIPHERY):
    return {"status": "0x1", "from": frm, "to": to, "blockNumber": hex(BLOCK), "logs": logs}


def _token_row(frm, to, units, token, h=TX, block=BLOCK, ts=TS, decimals="6", symbol="USDT"):
    return {
        "from": frm,
        "to": to,
        "value": str(units),
        "blockNumber": str(block),
        "timeStamp": str(ts),
        "hash": h,
        "input": "0x",
        "isError": "0",
        "contractAddress": token,
        "tokenDecimal": decimals,
        "tokenSymbol": symbol,
        "transactionIndex": "7",
    }


def _native_row(frm, to, wei, h=TX, block=BLOCK, ts=TS, data="0x"):
    return {
        "from": frm,
        "to": to,
        "value": str(wei),
        "blockNumber": str(block),
        "timeStamp": str(ts),
        "hash": h,
        "input": data,
        "isError": "0",
        "transactionIndex": "7",
    }


class Chain:
    """One chain: token and native rows, internal transactions, receipts, transactions and
    Across fill logs."""

    def __init__(self, tokens=(), native=(), internal=(), receipts=None, txs=None, fills=()):
        self.tokens, self.native = list(tokens), list(native)
        self.internal = list(internal)
        self.receipts, self.txs = receipts or {}, txs or {}
        self.fills = list(fills)

    def token_transfers(self, addr, contract=None, **kw):
        return [
            r
            for r in self.tokens
            if addr in (r["from"], r["to"]) and contract in (None, r["contractAddress"])
        ]

    def outgoing_txs(self, addr):
        return [r for r in self.native if r["from"] == addr]

    def internal_txs(self, addr):
        return [r for r in self.internal if addr in (r["from"], r["to"])]

    def internal_from(self, contract, start, end):
        return [
            r
            for r in self.internal
            if r["from"] == contract and start <= int(r["blockNumber"]) <= end
        ]

    def tx_receipt(self, tx_hash):
        return self.receipts.get(tx_hash)

    def tx_info(self, tx_hash):
        return self.txs.get(tx_hash)

    def block_by_time(self, ts, closest="before"):
        return 499_000_000 if closest == "before" else 501_000_000

    def current_block(self):
        return 501_000_000

    def get_logs(self, address, topic0, start, end, topics=None):
        return [
            r
            for r in self.fills
            if r["address"] == address
            and r["topics"][0] == topic0
            and all(r["topics"][i] == t for i, t in (topics or {}).items())
        ]


def _dests(chain_id, client, labels=None):
    def dest(chain):
        if chain != chain_id:
            return None
        return {
            "client": client,
            "labels": labels if labels is not None else {EXCHANGE: {"entity": "Some exchange"}},
            "currency": "ETH",
            "is_contract": None,
        }

    return dest


def _trace(origin, amount, dests, token=USDT, user=USER):
    return trace_funds(
        origin,
        user,
        amount,
        start_block=BLOCK - 5,
        token=token,
        chain_id=1,
        destinations=dests,
    )


# --- 1. a swap before the deposit ------------------------------------------------------

SOLD = 1_000_000_000  # 1000 USDT
BOUGHT = 999_093_290  # USDC the periphery deposited
FILLED = 998_100_000  # USDC paid on Arbitrum


def _swap_logs(extra=(), depositor=USER, deposits=None):
    logs = [
        _transfer(USDT, USER, PERIPHERY, SOLD, 68),
        _transfer(USDT, PERIPHERY, SWAPPER, SOLD, 69),
        _transfer(USDC, SWAPPER, PERIPHERY, BOUGHT, 73),
        _transfer(USDC, PERIPHERY, SPOKE, BOUGHT, 77),
    ]
    if deposits is None:
        deposits = [_deposit(depositor, USDC, BOUGHT, ARB_USDC, FILLED, 78)]
    return logs + deposits + list(extra)


def _swap_run(logs, dests=None, receipt_kw=None, to=PERIPHERY):
    row = _token_row(USER, to, SOLD, USDT)
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, to=to, **(receipt_kw or {}))})
    return _trace(origin, 1000.0, dests if dests is not None else (lambda chain: None))


def _arb(rows=(), fills=None):
    fill = _fill(USER, USDC, BOUGHT, ARB_USDC, FILLED)
    return Chain(tokens=list(rows), fills=[fill] if fills is None else fills)


def test_deposit_of_the_swapped_token_is_tied_to_the_transfer_into_the_periphery():
    result = _swap_run(_swap_logs())
    edge = result["edges"][0]
    assert edge["kind"] == "bridge" and edge["to"] == PERIPHERY
    info = edge["bridge"]
    assert info["state"] == "destination_chain_not_supported" and info["deposit_id"] == DEP_ID
    assert info["via"] == PERIPHERY
    assert info["swapped_before"] == {
        "from_token": USDT,
        "from_symbol": "USDT",
        "input_token": USDC,
        "input_symbol": "USDC",
        "input_amount": BOUGHT,
    }
    assert f"swapped to USDC ({USDC}) before the deposit" in bridge_text(info)
    (end,) = result["terminals"]
    assert end["reason"] == "destination_chain_not_supported" and end["amount"] == 1000.0


def test_swapped_deposit_is_followed_in_the_filled_token():
    paid = _token_row(RELAYER, RECIPIENT, FILLED, ARB_USDC, FILL_TX, 500_000_100, TS + 30)
    paid["tokenSymbol"] = "USDC"
    onward = _token_row(RECIPIENT, EXCHANGE, FILLED, ARB_USDC, "0xee", 500_000_200, TS + 90)
    result = _swap_run(_swap_logs(), _dests(42161, _arb([paid, onward])))
    bridge, after = result["edges"]
    assert bridge["bridge"]["state"] == "filled"
    assert after["chain_id"] == 42161 and after["from"] == RECIPIENT
    (end,) = result["terminals"]
    # the whole 1000 USDT became the deposit: followed as the 998.1 USDC the fill paid
    assert end["reason"] == "labelled address" and abs(end["amount"] - 998.1) < 1e-6
    assert end["asset_address"] == ARB_USDC


def test_swapped_deposit_whose_payout_is_not_listed_ends_with_its_own_state():
    result = _swap_run(_swap_logs(), _dests(42161, _arb()))
    (end,) = result["terminals"]
    assert end["reason"] == "bridge across (swapped before the deposit, fill amount not read)"
    assert end["bridge"]["state"] == "filled"


def test_swapped_deposit_paid_in_the_native_coin_needs_no_decimals():
    logs = _swap_logs(deposits=[_deposit(USER, USDC, BOUGHT, ARB_WETH, 4 * 10**17, 78)])
    fill = _fill(USER, USDC, BOUGHT, ARB_WETH, 4 * 10**17)
    result = _swap_run(logs, _dests(42161, Chain(fills=[fill])))
    (end,) = result["terminals"]
    assert end["chain_id"] == 42161 and end["address"] == RECIPIENT
    assert end["asset_address"] is None and abs(end["amount"] - 0.4) < 1e-9


def test_another_inflow_of_the_sold_token_into_the_periphery_unties_the_swap():
    extra = [_transfer(USDT, OTHER, PERIPHERY, 5_000_000, 90)]
    (end,) = _swap_run(_swap_logs(extra))["terminals"]
    assert end["reason"] == NOT_TIED


def test_two_swapped_deposits_in_the_transaction_are_not_told_apart():
    deposits = [
        _deposit(USER, USDC, BOUGHT, ARB_USDC, FILLED, 78),
        _deposit(USER, DAI, 10**18, ARB_USDC, 10**6, 79, dep_id=DEP_ID + 1),
    ]
    (end,) = _swap_run(_swap_logs(deposits=deposits))["terminals"]
    assert end["reason"] == NOT_TIED


def test_a_swapped_deposit_for_a_third_depositor_is_not_taken():
    (end,) = _swap_run(_swap_logs(depositor=OTHER))["terminals"]
    assert end["reason"] == NOT_TIED


def test_a_router_swapping_in_its_own_name_needs_the_strong_tie():
    logs = _swap_logs(depositor=PERIPHERY)
    result = _swap_run(logs)
    assert result["edges"][0]["bridge"]["deposit_id"] == DEP_ID  # sent by the user to it
    (end,) = _swap_run(logs, receipt_kw={"frm": RELAYER})["terminals"]
    assert end["reason"] == NOT_TIED


# --- 2. a fill paid to the MulticallHandler ----------------------------------------------


def _mh():
    with open(os.path.join(FIXTURES, "across_multicall_handler.json"), encoding="utf-8") as fh:
        return json.load(fh)


MH_USER = "0xf483f79e289a18d68c207fab7c575c9b790a69b1"
FEE_TO = "0x" + "e3" * 20
TO_USER, TO_FEE = 4979391723162500, 12479678504166


def _mh_run(internal=None, receipt=None, no_internal=False):
    fx = _mh()
    block = int(fx["receipt"]["blockNumber"], 16)
    row = _native_row(MH_USER, SPOKE, 5 * 10**15, fx["tx_hash"], block, fx["ts"], "0x7b939232")
    origin = Chain(native=[row], receipts={fx["tx_hash"]: fx["receipt"]})
    dest = Chain(
        internal=fx["fill_internal"] if internal is None else internal,
        receipts={fx["fills"][0]["transactionHash"]: receipt or fx["fill_receipt"]},
        fills=fx["fills"],
    )
    dest.block_by_time = lambda ts, closest="before": fx["dest_window"][closest != "before"]
    dest.current_block = lambda: fx["dest_window"][1]
    if no_internal:
        dest.internal_from = None  # a client that cannot list internal transactions
    result = trace_funds(
        origin,
        MH_USER,
        0.005,
        start_block=block - 5,
        chain_id=1,
        destinations=_dests(42161, dest, labels={}),
    )
    return fx, result


def test_fixture_fill_pays_the_handler_and_the_handler_pays_on():
    fx = _mh()
    assert B.is_multicall_handler(42161, HANDLER) and not B.is_multicall_handler(56, HANDLER)
    assert fx["fills"][0]["data"][2 + 9 * 64 + 24 : 2 + 10 * 64] == HANDLER[2:]
    assert sum(int(r["value"]) for r in fx["fill_internal"]) == TO_USER + TO_FEE
    assert B.handler_payouts(fx["fill_receipt"], HANDLER, ARB_WETH, 1) == []  # unwrapped


def test_handler_fill_is_followed_to_the_handlers_payees():
    fx, result = _mh_run()
    edge = result["edges"][0]
    info = edge["bridge"]
    assert info["state"] == "filled" and info["deposit_id"] == 4878172
    assert info["fill_recipient"] == HANDLER and info["handler"] == HANDLER
    assert sorted(p["to"] for p in info["paid_on"]) == sorted([MH_USER, FEE_TO])
    ends = {t["address"]: t for t in result["terminals"]}
    assert set(ends) == {MH_USER, FEE_TO}
    assert all(t["chain_id"] == 42161 and t["asset_address"] is None for t in ends.values())
    assert abs(ends[MH_USER]["amount"] - TO_USER / 1e18) < 1e-6
    assert abs(ends[FEE_TO]["amount"] - TO_FEE / 1e18) < 1e-6
    assert "multicall handler" in bridge_text(info)


def test_handler_without_an_onward_payment_ends_at_the_handler():
    _fx, result = _mh_run(internal=[])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge across (multicall handler, onward transfer not tied)"
    assert end["address"] == HANDLER and end["chain_id"] == 42161
    assert abs(end["amount"] - (TO_USER + TO_FEE) / 1e18) < 1e-6


def test_handler_paying_out_more_than_the_fill_is_not_tied():
    fx = _mh()
    doubled = [dict(r, value=str(2 * int(r["value"]))) for r in fx["fill_internal"]]
    _fx, result = _mh_run(internal=doubled)
    (end,) = result["terminals"]
    assert end["reason"] == "bridge across (multicall handler, onward transfer not tied)"


def test_handler_paying_part_of_the_fill_leaves_the_rest_at_the_handler():
    fx = _mh()
    _fx, result = _mh_run(internal=fx["fill_internal"][:1])
    ends = {t["address"]: t for t in result["terminals"]}
    assert abs(ends[MH_USER]["amount"] - TO_USER / 1e18) < 1e-6
    rest = ends[HANDLER]
    assert rest["reason"] == "bridge across (multicall handler, rest not paid on)"
    assert abs(rest["amount"] - TO_FEE / 1e18) < 1e-6


def test_two_fills_to_the_handler_in_one_transaction_are_not_split():
    fx = _mh()
    receipt = json.loads(json.dumps(fx["fill_receipt"]))
    fill_log = next(lg for lg in receipt["logs"] if lg["topics"][0] == B.TOPIC_ACROSS_FILL)
    receipt["logs"].append(dict(fill_log, logIndex=hex(99)))
    _fx, result = _mh_run(receipt=receipt)
    (end,) = result["terminals"]
    assert end["reason"] == "bridge across (multicall handler, onward transfer not tied)"


def test_handler_token_payouts_are_read_from_the_fill_receipt():
    logs = [
        _transfer(ARB_USDC, ARB_SPOKE, HANDLER, 100, 4),
        _transfer(ARB_USDC, HANDLER, OTHER, 60, 5),
        _transfer(ARB_USDC, HANDLER, "0x" + "0" * 40, 1, 6),
        _transfer(ARB_USDC, HANDLER, HANDLER, 1, 7),
        _transfer(ARB_USDC, HANDLER, RECIPIENT, 40, 2),  # before the fill log
    ]
    assert B.handler_payouts({"logs": logs}, HANDLER, ARB_USDC, 3) == [(OTHER, 60, 5)]


# --- 3. ERC-4337: a smart account deposits through an EntryPoint ------------------------

SA = "0x" + "5a" * 20
AMOUNT = 3_000_000_000  # 3000 USDC


def _aa_run(logs, frm=BUNDLER, to=EP7, rows_to=LIFI, fill_depositor=LIFI):
    row = _token_row(SA, rows_to, AMOUNT, USDC, symbol="USDC")
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, frm=frm, to=to)})
    fill = _fill(fill_depositor, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6)
    return _trace(origin, 3000.0, _dests(42161, Chain(fills=[fill])), token=USDC, user=SA)


def _aa_logs(sender=SA, success=1, entry_point=EP7, deposit_index=11, depositor=LIFI):
    return [
        _uop(OTHER, 5, entry_point=entry_point),
        _transfer(USDC, SA, LIFI, AMOUNT, 10),
        _deposit(depositor, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6, deposit_index),
        _uop(sender, 12, success, entry_point),
        _uop(OTHER, 14, entry_point=entry_point),
    ]


def _filled(result):
    return [e for e in result["edges"] if e.get("bridge", {}).get("state") == "filled"]


def test_user_operations_are_read_from_the_entry_point_logs():
    ops = B.user_operations({"logs": _aa_logs()})
    assert [(o["sender"], o["start"], o["end"]) for o in ops] == [
        (OTHER, -1, 5),
        (SA, 5, 12),
        (OTHER, 12, 14),
    ]
    assert B.operation_of({"logs": _aa_logs()}, SA, 11)["end"] == 12
    assert B.operation_of({"logs": _aa_logs()}, SA, 13) is None
    assert B.user_operations({"logs": _aa_logs(entry_point=OTHER)}) == []
    assert [o["sender"] for o in B.user_operations({"logs": _aa_logs(success=0)})] == [
        OTHER,
        OTHER,
    ]


def test_router_deposit_in_a_smart_accounts_operation_is_followed():
    result = _aa_run(_aa_logs())
    (edge,) = _filled(result)
    assert edge["from"] == SA and edge["bridge"]["via"] == LIFI


def test_router_deposit_in_another_accounts_operation_is_not_taken():
    assert not _filled(_aa_run(_aa_logs(sender=OTHER)))
    assert not _filled(_aa_run(_aa_logs(deposit_index=13)))


def test_failed_or_unlisted_entry_point_operations_do_not_count():
    assert not _filled(_aa_run(_aa_logs(success=0)))
    assert not _filled(_aa_run(_aa_logs(entry_point=OTHER), to=OTHER))


def test_smart_account_depositing_straight_into_the_spokepool_is_followed():
    logs = _aa_logs(depositor=SA)
    logs[1] = _transfer(USDC, SA, SPOKE, AMOUNT, 10)
    result = _aa_run(logs, rows_to=SPOKE, fill_depositor=SA)
    (edge,) = _filled(result)
    assert edge["to"] == SPOKE and "via" not in edge["bridge"]


# --- 4. a router depositing in its own name ---------------------------------------------


def _m5_run(logs, frm=USER, to=LIFI):
    row = _token_row(USER, LIFI, AMOUNT, USDC, symbol="USDC")
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, frm=frm, to=to)})
    fill = _fill(LIFI, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6)
    return _trace(origin, 3000.0, _dests(42161, Chain(fills=[fill])), token=USDC)


def _m5_logs(extra=()):
    return [
        _transfer(USDC, USER, LIFI, AMOUNT, 10),
        _deposit(LIFI, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6, 11),
        *extra,
    ]


def test_router_deposit_in_its_own_name_is_tied_when_the_user_called_it():
    (edge,) = _filled(_m5_run(_m5_logs()))
    assert edge["bridge"]["via"] == LIFI


def test_router_deposit_in_its_own_name_through_another_contract_is_not_tied():
    result = _m5_run(_m5_logs(), to="0x" + "66" * 20)
    assert not _filled(result)
    (end,) = result["terminals"]
    assert end["reason"] == NOT_TIED


def test_router_deposit_in_its_own_name_with_another_inflow_is_not_tied():
    result = _m5_run(_m5_logs([_transfer(USDC, OTHER, LIFI, 10**6, 12)]))
    assert not _filled(result)
    (end,) = result["terminals"]
    assert end["reason"] == NOT_TIED


# --- 5. a listed aggregator that only refunds the native coin --------------------------


def _m7_run(back, tx_value, extra_tokens=(), logs=None, fills=()):
    row = _token_row(USER, LIFI, AMOUNT, USDC, symbol="USDC")
    refund = {
        "from": LIFI,
        "to": USER,
        "value": str(back),
        "hash": TX,
        "blockNumber": str(BLOCK),
        "timeStamp": str(TS),
        "isError": "0",
        "input": "",
    }
    origin = Chain(
        tokens=[row, *extra_tokens],
        internal=[refund],
        receipts={TX: _receipt(logs or [], to=LIFI)},
        txs={TX: {"from": USER, "to": LIFI, "value": hex(tx_value)}},
    )
    return _trace(origin, 3000.0, _dests(42161, Chain(fills=list(fills))), token=USDC)


def test_native_refund_within_the_attached_value_is_bridge_or_swap_unknown():
    result = _m7_run(10**14, 2 * 10**14)
    edge = result["edges"][0]
    assert edge["kind"] == "bridge" and "swapped_to" not in edge
    assert edge["bridge"]["state"] == "bridge_or_swap_unknown"
    (end,) = result["terminals"]
    assert end["reason"] == "aggregator lifi (bridge or swap unknown)" and end["address"] == LIFI
    assert end_state(end["reason"]) == ("bridge", "bridge or swap unknown")


def test_native_payment_back_above_the_attached_value_is_a_swap():
    result = _m7_run(10**17, 0)
    assert result["edges"][0]["kind"] == "swap" and result["edges"][0]["swapped_to"] == "ETH"


def test_token_output_beside_a_native_refund_is_the_swap():
    dai = _token_row(LIFI, USER, 2999 * 10**18, DAI, decimals="18", symbol="DAI")
    result = _m7_run(10**14, 2 * 10**14, extra_tokens=[dai])
    edge = result["edges"][0]
    assert edge["kind"] == "swap" and edge["swapped_to_address"] == DAI


def test_across_deposit_through_the_aggregator_with_a_refund_is_followed():
    logs = [
        _transfer(USDC, USER, LIFI, AMOUNT, 10),
        _deposit(USER, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6, 11),
    ]
    fill = _fill(USER, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6)
    result = _m7_run(10**14, 2 * 10**14, logs=logs, fills=[fill])
    (edge,) = _filled(result)
    assert edge["bridge"]["via"] == LIFI


# --- documentation ----------------------------------------------------------------------


def test_end_states_note_names_the_new_cases():
    for text in (
        "swapped before the deposit",
        "MulticallHandler",
        "UserOperationEvent",
        "bridge or swap unknown",
        "own name",
    ):
        assert text in END_STATES_NOTE


# --- a swapped deposit must be paid by the contract the transfer went to ----------------

X = "0x" + "77" * 20
HANDLER_A = "0x0f7ae28de1c8532170ad4ee566b5801485c13a0e"
EP6 = "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"
EP8 = "0x4337084d9e255ff0702461cf8895ce9e3b5ff108"


def test_an_own_deposit_beside_an_unrelated_payment_in_one_batch_is_not_taken():
    """One batch transaction: 1000 USDT to an unlisted contract that pays nothing back,
    and a 0.5 DAI deposit the address makes straight into Across."""
    logs = [
        _transfer(USDT, USER, X, SOLD, 10),
        _transfer(DAI, USER, SPOKE, 5 * 10**17, 20),
        _deposit(USER, DAI, 5 * 10**17, ARB_USDC, 400_000, 21),
    ]
    row = _token_row(USER, X, SOLD, USDT)
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, to="0x" + "99" * 20)})
    paid = _token_row(RELAYER, RECIPIENT, 400_000, ARB_USDC, FILL_TX, 500_000_100, TS + 30)
    fill = _fill(USER, DAI, 5 * 10**17, ARB_USDC, 400_000)
    result = _trace(origin, 1000.0, _dests(42161, Chain(tokens=[paid], fills=[fill])))
    assert not _filled(result) and result["edges"][0]["kind"] == "transfer"
    (end,) = result["terminals"]
    assert end["reason"] == "contract without a payment back" and end["address"] == X


def test_a_swapped_deposit_the_periphery_did_not_pay_is_not_tied():
    logs = _swap_logs()
    logs[3] = _transfer(USDC, USER, SPOKE, BOUGHT, 77)  # paid by the address, not the periphery
    (end,) = _swap_run(logs)["terminals"]
    assert end["reason"] == NOT_TIED


def _native_swap(to_in_receipt):
    logs = [
        _transfer(USDT, USER, PERIPHERY, SOLD, 68),
        _transfer(USDT, PERIPHERY, SWAPPER, SOLD, 69),
        _deposit(USER, WETH, 4 * 10**17, ARB_WETH, 4 * 10**17, 78),  # paid as ETH: no Transfer
    ]
    row = _token_row(USER, PERIPHERY, SOLD, USDT)
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, to=to_in_receipt)})
    return _trace(origin, 1000.0, lambda chain: None)


def test_a_swapped_native_deposit_needs_the_address_to_have_called_the_contract():
    (end,) = _native_swap(PERIPHERY)["terminals"]
    assert end["reason"] == "destination_chain_not_supported"
    (end,) = _native_swap("0x" + "66" * 20)["terminals"]
    assert end["reason"] == NOT_TIED


# --- handlers: both listed deployments, token payouts, a swap before -------------------


def test_both_multicall_handler_deployments_are_listed_with_their_sources():
    for chain in (1, 10, 56, 137, 8453, 42161):
        assert B.is_multicall_handler(chain, HANDLER_A)
        assert "deployed-addresses" in B.MULTICALL_HANDLERS[(chain, HANDLER_A)]
    for chain in (1, 10, 137, 8453, 42161):
        assert B.is_multicall_handler(chain, HANDLER)
    assert B.is_multicall_handler(43114, "0x9610954acdca5ff7905f051a040ce33fe613c60e")
    assert not B.is_multicall_handler(56, HANDLER) and not B.is_multicall_handler(None, HANDLER)
    assert all(url.startswith("https://") for url in B.MULTICALL_HANDLERS.values())


OUT = AMOUNT - 10**6


def _handler_receipt(payouts):
    fill = _fill(USER, USDC, AMOUNT, ARB_USDC, OUT, recipient=HANDLER_A)
    logs = [fill, _transfer(ARB_USDC, ARB_SPOKE, HANDLER_A, OUT, 4)]
    for i, (to, amount) in enumerate(payouts):
        logs.append(_transfer(ARB_USDC, HANDLER_A, to, amount, 5 + i))
    return fill, {"status": "0x1", "logs": logs}


def test_handler_token_payouts_are_followed_end_to_end():
    pay_a = OUT * 3 // 5
    pay_b = OUT - pay_a
    fill, fill_receipt = _handler_receipt([(RECIPIENT, pay_a), (OTHER, pay_b)])
    logs = [
        _transfer(USDC, USER, SPOKE, AMOUNT, 10),
        _deposit(USER, USDC, AMOUNT, ARB_USDC, OUT, 11, recipient=HANDLER_A),
    ]
    row = _token_row(USER, SPOKE, AMOUNT, USDC, symbol="USDC")
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, to=SPOKE)})
    dest = Chain(receipts={FILL_TX: fill_receipt}, fills=[fill])
    result = _trace(origin, 3000.0, _dests(42161, dest, labels={}), token=USDC)
    (edge,) = _filled(result)
    assert edge["bridge"]["handler"] == HANDLER_A
    ends = {t["address"]: t for t in result["terminals"]}
    assert set(ends) == {RECIPIENT, OTHER}
    assert all(t["asset_address"] == ARB_USDC for t in ends.values())
    assert abs(ends[RECIPIENT]["amount"] - 3000.0 * pay_a / AMOUNT) < 1e-6
    assert abs(ends[OTHER]["amount"] - 3000.0 * pay_b / AMOUNT) < 1e-6


def _swapped_handler(receipt):
    deposits = [_deposit(USER, USDC, BOUGHT, ARB_USDC, FILLED, 78, recipient=HANDLER_A)]
    fill = _fill(USER, USDC, BOUGHT, ARB_USDC, FILLED, recipient=HANDLER_A)
    paid = _token_row(HANDLER_A, RECIPIENT, FILLED, ARB_USDC, FILL_TX, 500_000_100, TS + 30)
    dest = Chain(tokens=[paid], receipts={FILL_TX: receipt}, fills=[fill])
    if isinstance(receipt, Exception):

        def failing(tx_hash):
            raise receipt

        dest.tx_receipt = failing
    return _swap_run(_swap_logs(deposits=deposits), _dests(42161, dest, labels={}))


def test_swapped_deposit_filled_to_a_handler_is_followed_to_its_payee():
    fill = _fill(USER, USDC, BOUGHT, ARB_USDC, FILLED, recipient=HANDLER_A)
    receipt = {
        "status": "0x1",
        "logs": [fill, _transfer(ARB_USDC, HANDLER_A, RECIPIENT, FILLED, 5)],
    }
    result = _swapped_handler(receipt)
    (end,) = result["terminals"]
    assert end["address"] == RECIPIENT and abs(end["amount"] - 998.1) < 1e-6
    info = result["edges"][0]["bridge"]
    assert info["handler"] == HANDLER_A and info["swapped_before"]["input_token"] == USDC


def test_swapped_deposit_with_an_unreadable_handler_receipt_is_lookup_failed():
    result = _swapped_handler(ApiError("explorer down"))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed"


def test_native_handler_payments_without_internal_transactions_are_a_named_failure():
    _fx, result = _mh_run(no_internal=True)
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["address"] == HANDLER
    assert any("cannot list internal transactions" in json.dumps(e) for e in result["errors"])


# --- ERC-4337: every listed EntryPoint, the transfer in the deposit's operation ---------


@pytest.mark.parametrize("entry_point", [EP6, EP7, EP8])
def test_router_deposit_in_a_smart_accounts_operation_on_each_entry_point(entry_point):
    result = _aa_run(_aa_logs(entry_point=entry_point), to=entry_point)
    (edge,) = _filled(result)
    assert edge["from"] == SA


def test_traced_transfer_in_another_operation_of_the_same_account_is_not_tied():
    logs = [
        _uop(OTHER, 5),
        _transfer(USDC, SA, LIFI, AMOUNT, 10),
        _uop(SA, 12),
        _deposit(LIFI, USDC, AMOUNT, ARB_USDC, AMOUNT - 10**6, 13),
        _uop(SA, 14),
    ]
    assert not _filled(_aa_run(logs))


def _aa_native_batch(depositor):
    """One user operation of SA: 3000 USDC to the unlisted contract X, and a native ETH
    deposit into Across (no WETH Transfer) in ``depositor``'s name."""
    logs = [
        _uop(OTHER, 5),
        _transfer(USDC, SA, X, AMOUNT, 10),
        _deposit(depositor, WETH, 4 * 10**17, ARB_WETH, 4 * 10**17, 11),
        _uop(SA, 12),
    ]
    row = _token_row(SA, X, AMOUNT, USDC, symbol="USDC")
    origin = Chain(tokens=[row], receipts={TX: _receipt(logs, frm=BUNDLER, to=EP7)})
    return _trace(origin, 3000.0, lambda chain: None, token=USDC, user=SA)


def test_an_accounts_own_native_deposit_beside_a_payment_in_one_operation_is_not_tied():
    # the account paid X and deposited ETH itself: nothing shows X made the deposit
    result = _aa_native_batch(SA)
    assert all(e["kind"] != "bridge" for e in result["edges"])
    (end,) = result["terminals"]
    assert end["reason"] == "contract without a payment back" and end["address"] == X


def test_a_native_deposit_the_contract_made_in_its_own_name_in_the_operation_is_tied():
    (end,) = _aa_native_batch(X)["terminals"]
    assert end["reason"] == "destination_chain_not_supported"


def test_a_native_deposit_paid_by_the_contract_in_the_operation_is_tied():
    # the deposit is in the account's name, but X paid the SpokePool exactly the input
    # amount by an internal transfer in that transaction: X made the deposit
    logs = [
        _uop(OTHER, 5),
        _transfer(USDC, SA, X, AMOUNT, 10),
        _deposit(SA, WETH, 4 * 10**17, ARB_WETH, 4 * 10**17, 11),
        _uop(SA, 12),
    ]
    paid = _native_row(X, SPOKE, 4 * 10**17)
    other = _native_row(X, SPOKE, 4 * 10**17 - 1)
    for internal, reason in (
        ([paid], "destination_chain_not_supported"),
        ([other], "contract without a payment back"),
    ):
        row = _token_row(SA, X, AMOUNT, USDC, symbol="USDC")
        receipts = {TX: _receipt(logs, frm=BUNDLER, to=EP7)}
        origin = Chain(tokens=[row], internal=internal, receipts=receipts)
        (end,) = _trace(origin, 3000.0, lambda chain: None, token=USDC, user=SA)["terminals"]
        assert end["reason"] == reason
