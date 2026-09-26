"""The optional Flask UI: input handling and error surfacing.

The web UI had no tests at all, and three of its defects were of the kind tests
catch immediately: a form field parsed outside the error handler (500 with a
traceback), an unvalidated analysis name that fell through to a different
analysis, and a library ``SystemExit`` that its ``except Exception`` could not
catch, which killed the server process on an expired API key.
"""

import os
import sys

import pytest

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)

import app as webapp  # noqa: E402

from tornado_demix.errors import ApiError, ApiKeyError  # noqa: E402
from tornado_demix.report import deposited_by_asset  # noqa: E402

WALLET = "0x" + "a" * 40


class _QuietClient:
    """A provider that answers everything with nothing, instantly.

    The default for these tests. Without it, any request that gets past
    validation constructs a real EtherscanClient and spends the retry budget
    discovering it cannot reach the internet - which is neither what these
    tests are about nor something a test suite should do.
    """

    def __init__(self, *args, **kwargs):
        pass

    def outgoing_txs(self, wallet):
        return []

    def internal_txs(self, wallet):
        return []

    def token_transfers(self, wallet, contract=None, **kwargs):
        return []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", _QuietClient)
    return webapp.app.test_client()


def _post(client, **form):
    form.setdefault("wallets", WALLET)
    response = client.post("/", data=form)
    return response, response.get_data(as_text=True)


# window_days
@pytest.mark.parametrize("raw", ["abc", "1e9", "12.5", "0x1f", "30; DROP"])
def test_an_unparseable_window_is_a_message_not_a_500(client, raw):
    response, body = _post(client, window_days=raw)
    assert response.status_code == 200
    assert "Window must be a whole number of days." in body


@pytest.mark.parametrize("raw", ["", "  ", None])
def test_a_missing_window_is_not_an_error(client, raw):
    form = {"wallets": WALLET}
    if raw is not None:
        form["window_days"] = raw
    response = client.post("/", data=form)
    assert response.status_code == 200
    assert "Window must" not in response.get_data(as_text=True)


def test_non_ascii_digits_parse_the_way_python_parses_them():
    """int('٣') == 3. Harmless: the result is still range-checked."""
    assert webapp._window_days("٣") == (3, None)


@pytest.mark.parametrize("raw", ["0", "-5", "999999", "366"])
def test_a_window_outside_the_supported_range_is_rejected(client, raw):
    response, body = _post(client, window_days=raw)
    assert response.status_code == 200
    assert "Window must be between" in body


def test_a_blank_window_falls_back_to_the_default():
    assert webapp._window_days(None) == (30, None)
    assert webapp._window_days("") == (30, None)
    assert webapp._window_days("45") == (45, None)


# analysis
def test_an_unknown_analysis_is_rejected_not_silently_run_as_cluster(client):
    response, body = _post(client, analysis="../../etc/passwd")
    assert response.status_code == 200
    assert "Unknown analysis" in body


def test_the_real_analyses_are_accepted():
    assert set(webapp.ANALYSES) == {"demix", "multi", "cluster", "characterize"}


def test_characterize_needs_exactly_one_address(client):
    two = WALLET + "\n0x" + "b" * 40
    response, body = _post(client, analysis="characterize", wallets=two)
    assert response.status_code == 200
    assert "exactly one candidate address" in body


def test_characterize_runs_and_renders_a_classification(client):
    # The stub client returns no history, so the candidate has no pool inflows.
    response, body = _post(client, analysis="characterize", wallets=WALLET)
    assert response.status_code == 200
    assert "no pool inflows" in body
    assert "Dominant next hops" in body


# errors from the library
def test_a_rejected_api_key_renders_a_message_and_leaves_the_process_alive(client, monkeypatch):
    """A SystemExit here would escape `except Exception` and kill the server.

    The provider is stubbed rather than reached. An earlier version of this test
    hit the real Etherscan endpoint and relied on it answering "Invalid API
    Key" - which it does, until it starts answering "Too many invalid api key
    attempts" instead, at which point the test failed for a reason that had
    nothing to do with the code.
    """

    class RejectingClient:
        def __init__(self, *args, **kwargs):
            pass

        def outgoing_txs(self, wallet):
            raise ApiKeyError("the data provider rejected the API key: Invalid API Key")

    monkeypatch.setattr(webapp, "EtherscanClient", RejectingClient)
    response, body = _post(client)
    assert response.status_code == 200
    assert "rejected the API key" in body


def test_an_unreachable_provider_is_a_message_not_a_dead_server(client, monkeypatch):
    class DeadProvider:
        def __init__(self, *args, **kwargs):
            pass

        def outgoing_txs(self, wallet):
            raise ApiError("etherscan.io (txlist) gave no usable answer")

    monkeypatch.setattr(webapp, "EtherscanClient", DeadProvider)
    response, body = _post(client)
    assert response.status_code == 200
    assert "no usable answer" in body


# attribution labels in the demix ranked table
def _linked_demix_data():
    """The two-pool fixture with BOB flagged as a linked counterparty."""
    from tests.fixtures import demix_result as fx
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    return fx, data


def test_demix_ranked_view_carries_attribution_when_a_set_is_loaded(monkeypatch):
    fx, data = _linked_demix_data()
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: data)
    monkeypatch.setattr(
        webapp,
        "load_attribution",
        lambda name, *a, **k: {fx.BOB: {"label": "Kraken: Deposit", "category": "exchange"}},
    )
    blocks, _table, _reports = webapp._run_demix(
        _QuietClient(), [fx.WALLET], fx.network(), window_days=30, mode="transfers"
    )
    block = blocks[0]
    assert block["show_attr"] is True
    labelled = [r for r in block["ranked"] if r["address"] == fx.BOB]
    assert labelled and labelled[0]["attribution"] == "Kraken: Deposit (exchange)"


def test_demix_ranked_view_has_no_attribution_without_a_set(monkeypatch):
    fx, data = _linked_demix_data()
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: data)
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    blocks, _table, _reports = webapp._run_demix(
        _QuietClient(), [fx.WALLET], fx.network(), window_days=30, mode="transfers"
    )
    block = blocks[0]
    assert block["show_attr"] is False
    assert all(r["attribution"] == "" for r in block["ranked"])


def test_demix_view_shows_band_evidence_and_parameters(monkeypatch):
    fx, data = _linked_demix_data()
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: data)
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    blocks, _table, _reports = webapp._run_demix(
        _QuietClient(), [fx.WALLET], fx.network(), window_days=30, mode="transfers"
    )
    block = blocks[0]
    assert block["top_band"] == block["ranked"][0]["band"]
    assert all(r["evidence"] for r in block["ranked"])
    labels = [label for label, _value in block["assumptions"]["params"]]
    assert "Voucher gap" in labels and "Count match" in labels

    with webapp.app.test_request_context():
        html = webapp.render_template(
            "index.html",
            **{**webapp._base_context(), "result": {"kind": "demix", "blocks": blocks}},
        )
    assert "Strongest band" in html and "Top confidence" not in html
    assert "Analysis parameters" in html and "Evidence" in html


# wallet parsing and limits
def test_more_wallets_than_the_cap_is_refused(client):
    many = "\n".join("0x%040x" % i for i in range(webapp.MAX_WALLETS + 1))
    response, body = _post(client, wallets=many)
    assert response.status_code == 200
    assert "Too many addresses" in body


def test_no_valid_wallet_is_refused(client):
    response, body = _post(client, wallets="not-an-address\n0x123")
    assert response.status_code == 200
    assert "at least one 0x-prefixed address" in body


def test_multi_needs_two_wallets(client):
    response, body = _post(client, analysis="multi")
    assert response.status_code == 200
    assert "at least two addresses" in body


def test_parse_wallets_dedupes_and_reports_the_rest():
    not_hex = "0x" + "z" * 40
    wallets, bad = webapp._parse_wallets("{w}\n{w}\nnope\n0xshort\n{n}".format(w=WALLET, n=not_hex))
    assert wallets == [WALLET]
    assert bad == ["nope", "0xshort", not_hex]


# deposited totals
def test_deposited_is_reported_per_asset_never_summed_across_them():
    """1 ETH + 100 USDC is not 101 of anything."""
    vouchers = [
        {"asset": "ETH", "count": 1, "denom": 1.0},
        {"asset": "USDC", "count": 1, "denom": 100.0},
        {"asset": "ETH", "count": 2, "denom": 0.1},
    ]
    totals = deposited_by_asset(vouchers)
    assert totals == {"ETH": 1.2, "USDC": 100.0}


def test_deposited_of_nothing_is_empty():
    assert deposited_by_asset([]) == {}


# routes
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/", 200),
        ("/download.csv", 404),
        ("/report/0xdeadbeef.html", 404),
    ],
)
def test_routes_without_a_prior_run(client, path, expected):
    assert client.get(path).status_code == expected


def test_the_server_binds_to_localhost_only():
    source = open(os.path.join("webui", "app.py"), encoding="utf-8").read()
    assert 'host="127.0.0.1"' in source
    assert "debug=False" in source


# multi / cluster now build a downloadable report, served by /report/<key>.html
WALLET_B = "0x" + "b" * 40


def test_multi_run_offers_a_downloadable_report(client):
    response = client.post("/", data={"wallets": WALLET + "\n" + WALLET_B, "analysis": "multi"})
    assert response.status_code == 200
    assert "Download report" in response.get_data(as_text=True)
    report = client.get("/report/multi.html")
    assert report.status_code == 200
    body = report.get_data(as_text=True)
    assert "Tornado.Cash multi-wallet correlation" in body
    assert "Most likely: cross-wallet consolidators" in body


def test_cluster_run_offers_a_downloadable_report(client):
    response = client.post("/", data={"wallets": WALLET, "analysis": "cluster"})
    assert response.status_code == 200
    assert "Download report" in response.get_data(as_text=True)
    report = client.get("/report/cluster.html")
    assert report.status_code == 200
    body = report.get_data(as_text=True)
    assert "Tornado.Cash split-exit clusters" in body
    assert "Most likely reconvergence points" in body


def test_multi_view_shows_consolidator_grades_strongest_first(monkeypatch):
    a, b, weak_addr, strong_addr = ("0x" + c * 40 for c in "abcd")
    corr = {
        "strong": [],
        "profile_matches": {},
        "fingerprints": {},
        "cross_profile": {weak_addr: [a, b], strong_addr: [a, b]},
        "consolidator_grades": {
            weak_addr: {"band": "weak", "artefact_kind": "identical"},
            strong_addr: {"band": "strong", "artefact_kind": None},
        },
    }
    monkeypatch.setattr(webapp, "correlate", lambda *a, **k: corr)
    monkeypatch.setattr(webapp, "build_multi_report", lambda corr, net: "<html></html>")
    from tests.fixtures import demix_result as fx

    view, _table, _reports = webapp._run_multi(_QuietClient(), [a, b], fx.network(), 30, "events")
    assert [c["band"] for c in view["cross"]] == ["strong", "weak"]
    assert view["cross"][1]["artefact"] == "identical"
