"""Web UI hardening: CSRF on every POST, response headers, Host allowlist, address input."""

import os
import re
import sys

import pytest

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)

import app as webapp  # noqa: E402

CSRF = "test-csrf-token"
POST_ROUTES = ["/labels/fetch", "/labels/blockscout"]


@pytest.fixture
def client():
    return webapp.app.test_client()


@pytest.fixture
def session_client():
    test_client = webapp.app.test_client()
    with test_client.session_transaction() as sess:
        sess["csrf"] = CSRF
    return test_client


@pytest.mark.parametrize("route", POST_ROUTES)
def test_a_cross_site_post_without_cookie_or_token_is_refused(client, route):
    assert client.post(route, data={}).status_code == 403


@pytest.mark.parametrize("route", POST_ROUTES)
def test_an_empty_token_does_not_match_an_empty_session_token(client, route):
    with client.session_transaction() as sess:
        sess["csrf"] = ""
    assert client.post(route, data={"csrf": ""}).status_code == 403


@pytest.mark.parametrize("route", POST_ROUTES)
def test_a_missing_form_token_is_refused_even_with_a_session(session_client, route):
    assert session_client.post(route, data={}).status_code == 403
    assert session_client.post(route, data={"csrf": "wrong"}).status_code == 403


def test_the_main_form_refuses_a_post_without_cookie_or_token(client):
    response = client.post("/", data={"wallets": "0x" + "1" * 40})
    assert response.status_code == 400


def test_the_main_form_refuses_an_empty_token_with_an_empty_session_token(client):
    with client.session_transaction() as sess:
        sess["csrf"] = ""
    assert client.post("/", data={"csrf": "", "wallets": "0x" + "1" * 40}).status_code == 400


# security headers
@pytest.mark.parametrize("path", ["/", "/no/such/page", "/download.csv", "/report/nobody.html"])
def test_every_response_carries_the_security_headers(client, path):
    response = client.get(path)
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_the_csp_allows_scripts_only_by_nonce(client):
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "'unsafe-inline'" not in script_src
    nonce = re.search(r"'nonce-([^']+)'", script_src).group(1)
    body = response.get_data(as_text=True)
    assert 'nonce="{}"'.format(nonce) in body
    # every script tag of the page is the nonced one
    tags = re.findall(r"<script\b[^>]*>", body)
    assert tags
    for tag in tags:
        assert 'nonce="{}"'.format(nonce) in tag


def test_the_nonce_is_fresh_for_every_response(client):
    pattern = r"'nonce-([^']+)'"
    first = re.search(pattern, client.get("/").headers["Content-Security-Policy"]).group(1)
    second = re.search(pattern, client.get("/").headers["Content-Security-Policy"]).group(1)
    assert first != second and len(first) >= 16


def test_the_nonce_helper_is_stable_within_one_request():
    with webapp.app.test_request_context("/"):
        webapp.app.preprocess_request()
        nonce = webapp.csp_nonce()
        assert nonce and nonce == webapp.csp_nonce()


# Host allowlist
@pytest.mark.parametrize("host", ["127.0.0.1:5000", "localhost:5000", "localhost", "127.0.0.1"])
def test_loopback_hosts_are_served(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:5000", "127.0.0.1.evil.example", "10.0.0.5:5000"]
)
def test_other_hosts_are_refused_against_dns_rebinding(client, host):
    response = client.get("/", headers={"Host": host})
    assert response.status_code == 400
    assert "Content-Security-Policy" in response.headers


# placeholder
def test_the_form_placeholder_is_synthetic(client):
    body = client.get("/").get_data(as_text=True)
    match = re.search(r'id="wallets"[^>]*placeholder="([^"]*)"', body)
    assert match
    addresses = re.findall(r"0x[0-9a-fA-F]{40}", match.group(1))
    assert addresses
    for address in addresses:
        assert address.startswith("0x" + "0" * 30)


# EIP-55 in the form
CHECKSUMMED = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def _has_keccak():
    try:
        import Crypto.Hash.keccak  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _has_keccak(), reason="needs the labels extra (pycryptodome)")
def test_a_valid_checksum_is_accepted_and_lowercased():
    wallets, bad = webapp._parse_wallets(CHECKSUMMED)
    assert wallets == [CHECKSUMMED.lower()] and bad == []


@pytest.mark.skipif(not _has_keccak(), reason="needs the labels extra (pycryptodome)")
def test_a_wrong_checksum_is_refused_not_silently_lowercased():
    wrong = CHECKSUMMED[:-1] + "D"
    wallets, bad = webapp._parse_wallets(wrong)
    assert wallets == [] and len(bad) == 1


def test_a_wrong_checksum_is_refused_and_a_right_one_accepted_without_pycryptodome():
    wallets, bad = webapp._parse_wallets(CHECKSUMMED)
    assert wallets == [CHECKSUMMED.lower()] and not bad
    wrong = CHECKSUMMED[:-1] + ("e" if CHECKSUMMED[-1] != "e" else "F")
    wallets, bad = webapp._parse_wallets(wrong)
    assert wallets == [] and bad
    assert webapp._parse_wallets(CHECKSUMMED.lower()) == ([CHECKSUMMED.lower()], [])


def test_the_form_tells_the_user_why_a_mixed_case_address_failed(session_client):
    wrong = CHECKSUMMED[:-1] + ("e" if CHECKSUMMED[-1] != "e" else "F")
    response = session_client.post("/", data={"csrf": CSRF, "wallets": wrong})
    assert "lowercase" in response.get_data(as_text=True)
