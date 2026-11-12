"""Each evaluation tool appends its computed tests to the register (TORNADO_DEMIX_REGISTRY)."""

import json
import os
import sys
from collections import Counter

import pytest

from tornado_demix import registry

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)


@pytest.fixture
def reg_path(tmp_path, monkeypatch):
    p = tmp_path / "missing_dir" / "tests.jsonl"  # directory is created by the tool
    monkeypatch.setenv("TORNADO_DEMIX_REGISTRY", str(p))
    return p


def _records(p):
    assert p.exists()
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def _ids(p):
    return [r["test_id"] for r in _records(p)]


def test_holdout_report_records_each_signal(reg_path):
    pytest.importorskip("scipy")
    import holdout_report as hr

    def t(n):
        return {
            "exposure": n,
            "bands": {"moderate": 2},
            "signals": {"linked": 2},
            "combos": {},
            "ctx_shared": None,
        }

    out = hr.analyze([(t(50), t(50))], boot=20, window="30d")
    recs = _records(reg_path)
    assert "holdout.ratio.sig:linked" in _ids(reg_path)
    rec = next(r for r in recs if r["test_id"] == "holdout.ratio.sig:linked")
    assert rec["params"]["window"] == "30d"
    assert rec["result"]["R"] == out["early direct link (linked, <=72h)"]["R"]
    assert rec["script"].endswith("tools/holdout_report.py")
    assert registry.verify_chain(str(reg_path)) == []


def test_decoy_rates_records_each_kind(reg_path, tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    import decoy_rates as dr

    monkeypatch.setattr(dr, "BASE", str(tmp_path / "placebo"))
    monkeypatch.setattr(dr, "cluster_rr", lambda rows: None)
    for kind in ("target", "decoy"):
        d = tmp_path / "placebo" / "s1" / kind
        d.mkdir(parents=True)
        (d / "0xa.json").write_text(json.dumps({"wallet": "0xa", "denoms": {}}), encoding="utf-8")
    dr.analyse("s1")
    ids = _ids(reg_path)
    assert "decoy_rates.moderate+strong" in ids and len(ids) == len(dr.KINDS)
    assert _records(reg_path)[0]["params"]["stratum"] == "full"


def test_placebo_eval_records_each_band(reg_path, monkeypatch):
    import placebo_eval as pe

    pair = {"bands": Counter({"strong": 1}), "families": {}, "exposure": 10}
    monkeypatch.setattr(pe, "summarize", lambda data: dict(pair))
    pe.report({"0xa": {"target": {}, "decoy": {}}}, boot=5)
    assert [i for i in _ids(reg_path) if i.startswith("placebo.fdr.")] == [
        "placebo.fdr.up_to_" + b for b in pe.BANDS
    ]


def test_power_measure_records_summary(reg_path):
    pytest.importorskip("Crypto", reason="power_measure imports the labelling helpers")
    import power_measure as pm

    pm._record_summary({"network": "ethereum", "dir": "h", "windows": 4, "not_detectable": 1})
    assert _ids(reg_path) == ["power.not_detectable_share"]


def test_root_exclusion_eval_records_per_kind(reg_path):
    import root_exclusion_eval as rx

    kind = {"withdrawals_checked": 10, "withdrawals_excluded": 3}
    rx._record_summary(
        {"network": "ethereum", "dir": "h", "kinds": {"target": kind, "decoy": kind}}
    )
    assert _ids(reg_path) == ["root_exclusion.target", "root_exclusion.decoy"]


def test_root_ceiling_records_share(reg_path):
    import root_ceiling as rc

    rc._record_summary({"network": "ethereum", "dir": "h", "history": 5, "withdrawals": 4})
    assert _ids(reg_path) == ["root_ceiling.share"]


def test_tools_still_work_when_register_dir_missing(reg_path):
    assert not reg_path.parent.exists()
    registry.record("x.y", __file__, {}, {})
    assert reg_path.exists()


@pytest.mark.parametrize("breakage", ["unwritable", "corrupt"])
def test_failing_register_does_not_break_tools(tmp_path, monkeypatch, capsys, breakage):
    pytest.importorskip("scipy")
    import holdout_report as hr
    import root_ceiling as rc
    import root_exclusion_eval as rx

    if breakage == "unwritable":
        blocker = tmp_path / "file"
        blocker.write_text("x", encoding="utf-8")
        p = blocker / "sub" / "tests.jsonl"  # parent is a file: makedirs fails
    else:
        p = tmp_path / "bad.jsonl"
        p.write_text("garbage\n", encoding="utf-8")
    monkeypatch.setenv("TORNADO_DEMIX_REGISTRY", str(p))
    rc._record_summary({"network": "ethereum", "dir": "h"})
    rx._record_summary({"kinds": {"target": {}}})
    try:
        import power_measure as pm
    except ImportError:
        pm = None
    if pm:
        pm._record_summary({"network": "ethereum", "dir": "h"})
    t = {"exposure": 5, "bands": {}, "signals": {}, "combos": {}, "ctx_shared": None}
    assert "class strong" in hr.analyze([(t, t)], boot=5)
    assert "warning: test register not updated" in capsys.readouterr().err
