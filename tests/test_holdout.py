"""Hold-out tooling: sample exclusion, hash, network universes, holdout_report math (offline)."""

import json
import os
import random
import sys

import pytest

pytest.importorskip("scipy")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import holdout_report as hr  # noqa: E402
import placebo_eval as pe  # noqa: E402

DAY = 86400
T0 = 1_650_000_000
LATE = T0 + 10_000 * DAY


def _addr(i):
    return "0x" + format(i, "040x")


def _universe(n):
    deposits = [["P", _addr(i), T0 + i * DAY, "0xh%d" % i] for i in range(1, n + 1)]
    withdrawals = [["P", _addr(1000), T0 - 400 * DAY, "0xw"]]
    return {"deposits": deposits, "withdrawals": withdrawals}


def _loadf(root, name, default=None):
    p = root / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


@pytest.fixture
def cache(tmp_path, monkeypatch):
    for name, n in (("universe.json", 30), ("universe_polygon.json", 12)):
        (tmp_path / name).write_text(json.dumps(_universe(n)), encoding="utf-8")
    monkeypatch.setattr(pe, "CACHE", str(tmp_path))
    monkeypatch.setattr(pe, "_load", lambda n, d=None: _loadf(tmp_path, n, d))
    return tmp_path


def test_universe_file_per_network():
    assert pe.universe_file("ethereum") == "universe.json"
    assert pe.universe_file("polygon") == "universe_polygon.json"
    assert pe.universe_file("arbitrum") == "universe_arbitrum.json"


def test_network_universe_selection(cache):
    assert len(pe.eligible_depositors(30, end_ts=LATE)) == 30
    assert len(pe.eligible_depositors(30, network="polygon", end_ts=LATE)) == 12
    with pytest.raises(SystemExit):
        pe.eligible_depositors(30, network="arbitrum")


def test_end_date_override_drops_late_depositors(cache):
    # deposit i is eligible while T0 + i days + 30 days <= end
    assert len(pe.eligible_depositors(30, end_ts=T0 + 40 * DAY)) == 10


def test_exclusion_and_stats(cache):
    ex = {_addr(1), _addr(2).upper().replace("0X", "0x"), _addr(3)}
    sample, stats = pe.draw_sample(10, 11, 30, end_ts=LATE, exclude=ex)
    assert stats["eligible"] == 30 and stats["excluded"] == 3
    assert stats["pool_after_exclusion"] == 27 and stats["sampled"] == 10 == len(sample)
    assert not set(sample) & {_addr(1), _addr(2), _addr(3)}
    assert stats["sample_sha256"] == pe.sample_hash(sample)


def test_hash_is_deterministic_and_order_free(cache):
    a, sa = pe.draw_sample(10, 5, 30, end_ts=LATE)
    b, sb = pe.draw_sample(10, 5, 30, end_ts=LATE)
    assert a == b and sa == sb
    assert pe.sample_hash(a) == pe.sample_hash(list(reversed(a)))
    assert pe.sample_hash(a) != pe.sample_hash(a[:-1])
    assert pe.draw_sample(10, 6, 30, end_ts=LATE)[1]["sample_sha256"] != sa["sample_sha256"]


def test_default_sample_unchanged_without_exclusions(cache):
    old_style = sorted(random.Random(3).sample(pe.eligible_depositors(30, end_ts=LATE), 7))
    assert pe.sample_depositors(7, 3, 30, end_ts=LATE) == old_style


def test_wallets_in_dir_variants(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "decoy").mkdir()
    (tmp_path / "target" / (_addr(1) + ".json")).write_text("{}", encoding="utf-8")
    (tmp_path / "decoy" / (_addr(9) + ".json")).write_text("{}", encoding="utf-8")
    dar = tmp_path / "dar"
    dar.mkdir()
    (dar / (_addr(2) + ".json")).write_text("{}", encoding="utf-8")
    (dar / "notes.json").write_text("{}", encoding="utf-8")
    base = str(tmp_path)
    assert pe.wallets_in_dir(base, ".") == {_addr(1)}  # top-level sample = its target/
    assert pe.wallets_in_dir(base, "dar") == {_addr(2)}  # no target/: files in the dir
    with pytest.raises(SystemExit):
        pe.wallets_in_dir(base, "missing")


def test_wallets_in_file(tmp_path):
    p = tmp_path / "ex.txt"
    p.write_text(f"{_addr(1)}\n\n# comment\n{_addr(2)}\n", encoding="utf-8")
    assert pe.wallets_in_file(str(p)) == {_addr(1), _addr(2)}


def test_dry_run_makes_no_network_call(cache, monkeypatch, capsys):
    monkeypatch.setattr(pe, "OUT", str(cache / "placebo"))
    monkeypatch.setattr(pe, "END_TS", LATE)
    monkeypatch.setattr(pe, "EtherscanClient", lambda *a, **k: pytest.fail("network"))
    stats = pe.main(["--sample", "5", "--seed", "1", "--dry-run", "--network", "polygon"])
    assert stats["eligible"] == 12 and stats["sampled"] == 5 and stats["excluded"] == 0
    assert json.loads(capsys.readouterr().out)["sample_sha256"] == stats["sample_sha256"]


# ---------------------------------------------------------------- holdout_report math


def test_ratio_and_zero_leads():
    rng = random.Random(1)
    r = hr.ratio_stats([(10, 100, 2, 100), (10, 100, 2, 100)], rng, boot=200)
    assert r["target"] == 20 and r["decoy"] == 4
    assert r["R"] == pytest.approx(0.2)
    assert hr.ratio_stats([(0, 100, 3, 100)], rng, boot=50)["R"] is None
    # depositors without withdrawals on a side are dropped
    assert hr.ratio_stats([(5, 0, 1, 10), (5, 10, 1, 10)], rng, boot=50)["depositors"] == 1


def test_ci_type_selection():
    rng = random.Random(1)
    many = hr.ratio_stats([(10, 100, 6, 100)] * 4, rng, boot=300)
    assert many["ci_type"] == "bootstrap" and many["ci_used"] == many["R_boot95"]
    few = hr.ratio_stats([(10, 100, 1, 100)] * 4, rng, boot=300)
    assert few["ci_type"] == "exact" and few["ci_used"] == few["R_exact95"]
    assert few["R_boot95"] is not None  # both are reported


def test_exact_ci_values():
    lo, hi = hr.exact_ratio_ci(10, 100, 10, 100)
    assert lo < 1 < hi
    lo, hi = hr.exact_ratio_ci(20, 200, 0, 200)
    assert lo == 0.0 and 0 < hi < 0.25
    # halving the decoy exposure halves R and both limits
    a = hr.exact_ratio_ci(10, 100, 3, 100)
    b = hr.exact_ratio_ci(10, 100, 3, 200)
    assert b[0] == pytest.approx(a[0] / 2) and b[1] == pytest.approx(a[1] / 2)
    assert hr.exact_ratio_ci(0, 10, 0, 10) is None


def test_bootstrap_is_seeded():
    per = [(i % 4, 50, i % 3, 50) for i in range(1, 30)]
    a = hr.ratio_stats(per, random.Random(7), boot=300)
    b = hr.ratio_stats(per, random.Random(7), boot=300)
    assert a == b


def _t(exposure, bands=None, sigs=None, combos=None, ctx=None):
    return {
        "exposure": exposure,
        "bands": bands or {},
        "signals": sigs or {},
        "combos": combos or {},
        "ctx_shared": ctx,
    }


def test_analyze_metrics_and_combos():
    combo = "direct link + early multi-pool profile"
    pairs = [
        (
            _t(50, {"strong": 1, "moderate": 2}, {"linked": 2, "early_profile": 1}, {combo: 1}),
            _t(50, {"moderate": 1}, {"linked": 1}),
        ),
        (_t(50, {"moderate": 1}, {"linked": 1}), _t(0)),  # dropped: decoy has no withdrawals
    ]
    out = hr.analyze(pairs, boot=50)
    lk = out["early direct link (linked, <=72h)"]
    assert (lk["target"], lk["decoy"], lk["depositors"]) == (2, 1, 1)
    sm = out["class strong+moderate"]
    assert (sm["target"], sm["decoy"]) == (3, 1) and sm["R"] == pytest.approx(1 / 3, abs=1e-3)
    assert out["class strong"]["decoy"] == 0 and out["class strong"]["R"] == 0.0
    assert out["strong_source_combinations"]["target"] == {combo: 1}
    assert "note" in out["shared deposit, unlabelled (context)"]  # no deposit_addresses data


def test_tally_counts_signals_bands_and_context(monkeypatch):
    cands = [
        {"band": "strong", "signals": ["linked", "early_profile"]},
        {"band": "moderate", "signals": ["shared_deposit"]},
        {"band": "weak", "signals": ["count_match"]},
    ]
    monkeypatch.setattr(hr, "ranked_candidates", lambda data: cands)
    data = {
        "wallet": _addr(1),
        "denoms": {"P": {"counts": {_addr(5): 2, _addr(6): 1}}},
        "deposit_addresses": [
            {"address": "0xdead", "senders": [_addr(5), _addr(1)], "evidence": False},
            {"address": "0xbeef", "senders": [_addr(6)], "evidence": True},
        ],
    }
    t = hr.tally(data)
    assert t["exposure"] == 3
    assert t["bands"] == {"strong": 1, "moderate": 1, "weak": 1}
    assert t["signals"]["linked"] == 1 and t["ctx_shared"] == 1
    assert t["combos"] == {"direct link + early multi-pool profile": 1}
    assert hr.metric(t, "strong+moderate") == 2 and hr.metric(t, "sig:shared_deposit") == 1


def test_render_and_load_runs(tmp_path):
    for k in ("target", "decoy"):
        (tmp_path / k).mkdir()
        body = json.dumps({"wallet": _addr(1)})
        (tmp_path / k / (_addr(1) + ".json")).write_text(body, encoding="utf-8")
    (tmp_path / "decoy" / (_addr(2) + ".json")).write_text("{}", encoding="utf-8")
    assert len(hr.load_runs(str(tmp_path))) == 1
    pairs = [(_t(10, {"moderate": 1}, {"linked": 1}), _t(10))]
    win = {**hr.analyze(pairs, 50), "hours": 72, "depositors_with_withdrawals": 1}
    md = hr.render({"depositors": 1, "boot": 50, "seed": 1, "windows": {"72h": win}})
    assert "| class moderate | 1 | 0 | 10 | 10 |" in md and "## 72h" in md
