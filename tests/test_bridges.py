"""Across deposit decoding and the destination-chain join, on real logs.

The fixtures under tests/fixtures/bridges/ are Ethereum receipts of Across
deposits and the matching fill logs read from the destination SpokePool.
Source: public Ethereum / Arbitrum transactions (the tx hashes in the fixtures resolve on
Etherscan / Arbiscan). They are public Across deposits picked from the SpokePool event
stream, not taken from Across's documentation; only public on-chain data is stored.
"""

import json
import os

import pytest

from tornado_demix.bridges import (
    TOPIC_ACROSS_FILL,
    TOPIC_ACROSS_FILL_V3,
    decode_across_deposits,
    decode_deposits,
    find_fill,
    load_bridges,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()
ARBITRUM_SPOKE = "0xe35e9842fceaca96570b734083f4a58e8f7c5f2a"


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class L1:
    """Receipt reader over a fixture."""

    def __init__(self, fx):
        self.fx = fx

    def tx_receipt(self, tx_hash):
        return self.fx["receipt"] if tx_hash == self.fx["tx_hash"] else None


class Dest:
    """Destination chain over the fixture's fill logs; records the queries."""

    def __init__(self, fx):
        self.fx, self.calls = fx, []

    def block_by_time(self, ts, closest="before"):
        return self.fx["dest_window"][0 if closest == "before" else 1]

    def get_logs(self, address, topic0, start, end, topics=None):
        self.calls.append((address, topic0, start, end, dict(topics or {})))
        return [
            r
            for r in self.fx["fills"]
            if r["address"].lower() == address.lower()
            and r["topics"][0] == topic0
            and all(r["topics"][i] == t for i, t in (topics or {}).items())
        ]


def test_current_deposit_event_decodes_id_chain_recipient_amount():
    fx = _fx("across_new_arbitrum.json")
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    assert d["event"] == "FundsDeposited"
    assert d["deposit_id"] == 3497136
    assert d["dest_chain_id"] == 42161
    assert d["depositor"] == "0xb9f84f474e178aee1434c854337a2d9b2d8a48fa"
    assert d["recipient"]
    assert d["input_token"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"  # USDC
    assert d["input_amount"] == 3261597488
    assert 0 < d["output_amount"] <= d["input_amount"]
    assert d["spoke"] == "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5"


def test_v3_deposit_event_decodes_the_same_fields():
    fx = _fx("across_v3_arbitrum.json")
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    assert d["event"] == "V3FundsDeposited"
    assert d["deposit_id"] == 1440177
    assert d["dest_chain_id"] == 42161
    assert d["recipient"] and d["depositor"]


def test_a_log_from_an_unlisted_emitter_is_not_a_deposit():
    fx = _fx("across_new_arbitrum.json")
    logs = [dict(log, address="0x" + "ab" * 20) for log in fx["receipt"]["logs"]]
    assert decode_across_deposits(logs, 1, TABLE) == []
    # the same logs on a chain where that address is not an Across pool
    assert decode_across_deposits(fx["receipt"]["logs"], 137, TABLE) == []


def test_failed_transaction_has_no_deposit():
    fx = _fx("across_new_arbitrum.json")
    bad = dict(fx, receipt=dict(fx["receipt"], status="0x0"))
    assert decode_deposits(L1(bad), fx["tx_hash"], table=TABLE) == []
    assert decode_deposits(L1(fx), "0x" + "00" * 32, table=TABLE) == []


@pytest.mark.parametrize("name", ["across_new_arbitrum.json", "across_v3_arbitrum.json"])
def test_fill_is_found_by_origin_chain_and_deposit_id(name):
    fx = _fx(name)
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    dest = Dest(fx)
    fill = find_fill(dest, d, TABLE)
    assert fill["chain_id"] == 42161
    assert fill["recipient"] == d["recipient"]
    assert fill["depositor"] == d["depositor"]
    assert fill["output_amount"] == d["output_amount"]
    assert fill["tx_hash"] == fx["fills"][0]["transactionHash"]
    # the query names the destination pool and both id topics
    address, _topic0, start, end, topics = dest.calls[0]
    assert address == ARBITRUM_SPOKE
    assert topics == {1: "0x{:064x}".format(1), 2: "0x{:064x}".format(d["deposit_id"])}
    assert start <= fill["block"] <= end


def test_no_fill_returns_none_and_both_fill_events_are_tried():
    fx = _fx("across_new_arbitrum.json")
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    dest = Dest(dict(fx, fills=[]))
    assert find_fill(dest, d, TABLE) is None
    assert [c[1] for c in dest.calls] == [TOPIC_ACROSS_FILL, TOPIC_ACROSS_FILL_V3]


def test_fill_of_another_depositor_is_ignored():
    fx = _fx("across_new_arbitrum.json")
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    other = dict(d, depositor="0x" + "12" * 20)
    assert find_fill(Dest(fx), other, TABLE) is None


def test_destination_without_a_listed_pool_has_no_fill():
    fx = _fx("across_new_arbitrum.json")
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    assert find_fill(Dest(fx), dict(d, dest_chain_id=59144), TABLE) is None


@pytest.mark.live
def test_live_deposit_to_fill_pair():
    """Real chain: the Across deposit of the fixture and its Arbitrum fill (needs an API key)."""
    from tornado_demix import config
    from tornado_demix.etherscan import EtherscanClient
    from tornado_demix.networks import get_network

    fx = _fx("across_new_arbitrum.json")
    key = config.load_api_key()
    l1 = EtherscanClient(key)
    arb = EtherscanClient(key, **get_network("arbitrum").client_kwargs())
    (d,) = decode_deposits(l1, fx["tx_hash"], table=TABLE, ts=fx["ts"])
    fill = find_fill(arb, d, TABLE)
    assert fill and fill["tx_hash"] == fx["fills"][0]["transactionHash"]
    assert fill["recipient"] == d["recipient"]


def _with_word(fx, index, value):
    """Fixture copy whose first fill has word ``index`` of its data replaced."""
    fills = [dict(f) for f in fx["fills"]]
    data = fills[0]["data"]
    pos = 2 + index * 64
    fills[0]["data"] = data[:pos] + "{:064x}".format(value) + data[pos + 64 :]
    return dict(fx, fills=fills)


def _deposit(fx):
    (d,) = decode_deposits(L1(fx), fx["tx_hash"], table=TABLE, ts=fx["ts"])
    return d


@pytest.mark.parametrize("name", ["across_new_arbitrum.json", "across_v3_arbitrum.json"])
@pytest.mark.parametrize("index", [0, 2, 3, 8, 9])
def test_fill_that_differs_from_the_deposit_is_ignored(name, index):
    """Same id and depositor, but another token, amount or recipient: not this deposit."""
    fx = _fx(name)
    other = _with_word(fx, index, 0x1234)
    assert find_fill(Dest(other), _deposit(fx), TABLE) is None


@pytest.mark.parametrize(
    "name,recipient_word,amount_word",
    [("across_new_arbitrum.json", 11, 13), ("across_v3_arbitrum.json", 13, 15)],
)
def test_sped_up_fill_is_followed_to_the_updated_recipient_and_amount(
    name, recipient_word, amount_word
):
    fx = _fx(name)
    d = _deposit(fx)
    new_recipient = "0x" + "ab" * 20
    sped = _with_word(_with_word(fx, recipient_word, int(new_recipient, 16)), amount_word, 777)
    fill = find_fill(Dest(sped), d, TABLE)
    assert fill["recipient"] == new_recipient and fill["output_amount"] == 777
    assert fill["original_recipient"] == d["recipient"]
    assert fill["original_output_amount"] == d["output_amount"]
    assert fill["sped_up"] is True
    plain = find_fill(Dest(fx), d, TABLE)
    assert plain["sped_up"] is False


def test_search_window_never_reaches_past_now():
    import time

    fx = _fx("across_new_arbitrum.json")
    d = dict(_deposit(fx), ts=int(time.time()) - 100, fill_deadline=2**32 - 1)
    asked = []

    class Dst(Dest):
        def block_by_time(self, ts, closest="before"):
            asked.append(ts)
            return 1

        def current_block(self):
            return 10

    find_fill(Dst(fx), d, TABLE)
    assert max(asked) <= int(time.time())


def test_window_reaching_the_present_searches_up_to_the_current_block():
    import time

    from tornado_demix.errors import BlockLookupError

    fx = _fx("across_new_arbitrum.json")
    d = dict(_deposit(fx), ts=int(time.time()) - 100, fill_deadline=2**32 - 1)
    latest_time, head = int(time.time()) - 30, 987_654
    seen = {}

    class Fresh(Dest):
        def block_by_time(self, ts, closest="before"):
            if closest == "after" and ts >= latest_time:
                raise BlockLookupError("timestamp not mined yet")
            return 1

        def current_block(self):
            return head

        def get_logs(self, address, topic0, start, end, topics=None):
            seen["end"] = end
            return super().get_logs(address, topic0, start, end, topics)

    find_fill(Fresh(fx), d, TABLE)
    assert seen["end"] == head


def test_an_after_lookup_that_fails_for_an_older_end_falls_back_to_the_head():
    from tornado_demix.errors import BlockLookupError

    fx = _fx("across_new_arbitrum.json")
    d = _deposit(fx)  # a 2025-12 deposit: the end is in the past
    seen = {}

    class Odd(Dest):
        def block_by_time(self, ts, closest="before"):
            if closest == "after":
                raise BlockLookupError("provider cannot answer")
            return 1

        def current_block(self):
            return 5

        def get_logs(self, address, topic0, start, end, topics=None):
            seen["end"] = end
            return []

    assert find_fill(Odd(fx), d, TABLE) is None
    assert seen["end"] == 5
