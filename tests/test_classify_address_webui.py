"""classify-address and the characterize deposit check in the web UI."""

import json
import os
import sys

import pytest

from tests.test_classify_address import LAB, Stub, W, X, _lists, eoa

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)
import app as webapp  # noqa: E402

CSRF = "test-csrf-token"


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: Stub(_lists()))
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **kw: dict(LAB))
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: eoa)
    test_client = webapp.app.test_client()
    with test_client.session_transaction() as sess:
        sess["csrf"] = CSRF
    return test_client


def test_web_ui_classifies_one_address_with_report_and_json(web):
    body = web.post(
        "/", data={"csrf": CSRF, "analysis": "classify", "wallets": X, "network": "polygon"}
    ).get_data(as_text=True)
    assert "exchange deposit" in body and "Binance" in body
    assert "/report/classify.html" in body and f"/result/{X}.json" in body
    assert web.get("/report/classify.html").status_code == 200
    data = json.loads(web.get(f"/result/{X}.json").get_data(as_text=True))
    assert data["kind"] == "exchange_deposit"
    assert "kind" in web.get("/download.csv").get_data(as_text=True)


def test_web_ui_classify_needs_exactly_one_address(web):
    body = web.post(
        "/", data={"csrf": CSRF, "analysis": "classify", "wallets": X + "\n" + W}
    ).get_data(as_text=True)
    assert "exactly one address" in body


def test_web_ui_characterize_shows_the_deposit_check(web):
    body = web.post(
        "/",
        data={
            "csrf": CSRF,
            "analysis": "characterize",
            "wallets": X,
            "network": "polygon",
            "deposit_check": "1",
        },
    ).get_data(as_text=True)
    assert "Deposit-address check" in body and "exchange deposit" in body
    assert "not a deposit address" in body


def test_web_ui_characterize_deposit_check_can_be_switched_off(web, monkeypatch):
    seen = {}
    real = webapp.characterize_address

    def spy(*a, **kw):
        seen["deposit_check"] = kw.get("deposit_check")
        return real(*a, **kw)

    monkeypatch.setattr(webapp, "characterize_address", spy)
    body = web.post(
        "/", data={"csrf": CSRF, "analysis": "characterize", "wallets": X, "network": "polygon"}
    ).get_data(as_text=True)
    assert seen["deposit_check"] is False
    assert "Deposit-address check:" not in body
    assert 'id="deposit_check"' in web.get("/").get_data(as_text=True)


def test_classify_never_says_scored(web):
    body = web.post(
        "/", data={"csrf": CSRF, "analysis": "classify", "wallets": X, "network": "polygon"}
    ).get_data(as_text=True)
    page = body.replace("not scored", "")
    assert "scored" not in page[page.index('<section class="result">') :]
    assert "context, not scored" in body
    report = web.get("/report/classify.html").get_data(as_text=True)
    assert "scored" not in report.replace("not scored", "")
