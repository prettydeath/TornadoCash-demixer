"""The early_profile signal: the wallet's whole note profile inside 72 h windows."""

import os
import sys

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix.heuristics import (
    EARLY_PROFILE_HOURS,
    LEAD_SIGNALS,
    LINKED_SIGNALS,
    METHOD_FAMILY,
    SIGNAL_LABEL,
    SIGNAL_WEIGHTS,
    apply_heuristics,
    band_rationale,
    candidate_reason,
    confidence_band,
    method_breakdown,
    ranked_candidates,
)
from tornado_demix.report_html import build_html_report

HOUR = 3600
T0 = 1_000_000
PROFILE = {"0.1 ETH": 6, "1 ETH": 4}
POOLS = {"0.1 ETH": fx.POOL_01_ETH, "1 ETH": fx.POOL_1_ETH}
HIT = "0xeeee000000000000000000000000000000000005"


def _rec(ts, n):
    return {
        "value": 0.98,
        "ts": ts,
        "hash": "0xe%d-%d" % (ts, n),
        "relayer": "0xr",
        "fee": 0.02,
        "asset": "ETH",
        "self_relayed": False,
        "gas_price": 5,
    }


def _background(n, pool_key):
    return {
        "0x%040x" % (0xB000 + i): [_rec(T0 + 10 * HOUR + i, i)] for i in range(n)
    }  # one withdrawal each


def _deposits(profile):
    out = []
    for pool_key, n in profile.items():
        for i in range(n):
            out.append(
                {
                    "pool_key": pool_key,
                    "denom": float(pool_key.split()[0]),
                    "asset": "ETH",
                    "ts": T0 + i * HOUR,
                    "hash": "0xd%s%d" % (pool_key[0], i),
                    "block": 10 + i,
                    "to": POOLS[pool_key],
                    "via": "pool",
                    "gas_price": 7,
                }
            )
    return out


def make(profile=None, counts=None, late=None, extra=None):
    """A wallet with ``profile`` notes per pool and recipient HIT holding ``counts``.

    ``late`` names pools where HIT's last withdrawal falls after the 72 h window.
    """
    profile = PROFILE if profile is None else profile
    counts = profile if counts is None else counts
    late = late or ()
    denoms = {}
    for pool_key in profile:
        last = T0 + (profile[pool_key] - 1) * HOUR
        recs = [_rec(last + (i + 1) * HOUR, i) for i in range(counts[pool_key])]
        if pool_key in late:
            recs[-1]["ts"] = last + EARLY_PROFILE_HOURS * HOUR + 1
        detail = {HIT: recs, **_background(6, pool_key)}
        denoms[pool_key] = fx._res(
            type(
                "P",
                (),
                {"key": pool_key, "denom": 0.0, "asset": "ETH", "decimals": 18, "address": "0x"},
            ),
            detail,
            target_counts=[],
        )
    data = {
        "wallet": fx.WALLET,
        "params": {"window_days": 30, "mode": "events", "network": "ethereum", "currency": "ETH"},
        "deposits": _deposits(profile),
        "vouchers": [
            {
                "pool_key": pk,
                "denom": float(pk.split()[0]),
                "asset": "ETH",
                "count": n,
                "first_ts": T0,
                "last_ts": T0 + (n - 1) * HOUR,
                "first_block": 10,
                "deposits": [],
            }
            for pk, n in profile.items()
        ],
        "denoms": denoms,
    }
    for pool_key, res in denoms.items():
        res["denom"] = float(pool_key.split()[0])
    if extra:
        extra(data)
    return data


def _row(data, address=HIT, pool_key=None):
    return next(
        r
        for r in ranked_candidates(data)
        if r["address"] == address and (pool_key is None or r["pool_key"] == pool_key)
    )


def _line(row):
    return next(e for e in row["evidence"] if e["signal"] == "early_profile")


def test_wiring():
    assert METHOD_FAMILY["early_profile"] == "amount+timing"
    assert SIGNAL_WEIGHTS["early_profile"] == 0.35
    assert SIGNAL_LABEL["early_profile"] == "Early multi-pool profile match"
    assert LEAD_SIGNALS == LINKED_SIGNALS | {"early_profile"}
    assert "early_profile" not in LINKED_SIGNALS


def test_qualifies_with_two_pools_ten_notes_and_exact_counts():
    data = make()
    apply_heuristics(data, counterparties=set())
    for pool_key in PROFILE:
        assert "early_profile" in data["denoms"][pool_key]["signals"][HIT]
    assert sorted(data["heuristics"]["early_profiles"]) == [
        ("0.1 ETH", HIT, PROFILE),
        ("1 ETH", HIT, PROFILE),
    ]
    # the one-withdrawal background recipients never qualify
    assert {a for _p, a, _c in data["heuristics"]["early_profiles"]} == {HIT}


def test_not_with_nine_notes():
    data = make({"0.1 ETH": 5, "1 ETH": 4})
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"] == []
    assert all(
        "early_profile" not in s for r in data["denoms"].values() for s in r["signals"].values()
    )


def test_not_with_one_pool():
    data = make({"1 ETH": 12})
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"] == []


def test_not_if_one_pools_count_differs():
    data = make(counts={"0.1 ETH": 6, "1 ETH": 3})
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"] == []
    data = make(counts={"0.1 ETH": 7, "1 ETH": 4})
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"] == []


def test_not_if_a_withdrawal_falls_after_the_window():
    data = make(late=("1 ETH",))
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"] == []


def test_the_window_end_is_inclusive_and_starts_at_the_first_deposit():
    data = make()
    last = T0 + 3 * HOUR  # last deposit in 1 ETH
    data["denoms"]["1 ETH"]["detail"][HIT][-1]["ts"] = last + EARLY_PROFILE_HOURS * HOUR
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"]
    data["denoms"]["1 ETH"]["detail"][HIT][0]["ts"] = T0 - 1  # before the first deposit
    apply_heuristics(data, counterparties=set())
    assert data["heuristics"]["early_profiles"] == []


def test_band_moderate_alone_strong_with_linked_moderate_with_count_match():
    assert confidence_band({"early_profile"}) == "moderate"
    assert confidence_band({"early_profile", "linked"}) == "strong"
    assert confidence_band({"early_profile", "gas_price"}) == "strong"
    assert confidence_band({"early_profile", "count_match"}) == "moderate"
    assert confidence_band({"early_profile", "count_match", "self_relayed"}) == "moderate"
    assert confidence_band({"count_match", "gas_price"}) == "weak"


def test_ranked_admission_band_and_rationale():
    data = make()
    apply_heuristics(data, counterparties=set())
    row = _row(data, pool_key="1 ETH")
    assert row["signals"] == ["early_profile"]
    assert row["band"] == "moderate"
    assert row["early_exit"] is False  # early_exit stays linked-only
    assert row["confidence"] == SIGNAL_WEIGHTS["early_profile"]
    assert "early multi-pool profile match" in band_rationale("moderate", {"early_profile"})
    assert "linked address" in band_rationale("moderate", {"linked"})
    assert {r["address"] for r in ranked_candidates(data)} == {HIT}


def test_with_a_linked_address_the_band_is_strong_and_score_is_noisy_or():
    data = make()
    apply_heuristics(data, counterparties={HIT})
    row = _row(data, pool_key="1 ETH")
    assert row["band"] == "strong"
    assert row["confidence"] == round(1 - (1 - 0.35) * (1 - 0.40), 4)


def test_evidence_present_absent_and_not_applicable():
    data = make()
    apply_heuristics(data, counterparties=set())
    row = _row(data, pool_key="1 ETH")
    line = _line(row)
    assert line["holds"] is True and line["family"] == "amount+timing"
    assert "6×0.1 ETH + 4×1 ETH" in line["detail"]
    assert "within 72 h of the last deposit in each pool" in line["detail"]
    assert "full note profile within 72 h" in candidate_reason(row)

    missed = make(counts={"0.1 ETH": 6, "1 ETH": 3})
    apply_heuristics(missed, counterparties={HIT})
    row = _row(missed, pool_key="1 ETH")
    line = _line(row)
    assert line["holds"] is False
    assert "did not receive" in line["detail"]
    assert "full note profile" not in candidate_reason(row)

    small = make({"0.1 ETH": 3, "1 ETH": 4})
    apply_heuristics(small, counterparties={HIT})
    line = _line(_row(small, pool_key="1 ETH"))
    assert line["holds"] is False and "does not apply" in line["detail"]
    assert "2 pools and 10 notes" in line["detail"]


def test_method_breakdown_lists_the_recipient():
    data = make()
    apply_heuristics(data, counterparties=set())
    group = next(m for m in method_breakdown(data) if m["method"] == "early_profile")
    assert group["label"] == "Early multi-pool profile match"
    assert {r["address"] for r in group["rows"]} == {HIT}


def test_the_html_report_shows_it():
    data = make()
    apply_heuristics(data, counterparties=set())
    html = build_html_report(data, fx.network())
    assert "full note profile within 72 h" in html
    assert "6×0.1 ETH + 4×1 ETH" in html
    assert "an early multi-pool profile match on its own" in html
    plain = make(counts={"0.1 ETH": 6, "1 ETH": 3})
    apply_heuristics(plain, counterparties=set())
    assert "full note profile within 72 h" not in build_html_report(plain, fx.network())


pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)
import app as webapp  # noqa: E402


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: object())
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = "tok"
    return client


def test_the_web_ui_shows_the_evidence_and_the_band_legend(web, monkeypatch):
    data = make()
    apply_heuristics(data, counterparties=set())
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **kw: data)
    body = web.post("/", data={"wallets": fx.WALLET, "analysis": "demix", "csrf": "tok"}).get_data(
        as_text=True
    )
    assert HIT in body
    assert "6×0.1 ETH + 4×1 ETH" in body
    assert "early multi-pool profile match" in body
