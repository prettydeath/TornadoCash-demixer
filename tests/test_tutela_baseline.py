"""Tutela / Beres baseline under the placebo design: offline logic of tools/tutela_baseline.py."""

import os
import sys

import pytest

pytest.importorskip("Crypto", reason="the tools' loaders import ens_labels (pycryptodome)")
pytest.importorskip("scipy")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import tutela_baseline as tb  # noqa: E402

H = 3600
G = 10**9


def _a(i):
    return "0x" + format(i, "040x")


DEP, R1, R2, R3, RELAYER = _a(1), _a(2), _a(3), _a(4), _a(99)


def _w(ts, h, gas=20 * G, relayer=RELAYER):
    return {"ts": ts, "hash": h, "gas_price": gas, "relayer": relayer}


def _run(detail, deposits, heuristics=None, wallet=DEP):
    """detail: {pool_key: {recipient: [withdrawal records]}}."""
    return {
        "wallet": wallet,
        "deposits": deposits,
        "denoms": {
            pk: {"detail": recs, "counts": {a: len(r) for a, r in recs.items()}}
            for pk, recs in detail.items()
        },
        "heuristics": heuristics or {},
    }


def _dep(pk, ts, h, gas=20 * G, block=100):
    return {"pool_key": pk, "ts": ts, "hash": h, "gas_price": gas, "block": block}


# ----------------------------------------------------------------- address match


def test_address_match_needs_an_earlier_deposit_but_beres_h1_does_not():
    run = _run(
        {"1 ETH": {DEP: [_w(50, "0xw1")], R1: [_w(500, "0xw2")]}},
        [_dep("0.1 ETH", 100, "0xd1")],
    )
    assert tb.address_match_leads(run) == set()  # withdrawal before any deposit
    assert tb.beres_h1_leads(run) == {("1 ETH", DEP)}
    run["denoms"]["1 ETH"]["detail"][DEP].append(_w(200, "0xw3"))
    assert tb.address_match_leads(run) == {("1 ETH", DEP)}


# ----------------------------------------------------------------- gas price


def test_gas_tutela_requires_unique_price_self_sent_withdrawal_and_earlier_deposit():
    price = 23 * G
    run = _run(
        {
            "1 ETH": {
                R1: [_w(500, "0xa", gas=price)],  # self-sent below
                R2: [_w(500, "0xb", gas=price)],  # relayed
                R3: [_w(50, "0xc", gas=price)],  # before the deposit
            }
        },
        [_dep("1 ETH", 100, "0xd1", gas=price)],
    )
    senders = {"0xa": R1, "0xb": RELAYER, "0xc": R3}
    assert tb.gas_leads(run, lambda p: True, senders, "tutela") == {("1 ETH", R1)}
    assert tb.gas_leads(run, lambda p: False, senders, "tutela") == set()


def test_gas_beres_requires_a_manually_set_price_and_no_sender_filter():
    round_price, odd_price = 23 * G, 5130909091
    run = _run(
        {"1 ETH": {R1: [_w(500, "0xa", gas=round_price)], R2: [_w(500, "0xb", gas=odd_price)]}},
        [_dep("1 ETH", 100, "0xd1", gas=round_price), _dep("1 ETH", 110, "0xd2", gas=odd_price)],
    )
    assert tb.gas_leads(run, lambda p: True, {}, "beres") == {("1 ETH", R2)}
    assert tb.gas_leads(run, lambda p: p != odd_price, {}, "beres") == set()


def test_gas_uniqueness_counts_distinct_deposits_in_cached_tiles(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "TUT", str(tmp_path))
    price, block = 31 * G, 12_000_000
    tile = block // tb.TILE
    contract = tb.SCAN_CONTRACTS[0]
    tb._jsave(
        os.path.join("tiles", f"{contract}_{tile}.json"),
        [["0xown", DEP, price, True], ["0xwd", R1, price, False]],
    )
    for c in tb.SCAN_CONTRACTS:
        for t in tb.tiles_for(block, tb.LEVELS[0]):
            if not os.path.exists(os.path.join(tmp_path, "tiles", f"{c}_{t}.json")):
                tb._jsave(os.path.join("tiles", f"{c}_{t}.json"), [])
    gu = tb.GasUniqueness(None, set(), levels=(tb.LEVELS[0],))
    senders = {}
    v = gu.resolve(price, {"0xown": block}, senders)
    assert v["unique"] is True  # the withdrawal at the same price is not a deposit
    assert senders["0xwd"] == R1
    tb._jsave(
        os.path.join("tiles", f"{contract}_{tile}.json"),
        [["0xown", DEP, price, True], ["0xother", R2, price, True]],
    )
    gu = tb.GasUniqueness(None, set(), levels=(tb.LEVELS[0],))
    gu.state = {}
    assert gu.resolve(price, {"0xown": block}, {})["unique"] is False


def test_gas_uniqueness_is_unresolved_offline_without_tiles(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "TUT", str(tmp_path))
    gu = tb.GasUniqueness(None, set())
    assert gu.resolve(7 * G, {"0xown": 12_000_000}, {}) is None


# ----------------------------------------------------------------- linked addresses


def test_beres_h3_uses_direct_counterparties_and_contracts_but_not_the_depositor():
    run = _run(
        {"1 ETH": {R1: [_w(500, "0xa")], R2: [_w(500, "0xb")], DEP: [_w(500, "0xc")]}},
        [_dep("1 ETH", 100, "0xd1")],
        heuristics={"linked_addresses": [["1 ETH", R1], ["1 ETH", DEP]], "linked_contracts": [R2]},
    )
    assert tb.beres_h3_leads(run) == {("1 ETH", R1), ("1 ETH", R2)}


def test_linked_tutela_needs_three_txs_in_one_direction_and_a_same_pool_deposit():
    run = _run(
        {
            "1 ETH": {R1: [_w(500, "0xa")], R2: [_w(500, "0xb")]},
            "10 ETH": {R3: [_w(500, "0xc")]},
        },
        [_dep("1 ETH", 100, "0xd1")],
    )
    counts = {R1: [3, 0], R2: [2, 2], R3: [0, 5]}
    # R2: 4 txs but only 2 per direction; R3: no deposit in the 10 ETH pool
    assert tb.linked_tutela_leads(run, counts) == {("1 ETH", R1)}


# ----------------------------------------------------------------- multi-denomination


def _univ(entries):
    out = {}
    for addr, pool, ts, h in entries:
        out.setdefault(addr, []).append((pool, ts, h))
    return lambda a: out.get(a, [])


def test_multi_denom_matches_an_exact_portfolio_within_24h():
    deposits = [
        _dep("1 ETH", 1000, "0xd1"),
        _dep("0.1 ETH", 1100, "0xd2"),
        _dep("0.1 ETH", 1200, "0xd3"),
    ]  # portfolio at the last deposit: {1 ETH: 1, 0.1 ETH: 2}
    run = _run(
        {"0.1 ETH": {R1: [_w(5000, "0xw3")], R2: [_w(5000, "0xx2")], R3: [_w(5000, "0xy2")]}},
        deposits,
    )
    univ = _univ(
        [
            (R1, "1 ETH", 4000, "0xw1"),
            (R1, "0.1 ETH", 4500, "0xw2"),
            (R1, "0.1 ETH", 5000, "0xw3"),  # same portfolio -> lead
            (R2, "1 ETH", 4000, "0xx1"),
            (R2, "0.1 ETH", 5000, "0xx2"),  # {1 ETH: 1, 0.1 ETH: 1}: no match
            (R3, "1 ETH", 5000 - 25 * H, "0xy1"),  # outside 24 h
            (R3, "0.1 ETH", 4500, "0xy0"),
            (R3, "0.1 ETH", 5000, "0xy2"),
        ]
    )
    assert tb.multi_denom_leads(run, univ) == {("0.1 ETH", R1)}


def test_multi_denom_ignores_single_pool_and_two_tx_portfolios():
    one_pool = _run(
        {"1 ETH": {R1: [_w(5000, "0xw2")]}},
        [_dep("1 ETH", 1000, "0xd1"), _dep("1 ETH", 1100, "0xd2"), _dep("1 ETH", 1200, "0xd3")],
    )
    univ = _univ(
        [(R1, "1 ETH", 4000, "0xw0"), (R1, "1 ETH", 4500, "0xw1"), (R1, "1 ETH", 5000, "0xw2")]
    )
    assert tb.multi_denom_leads(one_pool, univ) == set()
    two_tx = _run(
        {"1 ETH": {R1: [_w(5000, "0xw2")]}},
        [_dep("0.1 ETH", 1000, "0xd1"), _dep("1 ETH", 1100, "0xd2")],
    )
    univ = _univ([(R1, "0.1 ETH", 4000, "0xw1"), (R1, "1 ETH", 5000, "0xw2")])
    assert tb.multi_denom_leads(two_tx, univ) == set()
    assert tb.multi_denom_leads(two_tx, univ, min_txs=2) == {("1 ETH", R1)}


def test_multi_denom_counts_the_examined_withdrawal_when_the_universe_misses_it():
    run = _run(
        {"1 ETH": {R1: [_w(5000, "0xmissing")]}},
        [_dep("0.1 ETH", 1000, "0xd1"), _dep("0.1 ETH", 1050, "0xd2"), _dep("1 ETH", 1100, "0xd3")],
    )
    univ = _univ([(R1, "0.1 ETH", 4000, "0xw1"), (R1, "0.1 ETH", 4100, "0xw2")])
    assert tb.multi_denom_leads(run, univ) == {("1 ETH", R1)}


def test_multi_denom_never_fires_in_a_token_pool():
    run = _run(
        {"100 DAI": {R1: [_w(5000, "0xw3")]}},
        [_dep("1 ETH", 1000, "0xd1"), _dep("0.1 ETH", 1100, "0xd2"), _dep("0.1 ETH", 1200, "0xd3")],
    )
    univ = _univ([(R1, "1 ETH", 4000, "0xw1"), (R1, "0.1 ETH", 4500, "0xw2")])
    assert tb.multi_denom_leads(run, univ) == set()


# ----------------------------------------------------------------- combined + ratio


def test_run_leads_unions_and_omits_linked_tutela_without_pair_counts():
    run = _run(
        {"1 ETH": {DEP: [_w(500, "0xa")], R1: [_w(500, "0xb")]}},
        [_dep("1 ETH", 100, "0xd1")],
        heuristics={"linked_addresses": [["1 ETH", R1]]},
    )
    ctx = {"unique": lambda p: False, "senders": {}, "withdrawals_of": lambda a: []}
    leads = tb.run_leads(run, ctx)
    assert "linked_tutela" not in leads
    assert leads["any_tutela"] == {("1 ETH", DEP)}
    assert leads["any_beres"] == {("1 ETH", DEP), ("1 ETH", R1)}
    ctx["pair_counts"] = {DEP: {R1: [4, 0]}}
    leads = tb.run_leads(run, ctx)
    assert leads["any_tutela"] == {("1 ETH", DEP), ("1 ETH", R1)}


def test_ratio_row_point_estimate_bootstrap_and_exact_interval():
    # decoy rate 10/1000, target rate 20/1000 -> R = 0.5
    per = [(10, 500, 5, 500), (10, 500, 5, 500), (0, 0, 3, 100)]  # last: no target window
    r = tb.ratio_row(per, seed=1, boot=200)
    assert r["depositors"] == 2
    assert (r["target_leads"], r["decoy_leads"]) == (20, 10)
    assert r["R"] == 0.5
    lo, hi = r["R_exact95"]
    assert lo < 0.5 < hi
    assert r["R_boot95"][0] <= 0.5 <= r["R_boot95"][1]
    assert r["few"] is False
    assert tb.ratio_row(per, seed=1, boot=200) == r  # fixed seed


def test_ratio_row_with_no_target_leads_has_no_R_but_an_exact_interval():
    r = tb.ratio_row([(0, 100, 2, 100)], seed=1, boot=50)
    assert r["R"] is None and r["few"] is True
    assert r["R_exact95"][0] > 0 and r["R_exact95"][1] is None


def test_cp_ratio_ci_matches_the_review4_formula_on_a_known_case():
    # t = d = 5, equal exposure: symmetric around 1 on the log scale
    lo, hi = tb.cp_ratio_ci(5, 100, 5, 100)
    assert lo == pytest.approx(1 / hi, rel=1e-2)
    assert lo < 1 < hi


def test_main_offline_end_to_end_on_a_tiny_sample(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(tb, "TUT", str(tmp_path / "out"))
    uni = tmp_path / "universe.json"
    uni.write_text(
        json.dumps({"blocks": [1, 10**8], "deposits": [], "withdrawals": []}), encoding="utf-8"
    )
    monkeypatch.setattr(tb, "UNIVERSE", str(uni))
    deposits = [_dep("1 ETH", 100, "0xd1")]
    target = _run({"1 ETH": {DEP: [_w(500, "0xa")], R1: [_w(600, "0xb")]}}, deposits)
    decoy = _run({"1 ETH": {R2: [_w(500, "0xc")]}}, deposits)
    for side, run in (("target", target), ("decoy", decoy)):
        (tmp_path / "s" / side).mkdir(parents=True)
        (tmp_path / "s" / side / (DEP + ".json")).write_text(json.dumps(run), encoding="utf-8")
    tb.main(["--dir", str(tmp_path / "s"), "--offline-only"])
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    row = next(iter(summary["sets"].values()))["address_match"]
    assert (row["target_leads"], row["decoy_leads"]) == (1, 0)
    assert (row["target_withdrawals"], row["decoy_withdrawals"]) == (2, 1)
    assert summary["api_calls"] == 0
    assert "linked_tutela" not in next(iter(summary["sets"].values()))
    assert (tmp_path / "out" / "TUTELA.md").exists()
