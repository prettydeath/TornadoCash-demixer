"""Placebo (decoy window) check: offset, run_demix shift, summary, CLI, report, web UI."""

import argparse
import copy
import json
import os
import sys
from collections import Counter

import pytest

from tests.fixtures import demix_result as fx
from tests.test_cli import NETWORKS_CSV
from tests.test_cli import WALLET as CLI_WALLET
from tests.test_demix import RELAYER, _FakeClient, _recipient, _tx, _wlog
from tornado_demix import cli, placebo
from tornado_demix.demix import run_demix
from tornado_demix.placebo import DAY, decoy_offset, run_placebo, summarize
from tornado_demix.report_html import build_html_report


@pytest.fixture(autouse=True)
def rows_from_data(monkeypatch):
    """Hand-built sides carry their leads in ``rows`` instead of full heuristics output."""
    monkeypatch.setattr(placebo, "ranked_candidates", lambda data: data.get("rows", []))


def test_offset_puts_every_decoy_window_a_day_before_the_first_deposit():
    deps = [{"ts": 1_000_000}, {"ts": 1_000_000 + 5 * DAY}]
    assert decoy_offset(deps, 30) == 5 * DAY + 31 * DAY
    assert decoy_offset([{"ts": 42}], 10) == 11 * DAY
    assert decoy_offset([], 30) == 0


def _client(n_deposits=3):
    deposits = [
        _tx(fx.POOL_01_ETH, 0.1, 1_700_000_000 + i * 3600, 10 + i, "0xdep%d" % i)
        for i in range(n_deposits)
    ]
    logs = [_wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15) for i in range(1, 41)]
    return _FakeClient(deposits, logs)


def test_run_demix_shift_moves_deposits_and_windows_back_and_default_is_unchanged():
    base = run_demix(_client(), fx.WALLET, network=fx.network())
    again = run_demix(_client(), fx.WALLET, network=fx.network(), deposit_shift_seconds=0)
    assert base == again
    assert base["params"]["deposit_shift_seconds"] == 0

    shift = 40 * DAY
    moved = run_demix(_client(), fx.WALLET, network=fx.network(), deposit_shift_seconds=shift)
    assert moved["params"]["deposit_shift_seconds"] == shift
    assert [d["ts"] for d in moved["deposits"]] == [d["ts"] - shift for d in base["deposits"]]
    # Only ts moves: block and hash are display-only and stay.
    assert [(d["block"], d["hash"]) for d in moved["deposits"]] == [
        (d["block"], d["hash"]) for d in base["deposits"]
    ]
    (w0,) = base["denoms"]["0.1 ETH"]["windows"]
    (w1,) = moved["denoms"]["0.1 ETH"]["windows"]
    assert w1["first_ts"] == w0["first_ts"] - shift and w1["end_ts"] == w0["end_ts"] - shift


def test_shift_is_recorded_when_there_are_no_deposits():
    data = run_demix(_FakeClient([], []), fx.WALLET, network=fx.network(), deposit_shift_seconds=5)
    assert data["deposits"] == [] and data["params"]["deposit_shift_seconds"] == 5


def _side(rows, withdrawals, spans=(), shift=0, deposits=()):
    return {
        "rows": rows,
        "deposits": list(deposits),
        "denoms": {"1 ETH": {"counts": Counter({"0xa": withdrawals}), "windows": list(spans)}},
        "params": {"deposit_shift_seconds": shift},
    }


def _row(band, addr="0xa", signals=("linked",)):
    return {"band": band, "address": addr, "pool_key": "1 ETH", "signals": list(signals)}


def test_summarize_counts_rates_and_verdicts():
    target = _side([_row("moderate")] * 2 + [_row("weak")] * 5, 1000)
    decoy = _side(
        [_row("moderate", "0xb"), _row("moderate", "0xc"), _row("strong", "0xd"), _row("weak")],
        500,
        spans=[{"first_ts": 1_600_000_000, "end_ts": 1_602_000_000}],
        shift=10 * DAY,
    )
    out = summarize(target, decoy)
    assert out["offset_days"] == 10
    assert out["decoy_window"] == [1_600_000_000, 1_602_000_000]
    assert out["target"] == {"strong": 0, "moderate": 2, "weak": 5}
    assert out["decoy"] == {"strong": 1, "moderate": 2, "weak": 1}
    assert (out["target_withdrawals"], out["decoy_withdrawals"]) == (1000, 500)
    assert out["target_per_1000"]["moderate"] == 2.0 and out["decoy_per_1000"]["moderate"] == 4.0
    assert "indistinguishable from chance" in out["verdicts"]["moderate"]
    assert "more than the decoy" in out["verdicts"]["weak"]
    assert [lead["address"] for lead in out["decoy_leads"]] == ["0xb", "0xc", "0xd"]
    assert "tiny sample" in out["note"] and out["caveats"] == []


def test_summarize_edge_verdicts_and_caveats():
    deps = [{"ts": 1_600_000_000}, {"ts": 1_600_000_000 + 70 * DAY}]
    span = {"first_ts": 1_500_000_000, "end_ts": 1_500_100_000}
    out = summarize(_side([], 10, deposits=deps), _side([], 0, spans=[span]))
    assert "cannot be compared" in out["verdicts"]["strong"]
    assert out["target_per_1000"]["strong"] == 0.0 and out["decoy_per_1000"]["strong"] is None
    assert len(out["caveats"]) == 2  # long span, decoy before Tornado existed
    quiet = summarize(_side([], 10), _side([], 10))
    assert quiet["verdicts"]["weak"] == "No weak leads in either window"


def test_run_placebo_runs_the_decoy_with_the_target_parameters(monkeypatch):
    seen = {}

    def fake(client, wallet, **kw):
        seen.update(kw)
        return {"deposits": [], "denoms": {}, "params": {}}

    monkeypatch.setattr(placebo, "run_demix", fake)
    target = {"deposits": [{"ts": 100}, {"ts": 100 + DAY}]}
    out = run_placebo(
        None, "0xw", target, window_days=7, exit_window_hours=6, gap_hours=12, mode="events"
    )
    assert seen["deposit_shift_seconds"] == DAY + 8 * DAY
    assert seen["window_days"] == 7 and seen["exit_window_hours"] == 6
    assert seen["gap_hours"] == 12 and seen["mode"] == "events"
    assert out["decoy_leads"] == [] and set(out["verdicts"]) == {"strong", "moderate", "weak"}


# CLI and HTML report

_DEPOSITED = {**fx.two_pool_result(), "deposits": [{"ts": 1_700_000_000}]}


def _summary():
    decoy = _side([_row("strong", fx.BOB)], 100, spans=[{"first_ts": 1, "end_ts": 2}], shift=DAY)
    target = _side([_row("moderate", fx.ALICE)], 100)
    return summarize(target, decoy)


def _cli_args(networks_csv, **over):
    base = dict(
        api_csv=None,
        network="ethereum",
        networks_csv=networks_csv,
        wallet=CLI_WALLET,
        out_dir="",
        report="",
        json="",
        window_days=30,
        exit_window=None,
        fee_lo=0.9,
        fee_hi=0.995,
        gap_hours=24,
        mode="transfers",
        placebo=True,
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def stubbed_cli(monkeypatch, tmp_path):
    csv_path = tmp_path / "networks.csv"
    csv_path.write_text(NETWORKS_CSV)
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "k")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **kw: {})
    monkeypatch.setattr(cli, "run_demix", lambda *a, **kw: copy.deepcopy(_DEPOSITED))
    return str(csv_path)


def test_the_placebo_flag_parses_and_defaults_off():
    parser = cli.build_parser()
    assert parser.parse_args(["demix", CLI_WALLET, "--placebo"]).placebo is True
    assert parser.parse_args(["demix", CLI_WALLET]).placebo is False


def test_cmd_demix_placebo_passes_parameters_and_json_and_report_carry_it(
    monkeypatch, stubbed_cli, tmp_path
):
    calls = {}

    def fake_placebo(client, wallet, data, window_days, **kw):
        calls.update(kw, wallet=wallet, window_days=window_days)
        return _summary()

    monkeypatch.setattr(cli, "run_placebo", fake_placebo)
    out_json, out_html = tmp_path / "r.json", tmp_path / "r.html"
    cli.cmd_demix(_cli_args(stubbed_cli, json=str(out_json), report=str(out_html)))
    assert calls["wallet"] == CLI_WALLET and calls["mode"] == "transfers"
    assert calls["fee_lo"] == 0.9 and calls["exit_window_hours"] is None
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["result"]["placebo"]["decoy"]["strong"] == 1
    assert "Placebo check" in out_html.read_text(encoding="utf-8")


def test_cmd_demix_without_the_flag_does_not_run_the_placebo(monkeypatch, stubbed_cli, capsys):
    monkeypatch.setattr(cli, "run_placebo", lambda *a, **kw: pytest.fail("ran"))
    cli.cmd_demix(_cli_args(stubbed_cli, placebo=False))
    assert "[placebo]" not in capsys.readouterr().out


def test_cmd_demix_placebo_prints_a_compact_summary(monkeypatch, stubbed_cli, capsys):
    monkeypatch.setattr(cli, "run_placebo", lambda *a, **kw: _summary())
    cli.cmd_demix(_cli_args(stubbed_cli))
    out = capsys.readouterr().out
    assert "[placebo] decoy window 1 days before the real one" in out
    assert "strong" in out and "tiny sample" in out


def test_html_report_has_a_placebo_section_only_when_present():
    data = fx.two_pool_result()
    assert "Placebo check" not in build_html_report(data, fx.network())
    data["placebo"] = _summary()
    html = build_html_report(data, fx.network())
    assert "Placebo check" in html and "decoy window is a search window" in html
    assert fx.BOB in html and "tiny sample" in html and "Decoy lead" in html


# web UI

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)
import app as webapp  # noqa: E402


def test_web_placebo_view_and_template(monkeypatch):
    data = fx.two_pool_result()
    data["deposits"] = [{"ts": 1000}]
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: copy.deepcopy(data))
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    monkeypatch.setattr(webapp, "run_placebo", lambda *a, **k: _summary())
    blocks, _t, reports = webapp._run_demix(
        object(), [fx.WALLET], fx.network(), 30, "transfers", placebo=True
    )
    view = blocks[0]["placebo"]
    assert [r["band"] for r in view["rows"]] == ["strong", "moderate", "weak"]
    assert view["leads"][0]["url"].endswith(fx.BOB)
    assert "Placebo check" in reports[fx.WALLET]
    with webapp.app.test_request_context():
        html = webapp.render_template(
            "index.html",
            **{**webapp._base_context(), "result": {"kind": "demix", "blocks": blocks}},
        )
        assert "Placebo check" in html and "chance by construction" in html
        form = webapp.render_template("index.html", **webapp._base_context())
        assert 'name="placebo"' in form and "doubles explorer calls" in form

    blocks, _t, _r = webapp._run_demix(object(), [fx.WALLET], fx.network(), 30, "transfers")
    assert blocks[0]["placebo"] is None
