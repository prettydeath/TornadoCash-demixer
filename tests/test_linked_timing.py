"""A direct link is a lead only when withdrawn within EARLY_EXIT_HOURS (since 2.17).

Placebo test on 302 random depositors: a direct link first withdrawn within 72 h
was a lead 26 times in real windows against 1 in decoy windows; a later one 8
against 13, chance level. A late one is ``linked_late``: shown, never scored.
"""

from collections import Counter

from tornado_demix.graph import WALLET_SPECIFIC_SIGNALS
from tornado_demix.heuristics import (
    EARLY_EXIT_HOURS,
    LEAD_SIGNALS,
    LEAD_SOURCE,
    SIGNAL_WEIGHTS,
    apply_heuristics,
    band_rationale,
    candidate_evidence,
    candidate_reason,
    conclusion,
    confidence_band,
    cross_method,
    ranked_candidates,
)

WALLET = "0x" + "a" * 40
LINKED = "0x" + "b" * 40
DEPOSIT_TS = 1_700_000_000
HOUR = 3600


def _data(link_delay_hours, linked_addr=LINKED, n_others=9):
    """One 0.1 ETH voucher; ``linked_addr`` first withdraws after the given delay."""
    detail = {
        linked_addr: [
            {
                "value": 0.099,
                "ts": DEPOSIT_TS + int(link_delay_hours * HOUR),
                "hash": "0xlink",
                "relayer": "0xrelayer",
                "self_relayed": False,
            }
        ]
    }
    for i in range(n_others):
        detail["0x%040x" % (i + 1)] = [
            {
                "value": 0.099,
                "ts": DEPOSIT_TS + HOUR * (i + 1),
                "hash": "0x%x" % (i + 1),
                "relayer": "0xrelayer",
                "self_relayed": False,
            }
            for _ in range(2)
        ]
    res = {
        "pool_key": "0.1 ETH",
        "denom": 0.1,
        "asset": "ETH",
        "counts": Counter({a: len(r) for a, r in detail.items()}),
        "detail": detail,
        "target_counts": [1],
        "unique_recipients": len(detail),
    }
    return {
        "wallet": WALLET,
        "deposits": [{"pool_key": "0.1 ETH", "ts": DEPOSIT_TS}],
        "vouchers": [
            {"pool_key": "0.1 ETH", "count": 1, "first_ts": DEPOSIT_TS, "last_ts": DEPOSIT_TS}
        ],
        "denoms": {"0.1 ETH": res},
    }


def _row(data, addr=LINKED):
    return next(r for r in ranked_candidates(data) if r["address"] == addr)


def _signals(data, addr=LINKED):
    return set(data["denoms"]["0.1 ETH"]["signals"][addr])


def test_an_early_direct_link_is_a_lead():
    data = apply_heuristics(_data(10), counterparties={LINKED})
    assert "linked" in _signals(data)
    assert "linked_late" not in _signals(data)
    row = _row(data)
    assert row["band"] == "moderate"
    assert row["early_exit"] is True


def test_the_boundary_is_inclusive():
    data = apply_heuristics(_data(EARLY_EXIT_HOURS), counterparties={LINKED})
    assert "linked" in _signals(data)


def test_a_late_direct_link_is_context_with_an_unchanged_score():
    late = apply_heuristics(_data(EARLY_EXIT_HOURS + 1), counterparties={LINKED})
    none = apply_heuristics(_data(EARLY_EXIT_HOURS + 1), counterparties=set())
    assert _signals(late) - _signals(none) == {"linked_late"}
    row = _row(late)
    assert row["band"] == "weak"
    assert row["early_exit"] is False
    assert row["confidence"] == none["denoms"]["0.1 ETH"]["confidence"][LINKED]
    assert "context" in band_rationale(row["band"], set(row["signals"]))
    assert "context only" in candidate_reason(row)


def test_a_late_link_alone_is_still_listed_as_weak():
    data = _data(EARLY_EXIT_HOURS * 3)
    data["denoms"]["0.1 ETH"]["target_counts"] = [5]  # no count match for anyone
    apply_heuristics(data, counterparties={LINKED})
    row = _row(data)
    assert set(row["signals"]) == {"linked_late"}
    assert row["band"] == "weak"
    assert row["confidence"] == 0.0


def test_the_depositor_itself_follows_the_same_rule():
    early = apply_heuristics(_data(5, linked_addr=WALLET), counterparties=set())
    late = apply_heuristics(_data(200, linked_addr=WALLET), counterparties=set())
    assert "linked" in _signals(early, WALLET)
    assert "linked_late" in _signals(late, WALLET)
    assert "linked" not in _signals(late, WALLET)
    assert _row(late, WALLET)["band"] == "weak"


def test_a_late_link_is_never_a_lead_scored_or_an_operator_edge():
    assert "linked_late" not in LEAD_SOURCE
    assert "linked_late" not in LEAD_SIGNALS
    assert "linked_late" not in SIGNAL_WEIGHTS
    assert "linked_late" not in WALLET_SPECIFIC_SIGNALS
    assert confidence_band({"linked_late"}) == "weak"
    assert confidence_band({"linked_late", "count_match", "gas_price"}) == "weak"


def test_the_evidence_line_states_the_delay():
    data = apply_heuristics(_data(100), counterparties={LINKED})
    lines = {e["signal"]: e for e in candidate_evidence(data, "0.1 ETH", LINKED)}
    late = lines["linked_late"]
    assert late["holds"]
    assert "100 h after the last deposit" in late["detail"]
    assert "chance-level" in late["detail"] and "context" in late["detail"]
    assert not lines["linked"]["holds"]
    assert data["heuristics"]["linked_late"] == [("0.1 ETH", LINKED, 100.0)]
    # still recorded as a direct counterparty, so reports can list it
    assert ("0.1 ETH", LINKED) in data["heuristics"]["linked_addresses"]


def test_an_early_link_has_no_late_line():
    data = apply_heuristics(_data(10), counterparties={LINKED})
    signals = [e["signal"] for e in candidate_evidence(data, "0.1 ETH", LINKED)]
    assert "linked_late" not in signals
    assert data["heuristics"]["linked_late"] == []


def test_a_late_link_plus_a_shared_deposit_is_moderate():
    data = apply_heuristics(
        _data(200), counterparties={LINKED}, shared_deposits={LINKED: ["0x" + "d" * 40]}
    )
    row = _row(data)
    assert {"linked_late", "shared_deposit"} <= set(row["signals"])
    assert row["band"] == "moderate"
    assert band_rationale(row["band"], set(row["signals"])).startswith(
        "one lead signal (shared deposit address)"
    )


def test_an_early_link_plus_a_shared_deposit_is_strong():
    data = apply_heuristics(
        _data(10), counterparties={LINKED}, shared_deposits={LINKED: ["0x" + "d" * 40]}
    )
    assert _row(data)["band"] == "strong"


def test_a_late_link_is_not_an_independent_family():
    data = apply_heuristics(_data(200), counterparties={LINKED})
    assert all(r["address"] != LINKED for r in cross_method(data))
    text = conclusion(data)
    assert "context" not in text.split("Evidence families:")[1].split("\n")[0]
