"""Two things the confidence model got wrong, and the shape of the fixes.

1. cross_method counted count_match and self_relayed as two independent
   methods. self_relayed is only ever credited on an address that already
   count-matched, and both read the same withdrawals, so every count-matched
   candidate with one zero-fee withdrawal scored "confirmed by two independent
   methods" - which the report calls its most reliable signal.

2. profile_match carried a weight (0.35) and a label and was never assigned to
   anything, so the score could not express the evidence METHODOLOGY ranks
   highest.
"""

from tornado_demix.heuristics import (
    METHOD_FAMILY,
    SIGNAL_WEIGHTS,
    credit_profile_match,
    cross_method,
)

ADDR = "0x" + "e" * 40


def _result(signals, hits=3, unique=50, sharing=1, disc=None):
    """One pool's analysis with a single recipient carrying ``signals``."""
    counts = {ADDR: hits}
    for i in range(sharing - 1):
        counts["0x%040x" % i] = hits  # others sharing the count
    for i in range(unique - sharing):
        counts["0x%040x" % (1000 + i)] = hits + 1
    discrimination = disc if disc is not None else 1.0
    return {
        "denoms": {
            "1 ETH": {
                "denom": 1.0,
                "asset": "ETH",
                "counts": counts,
                "unique_recipients": len(counts),
                "target_counts": [hits],
                "signals": {ADDR: sorted(signals)},
                "detail": {ADDR: [{"self_relayed": "self_relayed" in signals}]},
                "discrimination": {ADDR: discrimination},
                "confidence": {ADDR: 0.5},
            }
        }
    }


# Method independence
def test_count_match_and_self_relayed_are_one_family():
    assert METHOD_FAMILY["count_match"] == METHOD_FAMILY["self_relayed"]


def test_count_and_self_relayed_alone_is_not_a_cross_method_match():
    """count_match + self_relayed is one family, not two independent methods."""
    data = _result({"count_match", "self_relayed"})
    assert cross_method(data) == []


def test_count_match_plus_linked_is_a_cross_method_match():
    data = _result({"count_match", "linked"})
    rows = cross_method(data)
    assert len(rows) == 1
    assert rows[0]["n_methods"] == 2
    assert set(rows[0]["families"]) == {"amount+timing", "linked address"}


def test_count_match_plus_gas_price_is_a_cross_method_match():
    rows = cross_method(_result({"count_match", "gas_price"}))
    assert rows and rows[0]["n_methods"] == 2


def test_all_four_signals_are_three_independent_families():
    rows = cross_method(_result({"count_match", "self_relayed", "gas_price", "linked"}))
    assert rows[0]["n_methods"] == 3
    assert len(rows[0]["methods"]) == 4  # every method still listed


def test_a_single_method_is_not_a_cross_method_match():
    assert cross_method(_result({"linked"})) == []


# profile_match
def test_profile_match_has_a_weight_that_is_now_reachable():
    assert SIGNAL_WEIGHTS["profile_match"] > 0
    data = _result({"count_match"})
    data["denoms"]["10 ETH"] = dict(data["denoms"]["1 ETH"])
    data["denoms"]["1 ETH"]["discrimination"][ADDR] = 0.9
    before = _score({"count_match"}, 0.9)
    credit_profile_match(data, ["1 ETH", "10 ETH"], ADDR)
    assert "profile_match" in data["denoms"]["1 ETH"]["signals"][ADDR]
    assert data["denoms"]["1 ETH"]["confidence"][ADDR] > before


def test_a_single_pool_fingerprint_is_not_a_profile_match():
    """One pool is the count match again; crediting it twice double-counts."""
    data = _result({"count_match"})
    credit_profile_match(data, ["1 ETH"], ADDR)
    assert "profile_match" not in data["denoms"]["1 ETH"]["signals"][ADDR]


def test_crediting_twice_does_not_compound_the_score():
    data = _result({"count_match"})
    data["denoms"]["10 ETH"] = dict(data["denoms"]["1 ETH"])
    credit_profile_match(data, ["1 ETH", "10 ETH"], ADDR)
    once = data["denoms"]["1 ETH"]["confidence"][ADDR]
    credit_profile_match(data, ["1 ETH", "10 ETH"], ADDR)
    assert data["denoms"]["1 ETH"]["confidence"][ADDR] == once


def test_an_unknown_address_is_ignored_rather_than_crashing():
    data = _result({"count_match"})
    data["denoms"]["10 ETH"] = dict(data["denoms"]["1 ETH"])
    credit_profile_match(data, ["1 ETH", "10 ETH"], "0x" + "9" * 40)


def test_a_weak_count_still_scales_the_score_after_a_profile_match():
    """The discrimination scaling survives the rescore."""
    weak = _result({"count_match"}, disc=0.0)
    weak["denoms"]["10 ETH"] = dict(weak["denoms"]["1 ETH"])
    credit_profile_match(weak, ["1 ETH", "10 ETH"], ADDR)
    # count_match contributes nothing at zero discrimination; only the
    # profile-match weight remains.
    assert weak["denoms"]["1 ETH"]["confidence"][ADDR] == SIGNAL_WEIGHTS["profile_match"]


# The weights and the guards themselves
# Mutation testing found these three unasserted: the discrimination scaling
# could be removed, any weight could be zeroed, and the gas-price rarity guard
# could be deleted, with the whole suite still green.
from tornado_demix.heuristics import _score, apply_heuristics  # noqa: E402


def test_the_count_match_weight_is_scaled_by_discrimination():
    """A count that narrows nothing must contribute nothing."""
    assert _score({"count_match"}, 1.0) == SIGNAL_WEIGHTS["count_match"]
    assert _score({"count_match"}, 0.0) == 0.0
    half = _score({"count_match"}, 0.5)
    assert 0.0 < half < SIGNAL_WEIGHTS["count_match"]
    assert half == round(SIGNAL_WEIGHTS["count_match"] * 0.5, 4)


def test_a_weak_count_still_adds_a_little_on_top_of_a_hard_signal():
    """The floor gates the binary decisions, not the score itself."""
    linked_only = _score({"linked"}, 0.0)
    with_weak_count = _score({"linked", "count_match"}, 0.2)
    assert with_weak_count > linked_only


def test_every_declared_weight_reaches_the_score():
    """A weight silently set to zero is a signal that stopped mattering."""
    for signal, weight in SIGNAL_WEIGHTS.items():
        assert weight > 0, signal
        scored = _score({signal}, 1.0)
        assert scored == round(weight, 4), signal


def test_the_signals_combine_as_a_noisy_or():
    combined = _score({"linked", "gas_price"}, 1.0)
    expected = 1 - (1 - SIGNAL_WEIGHTS["linked"]) * (1 - SIGNAL_WEIGHTS["gas_price"])
    assert combined == round(expected, 4)
    assert combined > max(SIGNAL_WEIGHTS["linked"], SIGNAL_WEIGHTS["gas_price"])


def _gas_data(shared_by):
    """A pool where the matched gas price is shared by ``shared_by`` withdrawals."""
    detail = {ADDR: [{"gas_price": 42, "hash": "0xw", "block": 1}]}
    for i in range(shared_by - 1):
        detail["0x%040x" % i] = [{"gas_price": 42, "hash": "0xo%d" % i, "block": 1}]
    return {
        "deposits": [{"gas_price": 42}],
        "denoms": {
            "1 ETH": {
                "counts": {a: 1 for a in detail},
                "unique_recipients": len(detail),
                "target_counts": [1],
                "detail": detail,
            }
        },
    }


def test_a_gas_price_shared_by_few_withdrawals_is_credited():
    data = _gas_data(shared_by=3)
    apply_heuristics(data, set())
    assert "gas_price" in data["denoms"]["1 ETH"]["signals"][ADDR]


def test_a_gas_price_shared_by_many_withdrawals_is_not_evidence():
    """A common auto-fee value matches by chance, not by authorship."""
    data = _gas_data(shared_by=12)
    apply_heuristics(data, set())
    assert "gas_price" not in data["denoms"]["1 ETH"]["signals"][ADDR]


def test_a_gas_price_the_wallet_never_used_is_not_credited():
    data = _gas_data(shared_by=1)
    data["deposits"] = [{"gas_price": 99}]
    apply_heuristics(data, set())
    assert "gas_price" not in data["denoms"]["1 ETH"]["signals"][ADDR]
