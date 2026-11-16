"""Relay, Mayan, THORChain, Maya, Layerswap, NEAR Intents, Unit and Hyperliquid hops in a
trace: recognised, decoded, tied to the traced transfer and ended with their own state;
a Mayan same-chain payout goes on. Fixtures and their selection rule:
tests/test_bridges_named.py.
"""

import json
import os

import pytest

from tornado_demix import bridges_named as bn
from tornado_demix.errors import ApiError
from tornado_demix.networks import get_network
from tornado_demix.report_html import build_trace_report, end_state, terminal_bridge_text
from tornado_demix.trace import BRIDGE_STATES_NOTE, bridge_text, trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
WEI = 10**18


def _fx(name):
    with open(os.path.join(FIXTURES, f"named_{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _row(fx, frm=None, value=None, tx_hash=None):
    tx = fx["tx"]
    return {
        "from": frm or tx["from"],
        "to": tx["to"],
        "value": str(int(tx["value"], 16) if value is None else value),
        "blockNumber": str(int(tx["blockNumber"], 16)),
        "timeStamp": str(fx["ts"]),
        "hash": tx_hash or fx["tx_hash"],
        "input": tx["input"],
        "isError": "0",
        "transactionIndex": str(int(tx["transactionIndex"], 16)),
    }


class Client:
    """One chain: the traced address's rows, the fixture's receipt, and optional
    histories of other addresses (``others``: {address: (native rows, token rows)})."""

    def __init__(self, fx, user, rows, receipt=None, others=None):
        self.fx, self.user, self.rows = fx, user, rows
        self.receipt = fx["receipt"] if receipt is None else receipt
        self.others = others or {}

    def _split(self, addr):
        if addr == self.user:
            native = [r for r in self.rows if "contractAddress" not in r]
            tokens = [r for r in self.rows if "contractAddress" in r]
            return native, tokens
        return self.others.get(addr, ([], []))

    def token_transfers(self, addr, contract=None, **kw):
        return [r for r in self._split(addr)[1] if contract in (None, r["contractAddress"])]

    def outgoing_txs(self, addr):
        return list(self._split(addr)[0])

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt if tx_hash == self.fx["tx_hash"] else None


def _no_dest(chain):
    return None


def _run(name, user=None, rows=None, receipt=None, chain=1, others=None, follow=True, **kw):
    fx = _fx(name)
    if rows is None:
        rows = fx.get("l1_rows") or [_row(fx, frm=user)]
    user = user or rows[0]["from"]
    token = rows[0].get("contractAddress")
    decimals = int(rows[0].get("tokenDecimal") or 18)
    amount = sum(int(r["value"]) for r in rows[:1]) / 10**decimals
    result = trace_funds(
        Client(fx, user, rows, receipt, others),
        user,
        amount,
        start_block=int(fx["tx"]["blockNumber"], 16) - 5,
        token=token,
        chain_id=chain,
        destinations=_no_dest if follow else None,
        **kw,
    )
    return fx, result


def _end(result):
    (end,) = result["terminals"]
    return end


# --- recognised and ended with their own state ------------------------------------------


def test_relay_depository_deposit_ends_with_the_request_id():
    fx, result = _run("relay_depository_native")
    end = _end(result)
    rid = "0x" + fx["tx"]["input"][-64:]
    assert end["reason"] == f"bridge relay (request {rid}, destination not on chain)"
    info = end["bridge"]
    assert info["protocol"] == "relay" and info["state"] == bn.DEST_NOT_ON_CHAIN
    assert info["request_id"] == rid and info["depositor"] == fx["tx"]["from"]
    assert result["edges"][0]["kind"] == "bridge"
    assert abs(end["amount"] - int(fx["tx"]["value"], 16) / WEI) < 1e-9


def test_relay_receiver_payment_ends_with_the_forwarded_data():
    fx, result = _run("relay_receiver")
    end = _end(result)
    assert end["bridge"]["kind"] == "receiver"
    assert end["bridge"]["request_id"] == fx["tx"]["input"]
    assert end["reason"].startswith("bridge relay (request 0x1650130b")


def test_thorchain_deposit_ends_with_the_memo_decoded_never_followed():
    fx, result = _run("thorchain_deposit")
    end = _end(result)
    rec = "thor17f9d0w3y2j9a6s83vz0xfssdmdwe5yzkkxvfqx"
    assert end["reason"] == f"bridge thorchain (TRADE+, recipient {rec})"
    info = end["bridge"]
    assert info["state"] == bn.VAULT_PAYOUT and info["memo_action"] == "TRADE+"
    assert info["dest_recipient"] == rec and info["memo"].startswith("TRADE+:")
    assert info["vault"] == "0x718f6c9df785bc4e323086de024ca822efbe8ab9"


def test_maya_swap_ends_with_target_chain_and_recipient():
    fx, result = _run("maya_deposit")
    end = _end(result)
    rec = "0x44B6eC12D65213d310f6B899B1da8f1987e66E19"
    assert end["reason"] == f"bridge maya (to ETH, recipient {rec})"
    info = end["bridge"]
    assert info["dest_asset"] == "ETH.USDT" and info["dest_chain_id"] == 1
    assert info["memo_refund"] == rec and info["state"] == bn.VAULT_PAYOUT


def test_mayan_swift_order_ends_with_solana_recipient():
    fx, result = _run("mayan_swift")
    end = _end(result)
    rec = "E17hHQ6qcqHDMRcidjaFn8UtocwWg1593Uka1RxYqu1J"
    assert end["reason"] == f"bridge mayan (to Solana, recipient {rec})"
    info = end["bridge"]
    assert info["state"] == bn.FULFIL_NOT_FOLLOWED and info["function"] == "createOrderWithToken"
    assert info["dest_chain"] == "Solana" and info["dest_chain_id"] is None
    assert info["middle_token"] == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def test_mayan_erc20_order_to_an_evm_chain_shows_the_evm_recipient():
    fx, result = _run("mayan_forward_erc20")
    end = _end(result)
    row = fx["l1_rows"][0]
    assert end["reason"] == f"bridge mayan (to HyperEVM, recipient {row['from']})"
    assert end["bridge"]["dest_chain_id"] == 999
    assert end["asset_address"] == row["contractAddress"]


def test_mayan_undecoded_call_is_named_with_the_mayan_contract():
    fx, result = _run("mayan_forward_eth")
    end = _end(result)
    proto = "0xbf5f3f65102ae745a48bd521d10bab5bf02a9ef4"
    assert end["reason"] == f"bridge mayan (via {proto}, order not decoded)"
    assert end["bridge"]["state"] == bn.ORDER_NOT_DECODED
    assert end["bridge"]["call_selector"] == "0x1eb1cff0"


@pytest.mark.parametrize(
    "name, reason",
    [
        (
            "layerswap_maker",
            "bridge layerswap (maker 0x2fc617e933a52713247ce25730f6695920b3befe, matched off chain)",
        ),
        (
            "near_intents",
            "bridge near-intents (deposit to 0x2cff890f0378a11913b6129b2e97417a2c302680, "
            "matched off chain)",
        ),
        ("unit_treasury", "bridge unit (to Hyperliquid, recipient assigned off chain)"),
    ],
)
def test_transfer_to_a_maker_or_treasury_is_matched_off_chain(name, reason):
    fx, result = _run(name, receipt=ApiError("no receipt is read for a maker"))
    end = _end(result)
    assert end["reason"] == reason
    assert end["bridge"]["state"] == bn.OFF_CHAIN_MATCHING
    assert end["bridge"]["maker"] == fx["tx"]["to"]
    assert result["error_count"] == 0


def test_layerswap_reference_is_the_call_data_sent_with_the_transfer():
    fx, result = _run("layerswap_maker")
    assert _end(result)["bridge"]["reference"] == fx["tx"]["input"]
    _fx_, plain = _run("near_intents")
    assert "reference" not in _end(plain)["bridge"]


def test_hyperliquid_bridge2_usdc_is_credited_to_the_sender_on_hypercore():
    fx, result = _run("hyperliquid_bridge2", chain=42161)
    end = _end(result)
    sender = fx["l1_rows"][0]["from"]
    assert end["reason"] == f"bridge hyperliquid (recipient {sender}, credited on HyperCore)"
    assert end["bridge"]["state"] == bn.CREDITED_OFF_EVM
    assert end["bridge"]["dest_recipient"] == sender
    assert "below_minimum" not in end["bridge"]


def test_hyperliquid_below_minimum_is_flagged_and_other_tokens_are_not_credited():
    fx = _fx("hyperliquid_bridge2")
    small = [dict(fx["l1_rows"][0], value="4000000")]
    _fx_, result = _run("hyperliquid_bridge2", rows=small, chain=42161)
    assert _end(result)["bridge"]["below_minimum"] is True
    other = [dict(fx["l1_rows"][0], contractAddress="0x" + "ab" * 20)]
    _fx_, result = _run("hyperliquid_bridge2", rows=other, chain=42161)
    assert _end(result)["reason"] == "bridge hyperliquid (not followed)"


# --- not tied: named only ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["relay_depository_native", "thorchain_deposit", "maya_deposit", "mayan_swift"]
)
def test_amount_not_tied_is_named_only(name):
    fx = _fx(name)
    rows = [_row(fx, value=int(fx["tx"]["value"], 16) - 1)]
    _fx_, result = _run(name, rows=rows)
    assert _end(result)["reason"] == f"bridge {_end(result)['bridge']['protocol']} (not followed)"


@pytest.mark.parametrize(
    "name",
    ["relay_receiver", "thorchain_deposit", "maya_deposit", "mayan_swift", "mayan_forward_eth"],
)
def test_transaction_sent_by_someone_else_is_named_only(name):
    fx = _fx(name)
    other = "0x" + "77" * 20
    _fx_, result = _run(name, user=other, rows=[_row(fx, frm=other)])
    assert _end(result)["reason"].endswith("(not followed)")


def test_mayan_erc20_order_of_another_token_is_named_only():
    fx = _fx("mayan_forward_erc20")
    rows = [dict(fx["l1_rows"][0], contractAddress="0x" + "ab" * 20)]
    _fx_, result = _run("mayan_forward_erc20", rows=rows)
    assert _end(result)["reason"] == "bridge mayan (not followed)"


def test_each_log_is_taken_by_one_transfer():
    fx = _fx("thorchain_deposit")
    first = _row(fx)
    second = dict(first)  # the same log cannot pay for a second transfer
    amount = 2 * int(first["value"]) / WEI
    client = Client(fx, first["from"], [first, second])
    result = trace_funds(
        client,
        first["from"],
        amount,
        start_block=int(fx["tx"]["blockNumber"], 16) - 5,
        chain_id=1,
        destinations=_no_dest,
    )
    reasons = sorted(t["reason"] for t in result["terminals"])
    assert len(reasons) == 2 and reasons[0].startswith("bridge thorchain (TRADE+"), reasons
    assert reasons[1] == "bridge thorchain (not followed)"


def test_receipt_failure_is_lookup_failed_with_the_protocol():
    fx, result = _run("thorchain_deposit", receipt=ApiError("explorer down"))
    end = _end(result)
    assert end["reason"] == "lookup_failed"
    assert end["bridge"]["protocol"] == "thorchain" and end["bridge"]["state"] == "lookup_failed"
    assert result["error_count"] >= 1


def test_without_destinations_the_bridge_is_named_only():
    fx, result = _run("thorchain_deposit", follow=False)
    assert _end(result)["reason"] == "bridge thorchain (not followed)"


# --- Mayan same-chain payout (MonoChainSwap) --------------------------------------------

MONO = "0x8a44fb98a2ad415809e6f8f57e5ca8fa2747ff3e"
FWD = "0x337685fdab40d39bd02028545a4ffa7d287cc3e2"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"


def _w(n):
    return "{:064x}".format(n)


def _mono_fixture(user, payee, amount_in, usdt_out):
    """A synthetic swapAndForwardEth to MonoChainSwap: ETH swapped to USDT, paid to ``payee``."""
    call = "29d8eb0a" + _w(int(USDT, 16)) + _w(usdt_out) + _w(int(payee, 16)) + _w(0) + _w(0)
    calldata = bytes.fromhex(call)
    data = (
        _w(amount_in)
        + _w(int("ee" * 20, 16))
        + _w(int(USDT, 16))
        + _w(usdt_out)
        + _w(int(MONO, 16))
        + _w(6 * 32)
        + _w(len(calldata))
        + calldata.hex().ljust(-(-len(calldata) // 32) * 64, "0")
    )
    tx_hash = "0x" + "5a" * 32
    receipt = {
        "status": "0x1",
        "from": user,
        "to": FWD,
        # as on chain: MonoChainSwap pays out, then the forwarder logs the forward
        "logs": [
            {
                "address": MONO,
                "topics": [bn.TOPIC_MONO_TOKEN],
                "data": "0x" + _w(int(USDT, 16)) + _w(int(payee, 16)) + _w(usdt_out),
                "logIndex": "0x4",
            },
            {
                "address": FWD,
                "topics": [bn.TOPIC_MAYAN_SWAP_ETH],
                "data": "0x" + data,
                "logIndex": "0x5",
            },
        ],
    }
    tx = {
        "from": user,
        "to": FWD,
        "value": hex(amount_in),
        "input": "0xfa74fd43",
        "blockNumber": hex(26_300_100),
        "transactionIndex": "0x3",
    }
    return {"tx_hash": tx_hash, "ts": 1_793_300_000, "tx": tx, "receipt": receipt}


def test_mayan_same_chain_payout_continues_from_the_payee_in_the_new_token():
    user, payee = "0x" + "a1" * 20, "0x" + "b2" * 20
    fx = _mono_fixture(user, payee, 2 * WEI, 6_000_000_000)
    usdt_in = {
        "from": MONO,
        "to": payee,
        "value": "6000000000",
        "contractAddress": USDT,
        "tokenSymbol": "USDT",
        "tokenDecimal": "6",
        "blockNumber": "26300100",
        "timeStamp": str(fx["ts"]),
        "hash": fx["tx_hash"],
        "transactionIndex": "3",
    }
    client = Client(fx, user, [_row(fx)], others={payee: ([], [usdt_in])})
    result = trace_funds(
        client, user, 2.0, start_block=26_300_000, chain_id=1, destinations=_no_dest
    )
    (edge,) = [e for e in result["edges"] if e["from"] == user]
    assert edge["kind"] == "swap" and edge["swapped_to"] == "USDT"
    assert edge["paid_to"] == payee and edge["bridge"]["state"] == bn.SAME_CHAIN
    (end,) = result["terminals"]
    assert end["address"] == payee and end["asset_address"] == USDT
    assert abs(end["amount"] - 6000.0) < 1e-6


def test_mayan_same_chain_payout_not_in_the_receipt_ends_the_branch():
    user, payee = "0x" + "a1" * 20, "0x" + "b2" * 20
    fx = _mono_fixture(user, payee, 2 * WEI, 6_000_000_000)
    fx["receipt"]["logs"] = [x for x in fx["receipt"]["logs"] if x["address"] == FWD]
    client = Client(fx, user, [_row(fx)])
    result = trace_funds(
        client, user, 2.0, start_block=26_300_000, chain_id=1, destinations=_no_dest
    )
    assert _end(result)["reason"] == f"bridge mayan (same chain to {payee}, payout not found)"
    assert _end(result)["bridge"]["state"] == bn.PAYOUT_NOT_FOUND


def _eth_forward(payee, amount_in, index):
    """A swapAndForwardEth log of MonoChainSwap.transferEth(payee) at log ``index``."""
    calldata = bytes.fromhex("6eb183d0" + _w(int(payee, 16)) + _w(0) + _w(0))
    data = (
        _w(amount_in)
        + _w(int("ee" * 20, 16))
        + _w(0)
        + _w(amount_in)
        + _w(int(MONO, 16))
        + _w(6 * 32)
        + _w(len(calldata))
        + calldata.hex().ljust(-(-len(calldata) // 32) * 64, "0")
    )
    return {
        "address": FWD,
        "topics": [bn.TOPIC_MAYAN_SWAP_ETH],
        "data": "0x" + data,
        "logIndex": hex(index),
    }


def _eth_payout(payee, amount, index):
    return {
        "address": MONO,
        "topics": [bn.TOPIC_MONO_ETH],
        "data": "0x" + _w(int(payee, 16)) + _w(amount),
        "logIndex": hex(index),
    }


def test_two_forwards_to_the_same_payee_each_take_their_own_payout():
    user, payee = "0x" + "a1" * 20, "0x" + "b2" * 20
    fx = _mono_fixture(user, payee, WEI, 1)
    # as on chain: each payout is logged just before its own forward
    fx["receipt"]["logs"] = [
        _eth_payout(payee, WEI // 2, 1),
        _eth_forward(payee, WEI, 2),
        _eth_payout(payee, WEI // 4, 3),
        _eth_forward(payee, WEI, 4),
    ]
    row = _row(fx)
    client = Client(fx, user, [row, dict(row)])
    result = trace_funds(
        client, user, 2.0, start_block=26_300_000, chain_id=1, destinations=_no_dest
    )
    edges = [e for e in result["edges"] if e["from"] == user]
    assert len(edges) == 2 and all(e["paid_to"] == payee for e in edges)
    assert all(e["kind"] == "transfer" for e in edges)  # ETH passed on, no swap
    at_payee = sum(t["amount"] for t in result["terminals"] if t["address"] == payee)
    assert abs(at_payee - 0.75) < 1e-9, result["terminals"]


def test_a_forward_whose_own_payout_is_missing_takes_no_other_payout():
    user, payee = "0x" + "a1" * 20, "0x" + "b2" * 20
    fx = _mono_fixture(user, payee, WEI, 1)
    # the first forward has no payout before it; the payout after it is the second's
    # (1 ETH, then 0.5 ETH: the forwards are told apart by their amounts)
    fx["receipt"]["logs"] = [
        _eth_forward(payee, WEI, 1),
        _eth_payout(payee, WEI // 4, 2),
        _eth_forward(payee, WEI // 2, 3),
    ]
    row = _row(fx)
    client = Client(fx, user, [row, _row(fx, value=WEI // 2)])
    result = trace_funds(
        client, user, 1.5, start_block=26_300_000, chain_id=1, destinations=_no_dest
    )
    missing = [t for t in result["terminals"] if t["reason"].endswith("payout not found)")]
    assert len(missing) == 1 and abs(missing[0]["amount"] - 1.0) < 1e-9
    assert missing[0]["bridge"]["state"] == bn.PAYOUT_NOT_FOUND
    at_payee = sum(t["amount"] for t in result["terminals"] if t["address"] == payee)
    assert abs(at_payee - 0.25) < 1e-9, result["terminals"]


def test_payout_for_takes_the_nearest_before_and_after_the_previous_forward():
    p = [{"log_index": 1}, {"log_index": 4}, {"log_index": 9}]
    assert bn.payout_for(p, 5) == {"log_index": 4}
    assert bn.payout_for(p, 5, previous=3) == {"log_index": 4}
    assert bn.payout_for(p, 5, previous=4) is None  # 4 is not after the previous forward
    assert bn.payout_for(p, 0) is None  # nothing before: no payout after it is taken
    assert bn.payout_for([], 5) is None
    assert bn.payout_for(p[2:], 5) is None


# --- shown in every view ----------------------------------------------------------------


def test_bridge_text_shows_chain_recipient_memo_and_request():
    _fx_, result = _run("maya_deposit")
    info = _end(result)["bridge"]
    text = bridge_text(info)
    assert text.startswith("bridge maya → chain 1")
    assert "memo =:ETH.USDT:" in text and "recipient 0x44B6eC12" in text
    assert "vault_payout_not_followed" in text
    _fx_, result = _run("mayan_swift")
    text = bridge_text(_end(result)["bridge"])
    assert text.startswith("bridge mayan → Solana") and "E17hHQ6qcq" in text
    fx, result = _run("relay_depository_native")
    assert "request 0x" + fx["tx"]["input"][-64:] in bridge_text(_end(result)["bridge"])
    _fx_, result = _run("layerswap_maker")
    assert "reference 0xc8d430a1" in bridge_text(_end(result)["bridge"])


def test_terminal_line_lists_the_recipient_and_the_badge_is_bridge():
    _fx_, result = _run("mayan_swift")
    end = _end(result)
    assert "recipient E17hHQ6qcqHDMRcidjaFn8UtocwWg1593Uka1RxYqu1J" in terminal_bridge_text(end)
    assert end_state(end["reason"]) == ("bridge", "bridge")


def test_states_note_names_every_new_end_state():
    for words in (
        "destination not on chain",
        "bridge mayan (to <chain>, recipient",
        "bridge thorchain (to <chain>, recipient",
        "matched off chain",
        "credited on HyperCore",
        "MonoChainSwap",
    ):
        assert words in BRIDGE_STATES_NOTE, words


@pytest.mark.parametrize("name", ["mayan_swift", "thorchain_deposit", "hyperliquid_bridge2"])
def test_html_report_shows_the_recipient_in_full(name):
    chain = 42161 if name.startswith("hyperliquid") else 1
    fx, result = _run(name, chain=chain)
    net = get_network("arbitrum" if chain == 42161 else "ethereum")
    html = build_trace_report(result, net)
    rec = _end(result)["bridge"]["dest_recipient"]
    assert rec in html
    assert 'class="badge bridge"' in html


def test_html_report_names_the_payee_of_a_same_chain_payout():
    user, payee = "0x" + "a1" * 20, "0x" + "b2" * 20
    fx = _mono_fixture(user, payee, 2 * WEI, 6_000_000_000)
    usdt_in = {
        "from": MONO,
        "to": payee,
        "value": "6000000000",
        "contractAddress": USDT,
        "tokenSymbol": "USDT",
        "tokenDecimal": "6",
        "blockNumber": "26300100",
        "timeStamp": str(fx["ts"]),
        "hash": fx["tx_hash"],
        "transactionIndex": "3",
    }
    client = Client(fx, user, [_row(fx)], others={payee: ([], [usdt_in])})
    result = trace_funds(
        client, user, 2.0, start_block=26_300_000, chain_id=1, destinations=_no_dest
    )
    html = build_trace_report(result, get_network("ethereum"))
    assert "paid out by mayan to" in html and payee in html


# --- THORChain memo forms in a trace -----------------------------------------------------


def _with_memo(fx, memo):
    """The fixture's receipt with the router log's memo replaced."""
    receipt = json.loads(json.dumps(fx["receipt"]))
    log = receipt["logs"][0]
    raw = log["data"][2:]
    body = memo.encode().hex()
    padded = body.ljust(-(-len(body) // 64) * 64, "0") if body else ""
    log["data"] = "0x" + raw[:64] + _w(64) + _w(len(memo.encode())) + padded
    return receipt


@pytest.mark.parametrize(
    "memo, reason, chain, asset",
    [
        (
            "=:BTC~BTC:thor1dest",
            "bridge thorchain (to THORChain (BTC~BTC), recipient thor1dest)",
            "THORChain",
            "BTC~BTC",
        ),
        (
            "=:BTC/BTC:thor1dest",
            "bridge thorchain (to THORChain (BTC/BTC), recipient thor1dest)",
            "THORChain",
            "BTC/BTC",
        ),
        (
            "=:BTC-BTC:thor1dest",
            "bridge thorchain (to THORChain (BTC-BTC), recipient thor1dest)",
            "THORChain",
            "BTC-BTC",
        ),
        ("=>:b:bc1qdest:0/1/0", "bridge thorchain (to BTC, recipient bc1qdest)", "BTC", "BTC.BTC"),
        ("=:ETH.ETH", "bridge thorchain (to ETH, pays the originator)", "ETH", "ETH.ETH"),
        ("LOAN+:e:0xabc", "bridge thorchain (LOAN+)", None, None),
    ],
)
def test_router_memo_forms_end_honestly(memo, reason, chain, asset):
    fx = _fx("thorchain_deposit")
    _fx_, result = _run("thorchain_deposit", receipt=_with_memo(fx, memo))
    end = _end(result)
    assert end["reason"] == reason
    assert end["bridge"]["memo"] == memo
    assert end["bridge"].get("dest_chain") == chain
    assert end["bridge"].get("dest_asset") == asset
    if "originator" in reason:
        assert end["bridge"]["pays_originator"] is True
        assert "dest_recipient" not in end["bridge"]
    if memo.startswith("LOAN+"):
        assert "dest_recipient" not in end["bridge"]


@pytest.mark.parametrize(
    "memo, reason",
    [
        ("wd:e:100", "bridge thorchain (WITHDRAW ETH.ETH pool)"),
        ("+:BTC.BTC:bc1qpaired", "bridge thorchain (ADD BTC.BTC pool, paired address bc1qpaired)"),
        ("d:e", "bridge thorchain (DONATE ETH.ETH pool)"),
    ],
)
def test_pool_memos_name_the_pool_and_no_destination(monkeypatch, memo, reason):
    real = bn.decode_router_deposits

    def with_memo(receipt, chain_id, table):
        out = real(receipt, chain_id, table)
        for d in out:
            d["memo"], d["parsed"] = memo, bn.parse_memo(memo, d["protocol"])
        return out

    monkeypatch.setattr(bn, "decode_router_deposits", with_memo)
    _fx, result = _run("thorchain_deposit")
    end = _end(result)
    assert end["reason"] == reason
    info = end["bridge"]
    assert info["memo"] == memo and info["pool"]
    for key in ("dest_chain", "dest_recipient", "dest_asset"):
        assert key not in info
    assert "recipient" not in bridge_text(info, lambda c: str(c))
