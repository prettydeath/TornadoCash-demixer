"""Fetching and reporting the public label set, with a fake opener (no network)."""

import io
import os
import sys
import urllib.error

import pytest

from tornado_demix import cli
from tornado_demix.attribution import (
    ATTRIBUTION_ENV,
    attribution_status,
    default_attribution_dir,
    fetch_attribution,
    load_attribution,
)
from tornado_demix.errors import RegistryError

HEADER = "address,network,entity,label,category,source,source_url,confidence,last_updated\n"
GOOD = (
    HEADER
    + "0x"
    + "a" * 40
    + ",ethereum,binance,Binance 1,exchange,s,u,high,2026-01-01\n"
    + "0x"
    + "b" * 40
    + ",ethereum,tornado,Router,mixer,s,u,high,2026-01-01\n"
    + "0x"
    + "c" * 40
    + ",ethereum,kraken,Kraken 2,exchange,s,u,high,2026-01-01\n"
).encode()
NETWORKS = ("ethereum", "bsc", "polygon", "avalanche", "gnosis", "optimism", "arbitrum", "base")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _opener(table):
    """Map a network slug (found in the URL) to bytes or an exception."""
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        value = table[url.rsplit("/", 1)[-1][: -len(".csv")]]
        if isinstance(value, Exception):
            raise value
        return _Resp(value)

    opener.calls = calls
    return opener


def test_fetch_writes_file_and_counts(tmp_path):
    result = fetch_attribution(["ethereum"], str(tmp_path), opener=_opener({"ethereum": GOOD}))
    r = result["ethereum"]
    assert r["status"] == "ok"
    assert (r["rows"], r["exchange"]) == (3, 2)
    assert r["path"] == os.path.join(str(tmp_path), "ethereum.csv")
    assert open(r["path"], "rb").read() == GOOD
    assert os.listdir(str(tmp_path)) == ["ethereum.csv"]


def test_url_is_built_from_the_template(tmp_path):
    opener = _opener({"ethereum": GOOD})
    fetch_attribution(["ethereum"], str(tmp_path), opener=opener)
    assert opener.calls == [
        "https://raw.githubusercontent.com/prettydeath/wallet-attribution/main/data/ethereum.csv"
    ]


def test_bad_header_is_rejected_and_no_file_written(tmp_path):
    opener = _opener({"ethereum": b"foo,bar\n1,2\n"})
    r = fetch_attribution(["ethereum"], str(tmp_path), opener=opener)
    assert r["ethereum"]["status"].startswith("failed:")
    assert os.listdir(str(tmp_path)) == []


def test_empty_table_is_rejected(tmp_path):
    opener = _opener({"ethereum": HEADER.encode()})
    r = fetch_attribution(["ethereum"], str(tmp_path), opener=opener)
    assert r["ethereum"]["status"].startswith("failed:")
    assert os.listdir(str(tmp_path)) == []


def test_http_error_for_one_network_does_not_stop_the_others(tmp_path):
    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    opener = _opener({"bsc": err, "ethereum": GOOD})
    r = fetch_attribution(["bsc", "ethereum"], str(tmp_path), opener=opener)
    assert r["bsc"]["status"].startswith("failed:") and "404" in r["bsc"]["status"]
    assert r["ethereum"]["status"] == "ok"
    assert os.listdir(str(tmp_path)) == ["ethereum.csv"]


def test_failure_keeps_an_existing_file_untouched(tmp_path):
    path = tmp_path / "ethereum.csv"
    path.write_bytes(GOOD)
    opener = _opener({"ethereum": urllib.error.URLError("offline")})
    r = fetch_attribution(["ethereum"], str(tmp_path), opener=opener)
    assert r["ethereum"]["status"].startswith("failed:")
    assert path.read_bytes() == GOOD
    assert os.listdir(str(tmp_path)) == ["ethereum.csv"]


def test_write_error_leaves_no_partial_file(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    r = fetch_attribution(["ethereum"], str(tmp_path), opener=_opener({"ethereum": GOOD}))
    assert "disk full" in r["ethereum"]["status"]
    assert os.listdir(str(tmp_path)) == []


@pytest.mark.parametrize("slug", ["nope", "../ethereum", "a/b", "a\\b"])
def test_invalid_network_is_rejected_before_any_download(tmp_path, slug):
    opener = _opener({})
    with pytest.raises(RegistryError):
        fetch_attribution(["ethereum", slug], str(tmp_path), opener=opener)
    assert opener.calls == []


def test_default_is_all_networks(tmp_path):
    opener = _opener({n: GOOD for n in NETWORKS})
    r = fetch_attribution(None, str(tmp_path), opener=opener)
    assert sorted(r) == sorted(NETWORKS) and len(opener.calls) == 8
    assert all(v["status"] == "ok" for v in r.values())


def test_default_dir_follows_env_then_config(monkeypatch, tmp_path):
    monkeypatch.delenv("TORNADO_DEMIX_CONFIG", raising=False)
    monkeypatch.delenv(ATTRIBUTION_ENV, raising=False)
    assert default_attribution_dir() == os.path.join("config", "attribution")
    monkeypatch.setenv(ATTRIBUTION_ENV, str(tmp_path))
    assert default_attribution_dir() == str(tmp_path)


def test_fetched_file_is_found_by_lookups_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv(ATTRIBUTION_ENV, str(tmp_path))
    assert attribution_status(["ethereum"])["ethereum"] == {"path": None, "rows": 0, "exchange": 0}
    fetch_attribution(["ethereum"], opener=_opener({"ethereum": GOOD}))
    assert len(load_attribution("ethereum")) == 3
    st = attribution_status(["ethereum"])["ethereum"]
    assert st["path"] == os.path.join(str(tmp_path), "ethereum.csv")
    assert (st["rows"], st["exchange"]) == (3, 2)


def test_cli_labels_fetch_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", _opener({"ethereum": GOOD}))
    cli.main(["labels", "fetch", "--network", "ethereum", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "not bundled" in out and "licence" in out
    assert "ethereum" in out and "ethereum.csv" in out
    assert (tmp_path / "ethereum.csv").exists()

    cli.main(
        ["labels", "status", "--network", "ethereum", "--network", "bsc", "--dir", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert "not loaded" in out and "ethereum.csv" in out


def test_cli_labels_fetch_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "urllib.request.urlopen", _opener({"ethereum": urllib.error.URLError("offline")})
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["labels", "fetch", "--network", "ethereum", "--dir", str(tmp_path)])
    assert exc.value.code == 1
    assert "failed" in capsys.readouterr().out


def test_cli_unknown_network_is_a_clean_exit(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["labels", "fetch", "--network", "nope", "--dir", str(tmp_path)])
    assert "RegistryError" in str(exc.value)


# web UI
pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)
import app as webapp  # noqa: E402

CSRF = "test-csrf-token"


@pytest.fixture
def web():
    c = webapp.app.test_client()
    with c.session_transaction() as sess:
        sess["csrf"] = CSRF
    return c


def test_labels_route_fetches_and_shows_result_once(web, monkeypatch):
    seen = {}

    def fake(networks=None, **kwargs):
        seen["networks"] = networks
        return {"bsc": {"path": "p", "rows": 5, "exchange": 2, "status": "ok"}}

    monkeypatch.setattr(webapp, "fetch_attribution", fake)
    r = web.post("/labels/fetch", data={"csrf": CSRF, "network": "bsc"})
    assert r.status_code == 302
    assert seen["networks"] == ["bsc"]
    assert "bsc: 5 labels, 2 exchange." in web.get("/").get_data(as_text=True)
    assert "bsc: 5 labels" not in web.get("/").get_data(as_text=True)


def test_labels_route_all_and_failure(web, monkeypatch):
    seen = {}

    def fake(networks=None, **kwargs):
        seen["networks"] = networks
        return {"bsc": {"path": "p", "rows": 0, "exchange": 0, "status": "failed: offline"}}

    monkeypatch.setattr(webapp, "fetch_attribution", fake)
    web.post("/labels/fetch", data={"csrf": CSRF, "network": "all"})
    assert seen["networks"] is None
    assert "bsc: failed: offline" in web.get("/").get_data(as_text=True)


def test_labels_route_requires_csrf(web, monkeypatch):
    monkeypatch.setattr(webapp, "fetch_attribution", lambda *a, **k: pytest.fail("fetched"))
    assert web.post("/labels/fetch", data={"network": "bsc"}).status_code == 400


def test_labels_route_unknown_network_is_a_message(web):
    r = web.post("/labels/fetch", data={"csrf": CSRF, "network": "nope"})
    assert r.status_code == 302
    assert "unknown network" in web.get("/").get_data(as_text=True)


class _Quiet:
    def __init__(self, *args, **kwargs):
        pass

    def outgoing_txs(self, wallet):
        return []

    def internal_txs(self, wallet):
        return []

    def token_transfers(self, wallet, contract=None, **kwargs):
        return []


@pytest.mark.parametrize("loaded", [False, True])
def test_demix_result_shows_notice_only_without_labels(web, monkeypatch, tmp_path, loaded):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", _Quiet)
    monkeypatch.setenv("TORNADO_DEMIX_CONFIG", str(tmp_path))
    monkeypatch.delenv(ATTRIBUTION_ENV, raising=False)
    if loaded:
        (tmp_path / "attribution").mkdir()
        (tmp_path / "attribution" / "ethereum.csv").write_bytes(GOOD)
    r = web.post("/", data={"csrf": CSRF, "wallets": "0x" + "a" * 40})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert ("No attribution set loaded for ethereum" in body) is (not loaded)
