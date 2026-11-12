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
    assert classify_deposit_address(client, DEPOSIT, is_contract=lambda a: True) is None
    assert client.calls == []


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
            tx(WALLET, DEPOSIT, h="0x01"),
            tx(DEPOSIT, TOKEN, value=0, h="0x02"),
            tx(DEPOSIT, TOKEN, value=0, h="0x03"),
        ],
        ("tokentx", DEPOSIT): [
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
