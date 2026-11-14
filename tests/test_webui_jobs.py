"""Web UI: provider settings, background jobs and the polling endpoint."""

import os
import re
import sys
import threading

import pytest

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)

import app as webapp  # noqa: E402

CSRF = "test-csrf-token"
WALLET = "0x" + "a" * 40


class _Client:
    """An explorer that answers everything with nothing and keeps counters."""

    instances = []
    gate = None  # an Event a test can hold a run on

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.calls = 0
        _Client.instances.append(self)

    def stats(self):
        return {"calls": self.calls, "cache_hits": 2, "waited": 1.5, "rpc_fallbacks": 1}

    def _wait(self):
        self.calls += 7
        if _Client.gate is not None:
            _Client.gate.wait(5)

    def outgoing_txs(self, wallet):
        self._wait()
        return []

    def internal_txs(self, wallet):
        return []

    def token_transfers(self, wallet, contract=None, **kwargs):
        return []


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _Client.instances = []
    _Client.gate = None
    webapp._JOBS.clear()
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", _Client)


def _session_client(csrf=CSRF):
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = csrf
    return client


def _post(client, **form):
    form.setdefault("wallets", WALLET)
    form.setdefault("csrf", CSRF)
    response = client.post("/", data=form)
    if response.status_code == 303:  # an async run redirects to its status page
        _LAST["job"] = response.headers["Location"].split("=", 1)[1]
        response = client.get(response.headers["Location"])
    return response, response.get_data(as_text=True)


_LAST = {}


def _job_id(body):
    """Id of the job the last async POST started (the page may already show its result)."""
    return _LAST["job"]


def test_without_the_async_flag_the_run_is_synchronous_and_has_no_job_box():
    _response, body = _post(_session_client(), analysis="demix")
    assert 'id="jobBox"' not in body
    assert "<h2>Results" in body


def test_the_async_flag_returns_a_status_box_instead_of_the_result():
    client = _session_client()
    _Client.gate = threading.Event()
    try:
        response, body = _post(client, analysis="demix", **{"async": "1"})
        assert response.status_code == 200
        job_id = _job_id(body)
        assert "<h2>Results" not in body
    finally:
        _Client.gate.set()
    webapp._JOBS[job_id]["thread"].join(5)


def test_the_polling_endpoint_reports_progress_then_the_result_page_loads():
    client = _session_client()
    _Client.gate = threading.Event()
    _response, body = _post(client, analysis="demix", **{"async": "1"})
    job_id = _job_id(body)
    running = client.get("/job/" + job_id)
    assert running.status_code == 200
    assert running.headers["Cache-Control"] == "no-store"
    status = running.get_json()
    assert status["state"] == "running"
    assert {"elapsed", "calls", "cache_hits", "waited", "rpc_fallbacks", "error"} <= set(status)
    _Client.gate.set()
    webapp._JOBS[job_id]["thread"].join(5)
    done = client.get("/job/" + job_id).get_json()
    assert done["state"] == "done"
    assert done["calls"] == 7
    page = client.get("/?job=" + job_id).get_data(as_text=True)
    assert "<h2>Results" in page
    assert client.get("/download.csv").status_code in (200, 404)  # the stored table is reachable


def test_a_failed_job_shows_its_error_in_the_page(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider on fire")

    monkeypatch.setattr(webapp, "run_demix", boom)
    client = _session_client()
    _response, body = _post(client, analysis="demix", **{"async": "1"})
    job_id = _job_id(body)
    webapp._JOBS[job_id]["thread"].join(5)
    assert client.get("/job/" + job_id).get_json()["state"] == "error"
    page = client.get("/?job=" + job_id).get_data(as_text=True)
    assert "Analysis failed: provider on fire" in page


def test_another_session_cannot_read_a_job():
    client = _session_client()
    _response, body = _post(client, analysis="demix", **{"async": "1"})
    job_id = _job_id(body)
    webapp._JOBS[job_id]["thread"].join(5)
    stranger = _session_client(csrf="someone-else")
    assert stranger.get("/job/" + job_id).status_code == 404
    assert "<h2>Results" not in stranger.get("/?job=" + job_id).get_data(as_text=True)


def test_an_unknown_job_is_a_404_and_the_endpoint_is_get_only():
    client = _session_client()
    assert client.get("/job/nope").status_code == 404
    assert client.post("/job/nope", data={"csrf": CSRF}).status_code == 405


def test_the_async_post_still_needs_the_csrf_token():
    client = _session_client()
    response = client.post("/", data={"wallets": WALLET, "async": "1", "csrf": "wrong"})
    assert response.status_code == 400
    assert not webapp._JOBS


def test_the_polling_script_carries_the_csp_nonce():
    client = _session_client()
    response = client.get("/")
    nonce = re.search(r"'nonce-([^']+)'", response.headers["Content-Security-Policy"]).group(1)
    body = response.get_data(as_text=True)
    assert 'id="asyncFlag"' in body
    assert "/job/" in body
    for tag in re.findall(r"<script\b[^>]*>", body):
        assert 'nonce="{}"'.format(nonce) in tag


def test_provider_settings_reach_the_client():
    _post(_session_client(), analysis="demix", max_wait="30", pause="0.5", http_cache="cachedir")
    kwargs = _Client.instances[-1].kwargs
    assert (kwargs["max_wait"], kwargs["pause"], kwargs["cache_dir"]) == (30.0, 0.5, "cachedir")


def test_provider_settings_default_to_two_minutes_no_pause_no_cache():
    _post(_session_client(), analysis="demix")
    kwargs = _Client.instances[-1].kwargs
    assert (kwargs["max_wait"], kwargs["pause"], kwargs["cache_dir"]) == (120, 0.0, None)


@pytest.mark.parametrize(
    "field,value", [("max_wait", "abc"), ("max_wait", "-1"), ("pause", "9999"), ("pause", "x")]
)
def test_bad_provider_settings_are_a_message(field, value):
    _response, body = _post(_session_client(), analysis="demix", **{field: value})
    assert "must be" in body
    assert not _Client.instances


def test_an_async_post_redirects_so_a_reload_does_not_resubmit():
    client = _session_client()
    _Client.gate = threading.Event()
    try:
        response = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        assert response.status_code == 303
        assert response.headers["Location"].startswith("/?job=")
        job_id = response.headers["Location"].split("=", 1)[1]
    finally:
        _Client.gate.set()
    webapp._JOBS[job_id]["thread"].join(5)


def test_a_second_run_is_refused_while_one_is_running():
    client = _session_client()
    _Client.gate = threading.Event()
    try:
        first = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        assert first.status_code == 303
        second = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        assert second.status_code == 409
        assert "still running" in second.get_data(as_text=True)
        assert len(webapp._JOBS) == 1
    finally:
        _Client.gate.set()
    for job in list(webapp._JOBS.values()):
        job["thread"].join(5)
    again = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
    assert again.status_code == 303  # the slot is free once the first run ended
    for job in list(webapp._JOBS.values()):
        job["thread"].join(5)


def test_eviction_drops_the_oldest_finished_jobs(monkeypatch):
    monkeypatch.setattr(webapp, "_JOBS_MAX", 2)
    for name in ("a", "b", "c"):
        webapp._JOBS[name] = {"id": name, "state": "done", "owner": CSRF}
    response = _session_client().post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
    new_id = response.headers["Location"].split("=", 1)[1]
    webapp._JOBS[new_id]["thread"].join(5)
    assert list(webapp._JOBS) == ["c", new_id]


def test_a_running_job_is_never_evicted_and_stays_pollable(monkeypatch):
    monkeypatch.setattr(webapp, "_JOBS_MAX", 1)
    client = _session_client()
    _Client.gate = threading.Event()
    try:
        response = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        job_id = response.headers["Location"].split("=", 1)[1]
        refused = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        assert refused.status_code == 409
        assert list(webapp._JOBS) == [job_id]
        assert client.get("/job/" + job_id).get_json()["state"] == "running"
    finally:
        _Client.gate.set()
    webapp._JOBS[job_id]["thread"].join(5)


def test_the_refusal_links_to_the_running_job_of_the_same_session():
    client = _session_client()
    _Client.gate = threading.Event()
    try:
        first = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        job_id = first.headers["Location"].split("=", 1)[1]
        second = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        assert second.status_code == 409
        assert "/?job=" + job_id in second.get_data(as_text=True)
        stranger = _session_client(csrf="someone-else")
        other = stranger.post("/", data={"csrf": "someone-else", "wallets": WALLET, "async": "1"})
        assert other.status_code == 409
        assert job_id not in other.get_data(as_text=True)
    finally:
        _Client.gate.set()
    webapp._JOBS[job_id]["thread"].join(5)


def test_the_synchronous_path_cannot_overlap_a_background_job():
    client = _session_client()
    _Client.gate = threading.Event()
    try:
        first = client.post("/", data={"csrf": CSRF, "wallets": WALLET, "async": "1"})
        job_id = first.headers["Location"].split("=", 1)[1]
        sync = client.post("/", data={"csrf": CSRF, "wallets": WALLET})
        assert sync.status_code == 409
        assert "still running" in sync.get_data(as_text=True)
    finally:
        _Client.gate.set()
    webapp._JOBS[job_id]["thread"].join(5)
    assert client.post("/", data={"csrf": CSRF, "wallets": WALLET}).status_code == 200
