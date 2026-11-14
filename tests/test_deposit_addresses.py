"""Exchange deposit addresses shared by a depositor and a withdrawal recipient."""

from tornado_demix.deposit_addresses import (
    FORWARD_SECONDS,
    HOT_WALLET_TXS,
    MAX_DEPOSIT_SENDERS,
    MAX_DEPOSIT_TXS,
    classify_deposit_address,
    depositor_deposit_addresses,
    forwards_to,
    shared_deposit_hits,
)
from tornado_demix.errors import ApiError

WALLET = "0x" + "a" * 40
EXIT = "0x" + "b" * 40
DEPOSIT = "0x" + "c" * 40
HOT = "0x" + "d" * 40
OTHER = "0x" + "e" * 40


def tx(frm, to, value=10**18, block=100, ts=None, h=None):
    row = {
        "from": frm,
        "to": to,
        "hash": h or "0x" + frm[-4:] + to[-4:],
        "value": str(value),
        "blockNumber": str(block),
    }
    if ts is not None:
        row["timeStamp"] = str(ts)
    return row


TOKEN = "0x" + "7" * 40


def tok(frm, to, value=10**18, block=100, ts=None, h=None, contract=TOKEN):
    row = tx(frm, to, value, block, ts, h)
    row["contractAddress"] = contract
    return row


class FakeClient:
    """Serves account lists by (action, address); has_at_least_txs by a set."""

    def __init__(self, lists, busy=()):
        self.lists = lists
        self.busy = set(busy)
        self.calls = []

    def call(self, params):
        self.calls.append((params["action"], params["address"]))
        return self.lists.get((params["action"], params["address"]), [])

    def has_at_least_txs(self, address, n):
        assert n == HOT_WALLET_TXS
        return address in self.busy


def deposit_lists(senders=(WALLET, EXIT), target=HOT, sweeps=3):
    rows = [tx(s, DEPOSIT) for s in senders] + [
        tx(DEPOSIT, target, value=10**18 - 10**15, block=101) for _ in range(sweeps)
    ]
    return {("txlist", DEPOSIT): rows}


def test_a_swept_address_with_few_senders_and_a_busy_target_is_a_deposit_address():
    client = FakeClient(deposit_lists(), busy={HOT})
    info = classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__)
    assert info["sweep_target"] == HOT
    assert info["senders"] == sorted([WALLET, EXIT])


def test_an_exchange_label_on_the_target_is_enough_without_the_busy_check():
    client = FakeClient(deposit_lists())
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    info = classify_deposit_address(client, DEPOSIT, labels=labels, busy=lambda a: False)
    assert info["exchange"] == "binance"


def test_a_quiet_unlabelled_target_is_not_a_hot_wallet():
    client = FakeClient(deposit_lists())
    assert classify_deposit_address(client, DEPOSIT, busy=lambda a: False) is None
    # without a busy check or a label nothing qualifies
    assert classify_deposit_address(client, DEPOSIT) is None


def test_too_many_senders_or_rows_or_no_dominant_sweep_is_not_a_deposit_address():
    many = [f"0x{i:040x}" for i in range(MAX_DEPOSIT_SENDERS + 1)]
    client = FakeClient(deposit_lists(senders=many), busy={HOT})
    assert classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__) is None

    rows = [tx(WALLET, DEPOSIT)] * MAX_DEPOSIT_TXS
    client = FakeClient({("txlist", DEPOSIT): rows}, busy={HOT})
    assert classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__) is None

    split = [tx(WALLET, DEPOSIT), tx(DEPOSIT, HOT), tx(DEPOSIT, OTHER)]
    client = FakeClient({("txlist", DEPOSIT): split}, busy={HOT})
    assert classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__) is None


def test_a_contract_is_never_a_deposit_address():
    client = FakeClient(deposit_lists(), busy={HOT})
    # a contract is still read, but only a smart account / forwarder can come out of it
    assert classify_deposit_address(client, DEPOSIT, is_contract=lambda a: True) is None


def test_depositor_scan_skips_labelled_and_excluded_counterparties():
    lists = deposit_lists()
    client = FakeClient(lists, busy={HOT})
    txs = [tx(WALLET, DEPOSIT), tx(WALLET, OTHER), tx(WALLET, HOT)]
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    found = depositor_deposit_addresses(client, WALLET, txs, labels=labels, exclude={OTHER})
    assert [f["address"] for f in found] == [DEPOSIT]
    assert ("txlist", OTHER) not in client.calls and ("txlist", HOT) not in client.calls


def test_shared_hits_list_recipients_that_sent_to_the_same_deposit_address():
    found = [{"address": DEPOSIT, "senders": [WALLET, EXIT]}]
    assert shared_deposit_hits(found, {EXIT, OTHER}, WALLET) == {EXIT: [DEPOSIT]}
    assert shared_deposit_hits(found, {WALLET}, WALLET) == {}


def test_sweeps_split_over_several_hot_wallets_still_count():
    hot2 = "0x" + "f" * 40
    fwd = 10**18 - 10**15
    rows = [
        tx(WALLET, DEPOSIT),
        tx(DEPOSIT, HOT, value=fwd, block=101),
        tx(DEPOSIT, hot2, value=fwd, block=102),
        tx(DEPOSIT, HOT, value=fwd, block=103),
    ]
    client = FakeClient({("txlist", DEPOSIT): rows}, busy={HOT, hot2})
    info = classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__)
    assert info["sweep_targets"] == [HOT, hot2]


def test_an_explorer_error_on_one_address_skips_it_without_failing_the_run():
    from tornado_demix.errors import ApiError

    class Flaky(FakeClient):
        def call(self, params):
            if params["address"] == OTHER:
                raise ApiError("html error page")
            return super().call(params)

        def has_at_least_txs(self, address, n):
            raise ApiError("page too large")

    lists = deposit_lists()
    client = Flaky(lists)
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    txs = [tx(WALLET, OTHER), tx(WALLET, DEPOSIT)]
    found = depositor_deposit_addresses(client, WALLET, txs, labels={**labels})
    # OTHER is skipped, DEPOSIT still qualifies through the exchange label
    assert [f["address"] for f in found] == [DEPOSIT]


def test_a_busy_contract_is_not_a_hot_wallet():
    # A token or router contract has millions of transactions but is not an exchange:
    # an address whose outflow goes to USDC or the Tornado router is not a deposit address.
    client = FakeClient(deposit_lists(), busy={HOT})
    info = classify_deposit_address(
        client, DEPOSIT, is_contract=lambda a: a == HOT, busy=client.busy.__contains__
    )
    assert info is None
    # a labelled exchange still counts, contract or not
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    info = classify_deposit_address(
        client, DEPOSIT, is_contract=lambda a: a == HOT, labels=labels, busy=lambda a: False
    )
    assert info["exchange"] == "binance"


def test_without_a_label_a_busy_target_must_receive_forwarded_amounts():
    # the wallet pays the busy address an amount it did not just receive: not a sweep
    rows = [
        tx(WALLET, DEPOSIT, value=10**18, block=100),
        tx(DEPOSIT, HOT, value=5 * 10**17, block=101),
    ]
    client = FakeClient({("txlist", DEPOSIT): rows}, busy={HOT})
    assert classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__) is None
    # forwarded too late (more than FORWARD_SECONDS after the incoming transfer)
    rows = [
        tx(WALLET, DEPOSIT, ts=1000),
        tx(DEPOSIT, HOT, value=10**18, ts=1000 + FORWARD_SECONDS + 1),
    ]
    client = FakeClient({("txlist", DEPOSIT): rows}, busy={HOT})
    assert classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__) is None
    # a labelled exchange needs no forwarding test
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    assert classify_deposit_address(client, DEPOSIT, labels=labels, busy=lambda a: False)


def test_only_deposit_addresses_swept_to_labelled_exchanges_count_as_evidence():
    # found by activity alone: shown, not scored (chance-level in the placebo test)
    client = FakeClient(deposit_lists(), busy={HOT})
    info = classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__)
    assert info["evidence"] is False
    assert shared_deposit_hits([info], {EXIT}, WALLET) == {}
    assert shared_deposit_hits([info], {EXIT}, WALLET, evidence_only=False) == {EXIT: [DEPOSIT]}
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    info = classify_deposit_address(client, DEPOSIT, labels=labels, busy=lambda a: False)
    assert info["evidence"] is True
    assert shared_deposit_hits([info], {EXIT}, WALLET) == {EXIT: [DEPOSIT]}


LABELS = {HOT: {"category": "exchange", "entity": "binance"}}


def info_of(lists, labels=None):
    client = FakeClient(lists, busy={HOT})
    return classify_deposit_address(client, DEPOSIT, labels=labels, busy=client.busy.__contains__)


def test_a_token_transfer_is_counted_once_not_as_txlist_call_plus_tokentx():
    # A token sweep is listed twice by the explorer: a txlist call to the token
    # contract (value 0) and the tokentx movement. The call is not a second
    # outgoing transfer, to the contract.
    fwd = 10**18 - 10**15
    lists = {
        ("txlist", DEPOSIT): [
            tx(DEPOSIT, TOKEN, value=0, h="0x02"),
            tx(DEPOSIT, TOKEN, value=0, h="0x03"),
        ],
        ("tokentx", DEPOSIT): [
            tok(WALLET, DEPOSIT, h="0x01"),
            tok(DEPOSIT, HOT, value=fwd, h="0x02"),
            tok(DEPOSIT, HOT, value=fwd, h="0x03"),
        ],
    }
    # counted twice, 2 of 4 outgoing rows would go to HOT (50 %) and it would fail
    info = info_of(lists, LABELS)
    assert info is not None and info["sweep_targets"] == [HOT]


def test_unknown_contract_status_is_not_a_hot_wallet():
    # A failed eth_getCode lookup (None) must not let a busy contract pass as a hot wallet.
    client = FakeClient(deposit_lists(), busy={HOT})
    info = classify_deposit_address(
        client, DEPOSIT, is_contract=lambda a: None, busy=client.busy.__contains__
    )
    assert info is None
    info = classify_deposit_address(
        client, DEPOSIT, is_contract=lambda a: False, busy=client.busy.__contains__
    )
    assert info is not None and info["sweep_target"] == HOT


def test_an_eth_value_row_to_a_token_contract_is_kept():
    fwd = 10**18 - 10**15
    lists = {
        ("txlist", DEPOSIT): [
            tx(WALLET, DEPOSIT, h="0x01"),
            tx(DEPOSIT, HOT, value=fwd, h="0x02"),
            tx(DEPOSIT, HOT, value=fwd, h="0x03"),
            tx(DEPOSIT, HOT, value=fwd, h="0x04"),
            tx(DEPOSIT, TOKEN, value=10**18, h="0x05"),  # ETH sent to a contract
        ],
    }
    assert info_of(lists, LABELS) is None  # 3 of 4 transfers (75 %) go to HOT
    del lists[("txlist", DEPOSIT)][-1]
    assert info_of(lists, LABELS) is not None


def test_zero_value_poisoning_transfers_are_ignored():
    spoof = "0x" + "9" * 40
    lists = deposit_lists()
    lists[("txlist", DEPOSIT)] += [tx(spoof, DEPOSIT, value=0, h="0xaa")]
    lists[("tokentx", DEPOSIT)] = [
        tok(spoof, DEPOSIT, value=0, h="0xab"),
        tok(DEPOSIT, OTHER, value=0, h="0xac"),
        tok(DEPOSIT, OTHER, value=0, h="0xad"),
    ]
    info = info_of(lists)
    assert info is not None
    assert info["senders"] == sorted([WALLET, EXIT])
    assert info["sweep_targets"] == [HOT]


def test_a_token_transfer_out_not_initiated_by_the_address_is_dropped():
    # transferFrom spoof: the explorer lists the transfer from DEPOSIT, but the
    # transaction was not sent by DEPOSIT (no txlist row of DEPOSIT carries it).
    lists = deposit_lists(sweeps=1)
    lists[("tokentx", DEPOSIT)] = [
        tok(DEPOSIT, OTHER, value=10**18, h=f"0xspoof{i}") for i in (1, 2, 3)
    ]
    info = info_of(lists)
    assert info is not None and info["sweep_targets"] == [HOT]
    # the same transfers sent by the address itself are counted: no dominant sweep
    lists[("txlist", DEPOSIT)] += [tx(DEPOSIT, TOKEN, value=0, h=f"0xspoof{i}") for i in (1, 2, 3)]
    assert info_of(lists) is None


def test_the_window_is_a_time_not_a_block_count():
    # Fast chains have many blocks in 38,400 s; the window is the duration.
    rows = [
        tx(WALLET, DEPOSIT, block=100, ts=1000),
        tx(DEPOSIT, HOT, value=10**18, block=100 + 50_000, ts=1000 + FORWARD_SECONDS),
    ]
    assert forwards_to(rows, DEPOSIT, HOT)
    rows[1]["timeStamp"] = str(1000 + FORWARD_SECONDS + 1)
    assert not forwards_to(rows, DEPOSIT, HOT)
    # without timestamps the fallback of 3200 blocks applies
    rows = [tx(WALLET, DEPOSIT, block=100), tx(DEPOSIT, HOT, value=10**18, block=100 + 3200)]
    assert forwards_to(rows, DEPOSIT, HOT)
    rows[1]["blockNumber"] = str(100 + 3201)
    assert not forwards_to(rows, DEPOSIT, HOT)


def test_an_aggregated_sweep_forwards_the_sum_of_several_deposits():
    rows = [
        tx(WALLET, DEPOSIT, value=4 * 10**17, ts=1000, block=10),
        tx(EXIT, DEPOSIT, value=6 * 10**17, ts=1100, block=11),
        tx(DEPOSIT, HOT, value=10**18 - 10**15, ts=1200, block=12),
    ]
    assert forwards_to(rows, DEPOSIT, HOT)
    rows[2]["value"] = str(10**18 - 2 * 10**16)  # short by more than 0.01 ETH
    assert not forwards_to(rows, DEPOSIT, HOT)
    rows[2]["value"] = str(2 * 10**18)  # the sum does not cover it
    assert not forwards_to(rows, DEPOSIT, HOT)


def test_an_aggregate_counts_only_deposits_after_the_previous_outgoing_transfer():
    rows = [
        tx(WALLET, DEPOSIT, value=4 * 10**17, ts=1000, block=10),
        tx(DEPOSIT, OTHER, value=4 * 10**17, ts=1050, block=11),  # spends the first deposit
        tx(EXIT, DEPOSIT, value=6 * 10**17, ts=1100, block=12),
        tx(DEPOSIT, HOT, value=10**18, ts=1200, block=13),
    ]
    assert not forwards_to(rows, DEPOSIT, HOT)
    del rows[1]  # without that spend the two deposits add up
    assert forwards_to(rows, DEPOSIT, HOT)


def test_an_aggregated_token_sweep_has_a_one_percent_tolerance():
    rows = [
        tok(WALLET, DEPOSIT, value=500, ts=1000, block=10),
        tok(EXIT, DEPOSIT, value=500, ts=1100, block=11),
        tok(DEPOSIT, HOT, value=995, ts=1200, block=12),
    ]
    assert forwards_to(rows, DEPOSIT, HOT)
    rows[2]["value"] = "980"
    assert not forwards_to(rows, DEPOSIT, HOT)
    rows[2]["value"] = "1000"
    rows[2]["contractAddress"] = OTHER  # another asset
    assert not forwards_to(rows, DEPOSIT, HOT)


# --- kinds: exchange / service / forwarder, gas funder ---------------------------------

GAMBLE = {HOT: {"category": "gambling", "entity": "somecasino"}}
OPERATOR = "0x" + "9" * 40
EXECUTOR = "0x" + "8" * 40
FUNDER = "0x" + "6" * 40


def test_kind_is_exchange_or_service_by_the_label_category_of_the_sweep_target():
    lists = deposit_lists()
    ex = info_of(lists, LABELS)
    assert ex["kind"] == "exchange_deposit" and ex["evidence"] is True
    unl = info_of(lists)  # an unlabelled busy target: deposit-like, service unknown
    assert unl["kind"] == "deposit_like" and unl["evidence"] is False
    svc = info_of(lists, GAMBLE)
    assert svc["kind"] == "service_deposit"
    assert svc["evidence"] is False and svc["exchange"] == "" and svc["service"] == "somecasino"
    # a category without an entity names the category
    svc = info_of(lists, {HOT: {"category": "bridge"}})
    assert svc["kind"] == "service_deposit" and svc["service"] == "bridge"


class OpClient(FakeClient):
    def __init__(self, lists, infos, **kw):
        super().__init__(lists, **kw)
        self.infos = infos
        self.info_calls = []

    def tx_info(self, h):
        self.info_calls.append(h)
        return self.infos.get(h)


def forwarder_lists(n=5, value=10**6, to_values=None):
    rows = [tok(WALLET, DEPOSIT, value=value, h=f"0xin{i}") for i in range(2)]
    rows += [tok(DEPOSIT, HOT, value=value, h=f"0xop{i}") for i in range(n)]
    return {("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): [tx(WALLET, DEPOSIT, h="0xin0")]}


def test_operator_executed_real_token_outflows_make_a_forwarder():
    infos = {f"0xop{i}": {"from": EXECUTOR, "to": OPERATOR} for i in range(5)}
    client = OpClient(forwarder_lists(), infos)
    info = classify_deposit_address(client, DEPOSIT, busy=lambda a: False)
    assert info == {
        "address": DEPOSIT,
        "kind": "forwarder",
        "operator": OPERATOR,
        "evidence": False,
        "senders": [WALLET],
    }
    assert len(client.info_calls) == 3  # capped


TOKEN2 = "0x" + "5" * 40
ENTRY_V08 = "0x4337084d9e255ff0702461cf8895ce9e3b5ff108"


def two_token_lists():
    rows = [tok(WALLET, DEPOSIT, h="0xin0")]
    rows += [tok(DEPOSIT, HOT, h="0xop0"), tok(DEPOSIT, HOT, h="0xop1", contract=TOKEN2)]
    return {("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): []}


def test_two_different_tokens_through_one_operator_contract_make_a_forwarder():
    infos = {
        "0xop0": {"from": EXECUTOR, "to": OPERATOR},
        "0xop1": {"from": "0x" + "7" * 40, "to": OPERATOR},
    }
    info = classify_deposit_address(OpClient(two_token_lists(), infos), DEPOSIT)
    assert info["kind"] == "forwarder" and info["operator"] == OPERATOR


def test_an_erc4337_entrypoint_operator_makes_a_smart_account():
    for ep in (
        "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789",
        "0x0000000071727de22e5e9d8baf0edac6f37da032",
        ENTRY_V08,
    ):
        infos = {
            "0xop0": {"from": EXECUTOR, "to": ep},
            "0xop1": {"from": "0x" + "7" * 40, "to": ep.upper().replace("0X", "0x")},
        }
        info = classify_deposit_address(OpClient(two_token_lists(), infos), DEPOSIT)
        assert info["kind"] == "smart_account" and info["operator"] == ep
        assert info["evidence"] is False


def test_a_forwarder_needs_one_operator_contract_and_resolvable_lookups():
    mixed = {
        f"0xop{i}": {"from": EXECUTOR, "to": "0x" + str(i) * 40} for i in range(5)
    }  # same sender is not enough
    assert classify_deposit_address(OpClient(forwarder_lists(), mixed), DEPOSIT) is None
    assert classify_deposit_address(OpClient(forwarder_lists(), {}), DEPOSIT) is None
    assert classify_deposit_address(FakeClient(forwarder_lists()), DEPOSIT) is None  # no tx_info


def test_outflow_rows_without_a_hash_do_not_count_towards_the_operator_share():
    # 7 of 9 outflow transactions run by the operator (0.78) is below the share; a row
    # with an empty hash must not turn it into 8 of 10
    rows = [tok(WALLET, DEPOSIT, h=f"0xin{i}") for i in range(2)]
    rows += [tok(DEPOSIT, HOT, h=f"0xop{i}") for i in range(7)]
    rows += [tok(DEPOSIT, HOT, h=f"0xown{i}") for i in range(2)]
    blank = tok(DEPOSIT, HOT)
    blank["hash"] = ""
    rows.append(blank)
    own = [tx(DEPOSIT, HOT, h=f"0xown{i}") for i in range(2)]
    lists = {("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): own}
    infos = {f"0xop{i}": {"from": EXECUTOR, "to": OPERATOR} for i in range(7)}
    info = classify_deposit_address(OpClient(lists, infos), DEPOSIT, busy=lambda a: False)
    assert info is None or info["kind"] != "forwarder"


def test_zero_value_or_single_operator_tx_never_make_a_forwarder():
    infos = {f"0xop{i}": {"from": EXECUTOR, "to": OPERATOR} for i in range(5)}
    zero = forwarder_lists(value=0)
    assert classify_deposit_address(OpClient(zero, infos), DEPOSIT) is None
    assert classify_deposit_address(OpClient(forwarder_lists(n=1), infos), DEPOSIT) is None
    lists = forwarder_lists(n=1)
    lists[("tokentx", DEPOSIT)] += [tok(DEPOSIT, OTHER, value=0, h=f"0xz{i}") for i in range(9)]
    assert classify_deposit_address(OpClient(lists, infos), DEPOSIT) is None


def test_self_sent_sweeps_are_not_a_forwarder_and_keep_their_kind():
    client = OpClient(deposit_lists(), {}, busy={HOT})
    info = classify_deposit_address(client, DEPOSIT, busy=client.busy.__contains__)
    assert info["kind"] == "deposit_like" and client.info_calls == []


def test_shared_hits_ignore_service_and_forwarder_kinds():
    base = {"address": DEPOSIT, "senders": [WALLET, EXIT], "evidence": True}
    for kind in ("service_deposit", "forwarder", "smart_account"):
        assert shared_deposit_hits([dict(base, kind=kind)], {EXIT}, WALLET) == {}
        assert shared_deposit_hits([dict(base, kind=kind)], {EXIT}, WALLET, False) == {}
    assert shared_deposit_hits([dict(base, kind="exchange_deposit")], {EXIT}, WALLET) == {
        EXIT: [DEPOSIT]
    }
    assert shared_deposit_hits([base], {EXIT}, WALLET) == {EXIT: [DEPOSIT]}  # legacy row


def gas_lists(gas_value, gas_ts=10, sweep_ts=100):
    rows = [
        tx(FUNDER, DEPOSIT, value=gas_value, ts=gas_ts, h="0xgas"),
        tok(WALLET, DEPOSIT, value=10**6, ts=50, h="0xin"),
    ]
    rows += [tok(DEPOSIT, HOT, value=10**6, ts=sweep_ts + i, h=f"0xs{i}") for i in range(3)]
    return {("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): [rows[0]] + rows[2:]}


def test_gas_funder_is_the_small_native_transfer_before_the_first_sweep():
    assert info_of(gas_lists(10**15), LABELS)["gas_funder"] == FUNDER
    assert info_of(gas_lists(10**15), GAMBLE)["gas_funder"] == FUNDER
    assert info_of(gas_lists(10**17), LABELS)["gas_funder"] is None  # not small
    assert info_of(gas_lists(10**15, gas_ts=200), LABELS)["gas_funder"] is None  # after sweep
    assert info_of(deposit_lists(), LABELS)["gas_funder"] is None  # no token sweep


def test_reports_render_the_kind_next_to_each_deposit_address():
    from tests.fixtures import demix_result as fx
    from tornado_demix.report_html import build_html_report

    rows = [
        {
            "address": DEPOSIT,
            "kind": "exchange_deposit",
            "exchange": "binance",
            "senders": [WALLET],
        },
        {
            "address": OTHER,
            "kind": "service_deposit",
            "service": "somecasino",
            "evidence": False,
            "senders": [WALLET],
        },
        {
            "address": HOT,
            "kind": "forwarder",
            "operator": OPERATOR,
            "evidence": False,
            "senders": [WALLET],
        },
    ]
    data = fx.two_pool_result()
    data["deposit_addresses"] = rows
    html = build_html_report(data, fx.network())
    assert "exchange deposit" in html
    assert "service deposit (somecasino)" in html
    assert f"forwarder (operator {OPERATOR})" in html
    data["deposit_addresses"] = [dict(rows[2], kind="smart_account")]
    assert f"smart account (operator {OPERATOR})" in build_html_report(data, fx.network())


def test_cli_prints_the_kind_of_each_deposit_address(monkeypatch, capsys):
    from tests.fixtures import demix_result as fx
    from tests.test_shared_deposit import _demix_args, _stub_cli
    from tornado_demix import cli
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    data["deposit_addresses"] = [
        {"address": OTHER, "kind": "service_deposit", "service": "somecasino", "senders": [WALLET]},
        {"address": HOT, "kind": "forwarder", "operator": OPERATOR, "senders": [WALLET]},
    ]
    apply_heuristics(data, counterparties=set())
    _stub_cli(monkeypatch, data, {})
    cli.cmd_demix(_demix_args())
    out = capsys.readouterr().out
    assert "[deposit] 2 deposit address(es)" in out
    assert "[service deposit (somecasino)]" in out and f"[forwarder (operator {OPERATOR})]" in out
    data["deposit_addresses"][1]["kind"] = "smart_account"
    cli.cmd_demix(_demix_args())
    assert f"[smart account (operator {OPERATOR})]" in capsys.readouterr().out


def test_a_fake_token_spoof_is_not_a_forwarder_with_the_token_as_operator():
    fake = "0x" + "4" * 40
    rows = [tok(WALLET, DEPOSIT, h="0xin0")]
    rows += [tok(DEPOSIT, HOT, value=5, h=f"0xsp{i}", contract=fake) for i in range(3)]
    infos = {f"0xsp{i}": {"from": EXECUTOR, "to": fake} for i in range(3)}
    client = OpClient({("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): []}, infos)
    assert classify_deposit_address(client, DEPOSIT) is None


def test_a_contract_account_behind_an_entrypoint_is_a_smart_account_never_an_exchange():
    infos = {
        "0xop0": {"from": EXECUTOR, "to": ENTRY_V08},
        "0xop1": {"from": "0x" + "7" * 40, "to": ENTRY_V08},
    }
    client = OpClient(two_token_lists(), infos)
    info = classify_deposit_address(client, DEPOSIT, is_contract=lambda a: True)
    assert info["kind"] == "smart_account" and info["operator"] == ENTRY_V08
    # a contract with exchange-like sweeps is still rejected
    ex = OpClient(deposit_lists(), {}, busy={HOT})
    assert classify_deposit_address(ex, DEPOSIT, is_contract=lambda a: True, labels=LABELS) is None


def test_an_api_error_from_tx_info_is_unknown_and_the_scan_continues():
    class Boom(OpClient):
        def tx_info(self, h):
            raise ApiError("explorer down")

    info = classify_deposit_address(Boom(two_token_lists(), {}), DEPOSIT)
    assert info["kind"] == "unknown" and "explorer down" in info["error"]
    txs = [tx(WALLET, DEPOSIT)]
    errors = []
    found = depositor_deposit_addresses(Boom(two_token_lists(), {}), WALLET, txs, errors=errors)
    assert found == []
    assert [(e["what"], e["address"]) for e in errors] == [("deposit address check", DEPOSIT)]


def test_the_operator_executed_share_boundary_is_eighty_percent():
    def lists(n_op, n_self):
        rows = [tok(WALLET, DEPOSIT, h="0xin0")]
        rows += [tok(DEPOSIT, HOT, h=f"0xop{i}") for i in range(n_op)]
        rows += [tok(DEPOSIT, HOT, h=f"0xme{i}") for i in range(n_self)]
        own = [tx(DEPOSIT, HOT, value=0, h=f"0xme{i}") for i in range(n_self)]
        return {("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): own}

    infos = {f"0xop{i}": {"from": EXECUTOR, "to": OPERATOR} for i in range(5)}
    assert classify_deposit_address(OpClient(lists(4, 1), infos), DEPOSIT)["kind"] == "forwarder"
    assert classify_deposit_address(OpClient(lists(3, 1), infos), DEPOSIT) is None  # 0.75
    assert classify_deposit_address(OpClient(lists(3, 2), infos), DEPOSIT) is None  # 0.6


def _bs(category, entity=""):
    return {"category": category, "entity": entity, "source": "blockscout", "lineage": "blockscout"}


def test_a_blockscout_deposit_tag_does_not_hide_the_customer_address():
    from tornado_demix.blockscout_labels import merge_labels

    client = FakeClient(deposit_lists(), busy={HOT})
    txs = [tx(WALLET, DEPOSIT)]
    labels = merge_labels({}, {DEPOSIT: _bs("deposit", "DEPOSIT ADDRESS")})
    found = depositor_deposit_addresses(client, WALLET, txs, labels=labels)
    assert [f["address"] for f in found] == [DEPOSIT]
    # an attribution label on the same address still marks it as a service's own wallet
    own = {DEPOSIT: {"category": "deposit", "entity": "x", "source": "attribution"}}
    assert depositor_deposit_addresses(client, WALLET, txs, labels=own) == []


def test_a_blockscout_other_tag_on_the_sweep_target_is_not_a_service_but_exchange_counts():
    from tornado_demix.blockscout_labels import merge_labels

    lists = deposit_lists()
    labels = merge_labels({}, {HOT: _bs("other", "Some Contract")})
    info = info_of(lists, labels)
    # the tag adds nothing: a busy unlabelled target, not a service, not evidence
    assert info["kind"] == "deposit_like" and info["evidence"] is False
    labels = merge_labels({}, {HOT: _bs("exchange", "binance")})
    info = info_of(lists, labels)
    assert info["kind"] == "exchange_deposit" and info["evidence"] is True
    assert info["exchange"] == "binance"
    # the same tag from an attribution source keeps making a service deposit address
    svc = info_of(lists, {HOT: {"category": "other", "entity": "casino", "source": "attr"}})
    assert svc["kind"] == "service_deposit"


# --- DEX / aggregator routers are never a forwarder's operator --------------------------

ONEINCH_V2 = "0x111111125434b319222cdbf8c261674adb56f3ae"


def test_a_dex_router_is_never_the_operator_of_a_forwarder():
    # third parties (resolvers, bots) run swaps of the address through the router: the same
    # row shape as a forwarder, but the router is the user's swap venue, not an operator
    from tornado_demix.deposit_addresses import dex_routers

    assert ONEINCH_V2 in dex_routers()
    infos = {f"0xop{i}": {"from": EXECUTOR, "to": ONEINCH_V2} for i in range(5)}
    assert classify_deposit_address(OpClient(forwarder_lists(), infos), DEPOSIT) is None
    upper = {
        f"0xop{i}": {"from": EXECUTOR, "to": ONEINCH_V2.upper().replace("0X", "0x")}
        for i in range(5)
    }
    assert classify_deposit_address(OpClient(forwarder_lists(), upper), DEPOSIT) is None


def test_the_router_table_has_a_source_for_every_row_and_the_named_routers():
    import csv

    from tornado_demix.deposit_addresses import DEX_ROUTERS_CSV, dex_routers

    with open(DEX_ROUTERS_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and all(r["source_url"].startswith("https://") for r in rows)
    assert all(len(r["address"]) == 42 and r["address"] == r["address"].lower() for r in rows)
    protocols = {r["protocol"] for r in rows}
    for name in (
        "1inch",
        "0x",
        "uniswap",
        "paraswap",
        "cow",
        "lifi",
        "odos",
        "kyberswap",
        "metamask",
    ):
        assert name in protocols
    assert len(dex_routers()) == len(rows)


def test_a_relayer_contract_moving_tokens_out_of_many_per_user_addresses_is_a_forwarder():
    # genuine pattern: one operator contract, over and over, for every per-user address
    for user in ("0x" + "1" * 40, "0x" + "2" * 40):
        rows = [tok(WALLET, user, h=f"0xin{user[2:4]}")]
        rows += [tok(user, HOT, h=f"0xop{user[2:4]}{i}") for i in range(3)]
        infos = {f"0xop{user[2:4]}{i}": {"from": EXECUTOR, "to": OPERATOR} for i in range(3)}
        client = OpClient({("tokentx", user): rows, ("txlist", user): []}, infos)
        info = classify_deposit_address(client, user)
        assert info["kind"] == "forwarder" and info["operator"] == OPERATOR


def test_an_address_that_sends_most_of_its_outflow_transactions_is_not_a_forwarder():
    # the share is counted per transaction: one executed tx with many transfers does not
    # outweigh the address's own transactions
    rows = [tok(WALLET, DEPOSIT, h="0xin0")]
    rows += [tok(DEPOSIT, HOT, h=f"0xop{i}") for i in range(2)]
    rows += [tok(DEPOSIT, HOT, h=f"0xself{i}") for i in range(3)]
    own = [tx(DEPOSIT, HOT, value=0, h=f"0xself{i}") for i in range(3)]
    infos = {f"0xop{i}": {"from": EXECUTOR, "to": OPERATOR} for i in range(2)}
    client = OpClient({("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): own}, infos)
    assert classify_deposit_address(client, DEPOSIT) is None
    # many transfers inside two executed transactions are still two transactions
    rows = [tok(WALLET, DEPOSIT, h="0xin0")]
    rows += [tok(DEPOSIT, HOT, h=f"0xop{i % 2}") for i in range(8)]
    rows += [tok(DEPOSIT, HOT, h="0xself0") for _ in range(2)]
    own = [tx(DEPOSIT, HOT, value=0, h="0xself0")]
    client = OpClient({("tokentx", DEPOSIT): rows, ("txlist", DEPOSIT): own}, infos)
    assert classify_deposit_address(client, DEPOSIT) is None  # 2 of 3 transactions: below 80 %
