"""The group view in every output: CLI, HTML report, JSON export and web UI."""

import json
import os
import sys

import pytest

from tests.test_multi import ALICE, BOB, EXIT_A, POOL_1, T0, network
from tests.test_multi_group import DAY, _bg, _correlate, _split_deposits, _split_withdrawals
from tornado_demix import cli
from tornado_demix.honest import EXACT_K_TITLE, GROUP_VIEW_CAVEAT, GROUP_VIEW_TITLE
from tornado_demix.report_html import build_multi_report
from tornado_demix.report_json import multi_json


@pytest.fixture(scope="module")
def corr():
    # some activity in the two base windows before the group window, so the tail is defined
    base = _bg("e5", 3, T0 - 10 * DAY) + _bg("e6", 3, T0 - 40 * DAY)
    withdrawals = {POOL_1: _split_withdrawals()[POOL_1] + base}
    return _correlate(_split_deposits(), withdrawals, group=True, exact_k=True, exact_k_windows=2)


def test_the_html_report_has_the_group_view_with_its_caveat(corr):
    html = build_multi_report(corr, network())
    assert GROUP_VIEW_TITLE in html
    assert "Conditional on your assertion" in html
    assert EXIT_A in html[html.index(GROUP_VIEW_TITLE) :]
    assert EXACT_K_TITLE in html
    # the caveat comes before the group's candidate table
    section = html[html.index(GROUP_VIEW_TITLE) :]
    assert section.index("Conditional on your assertion") < section.index(EXIT_A)


def test_the_html_report_has_no_group_section_without_group(corr):
    plain = dict(corr)
    plain.pop("group")
    assert GROUP_VIEW_TITLE not in build_multi_report(plain, network())


def test_the_multi_json_carries_the_group_view(corr):
    doc = json.loads(multi_json(corr))
    assert doc["group"]["caveat"] == GROUP_VIEW_CAVEAT
    assert doc["group"]["candidates"][0]["address"] == EXIT_A
    assert doc["group"]["exact_k"][0]["k"] == 5
    assert doc["group"]["exact_k"][0]["context"] is True
    assert set(doc["per_wallet"]) == {ALICE, BOB}
    assert doc["wallets"] == sorted([ALICE, BOB])


def test_the_multi_json_without_group_has_null_group(corr):
    plain = dict(corr)
    plain.pop("group")
    assert json.loads(multi_json(plain))["group"] is None


def test_the_multi_json_tolerates_a_sparse_corr():
    doc = json.loads(multi_json({"strong": [], "profile_matches": {}, "fingerprints": {}}))
    assert doc["group"] is None and doc["per_wallet"] == {}


# ---- CLI --------------------------------------------------------------------------


def test_the_parser_has_the_group_flags():
    ns = cli.build_parser().parse_args(
        ["multi", ALICE, BOB, "--group", "--exact-k", "--exact-k-windows", "3", "--json", "x.json"]
    )
    assert ns.group and ns.exact_k and ns.exact_k_windows == 3 and ns.json == "x.json"
    ns = cli.build_parser().parse_args(["multi", ALICE, BOB])
    assert not ns.group and not ns.exact_k and ns.exact_k_windows is None and ns.json == ""


def test_exact_k_without_group_is_refused():
    with pytest.raises(SystemExit):
        cli.main(["multi", ALICE, BOB, "--exact-k"])


def test_cmd_multi_prints_the_group_view_and_writes_json(monkeypatch, capsys, tmp_path, corr):
    import argparse

    seen = {}

    def fake_correlate(*a, **kw):
        seen.update(kw)
        return corr

    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "correlate", fake_correlate)
    monkeypatch.setattr(cli, "_network", lambda args: network())
    out_json = str(tmp_path / "multi.json")
    args = argparse.Namespace(
        api_csv=None,
        network="testnet",
        networks_csv=None,
        wallets=[ALICE, BOB],
        wallets_csv="",
        out_dir="",
        report="",
        cache_dir=".cache",
        window_days=30,
        exit_window=None,
        fee_lo=0.90,
        fee_hi=0.995,
        gap_hours=24,
        mode="events",
        group=True,
        exact_k=True,
        exact_k_windows=2,
        json=out_json,
    )
    cli.cmd_multi(args)
    out = capsys.readouterr().out
    assert seen["group"] is True and seen["exact_k"] is True and seen["exact_k_windows"] == 2
    assert GROUP_VIEW_TITLE in out
    assert "Conditional on your assertion" in out
    assert EXIT_A in out[out.index(GROUP_VIEW_TITLE) :]
    assert "exactly 5" in out and "base mean" in out
    assert json.load(open(out_json, encoding="utf-8"))["group"]["candidates"]


# ---- web UI -----------------------------------------------------------------------


@pytest.fixture
def webapp():
    pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "webui"))
    import app

    return app


def test_the_web_multi_view_has_the_group_section(webapp, monkeypatch, corr):
    seen = {}

    def fake_correlate(*a, **kw):
        seen.update(kw)
        return corr

    monkeypatch.setattr(webapp, "correlate", fake_correlate)
    view, _table, reports = webapp._run_multi(
        None, [ALICE, BOB], network(), 30, "events", group=True, exact_k=True
    )
    assert seen["group"] is True and seen["exact_k"] is True
    assert view["group"]["caveat"] == GROUP_VIEW_CAVEAT
    assert view["group"]["candidates"][0]["address"] == EXIT_A
    assert view["group"]["exact_k"][0]["k"] == 5
    assert "multi.json" in reports
    assert json.loads(reports["multi.json"])["group"]["candidates"]


def test_the_web_multi_view_has_no_group_without_the_checkbox(webapp, monkeypatch, corr):
    plain = dict(corr)
    plain.pop("group")
    monkeypatch.setattr(webapp, "correlate", lambda *a, **kw: plain)
    view, _table, _reports = webapp._run_multi(None, [ALICE, BOB], network(), 30, "events")
    assert view["group"] is None


def test_the_form_has_the_group_checkboxes_and_renders_the_section(webapp, monkeypatch, corr):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    seen = {}

    def fake_correlate(*a, **kw):
        seen.update(kw)
        return corr

    monkeypatch.setattr(webapp, "correlate", fake_correlate)
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: object())
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = "tok"
    body = client.get("/").get_data(as_text=True)
    assert 'id="group"' in body and 'id="exact_k"' in body
    assert body.index('id="multiFields"') < body.index('id="group"')
    response = client.post(
        "/",
        data={
            "csrf": "tok",
            "wallets": ALICE + "\n" + BOB,
            "analysis": "multi",
            "group": "1",
            "exact_k": "1",
        },
    )
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert seen["group"] is True and seen["exact_k"] is True
    assert GROUP_VIEW_TITLE in page and "Conditional on your assertion" in page
    assert EXACT_K_TITLE in page
    assert 'href="/result/multi.json"' in page
    assert client.get("/result/multi.json").status_code == 200


def test_exact_k_without_group_is_ignored_by_the_web_form(webapp, monkeypatch, corr):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    seen = {}

    def fake_correlate(*a, **kw):
        seen.update(kw)
        plain = dict(corr)
        plain.pop("group")
        return plain

    monkeypatch.setattr(webapp, "correlate", fake_correlate)
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: object())
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = "tok"
    client.post(
        "/",
        data={"csrf": "tok", "wallets": ALICE + "\n" + BOB, "analysis": "multi", "exact_k": "1"},
    )
    assert seen["group"] is False and seen["exact_k"] is False


# ---- long-window notice and the smoothed tail in every output -----------------------


@pytest.fixture(scope="module")
def long_corr():
    deposits = {ALICE: [(POOL_1, T0), (POOL_1, T0 + 60)], BOB: [(POOL_1, T0 + 100 * DAY)]}
    return _correlate(deposits, _split_withdrawals(), group=True, exact_k=True)


def test_the_long_window_notice_reaches_html_json_and_web(webapp, monkeypatch, long_corr):
    (note,) = long_corr["group"]["notices"]
    assert "130 days" in note
    html = build_multi_report(long_corr, network())
    assert "Long window." in html and "130 days" in html
    assert json.loads(multi_json(long_corr))["group"]["notices"] == [note]
    monkeypatch.setattr(webapp, "correlate", lambda *a, **kw: long_corr)
    view, _t, _r = webapp._run_multi(None, [ALICE, BOB], network(), 30, "events", group=True)
    assert view["group"]["notices"] == [note]


def test_the_cli_prints_the_long_window_notice_and_the_smoothed_tail(capsys, long_corr, corr):
    cli._print_group(long_corr["group"])
    out = capsys.readouterr().out
    assert "130 days" in out and "dropped by the 365-day base cap" in out
    cli._print_group(corr["group"])
    out = capsys.readouterr().out
    assert "(smoothed, " in out and "of n=" in out


def test_the_html_and_web_show_the_smoothed_tail_with_n(webapp, monkeypatch, corr):
    html = build_multi_report(corr, network())
    assert "(smoothed, " in html and "of n=" in html
    monkeypatch.setattr(webapp, "correlate", lambda *a, **kw: corr)
    view, _t, _r = webapp._run_multi(
        None, [ALICE, BOB], network(), 30, "events", group=True, exact_k=True
    )
    assert "(smoothed, " in view["group"]["exact_k"][0]["tail_text"]
