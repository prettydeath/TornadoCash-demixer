"""Token deposits through canonical bridges: Arbitrum token gateways, ZKsync ERC-20 and the
legacy ZKsync Era mailbox (pure decoders over calldata and logs).

Real-transaction fixtures in tests/fixtures/bridges/, all public Ethereum mainnet
transactions. Selection rule as in tests/test_bridges_canonical.py: the FIRST successful
transaction (Etherscan txlist of the contract, ascending) at or after block 26,000,000
whose input starts with the selector and which matches the kind named below, and whose
addresses (sender, L2 recipient, refund addresses and every address in the stored L2
rows) appear in none of the project's local label caches and excluded-case lists
(checked with a fixed-string, case-insensitive search; only protocol and token contracts
may match). Earlier matching transactions that failed this check were skipped.

* arbitrum_gateway_outbound.json: L1GatewayRouter
  0x72ce9c846789fdb6fc1f34ac4ad25dd9ef7031ef, outboundTransfer whose tokens went to the
  standard L1ERC20Gateway 0xa3a7b6f88361f48403514059f1f16c8e78d60eec (block 26,002,204;
  the earlier router transactions used other gateways). Tx
  0xf2991c75b9c2e8e1f3924ce918448c310419b270221f6202ee1719fc1ffd62ba: sender and L2
  recipient 0x05cb44f8ac18cb2ded4cabd906fec6876e941d27, 36825.474728810314590492 MOON
  (L1 0xb2490e357980ce57bf5745e181e537a64eb367b1, L2
  0x24404dc041d74cd03cfe28855f555559390c931b by the router's calculateL2TokenAddress),
  message 2567922. L2 credit (a mint to the recipient):
  0x00242db8538ab3f30f755dcc395cb04e642da5ac7e111e3f72374bd7e6177c80.
* arbitrum_gateway_custom_refund.json: the router, outboundTransferCustomRefund (block
  26,010,599). Tx 0xf5ad03f1c77284980008befb45b41d3d6c741e4ac605a6af9b80ae8b5941c8b6:
  sender, refund address and L2 recipient 0xd411d428a63cf4c7029bc53f0e0f56c4933fdbb7,
  0.90010523 WBTC (L2 0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f), message 2569384. The
  fixture keeps the recipient's L2 mints of that token in the hour after the L1 block
  (three, of different amounts); the credit is
  0x659644b1b0b6b90b9e451e80cbc4db687ace36e75025bd0a027380452e36faa2.
* zksync_two_bridges.json (also used by tests/test_bridges_canonical.py): Bridgehub
  requestL2TransactionTwoBridges, the first ERC-20 deposit through the L1AssetRouter at
  or after block 26,000,000 that passed the check: 74977.8 USDC from
  0x9452ed6da616783786c6d8aaf73047ecad126c57 into the L1NativeTokenVault, new (0x01)
  encoding. ``l1_rows`` is the sender's USDC transfer in that transaction.
* zksync_legacy_request.json: ZKsync Era DiamondProxy
  0x32400084c286cf3e17e7b677ea9583e60a000324, requestL2Transaction (block 26,250,210).
  Tx 0xa9f7e5ea68c14e89b1ab9cc6584fbf58dc627d4b431c019a4a7bbe5eb59c81c1: _contractL2 =
  refund recipient = sender 0xcdb91c04b6a730f52fd23887d002d9d6bee4c066, l2Value 0.0015
  ETH of 0.001756158824 ETH sent.

Each fixture keeps the L1 transaction and its receipt (and, for Arbitrum, the token
mapping and the L2 rows), as read from Etherscan V2 on 2026-11-14.
"""

import json
import os

import pytest

from tornado_demix import bridges_canonical as bc
from tornado_demix.bridges import listed, load_bridges

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()
MOON_USER = "0x05cb44f8ac18cb2ded4cabd906fec6876e941d27"
WBTC_USER = "0xd411d428a63cf4c7029bc53f0e0f56c4933fdbb7"
STD_GATEWAY = "0xa3a7b6f88361f48403514059f1f16c8e78d60eec"
L2_STD_GATEWAY = "0x09e9222e96e7b4ae2a407b98d48e330053351eee"
ZERO = "0x" + "0" * 40


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _keccak(text):
    try:
        from Crypto.Hash import keccak
    except ImportError:
        pytest.skip("pycryptodome not installed")
    return "0x" + keccak.new(digest_bits=256, data=text.encode()).hexdigest()


SIGNATURES = {
    "outboundTransfer": "outboundTransfer(address,address,uint256,uint256,uint256,bytes)",
    "outboundTransferCustomRefund": (
        "outboundTransferCustomRefund(address,address,address,uint256,uint256,uint256,bytes)"
    ),
    "requestL2Transaction": (
        "requestL2Transaction(address,uint256,bytes,uint256,uint256,bytes[],address)"
    ),
}


def test_selectors_and_topics_are_derived_from_the_signatures():
    table = {**bc.GATEWAY_SELECTORS, **bc.LEGACY_MAILBOX_SELECTORS}
    assert sorted(table.values()) == sorted(SIGNATURES)
    for selector, name in table.items():
        assert selector == _keccak(SIGNATURES[name])[:10], name
    assert (
        bc.SELECTOR_FINALIZE_INBOUND
        == _keccak("finalizeInboundTransfer(address,address,address,uint256,bytes)")[:10]
    )
    assert bc.SELECTOR_CALCULATE_L2_TOKEN == _keccak("calculateL2TokenAddress(address)")[:10]
    assert bc.TOPIC_DEPOSIT_INITIATED == _keccak(
        "DepositInitiated(address,address,address,uint256,uint256)"
    )


def test_table_lists_the_gateways_and_the_zksync_token_contracts_from_the_docs():
    arbitrum_docs = "https://docs.arbitrum.io/build-decentralized-apps/reference/contract-addresses"
    zksync_docs = "https://docs.zksync.io/zksync-protocol/contracts/l1-contracts/zk-chain-addresses"
    for addr, role in (
        ("0xcee284f754e854890e311e3280b767f80797180d", "L1CustomGateway"),
        ("0xd92023e9d9911199a6711321d1277285e6d4e2db", "L1WethGateway"),
    ):
        row = listed(TABLE, 1, addr)
        assert row["protocol"] == "arbitrum" and row["role"] == role
        assert row["source_url"] == arbitrum_docs
    for addr in (
        "0xbed1eb542f9a5aa6419ff3deb921a372681111f6",
        "0x8829ad80e425c646dab305381ff105169feece56",
    ):
        row = listed(TABLE, 1, addr)
        assert row["protocol"] == "zksync" and row["source_url"] == zksync_docs
    assert listed(TABLE, 1, STD_GATEWAY)["role"] == "L1ERC20Gateway"


# --- Arbitrum: calldata ---------------------------------------------------------------


def test_outbound_transfer_calldata():
    call = bc.decode_gateway_call(_fx("arbitrum_gateway_outbound.json")["tx"]["input"])
    assert call["function"] == "outboundTransfer"
    assert call["token"] == "0xb2490e357980ce57bf5745e181e537a64eb367b1"
    assert call["to"] == MOON_USER and call["refund_to"] is None
    assert call["amount"] == 36825474728810314590492
    assert call["max_gas"] == 0x1EAE9 and call["gas_price_bid"] == 0x7273CE0


def test_outbound_transfer_custom_refund_calldata():
    call = bc.decode_gateway_call(_fx("arbitrum_gateway_custom_refund.json")["tx"]["input"])
    assert call["function"] == "outboundTransferCustomRefund"
    assert call["token"] == "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    assert call["refund_to"] == WBTC_USER and call["to"] == WBTC_USER
    assert call["amount"] == 90010523


def test_gateway_call_decoder_rejects_other_or_short_calls():
    assert bc.decode_gateway_call("0x") is None
    assert bc.decode_gateway_call("0xd2ce7d65" + "00" * 31) is None
    assert bc.decode_gateway_call(_fx("arbitrum_retryable.json")["tx"]["input"]) is None


# --- Arbitrum: receipt ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, user, token, amount, num",
    [
        (
            "arbitrum_gateway_outbound.json",
            MOON_USER,
            "0xb2490e357980ce57bf5745e181e537a64eb367b1",
            36825474728810314590492,
            2567922,
        ),
        (
            "arbitrum_gateway_custom_refund.json",
            WBTC_USER,
            "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
            90010523,
            2569384,
        ),
    ],
)
def test_gateway_deposit_from_the_receipt(name, user, token, amount, num):
    fx = _fx(name)
    (dep,) = bc.decode_gateway_deposits(fx["receipt"], fx["tx_hash"], 1, TABLE)
    assert dep["protocol"] == "arbitrum" and dep["kind"] == "token_deposit"
    assert dep["gateway"] == STD_GATEWAY and dep["l2_gateway"] == L2_STD_GATEWAY
    assert dep["l1_token"] == token and dep["from"] == user and dep["recipient"] == user
    assert dep["amount"] == amount and dep["message_num"] == num
    assert dep["tx_hash"] == fx["tx_hash"]
    # the token transfer of the receipt went from the sender to that gateway
    assert any(r["to"] == STD_GATEWAY and int(r["value"]) == amount for r in fx["l1_rows"])


def test_the_retryable_of_a_gateway_deposit_carries_finalize_inbound_transfer():
    fx = _fx("arbitrum_gateway_outbound.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    # a fee-only retryable to the L2 gateway: the funds are in the L2 calldata
    assert msg["l2_call_value"] == 0 and msg["recipient"] == L2_STD_GATEWAY
    call = bc.decode_finalize_inbound(msg["l2_data"])
    assert call["token"] == "0xb2490e357980ce57bf5745e181e537a64eb367b1"
    assert call["from"] == MOON_USER and call["to"] == MOON_USER
    assert call["amount"] == 36825474728810314590492
    assert bc.decode_finalize_inbound("0x1234") is None


def _drop(receipt, topic):
    logs = [lg for lg in receipt["logs"] if lg["topics"][0] != topic]
    return dict(receipt, logs=logs)


def test_gateway_deposit_needs_the_inbox_message_of_the_gateway():
    fx = _fx("arbitrum_gateway_outbound.json")
    rc = fx["receipt"]
    assert bc.decode_gateway_deposits(_drop(rc, bc.TOPIC_INBOX_MESSAGE), "0x1", 1, TABLE) == []
    # without the Bridge log the L1 sender of the message is unknown: not taken
    assert bc.decode_gateway_deposits(_drop(rc, bc.TOPIC_BRIDGE_MESSAGE), "0x1", 1, TABLE) == []
    # an L2 calldata that disagrees with the event (another amount) is not taken
    bad = json.loads(json.dumps(rc))
    for lg in bad["logs"]:
        if lg["topics"][0] == bc.TOPIC_DEPOSIT_INITIATED:
            lg["data"] = lg["data"][:-1] + ("1" if lg["data"][-1] != "1" else "2")
    assert bc.decode_gateway_deposits(bad, "0x1", 1, TABLE) == []
    assert bc.decode_gateway_deposits(dict(rc, status="0x0"), "0x1", 1, TABLE) == []


def test_l2_token_call_and_result():
    data = bc.l2_token_call("0xB2490E357980CE57BF5745E181E537A64EB367B1")
    assert (
        data
        == bc.SELECTOR_CALCULATE_L2_TOKEN
        + "b2490e357980ce57bf5745e181e537a64eb367b1".rjust(64, "0")
    )
    word = "0x" + "24404dc041d74cd03cfe28855f555559390c931b".rjust(64, "0")
    assert bc.address_result(word) == "0x24404dc041d74cd03cfe28855f555559390c931b"
    assert bc.address_result("0x" + "0" * 64) is None
    assert bc.address_result("0x") is None and bc.address_result(None) is None


# --- Arbitrum: the L2 credit ----------------------------------------------------------


class _L2:
    def __init__(self, rows, window=(0, 10**12)):
        self.rows, self.window, self.calls = rows, window, []

    def block_by_time(self, ts, closest="before"):
        return self.window[0]

    def current_block(self):
        return self.window[1]

    def fetch_all(self, action, address, start_block=0, end_block=0, extra=None):
        self.calls.append((action, address, (extra or {}).get("contractaddress")))
        contract = (extra or {}).get("contractaddress")
        return [
            r
            for r in self.rows
            if address in (r["from"], r["to"])
            and (contract is None or r["contractAddress"] == contract)
        ]


def _deposit(name):
    fx = _fx(name)
    (dep,) = bc.decode_gateway_deposits(fx["receipt"], fx["tx_hash"], 1, TABLE)
    return fx, dep


def test_token_credit_is_the_mint_of_the_same_amount_to_the_recipient():
    fx, dep = _deposit("arbitrum_gateway_custom_refund.json")
    l2 = _L2(fx["l2_rows"])
    credit = bc.find_l2_token_credit(l2, dep, fx["l2_token"], fx["ts"], now=fx["ts"] + 3600)
    assert credit["tx_hash"] == "0x659644b1b0b6b90b9e451e80cbc4db687ace36e75025bd0a027380452e36faa2"
    assert credit["value"] == 90010523 and credit["symbol"] == "WBTC"
    assert l2.calls[0] == ("tokentx", WBTC_USER, fx["l2_token"])


def test_token_credit_claimed_once_and_tied_by_amount():
    fx, dep = _deposit("arbitrum_gateway_custom_refund.json")
    first = "0x659644b1b0b6b90b9e451e80cbc4db687ace36e75025bd0a027380452e36faa2"
    l2 = _L2(fx["l2_rows"])
    now = fx["ts"] + 3600
    assert bc.find_l2_token_credit(l2, dep, fx["l2_token"], fx["ts"], now, claimed={first}) is None
    other = dict(dep, amount=dep["amount"] + 1)
    assert bc.find_l2_token_credit(l2, other, fx["l2_token"], fx["ts"], now) is None
    # a credit before the L1 transaction is not this deposit
    assert bc.find_l2_token_credit(l2, dep, fx["l2_token"], fx["ts"] + 10**6, now + 10**6) is None


def test_token_credit_from_a_third_address_is_not_the_bridge():
    fx, dep = _deposit("arbitrum_gateway_outbound.json")
    rows = [dict(r, **{"from": "0x" + "4" * 40}) for r in fx["l2_rows"]]
    now = fx["ts"] + 3600
    assert bc.find_l2_token_credit(_L2(rows), dep, fx["l2_token"], fx["ts"], now) is None
    # the L2 gateway paying out (a WETH-style gateway) counts
    rows = [dict(r, **{"from": dep["l2_gateway"]}) for r in fx["l2_rows"]]
    assert bc.find_l2_token_credit(_L2(rows), dep, fx["l2_token"], fx["ts"], now) is not None


# --- ZKsync ---------------------------------------------------------------------------


def test_two_bridges_erc20_deposit_decodes_token_amount_and_receiver():
    call = bc.decode_bridgehub_call(_fx("zksync_two_bridges.json")["tx"]["input"])
    assert call["l1_token"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert call["amount"] == 74977800000
    assert call["l2_recipient"] == "0x9452ed6da616783786c6d8aaf73047ecad126c57"
    assert call["second_bridge"] == "0x8829ad80e425c646dab305381ff105169feece56"


def test_two_bridges_legacy_encoding_decodes_token_amount_and_receiver():
    token, receiver = "0x" + "a1" * 20, "0x" + "b2" * 20
    blob = token[2:].rjust(64, "0") + f"{12345:064x}" + receiver[2:].rjust(64, "0")
    head = [
        324,
        10**15,
        0,
        1,
        800,
        int("c3" * 20, 16),
        int("8829ad80e425c646dab305381ff105169feece56", 16),
        0,
    ]
    words = [32] + head + [9 * 32, len(blob) // 2]
    data = "0x24fd57fb" + "".join(f"{w:064x}" for w in words) + blob
    call = bc.decode_bridgehub_call(data)
    assert (call["l1_token"], call["amount"], call["l2_recipient"]) == (token, 12345, receiver)


def test_direct_request_has_no_token():
    call = bc.decode_bridgehub_call(_fx("zksync_direct.json")["tx"]["input"])
    assert call["l1_token"] is None and call["amount"] is None


def test_legacy_request_l2_transaction():
    fx = _fx("zksync_legacy_request.json")
    call = bc.decode_legacy_request(fx["tx"]["input"])
    user = "0xcdb91c04b6a730f52fd23887d002d9d6bee4c066"
    assert call["function"] == "requestL2Transaction" and call["chain_id"] == bc.ZKSYNC_ERA
    assert call["l2_recipient"] == user and call["refund_recipient"] == user
    assert call["l2_value"] == 1500000000000000
    assert call["l2_value"] <= int(fx["tx"]["value"], 16)
    assert bc.decode_legacy_request(_fx("zksync_direct.json")["tx"]["input"]) is None
    assert bc.decode_legacy_request("0xeb672419" + "00" * 40) is None


def test_router_get_gateway_call():
    assert bc.SELECTOR_GET_GATEWAY == _keccak("getGateway(address)")[:10]
    token = "0xb2490e357980ce57bf5745e181e537a64eb367b1"
    assert bc.gateway_of_call(token.upper().replace("0X", "0x")) == (
        bc.SELECTOR_GET_GATEWAY + token[2:].rjust(64, "0")
    )
