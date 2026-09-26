"""API client: paged queries and token-transfer helpers."""

import pytest
import requests

from tornado_demix.errors import ApiError
from tornado_demix.etherscan import MAX_BLOCK, EtherscanClient

DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
WALLET = "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"
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


def test_tx_sender_swallows_a_proxy_error_as_none():
    """tx_sender's contract is None on failure, so an ApiError becomes None."""
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid argument"}}
    client = _proxy_client([err, err, err])
    assert client.tx_sender("0xbad") is None


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
    assert [q["fromBlock"] for q in client.queries if q["page"] == 1] == [0, 16]


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
