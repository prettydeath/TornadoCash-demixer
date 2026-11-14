"""Token rows make a direct counterparty only when the depositor really made the transfer:
spoofed, zero-value and fake-token rows never earn `linked`. Synthetic addresses only."""

import pytest

from tests.fixtures import demix_result as fx
from tests.test_demix import RELAYER, _FakeClient, _recipient, _tx, _wlog
from tornado_demix.demix import run_demix
from tornado_demix.deposit_addresses import own_token_transfers

FAKE = "0x" + "f" * 40  # an attacker's fake token
USDC = "0x" + "1" * 40  # a real token
R = _recipient(3)  # a withdrawal recipient


def tok(frm, to, ts, h, contract, value):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 10),
        "hash": h,
        "contractAddress": contract,
    }


class _Client(_FakeClient):
    def __init__(self, txs, logs, tokens):
        super().__init__(txs, logs)
        self.tokens = tokens

    def token_transfers(self, addr, contract=None):
        return self.tokens

    def call(self, params):
        return []

    def has_at_least_txs(self, a, n):
        return False


def _run(tokens, extra_txs=()):
    deposit = _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xdep")
    logs = [
        _wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15, ts=1100 + i) for i in range(1, 8)
    ]
    client = _Client([deposit, *extra_txs], logs, tokens)
    data = run_demix(client, fx.WALLET, network=fx.network(), labels={})
    return data, data["denoms"]["1 ETH"]["signals"][R]


@pytest.mark.parametrize(
    "rows",
    [
        [tok(fx.WALLET, R, 1050, "0xa1", FAKE, 1)],  # fake Transfer(from=depositor), attacker tx
        [tok(fx.WALLET, R, 1050, "0xa2", USDC, 0)],  # real token, zero-value transferFrom
        [tok(R, fx.WALLET, 1050, "0xa3", FAKE, 10**18)],  # fake Transfer(from=R, to=depositor)
        [tok(fx.WALLET, R, 5000, "0xa4", FAKE, 1)],  # spoof placed after the withdrawal
    ],
)
def test_a_spoofed_token_row_gives_no_counterparty_and_no_linked(rows):
    data, signals = _run(rows)
    assert R not in data["counterparty_addresses"]
    assert "linked" not in signals


def test_a_token_transfer_the_depositor_sent_still_links():
    sent = dict(_tx(USDC, 0, 1050, 105, "0xmine"))  # the depositor's call to the token
    data, signals = _run([tok(fx.WALLET, R, 1050, "0xmine", USDC, 10**6)], [sent])
    assert R in data["counterparty_addresses"]
    assert "linked" in signals


def test_incoming_token_counts_only_when_the_depositor_later_moves_that_token():
    pay = dict(_tx(USDC, 0, 1200, 120, "0xspend"))
    incoming = tok(R, fx.WALLET, 1050, "0xin", USDC, 10**6)
    spend = tok(fx.WALLET, "0x" + "9" * 40, 1200, "0xspend", USDC, 10**6)
    kept = own_token_transfers(fx.WALLET, [pay], [incoming, spend])
    assert incoming in kept and spend in kept
    # never moved by the depositor (a fake token, or an unspent airdrop): dropped
    assert own_token_transfers(fx.WALLET, [], [incoming]) == []
    # moved before it arrived: not "later"
    early = tok(fx.WALLET, "0x" + "9" * 40, 900, "0xspend", USDC, 10**6)
    assert own_token_transfers(fx.WALLET, [pay], [incoming, early]) == [early]


def test_a_sponsored_7702_token_transfer_of_the_depositor_counts():
    sponsored = {
        "from": "0x" + "3" * 40,
        "to": fx.WALLET,
        "value": "0",
        "timeStamp": "1050",
        "blockNumber": "105",
        "hash": "0xsp",
    }
    row = tok(fx.WALLET, R, 1050, "0xsp", USDC, 10**6)
    assert own_token_transfers(fx.WALLET, [sponsored], [row]) == [row]


def native_in(frm, ts, h, value, err="0", status="1"):
    return {
        "from": frm,
        "to": fx.WALLET,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 10),
        "hash": h,
        "gasPrice": "1",
        "isError": err,
        "txreceipt_status": status,
    }


@pytest.mark.parametrize(
    "row",
    [
        native_in(R, 1050, "0xb1", 0),  # 0-value native tx to the depositor
        native_in(R, 1050, "0xb3", 10**18, err="1"),  # failed (isError)
        native_in(R, 1050, "0xb5", 10**18, status="0"),  # failed (receipt status)
        native_in(R, 9000, "0xb4", 0),  # 0-value, after the withdrawal
    ],
)
def test_a_zero_value_or_failed_incoming_native_tx_gives_no_linked(row):
    data, signals = _run([], [row])
    assert R not in data["counterparty_addresses"]
    assert "linked" not in signals


def test_a_one_wei_successful_incoming_native_tx_still_links():
    data, signals = _run([], [native_in(R, 1050, "0xb2", 1)])
    assert R in data["counterparty_addresses"] and "linked" in signals


def test_a_failed_outgoing_native_tx_gives_no_counterparty():
    failed = dict(_tx(R, 1.0, 1050, 105, "0xfail"), isError="1")
    data, signals = _run([], [failed])
    assert R not in data["counterparty_addresses"] and "linked" not in signals
    data, signals = _run([], [_tx(R, 1.0, 1050, 105, "0xok")])
    assert "linked" in signals
