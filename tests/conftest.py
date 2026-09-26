"""Shared test fixtures, and a hard block on outbound network access.

One of these tests reached the real Etherscan API without anyone intending it
to. It passed most of the time, took several seconds, and then started failing
intermittently once the provider began answering repeated bad-key requests with
a lockout message instead of a plain rejection. A unit suite that depends on a
third party is not a unit suite: it is slow, it is flaky in a way that looks
like a code defect, and it hammers someone else's service on every run.

Sockets are therefore blocked for the whole suite. A test that needs the
network must say so with ``@pytest.mark.live``, which is already the marker the
on-chain registry check uses and is deselected by default (see pyproject.toml).
"""

import socket

import pytest

_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection


class NetworkAccessDenied(RuntimeError):
    """A test tried to open a socket without being marked ``live``."""


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Fail loudly on any socket a non-``live`` test tries to open."""
    if request.node.get_closest_marker("live"):
        return

    def denied(*args, **kwargs):
        raise NetworkAccessDenied(
            "{} tried to open a network connection. Stub the client, or mark "
            "the test @pytest.mark.live if it genuinely needs the chain.".format(
                request.node.nodeid
            )
        )

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture(autouse=True)
def _no_backoff_sleeps(request, monkeypatch):
    """Run the retry/backoff paths without actually waiting.

    ``EtherscanClient.call`` sleeps between attempts, and ``cluster`` pauses
    between forward lookups. Those delays are correct in production and pure
    dead time in a test: exercising a five-attempt retry loop costs 22 seconds
    of real sleeping, which is most of the suite's runtime and none of its
    value. The timing is not what any of these tests assert.
    """
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
