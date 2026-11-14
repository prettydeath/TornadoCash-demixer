"""The depositor's token transfers are read on every network: a USDC deposit address on a
chain without token pools is examined, and a token transfer is a counterparty."""

from tests.fixtures import demix_result as fx
from tests.test_demix import ZERO_ADDRESS, _FakeClient, _recipient, _tx, _wlog
from tornado_demix.demix import run_demix

X = "0x" + "c" * 40  # the depositor's USDC deposit address
HOT = "0x" + "d" * 40
USDC = "0x" + "1" * 40


def _tok(frm, to, ts, h, value=10**8):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 10),
        "hash": h,
        "contractAddress": USDC,
    }


class _TokenClient(_FakeClient):
    def __init__(self, txs, logs, tokens, lists):
        super().__init__(txs, logs)
        self.tokens, self.lists, self.token_calls = tokens, lists, []

    def token_transfers(self, addr, contract=None):
        self.token_calls.append(addr)
        return self.tokens

    def call(self, params):
        return self.lists.get((params["action"], params["address"]), [])

    def has_at_least_txs(self, address, n):
        return False


def test_a_usdc_deposit_address_is_found_on_a_network_without_token_pools():
    net = fx.network()
    assert all(p.is_native for p in net.pools)
    deposit = _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xdep")
    usdc_call = dict(_tx(USDC, 0, 900, 9, "0xusdc"))  # what txlist shows of the transfer
    tokens = [_tok(fx.WALLET, X, 900, "0xusdc")]
    lists = {
        ("txlist", X): [dict(_tx(USDC, 0, 1500, 150, "0xsw"), **{"from": X})],
        ("tokentx", X): [_tok(fx.WALLET, X, 900, "0xusdc"), _tok(X, HOT, 1500, "0xsw")],
    }
    logs = [_wlog(_recipient(i), format(i, "064x"), ZERO_ADDRESS, 0) for i in range(1, 4)]
    client = _TokenClient([deposit, usdc_call], logs, tokens, lists)
    labels = {HOT: {"category": "exchange", "entity": "Binance", "source": "attr"}}
    data = run_demix(client, fx.WALLET, network=net, labels=labels)
    assert client.token_calls == [fx.WALLET]
    assert [d["address"] for d in data["deposit_addresses"]] == [X]
    assert data["deposit_addresses"][0]["kind"] == "exchange_deposit"
    assert X in data["counterparty_addresses"]  # a token transfer earns linked too
