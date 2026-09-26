"""Recipient-side characterisation of an exit-candidate address.

Driven with a stub client so the whole path - activity envelope, pool-inflow
detection, classification and next-hop ranking - runs without a network.
"""

from tornado_demix.characterize import characterize_address
from tornado_demix.networks import Network
from tornado_demix.pools import Pool

P01 = "0x" + "1" * 40  # 0.1 ETH pool
P1 = "0x" + "2" * 40  # 1 ETH pool
P10 = "0x" + "3" * 40  # 10 ETH pool
ADDR = "0x" + "e" * 40  # the candidate
HOP = "0x" + "d" * 40  # its dominant next hop
OTHER = "0x" + "f" * 40


def _network():
    return Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(P01, 0.1, "ETH", 18, None),
            Pool(P1, 1.0, "ETH", 18, None),
            Pool(P10, 10.0, "ETH", 18, None),
        ],
    )


def _internal(frm, to, value, ts, h):
    return {"from": frm, "to": to, "value": str(value), "timeStamp": str(ts), "hash": h}


def _normal(frm, to, value, ts, h):
    return {"from": frm, "to": to, "value": str(value), "timeStamp": str(ts), "hash": h}


class StubClient:
    def __init__(self, normal, internal):
        self._normal = normal
        self._internal = internal

    def outgoing_txs(self, address):
        return self._normal

    def internal_txs(self, address):
        return self._internal


def test_pool_inflows_are_detected_with_pool_key_and_value():
    internal = [
        _internal(P01, ADDR, 10**17, 1000, "0xa"),  # 0.1 ETH
        _internal(P1, ADDR, 994 * 10**15, 1100, "0xb"),  # 0.994 ETH (fee)
        _internal(OTHER, ADDR, 5 * 10**18, 1200, "0xc"),  # not a pool -> ignored
    ]
    info = characterize_address(StubClient([], internal), ADDR, _network())
    keys = [r["pool_key"] for r in info["pool_inflows"]]
    assert keys == ["0.1 ETH", "1 ETH"]
    assert info["pool_inflows"][0]["value"] == 0.1
    assert info["distinct_pools"] == ["0.1 ETH", "1 ETH"]
    assert info["inflow_totals"]["ETH"] == round(0.1 + 0.994, 6)


def test_many_distinct_pools_classify_as_aggregator():
    internal = [
        _internal(P01, ADDR, 10**17, 1000, "0xa"),
        _internal(P1, ADDR, 10**18, 1100, "0xb"),
        _internal(P10, ADDR, 10**19, 1200, "0xc"),
    ]
    info = characterize_address(StubClient([], internal), ADDR, _network())
    assert info["classification"] == "aggregator"
    assert "3 distinct pool(s)" in info["classification_reason"]


def test_aggregator_by_inflow_count_with_a_single_pool():
    # The OR arm: >=10 inflows from just one pool is still an aggregator.
    internal = [_internal(P1, ADDR, 10**18, 1000 + i, "0x%d" % i) for i in range(10)]
    info = characterize_address(StubClient([], internal), ADDR, _network())
    assert info["classification"] == "aggregator"
    assert info["distinct_pools"] == ["1 ETH"]


def test_relayer_fee_sized_transfer_is_not_counted_as_inflow():
    # The pool also sends the small relayer fee as its own internal transfer;
    # a fee-sized amount (0.006 ETH on the 1 ETH pool) is not a received note.
    internal = [_internal(P1, ADDR, 6 * 10**15, 1000, "0xfee")]
    info = characterize_address(StubClient([], internal), ADDR, _network())
    assert info["pool_inflows"] == []
    assert info["classification"] == "no pool inflows"


def test_erc20_pool_transfers_are_out_of_scope():
    # A non-native (ERC-20) pool is excluded from inflow detection.
    token_pool = "0x" + "9" * 40
    net = Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(P1, 1.0, "ETH", 18, None),
            Pool(token_pool, 100.0, "DAI", 18, "0x" + "7" * 40),  # token != None
        ],
    )
    internal = [_internal(token_pool, ADDR, 100 * 10**18, 1000, "0xt")]
    info = characterize_address(StubClient([], internal), ADDR, net)
    assert info["pool_inflows"] == []


def test_next_hop_limit_caps_the_list():
    normal = [_normal(ADDR, "0x" + f"{i:040x}", 10**18, 2000 + i, "0x%d" % i) for i in range(5)]
    info = characterize_address(StubClient(normal, []), ADDR, _network(), next_hop_limit=2)
    assert len(info["top_next_hops"]) == 2


def test_a_single_low_activity_inflow_is_a_possible_personal_exit():
    internal = [_internal(P1, ADDR, 10**18, 1000, "0xb")]
    info = characterize_address(StubClient([], internal), ADDR, _network())
    assert info["classification"] == "possible personal exit"


def test_no_pool_inflows_is_flagged():
    internal = [_internal(OTHER, ADDR, 10**18, 1000, "0xz")]
    info = characterize_address(StubClient([], internal), ADDR, _network())
    assert info["classification"] == "no pool inflows"
    assert info["pool_inflows"] == []


def test_high_activity_is_a_service():
    normal = [_normal(ADDR, OTHER, 0, 1000 + i, "0x%d" % i) for i in range(600)]
    info = characterize_address(StubClient(normal, []), ADDR, _network())
    assert info["classification"] == "high-activity service"
    assert info["normal_txs"] == 600


def test_next_hops_are_ranked_by_count():
    normal = (
        [_normal(ADDR, HOP, 10**18, 2000 + i, "0xh%d" % i) for i in range(5)]
        + [_normal(ADDR, OTHER, 10**18, 3000, "0xo")]
        + [_normal(OTHER, ADDR, 10**18, 4000, "0xin")]  # incoming, ignored
    )
    info = characterize_address(StubClient(normal, []), ADDR, _network())
    assert info["top_next_hops"][0]["address"] == HOP
    assert info["top_next_hops"][0]["count"] == 5
    assert info["top_next_hops"][0]["total_value"] == 5.0


def test_labels_annotate_the_subject_and_next_hops():
    normal = [_normal(ADDR, HOP, 10**18, 2000, "0xh")]
    labels = {
        ADDR.lower(): {"label": "Subject Co", "category": "exchange", "entity": "S"},
        HOP.lower(): {"label": "Tornado.Cash: Router", "category": "mixer", "entity": "blocked"},
    }
    info = characterize_address(StubClient(normal, []), ADDR, _network(), labels=labels)
    assert info["label"]["label"] == "Subject Co"
    assert info["top_next_hops"][0]["label"]["category"] == "mixer"


def test_no_labels_leaves_annotations_none():
    normal = [_normal(ADDR, HOP, 10**18, 2000, "0xh")]
    info = characterize_address(StubClient(normal, []), ADDR, _network())
    assert info["label"] is None
    assert info["top_next_hops"][0]["label"] is None


def test_activity_envelope_spans_both_lists():
    normal = [_normal(ADDR, OTHER, 0, 5000, "0xn")]
    internal = [_internal(P1, ADDR, 10**18, 1000, "0xi")]
    info = characterize_address(StubClient(normal, internal), ADDR, _network())
    assert info["first_activity_ts"] == 1000
    assert info["last_activity_ts"] == 5000
