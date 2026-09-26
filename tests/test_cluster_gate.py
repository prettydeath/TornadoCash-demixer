"""Regression test for Finding 1 (audit): the cluster layer gate must reuse
demix's gated ``candidates_by_count``, not recompute candidates from raw
``counts``.

Before the fix, ``_demix_layers`` recomputed:

    candidates = [addr for addr, hits in res["counts"].items() if hits == need]

gated only by ``layer_cap`` (35). A single-note voucher (need=1) makes every
recipient in the window a "hit" of count 1, so with the recipient count under
the layer cap the raw recompute admits the whole field as candidates to trace
forward -- the exact single-note fabrication that ``candidates_by_count`` (see
``tornado_demix/heuristics.py``, ``MIN_COUNT_DISCRIMINATION``) was built to
shut out in ``demix``/``multi``. ``cluster`` bypassed that gate entirely.

These tests drive the real ``run_demix`` (via a stub client, no network) so
the gated ``candidates_by_count`` is genuinely computed, not hand-waved.
"""

from tornado_demix import cluster
from tornado_demix.constants import TOPIC_WITHDRAWAL, ZERO_ADDRESS
from tornado_demix.networks import Network
from tornado_demix.pools import Pool

WALLET = "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"
POOL_1_ETH = "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936"


def _tx(to, value_eth, ts, block, tx_hash, gas_price=1, is_error="0"):
    return {
        "from": WALLET,
        "to": to,
        "value": str(int(value_eth * 10**18)),
        "timeStamp": str(ts),
        "blockNumber": str(block),
        "hash": tx_hash,
        "gasPrice": str(gas_price),
        "isError": is_error,
    }


def _wlog(to, nullifier, relayer, fee_wei, ts=1100, block=100, gas_price=1):
    """Build a raw Withdrawal log the way the logs endpoint returns them."""
    data = ("0" * 24 + to[2:]) + nullifier + format(fee_wei, "064x")
    return {
        "data": "0x" + data,
        "topics": [TOPIC_WITHDRAWAL, "0x" + "0" * 24 + relayer[2:]],
        "gasPrice": hex(gas_price),
        "transactionHash": "0x" + nullifier,
        "blockNumber": hex(block),
        "timeStamp": hex(ts),
    }


class _FakeClient:
    """A client that replays a fixed tx list and a fixed set of Withdrawal logs."""

    def __init__(self, txs, logs):
        self._txs = txs
        self._logs = logs

    def internal_txs(self, addr):

        return []

    def outgoing_txs(self, addr):
        return self._txs

    def block_by_time(self, ts, closest="before"):
        return int(ts)

    def get_logs(self, address, topic0, start_block, end_block):
        return self._logs


def _recipient(i):
    return "0x" + format(i, "040x")


def _network():
    return Network("ethereum", 1, "ETH", [Pool(POOL_1_ETH, 1.0, "ETH")])


def _single_note_voucher_client(n_recipients=30):
    """One 1 ETH deposit (count 1); ``n_recipients`` each hit exactly once.

    Every recipient shares count 1, so the count-match eliminates nobody: the
    audit's headline single-note-voucher fabrication case, reproduced for the
    cluster path with 30 recipients (under LAYER_CAP=35).
    """
    deposit = _tx(POOL_1_ETH, 1.0, 1000, 10, "0xdep")
    logs = [
        _wlog(_recipient(i), format(i, "064x"), ZERO_ADDRESS, 0) for i in range(1, n_recipients + 1)
    ]
    return _FakeClient([deposit], logs)


def _discriminating_voucher_client():
    """A 3-note voucher: one recipient hit 3x, 28 others hit once each.

    Count 3 is shared by only one of 29 distinct recipients: genuinely
    discriminating, so it must still populate a usable layer.
    """
    deposits = [_tx(POOL_1_ETH, 1.0, 1000 + i * 10, 10 + i, "0xdep%d" % i) for i in range(3)]
    winner = _recipient(1)
    logs = [_wlog(winner, format(9000 + k, "064x"), ZERO_ADDRESS, 0) for k in range(3)]
    logs.extend(_wlog(_recipient(i), format(i, "064x"), ZERO_ADDRESS, 0) for i in range(2, 30))
    return _FakeClient(deposits, logs), winner


def _params(network):
    return dict(window_days=30, fee_lo=0.90, fee_hi=0.995, gap_hours=24, network=network)


def test_single_note_voucher_yields_no_usable_cluster_layer(tmp_path):
    """A single-note voucher must produce NO usable cluster layer.

    Pre-fix, ``_demix_layers`` recomputed 30 raw count-1 candidates (all
    under the 35 layer-cap) and traced every one of them forward -- pure
    noise dressed as a lead. ``demix``'s own gated
    ``candidates_by_count[1]`` is empty for this wallet, and ``cluster`` must
    agree: the layer is unusable, not "30 candidates under the cap".
    """
    network = _network()
    client = _single_note_voucher_client(n_recipients=30)
    scope = cluster.cache_scope(network)

    fingerprint, layers = cluster._demix_layers(
        client, WALLET, _params(network), str(tmp_path), cluster.LAYER_CAP, scope
    )

    assert fingerprint == {"1 ETH": 1}
    assert layers["1 ETH"] is None, (
        "single-note voucher produced a usable cluster layer with {} "
        "candidates -- the discrimination gate was bypassed".format(len(layers.get("1 ETH") or {}))
    )


def test_discriminating_voucher_still_yields_a_usable_layer(tmp_path):
    """Positive control: a count few recipients share still traces forward."""
    network = _network()
    client, winner = _discriminating_voucher_client()
    scope = cluster.cache_scope(network)

    fingerprint, layers = cluster._demix_layers(
        client, WALLET, _params(network), str(tmp_path), cluster.LAYER_CAP, scope
    )

    assert fingerprint == {"1 ETH": 3}
    assert layers["1 ETH"] is not None
    assert list(layers["1 ETH"]) == [winner]


def _two_vouchers_one_pool_client():
    """Two separate vouchers into the same pool: count 2, then (>gap_hours
    later) count 3. fingerprint sums them to need=5 for that pool - a
    hypothesis that one address received all 5 withdrawals. One address
    (``winner``) gets exactly 5 hits; 20 others get 1 hit each as noise.

    ``run_demix``'s own ``candidates_by_count`` is keyed by the *individual*
    voucher counts {2, 3} (target_counts), never their sum: it has no "5"
    key at all. A fix that reads ``candidates_by_count.get(need, [])``
    therefore returns [] here even though winner is a real, discriminating
    consolidation address - this is the regression the corrected ruling
    (raw recompute + count_discrimination gate) exists to avoid.
    """
    winner = _recipient(1)
    deposits = [
        _tx(POOL_1_ETH, 1.0, 1000, 10, "0xdA0"),
        _tx(POOL_1_ETH, 1.0, 1010, 11, "0xdA1"),
        # >24h (gap_hours) after the first pair -> a second, separate voucher
        _tx(POOL_1_ETH, 1.0, 1000 + 30 * 3600, 40, "0xdB0"),
        _tx(POOL_1_ETH, 1.0, 1010 + 30 * 3600, 41, "0xdB1"),
        _tx(POOL_1_ETH, 1.0, 1020 + 30 * 3600, 42, "0xdB2"),
    ]
    logs = [_wlog(winner, format(9000 + k, "064x"), ZERO_ADDRESS, 0) for k in range(5)]
    logs.extend(_wlog(_recipient(i), format(i, "064x"), ZERO_ADDRESS, 0) for i in range(2, 22))
    return _FakeClient(deposits, logs), winner


def test_two_same_pool_vouchers_still_find_the_consolidation_address(tmp_path):
    """Regression: need (summed fingerprint) must not be looked up as a
    candidates_by_count key, since that dict is keyed by individual voucher
    counts and never their sum. The address that received all 5 withdrawals
    across two vouchers (2 + 3) must still be found and gated correctly."""
    network = _network()
    client, winner = _two_vouchers_one_pool_client()
    scope = cluster.cache_scope(network)

    fingerprint, layers = cluster._demix_layers(
        client, WALLET, _params(network), str(tmp_path), cluster.LAYER_CAP, scope
    )

    assert fingerprint == {"1 ETH": 5}
    assert layers["1 ETH"] is not None, (
        "the address that received all 5 withdrawals across two vouchers "
        "was silently dropped because candidates_by_count has no '5' key"
    )
    assert list(layers["1 ETH"]) == [winner]
