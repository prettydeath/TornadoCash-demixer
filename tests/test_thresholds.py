"""Each methodology threshold, tested exactly at its boundary."""

from collections import Counter, defaultdict

from tests.fixtures import demix_result as fx
from tornado_demix.demix import (
    NATIVE_DENOM_TOLERANCE,
    cluster_vouchers,
    detect_deposits,
    voucher_windows,
)
from tornado_demix.heuristics import (
    MAX_GAS_PRICE_SHARE,
    MIN_COUNT_DISCRIMINATION,
    MIN_FIELD_SIZE,
    SIGNAL_WEIGHTS,
    apply_heuristics,
    confidence_band,
    count_is_evidence,
)
from tornado_demix.multi import SYNC_GAP_HOURS, SYNC_MAX_SPAN_HOURS


def _field(counts):
    """A pool result whose recipients have the given withdrawal counts."""
    return {"counts": Counter({"0x%040x" % (i + 1): c for i, c in enumerate(counts)})}


def test_the_documented_thresholds_and_weights():
    assert MIN_COUNT_DISCRIMINATION == 0.5
    assert MIN_FIELD_SIZE == 5
    assert MAX_GAS_PRICE_SHARE == 3
    assert NATIVE_DENOM_TOLERANCE == 0.005
    assert (SYNC_GAP_HOURS, SYNC_MAX_SPAN_HOURS) == (6.0, 12.0)
    assert SIGNAL_WEIGHTS == {
        "count_match": 0.30,
        "self_relayed": 0.25,
        "gas_price": 0.25,
        "linked": 0.40,
        "profile_match": 0.35,
    }


def test_a_unique_count_needs_a_field_of_five():
    assert not count_is_evidence(_field([3, 1, 1, 1]), 3)
    assert count_is_evidence(_field([3, 1, 1, 1, 1]), 3)


def test_a_count_at_exactly_the_discrimination_floor_counts():
    # five recipients, three share the count: disc = 1 - 2/4 = 0.5
    assert count_is_evidence(_field([3, 3, 3, 1, 1]), 3)
    # four share it: disc = 0.25
    assert not count_is_evidence(_field([3, 3, 3, 3, 1]), 3)


def _gas_data(sharing):
    """One candidate reusing the deposit gas price, ``sharing`` withdrawals in total using it."""
    detail = defaultdict(list)
    for i in range(10):
        addr = "0x%040x" % (i + 1)
        gas = 999 if i < sharing else 5
        detail[addr] = [{"hash": "0x%x" % i, "gas_price": gas, "block": 1}]
    res = {"counts": Counter({a: 1 for a in detail}), "detail": detail, "target_counts": []}
    return {"deposits": [{"gas_price": 999}], "denoms": {"0.1 ETH": res}}


def test_a_gas_price_shared_by_three_withdrawals_still_counts_but_not_by_four():
    first = "0x%040x" % 1
    held = apply_heuristics(_gas_data(3), counterparties=set())
    assert "gas_price" in held["denoms"]["0.1 ETH"]["signals"][first]
    common = apply_heuristics(_gas_data(4), counterparties=set())
    assert "gas_price" not in common["denoms"]["0.1 ETH"]["signals"][first]


def test_a_gas_price_match_alone_is_moderate():
    assert confidence_band({"gas_price"}) == "moderate"


def _tx(value_eth, ts, tx_hash):
    return {
        "from": fx.WALLET,
        "to": fx.POOL_1_ETH,
        "value": str(int(value_eth * 10**18)),
        "timeStamp": str(ts),
        "blockNumber": str(ts),
        "hash": tx_hash,
        "gasPrice": "1",
        "isError": "0",
    }


def test_a_native_deposit_within_half_a_percent_is_detected():
    txs = [_tx(0.996, 100, "0xin"), _tx(0.994, 200, "0xout")]
    deposits = detect_deposits(None, fx.WALLET, network=fx.network(), txs=txs)
    assert [d["hash"] for d in deposits] == ["0xin"]


def _deposit(ts):
    return {"pool_key": "1 ETH", "denom": 1.0, "asset": "ETH", "ts": ts, "block": ts}


def test_deposits_exactly_gap_hours_apart_are_one_voucher():
    gap = 24 * 3600
    assert len(cluster_vouchers([_deposit(0), _deposit(gap)], gap_hours=24)) == 1
    assert len(cluster_vouchers([_deposit(0), _deposit(gap + 1)], gap_hours=24)) == 2


def test_windows_that_touch_are_merged():
    vouchers = [
        {"first_ts": 0, "last_ts": 0},
        {"first_ts": 3600, "last_ts": 3600},
        {"first_ts": 3601 + 3600, "last_ts": 3601 + 3600},
    ]
    windows = voucher_windows(vouchers, window_days=30, exit_window_hours=1)
    assert [w["voucher_count"] for w in windows] == [2, 1]
