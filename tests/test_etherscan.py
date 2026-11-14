"""API client: paged queries and token-transfer helpers."""

import pytest
import requests

from tornado_demix.errors import ApiError
from tornado_demix.etherscan import MAX_BLOCK, EtherscanClient

DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
WALLET = "0x1234567890abcdef1234567890abcdef12345678"
POOL = "0xd4b88df4d29f5cedd6857912842cff3b20c8cfa3"


class RecordingClient(EtherscanClient):
    """Captures the query dicts instead of performing HTTP calls."""

    def __init__(self, pages):
        EtherscanClient.__init__(self, "KEY")
        self.pages = list(pages)
        self.queries = []

    def call(self, params):
        self.queries.append(dict(params))
        return self.pages.pop(0) if self.pages else []


def _row(to, raw, ts, tx_hash, contract=DAI, sender=WALLET):
    return {
        "from": sender,
        "to": to,
        "value": str(raw),
        "timeStamp": str(ts),
        "blockNumber": "1",
        "hash": tx_hash,
        "contractAddress": contract,
        "tokenDecimal": "18",
        "tokenSymbol": "DAI",
        "gasPrice": "1",
    }


def test_fetch_all_passes_extra_params_on_every_page():
    client = RecordingClient([[_row(POOL, 1, 1, "0xa")]])
    client.fetch_all("tokentx", WALLET, extra={"contractaddress": DAI})
    assert client.queries[0]["contractaddress"] == DAI
    assert client.queries[0]["action"] == "tokentx"


def test_fetch_all_without_extra_is_unchanged():
    client = RecordingClient([[{"hash": "0xa", "blockNumber": "1"}]])
    client.fetch_all("txlist", WALLET)
    assert "contractaddress" not in client.queries[0]


def test_token_transfers_filters_by_contract_when_given():
    client = RecordingClient([[_row(POOL, 1, 1, "0xa")]])
    client.token_transfers(WALLET, contract=DAI)
    assert client.queries[0]["contractaddress"] == DAI

    client = RecordingClient([[_row(POOL, 1, 1, "0xb")]])
    client.token_transfers(WALLET)
    assert "contractaddress" not in client.queries[0]


def test_outgoing_token_in_window_returns_raw_integers():
    """Token amounts must not pass through float."""
    raw = 100000 * 10**18
    client = RecordingClient([[_row(POOL, raw, 500, "0xa")]])
    sends = client.outgoing_token_in_window(WALLET, DAI, 0, 1000)
    assert sends == [{"to": POOL, "raw": raw, "ts": 500, "hash": "0xa"}]
    assert isinstance(sends[0]["raw"], int)


def test_outgoing_token_in_window_filters_window_and_direction():
    rows = [
        _row(POOL, 1, 100, "0xearly"),  # before window
        _row(POOL, 2, 500, "0xin"),  # inside
        _row(POOL, 3, 9000, "0xlate"),  # after window
        _row(POOL, 4, 500, "0xincoming", sender=POOL),  # not from wallet
    ]
    client = RecordingClient([rows])
    sends = client.outgoing_token_in_window(WALLET, DAI, 200, 1000)
    assert [s["hash"] for s in sends] == ["0xin"]


# The "no end block" sentinel
# Chain heads read on 2026-08-20. These are not invariants - they only ever go
# up - which is exactly why the assertion below is a lower bound: the sentinel
# has to stay above a head that keeps rising. 99999999, the value Etherscan's
# own examples use, had already fallen below three of them.
CHAIN_HEADS_2026_08 = {
    "ethereum": 25_795_552,
    "bsc": 117_019_343,
    "arbitrum": 496_475_600,
    "polygon": 92_342_993,
    "optimism": 155_810_369,
    "base": 50_215_084,
    "gnosis": 47_820_580,
    "avalanche": 93_252_385,
}


def test_max_block_is_above_every_supported_chain_head():
    """An endblock below the head silently returns "no transactions found".

    Nothing in the stack treats that as an error: the explorer reports an empty
    range, ``call`` maps it to the benign empty list, and the wallet is reported
    as having made no Tornado deposits at all.
    """
    for name, head in CHAIN_HEADS_2026_08.items():
        assert MAX_BLOCK > head * 100, name


def test_fetch_all_defaults_to_the_sentinel_not_a_stale_literal():
    client = RecordingClient([[{"hash": "0xa", "blockNumber": "1"}]])
    client.fetch_all("txlist", WALLET)
    assert int(client.queries[0]["endblock"]) == MAX_BLOCK
    for name, head in CHAIN_HEADS_2026_08.items():
        assert int(client.queries[0]["endblock"]) > head, name


def test_token_transfers_defaults_to_the_sentinel():
    client = RecordingClient([[_row(POOL, 1, 1, "0xa")]])
    client.token_transfers(WALLET)
    assert int(client.queries[0]["endblock"]) == MAX_BLOCK


# The JSON-RPC proxy module (eth_getTransactionByHash)
# The proxy module answers with a raw JSON-RPC envelope - ``jsonrpc``/``result``
# and no top-level ``status``/``message`` - so ``call`` cannot read it the way it
# reads the classic account/logs endpoints. Misreading it would make
# ``tx_sender`` return None for every hash, so no self-relayed lead would ever
# be verified.
SENDER = "0xAbC0000000000000000000000000000000000001"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Replays a queue of JSON payloads in place of real HTTP calls."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        return _FakeResp(self._payloads.pop(0))


def _proxy_client(payloads, retries=3):
    client = EtherscanClient("KEY", retries=retries)
    client.session = _FakeSession(payloads)
    return client


def _tx_payload(from_addr):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"hash": "0xdead", "from": from_addr, "to": "0xpool", "input": "0x13d98d13"},
    }


def test_call_reads_a_proxy_jsonrpc_result_as_success():
    """A reply with jsonrpc/result and no status is a success, not a soft error."""
    client = _proxy_client([_tx_payload(SENDER)])
    result = client.call(
        {"module": "proxy", "action": "eth_getTransactionByHash", "txhash": "0xdead"}
    )
    assert isinstance(result, dict)
    assert result["from"] == SENDER
    # It must not burn retries on a perfectly good answer.
    assert len(client.session.calls) == 1


def test_tx_sender_returns_the_broadcaster_lowercased():
    client = _proxy_client([_tx_payload(SENDER)])
    assert client.tx_sender("0xdead") == SENDER.lower()


def test_tx_sender_returns_none_for_an_unknown_hash():
    """A JSON-RPC result of null (hash the node has never seen) is a clean None,
    not an exhausted-retry failure and not an empty list."""
    client = _proxy_client([{"jsonrpc": "2.0", "id": 1, "result": None}])
    assert client.tx_sender("0xunknown") is None
    assert len(client.session.calls) == 1


def test_call_raises_on_a_proxy_error_object():
    """An ``error`` object is a real error, surfaced as ApiError."""
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid argument"}}
    client = _proxy_client([err, err, err])
    with pytest.raises(ApiError):
        client.call({"module": "proxy", "action": "eth_getTransactionByHash", "txhash": "0xbad"})


def test_tx_sender_raises_on_a_proxy_error_instead_of_reading_as_unknown():
    """A failed lookup is not an unknown hash: with no RPC to fall back on it raises."""
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid argument"}}
    client = _proxy_client([err, err, err])
    with pytest.raises(ApiError):
        client.tx_sender("0xbad")


def test_current_block_reads_the_proxy_hex_number():
    """eth_blockNumber answers with a hex string; it is parsed to an int."""
    client = _proxy_client([{"jsonrpc": "2.0", "id": 1, "result": "0x10d4f"}])
    assert client.current_block() == 0x10D4F
    assert client.session.calls[0]["action"] == "eth_blockNumber"


def test_current_block_raises_on_an_unusable_reply():
    from tornado_demix.errors import BlockLookupError

    client = _proxy_client([{"jsonrpc": "2.0", "id": 1, "result": None}])
    with pytest.raises(BlockLookupError):
        client.current_block()


class _FailingSession:
    """Raises each queued exception in turn, then replays JSON payloads."""

    def __init__(self, events):
        self._events = list(events)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        event = self._events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return _FakeResp(event)


def test_call_retries_a_network_error_and_then_succeeds():
    import requests

    client = EtherscanClient("KEY", retries=3)
    client.session = _FailingSession(
        [requests.ConnectionError("reset"), {"status": "1", "message": "OK", "result": ["row"]}]
    )
    assert client.call({"module": "account", "action": "txlist"}) == ["row"]
    assert client.session.calls == 2


def test_call_raises_api_error_when_every_attempt_fails():
    import requests

    client = EtherscanClient("KEY", retries=2)
    client.session = _FailingSession([requests.Timeout("slow"), ValueError("not json")])
    with pytest.raises(ApiError, match="ValueError"):
        client.call({"module": "account", "action": "txlist", "address": WALLET})


def test_call_does_not_swallow_programming_errors():
    client = EtherscanClient("KEY", retries=3)
    client.session = _FailingSession([TypeError("bug in the caller")])
    with pytest.raises(TypeError):
        client.call({"module": "account", "action": "txlist"})


class _RangeClient(EtherscanClient):
    """Serves rows from a fixed list, honouring block range, page and offset."""

    def __init__(self, rows, block_key, start_key, end_key):
        EtherscanClient.__init__(self, "KEY")
        self.rows = rows
        self.keys = (block_key, start_key, end_key)
        self.queries = []

    def call(self, params):
        self.queries.append(dict(params))
        block_key, start_key, end_key = self.keys
        low, high = params[start_key], params[end_key]
        in_range = [r for r in self.rows if low <= _block(r[block_key]) <= high]
        size = params["offset"]
        start = (params["page"] - 1) * size
        return in_range[start : start + size]


def _block(value):
    return int(value, 16) if str(value).startswith("0x") else int(value)


@pytest.fixture
def small_pages(monkeypatch):
    import tornado_demix.etherscan as etherscan

    monkeypatch.setattr(etherscan, "PAGE_SIZE", 2)
    monkeypatch.setattr(etherscan, "MAX_PAGES", 2)


def test_fetch_all_splits_the_block_range_when_the_page_cap_overflows(small_pages):
    rows = [{"hash": "0x%d" % i, "blockNumber": str(b)} for i, b in enumerate([1, 2, 3, 3, 3, 4])]
    client = _RangeClient(rows, "blockNumber", "startblock", "endblock")

    got = client.fetch_all("txlist", WALLET)

    assert [r["hash"] for r in got] == ["0x0", "0x1", "0x2", "0x3", "0x4", "0x5"]
    # The second range starts at the last block of the first, so no row in
    # block 3 is lost when the first batch is cut there.
    assert [q["startblock"] for q in client.queries if q["page"] == 1] == [0, 3, 4]


def test_fetch_all_takes_a_single_overflowing_block_as_is(small_pages):
    rows = [{"hash": "0x%d" % i, "blockNumber": "7"} for i in range(5)]
    client = _RangeClient(rows, "blockNumber", "startblock", "endblock")

    got = client.fetch_all("txlist", WALLET, start_block=7, end_block=7)

    assert [r["hash"] for r in got] == ["0x0", "0x1", "0x2", "0x3"]


def test_get_logs_splits_on_hex_block_numbers_and_dedupes_by_log_index(small_pages):
    rows = [
        {"transactionHash": "0xa", "logIndex": "0x0", "blockNumber": "0x1"},
        {"transactionHash": "0xb", "logIndex": "0x0", "blockNumber": "0x2"},
        {"transactionHash": "0xc", "logIndex": "0x0", "blockNumber": "0x10"},
        {"transactionHash": "0xc", "logIndex": "0x1", "blockNumber": "0x10"},
        {"transactionHash": "0xd", "logIndex": "0x0", "blockNumber": "0x11"},
    ]
    client = _RangeClient(rows, "blockNumber", "fromBlock", "toBlock")

    got = client.get_logs(POOL, "0xtopic", 0, 100)

    assert [(r["transactionHash"], r["logIndex"]) for r in got] == [
        ("0xa", "0x0"),
        ("0xb", "0x0"),
        ("0xc", "0x0"),
        ("0xc", "0x1"),
        ("0xd", "0x0"),
    ]
    assert [q["fromBlock"] for q in client.queries if q["page"] == 1] == [0, 16, 17]


def test_an_error_with_a_null_result_is_retried_not_read_as_empty():
    """status=0 with result=null is a provider error, not "no records"."""
    client = EtherscanClient("KEY", retries=2)
    client.session = _FakeSession(
        [{"status": "0", "message": "Query Timeout occured", "result": None}] * 2
    )
    with pytest.raises(ApiError, match="Query Timeout"):
        client.call({"module": "logs", "action": "getLogs", "address": POOL})


def test_a_null_result_after_a_transient_error_can_still_succeed():
    client = EtherscanClient("KEY", retries=2)
    client.session = _FakeSession(
        [
            {"status": "0", "message": "NOTOK", "result": None},
            {"status": "1", "message": "OK", "result": ["log"]},
        ]
    )
    assert client.call({"module": "logs", "action": "getLogs"}) == ["log"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "0", "message": "No transactions found", "result": []},
        {"status": "0", "message": "No records found", "result": []},
        {"status": "0", "message": "No records found", "result": None},
        {"status": "0", "message": "OK", "result": []},
    ],
)
def test_a_definite_empty_answer_is_still_empty(payload):
    client = EtherscanClient("KEY", retries=2)
    client.session = _FakeSession([payload])
    assert client.call({"module": "account", "action": "txlist"}) == []


class _OnePage(EtherscanClient):
    """Returns ``rows`` for the first page of any query."""

    def __init__(self, rows):
        EtherscanClient.__init__(self, "KEY")
        self.rows = rows

    def call(self, params):
        return list(self.rows) if params["page"] == 1 else []


def _internal(trace, tx_hash="0xmulti", hash_key="hash", trace_key="traceId"):
    return {
        hash_key: tx_hash,
        trace_key: trace,
        "from": WALLET,
        "to": POOL,
        "value": str(10**18),
        "timeStamp": "1700000000",
        "blockNumber": "100",
        "isError": "0",
    }


def test_internal_transfers_sharing_a_tx_hash_are_all_kept():
    """A contract wallet depositing three notes in one multiSend is three rows."""
    rows = [_internal("0_%d" % i) for i in range(3)]
    assert len(_OnePage(rows).internal_txs(WALLET)) == 3


def test_blockscout_internal_rows_are_kept_and_get_a_hash():
    """Blockscout names the field transactionHash and numbers calls by index."""
    rows = [_internal(str(i), hash_key="transactionHash", trace_key="index") for i in range(2)]
    got = _OnePage(rows).internal_txs(WALLET)
    assert len(got) == 2
    assert all(r["hash"] == "0xmulti" for r in got)


def test_a_refetched_identical_row_is_still_dropped():
    row = _internal("0_1")
    assert len(_OnePage([row, dict(row)]).internal_txs(WALLET)) == 1


def test_a_batched_contract_wallet_deposit_counts_every_note():
    from tornado_demix.demix import detect_deposits
    from tornado_demix.networks import load_networks

    eth = load_networks()["ethereum"]
    pool = eth.by_key["1 ETH"].address
    rows = [dict(_internal("0_%d" % i), to=pool) for i in range(3)]
    internal = _OnePage(rows).internal_txs(WALLET)
    deposits = detect_deposits(
        None, WALLET, network=eth, txs=[], internal_txs=internal, token_txs=[]
    )
    assert len(deposits) == 3


class _UnreachableSession:
    def get(self, url, params=None, timeout=None):
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        raise requests.ConnectionError(f"Max retries exceeded with url: /v2/api?{query}")


def test_a_network_error_does_not_leak_the_api_key():
    client = EtherscanClient("SECRETKEY123", retries=1)
    client.session = _UnreachableSession()
    with pytest.raises(ApiError) as err:
        client.call({"module": "proxy", "action": "eth_blockNumber"})
    assert "SECRETKEY123" not in str(err.value)
    assert "apikey=***" in str(err.value)


def test_two_identical_token_transfers_in_one_page_are_both_kept():
    row = {
        "hash": "0xbatch",
        "from": WALLET,
        "to": "0xpool",
        "value": "100",
        "contractAddress": DAI,
        "tokenDecimal": "18",
        "blockNumber": "5",
    }
    assert len(_OnePage([row, dict(row)]).token_transfers(WALLET)) == 2


def test_a_token_transfer_refetched_on_another_page_is_dropped():
    from tornado_demix.etherscan import _dedupe, _number_repeats, _row_identity

    row = {
        "hash": "0xbatch",
        "from": WALLET,
        "to": "0xpool",
        "value": "100",
        "contractAddress": DAI,
        "tokenDecimal": "18",
    }
    rows = _number_repeats([dict(row)]) + _number_repeats([dict(row)])
    assert len(_dedupe(rows, _row_identity)) == 1


def test_current_block_is_asked_once_per_minute():
    calls = []

    class Head(EtherscanClient):
        def call(self, params):
            calls.append(params["action"])
            return "0x10"

    client = Head("KEY")
    assert client.current_block() == 16
    assert client.current_block() == 16
    assert calls == ["eth_blockNumber"]


def test_rows_without_a_hash_are_reported_when_dropped(capsys):
    from tornado_demix.etherscan import _dedupe, _row_identity

    assert _dedupe([{"hash": ""}, {"hash": "0x1"}], _row_identity) == [{"hash": "0x1"}]
    assert "dropped 1 row(s) without a transaction hash" in capsys.readouterr().err


def test_a_block_that_overflows_every_page_is_reported(monkeypatch, capsys):
    import tornado_demix.etherscan as es

    monkeypatch.setattr(es, "MAX_PAGES", 1)
    monkeypatch.setattr(es, "PAGE_SIZE", 2)

    class Full(EtherscanClient):
        def call(self, params):
            return [{"hash": "0x%d" % i, "blockNumber": "7"} for i in range(2)]

    Full("KEY").fetch_all("txlist", WALLET, 7, 9)
    assert "block 7 holds at least 2 rows" in capsys.readouterr().err


def test_first_activity_takes_the_earlier_of_the_two_lists():
    client = _proxy_client(
        [
            {"status": "1", "message": "OK", "result": [{"timeStamp": "5000"}]},
            {"status": "1", "message": "OK", "result": [{"timeStamp": "4000"}]},
        ]
    )
    assert client.first_activity(SENDER) == 4000
    assert [c["offset"] for c in client.session.calls] == [1, 1]
    assert [c["action"] for c in client.session.calls] == ["txlist", "txlistinternal"]


def test_first_activity_is_none_without_transactions():
    empty = {"status": "0", "message": "No transactions found", "result": []}
    assert _proxy_client([empty, empty]).first_activity(SENDER) is None


def test_has_at_least_txs_reads_the_single_row_at_position_n():
    # Explorers cap a page at 1,000 rows, so the check asks for row n alone.
    client = _proxy_client([{"status": "1", "message": "OK", "result": [{"hash": "0x1"}]}])
    assert client.has_at_least_txs(SENDER, 10000) is True
    assert client.session.calls[0]["page"] == 10000
    assert client.session.calls[0]["offset"] == 1
    empty = {"status": "0", "message": "No transactions found", "result": []}
    assert _proxy_client([empty]).has_at_least_txs(SENDER, 10000) is False


# Blockscout has no proxy module: current_block / tx_sender fall back.
BLOCKSCOUT_NO_PROXY = {"status": "0", "message": "NOTOK", "result": "Unknown module"}
RPC_URL = "https://rpc.example"


def _compat_client(payloads, rpc_url="", retries=1):
    client = EtherscanClient(
        "",
        retries=retries,
        base_url="https://x.blockscout.com/api",
        style="compat",
        rpc_url=rpc_url,
    )
    client.session = _FakeSession(payloads)
    return client


def test_current_block_falls_back_to_the_block_module():
    client = _compat_client(
        [BLOCKSCOUT_NO_PROXY, {"jsonrpc": "2.0", "id": 1, "result": "0x319f4cb"}]
    )
    assert client.current_block() == 0x319F4CB
    assert [c["module"] for c in client.session.calls] == ["proxy", "block"]
    assert client.session.calls[1]["action"] == "eth_block_number"


def test_current_block_falls_back_to_the_rpc_node(monkeypatch):
    client = _compat_client([BLOCKSCOUT_NO_PROXY, BLOCKSCOUT_NO_PROXY], rpc_url=RPC_URL)
    monkeypatch.setattr(client, "_rpc", lambda method, params: "0x10")
    assert client.current_block() == 16


def test_current_block_raises_when_every_source_fails():
    from tornado_demix.errors import BlockLookupError

    client = _compat_client([BLOCKSCOUT_NO_PROXY, BLOCKSCOUT_NO_PROXY])
    with pytest.raises(BlockLookupError):
        client.current_block()


def test_a_provider_without_the_proxy_is_probed_once():
    # without the proxy module and without an RPC node the sender cannot be read:
    # an error, not an unknown hash
    client = _compat_client([BLOCKSCOUT_NO_PROXY], retries=3)
    with pytest.raises(ApiError):
        client.tx_sender("0xa")
    assert len(client.session.calls) == 1  # one attempt, not `retries`
    with pytest.raises(ApiError):
        client.tx_sender("0xb")
    assert len(client.session.calls) == 1  # the proxy is not asked again


def test_tx_sender_falls_back_to_the_rpc_node(monkeypatch):
    client = _compat_client([BLOCKSCOUT_NO_PROXY], rpc_url=RPC_URL)
    seen = []

    def fake_rpc(method, params):
        seen.append((method, params))
        return True, {"from": SENDER}

    monkeypatch.setattr(client, "_rpc_result", fake_rpc)
    assert client.tx_sender("0xdead") == SENDER.lower()
    assert seen == [("eth_getTransactionByHash", ["0xdead"])]


def test_tx_sender_raises_without_proxy_and_rpc():
    with pytest.raises(ApiError):
        _compat_client([BLOCKSCOUT_NO_PROXY]).tx_sender("0xdead")


def test_the_rpc_helper_posts_json_rpc_and_survives_a_failure(monkeypatch):
    import io
    import json

    import tornado_demix.etherscan as mod

    sent = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        sent["timeout"] = timeout
        return io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x5"}')

    monkeypatch.setattr(mod, "_fetch", fake_urlopen)
    client = EtherscanClient("KEY", rpc_url=RPC_URL)
    assert client._rpc("eth_blockNumber", []) == "0x5"
    assert sent["url"] == RPC_URL
    assert sent["body"]["method"] == "eth_blockNumber"
    assert sent["timeout"]

    def broken(request, timeout=None):
        raise OSError("down")

    monkeypatch.setattr(mod, "_fetch", broken)
    assert client._rpc("eth_blockNumber", []) is None
    assert EtherscanClient("KEY")._rpc("eth_blockNumber", []) is None  # no rpc_url


def test_etherscan_v2_does_not_try_the_block_module():
    """A V2 client keeps its behaviour: no extra module probing."""
    client = _proxy_client([{"jsonrpc": "2.0", "id": 1, "result": None}])
    from tornado_demix.errors import BlockLookupError

    with pytest.raises(BlockLookupError):
        client.current_block()
    assert len(client.session.calls) == 1


def test_a_partially_indexed_blockscout_answer_keeps_its_rows(monkeypatch):
    """Blockscout's status "2" (range still being indexed) carries valid rows."""
    rows = [{"hash": "0xabc", "blockNumber": "1"}]
    payload = {
        "status": "2",
        "message": "Some internal transactions within this block range have not yet been processed",
        "result": rows,
    }

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    client = EtherscanClient(
        "k", pause=0, base_url="https://base.blockscout.com/api", style="compat"
    )
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp())
    assert client.call({"module": "account", "action": "txlistinternal", "address": "0x1"}) == rows


def test_get_logs_ands_the_indexed_topics_with_topic0():
    client = EtherscanClient("k")
    seen = []

    def fake_call(params):
        seen.append(params)
        return []

    client.call = fake_call
    client.get_logs("0xAbC", "0xt0", 1, 2, topics={1: "0xt1", 2: "0xt2"})
    q = seen[0]
    assert (q["topic0"], q["topic1"], q["topic2"]) == ("0xt0", "0xt1", "0xt2")
    assert q["topic0_1_opr"] == q["topic0_2_opr"] == q["topic1_2_opr"] == "and"
    seen.clear()
    client.get_logs("0xAbC", "0xt0", 1, 2)
    assert not any(k.startswith("topic") and k != "topic0" for k in seen[0])


class _ScriptedClient(EtherscanClient):
    """Serves the given pages in order, then empty pages; records the queries."""

    def __init__(self, pages):
        EtherscanClient.__init__(self, "KEY")
        self.pages = list(pages)
        self.queries = []

    def call(self, params):
        self.queries.append(dict(params))
        return self.pages.pop(0) if self.pages else []


def _log_row(i, block):
    return {"transactionHash": "0x%04x" % i, "logIndex": "0x0", "blockNumber": hex(block)}


def test_a_short_page_that_is_not_the_last_does_not_end_the_walk():
    sizes = [1000, 999, 386]
    pages, n = [], 0
    for size in sizes:
        pages.append([_log_row(n + i, 100 + n + i) for i in range(size)])
        n += size
    client = _ScriptedClient(pages)

    got = client.get_logs(POOL, "0xtopic", 0, 10**6)

    assert len(got) == 1000 + 999 + 386
    assert client.truncated == []


def test_the_walk_stops_on_the_first_empty_page_and_an_empty_first_page_is_empty():
    client = _ScriptedClient([])
    assert client.fetch_all("txlist", WALLET) == []
    assert len(client.queries) == 1
    assert client.truncated == []


def test_rows_repeated_across_the_cursor_boundary_are_deduped(small_pages):
    rows = [{"hash": "0x%d" % i, "blockNumber": str(b)} for i, b in enumerate([1, 2, 3, 3, 3, 4])]
    client = _RangeClient(rows, "blockNumber", "startblock", "endblock")
    got = client.fetch_all("txlist", WALLET)
    assert sorted(r["hash"] for r in got) == sorted(r["hash"] for r in rows)
    assert len(got) == len(rows)


def test_a_block_beyond_the_page_window_is_flagged_on_the_client(small_pages, capsys):
    rows = [{"hash": "0x%d" % i, "blockNumber": "7"} for i in range(5)]
    client = _RangeClient(rows, "blockNumber", "startblock", "endblock")

    client.fetch_all("txlist", WALLET, start_block=7, end_block=7)

    assert len(client.truncated) == 1
    note = client.truncated[0]
    assert note["block"] == 7
    assert note["action"] == "txlist"
    assert note["limit"] == 4
    assert "truncated" in capsys.readouterr().err


def test_more_than_ten_thousand_rows_in_one_block_set_the_flag():
    class Full(EtherscanClient):
        def call(self, params):
            if params["page"] > 10:  # the page * offset <= 10000 window
                raise AssertionError("page beyond the window")
            return [
                {"hash": "0x%d-%d" % (params["page"], i), "blockNumber": "7"} for i in range(1000)
            ]

    client = Full("KEY")
    got = client.fetch_all("txlist", WALLET, 7, 9)
    assert len(got) == 10_000
    assert [n["block"] for n in client.truncated] == [7]


def test_token_transfers_and_internal_txs_use_the_same_walk():
    pages = [
        [{"hash": "0xa", "blockNumber": "1", "tokenDecimal": "18", "value": "1"}] * 1,
        [{"hash": "0xb", "blockNumber": "2", "tokenDecimal": "18", "value": "1"}],
    ]
    client = _ScriptedClient(pages)
    got = client.token_transfers(WALLET)
    assert [r["hash"] for r in got] == ["0xa", "0xb"]


def test_a_capped_deposit_address_page_is_reported(monkeypatch):
    import tornado_demix.deposit_addresses as da

    monkeypatch.setattr(da, "MAX_DEPOSIT_TXS", 2)

    class Capped:
        truncated: list = []

        def call(self, params):
            return [{"hash": "0x1"}, {"hash": "0x2"}]

    client = Capped()
    client.truncated = []
    rows = da._rows(client, "tokentx", WALLET)
    assert len(rows) == 2
    assert client.truncated and client.truncated[0]["action"] == "tokentx"


def test_a_txlist_at_the_deposit_cap_is_not_a_truncation_note(monkeypatch):
    import tornado_demix.deposit_addresses as da

    monkeypatch.setattr(da, "MAX_DEPOSIT_TXS", 2)

    class Capped:
        def __init__(self):
            self.truncated = []

        def call(self, params):
            return [{"hash": "0x1"}, {"hash": "0x2"}]

    client = Capped()
    assert da.classify_deposit_address(client, WALLET) is None
    assert client.truncated == []


def test_a_provider_ignoring_page_does_not_loop_or_flag(small_pages):
    class Stuck(EtherscanClient):
        def __init__(self):
            EtherscanClient.__init__(self, "KEY")
            self.n = 0

        def call(self, params):
            self.n += 1
            return [{"hash": "0xa", "blockNumber": "5"}, {"hash": "0xb", "blockNumber": "5"}]

    client = Stuck()
    got = client.fetch_all("txlist", WALLET)
    assert [r["hash"] for r in got] == ["0xa", "0xb"]
    assert client.n == 2
    assert client.truncated == []


def test_a_short_last_page_combined_with_a_cursor_pass(small_pages):
    # window 2x2: page 2 of the first pass is full, the continuation ends on a short page
    rows = [{"hash": "0x%d" % i, "blockNumber": str(b)} for i, b in enumerate([1, 1, 2, 3, 4])]
    client = _RangeClient(rows, "blockNumber", "startblock", "endblock")
    got = client.fetch_all("txlist", WALLET)
    assert sorted(r["hash"] for r in got) == sorted(r["hash"] for r in rows)
    assert client.truncated == []


def test_the_truncation_wording_is_a_lower_bound(small_pages, capsys):
    rows = [{"hash": "0x%d" % i, "blockNumber": "7"} for i in range(5)]
    _RangeClient(rows, "blockNumber", "startblock", "endblock").fetch_all("txlist", WALLET, 7, 7)
    assert "at least 4 rows" in capsys.readouterr().err


def test_eth_call_returns_the_proxy_answer():
    client = _proxy_client([{"jsonrpc": "2.0", "id": 1, "result": "0x" + format(30, "064x")}])
    assert int(client.eth_call(POOL, "0xcd87a3b4"), 16) == 30
    sent = client.session.calls[0]
    assert sent["action"] == "eth_call" and sent["tag"] == "latest" and sent["to"] == POOL


def test_eth_call_raises_when_no_source_answers():
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "execution reverted"}}
    client = _proxy_client([err, err, err])
    with pytest.raises(ApiError):
        client.eth_call(POOL, "0xcd87a3b4")


T1 = "0x" + "00" * 31 + "01"
T2 = "0x" + "00" * 29 + "4636e4"


def _topic_log(i, t1, t2):
    return {
        "transactionHash": "0x%04x" % i,
        "logIndex": "0x0",
        "blockNumber": hex(100 + i),
        "topics": ["0xt0", t1, t2, "0x" + "00" * 32],
    }


class _Recorded:
    def __init__(self, payload):
        self.payload, self.sent = payload, []

    def get(self, url, params=None, timeout=None):
        self.sent.append(dict(params))
        payload = self.payload

        class R:
            def json(self):
                return payload

        return R()


def test_blockscout_log_query_drops_the_indexed_topic_filters_and_filters_locally():
    """Blockscout answers "No logs found" to a getLogs query with topic1/topic2 filters
    even when matching logs exist (observed on optimism.blockscout.com, 2026-11-13): the
    query goes out with topic0 only and the indexed topics are matched here."""
    rows = [_topic_log(1, T1, T2), _topic_log(2, T1, "0x" + "00" * 32), _topic_log(3, T2, T2)]
    client = EtherscanClient(
        "", pause=0, base_url="https://optimism.blockscout.com/api", style="compat"
    )
    client.session = _Recorded({"status": "1", "message": "OK", "result": rows})
    got = client.get_logs("0xAbC", "0xt0", 1, 2, topics={1: T1, 2: T2})
    assert [r["transactionHash"] for r in got] == ["0x0001"]
    first = client.session.sent[0]
    assert first["topic0"] == "0xt0"
    assert not any(k.startswith("topic") and k != "topic0" for k in first)


def test_etherscan_log_query_keeps_the_topic_filters_and_still_checks_the_rows():
    rows = [_topic_log(1, T1, T2), _topic_log(2, T2, T2)]
    client = EtherscanClient("k", pause=0)
    client.session = _Recorded({"status": "1", "message": "OK", "result": rows})
    got = client.get_logs("0xAbC", "0xt0", 1, 2, topics={1: T1, 2: T2})
    assert [r["transactionHash"] for r in got] == ["0x0001"]
    assert client.session.sent[0]["topic1"] == T1


def test_log_rows_without_topics_under_a_topic_filter_are_a_failed_lookup():
    rows = [{"transactionHash": "0x01", "logIndex": "0x0", "blockNumber": "0x65"}]
    client = EtherscanClient("k", pause=0)
    client.session = _Recorded({"status": "1", "message": "OK", "result": rows})
    with pytest.raises(ApiError):
        client.get_logs("0xAbC", "0xt0", 1, 2, topics={1: T1})
