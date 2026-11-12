"""A decoy window is informative only if a planted early direct link would be found in it."""

import copy
from collections import Counter

from tornado_demix.heuristics import ranked_candidates
from tornado_demix.power import plant_early_link, window_detectability, window_detectable

WALLET = "0x" + "a" * 40
COUNTERPARTY = "0x" + "c" * 40
POOL = "0.1 ETH"
T = 1_700_000_000
HOUR = 3600


def _rec(ts, n):
    return {
        "value": 0.099,
        "ts": ts,
        "hash": "0x%x" % n,
        "relayer": "0xrelayer",
        "self_relayed": False,
    }


def _data(extra=None):
    """One 0.1 ETH voucher; ten unrelated recipients; ``extra`` {addr: [ts, ...]} is added."""
    detail = {"0x%040x" % (i + 1): [_rec(T + HOUR * (i + 1), i + 1)] for i in range(10)}
    for k, (addr, stamps) in enumerate((extra or {}).items()):
        detail[addr] = [_rec(ts, 100 + k * 10 + j) for j, ts in enumerate(stamps)]
    res = {
        "pool_key": POOL,
        "denom": 0.1,
        "asset": "ETH",
        "counts": Counter({a: len(r) for a, r in detail.items()}),
        "detail": detail,
        "target_counts": [1],
        "unique_recipients": len(detail),
    }
    return {
        "wallet": WALLET,
        "deposits": [{"pool_key": POOL, "ts": T}],
        "vouchers": [{"pool_key": POOL, "count": 1, "first_ts": T, "last_ts": T}],
        "params": {"window_days": 30, "exit_window_hours": None},
        "denoms": {POOL: res},
    }


def test_plant_adds_withdrawal_without_touching_input():
    data = _data()
    before = copy.deepcopy(data)
    out = plant_early_link(data, WALLET, POOL, COUNTERPARTY, T + HOUR)
    assert data == before
    res = out["denoms"][POOL]
    assert res["counts"][COUNTERPARTY] == 1
    assert res["detail"][COUNTERPARTY][0]["ts"] == T + HOUR
    assert res["unique_recipients"] == 11


def test_ordinary_window_is_detectable():
    assert window_detectable(_data(), WALLET, counterparties={COUNTERPARTY}) is True


def test_planted_recipient_gets_moderate_band():
    data = plant_early_link(_data(), WALLET, POOL, COUNTERPARTY, T + HOUR)
    from tornado_demix.demix import score_collected

    score_collected(data, {COUNTERPARTY}, {})
    row = next(r for r in ranked_candidates(data) if r["address"] == COUNTERPARTY)
    assert row["band"] == "moderate"


def test_wallet_without_counterparty_is_not_detectable():
    assert window_detectable(_data(), WALLET, counterparties=set()) is False
    got = window_detectability(_data(), WALLET, counterparties={WALLET})
    assert got["reason"] == "no_counterparty"


def test_contract_counterparty_is_not_a_place_to_plant():
    got = window_detectability(
        _data(), WALLET, counterparties={COUNTERPARTY}, is_contract=lambda a: True
    )
    assert got["detectable"] is False and got["reason"] == "no_counterparty"


def test_existing_late_link_becomes_early_with_planted_withdrawal():
    data = _data({COUNTERPARTY: [T + 200 * HOUR]})
    score_before = copy.deepcopy(data)
    from tornado_demix.demix import score_collected

    score_collected(score_before, {COUNTERPARTY}, {})
    assert score_before["denoms"][POOL]["signals"][COUNTERPARTY] == ["linked_late"]
    got = window_detectability(data, WALLET, counterparties={COUNTERPARTY})
    assert got["detectable"] is True and got["recipient"] == COUNTERPARTY


def test_earlier_withdrawal_outside_72h_makes_plant_missed():
    data = _data({COUNTERPARTY: [T - 5 * HOUR]})
    got = window_detectability(data, WALLET, counterparties={COUNTERPARTY})
    assert got["detectable"] is False and got["reason"] == "missed"


def test_no_voucher_is_reported():
    data = _data()
    data["vouchers"] = []
    got = window_detectability(data, WALLET, counterparties={COUNTERPARTY})
    assert got["reason"] == "no_voucher"


def test_self_withdrawal_is_reported_when_only_contract_counterparties_exist():
    got = window_detectability(
        _data(), WALLET, counterparties={COUNTERPARTY}, is_contract=lambda a: a == COUNTERPARTY
    )
    # the flag keeps its meaning (no non-contract counterparty), the self plant is separate
    assert got["detectable"] is False and got["reason"] == "no_counterparty"
    assert got["self_detectable"] is True


def test_self_plant_is_not_tried_when_a_counterparty_exists_or_wallet_is_a_contract():
    ok = window_detectability(_data(), WALLET, counterparties={COUNTERPARTY})
    assert ok["self_detectable"] is None
    got = window_detectability(
        _data(), WALLET, counterparties={COUNTERPARTY}, is_contract=lambda a: True
    )
    assert got["self_detectable"] is False
