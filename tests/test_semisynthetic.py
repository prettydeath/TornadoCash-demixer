"""The semi-synthetic benchmark injects planted exits consistently and scores them right."""

import importlib.util
import os
import random
import sys

import pytest

from tornado_demix.heuristics import confidence_band, count_discrimination

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "semisynthetic.py")
_spec = importlib.util.spec_from_file_location("semisynthetic", _PATH)
sem = importlib.util.module_from_spec(_spec)
sys.modules["semisynthetic"] = sem  # dataclasses resolve annotations through it
_spec.loader.exec_module(sem)

POOL = "1 ETH"
POOL2 = "0.1 ETH"
T = 1_700_000_000
WALLET = "0x" + "aa" * 20


def _bg_addr(i):
    return "0x%040x" % (0xB0000 + i)


def background(n=30, pools=(POOL,), counterparties=(), senders=None, shared=None):
    """A small slim background run: n recipients per pool, 1-3 withdrawals each."""
    denoms = {}
    for p, pk in enumerate(pools):
        detail = {}
        for i in range(n):
            recs = []
            for j in range(1 + i % 3):
                recs.append(
                    {
                        "value": 1.0,
                        "ts": T + 3600 * (1 + i + j) + p,
                        "hash": "0x%064x" % (1000 * p + 10 * i + j),
                        "block": None,
                        "self_relayed": i % 5 == 0,
                        "gas_price": None,
                    }
                )
            detail[_bg_addr(i)] = recs
        # one withdrawal before the planted voucher: must be narrowed away
        detail[_bg_addr(999)] = [
            {
                "value": 1.0,
                "ts": T - 10,
                "hash": "0x%064x" % (99999 + p),
                "block": None,
                "self_relayed": False,
                "gas_price": None,
            }
        ]
        denoms[pk] = {
            "pool_key": pk,
            "denom": 1.0 if pk == POOL else 0.1,
            "asset": "ETH",
            "start": T - 60,
            "detail": detail,
        }
    return {
        "wallet": WALLET,
        "denoms": denoms,
        "counterparties": set(counterparties),
        "senders": dict(senders or {}),
        "shared": dict(shared or {}),
        "deposit_gas": [33 * 10**9],
    }


NO_ERRORS = dict(p_self=0.0, p_gas=0.0, p_link=0.0, p_sender=0.0, p_shared=0.0)


@pytest.fixture
def fast_and_late(monkeypatch):
    monkeypatch.setitem(sem.DELAYS, "now", (1.0, 0.01))
    monkeypatch.setitem(sem.DELAYS, "late", (200.0, 0.01))
    monkeypatch.setitem(sem.DELAYS, "never", (5000.0, 0.01))


def planted_exits(positives):
    return {a for _pk, a in positives}


def test_injection_updates_counts_field_and_discrimination(fast_and_late):
    run = background()
    beh = sem.Behaviour("now", "single", **NO_ERRORS)
    data, positives, info = sem.plant(run, {POOL: 4}, beh, random.Random(1))
    res = data["denoms"][POOL]
    (exit_addr,) = planted_exits(positives)
    assert len(res["detail"][exit_addr]) == 4 and res["counts"][exit_addr] == 4
    assert res["counts"] == {a: len(r) for a, r in res["detail"].items()}
    # 30 background recipients in the window + the planted exit; the early one is gone
    assert res["unique_recipients"] == 31 and _bg_addr(999) not in res["detail"]
    assert res["target_counts"] == [4]
    for addr in res["detail"]:
        assert res["discrimination"][addr] == round(
            count_discrimination(res, res["counts"][addr]), 4
        )
    # a unique 4-note count in a 31-recipient field is a count match
    assert "count_match" in res["signals"][exit_addr]
    assert info["planted_observed"] == 4 and info["exits"] == 1
    # the real deposits are gone: only the planted voucher is searched
    assert [v["count"] for v in data["vouchers"]] == [4]


def test_background_is_kept_as_cached(fast_and_late):
    run = background()
    data, positives, _ = sem.plant(
        run, {POOL: 2}, sem.Behaviour("now", "single", **NO_ERRORS), random.Random(2)
    )
    exit_addr = next(iter(planted_exits(positives)))
    for addr, recs in data["denoms"][POOL]["detail"].items():
        if addr != exit_addr:
            assert recs == run["denoms"][POOL]["detail"][addr]
    # the background run itself is not mutated
    assert exit_addr not in run["denoms"][POOL]["detail"]


@pytest.mark.parametrize(
    "rates, signal",
    [
        (dict(NO_ERRORS, p_link=1.0), "linked"),
        (dict(NO_ERRORS, p_sender=1.0), "linked_sender"),
        (dict(NO_ERRORS, p_shared=1.0), "shared_deposit"),
    ],
)
def test_link_errors_reach_the_heuristics(fast_and_late, rates, signal):
    data, positives, _ = sem.plant(
        background(), {POOL: 1}, sem.Behaviour("now", "single", **rates), random.Random(3)
    )
    (exit_addr,) = planted_exits(positives)
    sig = set(data["denoms"][POOL]["signals"][exit_addr])
    assert signal in sig
    assert confidence_band(sig) == "moderate"


def test_a_late_direct_link_is_context_only(fast_and_late):
    data, positives, info = sem.plant(
        background(),
        {POOL: 1},
        sem.Behaviour("late", "single", **dict(NO_ERRORS, p_link=1.0)),
        random.Random(4),
    )
    (exit_addr,) = planted_exits(positives)
    sig = set(data["denoms"][POOL]["signals"][exit_addr])
    assert "linked_late" in sig and "linked" not in sig
    assert info["early_link"] == 0


def test_two_independent_leads_make_strong(fast_and_late):
    rates = dict(NO_ERRORS, p_link=1.0, p_shared=1.0)
    data, positives, _ = sem.plant(
        background(), {POOL: 2}, sem.Behaviour("now", "single", **rates), random.Random(5)
    )
    (exit_addr,) = planted_exits(positives)
    assert confidence_band(set(data["denoms"][POOL]["signals"][exit_addr])) == "strong"


def test_self_relay_and_gas_reuse(fast_and_late):
    rates = dict(NO_ERRORS, p_self=1.0, p_gas=1.0)
    data, positives, _ = sem.plant(
        background(), {POOL: 2}, sem.Behaviour("now", "single", **rates), random.Random(6)
    )
    (exit_addr,) = planted_exits(positives)
    sig = set(data["denoms"][POOL]["signals"][exit_addr])
    assert {"self_relayed", "gas_price"} <= sig
    gas = [r["gas_price"] for r in data["denoms"][POOL]["detail"][exit_addr] if r["gas_price"]]
    assert gas == [33 * 10**9]  # one withdrawal carries the deposit gas price


def test_inherited_links_apply_to_background_recipients(fast_and_late):
    run = background(counterparties={_bg_addr(1)}, shared={_bg_addr(2): ["0xdep"]})
    data, _positives, _ = sem.plant(
        run, {POOL: 2}, sem.Behaviour("now", "single", **NO_ERRORS), random.Random(7)
    )
    sig = data["denoms"][POOL]["signals"]
    assert "linked" in sig[_bg_addr(1)]  # first withdrawal 2 h after the planted voucher
    assert "shared_deposit" in sig[_bg_addr(2)]


def test_an_exit_after_the_window_is_a_missed_positive(fast_and_late):
    data, positives, info = sem.plant(
        background(), {POOL: 3}, sem.Behaviour("never", "single", **NO_ERRORS), random.Random(8)
    )
    (exit_addr,) = planted_exits(positives)
    assert exit_addr not in data["denoms"][POOL]["detail"]
    assert info["planted_observed"] == 0
    c = sem.evaluate(data, positives, sem.new_counter())
    assert c["pos"] == 1 and c["pos_observed"] == 0 and c["unlisted_pos"] == 1
    m = sem.metrics(c)
    assert m["recall_any"] == 0.0 and m["observed_share"] == 0.0


def test_spread_and_multi_pool(fast_and_late):
    beh = sem.Behaviour("now", "spread", **NO_ERRORS)
    data, positives, info = sem.plant(
        background(pools=(POOL, POOL2)), {POOL: 6, POOL2: 5}, beh, random.Random(9)
    )
    assert set(data["denoms"]) == {POOL, POOL2}
    assert 1 <= info["exits"] <= 11 and info["notes"] == 11
    planted = sum(
        len(data["denoms"][pk]["detail"][a])
        - len(background(pools=(POOL, POOL2))["denoms"][pk]["detail"].get(a, []))
        for pk, a in positives
    )
    assert planted == 11


def test_one_exit_with_a_wide_profile_earns_the_early_profile(fast_and_late):
    beh = sem.Behaviour("now", "single", **NO_ERRORS)
    data, positives, _ = sem.plant(
        background(pools=(POOL, POOL2)), {POOL: 6, POOL2: 5}, beh, random.Random(10)
    )
    (exit_addr,) = planted_exits(positives)
    for pk in (POOL, POOL2):
        assert "early_profile" in data["denoms"][pk]["signals"][exit_addr]


def test_reuse_puts_the_notes_on_a_busy_background_address(fast_and_late):
    beh = sem.Behaviour("now", "single", **dict(NO_ERRORS, p_reuse=1.0))
    run = background()
    data, positives, _ = sem.plant(run, {POOL: 2}, beh, random.Random(11))
    (exit_addr,) = planted_exits(positives)
    before = len(run["denoms"][POOL]["detail"][exit_addr])
    assert data["denoms"][POOL]["counts"][exit_addr] == before + 2


def test_fast_discrimination_matches_the_rule(fast_and_late):
    rng = random.Random(12)
    for _ in range(200):
        counts = {f"a{i}": rng.choice([1, 1, 1, 2, 3, 5]) for i in range(rng.randint(0, 40))}
        res = {"counts": counts, "unique_recipients": len(counts)}
        for hits in (1, 2, 3, 4, 5):
            assert sem.fast_count_discrimination(res, hits) == count_discrimination(res, hits)
    # and the whole scoring is identical with and without it
    beh = sem.Behaviour("now", "spread", **sem.ERRORS["frequent"])
    slow, pos_slow, _ = sem.plant(background(), {POOL: 3}, beh, random.Random(13))
    with sem.fast_discrimination():
        fast, pos_fast, _ = sem.plant(background(), {POOL: 3}, beh, random.Random(13))
    assert pos_slow == pos_fast
    for key in ("signals", "confidence", "discrimination"):
        assert slow["denoms"][POOL][key] == fast["denoms"][POOL][key]


def _scored(signals_by_addr, positives):
    """A minimal scored window for evaluate(): one pool, given signals."""
    detail = {a: [{"value": 1.0, "ts": T, "self_relayed": False}] for a in signals_by_addr}
    from tornado_demix.heuristics import _score

    res = {
        "pool_key": POOL,
        "denom": 1.0,
        "asset": "ETH",
        "detail": detail,
        "counts": {a: 1 for a in detail},
        "unique_recipients": len(detail),
        "target_counts": [1],
        "signals": {a: sorted(s) for a, s in signals_by_addr.items()},
        "discrimination": {a: 0.0 for a in detail},
        "confidence": {a: _score(set(s), 0.0) for a, s in signals_by_addr.items()},
    }
    return {"wallet": WALLET, "vouchers": [], "deposits": [], "denoms": {POOL: res}}, {
        (POOL, a) for a in positives
    }


def test_metrics_by_level():
    sig = {
        "p_strong": {"linked", "shared_deposit"},
        "p_mod": {"linked"},
        "p_none": set(),
        "n_mod": {"shared_deposit"},
        "n_weak": {"gas_price"},
        "n_none": set(),
        "n_none2": set(),
    }
    data, positives = _scored(sig, {"p_strong", "p_mod", "p_none"})
    m = sem.metrics(sem.evaluate(data, positives, sem.new_counter()))
    assert m["positives"] == 3 and m["negatives"] == 4
    assert m["precision_strong"] == 1.0 and m["recall_strong"] == pytest.approx(1 / 3)
    assert m["precision_moderate+"] == pytest.approx(2 / 3)
    assert m["recall_moderate+"] == pytest.approx(2 / 3)
    assert m["precision_any"] == pytest.approx(2 / 4)
    assert m["false_per_1000_moderate+"] == pytest.approx(250.0)
    assert m["rep_false_moderate+"] == 1.0 and m["rep_false_strong"] == 0.0
    assert m["top1"] == 1.0 and m["top5"] == 1.0
    # order: strong(p), moderate p_mod / n_mod tie on score, weak n -> AP
    assert m["pr_auc"] == pytest.approx((1 / 3) * 1 + (1 / 3) * (2 / 3))
    assert m["signals"]["shared_deposit"] == {
        "tp": 1,
        "fp": 1,
        "precision": 0.5,
        "recall": pytest.approx(1 / 3),
    }
    assert m["band_precision_moderate"] == 0.5


def test_counters_pool_and_bootstrap():
    data, positives = _scored({"p": {"linked"}, "n": set()}, {"p"})
    per_run = {}
    for run in ("a", "b", "c"):
        c = sem.new_counter()
        sem.evaluate(data, positives, c, sem.new_counter())
        per_run[run] = c
    pooled = sem.metrics(sem.merge(per_run.values()))
    assert pooled["replicates"] == 3 and pooled["recall_moderate+"] == 1.0
    ci = sem.bootstrap(per_run, random.Random(1), n=20)
    assert ci["recall_moderate+"] == [1.0, 1.0]


def test_session_profiles_follow_the_voucher_rule():
    day = 86400
    deposits = [
        ["1 ETH", "s1", 0, "h"],
        ["1 ETH", "s1", 600, "h"],
        ["0.1 ETH", "s1", 1200, "h"],
        ["1 ETH", "s1", 10 * day, "h"],  # a new session
        ["1 ETH", "s2", 0, "h"],
    ]
    dist = sem.session_profiles(deposits)
    assert dist == {(2, 1): 1, (1,): 2}
    sampler = sem.ProfileSampler(sorted(dist.items()))
    assert sampler.draw(random.Random(1)) in dist
    assert sem.map_profile((2, 1), ["1 ETH"], random.Random(1)) == {"1 ETH": 2}


def test_slim_run_keeps_only_gated_gas_prices():
    rec = {"value": 1.0, "ts": T, "hash": "0xAB", "block": 1, "self_relayed": True}
    data = {
        "wallet": WALLET.upper(),
        "deposits": [{"gas_price": 5}],
        "denoms": {
            POOL: {
                "denom": 1.0,
                "asset": "ETH",
                "windows": [{"first_ts": T}],
                "window_ts": [T, T + 1],
                "detail": {
                    "x": [dict(rec, gas_price=7)],
                    "y": [dict(rec, hash="0xcd", gas_price=9)],
                },
            }
        },
        "heuristics": {
            "gas_price_matches": [[POOL, "x", 7, "0xab"]],
            "linked_addresses": [[POOL, "y"]],
            "linked_senders": [[POOL, "y", WALLET, "0xCD"]],
            "shared_deposits": [["*", "x", ["0xdep"]]],
        },
    }
    run = sem.slim_run(data)
    assert run["denoms"][POOL]["detail"]["x"][0]["gas_price"] == 7
    assert run["denoms"][POOL]["detail"]["y"][0]["gas_price"] is None
    assert run["counterparties"] == {"y"} and run["senders"] == {"0xcd": WALLET}
    assert run["shared"] == {"x": ["0xdep"]} and run["deposit_gas"] == [5]
    assert run["wallet"] == WALLET


def test_default_grid_covers_the_requested_axes():
    names = [n for n, _b in sem.default_grid()]
    assert len(names) == len(set(names)) == 4 * 3 * 2 + 1
    assert sem.delay_beyond("heavy", 72) == pytest.approx(0.5)
    assert sem.delay_beyond("fast", 72) < 0.001
    assert 0.1 < sem.delay_beyond("heavy", 720) < 0.25


def test_histogram_average_precision_matches_the_simulator():
    sim_path = os.path.join(os.path.dirname(_PATH), "simulate.py")
    spec = importlib.util.spec_from_file_location("simulate_for_ap", sim_path)
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    rng = random.Random(14)
    from collections import Counter

    for _ in range(100):
        pairs = [
            (rng.choice([None, 10.4, 20.4, 20.64, 30.7, 10.0]), rng.random() < 0.3)
            for _ in range(rng.randint(1, 30))
        ]
        hist = Counter((s, p) for s, p in pairs if s is not None)
        unlisted = sum(p for s, p in pairs if s is None)
        assert sem.average_precision_hist(hist, unlisted) == pytest.approx(
            sim.average_precision(pairs)
        )
