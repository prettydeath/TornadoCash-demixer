"""Arbitrum Inbox and ZKsync Bridgehub decoders (pure functions over calldata and logs).

Real-transaction fixtures in tests/fixtures/bridges/, all public Ethereum mainnet
transactions. Selection rule: the FIRST successful transaction (Etherscan txlist of the
contract, ascending) at or after block 26,000,000 whose input starts with the selector
and whose addresses (sender, L2 recipient, refund addresses and every address in the
stored L2 rows) appear in none of the project's local label caches and excluded-case
lists (checked with a fixed-string, case-insensitive search; only protocol contracts may
match). Earlier matching transactions that failed this check were skipped:

* arbitrum_retryable.json: Arbitrum Inbox 0x4dbd4fc535ac27206064b68ffcf827b0a60bab3f,
  createRetryableTicket with l2CallValue > 0 and empty L2 calldata (block 26,002,140). Tx
  0xc36f8e0a22065ac7920da169cd90d80cd4ddf262ad7b26957e8fe8ef6e7c0941: sender
  0xa5424bd578424c3f582631105d27cb65e7e6e1ff (also excessFeeRefundAddress), L2 recipient
  0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9 (also callValueRefundAddress),
  l2CallValue 1782176074116919700 wei, message 2567906. L2 credit on Arbitrum One:
  0x13a653a59b5d3ff7a8f33c9add6fbd3d115d9b7bfda39716a968ccd7bbcd5d72 (from the
  aliased sender 0xb6534bd578424c3f582631105d27cb65e7e6f310, 405 s after the L1 block).
* arbitrum_unsafe_retryable.json: the Inbox, unsafeCreateRetryableTicket (first one
  scanning from block 26,000,000; found at block 26,078,571). Tx
  0x0acddd8534beec185f4ae7358521f1df7e4f0e30ed0cf66aa9128b286b4f682a: sender
  0xd1341302afbec6fad4c7c60eca984fe941c4cd5b, L2 recipient
  0x973a62b07f7561003b93e8baeb34cf6aa981495c, 0.01 ETH. L2 credit:
  0xd2fcbd1a5ce55b7e4d6ef28fbf9894c980c98f53f669e167497b35716c47e74e.
* arbitrum_deposit_eth.json: the Inbox, depositEth(). Tx
  0x7693848d69fd4415406d0075f4b690032376ed94fc56949d12eb4ded89ea5847 (block 26,000,740):
  sender and L2 recipient 0xc4af0bebf5365082bebd6cc743c5b656b5532c7e (an EOA: not
  aliased), 29.206751101137156080 ETH. L2 credit:
  0x3d0f11af7df34a3cee5759db6be72b18c4d40adc37219eae71528244c7c5e5d5.
* zksync_direct.json: ZKsync L1Bridgehub 0x303a465b659cbb0ab36ee643ea362c509eeb5213,
  requestL2TransactionDirect. Tx
  0x7676d3c5686e0e1ea9002284a31a2b50b2ac4f44b6d6689d418bf893dcd47b7a (block 26,000,044):
  chain 1942323, l2Contract = refundRecipient = the sender
  0x68f8fdb4ccc3552726a5429410edf0db9038c146.
* zksync_two_bridges.json: the Bridgehub, requestL2TransactionTwoBridges with a decodable
  L2 receiver (block 26,006,197). Tx
  0x2ca55638af12212f6c3482d2a886c94a3eec305ceccf0ac9dc9491fea670a674: chain 324 (ZKsync
  Era), second bridge 0x8829ad80e425c646dab305381ff105169feece56, new (0x01) encoding,
  L2 receiver = sender 0x9452ed6da616783786c6d8aaf73047ecad126c57, refundRecipient zero.

Each fixture keeps the L1 transaction, its receipt and (Arbitrum) the L2 recipient's
normal transactions in the hour after the L1 block, as read from Etherscan V2 on
2026-11-13.
"""

import json
import os

import pytest

from tornado_demix import bridges_canonical as bc
from tornado_demix.bridges import load_bridges

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()


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
    "createRetryableTicket": (
        "createRetryableTicket(address,uint256,uint256,address,address,uint256,uint256,bytes)"
    ),
    "unsafeCreateRetryableTicket": (
        "unsafeCreateRetryableTicket(address,uint256,uint256,address,address,uint256,uint256,bytes)"
    ),
    "depositEth": "depositEth()",
    "requestL2TransactionDirect": (
        "requestL2TransactionDirect((uint256,uint256,address,uint256,bytes,uint256,uint256,"
        "bytes[],address))"
    ),
    "requestL2TransactionTwoBridges": (
        "requestL2TransactionTwoBridges((uint256,uint256,uint256,uint256,uint256,address,"
        "address,uint256,bytes))"
    ),
}


def test_selectors_are_derived_from_the_abi_signatures():
    table = {**bc.INBOX_SELECTORS, **bc.BRIDGEHUB_SELECTORS}
    assert sorted(table.values()) == sorted(SIGNATURES)
    for selector, name in table.items():
        assert selector == _keccak(SIGNATURES[name])[:10], name


def test_event_topics_are_derived_from_the_event_signatures():
    assert bc.TOPIC_INBOX_MESSAGE == _keccak("InboxMessageDelivered(uint256,bytes)")
    assert bc.TOPIC_BRIDGE_MESSAGE == _keccak(
        "MessageDelivered(uint256,bytes32,address,uint8,address,bytes32,uint256,uint64)"
    )


def test_alias_rule_adds_the_offset_modulo_two_to_the_160():
    # the offset 0x1111000000000000000000000000000000001111, wrapping at 2**160
    assert bc.l1_to_l2_alias("0xa5424bd578424c3f582631105d27cb65e7e6e1ff") == (
        "0xb6534bd578424c3f582631105d27cb65e7e6f310"
    )
    assert bc.l1_to_l2_alias("0x" + "f" * 40) == "0x1111000000000000000000000000000000001110"
    for addr in ("0x" + "f" * 40, "0x" + "0" * 40, "0xc4af0bebf5365082bebd6cc743c5b656b5532c7e"):
        assert bc.l2_to_l1_alias(bc.l1_to_l2_alias(addr)) == addr


# --- Arbitrum: calldata ---------------------------------------------------------------


def test_create_retryable_ticket_calldata():
    fx = _fx("arbitrum_retryable.json")
    call = bc.decode_inbox_call(fx["tx"]["input"])
    assert call["function"] == "createRetryableTicket"
    assert call["to"] == "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    assert call["l2_call_value"] == 1782176074116919700
    assert call["max_submission_cost"] == 356403812800
    # the refund of unused fees goes to the sender, not to the L2 recipient
    assert call["excess_fee_refund_address"] == "0xa5424bd578424c3f582631105d27cb65e7e6e1ff"
    assert call["call_value_refund_address"] == "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    assert call["gas_limit"] == 0x6B77 and call["max_fee_per_gas"] == 0x72F1C80
    assert call["data"] == "0x"
    # msg.value = l2CallValue + maxSubmissionCost + gasLimit * maxFeePerGas
    assert int(fx["tx"]["value"], 16) == (1782176074116919700 + 356403812800 + 0x6B77 * 0x72F1C80)


def test_unsafe_create_retryable_ticket_calldata():
    call = bc.decode_inbox_call(_fx("arbitrum_unsafe_retryable.json")["tx"]["input"])
    assert call["function"] == "unsafeCreateRetryableTicket"
    assert call["to"] == "0x973a62b07f7561003b93e8baeb34cf6aa981495c"
    assert call["l2_call_value"] == 10**16


def test_deposit_eth_calldata_has_no_arguments():
    call = bc.decode_inbox_call(_fx("arbitrum_deposit_eth.json")["tx"]["input"])
    assert call == {"function": "depositEth"}


def test_other_calldata_is_not_an_inbox_call():
    assert bc.decode_inbox_call("0x") is None
    assert bc.decode_inbox_call("") is None
    assert bc.decode_inbox_call("0xa9059cbb" + "00" * 64) is None
    assert bc.decode_inbox_call("0x679b6ded" + "00" * 32) is None  # cut short


# --- Arbitrum: receipt logs -----------------------------------------------------------


def test_retryable_message_from_the_receipt():
    fx = _fx("arbitrum_retryable.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    assert msg["protocol"] == "arbitrum" and msg["kind"] == "retryable"
    assert msg["message_num"] == 2567906
    assert msg["recipient"] == "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    assert msg["l2_call_value"] == 1782176074116919700
    assert msg["deposit"] == int(fx["tx"]["value"], 16)
    assert msg["l2_sender"] == "0xb6534bd578424c3f582631105d27cb65e7e6f310"
    assert msg["l1_sender"] == fx["tx"]["from"]
    assert msg["tx_from"] == fx["tx"]["from"]
    assert msg["excess_fee_refund_address"] == "0xa5424bd578424c3f582631105d27cb65e7e6e1ff"
    assert msg["tx_hash"] == fx["tx_hash"] and msg["inbox"] == bc.ARBITRUM_INBOX


def test_unsafe_retryable_message_from_the_receipt():
    fx = _fx("arbitrum_unsafe_retryable.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    assert msg["kind"] == "retryable"
    assert msg["recipient"] == "0x973a62b07f7561003b93e8baeb34cf6aa981495c"
    assert msg["l2_call_value"] == 10**16 and msg["deposit"] == int(fx["tx"]["value"], 16)


def test_eth_deposit_message_credits_the_address_in_the_message():
    fx = _fx("arbitrum_deposit_eth.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    assert msg["kind"] == "eth_deposit"
    # an EOA calling depositEth is credited at its own address (no alias)
    assert msg["recipient"] == fx["tx"]["from"]
    assert msg["l2_call_value"] == msg["deposit"] == 29206751101137156080
    assert msg["l2_sender"] == bc.l1_to_l2_alias(fx["tx"]["from"])


def test_messages_need_a_listed_inbox_and_a_good_receipt():
    fx = _fx("arbitrum_retryable.json")
    assert bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 42161, TABLE) == []
    assert bc.decode_inbox_messages(dict(fx["receipt"], status="0x0"), "0x1", 1, TABLE) == []
    assert bc.decode_inbox_messages(None, "0x1", 1, TABLE) == []
    moved = [dict(log, address="0x" + "1" * 40) for log in fx["receipt"]["logs"]]
    assert bc.decode_inbox_messages(dict(fx["receipt"], logs=moved), "0x1", 1, TABLE) == []


def test_message_kind_falls_back_to_the_length_without_the_bridge_log():
    fx = _fx("arbitrum_deposit_eth.json")
    inbox_only = [log for log in fx["receipt"]["logs"] if log["address"] == bc.ARBITRUM_INBOX]
    (msg,) = bc.decode_inbox_messages(dict(fx["receipt"], logs=inbox_only), "0x1", 1, TABLE)
    assert msg["kind"] == "eth_deposit" and msg["l2_sender"] is None and msg["l1_sender"] is None


# --- Arbitrum: L2 credit --------------------------------------------------------------


class L2:
    def __init__(self, fx, fail=False):
        self.fx, self.fail, self.queries = fx, fail, []

    def block_by_time(self, ts, closest="before"):
        return self.fx["dest_window"][0 if closest == "before" else 1]

    def current_block(self):
        return self.fx["dest_window"][1]

    def fetch_all(self, action, address, start_block=0, end_block=0, extra=None):
        from tornado_demix.errors import ApiError

        if self.fail:
            raise ApiError("explorer down")
        self.queries.append((action, address, start_block, end_block))
        return [r for r in self.fx["l2_rows"] if address in (r["from"], r["to"])]


@pytest.mark.parametrize(
    "name, l2_tx",
    [
        (
            "arbitrum_retryable.json",
            "0x13a653a59b5d3ff7a8f33c9add6fbd3d115d9b7bfda39716a968ccd7bbcd5d72",
        ),
        (
            "arbitrum_unsafe_retryable.json",
            "0xd2fcbd1a5ce55b7e4d6ef28fbf9894c980c98f53f669e167497b35716c47e74e",
        ),
        (
            "arbitrum_deposit_eth.json",
            "0x3d0f11af7df34a3cee5759db6be72b18c4d40adc37219eae71528244c7c5e5d5",
        ),
    ],
)
def test_l2_credit_is_found_by_aliased_sender_recipient_and_value(name, l2_tx):
    fx = _fx(name)
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    client = L2(fx)
    credit = bc.find_l2_credit(client, msg, fx["ts"], now=fx["ts"] + 10 * 86400)
    assert credit["tx_hash"] == l2_tx
    assert credit["value"] == msg["l2_call_value"]
    assert credit["ts"] >= fx["ts"] and credit["block"] > 0
    ((action, addr, start, end),) = client.queries
    assert action == "txlist" and addr == msg["recipient"]
    assert (start, end) == tuple(fx["dest_window"])


def test_l2_credit_needs_the_same_value_and_the_aliased_sender():
    fx = _fx("arbitrum_retryable.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    now = fx["ts"] + 10 * 86400
    assert bc.find_l2_credit(L2(fx), dict(msg, l2_call_value=1), fx["ts"], now=now) is None
    other = dict(msg, l2_sender="0x" + "2" * 40)
    assert bc.find_l2_credit(L2(fx), other, fx["ts"], now=now) is None
    # a credit before the L1 transaction is not it
    assert bc.find_l2_credit(L2(fx), msg, fx["ts"] + 3600, now=now) is None


def test_l2_credit_lookup_errors_propagate():
    from tornado_demix.errors import ApiError

    fx = _fx("arbitrum_retryable.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    with pytest.raises(ApiError):
        bc.find_l2_credit(L2(fx, fail=True), msg, fx["ts"], now=fx["ts"] + 86400)


# --- ZKsync Bridgehub -----------------------------------------------------------------


def test_request_l2_transaction_direct_calldata():
    fx = _fx("zksync_direct.json")
    call = bc.decode_bridgehub_call(fx["tx"]["input"])
    assert call["function"] == "requestL2TransactionDirect"
    assert call["chain_id"] == 1942323
    assert call["l2_recipient"] == "0x68f8fdb4ccc3552726a5429410edf0db9038c146"
    assert call["refund_recipient"] == "0x68f8fdb4ccc3552726a5429410edf0db9038c146"
    assert call["mint_value"] == int(fx["tx"]["value"], 16)
    assert call["l2_value"] == 1
    assert call["msg_value"] == call["mint_value"]


def test_request_l2_transaction_two_bridges_calldata():
    fx = _fx("zksync_two_bridges.json")
    call = bc.decode_bridgehub_call(fx["tx"]["input"])
    assert call["function"] == "requestL2TransactionTwoBridges"
    assert call["chain_id"] == 324
    assert call["second_bridge"] == "0x8829ad80e425c646dab305381ff105169feece56"
    assert call["l2_recipient"] == "0x9452ed6da616783786c6d8aaf73047ecad126c57"
    assert call["refund_recipient"] == "0x" + "0" * 40
    assert call["mint_value"] == int(fx["tx"]["value"], 16)
    assert call["second_bridge_value"] == 0
    assert call["msg_value"] == call["mint_value"]


def _two_bridges(second_calldata, second_value=0):
    words = [324, 5, 0, 1, 800, 0xAB, 0xCD, second_value, 9 * 32]
    blob = bytes.fromhex(second_calldata)
    pad = blob + b"\0" * (-len(blob) % 32)
    body = "".join(f"{w:064x}" for w in [32] + words) + f"{len(blob):064x}" + pad.hex()
    return "0x24fd57fb" + body


def test_two_bridges_legacy_encoding_carries_the_receiver_in_the_third_word():
    legacy = f"{1:064x}{7:064x}{0xBEEF:064x}"  # (token, amount, l2Receiver)
    call = bc.decode_bridgehub_call(_two_bridges(legacy, second_value=7))
    assert call["l2_recipient"] == "0x" + "beef".rjust(40, "0")
    assert call["msg_value"] == 5 + 7


def test_two_bridges_with_an_unknown_encoding_has_no_recipient():
    call = bc.decode_bridgehub_call(_two_bridges("02" + "00" * 64))
    assert call["function"] == "requestL2TransactionTwoBridges"
    assert call["l2_recipient"] is None


def test_other_calldata_is_not_a_bridgehub_call():
    assert bc.decode_bridgehub_call("0x") is None
    assert bc.decode_bridgehub_call("0xd52471c1" + "00" * 31) is None


# --- explorer links -------------------------------------------------------------------


def test_recipient_links_use_the_registry_then_the_known_l2_explorers():
    from tornado_demix.networks import get_network

    class Registry:
        def network(self, chain_id):
            return get_network("arbitrum") if chain_id == 42161 else None

    addr = "0x" + "ab" * 20
    assert bc.recipient_url(42161, addr, Registry()) == get_network("arbitrum").addr_url(addr)
    assert bc.recipient_url(324, addr, Registry()) == "https://explorer.zksync.io/address/" + addr
    assert bc.recipient_url(1942323, addr, Registry()) == ""
    assert bc.recipient_url(42161, addr, None) == "https://arbiscan.io/address/" + addr


def test_l2_credit_search_reads_a_short_window_first_then_the_rest():
    fx = _fx("arbitrum_retryable.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)

    class Clock(L2):
        def block_by_time(self, ts, closest="before"):
            return int(ts)  # one block per second, to see the windows

        def current_block(self):
            return 10**12

    now = fx["ts"] + 10 * 86400
    found = Clock(fx)
    assert bc.find_l2_credit(found, msg, fx["ts"], now=now) is not None
    ((_a, _r, start, end),) = found.queries
    assert start == fx["ts"] - 60 and end == fx["ts"] + bc.FIRST_WINDOW
    missing = Clock(dict(fx, l2_rows=[]))
    assert bc.find_l2_credit(missing, msg, fx["ts"], now=now) is None
    first, second = missing.queries
    assert second[2] == first[3] + 1 and second[3] == fx["ts"] + bc.MAX_FILL_WAIT


def test_a_claimed_l2_credit_is_not_taken_again():
    fx = _fx("arbitrum_retryable.json")
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    now = fx["ts"] + 10 * 86400
    credit = bc.find_l2_credit(L2(fx), msg, fx["ts"], now=now)
    again = bc.find_l2_credit(L2(fx), msg, fx["ts"], now=now, claimed={credit["tx_hash"]})
    assert again is None


def _inbox_receipt(tx_from, caller, num, kind, payload):
    """A synthetic receipt: the Bridge's MessageDelivered and the Inbox's message."""
    body = payload + "00" * (-len(payload) // 2 % 32)
    inbox_data = "0x" + f"{32:064x}{len(payload) // 2:064x}" + body
    sender = bc.l1_to_l2_alias(caller)[2:]
    bridge_data = "0x" + f"{int(bc.ARBITRUM_INBOX, 16):064x}{kind:064x}" + sender.rjust(64, "0")
    bridge_data += "00" * 96
    num_topic = f"0x{num:064x}"
    return {
        "status": "0x1",
        "from": tx_from,
        "logs": [
            {
                "address": "0x8315177ab297ba92a06054ce80a67ed4dbd7ed3a",
                "topics": [bc.TOPIC_BRIDGE_MESSAGE, num_topic, "0x" + "00" * 32],
                "data": bridge_data,
                "logIndex": "0x1",
            },
            {
                "address": bc.ARBITRUM_INBOX,
                "topics": [bc.TOPIC_INBOX_MESSAGE, num_topic],
                "data": inbox_data,
                "logIndex": "0x2",
            },
        ],
    }


def test_contract_caller_of_deposit_eth_is_credited_at_its_aliased_address():
    safe, owner = "0x" + "5a" * 20, "0x" + "0e" * 20
    dest = bc.l1_to_l2_alias(safe)  # what the Inbox writes for a contract caller
    rc = _inbox_receipt(owner, safe, 9, bc.KIND_ETH_DEPOSIT, dest[2:] + f"{10**18:064x}")
    (msg,) = bc.decode_inbox_messages(rc, "0xab", 1, TABLE)
    assert msg["kind"] == "eth_deposit"
    assert msg["recipient"] == dest and msg["recipient"] != safe
    assert msg["l1_sender"] == safe and msg["tx_from"] == owner
    assert msg["l2_sender"] == dest
