"""The "results may be incomplete" notice in the web UI."""

import pytest

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")

from tests.fixtures import demix_result as fx  # noqa: E402
from tests.test_lookup_failures_reports import (  # noqa: E402
    FAILED,
    NOTICE,
    _demix_data,
    _with_errors,
)
from tests.test_report import _char_info, _multi_corr  # noqa: E402
from tests.test_webui import _TRACE, _post, client, webapp  # noqa: E402, F401


def test_webui_trace_shows_the_notice(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(webapp, "trace_funds", lambda *a, **kw: _with_errors(_TRACE))
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **k: {})
    _response, body = _post(client, analysis="trace", trace_amount="2")
    assert NOTICE in body and "Max rate limit reached" in body


def test_webui_demix_block_shows_the_notice(monkeypatch):
    from tests.test_webui import _QuietClient

    data = _demix_data()
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: data)
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    blocks, _table, _reports = webapp._run_demix(
        _QuietClient(), [fx.WALLET], fx.network(), window_days=30, mode="transfers"
    )
    assert blocks[0]["incomplete"]["summary"] == NOTICE
    assert any(FAILED in line for line in blocks[0]["incomplete"]["lines"])


def test_webui_demix_page_shows_the_notice(client, monkeypatch):  # noqa: F811
    data = _demix_data()
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: data)
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    _response, body = _post(client, analysis="demix", mode="transfers")
    assert NOTICE in body


@pytest.mark.parametrize("analysis", ["multi", "characterize"])
def test_webui_multi_and_characterize_show_the_notice(client, monkeypatch, analysis):  # noqa: F811
    monkeypatch.setattr(webapp, "correlate", lambda *a, **k: _with_errors(_multi_corr()))
    monkeypatch.setattr(webapp, "characterize_address", lambda *a, **k: _with_errors(_char_info()))
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **k: {})
    wallets = fx.WALLET + "\n" + fx.ALICE if analysis == "multi" else fx.BOB
    _response, body = _post(client, analysis=analysis, wallets=wallets)
    assert NOTICE in body
