"""The "why this candidate" text: each fired signal names its concrete evidence (the
sender, the transactions, the counts), never a generic either/or phrase."""

from tests.fixtures import demix_result as fx
from tornado_demix.demix import counterparty_links
from tornado_demix.heuristics import (
    apply_heuristics,
    candidate_reason,
    candidate_reason_short,
    numbered_deposits,
    ranked_candidates,
)

SENDER = "0x82e6000000000000000000000000000000000082"
LINK_TX = "0x" + "4c" * 32
DEPOSIT = "0x" + "c" * 40


def _row(data, address, pool="1 ETH"):
    return next(
        r for r in ranked_candidates(data) if r["address"] == address and r["pool_key"] == pool
    )


def test_deposits_are_numbered_once_in_time_order():
    data = fx.two_pool_result()
    nums = numbered_deposits(data)
    assert [(n, d["hash"]) for n, d in nums] == [(1, "0xd1"), (2, "0xd2"), (3, "0xd3")]


def test_linked_sender_names_the_sender_the_link_transaction_and_the_count():
    data = fx.two_pool_result()
    data["counterparty_links"] = {SENDER: {"tx": LINK_TX, "ts": 900}}
    apply_heuristics(data, counterparties={SENDER}, withdrawal_senders={"0xb1": SENDER})
    why = candidate_reason(_row(data, fx.BOB))
    assert "1 of 1 withdrawal to this address was sent by " + SENDER in why
    assert (
        "direct counterparty of the depositor (tx " + LINK_TX + ", first transaction between "
        "them on 1970-01-01)"
    ) in why
    assert "0xb1" in why
    assert "context only, not scored" in why
    assert "qualifying" not in why
    assert "the depositor or its direct counterparty" not in why


def test_linked_sender_counts_every_withdrawal_the_sender_sent():
    data = fx.two_pool_result()
    apply_heuristics(
        data, counterparties={SENDER}, withdrawal_senders={"0xa1": SENDER, "0xa2": SENDER}
    )
    why = candidate_reason(_row(data, fx.ALICE))
    assert "2 of 2 withdrawals to this address were sent by " + SENDER in why
    # the link transaction was not recorded: said so, never invented
    assert "linking transaction not recorded" in why


def test_linked_sender_by_the_depositor_itself():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), withdrawal_senders={"0xb1": fx.WALLET})
    why = candidate_reason(_row(data, fx.BOB))
    assert "was sent by the depositor itself (" + fx.WALLET + ")" in why


def test_count_match_names_the_voucher_its_deposits_and_the_discrimination():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())
    row = _row(data, fx.ALICE)
    assert "count_match" in row["signals"]
    why = candidate_reason(row)
    assert "received 2 withdrawals = the 2-note voucher of the 1 ETH pool (deposits D1–D2)" in why
    assert "discrimination " in why and "recipients in the window" in why
    assert "qualifying" not in why


def test_gas_price_names_the_withdrawal_and_the_deposit():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())
    row = _row(data, fx.ALICE)
    assert "gas_price" in row["signals"]
    why = candidate_reason(row)
    assert "self-relayed withdrawal 0xa1 paid 41.000 gwei" in why
    assert "deposit D1 (tx 0xd1)" in why


def test_self_relay_lists_the_self_relayed_transactions():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())
    why = candidate_reason(_row(data, fx.ALICE))
    assert "2 of 2 withdrawals sent without a relayer" in why
    assert "0xa1" in why and "0xa2" in why


def test_linked_names_the_link_transaction_and_the_delay():
    data = fx.two_pool_result()
    data["counterparty_links"] = {fx.BOB: {"tx": LINK_TX, "ts": 900}}
    apply_heuristics(data, counterparties={fx.BOB})
    row = _row(data, fx.BOB)
    assert "linked" in row["signals"]
    why = candidate_reason(row)
    assert (
        "direct counterparty of the depositor (tx " + LINK_TX + ", first transaction between "
        "them on 1970-01-01)"
    ) in why
    # BOB withdrew at 1300, the 1 ETH voucher's last deposit was at 1010
    assert "first withdrawal 5 min after the voucher's last deposit (D2)" in why
    assert "lead signal" in why


def test_shared_deposit_names_the_full_deposit_address():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    why = candidate_reason(_row(data, fx.BOB))
    assert DEPOSIT in why and "context only, not scored" in why


def test_only_fired_signals_are_in_the_reason():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), withdrawal_senders={"0xb1": SENDER})
    why = candidate_reason(_row(data, fx.BOB))
    for absent in ("voucher", "gas price", "without a relayer", "deposit address"):
        assert absent not in why, absent


def test_counterparty_links_keep_the_earliest_linking_transaction():
    wallet = fx.WALLET
    rows = [
        {"from": wallet, "to": SENDER, "hash": "0x2", "timeStamp": "200"},
        {"from": SENDER, "to": wallet, "hash": "0x1", "timeStamp": "100"},
        {"from": wallet, "to": fx.BOB, "hash": "0x3", "timeStamp": "50"},
    ]
    links = counterparty_links(wallet, rows, {SENDER})
    assert links == {SENDER: {"tx": "0x1", "ts": 100}}


def test_short_reason_is_one_line_and_still_concrete():
    data = fx.two_pool_result()
    apply_heuristics(
        data, counterparties={SENDER}, withdrawal_senders={"0xa1": SENDER, "0xa2": SENDER}
    )
    short = candidate_reason_short(_row(data, fx.ALICE))
    assert chr(10) not in short
    assert "2 withdrawals = a 2-note voucher, discrimination" in short
    assert "gas price of deposit D1" in short
    assert "2 of 2 withdrawals sent by direct counterparty " + SENDER + " (context)" in short


def test_short_reason_of_a_link_names_the_delay():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    short = candidate_reason_short(_row(data, fx.BOB))
    assert short == (
        "direct counterparty of the depositor, first withdrawal 5 min after the last "
        "deposit (D2) (lead)"
    )
