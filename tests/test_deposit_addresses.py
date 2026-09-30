"""Exchange deposit addresses shared by a depositor and a withdrawal recipient."""

from tornado_demix.deposit_addresses import (
    HOT_WALLET_TXS,
    MAX_DEPOSIT_SENDERS,
    MAX_DEPOSIT_TXS,
    classify_deposit_address,
    depositor_deposit_addresses,
    shared_deposit_hits,
)

WALLET = "0x" + "a" * 40
EXIT = "0x" + "b" * 40
DEPOSIT = "0x" + "c" * 40
HOT = "0x" + "d" * 40
OTHER = "0x" + "e" * 40


def tx(frm, to):
    return {"from": frm, "to": to, "hash": "0x" + frm[-4:] + to[-4:]}


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
    rows = [tx(s, DEPOSIT) for s in senders] + [tx(DEPOSIT, target) for _ in range(sweeps)]
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
    rows = [tx(WALLET, DEPOSIT), tx(DEPOSIT, HOT), tx(DEPOSIT, hot2), tx(DEPOSIT, HOT)]
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
