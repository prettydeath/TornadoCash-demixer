"""Boundary scenarios from the independent audit of v2.10.0.

The audit wrote these tests against v2.10.0 without running them. Most pin an
edge that held already; four documented defects (a profile match missing from
the candidate list, token transfers ignored for ``linked``, a multi-session
consolidation invisible to demix, a window end below its start), and are kept
here inverted, as regression tests of the fixes.
"""

from __future__ import annotations

import time

from tests.fixtures import demix_result as fx
from tornado_demix.constants import TOPIC_WITHDRAWAL, ZERO_ADDRESS
from tornado_demix.demix import (
    cluster_vouchers,
    detect_deposits,
    run_demix,
    voucher_windows,
    wallet_counterparties,
)
from tornado_demix.etherscan import EtherscanClient
from tornado_demix.heuristics import (
    SIGNAL_WEIGHTS,
    _score,
    apply_heuristics,
    credit_profile_match,
    ranked_candidates,
)
from tornado_demix.networks import Network
from tornado_demix.pools import Pool

WALLET = fx.WALLET
RELAYER = "0x" + "cc" * 20

DAI_TOKEN = "0x6b175474e89094c44da98b954eedeac495271d0f"
DAI_POOL = "0xd4b88df4d29f5cedd6857912842cff3b20c8cfa3"


# --------------------------------------------------------------------------
# Shared helpers (copied from tests/test_demix.py's style, kept local so this
# file drops into tests/ without a new cross-file import).
# --------------------------------------------------------------------------
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


def _ttx(to, raw, ts, tx_hash, contract, sender=WALLET):
    return {
        "from": sender,
        "to": to,
        "value": str(raw),
        "timeStamp": str(ts),
        "blockNumber": "10",
        "hash": tx_hash,
        "contractAddress": contract,
        "tokenDecimal": "18",
        "tokenSymbol": "DAI",
        "gasPrice": "7",
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


def _recipient(i):
    return "0x" + format(i, "040x")


class _FakeClient:
    """A client that replays a fixed tx list and a fixed set of Withdrawal logs
    (identical shape to tests/test_demix.py's ``_FakeClient``)."""

    def __init__(self, txs, logs, token_txs=None):
        self._txs = txs
        self._logs = logs
        self._token_txs = token_txs or []

    def internal_txs(self, addr):
        return []

    def outgoing_txs(self, addr):
        return self._txs

    def token_transfers(self, addr, contract=None):
        return self._token_txs

    def block_by_time(self, ts, closest="before"):
        return int(ts)

    def get_logs(self, address, topic0, start_block, end_block):
        return self._logs


# --------------------------------------------------------------------------
# 1. A profile match alone admits a candidate.
# --------------------------------------------------------------------------
def test_a_profile_match_alone_admits_a_candidate():
    """An exact two-pool profile match admits an address to the candidate list
    even when neither pool's field is large enough for a count match.
    """
    addr = "0x" + "e" * 40
    other1, other2, other3 = ("0x" + h * 40 for h in ("1", "2", "3"))

    def _pool_result(hits_addr, others):
        counts = {addr: hits_addr, **{o: 1 for o in others}}
        detail = {
            a: [{"self_relayed": False, "value": 1.0, "ts": 1000}] * n for a, n in counts.items()
        }
        return {
            "denom": 1.0,
            "asset": "ETH",
            "counts": counts,
            "unique_recipients": len(counts),  # 3, then 2 - both < MIN_FIELD_SIZE (5)
            "target_counts": [hits_addr],
            "detail": detail,
            "withdrawals": [],
        }

    data = {
        "wallet": "0xw",
        "deposits": [],
        "denoms": {
            "1 ETH": _pool_result(2, [other1, other2]),
            "10 ETH": _pool_result(1, [other3]),
        },
    }
    apply_heuristics(data, counterparties=set())

    # Neither pool's count clears the field-size floor: no count_match anywhere,
    # even though the count on "1 ETH" is, in isolation, fully discriminating
    # (disc == 1.0) - exactly the "two or three recipients" trap MIN_FIELD_SIZE
    # exists for (METHODOLOGY.md, "Candidate matching").
    assert "count_match" not in data["denoms"]["1 ETH"]["signals"][addr]
    assert "count_match" not in data["denoms"]["10 ETH"]["signals"][addr]
    before = data["denoms"]["1 ETH"]["confidence"][addr]

    credit_profile_match(data, ["1 ETH", "10 ETH"], addr)

    assert "profile_match" in data["denoms"]["1 ETH"]["signals"][addr]
    assert data["denoms"]["1 ETH"]["confidence"][addr] > before  # the score DID rise ...

    ranked = ranked_candidates(data)
    assert addr in {r["address"] for r in ranked}


# --------------------------------------------------------------------------
# 2. Identical token transfers in one transaction are all kept.
# --------------------------------------------------------------------------
def test_two_identical_token_transfers_in_one_tx_are_both_kept():
    """Two identical Transfer rows of one transaction on one page are two
    transfers (a Safe depositing two notes of one pool), not a duplicate.
    """

    class _OnePageClient(EtherscanClient):
        def __init__(self, rows):
            EtherscanClient.__init__(self, "KEY")
            self.rows = rows

        def call(self, params):
            return list(self.rows) if params["page"] == 1 else []

    row = _ttx(DAI_POOL, 100000 * 10**18, 1000, "0xbatch", DAI_TOKEN)
    client = _OnePageClient([dict(row), dict(row)])  # two separate Transfer rows, one tx

    got = client.token_transfers(WALLET)

    assert len(got) == 2, (
        "fetch_all's dedup collapsed two distinct same-tx token transfers into "
        "one; a Safe depositing two notes of the same pool in a single "
        "transaction is undercounted by one deposit (detect_deposits would "
        "then report only 1 deposit where 2 really happened)"
    )


# --------------------------------------------------------------------------
# 3. ERC-20 transfers count for linked.
# --------------------------------------------------------------------------
def _token_network():
    return Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(fx.POOL_1_ETH, 1.0, "ETH", 18, None),
            Pool(DAI_POOL, 100, "DAI", 18, DAI_TOKEN),
        ],
    )


def test_a_counterparty_known_only_through_an_erc20_transfer_is_linked():
    """A direct ERC-20 transfer between the depositor and a candidate earns
    ``linked``, like a native transfer does.
    """
    candidate = "0x" + "c" * 40
    deposit = _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xdep")
    # A direct, real transfer from the wallet to `candidate` - NOT a deposit
    # (1 DAI != the 100 DAI pool's denomination).
    token_txs = [_ttx(candidate, 1 * 10**18, 900, "0xtoken1", DAI_TOKEN)]
    # `candidate` also shows up as a lone, self-relayed exit so it has a
    # detail/signals row to inspect at all.
    logs = [_wlog(candidate, "ab" * 32, ZERO_ADDRESS, 0)]

    client = _FakeClient([deposit], logs, token_txs=token_txs)
    data = run_demix(client, WALLET, network=_token_network())

    # The relationship is real and detectable - just not with the tx lists
    # run_demix() actually hands to wallet_counterparties().
    assert candidate in wallet_counterparties(WALLET, token_txs)
    assert "linked" in data["denoms"]["1 ETH"]["signals"].get(candidate, [])


# --------------------------------------------------------------------------
# 4. A multi-session consolidation into one pool.
# --------------------------------------------------------------------------
def test_a_same_pool_multi_session_consolidation_is_a_demix_candidate():
    """Two sessions into one pool (2 and 3 notes) withdrawn in full to one
    address: the sum of the vouchers (5) is a target count, so demix lists it.
    """
    winner = _recipient(1)
    deposits = [
        _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xdA0"),
        _tx(fx.POOL_1_ETH, 1.0, 1010, 11, "0xdA1"),
        # >24h (gap_hours) after the first pair -> a second, separate voucher
        _tx(fx.POOL_1_ETH, 1.0, 1000 + 30 * 3600, 40, "0xdB0"),
        _tx(fx.POOL_1_ETH, 1.0, 1010 + 30 * 3600, 41, "0xdB1"),
        _tx(fx.POOL_1_ETH, 1.0, 1020 + 30 * 3600, 42, "0xdB2"),
    ]
    logs = [_wlog(winner, format(9000 + k, "064x"), ZERO_ADDRESS, 0) for k in range(5)]
    logs.extend(_wlog(_recipient(i), format(i, "064x"), ZERO_ADDRESS, 0) for i in range(2, 22))

    data = run_demix(_FakeClient(deposits, logs), WALLET, network=fx.network())
    res = data["denoms"]["1 ETH"]

    assert res["counts"][winner] == 5  # the withdrawals really are all there ...
    assert sorted(res["target_counts"]) == [2, 3, 5]  # the two sessions and their sum
    assert winner in res["candidates_by_count"][5]
    assert winner in {r["address"] for r in ranked_candidates(data)}


# --------------------------------------------------------------------------
# 5. _score has no clamp; the weights are checked on import.
# --------------------------------------------------------------------------
def test_score_has_no_defensive_clamp_on_out_of_range_weights():
    """_score does not clamp an out-of-range weight; the guard is the check of
    SIGNAL_WEIGHTS when heuristics is imported.
    """
    saved = dict(SIGNAL_WEIGHTS)
    try:
        SIGNAL_WEIGHTS["count_match"] = 1.4  # an out-of-range edit, hypothetically
        score = _score({"count_match"}, 1.0)
        # _score does not clamp; heuristics validates SIGNAL_WEIGHTS on import.
        assert not (0.0 <= score <= 1.0)
    finally:
        SIGNAL_WEIGHTS.clear()
        SIGNAL_WEIGHTS.update(saved)


# --------------------------------------------------------------------------
# 6. A lagging current block cannot invert a window.
# --------------------------------------------------------------------------
def test_a_lagging_current_block_never_ends_a_window_before_it_starts():
    """A current block behind the start block (a lagging proxy endpoint) is
    raised to the start block instead of producing an inverted range.
    """
    from tornado_demix import demix

    class LaggingCurrentBlock:
        def block_by_time(self, ts, closest="before"):
            return 1_000_000  # the timestamp endpoint is fresh/ahead

        def current_block(self):
            return 10  # the proxy endpoint is stale/lagging

        def get_logs(self, address, topic0, start_block, end_block):
            return []

    pool = fx.network().by_key["1 ETH"]
    # A window in the future, so its end is clamped to the current block.
    voucher = {
        "pool_key": "1 ETH",
        "count": 1,
        "first_ts": int(time.time()) + 10 * 86400,
        "last_ts": int(time.time()) + 10 * 86400,
        "first_block": 1,
        "deposits": [],
    }

    res = demix.analyze_denom_events(LaggingCurrentBlock(), pool, [voucher], window_days=30)
    window = res["windows"][0]

    assert window["end_block"] == window["start_block"]


# --------------------------------------------------------------------------
# Positive controls: edge cases explicitly named in the audit brief that DO
# hold, added because no existing test exercises them at exactly this edge.
# --------------------------------------------------------------------------
def test_count_discrimination_on_a_single_recipient_field_is_zero_not_a_crash():
    """unique <= 1 (a window with exactly one ever-seen recipient) is a
    distinct guard from the 'whole field shares the count' guard - both return
    0.0, but for a different reason (nothing to discriminate AGAINST). No
    existing test in test_scoring*.py exercises unique_recipients == 1."""
    from tornado_demix.heuristics import count_discrimination

    res = {"counts": {"0xonly": 7}, "unique_recipients": 1}
    assert count_discrimination(res, 7) == 0.0


def test_native_tolerance_boundary_is_inclusive_at_exactly_half_percent():
    """NATIVE_DENOM_TOLERANCE = 0.005; the comparison in demix._native_deposits
    is ``abs(value - denom) > denom * tolerance`` (strict '>'), so a deposit
    landing EXACTLY on the 0.5% boundary is accepted, not rejected. Float
    division (int(wei)/WEI) is safe here: the tolerance band (0.5% of the
    denomination) is ~13 orders of magnitude wider than a double's relative
    epsilon, so no realistic value can flip across this boundary due to
    rounding."""
    net = fx.network()
    denom_wei = int(1.0 * 10**18)
    boundary_wei = denom_wei + int(denom_wei * 0.005)  # exactly +0.5%
    tx = dict(_tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xboundary"))
    tx["value"] = str(boundary_wei)
    deposits = detect_deposits(None, WALLET, network=net, txs=[tx])
    assert len(deposits) == 1, "a deposit exactly at the 0.5% boundary must be accepted"


def test_gap_hours_chaining_can_make_one_voucher_span_far_more_than_gap_hours():
    """cluster_vouchers groups CONSECUTIVELY (each deposit compared to the
    previous one in its own cluster, ``cluster[-1]["ts"]`` - demix.py), the
    same single-linkage shape multi.deposit_synchronicity uses for wallet
    batches. multi.py explicitly warns about this for deposit_synchronicity
    ("Single-linkage batches can chain transitively into a group spanning
    days") and adds SYNC_MAX_SPAN_HOURS specifically to cap it. No analogous
    cap or warning exists for cluster_vouchers: a trickle of deposits each
    just inside gap_hours of the last can accumulate into one voucher whose
    own total span is many multiples of gap_hours, diluting the timing
    correlation voucher-level candidate matching depends on."""
    gap_hours = 24
    deposits = [
        {"pool_key": "1 ETH", "denom": 1.0, "asset": "ETH", "ts": i * gap_hours * 3600, "block": i}
        for i in range(10)  # each exactly gap_hours after the previous one
    ]
    vouchers = cluster_vouchers(deposits, gap_hours=gap_hours)
    assert len(vouchers) == 1
    voucher = vouchers[0]
    assert voucher["count"] == 10
    span_hours = (voucher["last_ts"] - voucher["first_ts"]) / 3600
    assert span_hours == 9 * gap_hours  # 216h from a 24h gap - far beyond "consecutive"


def test_voucher_windows_merge_is_inclusive_at_the_exact_boundary():
    """The merge condition in demix.voucher_windows is
    ``span["first_ts"] <= merged[-1]["end_ts"]`` - inclusive `<=`, so a second
    voucher whose window starts EXACTLY where the first one's ends (rather
    than genuinely overlapping it) still merges into one window. With
    window_days=1 (forward = 86400s), voucher 1 (first_ts=0) produces
    end_ts=86400; voucher 2 starts at first_ts=86400 exactly - touching, not
    overlapping - and must still merge, per this boundary rule."""
    windows = voucher_windows(
        [
            {"first_ts": 0, "last_ts": 0, "count": 1},
            {"first_ts": 86400, "last_ts": 86400, "count": 1},  # starts exactly at the first end
        ],
        window_days=1,
    )
    assert len(windows) == 1
    assert windows[0]["first_ts"] == 0
    assert windows[0]["end_ts"] == 86400 + 86400
    assert windows[0]["voucher_count"] == 2
