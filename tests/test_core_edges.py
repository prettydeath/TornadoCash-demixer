"""Edges of the core functions that mutation testing (mutmut) found unpinned."""

from tests.fixtures import demix_result as fx
from tornado_demix.demix import (
    cluster_vouchers,
    detect_deposits,
    wallet_counterparties,
)
from tornado_demix.heuristics import count_discrimination

HOUR = 3600


def test_a_unique_count_in_a_field_of_two_discriminates_fully():
    res = {"counts": {"0xa": 2, "0xb": 1}, "unique_recipients": 2}
    assert count_discrimination(res, 2) == 1.0


def test_counterparties_are_both_sides_case_insensitive_and_never_the_wallet():
    wallet = fx.WALLET.upper().replace("0X", "0x")
    txs = [
        {"from": fx.ALICE.upper().replace("0X", "0x"), "to": wallet},
        {"from": wallet, "to": fx.BOB},
        {"from": wallet, "to": ""},  # contract creation
        {"from": wallet, "to": wallet},
    ]
    assert wallet_counterparties(wallet, txs) == {fx.ALICE, fx.BOB}


def _dep(ts, block=1):
    return {"pool_key": "1 ETH", "denom": 1.0, "asset": "ETH", "ts": ts, "block": block}


def test_a_voucher_carries_its_pool_asset_first_block_and_deposits():
    deps = [_dep(1_000_000, block=7), _dep(1_000_000 + HOUR, block=9)]
    (voucher,) = cluster_vouchers(deps)
    assert voucher["denom"] == 1.0 and voucher["asset"] == "ETH"
    assert voucher["first_block"] == 7 and voucher["deposits"] == deps
    assert (voucher["first_ts"], voucher["last_ts"], voucher["count"]) == (
        1_000_000,
        1_000_000 + HOUR,
        2,
    )


def test_the_voucher_span_limit_is_inclusive_and_measured_from_the_first_deposit():
    start = 1_700_000_000
    at_limit = [_dep(start), _dep(start + 3 * HOUR), _dep(start + 6 * HOUR)]
    assert [v["count"] for v in cluster_vouchers(at_limit, max_span_hours=6)] == [3]
    past = [_dep(start), _dep(start + 3 * HOUR), _dep(start + 6 * HOUR + 1)]
    assert [v["count"] for v in cluster_vouchers(past, max_span_hours=6)] == [2, 1]


class _RecordingClient:
    def __init__(self):
        self.asked = []

    def outgoing_txs(self, wallet):
        self.asked.append(("txlist", wallet))
        return []

    def internal_txs(self, wallet):
        self.asked.append(("internal", wallet))
        return []

    def token_transfers(self, wallet, contract=None, **kw):
        self.asked.append(("tokens", wallet))
        return []


def test_detect_deposits_reads_every_list_for_the_wallet_itself():
    from tests.test_demix import _token_network

    client = _RecordingClient()
    detect_deposits(client, fx.WALLET, network=_token_network())
    assert sorted(client.asked) == [
        ("internal", fx.WALLET),
        ("tokens", fx.WALLET),
        ("txlist", fx.WALLET),
    ]


def test_a_deposit_records_the_pool_it_was_sent_to():
    tx = {
        "from": fx.WALLET,
        "to": fx.POOL_1_ETH,
        "value": str(10**18),
        "timeStamp": "1000",
        "blockNumber": "10",
        "hash": "0xdep",
        "isError": "0",
        "gasPrice": "1",
    }
    (deposit,) = detect_deposits(None, fx.WALLET, network=fx.network(), txs=[tx], internal_txs=[])
    assert deposit["to"] == fx.POOL_1_ETH


# Transfer mode (the legacy internal-transfer heuristic) had no test at all.
class _TransferClient:
    def __init__(self, internal):
        self.internal = internal
        self.ranges = []

    def block_by_time(self, ts, closest="before"):
        return 100 if closest == "before" else 200

    def current_block(self):
        return 200

    def internal_from(self, contract, start_block, end_block):
        self.ranges.append((contract, start_block, end_block))
        return list(self.internal)


def _itx(to, eth, h, frm=fx.POOL_1_ETH, error="0", ts=2000):
    return {
        "from": frm,
        "to": to,
        "value": str(int(eth * 10**18)),
        "hash": h,
        "isError": error,
        "timeStamp": str(ts),
    }


def test_transfer_mode_counts_only_pool_payouts_inside_the_fee_window():
    from tornado_demix.demix import analyze_denom

    pool = fx.network().by_key["1 ETH"]
    voucher = {"pool_key": "1 ETH", "count": 2, "first_ts": 1000, "last_ts": 1000}
    internal = [
        _itx(fx.ALICE, 0.99, "0x1"),
        _itx(fx.ALICE, 0.95, "0x2"),
        _itx(fx.ALICE, 0.95, "0x2"),  # the same transfer seen in two windows
        _itx(fx.BOB, 0.90, "0x3"),  # exactly at the lower bound
        _itx(fx.BOB, 0.995, "0x4"),  # exactly at the upper bound
        _itx(fx.BOB, 1.0, "0x5"),  # no fee: above the window
        _itx(fx.BOB, 0.5, "0x6"),  # far below
        _itx(fx.BOB, 0.95, "0x7", error="1"),  # failed
        _itx(fx.BOB, 0.95, "0x8", frm=fx.ALICE),  # not from the pool
    ]
    client = _TransferClient(internal)
    res = analyze_denom(client, pool, [voucher], 30, 0.90, 0.995)

    assert res["mode"] == "transfers"
    assert client.ranges == [(pool.address, 100, 200)]
    assert dict(res["counts"]) == {fx.ALICE: 2, fx.BOB: 2}
    assert res["total_qualifying_withdrawals"] == 4 and res["unique_recipients"] == 2
    assert [d["hash"] for d in res["detail"][fx.BOB]] == ["0x3", "0x4"]
    assert res["fee_window"] == [0.9, 0.995]
    assert res["window_blocks"] == [100, 200]
