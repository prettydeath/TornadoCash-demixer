"""Window resolution: a failed block lookup must stop, not widen."""

import pytest

from tornado_demix.etherscan import BlockLookupError, EtherscanClient


class StubClient(EtherscanClient):
    def __init__(self, result):
        EtherscanClient.__init__(self, "KEY")
        self.result = result

    def call(self, params):
        return self.result


def test_plain_integer_response_resolves():
    assert StubClient("112200611").block_by_time(1700000000) == 112200611


def test_blockscout_nested_response_resolves():
    """Blockscout answers with an object, not a bare number."""
    assert StubClient({"blockNumber": "112200611"}).block_by_time(1700000000) == 112200611


@pytest.mark.parametrize("bad", [None, [], "", "not-a-number", {"foo": "bar"}])
def test_unresolvable_response_raises(bad):
    """Returning None here silently widened the window to the whole chain."""
    with pytest.raises(BlockLookupError):
        StubClient(bad).block_by_time(1700000000)


def test_the_error_names_the_timestamp_and_the_response():
    with pytest.raises(BlockLookupError) as exc:
        StubClient({"foo": "bar"}).block_by_time(1700000000)
    message = str(exc.value)
    assert "1700000000" in message
    assert "foo" in message


def test_a_pool_that_cannot_resolve_is_skipped_and_recorded():
    """One unresolvable pool must not silently become a whole-chain search."""
    from tornado_demix.demix import run_demix
    from tornado_demix.networks import Network
    from tornado_demix.pools import Pool

    net = Network("testnet", 1, "ETH", [Pool("0x" + "a" * 40, 1.0, "ETH")])
    pool_addr = net.pools[0].address

    class Client:
        def internal_txs(self, addr):
            return []

        def outgoing_txs(self, addr):
            return [
                {
                    "from": "0x" + "b" * 40,
                    "to": pool_addr,
                    "value": str(10**18),
                    "timeStamp": "1000",
                    "blockNumber": "10",
                    "hash": "0xd1",
                    "gasPrice": "1",
                    "isError": "0",
                }
            ]

        def block_by_time(self, ts, closest="before"):
            raise BlockLookupError("provider returned None")

    data = run_demix(Client(), "0x" + "b" * 40, network=net)
    assert data["denoms"] == {}
    assert [u["pool_key"] for u in data["unresolved"]] == ["1 ETH"]


def test_each_voucher_gets_its_own_window():
    """Two vouchers two years apart are two events, not one 760-day window."""
    from tornado_demix.demix import voucher_windows

    vouchers = [
        {"first_ts": 0, "last_ts": 3600, "count": 2},
        {"first_ts": 730 * 86400, "last_ts": 730 * 86400 + 3600, "count": 1},
    ]
    windows = voucher_windows(vouchers, window_days=30)
    assert len(windows) == 2
    assert windows[0]["end_ts"] - windows[0]["first_ts"] <= 31 * 86400
    assert windows[1]["end_ts"] - windows[1]["first_ts"] <= 31 * 86400


def test_overlapping_voucher_windows_are_merged():
    """Adjacent vouchers must not cause the same block range to be fetched twice."""
    from tornado_demix.demix import voucher_windows

    vouchers = [
        {"first_ts": 0, "last_ts": 100, "count": 1},
        {"first_ts": 86400, "last_ts": 86500, "count": 1},
    ]
    windows = voucher_windows(vouchers, window_days=30)
    assert len(windows) == 1
    assert windows[0]["first_ts"] == 0


def test_a_single_voucher_window_is_exactly_the_requested_span():
    from tornado_demix.demix import voucher_windows

    windows = voucher_windows([{"first_ts": 1000, "last_ts": 1000, "count": 1}], window_days=30)
    assert windows[0]["first_ts"] == 1000
    assert windows[0]["end_ts"] == 1000 + 30 * 86400


def test_exit_window_hours_narrows_the_forward_reach():
    from tornado_demix.demix import voucher_windows

    windows = voucher_windows(
        [{"first_ts": 1000, "last_ts": 1200, "count": 1}], window_days=30, exit_window_hours=6
    )
    # forward reach is 6 hours from the last deposit, not 30 days
    assert windows[0]["first_ts"] == 1000
    assert windows[0]["end_ts"] == 1200 + 6 * 3600


def test_exit_window_none_keeps_the_full_window():
    from tornado_demix.demix import voucher_windows

    default = voucher_windows(
        [{"first_ts": 1000, "last_ts": 1000, "count": 1}], window_days=30, exit_window_hours=None
    )
    assert default[0]["end_ts"] == 1000 + 30 * 86400


def test_exit_window_non_positive_falls_back_to_window_days():
    # A non-positive exit window is meaningless and must not run a degenerate
    # empty search - it falls back to the full window instead.
    from tornado_demix.demix import voucher_windows

    for bad in (0, -5):
        w = voucher_windows(
            [{"first_ts": 1000, "last_ts": 1000, "count": 1}], window_days=30, exit_window_hours=bad
        )
        assert w[0]["end_ts"] == 1000 + 30 * 86400


def test_two_withdrawals_in_one_tx_to_one_recipient_both_survive():
    """A batched withdrawal: same tx and recipient, distinct nullifiers.
    The old (tx_hash, to) dedup dropped the second."""
    from tornado_demix.demix import analyze_denom_events
    from tornado_demix.networks import Network
    from tornado_demix.pools import Pool

    net = Network("ethereum", 1, "ETH", [Pool("0x" + "a" * 40, 1.0, "ETH")])
    pool = net.pools[0]

    def _log(to, nullifier):
        data = ("0" * 24 + to[2:]) + nullifier + format(10**16, "064x")
        return {
            "data": "0x" + data,
            "topics": ["0xtopic0", "0x" + "0" * 24 + "1" * 40],
            "gasPrice": "0x1",
            "transactionHash": "0xbatch",
            "blockNumber": "0x64",
            "timeStamp": "0x3e8",
        }

    recipient = "0x" + "b" * 40

    class Client:
        def block_by_time(self, ts, closest="before"):
            return 100

        def get_logs(self, address, topic0, start_block, end_block):
            return [_log(recipient, "ab" * 32), _log(recipient, "cd" * 32)]

    res = analyze_denom_events(
        Client(), pool, [{"first_ts": 1000, "last_ts": 1000, "count": 1}], 30
    )
    assert res["total_qualifying_withdrawals"] == 2
    assert res["counts"][recipient] == 2
