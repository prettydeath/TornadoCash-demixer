"""The "results may be incomplete" notice in the CLI, the HTML reports and the JSON."""

import json

from tests.fixtures import demix_result as fx
from tests.test_cli import args, networks_csv  # noqa: F401  (pytest fixtures)
from tests.test_report import _bsc_network, _char_info, _multi_corr
from tornado_demix import cli, incomplete
from tornado_demix.report import (
    build_characterize_report,
    build_html_report,
    build_multi_report,
    build_trace_report,
)
from tornado_demix.report_json import demix_json

FAILED = "0x" + "4" * 40
ERRORS = [incomplete.lookup_failed("deposit address check", "Max rate limit reached", FAILED)]
CUT = [{"action": "tokentx", "address": FAILED, "limit": 1000, "block": None, "reason": "x"}]
NOTICE = "1 lookup failed, 1 list cut off by the explorer — results may be incomplete"

# a trace result shaped like trace_funds() returns it
_TRACE = {
    "start": "0x" + "a" * 40,
    "amount": 2.0,
    "asset": "ETH",
    "edges": [],
    "terminals": [
        {
            "address": FAILED,
            "asset": "ETH",
            "amount": 2.0,
            "hop": 1,
            "reason": "lookup_failed",
            "label": None,
        }
    ],
    "nodes_expanded": 1,
}


def _with_errors(result):
    return incomplete.attach(dict(result), ERRORS, CUT)


def _demix_data():
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    return _with_errors(data)


# ---------------------------------------------------------------- HTML


def test_the_demix_report_shows_the_notice_near_the_top():
    html = build_html_report(_demix_data(), fx.network())
    assert NOTICE in html and FAILED in html and "Max rate limit reached" in html
    assert html.index(NOTICE) < html.index("Deposits<")


def test_a_clean_report_has_no_notice():
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    assert "may be incomplete" not in build_html_report(data, fx.network())


def test_the_multi_characterize_and_trace_reports_show_the_notice():
    assert NOTICE in build_multi_report(_with_errors(_multi_corr()), _bsc_network())
    assert NOTICE in build_characterize_report(_with_errors(_char_info()), _bsc_network())
    html = build_trace_report(_with_errors(_TRACE), fx.network())
    assert NOTICE in html and html.index(NOTICE) < html.index("Where the traced funds stop")


def test_the_trace_report_explains_the_new_end_states():
    trace = dict(
        _TRACE,
        terminals=[
            {"address": FAILED, "asset": "ETH", "amount": 1.0, "hop": 1, "reason": r}
            for r in ("lookup_failed", "below_threshold", "tornado_redeposit")
        ],
    )
    html = build_trace_report(trace, fx.network())
    assert "below_threshold" in html and "tornado_redeposit" in html
    assert "could not be read" in html


# ---------------------------------------------------------------- JSON


def test_the_demix_json_carries_the_errors_and_the_notice():
    doc = json.loads(demix_json(_demix_data()))
    assert doc["result"]["error_count"] == 1 and doc["result"]["errors"] == ERRORS
    assert doc["incomplete"] == NOTICE


# ---------------------------------------------------------------- CLI


def _stub(monkeypatch, **attrs):
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **kw: {})
    for name, value in attrs.items():
        monkeypatch.setattr(cli, name, value)


def test_cli_demix_prints_the_notice_and_the_list(monkeypatch, args, capsys):  # noqa: F811
    data = _with_errors({"wallet": fx.WALLET, "vouchers": [], "denoms": {}})
    _stub(monkeypatch, run_demix=lambda *a, **kw: data)
    cli.cmd_demix(args(wallet=fx.WALLET, out_dir="", report=""))
    out = capsys.readouterr().out
    assert "1 lookup failed, 1 list cut off by the explorer - results may be incomplete" in out
    assert f"deposit address check {FAILED}: Max rate limit reached" in out


def test_cli_trace_prints_the_notice(monkeypatch, args, capsys):  # noqa: F811
    _stub(monkeypatch, trace_funds=lambda *a, **kw: _with_errors(_TRACE))
    cli.cmd_trace(args(address=fx.BOB, amount=2.0, token="", start_block=0, max_hops=4, json=""))
    assert "results may be incomplete" in capsys.readouterr().out


def test_cli_multi_and_characterize_print_the_notice(monkeypatch, args, capsys):  # noqa: F811
    corr = _with_errors(_multi_corr())
    _stub(monkeypatch, correlate=lambda *a, **kw: corr)
    cli.cmd_multi(args(wallets=[fx.WALLET, fx.ALICE], out_dir="", report=""))
    assert "results may be incomplete" in capsys.readouterr().out
    info = _with_errors(_char_info())
    _stub(monkeypatch, characterize_address=lambda *a, **kw: info)
    cli.cmd_characterize(args(address=fx.BOB, report=""))
    assert "results may be incomplete" in capsys.readouterr().out
