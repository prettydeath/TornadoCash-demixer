"""Decoders of bridges_named.py: Relay, Mayan, THORChain, Maya, and the table rows of
Layerswap, NEAR Intents, Unit and Hyperliquid.

Fixtures (tests/fixtures/bridges/named_*.json): real transactions picked by a fixed
rule, the first successful matching transaction at or after Ethereum block 26,300,000
(Arbitrum block 524,000,000 for Hyperliquid), skipping any whose addresses (sender,
log and calldata addresses, also packed ones) appear in the project's private data
other than protocol or token contracts:

* relay_depository_native: first ``depositNative`` to the RelayDepository.
* relay_receiver: first payment to the RelayReceiver at or after block 26,000,000 (none
  since 26,300,000).
* thorchain_deposit / maya_deposit: first native ``depositWithExpiry`` to each router.
* mayan_swift: second SwiftSource order made through ``swapAndForwardEth`` (the first
  sender failed the check above); mayan_forward_erc20: third ``forwardERC20`` (the first
  two carried an address that failed it); mayan_forward_eth: first ``forwardEth``.
* layerswap_maker: first transfer with call data to the Layerswap maker; near_intents /
  unit_treasury: first transfer to the NEAR Intents and Unit (Ethereum) addresses.
* hyperliquid_bridge2: first USDC transfer to Bridge2 on Arbitrum.
"""

import json
import os

import pytest

from tornado_demix import bridges_named as bn
from tornado_demix.bridges import load_bridges
from tornado_demix.keccak import keccak256

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()


def _fx(name):
    with open(os.path.join(FIXTURES, f"named_{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _sel(sig):
    return "0x" + keccak256(sig.encode()).hex()[:8]


def _topic(sig):
    return "0x" + keccak256(sig.encode()).hex()


# --- selectors, topics and table rows ---------------------------------------------------


def test_topics_are_keccak_of_the_event_signatures():
    assert bn.TOPIC_RELAY_NATIVE == _topic("RelayNativeDeposit(address,uint256,bytes32)")
    assert bn.TOPIC_RELAY_ERC20 == _topic("RelayErc20Deposit(address,address,uint256,bytes32)")
    assert bn.TOPIC_RELAY_FORWARDED == _topic("FundsForwardedWithData(bytes)")
    assert bn.TOPIC_ROUTER_DEPOSIT == _topic("Deposit(address,address,uint256,string)")
    assert bn.TOPIC_MAYAN_FORWARDED_ETH == _topic("ForwardedEth(address,bytes)")
    assert bn.TOPIC_MAYAN_FORWARDED_ERC20 == _topic("ForwardedERC20(address,uint256,address,bytes)")
    assert bn.TOPIC_MAYAN_SWAP_ETH == _topic(
        "SwapAndForwardedEth(uint256,address,address,uint256,address,bytes)"
    )
    assert bn.TOPIC_MAYAN_SWAP_ERC20 == _topic(
        "SwapAndForwardedERC20(address,uint256,address,address,uint256,address,bytes)"
    )
    assert bn.TOPIC_MONO_ETH == _topic("EthTransferred(address,uint256)")
    assert bn.TOPIC_MONO_TOKEN == _topic("TokenTransferred(address,address,uint256)")


def test_mayan_call_selectors_are_keccak_of_the_verified_signatures():
    swift = "(uint8,bytes32,bytes32,uint16,bytes32,bytes32,uint64,uint64,uint64,uint64,uint64,uint8,uint8,bytes32)"
    sigs = {
        f"createOrderWithToken(address,uint256,{swift},bytes)": "createOrderWithToken",
        "createOrder(address,uint256,uint256,uint32,uint32,"
        "(uint8,bytes32,bytes32,uint64,uint64,uint64,uint64,uint64,bytes32,uint8))": "createOrder",
        "bridge(address,uint256,uint64,uint256,uint64,bytes32,uint32,bytes32,uint8,uint8,uint32,bytes)": "bridge",
        "bridgeWithFee(address,uint256,uint64,uint64,bytes32,uint32,uint8,bytes)": "bridgeWithFee",
        "bridgeWithLockedFee(address,uint256,uint64,uint256,uint32,bytes32)": "bridgeWithLockedFee",
        "createOrder((address,uint256,uint64,bytes32,uint16,bytes32,uint64,uint64,uint64,bytes32,uint8))": "createOrder",
        "transferEth(address,address,uint8)": "transferEth",
        "transferToken(address,uint256,address,address,uint8)": "transferToken",
    }
    assert {_sel(s): name for s, name in sigs.items()} == bn.MAYAN_CALLS


@pytest.mark.parametrize(
    "chain, address, protocol, kind",
    [
        (1, "0x4cd00e387622c35bddb9b4c962c136462338bc31", "relay", "contract"),
        (1, "0xa5f565650890fba1824ee0f21ebbbf660a179934", "relay", "contract"),
        (1, "0xd37bbe5744d730a1d98d8dc97c42f0ca46ad7146", "thorchain", "contract"),
        (1, "0xe3985e6b61b814f7cdb188766562ba71b446b46d", "maya", "contract"),
        (42161, "0x700e97ef07219440487840dc472e7120a7ff11f4", "maya", "contract"),
        (1, "0x337685fdab40d39bd02028545a4ffa7d287cc3e2", "mayan", "contract"),
        (42161, "0x337685fdab40d39bd02028545a4ffa7d287cc3e2", "mayan", "contract"),
        (1, "0x2fc617e933a52713247ce25730f6695920b3befe", "layerswap", "maker_eoa"),
        (1, "0x2cff890f0378a11913b6129b2e97417a2c302680", "near-intents", "maker_eoa"),
        (1, "0xbea9f7fd27f4ee20066f18def0bc586ec221055a", "unit", "maker_eoa"),
        (42161, "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7", "hyperliquid", "contract"),
    ],
)
def test_table_rows_name_the_protocol_with_a_source(chain, address, protocol, kind):
    row = TABLE[(chain, address)]
    assert row["protocol"] == protocol and row["kind"] == kind
    assert row["source_url"].startswith("https://")
    assert protocol in bn.PROTOCOLS


def test_base58_is_the_solana_form():
    assert bn.base58(b"\0" * 32) == "1" * 32
    assert bn.base58(bytes([0, 0, 1])) == "112"
    assert bn.base58(bytes.fromhex("ff")) == "5Q"


# --- Relay ------------------------------------------------------------------------------


def test_relay_depository_native_deposit_gives_depositor_amount_and_request_id():
    fx = _fx("relay_depository_native")
    (d,) = bn.decode_relay_deposits(fx["receipt"], 1, TABLE)
    assert d["kind"] == "depository" and d["emitter"] == fx["tx"]["to"]
    assert d["depositor"] == fx["tx"]["from"] and d["token"] is None
    assert d["amount"] == int(fx["tx"]["value"], 16)
    assert d["request_id"] == "0x" + fx["tx"]["input"][-64:]


def test_relay_receiver_payment_keeps_the_forwarded_data():
    fx = _fx("relay_receiver")
    (d,) = bn.decode_relay_deposits(fx["receipt"], 1, TABLE)
    assert d["kind"] == "receiver" and d["amount"] is None and d["depositor"] is None
    assert d["request_id"] == fx["tx"]["input"]  # the receiver logs the call data it got


def test_relay_logs_of_an_unlisted_emitter_or_a_failed_receipt_count_for_nothing():
    fx = _fx("relay_depository_native")
    forged = json.loads(json.dumps(fx["receipt"]))
    forged["logs"][0]["address"] = "0x" + "12" * 20
    assert bn.decode_relay_deposits(forged, 1, TABLE) == []
    assert bn.decode_relay_deposits(dict(fx["receipt"], status="0x0"), 1, TABLE) == []
    assert bn.decode_relay_deposits(fx["receipt"], 42161, TABLE) == []  # other chain


# --- THORChain / Maya -------------------------------------------------------------------


def test_thorchain_router_deposit_and_its_memo():
    fx = _fx("thorchain_deposit")
    (d,) = bn.decode_router_deposits(fx["receipt"], 1, TABLE)
    assert d["protocol"] == "thorchain" and d["router"] == fx["tx"]["to"]
    assert d["asset"] is None and d["amount"] == int(fx["tx"]["value"], 16)
    assert d["memo"] == "TRADE+:thor17f9d0w3y2j9a6s83vz0xfssdmdwe5yzkkxvfqx"
    assert d["parsed"]["action"] == "TRADE+"
    assert d["parsed"]["recipient"] == "thor17f9d0w3y2j9a6s83vz0xfssdmdwe5yzkkxvfqx"
    assert d["parsed"]["chain"] == "THOR"


def test_maya_router_swap_memo_with_refund_and_streaming_limit():
    fx = _fx("maya_deposit")
    (d,) = bn.decode_router_deposits(fx["receipt"], 1, TABLE)
    assert d["protocol"] == "maya" and d["amount"] == int(fx["tx"]["value"], 16)
    p = d["parsed"]
    assert p["action"] == "SWAP" and p["asset"] == "ETH.USDT" and p["chain"] == "ETH"
    assert p["recipient"] == "0x44B6eC12D65213d310f6B899B1da8f1987e66E19"
    assert p["refund"] == p["recipient"] and p["limit"] == "30706400/1/0"
    assert bn.memo_dest_chain_id(p) == 1


@pytest.mark.parametrize(
    "memo, protocol, want",
    [
        ("SWAP:BTC.BTC:bc1qexample", "thorchain", ("SWAP", "BTC.BTC", "BTC", "bc1qexample")),
        ("=:b:bc1qexample:0/1/0", "thorchain", ("SWAP", "BTC.BTC", "BTC", "bc1qexample")),
        ("s:e:0x" + "ab" * 20, "thorchain", ("SWAP", "ETH.ETH", "ETH", "0x" + "ab" * 20)),
        ("=:BTC~BTC:thor1x", "thorchain", ("SWAP", "BTC~BTC", "THOR", "thor1x")),
        ("=:BTC/BTC:thor1x", "thorchain", ("SWAP", "BTC/BTC", "THOR", "thor1x")),
        ("=:BTC-BTC:thor1x", "thorchain", ("SWAP", "BTC-BTC", "THOR", "thor1x")),
        ("=:BTC~BTC:maya1x", "maya", ("SWAP", "BTC~BTC", "MAYA", "maya1x")),
        ("=:ETH/ETH:0x" + "ab" * 20, "thorchain", ("SWAP", "ETH/ETH", "THOR", "0x" + "ab" * 20)),
        ("=:ETH.ETH:thor1x", "thorchain", ("SWAP", "ETH.ETH", "THOR", "thor1x")),
        ("=>:e:0x" + "ab" * 20, "thorchain", ("SWAP", "ETH.ETH", "ETH", "0x" + "ab" * 20)),
        ("=<:b:bc1q", "thorchain", ("SWAP", "BTC.BTC", "BTC", "bc1q")),
        ("=:ETH.ETH", "thorchain", ("SWAP", "ETH.ETH", "ETH", None)),
        ("TRADE+:thor1abc", "thorchain", ("TRADE+", None, "THOR", "thor1abc")),
        ("POOL+", "thorchain", ("POOL+", None, None, None)),
        ("m=<:1:2:3", "thorchain", ("M=<", None, None, None)),
        ("=:b:bc1qexample", "maya", ("SWAP", "b", None, "bc1qexample")),  # no Maya short names
        ("+:BTC.BTC:bc1qpaired", "thorchain", ("ADD", "BTC.BTC", "BTC", "bc1qpaired")),
        ("-:ETH.ETH:10000", "thorchain", ("WITHDRAW", "ETH.ETH", "ETH", None)),
        ("wd:e:100", "thorchain", ("WITHDRAW", "ETH.ETH", "ETH", None)),
        ("a:b:bc1qpaired", "thorchain", ("ADD", "BTC.BTC", "BTC", "bc1qpaired")),
        ("d:e", "thorchain", ("DONATE", "ETH.ETH", "ETH", None)),
        ("wd:e:100", "maya", ("WITHDRAW", "e", None, None)),  # no Maya short names
        ("LOAN+:BTC.BTC:bc1q", "thorchain", ("LOAN+", None, None, None)),  # memo only
        ("LOAN+:e:0xab", "thorchain", ("LOAN+", None, None, None)),
        ("SECURE+:thor1abc", "thorchain", ("SECURE+", None, "THOR", "thor1abc")),
        ("SECURE+:bc1qabc", "thorchain", ("SECURE+", None, None, "bc1qabc")),
        ("", "thorchain", (None, None, None, None)),
    ],
)
def test_parse_memo_without_guessing(memo, protocol, want):
    p = bn.parse_memo(memo, protocol)
    assert (p["action"], p["asset"], p["chain"], p["recipient"]) == want


def test_pool_actions_show_the_pool_not_a_destination():
    for memo, text in (
        ("wd:e:100", "WITHDRAW ETH.ETH pool"),
        ("-:ETH.ETH:10000", "WITHDRAW ETH.ETH pool"),
        ("d:e", "DONATE ETH.ETH pool"),
        ("+:BTC.BTC:bc1qpaired", "ADD BTC.BTC pool, paired address bc1qpaired"),
        ("ADD:BTC.BTC", "ADD BTC.BTC pool"),
    ):
        parsed = bn.parse_memo(memo, "thorchain")
        fields = bn.memo_pool_fields(parsed)
        assert "dest_chain" not in fields and "dest_recipient" not in fields
        assert fields["pool"] == parsed["asset"]
        assert bn.memo_pool_text(parsed) == text
    assert bn.memo_pool_fields(bn.parse_memo("+:BTC.BTC:bc1qpaired")) == {
        "pool": "BTC.BTC",
        "paired_address": "bc1qpaired",
    }
    assert bn.memo_pool_fields(bn.parse_memo("=:BTC.BTC:bc1q")) is None
    assert bn.memo_pool_fields(bn.parse_memo("TRADE+:thor1abc")) is None


def test_memo_destination_chain_only_for_an_evm_recipient_on_a_known_chain():
    assert bn.memo_dest_chain_id(bn.parse_memo("=:ETH.ETH:0x" + "ab" * 20)) == 1
    assert bn.memo_dest_chain_id(bn.parse_memo("=:ARB.ETH:0x" + "ab" * 20, "maya")) == 42161
    assert bn.memo_dest_chain_id(bn.parse_memo("=:BTC.BTC:bc1q")) is None
    assert bn.memo_dest_chain_id(bn.parse_memo("TRADE+:0x" + "ab" * 20)) is None


# --- Mayan ------------------------------------------------------------------------------


def test_mayan_swap_and_forward_eth_to_a_swift_order_for_solana():
    fx = _fx("mayan_swift")
    (d,) = bn.decode_mayan_forwards(fx["receipt"], 1, TABLE)
    assert d["event"] == "SwapAndForwardedEth" and d["forwarder"] == fx["tx"]["to"]
    assert d["token_in"] is None and d["amount_in"] == int(fx["tx"]["value"], 16)
    assert d["mayan_protocol"] == "0x40ffe85a28dc9993541449464d7529a922142960"
    call = d["call"]
    assert call["function"] == "createOrderWithToken" and not call["same_chain"]
    assert call["dest_chain"] == "Solana" and call["dest_chain_id"] is None
    assert call["recipient"] == "E17hHQ6qcqHDMRcidjaFn8UtocwWg1593Uka1RxYqu1J"
    assert call["token_out"] == "native"


def test_mayan_forward_erc20_to_a_swift_order_for_an_evm_chain():
    fx = _fx("mayan_forward_erc20")
    (d,) = bn.decode_mayan_forwards(fx["receipt"], 1, TABLE)
    row = fx["l1_rows"][0]
    assert d["event"] == "ForwardedERC20"
    assert d["token_in"] == row["contractAddress"] and d["amount_in"] == int(row["value"])
    call = d["call"]
    assert call["dest_chain"] == "HyperEVM" and call["dest_chain_id"] == 999
    assert call["recipient"] == row["from"]  # an EVM recipient reads as an address


def test_mayan_forward_eth_to_an_undecoded_call_keeps_the_selector():
    fx = _fx("mayan_forward_eth")
    (d,) = bn.decode_mayan_forwards(fx["receipt"], 1, TABLE)
    assert d["event"] == "ForwardedEth" and d["amount_in"] is None
    assert d["call"] is None and d["call_selector"] == "0x1eb1cff0"


def _word(n):
    return "{:064x}".format(n)


def test_mayan_calls_decode_destination_and_recipient_from_their_words():
    rec = int("cd" * 20, 16)
    # FastMCTP createOrder: tokenIn, amountIn, circleMaxFee, destDomain=3 (Arbitrum),
    # minFinality, (payloadType, destAddr, tokenOut, amountOutMin, ...)
    data = "0x2337e236" + "".join(
        _word(x) for x in (1, 2, 3, 3, 1000, 1, rec, 0, 77, 0, 0, 0, 0, 0, 0)
    )
    call = bn.decode_mayan_call(data)
    assert (call["dest_chain"], call["dest_chain_id"]) == ("Arbitrum", 42161)
    assert call["recipient"] == "0x" + "cd" * 20 and call["min_amount_out"] == 77
    # MayanCircle bridgeWithLockedFee: ..., destDomain=5 (Solana), destAddr
    data = "0x9be95bb4" + "".join(_word(x) for x in (1, 2, 0, 0, 5, 2**255))
    call = bn.decode_mayan_call(data)
    assert call["dest_chain"] == "Solana" and call["dest_chain_id"] is None
    assert call["recipient"] == bn.base58((2**255).to_bytes(32, "big"))
    # an unknown destination is named by its number, the recipient left as hex
    data = "0x9be95bb4" + "".join(_word(x) for x in (1, 2, 0, 0, 250, 9))
    call = bn.decode_mayan_call(data)
    assert call["dest_chain"] == "chain 250" and call["recipient"] == "0x" + _word(9)
    # MonoChainSwap transferToken: same chain, to the call's ``to``
    data = "0x29d8eb0a" + "".join(_word(x) for x in (0xAA, 5, rec, 0, 0))
    call = bn.decode_mayan_call(data)
    assert call["same_chain"] and call["recipient"] == "0x" + "cd" * 20
    assert call["token"] == "0x" + "0" * 38 + "aa"
    assert bn.decode_mayan_call("0x2337e236" + _word(1)) is None  # too short
    assert bn.decode_mayan_call("0xdeadbeef") is None


def test_mono_payouts_only_from_the_given_contract():
    mono = "0x" + "88" * 20
    to = "0x" + "cd" * 20
    receipt = {
        "logs": [
            {
                "address": mono,
                "topics": [bn.TOPIC_MONO_ETH],
                "data": "0x" + _word(int(to, 16)) + _word(9),
            },
            {
                "address": "0x" + "11" * 20,
                "topics": [bn.TOPIC_MONO_ETH],
                "data": "0x" + _word(1) + _word(1),
            },
            {
                "address": mono,
                "topics": [bn.TOPIC_MONO_TOKEN],
                "data": "0x" + _word(0xAA) + _word(int(to, 16)) + _word(5),
            },
        ]
    }
    assert bn.mono_payouts(receipt, mono) == [
        {"token": None, "to": to, "amount": 9, "log_index": None},
        {"token": "0x" + "0" * 38 + "aa", "to": to, "amount": 5, "log_index": None},
    ]


def test_reference_of_a_plain_transfer():
    fx = _fx("layerswap_maker")
    assert bn.reference(fx["tx"]["input"]) == fx["tx"]["input"]
    assert bn.reference("0x") is None and bn.reference("") is None


def test_swap_memo_affiliate_fields_and_target_text():
    p = bn.parse_memo("=:BTC~BTC:thor1x:0/1/0:aff/t1:10/20")
    assert p["affiliate"] == "aff/t1" and p["affiliate_fee"] == "10/20"
    assert bn.memo_target(p) == "THORChain (BTC~BTC)"
    assert bn.memo_target(bn.parse_memo("=:b:bc1q")) == "BTC"
    assert bn.memo_target(bn.parse_memo("=:BTC~BTC:maya1x", "maya")) == "Maya (BTC~BTC)"
    assert bn.memo_dest_chain_id(bn.parse_memo("=:ETH/ETH:0x" + "ab" * 20)) is None


def test_mayan_target_is_decoded_by_selector_and_the_docstring_says_why():
    assert "whitelists" in bn.__doc__ and "mayanProtocols" in bn.__doc__


def test_bridge2_row_says_legacy_and_skipped_forwarder_chains_are_noted():
    from tornado_demix.networks import load_networks

    role = TABLE[(42161, "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7")]["role"]
    assert "legacy bridge (CCTP preferred)" in role
    fwd = "0x337685fdab40d39bd02028545a4ffa7d287cc3e2"
    registry = {n.chain_id for n in load_networks().values()}
    # chains the Mayan forwarder page lists (EVM chain ids); Unichain, Linea, HyperEVM
    # and Monad (130, 59144, 999, 143) are not in the networks registry
    docs_chains = {1, 42161, 8453, 10, 43114, 137, 56, 130, 59144, 999, 143}
    assert {c for (c, a) in TABLE if a == fwd} == docs_chains & registry
    assert "not in the networks registry" in TABLE[(1, fwd)]["role"]
