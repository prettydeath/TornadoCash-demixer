"""Operator clustering: which evidence may merge two depositor wallets.

``self_relayed`` must never draw an edge: it is a property of the withdrawal,
not of any wallet, and two unrelated depositors with overlapping windows share
count-matched recipients by construction.
"""

from tornado_demix.graph import WALLET_SPECIFIC_SIGNALS, cluster_wallets

ALICE = "0x" + "a" * 40
BOB = "0x" + "b" * 40
CAROL = "0x" + "c" * 40
EXIT = "0x" + "e" * 40
OTHER_EXIT = "0x" + "f" * 40


def _wallet(signals, addr=EXIT, hits=3, pool="1 ETH"):
    """A run_demix-shaped result with one recipient carrying ``signals``."""
    return {
        "denoms": {
            pool: {
                "signals": {addr: sorted(signals)},
                "counts": {addr: hits},
                "target_counts": [hits],
            }
        }
    }


def _corr(results, cross_profile=None, fingerprints=None):
    return {
        "results": results,
        "cross_profile": cross_profile or {},
        "fingerprints": fingerprints or {},
    }


def _members(clusters):
    return sorted(sorted(c["wallets"]) for c in clusters)


# What must NOT merge
def test_a_shared_self_relayed_exit_does_not_merge_two_wallets():
    """The regression: this is the window-overlap artefact, not evidence."""
    corr = _corr(
        {
            ALICE: _wallet({"count_match", "self_relayed"}),
            BOB: _wallet({"count_match", "self_relayed"}),
        }
    )
    assert cluster_wallets(corr) == []


def test_a_bare_count_match_does_not_merge_two_wallets():
    corr = _corr({ALICE: _wallet({"count_match"}), BOB: _wallet({"count_match"})})
    assert cluster_wallets(corr) == []


def test_self_relayed_is_not_a_wallet_specific_signal():
    assert "self_relayed" not in WALLET_SPECIFIC_SIGNALS
    assert WALLET_SPECIFIC_SIGNALS == {"gas_price", "linked"}


def test_a_wallet_specific_signal_on_only_one_side_does_not_merge():
    corr = _corr({ALICE: _wallet({"linked"}), BOB: _wallet({"count_match"})})
    assert cluster_wallets(corr) == []


def test_different_exit_addresses_do_not_merge():
    corr = _corr(
        {
            ALICE: _wallet({"linked"}, addr=EXIT),
            BOB: _wallet({"linked"}, addr=OTHER_EXIT),
        }
    )
    assert cluster_wallets(corr) == []


def test_a_signal_on_a_recipient_that_is_not_count_matched_does_not_merge():
    a = _wallet({"linked"}, hits=3)
    b = _wallet({"linked"}, hits=3)
    b["denoms"]["1 ETH"]["target_counts"] = [9]  # 3 hits is not the voucher
    assert cluster_wallets(_corr({ALICE: a, BOB: b})) == []


# What must merge
def test_a_shared_linked_exit_merges():
    corr = _corr({ALICE: _wallet({"linked"}), BOB: _wallet({"linked"})})
    assert _members(cluster_wallets(corr)) == [[ALICE, BOB]]


def test_a_shared_gas_price_exit_merges():
    corr = _corr({ALICE: _wallet({"gas_price"}), BOB: _wallet({"gas_price"})})
    clusters = cluster_wallets(corr)
    assert _members(clusters) == [[ALICE, BOB]]
    assert clusters[0]["edges"][0][3] == EXIT


def test_a_cross_wallet_profile_consolidator_with_distinct_fingerprints_merges():
    # Distinct note-patterns matched by one address are not a shared-window
    # artefact, so the consolidator still merges the wallets.
    corr = _corr(
        {ALICE: _wallet(set()), BOB: _wallet(set())},
        cross_profile={EXIT: [ALICE, BOB]},
        fingerprints={ALICE: {"1 ETH": 9, "10 ETH": 1}, BOB: {"1 ETH": 3, "100 ETH": 2}},
    )
    clusters = cluster_wallets(corr)
    assert _members(clusters) == [[ALICE, BOB]]
    assert "distinct fingerprints" in clusters[0]["edges"][0][2]


def test_an_identical_fingerprint_consolidator_does_not_merge():
    """The user's report shape: several wallets, one identical fingerprint, and a
    full-fingerprint consolidator with no independent signal. That is the
    window-overlap artefact - synchronous equal-denomination deposits let one
    unrelated recipient satisfy every wallet's identical fingerprint - so it must
    NOT form an operator cluster on its own.
    """
    fp = {"0.1 ETH": 9, "1 ETH": 9}
    corr = _corr(
        {ALICE: _wallet(set()), BOB: _wallet(set())},
        cross_profile={EXIT: [ALICE, BOB]},
        fingerprints={ALICE: dict(fp), BOB: dict(fp)},
    )
    assert cluster_wallets(corr) == []


def test_an_identical_fingerprint_consolidator_with_an_independent_signal_merges():
    # Same identical-fingerprint shape, but the consolidator also carries a
    # linked signal (independent of the window). target_counts is set so the
    # recipient is NOT a section-1 count-matched edge, isolating the section-2
    # consolidator path.
    a, b = _wallet({"linked"}), _wallet({"linked"})
    a["denoms"]["1 ETH"]["target_counts"] = [9]
    b["denoms"]["1 ETH"]["target_counts"] = [9]
    fp = {"1 ETH": 9}
    corr = _corr(
        {ALICE: a, BOB: b},
        cross_profile={EXIT: [ALICE, BOB]},
        fingerprints={ALICE: dict(fp), BOB: dict(fp)},
    )
    clusters = cluster_wallets(corr)
    assert _members(clusters) == [[ALICE, BOB]]
    assert "linked" in clusters[0]["edges"][0][2]


def test_precomputed_grades_override_the_local_recompute():
    # When multi.correlate() supplies consolidator_grades, cluster_wallets must
    # honour the artefact flag directly rather than recomputing.
    corr = _corr(
        {ALICE: _wallet(set()), BOB: _wallet(set())},
        cross_profile={EXIT: [ALICE, BOB]},
        fingerprints={ALICE: {"1 ETH": 9, "10 ETH": 1}, BOB: {"1 ETH": 3, "100 ETH": 2}},
    )
    corr["consolidator_grades"] = {EXIT: {"band": "weak", "artefact": True, "edge_reason": None}}
    assert cluster_wallets(corr) == []


def test_clusters_are_transitive():
    corr = _corr(
        {
            ALICE: _wallet({"linked"}, addr=EXIT),
            BOB: _wallet({"linked"}, addr=EXIT),
            CAROL: _wallet({"gas_price"}, addr=OTHER_EXIT),
        }
    )
    corr["results"][BOB]["denoms"]["10 ETH"] = {
        "signals": {OTHER_EXIT: ["gas_price"]},
        "counts": {OTHER_EXIT: 3},
        "target_counts": [3],
    }
    assert _members(cluster_wallets(corr)) == [[ALICE, BOB, CAROL]]


def test_the_edge_records_why_the_link_was_drawn():
    corr = _corr({ALICE: _wallet({"linked"}), BOB: _wallet({"linked"})})
    edge = cluster_wallets(corr)[0]["edges"][0]
    assert edge[0] == ALICE and edge[1] == BOB
    assert "linked" in edge[2]


def test_a_lone_wallet_is_not_a_cluster():
    assert cluster_wallets(_corr({ALICE: _wallet({"linked"})})) == []


def test_no_wallets_is_not_an_error():
    assert cluster_wallets(_corr({})) == []


def test_larger_clusters_sort_first():
    corr = _corr(
        {
            ALICE: _wallet({"linked"}, addr=EXIT),
            BOB: _wallet({"linked"}, addr=EXIT),
            CAROL: _wallet({"linked"}, addr=EXIT),
            "0x" + "d" * 40: _wallet({"gas_price"}, addr=OTHER_EXIT),
            "0x" + "1" * 40: _wallet({"gas_price"}, addr=OTHER_EXIT),
        }
    )
    clusters = cluster_wallets(corr)
    assert [len(c["wallets"]) for c in clusters] == [3, 2]
