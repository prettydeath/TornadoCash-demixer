"""The package raises catchable exceptions, never SystemExit.

``SystemExit`` derives from ``BaseException``, so an embedder's
``except Exception`` does not catch it. When the library raised it, the Flask
UI's error handler was skipped and an expired API key killed the web server
process instead of rendering a message. Only :mod:`tornado_demix.cli` may turn
a package error into a process exit.
"""

import os

import pytest

from tornado_demix import cli, config
from tornado_demix.errors import (
    ApiError,
    ApiKeyError,
    ConfigError,
    RegistryError,
    TornadoDemixError,
)
from tornado_demix.etherscan import EtherscanClient
from tornado_demix.networks import get_network


def test_every_package_error_is_catchable_as_exception():
    for cls in (ConfigError, RegistryError, ApiError, ApiKeyError):
        assert issubclass(cls, TornadoDemixError)
        assert issubclass(cls, Exception)
    assert not issubclass(TornadoDemixError, SystemExit)


def test_a_rejected_key_raises_apikeyerror_not_systemexit():
    class Rejecting(EtherscanClient):
        def __init__(self):
            EtherscanClient.__init__(self, "BAD", retries=1)

        class _Resp:
            @staticmethod
            def json():
                return {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}

        def _get(self, *a, **k):
            return self._Resp()

    client = Rejecting()
    client.session.get = client._get
    with pytest.raises(ApiKeyError):
        client.call({"module": "account", "action": "txlist", "address": "0xa"})


def test_an_exhausted_retry_loop_raises_rather_than_returning_empty():
    """ "The provider never answered" must not read as "this address is clean"."""

    class Flaky(EtherscanClient):
        def __init__(self):
            EtherscanClient.__init__(self, "KEY", retries=2)
            self.attempts = 0

        def _get(self, *a, **k):
            self.attempts += 1
            raise OSError("connection reset")

    client = Flaky()
    client.session.get = client._get
    with pytest.raises(ApiError) as exc:
        client.call({"module": "account", "action": "txlist", "address": "0xa"})
    assert client.attempts == 2
    assert "connection reset" in str(exc.value)


def test_a_definite_empty_answer_is_still_an_empty_list():
    class Empty(EtherscanClient):
        class _Resp:
            @staticmethod
            def json():
                return {"status": "0", "message": "No transactions found", "result": []}

        def _get(self, *a, **k):
            return self._Resp()

    client = Empty("KEY", retries=1)
    client.session.get = client._get
    assert client.call({"module": "account", "action": "txlist"}) == []


def test_a_rate_limit_that_never_clears_raises_instead_of_reporting_no_deposits():
    class Limited(EtherscanClient):
        class _Resp:
            @staticmethod
            def json():
                return {"status": "0", "message": "NOTOK", "result": "Max daily rate limit reached"}

        def _get(self, *a, **k):
            return self._Resp()

    client = Limited("KEY", retries=1)
    client.session.get = client._get
    client.pause = 0
    with pytest.raises(ApiError) as exc:
        client.call({"module": "account", "action": "txlist"})
    assert "rate limit" in str(exc.value).lower()


def test_unknown_network_raises_registryerror():
    with pytest.raises(RegistryError):
        get_network("narnia")


def test_missing_api_key_raises_configerror(tmp_path, monkeypatch):
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path))
    with pytest.raises(ConfigError):
        config.load_api_key()


def test_no_library_module_raises_systemexit():
    """cli.py is the only place allowed to end the process."""
    offenders = []
    for name in sorted(os.listdir("tornado_demix")):
        if not name.endswith(".py") or name == "cli.py":
            continue
        full = os.path.join("tornado_demix", name)
        for lineno, line in enumerate(open(full, encoding="utf-8"), 1):
            if "raise SystemExit" in line:
                offenders.append("{}:{}".format(full, lineno))
    assert offenders == []


def test_cli_turns_a_package_error_into_a_clean_non_zero_exit(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "KEY")
    with pytest.raises(SystemExit) as exc:
        cli.main(["demix", "0x" + "a" * 40, "--network", "narnia"])
    assert exc.value.code != 0
    assert "RegistryError" in str(exc.value.code)


# How a provider says "your key is the problem"
# Etherscan answers a few bad-key requests with "Invalid API Key" and then
# switches to a lockout message. Matching only the first meant the second fell
# through to the unknown-soft-error path: every retry spent on a request that
# could never succeed, and finally an ApiError - the wrong diagnosis, several
# seconds late, for the commonest configuration mistake there is.
@pytest.mark.parametrize(
    "message",
    [
        "Invalid API Key",
        "Missing/Invalid API Key",
        "Too many invalid api key attempts, please try again later",
        "API Key not valid",
        "Missing API Key",
    ],
)
def test_every_shape_of_key_rejection_raises_apikeyerror(message):
    from tornado_demix.etherscan import EtherscanClient

    class Rejecting(EtherscanClient):
        class _Resp:
            def __init__(self, msg):
                self._msg = msg

            def json(self):
                return {"status": "0", "message": "NOTOK", "result": self._msg}

        def _get(self, *a, **k):
            return self._Resp(message)

    client = Rejecting("BAD", retries=3)
    client.session.get = client._get
    with pytest.raises(ApiKeyError):
        client.call({"module": "account", "action": "txlist"})


def test_a_key_rejection_does_not_burn_the_retry_budget():
    """A bad key cannot become a good one by asking again."""
    from tornado_demix.etherscan import EtherscanClient

    class Rejecting(EtherscanClient):
        def __init__(self):
            EtherscanClient.__init__(self, "BAD", retries=5)
            self.attempts = 0

        class _Resp:
            @staticmethod
            def json():
                return {
                    "status": "0",
                    "message": "NOTOK",
                    "result": "Too many invalid api key attempts",
                }

        def _get(self, *a, **k):
            self.attempts += 1
            return self._Resp()

    client = Rejecting()
    client.session.get = client._get
    with pytest.raises(ApiKeyError):
        client.call({"module": "account", "action": "txlist"})
    assert client.attempts == 1


@pytest.mark.parametrize(
    "message",
    [
        "No records found, missing data",
        "No transactions found",
    ],
)
def test_a_benign_message_mentioning_missing_is_not_a_key_error(message):
    """'missing' as a bare substring is not an authentication failure."""
    from tornado_demix.etherscan import EtherscanClient

    class Empty(EtherscanClient):
        class _Resp:
            def __init__(self, msg):
                self._msg = msg

            def json(self):
                return {"status": "0", "message": self._msg, "result": []}

        def _get(self, *a, **k):
            return self._Resp(message)

    client = Empty("KEY", retries=1)
    client.session.get = client._get
    assert client.call({"module": "account", "action": "txlist"}) == []
