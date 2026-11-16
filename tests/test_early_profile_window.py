"""The early profile needs the whole 72 h span of every pool read (a shorter window, a cut-off list)."""

import copy

from tests.test_early_profile import HIT, HOUR, PROFILE, T0, make
from tornado_demix.heuristics import (
    EARLY_PROFILE_HISTORY_NOTE,
    EARLY_PROFILE_WINDOW_NOTE,
    apply_heuristics,
    candidate_evidence,
    early_profile_gap,
)

WINDOW_NOTE = "early profile not applied: window shorter than 72 h"
HISTORY_NOTE = "early profile not applied: withdrawal history incomplete"
POOL_ADDR = "0x" + "aa" * 20


def _score(data, hours=None):
    if hours is not None:
        data["params"]["exit_window_hours"] = hours
    for res in data["denoms"].values():
        res["pool"] = POOL_ADDR
    return apply_heuristics(data, set(), is_contract=lambda a: False)


def _detail(data):
    ev = candidate_evidence(data, "1 ETH", HIT)
    return next(e["detail"] for e in ev if e["signal"] == "early_profile")


def _signals(data):
    return {pk: r["signals"][HIT] for pk, r in data["denoms"].items()}


def test_notes_are_the_documented_strings():
    assert EARLY_PROFILE_WINDOW_NOTE == WINDOW_NOTE
    assert EARLY_PROFILE_HISTORY_NOTE == HISTORY_NOTE


def test_one_hour_window_does_not_apply_the_profile():
    # short-window case: the notes fall inside one hour of the search, so a profile match
    # appears there; the full 72 h would hold more withdrawals and no match
    data = make()
    _score(data, hours=1)
    assert all("early_profile" not in s for s in _signals(data).values())
    assert data["heuristics"]["early_profiles"] == []
    assert data["heuristics"]["early_profile_note"] == WINDOW_NOTE
    assert WINDOW_NOTE in _detail(data)


def test_window_of_exactly_72_h_still_applies():
    data = make()
    _score(data, hours=72)
    assert all("early_profile" in s for s in _signals(data).values())
    assert "early_profile_note" not in data["heuristics"]


def test_default_window_days_still_applies():
    data = make()
    _score(data)
    assert all("early_profile" in s for s in _signals(data).values())
    assert "early_profile_note" not in data["heuristics"]


def test_full_window_with_extra_withdrawals_has_no_match_and_no_note():
    data = make(counts={"0.1 ETH": 7, "1 ETH": 5})
    _score(data, hours=72)
    assert all("early_profile" not in s for s in _signals(data).values())
    assert "early_profile_note" not in data["heuristics"]


def _cut(reason, address=POOL_ADDR):
    return {"action": "getLogs", "address": address, "block": None, "limit": None, "reason": reason}


def test_truncated_pool_list_does_not_apply_the_profile():
    for reason in ("truncated", "partial_indexing"):
        data = make()
        data["truncated"] = [_cut(reason)]
        _score(data)
        assert all("early_profile" not in s for s in _signals(data).values())
        assert data["heuristics"]["early_profile_note"] == HISTORY_NOTE
        assert HISTORY_NOTE in _detail(data)


def test_notice_or_other_address_does_not_block_the_profile():
    data = make()
    data["truncated"] = [
        _cut("rpc_fallback_no_gas_price"),
        _cut("truncated", address="0x" + "bb" * 20),
    ]
    _score(data)
    assert all("early_profile" in s for s in _signals(data).values())
    assert "early_profile_note" not in data["heuristics"]


def test_pool_note_wins_over_window_note():
    data = make()
    data["deposits"][0]["pool_ambiguous"] = ["0.1 ETH", "1 ETH"]
    _score(data, hours=1)
    assert data["heuristics"]["early_profile_note"] == "early profile not applied: pool uncertain"


def test_hole_between_vouchers_is_incomplete_history():
    data = make()
    # one pool, two vouchers 200 h apart, searched 72 h after each: the span between is unread
    first = data["vouchers"][0]
    far = copy.deepcopy(first)
    far["first_ts"] = far["last_ts"] = T0 + 200 * HOUR
    data["vouchers"].append(far)
    data["deposits"].append(dict(data["deposits"][0], ts=T0 + 200 * HOUR, hash="0xlate"))
    data["params"]["exit_window_hours"] = 72
    assert early_profile_gap(data, PROFILE) == HISTORY_NOTE
    data["params"]["exit_window_hours"] = 400
    assert early_profile_gap(data, PROFILE) is None


def test_group_view_reads_one_span_so_voucher_gaps_do_not_count():
    data = make()
    data["params"]["group"] = True
    far = copy.deepcopy(data["vouchers"][0])
    far["first_ts"] = far["last_ts"] = T0 + 500 * HOUR
    data["vouchers"].append(far)
    assert early_profile_gap(data, PROFILE) is None
