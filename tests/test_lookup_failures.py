"""A failed lookup is an explicit state, never "nothing found" (demix, trace, reports)."""

import random

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix import incomplete
from tornado_demix.deposit_addresses import (
    classify_deposit_address,
    depositor_deposit_addresses,
)
from tornado_demix.errors import ApiError, ApiKeyError
from tornado_demix.trace import trace_funds

WALLET = "0x" + "a" * 40
EXIT = "0x" + "b" * 40
DEPOSIT = "0x" + "c" * 40
HOT = "0x" + "d" * 40
OTHER = "0x" + "e" * 40
HOP1 = "0x" + "1" * 40
HOP2 = "0x" + "2" * 40
HOP3 = "0x" + "3" * 40
POOL = "0x" + "9" * 40
WEI = 10**18


# ---------------------------------------------------------------- the record


def test_the_record_has_one_shape_and_a_short_error_text():
    rec = incomplete.lookup_failed("history", ApiError("x" * 500), address=HOP1)
    assert rec["kind"] == "lookup_failed" and rec["what"] == "history"
    assert rec["address"] == HOP1 and "tx" not in rec
    assert len(rec["error"]) <= incomplete.ERROR_TEXT
    rec = incomplete.lookup_failed("receipt", "timeout", tx="0x" + "f" * 64)
    assert rec["tx"] == "0x" + "f" * 64 and rec["error"] == "timeout"


def test_the_summary_counts_failures_and_cut_off_lists():
    assert incomplete.summary({}) == ""
    assert incomplete.summary({"errors": [], "truncated": []}) == ""
    one = {"errors": [incomplete.lookup_failed("history", "timeout", address=HOP1)]}
    assert incomplete.summary(one) == "1 lookup failed — results may be incomplete"
    two = {
        "errors": [
            incomplete.lookup_failed("history", "timeout", address=HOP1),
            incomplete.lookup_failed("history", "timeout", address=HOP2),
        ],
        "truncated": [{"action": "txlist", "address": HOP1, "limit": 10000, "block": None}],
    }
    text = incomplete.summary(two, dash="-")
    assert text == "2 lookups failed, 1 list cut off by the explorer - results may be incomplete"
    lines = incomplete.lines(two)
    assert len(lines) == 3 and HOP1 in lines[0] and "timeout" in lines[0]
    assert "txlist" in lines[2] and "10000" in lines[2]


def test_attach_adds_errors_count_and_truncated():
    res = incomplete.attach({}, [incomplete.lookup_failed("a", "b")], [{"action": "x"}])
    assert (
        res["error_count"] == 1
        and len(res["errors"]) == 1
        and res["truncated"] == [{"action": "x"}]
    )


def test_truncated_entries_are_per_run():
    class C:
        truncated = [{"action": "txlist", "address": "old"}]

    c = C()
    mark = incomplete.mark(c)
    c.truncated.append({"action": "tokentx", "address": "new"})
    assert incomplete.truncated_since(c, mark) == [{"action": "tokentx", "address": "new"}]
    assert incomplete.mark(object()) == 0 and incomplete.truncated_since(object(), 0) == []


# ---------------------------------------------------------------- classifier


def _tx(frm, to, value=WEI, block=100, h=None):
    return {
        "from": frm,
        "to": to,
        "hash": h or "0x" + frm[-4:] + to[-4:] + format(block, "x"),
        "value": str(value),
        "blockNumber": str(block),
    }


def _deposit_rows():
    rows = [_tx(WALLET, DEPOSIT), _tx(EXIT, DEPOSIT, block=99)]
    rows += [_tx(DEPOSIT, HOT, value=WEI - 10**15, block=101 + i) for i in range(3)]
    return rows


class Lists:
    def __init__(self, lists, fail=()):
        self.lists, self.fail = lists, set(fail)

    def call(self, params):
        if params["address"] in self.fail:
            raise ApiError("Max rate limit reached")
        return self.lists.get((params["action"], params["address"]), [])


def test_a_history_error_is_unknown_not_none():
    client = Lists({}, fail={DEPOSIT})
    info = classify_deposit_address(client, DEPOSIT, busy=lambda a: True)
    assert info["kind"] == "unknown" and "rate limit" in info["error"]


def test_a_failed_busy_check_that_decides_the_outcome_is_unknown():
    client = Lists({("txlist", DEPOSIT): _deposit_rows()})
    info = classify_deposit_address(client, DEPOSIT, busy=lambda a: None)
    assert info["kind"] == "unknown" and HOT in info["error"]
    # the same address with a working check qualifies, a clean "no" stays None
    assert classify_deposit_address(client, DEPOSIT, busy=lambda a: True)["kind"] == (
        "deposit_like"
    )
    assert classify_deposit_address(client, DEPOSIT, busy=lambda a: False) is None


def test_a_failed_busy_check_that_cannot_change_the_outcome_is_ignored():
    client = Lists({("txlist", DEPOSIT): _deposit_rows()})
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    info = classify_deposit_address(client, DEPOSIT, labels=labels, busy=lambda a: None)
    assert info["kind"] == "exchange_deposit"


def test_a_failed_contract_check_on_the_target_is_unknown():
    def check(addr):
        return None

    check.failed = {HOT}
    client = Lists({("txlist", DEPOSIT): _deposit_rows()})
    info = classify_deposit_address(client, DEPOSIT, is_contract=check, busy=lambda a: True)
    assert info["kind"] == "unknown"
    # without an endpoint (nothing failed) an unknown status stays "not an EOA"
    check.failed = set()
    assert classify_deposit_address(client, DEPOSIT, is_contract=check, busy=lambda a: True) is None


def test_unknown_counterparties_become_error_records_not_deposit_addresses():
    class Client(Lists):
        def has_at_least_txs(self, address, n):
            raise ApiError("refused 10000-row page")

    client = Client({("txlist", DEPOSIT): _deposit_rows()}, fail={OTHER})
    errors = []
    txs = [_tx(WALLET, OTHER), _tx(WALLET, DEPOSIT)]
    found = depositor_deposit_addresses(client, WALLET, txs, errors=errors)
    assert found == []
    assert {e["address"] for e in errors} >= {OTHER, DEPOSIT, HOT}
    assert all(e["kind"] == "lookup_failed" and e["what"] and e["error"] for e in errors)


def test_a_failed_busy_check_is_retried_on_the_next_use():
    calls = []

    class Client(Lists):
        def has_at_least_txs(self, address, n):
            calls.append(address)
            if len(calls) == 1:
                raise ApiError("timeout")
            return True

    other = "0x" + "4" * 40
    lists = {("txlist", DEPOSIT): _deposit_rows()}
    lists[("txlist", other)] = [_tx(WALLET, other), _tx(EXIT, other, block=99)] + [
        _tx(other, HOT, value=WEI - 10**15, block=101 + i, h="0x9%d" % i) for i in range(3)
    ]
    errors = []
    txs = [_tx(WALLET, DEPOSIT), _tx(WALLET, DEPOSIT, block=5), _tx(WALLET, other)]
    found = depositor_deposit_addresses(Client(lists), WALLET, txs, errors=errors)
    assert calls == [HOT, HOT]  # the failure was not cached
    assert [f["address"] for f in found] == [other]
    assert len(errors) == 2  # the busy check, and the deposit address it left unknown


def test_an_api_key_error_still_stops_the_run():
    class Client(Lists):
        def call(self, params):
            raise ApiKeyError("Invalid API Key")

    with pytest.raises(ApiKeyError):
        depositor_deposit_addresses(Client({}), WALLET, [_tx(WALLET, OTHER)], errors=[])


# ---------------------------------------------------------------- rpc


def test_a_failed_contract_lookup_is_not_cached_and_is_recorded(monkeypatch):
    from tornado_demix import rpc

    answers = {"0xc": [None, "0x6080"]}

    class Client:
        def call(self, method, params):
            return answers[params[0]].pop(0)

    monkeypatch.setattr(rpc, "RpcClient", lambda url: Client())
    check = rpc.make_contract_check("https://rpc.example")
    assert check("0xc") is None and "0xc" in check.failed
    assert check.errors and check.errors[0]["address"] == "0xc"
    assert check("0xc") is True and "0xc" not in check.failed
    assert len(check.errors) == 1
    blank = rpc.make_contract_check("")
    assert blank("0xc") is None and blank.failed == set() and blank.errors == []


def test_a_failed_base_fee_lookup_is_not_cached_and_is_recorded(monkeypatch):
    from tornado_demix import rpc

    answers = [None, {"baseFeePerGas": None}]

    class Client:
        def base_fee(self, block):
            got = answers.pop(0)
            return rpc.UNKNOWN if got is None else got["baseFeePerGas"]

    monkeypatch.setattr(rpc, "RpcClient", lambda url: Client())
    gate = rpc.make_gas_price_gate("https://rpc.example")
    assert gate(10) is False and len(gate.errors) == 1
    assert gate(10) is True  # retried: pre-London, the sender chose the price
    assert len(gate.errors) == 1


# ---------------------------------------------------------------- run_demix


def _demix_client(fail_busy=True, fail_history=True):
    from tests.test_demix import _FakeClient, _recipient, _tx, _wlog
    from tornado_demix.constants import ZERO_ADDRESS

    deposits = [_tx(fx.POOL_01_ETH, 0.1, 1000 + i * 10, 10 + i, "0xdep%d" % i) for i in range(3)]
    deposits.append(_tx(OTHER, 1.0, 900, 9, "0xpay"))
    logs = [_wlog(_recipient(1), format(9000 + k, "064x"), ZERO_ADDRESS, 0) for k in range(3)]

    class Client(_FakeClient):
        truncated = [{"action": "txlist", "address": "stale", "limit": 1, "block": None}]

        def call(self, params):
            if fail_history:
                raise ApiError("Max rate limit reached")
            return []

        def has_at_least_txs(self, address, n):
            if fail_busy:
                raise ApiError("refused 10000-row page")
            return False

    return Client(deposits, logs)


def test_demix_completes_and_lists_every_failed_lookup():
    from tornado_demix.demix import run_demix

    data = run_demix(_demix_client(), fx.WALLET, network=fx.network())
    assert data["denoms"]  # the run finished
    whats = {(e["what"], e.get("address")) for e in data["errors"]}
    assert ("deposit address check", OTHER) in whats
    assert any(w.startswith("linked_sender") and a == OTHER for w, a in whats)
    assert data["error_count"] == len(data["errors"])
    assert data["truncated"] == []  # the stale entry belongs to an earlier run


def test_a_clean_demix_has_an_empty_error_list():
    from tornado_demix.demix import run_demix

    data = run_demix(
        _demix_client(fail_busy=False, fail_history=False), fx.WALLET, network=fx.network()
    )
    assert data["errors"] == [] and data["error_count"] == 0


def test_a_demix_without_deposits_carries_the_fields_too():
    from tests.test_demix import _FakeClient
    from tornado_demix.demix import run_demix

    data = run_demix(_FakeClient([], []), fx.WALLET, network=fx.network())
    assert data["errors"] == [] and data["error_count"] == 0 and data["truncated"] == []


def test_multi_collects_the_errors_of_every_wallet():
    from tornado_demix.multi import correlate

    corr = correlate(_demix_client(), [fx.WALLET], network=fx.network())
    assert corr["error_count"] == len(corr["errors"]) > 0
    assert all(e["wallet"] == fx.WALLET for e in corr["errors"])


def test_characterize_carries_the_fields():
    from tornado_demix.characterize import characterize_address

    class C:
        truncated = []

        def outgoing_txs(self, a):
            self.truncated.append({"action": "txlist", "address": a, "limit": 1, "block": None})
            return []

        def internal_txs(self, a):
            return []

    info = characterize_address(C(), EXIT, fx.network())
    assert info["errors"] == [] and info["error_count"] == 0 and len(info["truncated"]) == 1


# ---------------------------------------------------------------- trace


def _t(frm, to, eth, block, h, data="0x"):
    return {
        "from": frm,
        "to": to,
        "value": str(int(eth * WEI)),
        "blockNumber": str(block),
        "timeStamp": str(block * 12),
        "hash": h,
        "input": data,
        "isError": "0",
        "transactionIndex": "0",
    }


class TraceClient:
    def __init__(self, normal=(), fail=(), fail_internal=()):
        self.normal, self.fail, self.fail_internal = list(normal), set(fail), set(fail_internal)
        self.truncated = []

    def outgoing_txs(self, addr):
        if addr in self.fail:
            raise ApiError("Max rate limit reached")
        return [r for r in self.normal if addr in (r["from"], r["to"])]

    def internal_txs(self, addr):
        if addr in self.fail_internal:
            raise ApiError("timeout")
        return []

    def token_transfers(self, addr, contract=None, **kw):
        return []


def _ends(out):
    return {(t["address"], t["reason"]): t["amount"] for t in out["terminals"]}


def test_an_api_error_on_one_branch_ends_only_that_branch():
    client = TraceClient(
        normal=[
            _t(EXIT, HOP1, 2, 10, "0xa"),
            _t(EXIT, HOP2, 3, 11, "0xb"),
            _t(HOP2, HOP3, 3, 20, "0xc"),
        ],
        fail={HOP1},
    )
    out = trace_funds(client, EXIT, 5)
    ends = _ends(out)
    assert ends[(HOP1, "lookup_failed")] == 2
    assert ends[(HOP3, "not moved on (held or untraced)")] == 3  # the other branch went on
    assert [e["address"] for e in out["errors"]] == [HOP1]
    assert out["errors"][0]["kind"] == "lookup_failed" and out["error_count"] == 1


def test_an_api_error_on_the_start_address_keeps_the_amount_and_returns():
    out = trace_funds(TraceClient(fail={EXIT}), EXIT, 5)
    assert _ends(out) == {(EXIT, "lookup_failed"): 5}
    assert out["edges"] == [] and out["error_count"] == 1


def test_an_unreadable_internal_list_is_recorded():
    client = TraceClient(normal=[_t(EXIT, HOP1, 2, 10, "0xa")], fail_internal={HOP1})
    out = trace_funds(client, EXIT, 2)
    assert any(e["address"] == HOP1 and "internal" in e["what"] for e in out["errors"])


def test_an_api_key_error_still_stops_the_trace():
    class Bad(TraceClient):
        def outgoing_txs(self, addr):
            if addr == HOP1:
                raise ApiKeyError("Invalid API Key")
            return super().outgoing_txs(addr)

    with pytest.raises(ApiKeyError):
        trace_funds(Bad(normal=[_t(EXIT, HOP1, 2, 10, "0xa")]), EXIT, 2)


def test_branches_below_the_threshold_end_with_their_amount():
    client = TraceClient(
        normal=[
            _t(EXIT, HOP1, 99.5, 10, "0xa"),
            _t(EXIT, HOP2, 0.5, 11, "0xb"),  # 0.5 % of the start: not followed
            _t(HOP2, HOP3, 0.5, 20, "0xc"),
        ]
    )
    out = trace_funds(client, EXIT, 100)
    ends = _ends(out)
    assert ends[(HOP2, "below_threshold")] == 0.5
    assert sum(t["amount"] for t in out["terminals"]) == pytest.approx(100)


def test_a_small_remainder_held_at_an_address_is_below_threshold_not_dropped():
    client = TraceClient(normal=[_t(EXIT, HOP1, 99.6, 10, "0xa")])
    out = trace_funds(client, EXIT, 100)
    assert _ends(out)[(EXIT, "below_threshold")] == pytest.approx(0.4)
    assert sum(t["amount"] for t in out["terminals"]) == pytest.approx(100)


def test_outflows_of_an_address_holding_less_than_the_threshold_end_there():
    client = TraceClient(
        normal=[
            _t(EXIT, HOP1, 99.2, 10, "0xa"),
            _t(EXIT, HOP2, 0.4, 11, "0xb"),
            _t(EXIT, HOP3, 0.4, 12, "0xc"),
        ]
    )
    out = trace_funds(client, EXIT, 100)
    ends = _ends(out)
    assert ends[(HOP2, "below_threshold")] == pytest.approx(0.4)
    assert ends[(HOP3, "below_threshold")] == pytest.approx(0.4)
    assert sum(t["amount"] for t in out["terminals"]) == pytest.approx(100)


def test_a_redeposit_into_tornado_is_named_whatever_its_size():
    client = TraceClient(
        normal=[
            _t(EXIT, HOP1, 99.9, 10, "0xa"),
            _t(EXIT, POOL, 0.1, 11, "0xb", data="0xb214faa5"),  # deposit(): a contract call
            _t(HOP1, POOL, 10, 20, "0xc", data="0xb214faa5"),
        ]
    )
    out = trace_funds(client, EXIT, 100, tornado={POOL}, labels={POOL: {"label": "Tornado"}})
    assert _ends(out)[(POOL, "tornado_redeposit")] == pytest.approx(10.1)
    assert sum(t["amount"] for t in out["terminals"]) == pytest.approx(100)


def test_the_registry_names_tornado_pools_on_a_known_chain():
    from tornado_demix.networks import load_networks

    eth = load_networks()["ethereum"]
    pool = next(iter(eth.entry_points))
    client = TraceClient(normal=[_t(EXIT, pool, 1, 10, "0xa", data="0xb214faa5")])
    out = trace_funds(client, EXIT, 1, chain_id=1)
    assert _ends(out) == {(pool, "tornado_redeposit"): 1}


def test_trace_truncated_entries_are_per_run():
    client = TraceClient(normal=[_t(EXIT, HOP1, 1, 10, "0xa")])
    client.truncated.append({"action": "txlist", "address": "stale", "limit": 1, "block": None})
    out = trace_funds(client, EXIT, 1)
    assert out["truncated"] == []


def _random_case(rng):
    addrs = [EXIT] + ["0x" + format(i, "040x") for i in range(1, 9)]
    rows, block = [], 10
    for n in range(rng.randint(1, 14)):
        frm = rng.choice(addrs[:6])
        to = rng.choice([a for a in addrs if a != frm])
        rows.append(_t(frm, to, rng.choice([0.05, 0.3, 1, 2, 4]), block, "0x%x" % (n + 1)))
        block += rng.randint(0, 2)
    fail = set(rng.sample(addrs[1:], rng.randint(0, 2)))
    return rows, fail


def test_the_end_amounts_add_up_to_the_start_amount():
    rng = random.Random(20261113)
    for _ in range(400):
        rows, fail = _random_case(rng)
        start = rng.choice([1, 5, 10])
        inflow = _t("0x" + "f" * 40, EXIT, start, 1, "0xstart")
        client = TraceClient(normal=[inflow] + rows, fail=fail)
        out = trace_funds(client, EXIT, start, max_hops=rng.randint(1, 5), max_nodes=6)
        total = sum(t["amount"] for t in out["terminals"])
        assert total == pytest.approx(start, rel=1e-6, abs=len(out["terminals"]) * 1e-6), (
            rows,
            fail,
            out["terminals"],
        )
