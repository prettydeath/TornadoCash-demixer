"""The web UI result page uses the report layout, for every mode, and the form shows
only the fields of the chosen mode with validation messages next to the field."""

import copy
import os
import re
import sys

import pytest

from tests.fixtures import demix_result as fx
from tests.test_report import _avax_cluster_traces, _char_info, _multi_corr
from tests.test_report_layout import START, _trace
from tornado_demix.heuristics import apply_heuristics
from tornado_demix.report_html import SECTION_ORDER

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)
import app as webapp  # noqa: E402

CSRF = "layout-csrf"


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **kw: {})
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: lambda a: None)
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **kw: copy.deepcopy(data))
    monkeypatch.setattr(webapp, "verify_run", lambda client, d: ([], [], []))
    monkeypatch.setattr(webapp, "correlate", lambda *a, **kw: _multi_corr())
    monkeypatch.setattr(webapp, "characterize_address", lambda *a, **kw: _char_info())
    monkeypatch.setattr(
        webapp,
        "run_classify",
        lambda *a, **kw: {
            "address": fx.BOB,
            "kind": "deposit_like",
            "senders": [fx.ALICE],
            "sweep_targets": [START],
            "errors": [],
            "truncated": [],
        },
    )
    monkeypatch.setattr(webapp, "trace_funds", lambda *a, **kw: _trace())

    class Dest:
        def __init__(self, *a, **kw):
            pass

        def network(self, chain):
            return None

    monkeypatch.setattr(webapp, "Destinations", Dest)
    monkeypatch.setattr(webapp, "trace_wallet", lambda *a, **kw: _avax_cluster_traces()[0])
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = CSRF
    return client


FORMS = {
    "demix": {"wallets": fx.WALLET},
    "multi": {"wallets": fx.WALLET + "\n" + fx.ALICE},
    "cluster": {"wallets": fx.WALLET},
    "characterize": {"wallets": fx.BOB},
    "classify": {"wallets": fx.BOB},
    "trace": {"wallets": START, "trace_amount": "10"},
}


def _post(web, analysis, **extra):
    form = {"csrf": CSRF, "analysis": analysis, "network": "ethereum", **FORMS[analysis], **extra}
    response = web.post("/", data=form)
    return response, response.get_data(as_text=True)


@pytest.mark.parametrize("analysis", sorted(FORMS))
def test_every_mode_shows_the_report_layout(web, analysis):
    response, body = _post(web, analysis)
    assert response.status_code == 200
    result = body[body.index('<section class="result"') :]
    # the same body the HTML report has: verdict card, table of contents, sections
    assert 'class="verdict"' in result and 'class="toc"' in result
    assert result.index('class="verdict"') < result.index('class="toc"')
    keys = re.findall(r'data-sec="([a-z]+)"', result)
    assert keys == [k for k in SECTION_ORDER if k in keys]
    # the shared stylesheet and script are inline, the script with the response nonce
    nonce = re.search(r"'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
    assert body.count(f'<script nonce="{nonce}">') == body.count("<script")
    assert "details.sec" in body and "beforeprint" in body


@pytest.mark.parametrize(
    "analysis,html,json_key",
    [
        ("demix", f"/report/{fx.WALLET}.html", fx.WALLET),
        ("multi", "/report/multi.html", "multi"),
        ("cluster", "/report/cluster.html", "cluster"),
        ("characterize", "/report/characterize.html", fx.BOB),
        ("classify", "/report/classify.html", fx.BOB),
        ("trace", "/report/trace.html", START),
    ],
)
def test_every_mode_offers_html_json_and_csv(web, analysis, html, json_key):
    _response, body = _post(web, analysis)
    assert f'href="{html}"' in body
    assert f'href="/result/{json_key}.json"' in body
    assert web.get(html).status_code == 200
    assert web.get(f"/result/{json_key}.json").status_code == 200
    if 'href="/download.csv"' in body:
        assert web.get("/download.csv").status_code == 200


def test_demix_page_matches_the_downloaded_report(web):
    _response, body = _post(web, "demix")
    report = web.get(f"/report/{fx.WALLET}.html").get_data(as_text=True)
    for needle in ('data-count="leads" data-value="1"', f">{fx.BOB}</a>", "Evidence timeline"):
        assert needle in body and needle in report
    assert 'id="w1-leads"' in body  # ids are prefixed per wallet on the page


def test_the_form_shows_only_the_fields_of_the_chosen_mode(web):
    _response, body = _post(web, "trace", trace_amount="")
    form = body[body.index('<form method="post" class="run') : body.index("</form>")]
    # no depositor field for trace: the address field asks for the exit address
    label = re.search(r'<label for="wallets" id="walletsLabel">([^<]*)</label>', form).group(1)
    assert "Depositor" not in label and "Exit address" in label
    for hidden in ("demixFields", "multiFields", "charFields", "windowFields", "exitFields"):
        assert re.search(r'id="%s"[^>]*hidden' % hidden, form), hidden
    assert not re.search(r'id="traceFields"[^>]*hidden', form)
    # every mode is one radio of the same field
    assert form.count('name="analysis"') == len(webapp.ANALYSES)
    assert 'id="mode-trace" value="trace" checked' in form


def test_validation_messages_sit_next_to_their_field(web):
    _response, body = _post(web, "demix", window_days="abc")
    assert 'aria-describedby="window_days-err"' in body
    assert '<div class="ferr" id="window_days-err" role="alert">Window must' in body
    assert '<div class="err" role="alert">' not in body  # not repeated at the top
    _response, body = _post(web, "trace", trace_amount="")
    assert '<div class="ferr" id="trace-err"' in body and "amount to follow" in body
    _response, body = _post(web, "characterize", wallets=fx.BOB + "\n" + fx.ALICE)
    assert 'id="wallets-err"' in body and "exactly one candidate address" in body


def test_job_box_has_a_progress_indicator(web):
    with web.session_transaction() as sess:
        owner = sess["csrf"]
    job = {
        "id": "jobid",
        "owner": owner,
        "state": "running",
        "started": 0,
        "finished": None,
        "echo": {},
        "client": None,
        "error": None,
        "payload": None,
        "store": None,
        "token": None,
    }
    webapp._JOBS["jobid"] = job
    try:
        body = web.get("/?job=jobid").get_data(as_text=True)
    finally:
        webapp._JOBS.pop("jobid", None)
    box = body[body.index('id="jobBox"') :]
    assert 'class="prog"' in box[: box.index("</div>\n")] or 'class="prog"' in box
    assert "/job/" in body


# --- fix round 1 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [{"known_exits": "junk"}, {"window_days": "999"}, {"exit_window": "abc"}],
)
def test_fields_of_another_mode_are_not_validated(web, extra):
    # left over from another mode: hidden in trace, so it must not block the run
    response, body = _post(web, "trace", **extra)
    assert response.status_code == 200
    assert 'class="verdict"' in body and 'class="ferr"' not in body


def test_an_error_of_a_hidden_field_shows_in_the_top_box(web):
    with webapp.app.test_request_context():
        ctx = {
            **webapp._base_context(),
            "csrf": "x",
            "analysis": "trace",
            "error": "Known exits must be 0x addresses: junk",
            "error_field": "known_exits",
        }
        html = webapp.render_template("index.html", **ctx)
    assert '<div class="err" role="alert">Known exits must be 0x addresses: junk</div>' in html


def test_an_error_of_a_shown_field_sits_next_to_it(web):
    _response, body = _post(web, "demix", known_exits="junk")
    assert '<div class="ferr" id="known_exits-err" role="alert">Known exits must' in body
    assert '<div class="err" role="alert">' not in body
    _response, body = _post(web, "trace", trace_amount="")
    field = body[body.index('id="trace_amount"') :].split(">", 1)[0]
    assert 'aria-invalid="true"' in field and 'aria-describedby="trace-err"' in field


def test_theme_button_is_not_fixed_over_the_table_of_contents(web):
    body = web.get("/").get_data(as_text=True)
    css = body[body.index(".themebtn{") :].split("}", 1)[0]
    assert "position:fixed" not in css and "position:absolute" in css
    assert body.index('id="themeToggle"') > body.index('<div class="console">')


def test_hostile_attribution_label_is_escaped_on_the_page(web, monkeypatch):
    hostile = "<img src=x onerror=alert(1)><script>alert(2)</script>"
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **kw: {fx.BOB: {"label": hostile}})
    _response, body = _post(web, "demix")
    assert "<img src=x" not in body and "<script>alert" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
