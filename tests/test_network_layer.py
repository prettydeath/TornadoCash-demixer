"""Network layer: error classes, backoff budget, response cache, progress, RPC log fallback."""

import json
import os
import time

import pytest

from tornado_demix import etherscan as es
from tornado_demix.errors import ApiError, ApiKeyError, PermanentApiError
from tornado_demix.etherscan import EtherscanClient

ADDR = "0x" + "1" * 40
TOPIC0 = "0x" + "a" * 64
TOPIC1 = "0x" + "b" * 64


class _Resp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeTransport:
    """Replaces ``session.get``: serves scripted responses in order, records queries.

    The last response repeats once the script runs out.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def __call__(self, url, params=None, timeout=None):
        self.queries.append(dict(params))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _client(responses, **kwargs):
    kwargs.setdefault("retries", 6)
    client = EtherscanClient("SECRET-KEY", **kwargs)
    transport = FakeTransport(responses)
    client.session.get = transport
    return client, transport


def _ok(rows):
    return _Resp({"status": "1", "message": "OK", "result": rows})


def _err(text, message="NOTOK"):
    return _Resp({"status": "0", "message": message, "result": text})


QUERY = {"module": "account", "action": "txlist", "address": ADDR}


# --- permanent errors fail fast ---------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Free API access is not supported for this chain. Please upgrade your api plan",
        "Error! Invalid address format",
    ],
)
def test_a_permanent_error_is_one_call_and_a_clear_message(text):
    client, transport = _client([_err(text)])
    with pytest.raises(PermanentApiError) as exc:
        client.call(dict(QUERY))
    assert len(transport.queries) == 1
    assert "SECRET-KEY" not in str(exc.value)
    assert isinstance(exc.value, ApiError)


def test_a_rejected_key_is_still_one_call():
    client, transport = _client([_err("Invalid API Key")])
    with pytest.raises(ApiKeyError):
        client.call(dict(QUERY))
    assert len(transport.queries) == 1


def test_a_chain_not_on_the_plan_still_switches_to_the_keyless_fallback():
    client, transport = _client(
        [_err("Free API access is not supported for this chain"), _ok([])],
        fallback_url="https://fallback.example/api",
    )
    assert client.call(dict(QUERY)) == []
    assert client.base_url == "https://fallback.example/api"
    assert len(transport.queries) == 2


# --- transient errors: backoff with jitter, bounded wait -----------------------------------


def test_a_transient_error_then_success_returns_the_rows(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    rows = [{"hash": "0x1", "blockNumber": "1"}]
    client, transport = _client(
        [_err("Max rate limit reached"), _Resp({}, status_code=503), _ok(rows)]
    )
    assert client.call(dict(QUERY)) == rows
    assert len(transport.queries) == 3
    assert len(sleeps) == 2


def test_too_many_requests_and_http_429_are_transient(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    client, transport = _client(
        [_err("Too many requests"), _Resp({"status": "0", "result": ""}, 429), _ok([])]
    )
    assert client.call(dict(QUERY)) == []
    assert len(transport.queries) == 3


def test_the_backoff_grows_and_the_total_wait_stays_within_max_wait(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    client, transport = _client([_err("Max rate limit reached")], retries=50, max_wait=20)
    with pytest.raises(ApiError):
        client.call(dict(QUERY))
    assert sum(sleeps) <= 20 + 1e-6
    assert sum(sleeps) >= 10  # the budget is actually used, not abandoned after 5 tries
    assert max(sleeps) > 2 * min(sleeps)  # exponential, not constant
    assert len(transport.queries) < 50  # the budget ended it before the attempt cap


def test_the_default_wait_budget_is_about_two_minutes():
    assert EtherscanClient("k").max_wait == 120


def test_the_pause_is_applied_before_every_call(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    client, _ = _client([_ok([])], pause=0.5)
    client.call(dict(QUERY))
    client.call(dict(QUERY))
    assert sleeps == [0.5, 0.5]


# --- response cache --------------------------------------------------------------------------


def _head_client(tmp_path, responses, head=1000, **kwargs):
    client, transport = _client(responses, cache_dir=str(tmp_path), **kwargs)
    client._head = (head, time.time())  # what current_block() answers
    return client, transport


def test_an_old_range_is_served_from_the_cache_the_second_time(tmp_path):
    rows = [{"hash": "0x1", "blockNumber": "5"}]
    client, transport = _head_client(tmp_path, [_ok(rows)])
    query = {**QUERY, "startblock": 0, "endblock": 900, "page": 1, "offset": 1000}
    assert client.call(dict(query)) == rows
    assert client.call(dict(query)) == rows
    assert len(transport.queries) == 1
    assert client.cache_hits == 1


def test_the_cache_survives_a_new_client(tmp_path):
    rows = [{"hash": "0x1", "blockNumber": "5"}]
    query = {**QUERY, "startblock": 0, "endblock": 900}
    first, _ = _head_client(tmp_path, [_ok(rows)])
    first.call(dict(query))
    second, transport = _head_client(tmp_path, [_err("should not be called")])
    assert second.call(dict(query)) == rows
    assert transport.queries == []


def test_a_range_near_the_head_is_not_cached(tmp_path):
    client, transport = _head_client(tmp_path, [_ok([{"hash": "0x1", "blockNumber": "5"}])])
    near = {**QUERY, "startblock": 0, "endblock": 1000 - 63}
    client.call(dict(near))
    client.call(dict(near))
    assert len(transport.queries) == 2
    assert list(tmp_path.iterdir()) == []


def test_the_boundary_is_64_blocks_below_the_head(tmp_path):
    client, transport = _head_client(tmp_path, [_ok([])])
    exact = {**QUERY, "startblock": 0, "endblock": 1000 - 64}
    client.call(dict(exact))
    client.call(dict(exact))
    assert len(transport.queries) == 1


def test_an_open_ended_query_is_not_cached(tmp_path):
    client, transport = _head_client(tmp_path, [_ok([])])
    client.call({**QUERY, "startblock": 0, "endblock": es.MAX_BLOCK})
    client.call({**QUERY, "startblock": 0, "endblock": es.MAX_BLOCK})
    assert len(transport.queries) == 2


def test_an_error_is_never_cached(tmp_path):
    client, transport = _head_client(tmp_path, [_err("Max rate limit reached"), _ok([])])
    query = {**QUERY, "startblock": 0, "endblock": 500}
    assert client.call(dict(query)) == []
    assert len(transport.queries) == 2
    for path in tmp_path.iterdir():
        assert "rate limit" not in path.read_text().lower()


def test_a_permanent_error_is_never_cached(tmp_path):
    client, _ = _head_client(tmp_path, [_err("Error! Invalid address format")])
    with pytest.raises(PermanentApiError):
        client.call({**QUERY, "startblock": 0, "endblock": 500})
    assert list(tmp_path.iterdir()) == []


def test_the_api_key_is_in_neither_the_cache_key_nor_the_files(tmp_path):
    client, _ = _head_client(tmp_path, [_ok([{"hash": "0x1", "blockNumber": "5"}])])
    client.call({**QUERY, "startblock": 0, "endblock": 500})
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert "SECRET-KEY" not in files[0].name
    assert "SECRET-KEY" not in files[0].read_text()
    other, transport = _head_client(tmp_path, [_err("not called")])
    other.api_key = "A-DIFFERENT-KEY"  # same query, other key: same entry
    assert other.call({**QUERY, "startblock": 0, "endblock": 500})
    assert transport.queries == []


def test_a_corrupt_cache_file_is_ignored(tmp_path):
    client, transport = _head_client(tmp_path, [_ok([])])
    query = {**QUERY, "startblock": 0, "endblock": 500}
    client.call(dict(query))
    (path,) = tmp_path.iterdir()
    path.write_text("{not json")
    assert client.call(dict(query)) == []
    assert len(transport.queries) == 2


def test_a_partially_indexed_answer_is_not_cached(tmp_path):
    payload = {
        "status": "2",
        "message": "Some internal transactions within this block range have not yet been processed",
        "result": [{"hash": "0x1", "blockNumber": "5"}],
    }
    client, transport = _head_client(tmp_path, [_Resp(payload)])
    query = {**QUERY, "action": "txlistinternal", "startblock": 0, "endblock": 500}
    client.call(dict(query))
    client.call(dict(query))
    assert len(transport.queries) == 2


def test_different_networks_do_not_share_cache_entries(tmp_path):
    a, _ = _head_client(tmp_path, [_ok([{"hash": "0x1", "blockNumber": "5"}])], chain_id=1)
    a.call({**QUERY, "startblock": 0, "endblock": 500})
    b, transport = _head_client(tmp_path, [_ok([])], chain_id=10)
    assert b.call({**QUERY, "startblock": 0, "endblock": 500}) == []
    assert len(transport.queries) == 1


def test_no_cache_dir_means_no_head_lookup():
    client, transport = _client([_ok([])])
    client.call({**QUERY, "startblock": 0, "endblock": 5})
    assert all(q.get("module") != "proxy" for q in transport.queries)


# --- progress ----------------------------------------------------------------------------------


def test_progress_is_reported_every_n_calls(capsys):
    client, _ = _client([_ok([])], progress_every=3)
    for _i in range(7):
        client.call(dict(QUERY))
    lines = [x for x in capsys.readouterr().err.splitlines() if x.startswith("[*]")]
    assert len(lines) == 2
    assert "3 explorer calls" in lines[0]


def test_progress_goes_to_a_callback_when_one_is_set():
    seen = []
    client, _ = _client([_ok([])], progress_every=2)
    client.progress = seen.append
    for _i in range(4):
        client.call(dict(QUERY))
    assert [s["calls"] for s in seen] == [2, 4]
    assert {"calls", "cache_hits", "waited"} <= set(seen[0])


def test_no_progress_by_default(capsys):
    client, _ = _client([_ok([])])
    for _i in range(5):
        client.call(dict(QUERY))
    assert capsys.readouterr().err == ""


# --- JSON-RPC eth_getLogs fallback -----------------------------------------------------------


class _RpcResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRpc:
    """A JSON-RPC node with a block-range limit, serving a fixed set of logs."""

    def __init__(self, logs, limit=None, head=5000, fail_blocks=False):
        self.logs = logs
        self.limit = limit
        self.head = head
        self.fail_blocks = fail_blocks
        self.calls = []
        self.spans = []  # block spans of the getLogs requests the node accepted

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode())
        self.calls.append(body)
        method, params = body["method"], body["params"]
        if method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getBlockByNumber":
            if self.fail_blocks:
                return _RpcResponse({"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})
            result = {"timestamp": hex(1_600_000_000 + int(params[0], 16) * 10)}
        elif method == "eth_getLogs":
            flt = params[0]
            lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            if self.limit and hi - lo + 1 > self.limit:
                err = {"code": -32005, "message": "query exceeds max block range"}
                return _RpcResponse({"jsonrpc": "2.0", "id": 1, "error": err})
            self.spans.append(hi - lo + 1)
            topics = flt["topics"]
            result = [
                log
                for log in self.logs
                if lo <= int(log["blockNumber"], 16) <= hi
                and log["address"] == flt["address"]
                and all(t is None or log["topics"][i] == t for i, t in enumerate(topics))
            ]
        else:
            result = None
        return _RpcResponse({"jsonrpc": "2.0", "id": 1, "result": result})


def _log(block, index, topics=None, tx="0x1"):
    return {
        "address": ADDR,
        "topics": topics or [TOPIC0],
        "data": "0x",
        "blockNumber": hex(block),
        "transactionHash": tx + format(block * 100 + index, "x"),
        "transactionIndex": "0x0",
        "logIndex": hex(index),
    }


def _throttled_client(rpc, monkeypatch, **kwargs):
    monkeypatch.setattr(es, "_fetch", rpc)
    return _client(
        [_err("Too many requests")],
        rpc_url="https://rpc.example",
        base_url="https://op.blockscout.com/api",
        style="compat",
        retries=2,
        **kwargs,
    )


def test_a_throttled_explorer_falls_back_to_rpc_logs(monkeypatch):
    rpc = FakeRpc([_log(100, 0), _log(250, 1), _log(4000, 2)])
    client, _ = _throttled_client(rpc, monkeypatch)
    rows = client.get_logs(ADDR, TOPIC0, 0, 4999)
    assert [int(r["blockNumber"], 16) for r in rows] == [100, 250, 4000]


def test_rpc_rows_have_the_explorer_row_shape_with_timestamps(monkeypatch):
    rpc = FakeRpc([_log(100, 3, tx="0xabc")])
    client, _ = _throttled_client(rpc, monkeypatch)
    (row,) = client.get_logs(ADDR, TOPIC0, 0, 4999)
    assert row["blockNumber"] == hex(100)
    assert int(row["timeStamp"], 16) == 1_600_000_000 + 1000
    assert row["logIndex"] == "0x3"
    assert row["transactionHash"].startswith("0xabc")
    assert row["topics"] == [TOPIC0]
    assert row["data"] == "0x"
    assert row["gasPrice"] == "0x"  # a node log has no gas price; events.py reads "0x" as 0


def test_block_timestamps_are_fetched_once_per_block(monkeypatch):
    rpc = FakeRpc([_log(100, 0), _log(100, 1), _log(200, 0)])
    client, _ = _throttled_client(rpc, monkeypatch)
    client.get_logs(ADDR, TOPIC0, 0, 4999)
    asked = [c["params"][0] for c in rpc.calls if c["method"] == "eth_getBlockByNumber"]
    assert sorted(asked) == [hex(100), hex(200)]


def test_the_rpc_range_is_chunked_to_the_providers_limit(monkeypatch):
    monkeypatch.setattr(es, "RPC_LOG_SPAN", 4000)
    rpc = FakeRpc([_log(10, 0), _log(1500, 0), _log(4500, 0)], limit=500)
    client, _ = _throttled_client(rpc, monkeypatch)
    rows = client.get_logs(ADDR, TOPIC0, 0, 4999)
    assert len(rows) == 3
    spans = rpc.spans
    assert max(spans) <= 500


def test_an_open_ended_range_is_clamped_to_the_head(monkeypatch):
    rpc = FakeRpc([_log(100, 0)], head=3000)
    client, _ = _throttled_client(rpc, monkeypatch)
    client.get_logs(ADDR, TOPIC0, 0, es.MAX_BLOCK)
    last = [c for c in rpc.calls if c["method"] == "eth_getLogs"][-1]
    assert int(last["params"][0]["toBlock"], 16) == 3000


def test_indexed_topics_are_sent_to_the_node_and_matched(monkeypatch):
    rpc = FakeRpc([_log(100, 0, [TOPIC0, TOPIC1]), _log(101, 0, [TOPIC0, "0x" + "c" * 64])])
    client, _ = _throttled_client(rpc, monkeypatch)
    rows = client.get_logs(ADDR, TOPIC0, 0, 4999, topics={1: TOPIC1})
    assert [int(r["blockNumber"], 16) for r in rows] == [100]
    flt = [c for c in rpc.calls if c["method"] == "eth_getLogs"][0]["params"][0]
    assert flt["topics"] == [TOPIC0, TOPIC1]


def test_without_an_rpc_url_the_explorer_error_stands():
    client, _ = _client(
        [_err("Too many requests")],
        base_url="https://op.blockscout.com/api",
        style="compat",
        retries=2,
    )
    with pytest.raises(ApiError):
        client.get_logs(ADDR, TOPIC0, 0, 100)


def test_a_rejected_key_does_not_fall_back_to_rpc(monkeypatch):
    rpc = FakeRpc([])
    monkeypatch.setattr(es, "_fetch", rpc)
    client, _ = _client([_err("Invalid API Key")], rpc_url="https://rpc.example")
    with pytest.raises(ApiKeyError):
        client.get_logs(ADDR, TOPIC0, 0, 100)
    assert rpc.calls == []


def test_a_failing_rpc_leaves_the_original_error(monkeypatch):
    def dead(request, timeout=None):
        raise OSError("down")

    monkeypatch.setattr(es, "_fetch", dead)
    client, _ = _client(
        [_err("Too many requests")],
        rpc_url="https://rpc.example",
        base_url="https://op.blockscout.com/api",
        style="compat",
        retries=2,
    )
    with pytest.raises(ApiError, match="Too many requests"):
        client.get_logs(ADDR, TOPIC0, 0, 100)


def test_missing_block_timestamps_fail_the_lookup_instead_of_writing_zeros(monkeypatch):
    rpc = FakeRpc([_log(100, 0)], fail_blocks=True)
    client, _ = _throttled_client(rpc, monkeypatch)
    with pytest.raises(ApiError):
        client.get_logs(ADDR, TOPIC0, 0, 4999)


def test_after_a_fallback_the_next_lookup_goes_straight_to_the_node(monkeypatch):
    rpc = FakeRpc([_log(100, 0)])
    client, transport = _throttled_client(rpc, monkeypatch)
    client.get_logs(ADDR, TOPIC0, 0, 4999)
    asked = len(transport.queries)
    client.get_logs(ADDR, TOPIC0, 0, 4999)
    assert len(transport.queries) == asked
    assert client.rpc_fallbacks >= 2


def test_the_explorer_is_preferred_when_it_answers(monkeypatch):
    rpc = FakeRpc([_log(100, 0)])
    monkeypatch.setattr(es, "_fetch", rpc)
    row = {
        "address": ADDR,
        "topics": [TOPIC0],
        "data": "0x",
        "blockNumber": "0x64",
        "timeStamp": "0x5",
        "logIndex": "0x0",
        "transactionHash": "0xaa",
    }
    client, _ = _client([_ok([row])], rpc_url="https://rpc.example")
    assert client.get_logs(ADDR, TOPIC0, 0, 99)[0]["timeStamp"] == "0x5"
    assert not [c for c in rpc.calls if c["method"] == "eth_getLogs"]


# --- CLI wiring --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", ["demix", "multi", "cluster", "characterize", "trace", "classify-address"]
)
def test_every_chain_reading_command_takes_the_network_layer_flags(command, tmp_path):
    from tornado_demix import cli
    from tornado_demix.networks import get_network

    argv = [command, ADDR]
    if command == "trace":
        argv += ["--amount", "1"]
    args = cli.build_parser().parse_args(
        argv
        + [
            "--max-wait",
            "7",
            "--pause",
            "0.25",
            "--http-cache",
            str(tmp_path),
            "--progress-every",
            "3",
        ]
    )
    client = cli._make_client(args, "K", get_network("ethereum"))
    assert (client.max_wait, client.pause, client.cache_dir, client.progress_every) == (
        7.0,
        0.25,
        str(tmp_path),
        3,
    )


def test_the_flag_defaults_keep_the_cache_off_and_the_budget_at_two_minutes():
    from tornado_demix import cli
    from tornado_demix.networks import get_network

    args = cli.build_parser().parse_args(["demix", ADDR])
    client = cli._make_client(args, "K", get_network("ethereum"))
    assert (client.max_wait, client.pause, client.cache_dir) == (120, 0, None)
    assert client.progress_every == cli.DEFAULT_PROGRESS_EVERY


@pytest.mark.parametrize("flag", ["--max-wait", "--pause"])
def test_a_negative_wait_is_refused(flag):
    from tornado_demix import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["demix", ADDR, flag, "-1"])


# --- fix round 1 ---------------------------------------------------------------------------------


def _proxy_throttled(account_answer):
    """A transport whose proxy module is always throttled."""

    class Transport:
        def __init__(self):
            self.queries = []

        def __call__(self, url, params=None, timeout=None):
            self.queries.append(dict(params))
            if params.get("module") == "proxy":
                return _err("Max rate limit reached")
            return account_answer

    return Transport()


def test_a_failed_head_lookup_is_one_attempt_and_is_remembered(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    client = EtherscanClient("K", cache_dir=str(tmp_path))
    transport = _proxy_throttled(_ok([{"hash": "0x1", "blockNumber": "5"}]))
    client.session.get = transport
    for page in range(3):
        client.call({**QUERY, "startblock": 0, "endblock": 100, "page": page + 1})
    proxy = [q for q in transport.queries if q.get("module") == "proxy"]
    assert len(proxy) == 1  # one attempt, then not asked again for a while
    assert sum(sleeps) == 0
    assert list(tmp_path.iterdir()) == []  # no head, no cache
    assert client.retries == 10  # the single-attempt probe restored the setting


def test_the_head_lookup_is_retried_after_the_failure_memory_expires(tmp_path):
    client = EtherscanClient("K", cache_dir=str(tmp_path))
    transport = _proxy_throttled(_ok([]))
    client.session.get = transport
    client.call({**QUERY, "startblock": 0, "endblock": 100})
    client._head_failed_until = 0.0
    client.call({**QUERY, "startblock": 0, "endblock": 100})
    assert len([q for q in transport.queries if q.get("module") == "proxy"]) == 2


class _NodeFetch:
    """urlopen stand-in: scripted JSON-RPC behaviour, HTTP errors included."""

    def __init__(self, throttle_first=0, head=1_000_000, logs=()):
        self.throttle_left = throttle_first
        self.head = head
        self.logs = list(logs)
        self.methods = []
        self.spans = []

    def __call__(self, request, timeout=None):
        import urllib.error

        body = json.loads(request.data.decode())
        self.methods.append(body["method"])
        if body["method"] == "eth_blockNumber":
            return _RpcResponse({"result": hex(self.head)})
        if body["method"] == "eth_getBlockByNumber":
            return _RpcResponse({"result": {"timestamp": "0x10"}})
        if self.throttle_left:
            self.throttle_left -= 1
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        flt = body["params"][0]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        self.spans.append(hi - lo + 1)
        return _RpcResponse(
            {"result": [x for x in self.logs if lo <= int(x["blockNumber"], 16) <= hi]}
        )


def test_a_node_429_is_backed_off_not_read_as_a_range_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    node = _NodeFetch(throttle_first=3, logs=[_log(10, 0)])
    monkeypatch.setattr(es, "_fetch", node)
    client = EtherscanClient("K", rpc_url="http://node/SECRET-PATH")
    rows = client._rpc_logs(ADDR, TOPIC0, None, 0, 100)
    assert len(rows) == 1
    assert len(sleeps) >= 3  # backed off for each throttled answer
    assert node.spans and node.spans[0] == 101  # the span was never halved by the 429s


def test_a_node_that_keeps_throttling_ends_with_an_error_not_a_halving_storm(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    node = _NodeFetch(throttle_first=10_000)
    monkeypatch.setattr(es, "_fetch", node)
    client = EtherscanClient("K", rpc_url="http://node/SECRET-PATH", max_wait=30)
    with pytest.raises(ApiError) as exc:
        client._rpc_logs(ADDR, TOPIC0, None, 0, 20_000_000)
    assert "SECRET-PATH" not in str(exc.value)
    assert node.methods.count("eth_getLogs") <= client.retries + 1
    assert sum(sleeps) <= 30 + 1e-6


@pytest.mark.parametrize(
    "text,is_range",
    [
        ("query exceeds max block range", True),
        ("Log response size exceeded. up to a 2K block range", True),
        ("query returned more than 10000 results", True),
        ("rate limit exceeded", False),
        ("daily request limit reached", False),
        ("HTTP Error 429: Too Many Requests", False),
    ],
)
def test_range_limit_words_do_not_catch_throttling(text, is_range):
    assert es._range_limit(text) is is_range


def test_the_span_grows_back_after_clean_requests(monkeypatch):
    node = _NodeFetch(logs=[])
    monkeypatch.setattr(es, "_fetch", node)
    client = EtherscanClient("K", rpc_url="http://node/x")
    client._rpc_span, client._rpc_span_ceiling = 1000, 8000
    client._rpc_logs(ADDR, TOPIC0, None, 0, 500_000)
    assert node.spans[:4] == [1000, 1000, 2000, 2000]
    assert max(node.spans) == 8000  # never above the ceiling


def test_a_range_limit_sets_the_ceiling_and_is_remembered(monkeypatch):
    rpc = FakeRpc([], limit=500, head=100_000)
    monkeypatch.setattr(es, "_fetch", rpc)
    client = EtherscanClient("K", rpc_url="http://node/x")
    client._rpc_logs(ADDR, TOPIC0, None, 0, 100_000)
    assert max(rpc.spans) <= 500
    first = len([c for c in rpc.calls if c["method"] == "eth_getLogs"])
    rpc.calls.clear()
    client._rpc_logs(ADDR, TOPIC0, None, 0, 100_000)
    second = len([c for c in rpc.calls if c["method"] == "eth_getLogs"])
    assert second < first  # no new halving probes the second time


def test_the_walk_starts_at_the_first_known_log_block(monkeypatch):
    node = _NodeFetch(logs=[_log(400_000, 0), _log(400_050, 1)])
    monkeypatch.setattr(es, "_fetch", node)
    client = EtherscanClient("K", rpc_url="http://node/x")
    client._rpc_logs(ADDR, TOPIC0, None, 0, 1_000_000)
    assert client.first_blocks[(ADDR, TOPIC0)] == 400_000
    node.spans.clear()
    client._rpc_logs(ADDR, TOPIC0, None, 0, 1_000_000)
    assert sum(node.spans) <= 1_000_000 - 400_000 + 1  # blocks below the first log skipped


def test_a_caller_can_set_the_deploy_block(monkeypatch):
    node = _NodeFetch(logs=[])
    monkeypatch.setattr(es, "_fetch", node)
    client = EtherscanClient("K", rpc_url="http://node/x")
    client.first_blocks[(ADDR, TOPIC0)] = 900_000
    client._rpc_logs(ADDR, TOPIC0, None, 0, 1_000_000)
    assert sum(node.spans) == 100_001


def test_a_node_walk_is_ticked_and_honours_the_pause(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    node = _NodeFetch(logs=[_log(10, 0)])
    monkeypatch.setattr(es, "_fetch", node)
    seen = []
    client = EtherscanClient("K", rpc_url="http://node/x", pause=0.25, progress_every=1)
    client.progress = seen.append
    client._rpc_logs(ADDR, TOPIC0, None, 0, 100)
    assert client.rpc_calls == len(node.methods) >= 2
    assert client.stats()["rpc_calls"] == client.rpc_calls
    assert sleeps.count(0.25) == client.rpc_calls
    assert seen and "rpc_calls" in seen[-1]


def test_a_list_read_from_the_node_is_recorded_as_a_notice(monkeypatch):
    rpc = FakeRpc([_log(100, 0)])
    client, _ = _throttled_client(rpc, monkeypatch)
    client.get_logs(ADDR, TOPIC0, 0, 4999)
    (note,) = client.truncated
    assert note["reason"] == "rpc_fallback_no_gas_price"
    assert note["action"] == "getLogs" and note["address"] == ADDR


def test_the_notice_is_worded_as_a_notice_not_a_failure():
    from tornado_demix import incomplete

    result = {
        "errors": [],
        "truncated": [
            {"action": "getLogs", "address": ADDR, "reason": "rpc_fallback_no_gas_price"}
        ],
    }
    text = incomplete.summary(result)
    assert text == "1 list read from the RPC node (gas-price signal unavailable) — notice"
    assert "incomplete" not in text
    assert "no gas price" in incomplete.lines(result)[0]
    assert incomplete.notice_only(result)
    result["errors"] = [incomplete.lookup_failed("history", "timeout", address=ADDR)]
    assert "results may be incomplete" in incomplete.summary(result)
    assert not incomplete.notice_only(result)


def test_a_run_result_carries_the_node_notice():
    from tests.fixtures import demix_result as fx
    from tests.test_demix import _FakeClient, _recipient, _tx, _wlog
    from tornado_demix.constants import ZERO_ADDRESS
    from tornado_demix.demix import run_demix
    from tornado_demix.report_json import demix_json

    class Client(_FakeClient):
        truncated = []
        rpc_fallbacks = 0

        def get_logs(self, *args, **kwargs):
            es.EtherscanClient._note_rpc_fallback(self, ADDR)
            return super().get_logs(*args, **kwargs)

    deposits = [_tx(fx.POOL_01_ETH, 0.1, 1000, 10, "0xdep0")]
    logs = [_wlog(_recipient(1), format(9000, "064x"), ZERO_ADDRESS, 0)]
    data = run_demix(Client(deposits, logs), fx.WALLET, network=fx.network())
    assert data["truncated"]
    assert {t["reason"] for t in data["truncated"]} == {"rpc_fallback_no_gas_price"}
    assert "rpc_fallback_no_gas_price" in demix_json(data)


# --- wall-clock budget, Retry-After, permanent URL errors -------------------------------------------


def test_max_wait_bounds_wall_time_including_timeouts(monkeypatch):
    import requests

    clock = [0.0]
    monkeypatch.setattr(es.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    seen = []

    def hang(url, params=None, timeout=None):
        seen.append(timeout)
        clock[0] += timeout  # the attempt takes its whole timeout
        raise requests.exceptions.Timeout("slow")

    client = EtherscanClient("K", retries=50, max_wait=100)
    client.session.get = hang
    with pytest.raises(ApiError):
        client.call(dict(QUERY))
    assert clock[0] <= 100 + 40  # the allowance plus at most the last attempt
    assert len(seen) < 10  # not 50 attempts of 40 s
    assert min(seen) >= 1


def test_retry_after_zero_does_not_cause_instant_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    client, _ = _client(
        [_Resp({}, 429, {"Retry-After": "0"}), _Resp({}, 429, {"Retry-After": "0"}), _ok([])]
    )
    assert client.call(dict(QUERY)) == []
    assert len(sleeps) == 2 and all(s >= 0.5 for s in sleeps)


def test_retry_after_can_lengthen_but_not_shorten(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    client, _ = _client([_Resp({}, 429, {"Retry-After": "12"}), _ok([])])
    client.call(dict(QUERY))
    assert sleeps == [12]


def test_a_malformed_provider_url_is_permanent():
    client = EtherscanClient("SECRET-KEY", base_url="not a url", style="compat")
    with pytest.raises(PermanentApiError) as exc:
        client.call(dict(QUERY))
    assert "SECRET-KEY" not in str(exc.value)


def test_the_missing_action_wording_of_etherscan_is_permanent():
    client, transport = _client([_err("Missing Or invalid Action name")])
    with pytest.raises(PermanentApiError):
        client.call(dict(QUERY))
    assert len(transport.queries) == 1


def test_cache_temp_names_are_unique_per_write(tmp_path, monkeypatch):
    names = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (names.append(a), real(a, b)))
    path = str(tmp_path / "k.json")
    es._cache_write(path, str(tmp_path), [])
    es._cache_write(path, str(tmp_path), [])
    assert len(set(names)) == 2


# --- fix round 2 ---------------------------------------------------------------------------------


def test_a_walk_from_a_later_block_does_not_teach_a_wrong_first_block(monkeypatch):
    node = _NodeFetch(logs=[_log(100, 0), _log(5000, 0)], head=10_000)
    monkeypatch.setattr(es, "_fetch", node)
    client = EtherscanClient("K", rpc_url="http://node/x")
    later = client._rpc_logs(ADDR, TOPIC0, None, 1000, 6000)
    assert [int(r["blockNumber"], 16) for r in later] == [5000]
    assert client.first_blocks == {}
    whole = client._rpc_logs(ADDR, TOPIC0, None, 0, 6000)
    assert [int(r["blockNumber"], 16) for r in whole] == [100, 5000]
    assert client.first_blocks[(ADDR, TOPIC0)] == 100


def test_an_attempt_that_has_started_keeps_its_full_timeout(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(es.time, "monotonic", lambda: clock[0])
    seen = []

    def slow(url, params=None, timeout=None):
        seen.append(timeout)
        clock[0] += 5  # a slow but successful answer
        return _ok([])

    client = EtherscanClient("K", max_wait=0)
    client.session.get = slow
    assert client.call(dict(QUERY)) == []
    assert seen == [40]
