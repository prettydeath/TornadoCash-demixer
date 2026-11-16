"""Failed transactions move nothing, and a partly indexed explorer answer is incomplete.
Synthetic addresses only."""

from tornado_demix import incomplete
from tornado_demix.deposit_addresses import (
    check_text,
    classify_address,
    classify_deposit_address,
    depositor_deposit_addresses,
    transfers,
)
from tornado_demix.etherscan import EtherscanClient
from tornado_demix.report_csv import _lookup_errors_csv
from tornado_demix.report_html import failed_parts, incomplete_phrase
from tornado_demix.trace import trace_funds
from tornado_demix.txstatus import PARTIAL_INDEXING, failed

W = "0x" + "a" * 40
S = "0x" + "b" * 40
X = "0x" + "c" * 40
HOT = "0x" + "d" * 40
LAB = {HOT: {"category": "exchange", "entity": "Fixture Exchange", "source": "fixture"}}


def _tx(frm, to, value, ts, h, is_error="0", status="1"):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts),
        "hash": h,
        "isError": is_error,
        "txreceipt_status": status,
    }


class Client:
    def __init__(self, lists):
        self.lists = lists
        self.truncated = []

    def call(self, p):
        return self.lists.get((p["action"], p["address"]), [])

    def has_at_least_txs(self, a, n):
        return False


# --- the shared predicate -------------------------------------------------------------


def test_failed_reads_both_status_fields():
    assert failed({"isError": "1"})
    assert failed({"txreceipt_status": "0"})
    assert failed({"isError": 1, "txreceipt_status": "1"})
    assert not failed({"isError": "0", "txreceipt_status": "1"})
    assert not failed({})  # tokentx rows carry neither field: a log only exists on success
    assert not failed({"txreceipt_status": ""})  # pre-Byzantium rows leave it empty
    assert not failed({"isError": "0"})  # an internal row carries only isError


def test_a_receipt_status_only_failure_moves_nothing_in_a_trace():
    a, b, c = X, HOT, S
    normal = [
        _indexed(_tx(W, a, 10**18, 1, "0x1")),
        _indexed(_tx(a, b, 10**18, 3, "0x3", "0", "0")),  # reverted, isError left 0
        _indexed(_tx(a, c, 10**18, 4, "0x4")),
    ]
    out = trace_funds(TraceClient(normal), a, 1, max_hops=1, arrival_tx="0x1")
    assert [e["to"] for e in out["edges"]] == [c]


def test_a_receipt_status_only_failure_is_no_native_deposit():
    from tornado_demix.demix import _native_deposits
    from tornado_demix.networks import load_networks

    eth = load_networks()["ethereum"]
    pool = next(p for p in eth.pools if p.is_native)
    row = _tx(W, pool.address, int(pool.denom * 10**18), 10, "0x9", "0", "0")
    assert _native_deposits(W, [row], eth) == []
    assert len(_native_deposits(W, [dict(row, txreceipt_status="1")], eth)) == 1


def test_failed_sends_are_kept_on_an_unknown_result():
    lists = {
        ("txlist", X): [
            _tx(W, X, 10**18, 1000, "0x01"),
            _tx(X, HOT, 10**18, 2000, "0x02"),  # forwards what arrived
            _tx(X, HOT, 10**17, 2100, "0x03", "1", "0"),
        ]
    }
    unlabelled = {}

    def busy(target):
        return None  # the busy check of the sweep target failed

    info = classify_deposit_address(Client(lists), X, lambda a: False, unlabelled, busy)
    assert info["kind"] == "unknown" and info["failed_sends"] == 1
    assert "1 failed transaction(s) sent" in check_text(info)


# --- failed transactions in the deposit-address classifier -----------------------------------------------


def _x_history(sweep_error="0", sweep_status="1"):
    return {
        ("txlist", X): [
            _tx(W, X, 10**18, 1000, "0x01"),
            # forwards what arrived, less 0.001 ETH
            _tx(X, HOT, 10**18 - 10**15, 2000, "0x02", sweep_error, sweep_status),
        ]
    }


def test_a_failed_outgoing_sweep_to_an_exchange_is_not_an_exchange_deposit():
    for err, status in (("1", "0"), ("1", "1"), ("0", "0")):
        client = Client(_x_history(err, status))
        assert classify_deposit_address(client, X, lambda a: False, LAB, lambda a: False) is None
        info, errors = classify_address(client, X, LAB, lambda a: False)
        assert info["kind"] == "not_deposit" and errors == []
        assert info["failed_sends"] == 1  # recorded apart, never as movement
        assert "1 failed transaction(s) sent" in check_text(info)


def test_a_successful_sweep_to_an_exchange_still_classifies():
    client = Client(_x_history())
    info = classify_deposit_address(client, X, lambda a: False, LAB, lambda a: False)
    assert info["kind"] == "exchange_deposit" and info["evidence"] is True
    assert info["sweep_target"] == HOT and info["senders"] == [W]
    assert "failed_sends" not in info


def test_a_failed_send_next_to_real_sweeps_is_counted_apart():
    lists = _x_history()
    lists[("txlist", X)].append(_tx(X, HOT, 10**17, 2100, "0x03", "1", "0"))
    info = classify_deposit_address(Client(lists), X, lambda a: False, LAB, lambda a: False)
    assert info["kind"] == "exchange_deposit" and info["failed_sends"] == 1
    assert check_text(info).endswith("1 failed transaction(s) sent, moved nothing, not counted")


def test_a_failed_incoming_transfer_makes_no_sender():
    lists = {
        ("txlist", X): [
            _tx(S, X, 10**18, 1000, "0x01"),
            _tx(W, X, 10**18, 1001, "0x02", "1", "0"),  # reverted: nothing arrived
            _tx(X, HOT, 10**18, 1100, "0x03"),
        ]
    }
    info = classify_deposit_address(Client(lists), X, lambda a: False, LAB, lambda a: False)
    assert info["kind"] == "exchange_deposit" and info["senders"] == [S]


def test_only_failed_incoming_transfers_make_no_deposit_address():
    lists = {
        ("txlist", X): [
            _tx(W, X, 10**18, 1000, "0x01", "1", "0"),
            _tx(X, HOT, 10**18, 1100, "0x03"),
        ]
    }
    assert classify_deposit_address(Client(lists), X, lambda a: False, LAB, lambda a: False) is None


def test_transfers_drop_failed_rows():
    ok = _tx(W, X, 10**18, 1000, "0x01")
    bad = _tx(W, S, 10**18, 1001, "0x02", "1", "0")
    assert transfers(W, [ok, bad], []) == [ok]


def test_a_failed_send_of_the_depositor_makes_no_candidate():
    lists = _x_history()
    client = Client(lists)
    txs = [_tx(W, X, 10**18, 1000, "0x01", "1", "0")]
    assert depositor_deposit_addresses(client, W, txs, lambda a: False, LAB) == []
    ok = [_tx(W, X, 10**18, 1000, "0x01")]
    (found,) = depositor_deposit_addresses(client, W, ok, lambda a: False, LAB)
    assert found["address"] == X


# --- partial indexing -----------------------------------------------------------------

PARTIAL = {
    "status": "2",
    "message": "Some internal transactions within this block range have not yet been processed",
    "result": [{"hash": "0x" + "a" * 64, "blockNumber": "1"}],
}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        return _Resp(self.payload)


def _partial_client():
    client = EtherscanClient(
        "", pause=0, retries=1, base_url="https://explorer.invalid/api", style="compat"
    )
    client.session = _Session(PARTIAL)
    return client


def test_a_partial_answer_returns_its_rows_and_is_recorded():
    client = _partial_client()
    query = {"module": "account", "action": "txlistinternal", "address": X}
    assert client.call(dict(query)) == PARTIAL["result"]
    assert client.truncated == [
        {
            "action": "txlistinternal",
            "address": X,
            "block": None,
            "limit": None,
            "reason": PARTIAL_INDEXING,
        }
    ]
    assert client.stats()["partial_indexing"] == 1
    # the later pages of one lookup add nothing
    client.call(dict(query, page=2))
    assert len(client.truncated) == 1 and client.stats()["partial_indexing"] == 1


def test_every_run_on_the_same_client_records_its_partial_lookup():
    client = _partial_client()
    query = {"module": "account", "action": "txlistinternal", "address": X, "page": 1}
    first = incomplete.mark(client)
    client.call(dict(query))
    run1 = incomplete.truncated_since(client, first)
    second = incomplete.mark(client)
    client.call(dict(query))
    run2 = incomplete.truncated_since(client, second)
    assert run1 and run2 and run1[0]["reason"] == run2[0]["reason"] == PARTIAL_INDEXING
    assert client.stats()["partial_indexing"] == 2


def test_a_partial_lookup_makes_a_run_incomplete_with_its_own_words():
    client = _partial_client()
    client.call({"module": "account", "action": "txlistinternal", "address": X})
    result = incomplete.attach({}, [], client.truncated)
    text = incomplete.summary(result)
    assert "partial indexing" in text and "results may be incomplete" in text
    assert "cut off" not in text
    assert not incomplete.notice_only(result)
    (line,) = incomplete.lines(result)
    assert "partial indexing" in line and X in line
    assert failed_parts(result) == (0, 1)  # blocks a clean data check like a cut-off list
    assert "partial indexing" in incomplete_phrase(result)


def test_a_partial_lookup_is_listed_in_the_lookup_errors_csv(tmp_path):
    client = _partial_client()
    client.call({"module": "account", "action": "txlistinternal", "address": X})
    result = incomplete.attach({"wallet": W}, [], client.truncated)
    (path,) = _lookup_errors_csv([result], str(tmp_path))
    text = open(path, encoding="utf-8").read()
    assert "partial_indexing" in text and X in text


class TraceClient:
    """A stub explorer; ``partial`` maps an address to the entry a real client records
    when its internal list comes back partly indexed."""

    def __init__(self, normal, partial=None):
        self.normal, self.partial, self.truncated = list(normal), dict(partial or {}), []

    def outgoing_txs(self, addr):
        return [r for r in self.normal if addr in (r["from"], r["to"])]

    def internal_txs(self, addr):
        if addr in self.partial:
            self.truncated.append(dict(self.partial[addr]))
        return []

    def token_transfers(self, addr, contract=None, **kw):
        return []


def _indexed(row):
    return dict(row, transactionIndex="0", input="0x")


def test_a_trace_never_calls_a_partly_indexed_ledger_certain():
    a, b, c = X, HOT, S
    normal = [
        _indexed(_tx(W, a, 10**18, 1, "0x1")),
        _indexed(_tx(a, b, 10**18, 3, "0x3")),
        _indexed(_tx(b, c, 10**18, 4, "0x4")),
    ]
    clean = trace_funds(TraceClient(normal), a, 1, max_hops=3, arrival_tx="0x1")
    assert clean["edges"][0]["status"].startswith("certain")
    source = _partial_client()
    source.call({"module": "account", "action": "txlistinternal", "address": a})
    (entry,) = source.truncated
    out = trace_funds(TraceClient(normal, {a: entry}), a, 1, max_hops=3, arrival_tx="0x1")
    assert out["edges"] and not any(e["status"].startswith("certain") for e in out["edges"])
    assert "partial indexing" in incomplete.summary(out)


def test_failed_sends_of_an_unknown_counterparty_reach_the_run_errors():
    lists = {
        ("txlist", X): [
            _tx(W, X, 10**18, 1000, "0x01"),
            _tx(X, HOT, 10**18, 2000, "0x02"),
            _tx(X, HOT, 10**17, 2100, "0x03", "1", "0"),
        ]
    }

    class FailingBusy(Client):
        def has_at_least_txs(self, a, n):
            from tornado_demix.errors import ApiError

            raise ApiError("Max rate limit reached")

    errors = []
    ok = [_tx(W, X, 10**18, 1000, "0x01")]
    assert (
        depositor_deposit_addresses(FailingBusy(lists), W, ok, lambda a: False, {}, errors=errors)
        == []
    )
    (check,) = [e for e in errors if e["what"] == "deposit address check"]
    assert "1 failed transaction(s) sent, not counted" in check["error"]


def test_the_classify_report_shows_failed_sends_of_a_non_deposit_address():
    from tests.fixtures import demix_result as fx
    from tornado_demix.report import build_classify_report

    info, _errors = classify_address(Client(_x_history("1", "0")), X, LAB, lambda a: False)
    html = build_classify_report(info, fx.network())
    assert "1 failed transaction(s) sent, moved nothing, not counted" in html
