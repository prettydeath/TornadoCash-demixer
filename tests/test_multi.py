"""Cross-wallet correlation.

``multi`` is one of the three commands and its correlation core was covered at
roughly a tenth. These tests drive :func:`correlate` through a stub client so
the whole path - demix per wallet, intersections, fingerprints, profile
matching, operator clustering - runs without a network.
"""

from tornado_demix.multi import (
    _fingerprints,
    _profile_match,
    _shared,
    correlate,
    deposit_synchronicity,
    grade_consolidators,
)
from tornado_demix.networks import Network
from tornado_demix.pools import Pool

POOL_1 = "0x" + "1" * 40
POOL_10 = "0x" + "2" * 40
ALICE = "0x" + "a" * 40
BOB = "0x" + "b" * 40
EXIT_A = "0x" + "e" * 40
EXIT_B = "0x" + "f" * 40


def network():
    return Network(
        "testnet",
        1,
        "ETH",
        [
            Pool(POOL_1, 1.0, "ETH", 18, None),
            Pool(POOL_10, 10.0, "ETH", 18, None),
        ],
    )


class StubClient:
    """Deposits per wallet, withdrawals per pool - both handed in verbatim."""

    def __init__(self, deposits, withdrawals):
        self.deposits = deposits  # wallet -> [(pool_addr, ts)]
        self.withdrawals = withdrawals  # pool_addr -> [(recipient, ts)]

    def internal_txs(self, wallet):

        return []

    def outgoing_txs(self, wallet):
        rows = []
        for i, (pool, ts) in enumerate(self.deposits.get(wallet.lower(), [])):
            denom = 10**18 if pool == POOL_1 else 10 * 10**18
            rows.append(
                {
                    "from": wallet.lower(),
                    "to": pool,
                    "value": str(denom),
                    "timeStamp": str(ts),
                    "blockNumber": str(ts // 12),
                    "hash": "0xd%s%d" % (wallet[2:6], i),
                    "gasPrice": "1",
                    "isError": "0",
                }
            )
        return rows

    def block_by_time(self, ts, closest="before"):
        return ts // 12

    def get_logs(self, address, topic0, start_block, end_block):
        logs = []
        for i, (to, ts) in enumerate(self.withdrawals.get(address.lower(), [])):
            if not start_block <= ts // 12 <= end_block:
                continue
            logs.append(
                {
                    "data": "0x" + "0" * 24 + to[2:] + "00" * 32 + format(10**15, "064x"),
                    "topics": ["0xtopic", "0x" + "0" * 24 + "cc" * 20],
                    "gasPrice": "0x2",
                    "transactionHash": "0xw%d" % i,
                    "blockNumber": hex(ts // 12),
                    "timeStamp": hex(ts),
                }
            )
        return logs


T0 = 1_700_000_000


def _run(deposits, withdrawals, wallets):
    client = StubClient(deposits, withdrawals)
    return correlate(client, wallets, window_days=30, network=network())


def test_a_recipient_matched_by_two_wallets_is_a_strong_link():
    """Three notes each, one address receiving three - for both wallets."""
    deposits = {
        ALICE: [(POOL_1, T0), (POOL_1, T0 + 60), (POOL_1, T0 + 120)],
        BOB: [(POOL_1, T0), (POOL_1, T0 + 60), (POOL_1, T0 + 120)],
    }
    withdrawals = {
        POOL_1: [(EXIT_A, T0 + 86400 * i) for i in range(1, 4)]
        + [(EXIT_B, T0 + 86400 * 5)]
        + [("0x%040x" % (0xB0 + i), T0 + 86400 * 6 + i) for i in range(4)]  # background
    }
    corr = _run(deposits, withdrawals, [ALICE, BOB])
    shared = {addr for _pool, addr, wallets in corr["strong"] if len(wallets) == 2}
    assert EXIT_A in shared
    assert EXIT_B not in shared


def test_soft_overlaps_exclude_anything_already_a_strong_link():
    deposits = {
        ALICE: [(POOL_1, T0), (POOL_1, T0 + 60), (POOL_1, T0 + 120)],
        BOB: [(POOL_1, T0), (POOL_1, T0 + 60), (POOL_1, T0 + 120)],
    }
    withdrawals = {POOL_1: [(EXIT_A, T0 + 86400 * i) for i in range(1, 4)]}
    corr = _run(deposits, withdrawals, [ALICE, BOB])
    strong_keys = {(p, a) for p, a, _ in corr["strong"]}
    soft_keys = {(p, a) for p, a, _ in corr["soft"]}
    assert not (strong_keys & soft_keys)


def test_the_fingerprint_totals_notes_per_pool():
    deposits = {ALICE: [(POOL_1, T0), (POOL_1, T0 + 60), (POOL_10, T0 + 120)]}
    corr = _run(deposits, {}, [ALICE])
    assert corr["fingerprints"][ALICE] == {"1 ETH": 2, "10 ETH": 1}


def test_a_wallet_with_no_deposits_gets_an_empty_fingerprint():
    corr = _run({}, {}, [ALICE])
    assert corr["fingerprints"][ALICE] == {}
    assert corr["strong"] == []
    assert corr["operator_clusters"] == []


def test_correlate_lowercases_the_wallets_it_reports():
    corr = _run({ALICE: [(POOL_1, T0)]}, {}, [ALICE.upper()])
    assert list(corr["results"]) == [ALICE]


# The pure helpers
def test_shared_reports_only_addresses_seen_by_two_or_more_wallets():
    membership = {"1 ETH": {EXIT_A: {ALICE, BOB}, EXIT_B: {ALICE}}}
    assert _shared(membership) == [("1 ETH", EXIT_A, [ALICE, BOB])]


def test_shared_sorts_the_widest_first():
    membership = {"1 ETH": {EXIT_A: {ALICE, BOB}, EXIT_B: {ALICE, BOB, "0xc"}}}
    assert [e[1] for e in _shared(membership)] == [EXIT_B, EXIT_A]


def test_fingerprints_sum_across_several_vouchers_in_one_pool():
    results = {
        ALICE: {
            "vouchers": [
                {"pool_key": "1 ETH", "count": 2},
                {"pool_key": "1 ETH", "count": 3},
                {"pool_key": "10 ETH", "count": 1},
            ]
        }
    }
    assert _fingerprints(results) == {ALICE: {"1 ETH": 5, "10 ETH": 1}}


def test_an_exact_multi_pool_profile_match_is_flagged_exact():
    fingerprints = {ALICE: {"1 ETH": 2, "10 ETH": 1}}
    addr_detail = {("1 ETH", EXIT_A): {ALICE: 2}, ("10 ETH", EXIT_A): {ALICE: 1}}
    matches, cross = _profile_match(fingerprints, addr_detail)
    assert matches[ALICE] == [(EXIT_A, {"1 ETH": (2, 2), "10 ETH": (1, 1)}, True)]
    assert cross == {}


def test_receiving_more_than_the_fingerprint_is_a_match_but_not_exact():
    fingerprints = {ALICE: {"1 ETH": 2}}
    addr_detail = {("1 ETH", EXIT_A): {ALICE: 5}}
    matches, _cross = _profile_match(fingerprints, addr_detail)
    assert matches[ALICE][0][2] is False


def test_missing_one_pool_of_the_fingerprint_is_not_a_match():
    fingerprints = {ALICE: {"1 ETH": 2, "10 ETH": 1}}
    addr_detail = {("1 ETH", EXIT_A): {ALICE: 2}}  # nothing from 10 ETH
    matches, _cross = _profile_match(fingerprints, addr_detail)
    assert matches.get(ALICE, []) == []


def test_one_address_matching_two_wallets_is_a_cross_consolidator():
    fingerprints = {ALICE: {"1 ETH": 1, "10 ETH": 1}, BOB: {"1 ETH": 1, "10 ETH": 1}}
    addr_detail = {
        ("1 ETH", EXIT_A): {ALICE: 1, BOB: 1},
        ("10 ETH", EXIT_A): {ALICE: 1, BOB: 1},
    }
    _matches, cross = _profile_match(fingerprints, addr_detail)
    assert cross == {EXIT_A: [ALICE, BOB]}


def test_a_single_pool_fingerprint_never_becomes_a_cross_consolidator():
    """One denomination is not a fingerprint; it is the window-overlap artefact."""
    fingerprints = {ALICE: {"1 ETH": 1}, BOB: {"1 ETH": 1}}
    addr_detail = {("1 ETH", EXIT_A): {ALICE: 1, BOB: 1}}
    _matches, cross = _profile_match(fingerprints, addr_detail)
    assert cross == {}


# Consolidator grading: strong / moderate / weak(artefact)
def _no_signals(wallets):
    """results-shaped dict where no consolidator carries any signal."""
    return {w: {"denoms": {}} for w in wallets}


def test_grade_identical_fingerprint_no_signal_is_a_weak_artefact():
    fp = {"0.1 ETH": 9, "1 ETH": 9}
    grades = grade_consolidators(
        {EXIT_A: [ALICE, BOB]}, _no_signals([ALICE, BOB]), {ALICE: dict(fp), BOB: dict(fp)}
    )
    g = grades[EXIT_A]
    assert g["band"] == "weak"
    assert g["artefact"] is True
    assert g["distinct_fingerprints"] == 1
    assert g["edge_reason"] is None


def test_grade_distinct_fingerprints_is_moderate():
    grades = grade_consolidators(
        {EXIT_A: [ALICE, BOB]},
        _no_signals([ALICE, BOB]),
        {ALICE: {"1 ETH": 9, "10 ETH": 1}, BOB: {"1 ETH": 3, "100 ETH": 2}},
    )
    g = grades[EXIT_A]
    assert g["band"] == "moderate"
    assert g["artefact"] is False
    assert g["distinct_fingerprints"] == 2
    assert "distinct fingerprints" in g["edge_reason"]


def test_grade_independent_signal_is_strong_even_on_identical_fingerprints():
    fp = {"1 ETH": 9}
    results = {
        ALICE: {"denoms": {"1 ETH": {"signals": {EXIT_A: ["linked"]}}}},
        BOB: {"denoms": {}},
    }
    grades = grade_consolidators({EXIT_A: [ALICE, BOB]}, results, {ALICE: dict(fp), BOB: dict(fp)})
    g = grades[EXIT_A]
    assert g["band"] == "strong"
    assert g["artefact"] is False
    assert g["independent"] == ["linked"]
    assert "linked" in g["edge_reason"]


def test_grade_self_relayed_alone_stays_a_weak_artefact():
    # self_relayed is a property of the withdrawal, shared across the window - it
    # does not lift an identical-fingerprint overlap out of the artefact band.
    fp = {"1 ETH": 9}
    results = {
        ALICE: {"denoms": {"1 ETH": {"signals": {EXIT_A: ["self_relayed"]}}}},
        BOB: {"denoms": {"1 ETH": {"signals": {EXIT_A: ["self_relayed"]}}}},
    }
    grades = grade_consolidators({EXIT_A: [ALICE, BOB]}, results, {ALICE: dict(fp), BOB: dict(fp)})
    g = grades[EXIT_A]
    assert g["band"] == "weak"
    assert g["artefact"] is True
    assert g["self_relayed"] is True


def test_correlate_attaches_consolidator_grades():
    corr = _run({ALICE: [(POOL_1, T0)]}, {}, [ALICE])
    assert "consolidator_grades" in corr
    assert isinstance(corr["consolidator_grades"], dict)
    assert "sync_groups" in corr
    assert isinstance(corr["sync_groups"], list)


# Deposit synchronicity + synchronized-batch downgrade
CAROL = "0x" + "c" * 40
DAVE = "0x" + "d" * 40


def _deposits(ts_list):
    return {"deposits": [{"ts": t} for t in ts_list], "denoms": {}}


def test_deposit_synchronicity_chains_wallets_within_the_gap():
    results = {
        ALICE: _deposits([1_000, 1_100]),
        BOB: _deposits([1_100 + 3600]),  # 1h after ALICE's last
        CAROL: _deposits([1_000 + 40 * 3600]),  # 40h later, own batch
    }
    groups = deposit_synchronicity(results, gap_hours=6)
    assert len(groups) == 1  # CAROL is alone -> no group
    assert groups[0]["wallets"] == sorted([ALICE, BOB])
    assert groups[0]["span_seconds"] == (1_100 + 3600) - 1_000


def test_deposit_synchronicity_separates_two_batches():
    results = {
        ALICE: _deposits([1_000]),
        CAROL: _deposits([1_200]),  # batch 1
        BOB: _deposits([1_000 + 30 * 3600]),  # batch 2 ...
        DAVE: _deposits([1_000 + 30 * 3600 + 600]),
    }
    groups = deposit_synchronicity(results, gap_hours=6)
    assert [g["wallets"] for g in groups] == [sorted([ALICE, CAROL]), sorted([BOB, DAVE])]


_TWO_FPS = {ALICE: {"1 ETH": 9, "10 ETH": 1}, BOB: {"1 ETH": 3, "100 ETH": 2}}


def test_grade_synchronized_distinct_fingerprints_is_window_prone():
    # Both matched wallets deposited within 20 minutes -> one tight window.
    spans = {ALICE: (1_000, 1_600), BOB: (1_200, 1_800)}
    grades = grade_consolidators({EXIT_A: [ALICE, BOB]}, _no_signals([ALICE, BOB]), _TWO_FPS, spans)
    g = grades[EXIT_A]
    assert g["band"] == "weak"
    assert g["artefact"] is True
    assert g["artefact_kind"] == "synchronized"
    assert g["synchronized"] is True


def test_grade_distinct_fingerprints_far_apart_stays_moderate():
    # Matched wallets deposited > SYNC_MAX_SPAN_HOURS apart: their windows do not
    # overlap enough, so the distinct-fingerprint match survives as moderate. This
    # is the transitive/interval-fill guard - span, not batch membership, decides.
    spans = {ALICE: (1_000, 1_000), BOB: (1_000 + 20 * 3600, 1_000 + 20 * 3600)}
    grades = grade_consolidators({EXIT_A: [ALICE, BOB]}, _no_signals([ALICE, BOB]), _TWO_FPS, spans)
    g = grades[EXIT_A]
    assert g["band"] == "moderate"
    assert g["synchronized"] is False


def test_grade_missing_deposit_span_is_not_downgraded():
    # If a matched wallet has no deposit times, do not downgrade on incomplete
    # evidence - stay moderate.
    grades = grade_consolidators(
        {EXIT_A: [ALICE, BOB]}, _no_signals([ALICE, BOB]), _TWO_FPS, {ALICE: (1_000, 1_100)}
    )  # BOB span missing
    assert grades[EXIT_A]["band"] == "moderate"


def test_grade_independent_signal_beats_synchronized_downgrade():
    # An independent signal is strong even when the wallets are one tight batch.
    spans = {ALICE: (1_000, 1_100), BOB: (1_100, 1_200)}
    results = {
        ALICE: {"denoms": {"1 ETH": {"signals": {EXIT_A: ["linked"]}}}},
        BOB: {"denoms": {}},
    }
    grades = grade_consolidators({EXIT_A: [ALICE, BOB]}, results, _TWO_FPS, spans)
    assert grades[EXIT_A]["band"] == "strong"
