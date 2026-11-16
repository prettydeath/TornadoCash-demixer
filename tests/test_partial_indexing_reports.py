"""A partly indexed explorer answer in the CLI, the HTML reports, the JSON and the web UI."""

import json

import pytest

from tests.fixtures import demix_result as fx
from tests.test_cli import args, networks_csv  # noqa: F401  (pytest fixtures)
from tests.test_lookup_failures_reports import _TRACE, _stub
from tornado_demix import cli, incomplete
from tornado_demix.report import build_html_report, build_trace_report
from tornado_demix.report_json import demix_json
from tornado_demix.txstatus import PARTIAL_INDEXING

HOP = "0x" + "4" * 40
PARTIAL = [
    {
        "action": "txlistinternal",
        "address": HOP,
        "block": None,
        "limit": None,
        "reason": PARTIAL_INDEXING,
    }
]
NOTICE = "1 list still being indexed by the explorer (partial indexing) — results may be incomplete"


def _partial(result):
    return incomplete.attach(dict(result), [], PARTIAL)


def _demix_data():
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    return _partial(data)


def test_the_demix_report_shows_the_partial_indexing_notice():
    html = build_html_report(_demix_data(), fx.network())
    assert NOTICE in html and HOP in html
    assert "partial indexing" in html.split(NOTICE, 1)[1]  # the per-list line as well


def test_the_trace_report_shows_the_partial_indexing_notice():
    html = build_trace_report(_partial(_TRACE), fx.network())
    assert NOTICE in html
    assert "every lookup answered" not in html


def test_the_demix_json_carries_the_partial_indexing_notice():
    doc = json.loads(demix_json(_demix_data()))
    assert doc["incomplete"] == NOTICE
    assert doc["result"]["truncated"] == PARTIAL


def test_cli_demix_prints_the_partial_indexing_notice(monkeypatch, args, capsys):  # noqa: F811
    data = _partial({"wallet": fx.WALLET, "vouchers": [], "denoms": {}})
    _stub(monkeypatch, run_demix=lambda *a, **kw: data)
    cli.cmd_demix(args(wallet=fx.WALLET, out_dir="", report=""))
    out = capsys.readouterr().out
    assert NOTICE.replace("—", "-") in out
    assert f"txlistinternal of {HOP}: partial indexing" in out


def test_the_web_ui_shows_the_partial_indexing_notice_and_counter():
    pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
    from tests.test_lookup_failures_webui import webapp

    view = webapp._incomplete_view(_partial(_TRACE))
    assert view["summary"] == NOTICE and not view["notice"]
    assert any("partial indexing" in line for line in view["lines"])

    class Counting:
        def stats(self):
            return {"calls": 3, "partial_indexing": 2}

    job = {"client": Counting(), "state": "running", "finished": None, "started": 0.0}
    job["error"] = None
    assert webapp._job_status(job)["partial_indexing"] == 2
