"""A signal every candidate satisfies must not raise anyone's confidence."""

from collections import Counter, defaultdict

import pytest

from tornado_demix.heuristics import (
    apply_heuristics,
    band_rationale,
    candidate_reason,
    conclusion,
    confidence_band,
    count_discrimination,
    cross_method,
    ranked_candidates,
)


def _pool_result(n_recipients, n_self_relayed, hits_each=1, target=None):
    detail = defaultdict(list)
    for i in range(n_recipients):
        addr = "0x%040x" % (i + 1)
        detail[addr] = [
            {
                "value": 0.099,
                "ts": 1000 + i,
                "hash": "0x%x" % (i * 10 + j),
                "relayer": "0x0" if i < n_self_relayed else "0xrelayer",
                "fee": 0.001,
                "asset": "ETH",
                "self_relayed": i < n_self_relayed,
                "gas_price": 5,
            }
            for j in range(hits_each)
        ]
    return {
        "pool_key": "0.1 ETH",
        "denom": 0.1,
        "asset": "ETH",
        "decimals": 18,
        "pool": "0xpool",
        "mode": "events",
        "counts": Counter({a: hits_each for a in detail}),
        "detail": detail,
        "withdrawals": [],
        "target_counts": target or [hits_each],
        "candidates_by_count": {hits_each: list(detail)},
        "total_qualifying_withdrawals": n_recipients * hits_each,
        "unique_recipients": n_recipients,
    }


def _data(res, voucher_count=1):
    return {
        "wallet": "0xw",
        "params": {"mode": "events"},
        "deposits": [{"gas_price": 999, "pool_key": "0.1 ETH"}],
        "vouchers": [{"pool_key": "0.1 ETH", "denom": 0.1, "asset": "ETH", "count": voucher_count}],
        "denoms": {"0.1 ETH": res},
    }


def test_a_count_every_recipient_shares_discriminates_nothing():
    res = _pool_result(n_recipients=40, n_self_relayed=0)
    assert count_discrimination(res, 1) == 0.0


def test_a_count_one_recipient_holds_discriminates_fully():
    res = _pool_result(n_recipients=40, n_self_relayed=0, hits_each=3, target=[3])
    res["counts"] = Counter({a: 1 for a in res["detail"]})
    res["counts"]["0x%040x" % 1] = 3
    assert count_discrimination(res, 3) > 0.9


def test_single_note_voucher_yields_no_ranked_candidates():
    """Reproduces the audit: this returned 20 candidates at 47%."""
    data = _data(_pool_result(n_recipients=40, n_self_relayed=20))
    apply_heuristics(data, counterparties=set())
    assert ranked_candidates(data) == []


def test_single_note_voucher_yields_no_cross_method_claims():
    data = _data(_pool_result(n_recipients=40, n_self_relayed=20))
    apply_heuristics(data, counterparties=set())
    assert cross_method(data) == []


def test_the_conclusion_says_a_single_note_cannot_be_narrowed():
    data = _data(_pool_result(n_recipients=40, n_self_relayed=20))
    apply_heuristics(data, counterparties=set())
    text = conclusion(data).lower()
    assert "single-note" in text or "single note" in text
    assert "funding source" in text


def test_a_discriminating_count_still_scores():
    """The fix must not silence a count match that genuinely narrows."""
    res = _pool_result(n_recipients=40, n_self_relayed=0, hits_each=1)
    winner = "0x%040x" % 1
    res["detail"][winner] = res["detail"][winner] * 3
    res["counts"][winner] = 3
    res["target_counts"] = [3]
    res["candidates_by_count"] = {3: [winner]}
    data = _data(res, voucher_count=3)
    apply_heuristics(data, counterparties=set())
    ranked = ranked_candidates(data)
    assert [r["address"] for r in ranked] == [winner]
    assert ranked[0]["confidence"] > 0.2


def test_a_near_single_note_count_is_not_credited():
    """39 of 40 share count 1: the count eliminates almost nobody."""
    res = _pool_result(n_recipients=40, n_self_relayed=20)
    winner = "0x%040x" % 1
    res["detail"][winner] = res["detail"][winner] * 2
    res["counts"][winner] = 2
    # voucher is still single-note; target stays [1]
    data = _data(res)
    apply_heuristics(data, counterparties=set())
    assert ranked_candidates(data) == []
    assert cross_method(data) == []


def test_a_count_narrowing_most_of_the_field_still_scores():
    """One recipient at count 3 among 40 (disc ~1.0) clears the floor."""
    res = _pool_result(n_recipients=40, n_self_relayed=0, hits_each=1)
    winner = "0x%040x" % 1
    res["detail"][winner] = res["detail"][winner] * 3
    res["counts"][winner] = 3
    res["target_counts"] = [3]
    res["candidates_by_count"] = {3: [winner]}
    data = _data(res, voucher_count=3)
    apply_heuristics(data, counterparties=set())
    assert [r["address"] for r in ranked_candidates(data)] == [winner]


def test_a_linked_address_still_scores_on_a_single_note():
    """Linked is real evidence regardless of how weak the count is."""
    linked = "0x%040x" % 7
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={linked})
    assert [r["address"] for r in ranked_candidates(data)] == [linked]


def _shared_count_pool():
    """20 recipients with count 2, one with count 1: a count of 2 is barely rare."""
    res = _pool_result(n_recipients=20, n_self_relayed=0, hits_each=2, target=[2])
    lone = "0x%040x" % 99
    res["detail"][lone] = [dict(res["detail"]["0x%040x" % 1][0])]
    res["counts"][lone] = 1
    res["unique_recipients"] = 21
    return res


def test_a_count_below_the_floor_is_not_a_count_match_signal():
    linked = "0x%040x" % 3
    data = _data(_shared_count_pool(), voucher_count=2)
    apply_heuristics(data, counterparties={linked})
    res = data["denoms"]["0.1 ETH"]
    assert res["discrimination"][linked] < 0.5
    assert "count_match" not in res["signals"][linked]


def test_a_barely_rare_count_plus_a_link_is_not_strong():
    """Two families only count when the count match actually discriminates."""
    linked = "0x%040x" % 3
    data = _data(_shared_count_pool(), voucher_count=2)
    apply_heuristics(data, counterparties={linked})
    row = next(r for r in ranked_candidates(data) if r["address"] == linked)
    assert row["band"] == "moderate"
    assert "matching a 2-note voucher" not in candidate_reason(row)


# --- confidence bands (structure of the evidence, not the score) -----------
@pytest.mark.parametrize(
    "signals, band",
    [
        pytest.param({"count_match"}, "weak", id="bare count match"),
        pytest.param({"linked"}, "moderate", id="one structural tie"),
        pytest.param({"count_match", "linked"}, "strong", id="amount+timing and linked"),
        pytest.param({"gas_price", "linked"}, "strong", id="gas price and linked"),
    ],
)
def test_band_counts_independent_families(signals, band):
    assert confidence_band(signals) == band


def test_band_count_plus_self_relayed_is_moderate():
    """The pin: count_match and self_relayed are the SAME family
    ('amount+timing'), so there is ONE family, not two - so this is NOT
    strong. self_relayed is a tie beyond the bare count, so it is moderate."""
    from tornado_demix.heuristics import METHOD_FAMILY

    assert METHOD_FAMILY["count_match"] == METHOD_FAMILY["self_relayed"]
    families = {METHOD_FAMILY[s] for s in {"count_match", "self_relayed"}}
    assert len(families) == 1
    assert confidence_band({"count_match", "self_relayed"}) == "moderate"


def test_band_rationale_is_defined_for_every_band():
    cases = [
        ("strong", {"count_match", "linked"}),
        ("moderate", {"linked"}),
        ("weak", {"count_match"}),
    ]
    for band, signals in cases:
        text = band_rationale(band, signals)
        assert isinstance(text, str) and text


def test_band_rationale_does_not_claim_a_count_match_a_candidate_lacks():
    # linked-only: moderate, but NO count match - must not mention one
    r = band_rationale("moderate", {"linked"})
    assert "amount+timing match" not in r and "count" not in r.lower()
    # count_match + self_relayed: moderate WITH a count - may mention it
    r2 = band_rationale("moderate", {"count_match", "self_relayed"})
    assert "amount+timing" in r2 or "count" in r2.lower()
    assert r != r2


def test_ranked_candidates_carry_an_attribution_label_when_a_set_is_loaded():
    """With an attribution set, a labelled candidate's row gains the label."""
    linked = "0x%040x" % 7
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={linked})
    attribution = {linked: {"label": "Binance: Hot Wallet", "category": "exchange"}}
    ranked = ranked_candidates(data, attribution=attribution)
    assert ranked
    assert ranked[0]["address"] == linked
    assert ranked[0]["attribution"] == "Binance: Hot Wallet (exchange)"


def test_ranked_candidates_have_an_empty_attribution_without_a_set():
    """Without an attribution set, rows are unchanged: attribution is empty."""
    linked = "0x%040x" % 7
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={linked})
    ranked = ranked_candidates(data)
    assert ranked
    assert all(r["attribution"] == "" for r in ranked)


def test_ranked_candidates_attribution_is_empty_for_an_unlabelled_address():
    """A set that does not name this address leaves its label empty."""
    linked = "0x%040x" % 7
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={linked})
    attribution = {"0x%040x" % 99: {"label": "Someone else", "category": "cex"}}
    ranked = ranked_candidates(data, attribution=attribution)
    assert ranked[0]["attribution"] == ""


def test_ranked_candidates_attach_a_band():
    """Every ranked row carries a band consistent with confidence_band."""
    linked = "0x%040x" % 7
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={linked})
    ranked = ranked_candidates(data)
    assert ranked
    for row in ranked:
        assert row["band"] == confidence_band(set(row["signals"]))
    # linked-only -> moderate
    assert ranked[0]["band"] == "moderate"


def test_evidence_lists_every_family_and_marks_what_holds():
    linked = "0x%040x" % 3
    data = _data(_shared_count_pool(), voucher_count=2)
    apply_heuristics(data, counterparties={linked})
    row = next(r for r in ranked_candidates(data) if r["address"] == linked)
    by_signal = {e["signal"]: e for e in row["evidence"]}
    assert set(by_signal) == {"count_match", "self_relayed", "gas_price", "linked"}
    assert by_signal["linked"]["holds"] is True
    assert by_signal["count_match"]["holds"] is False
    assert "20 of 21 recipients" in by_signal["count_match"]["detail"]
    assert row["field_size"] == 21
    assert row["discrimination"] < 0.5


def test_the_band_follows_the_evidence_that_holds():
    """The band must be derivable from the evidence lines alone."""
    res = _pool_result(n_recipients=40, n_self_relayed=0, hits_each=1)
    winner = "0x%040x" % 1
    res["detail"][winner] = res["detail"][winner] * 3
    res["counts"][winner] = 3
    res["target_counts"] = [3]
    data = _data(res, voucher_count=3)
    apply_heuristics(data, counterparties={winner})
    for row in ranked_candidates(data):
        held = {e["signal"] for e in row["evidence"] if e["holds"]}
        assert confidence_band(held) == row["band"]


def test_a_corroborated_lead_ranks_above_a_higher_scoring_single_family_lead():
    """A moderate lead scoring 0.48 must not outrank a strong lead scoring 0.36."""
    res = _pool_result(n_recipients=40, n_self_relayed=0, hits_each=3, target=[3])
    strong, moderate = "0x%040x" % 1, "0x%040x" % 2
    res["signals"] = {a: [] for a in res["detail"]}
    res["confidence"] = {a: 0.0 for a in res["detail"]}
    res["discrimination"] = {a: 0.0 for a in res["detail"]}
    res["signals"][strong] = ["count_match", "gas_price"]
    res["confidence"][strong] = 0.3625
    res["discrimination"][strong] = 0.5
    res["signals"][moderate] = ["count_match", "self_relayed"]
    res["confidence"][moderate] = 0.475
    res["discrimination"][moderate] = 1.0
    data = _data(res, voucher_count=3)
    data["heuristics"] = {"gas_price_matches": [], "linked_addresses": []}
    ranked = ranked_candidates(data)
    assert [r["address"] for r in ranked[:2]] == [strong, moderate]
    assert strong in conclusion(data)


def test_a_count_match_in_a_tiny_field_is_not_a_signal():
    """Unique among three recipients is disc 1.0, but proves little."""
    res = _pool_result(n_recipients=3, n_self_relayed=0, hits_each=1)
    winner = "0x%040x" % 1
    res["detail"][winner] = res["detail"][winner] * 2
    res["counts"][winner] = 2
    res["target_counts"] = [2]
    data = _data(res, voucher_count=2)
    apply_heuristics(data, counterparties=set())
    assert count_discrimination(res, 2) == 1.0
    assert "count_match" not in data["denoms"]["0.1 ETH"]["signals"][winner]
    assert ranked_candidates(data) == []


def test_the_score_takes_the_strongest_signal_within_a_family():
    from tornado_demix.heuristics import _score

    # count_match and self_relayed are one family: the stronger one counts
    assert _score({"count_match", "self_relayed"}, 1.0) == 0.30
    assert _score({"count_match", "self_relayed"}, 0.5) == 0.25
    # a profile match reads the same counts
    assert _score({"count_match", "profile_match"}, 1.0) == 0.35
    # independent families still combine by noisy-OR
    assert _score({"count_match", "linked"}, 1.0) == round(1 - 0.7 * 0.6, 4)


def test_a_contract_counterparty_does_not_earn_linked():
    contract, person = "0x%040x" % 3, "0x%040x" % 4
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={contract, person}, is_contract=lambda a: a == contract)
    sigs = data["denoms"]["0.1 ETH"]["signals"]
    assert "linked" not in sigs[contract] and "linked" in sigs[person]
    assert data["heuristics"]["linked_contracts"] == [contract]


def test_an_unknown_contract_status_keeps_linked():
    person = "0x%040x" % 4
    data = _data(_pool_result(n_recipients=40, n_self_relayed=0))
    apply_heuristics(data, counterparties={person}, is_contract=lambda a: None)
    assert "linked" in data["denoms"]["0.1 ETH"]["signals"][person]


def test_the_summary_lists_facts_and_counts_one_family_for_count_and_self_relay():
    res = _pool_result(n_recipients=40, n_self_relayed=0, hits_each=3, target=[3])
    lead = "0x%040x" % 2
    res["signals"] = {a: [] for a in res["detail"]}
    res["confidence"] = {a: 0.0 for a in res["detail"]}
    res["discrimination"] = {a: 0.0 for a in res["detail"]}
    res["signals"][lead] = ["count_match", "self_relayed"]
    res["confidence"][lead] = 0.3
    res["discrimination"][lead] = 1.0
    data = _data(res, voucher_count=3)
    data["heuristics"] = {"gas_price_matches": [], "linked_addresses": []}
    lines = conclusion(data).splitlines()
    assert lines[0].startswith(f"Top candidate: {lead}")
    assert "band moderate" in lines[0]
    assert lines[1] == "Evidence families: amount+timing (1 independent)."
    assert lines[-1].startswith("Limitations:")
    assert "strongest" not in conclusion(data).lower()
