"""A registry row is rejected loudly, never absorbed silently.

``float()`` accepts "nan" and "inf"; ``int()`` accepts anything Python calls a
digit. A nan denomination is not merely a wrong number - it disables the deposit
check outright, because ``abs(value - nan) > nan * 0.005`` is False for every
value. Every outgoing transaction to that address then becomes a "deposit" at a
denomination of nan, which poisons every total downstream.
"""

import math

import pytest

from tornado_demix.demix import detect_deposits
from tornado_demix.networks import MAX_DECIMALS, Network, load_networks
from tornado_demix.pools import Pool

HEADER = "network,chain_id,currency,asset,denomination,pool_address,decimals,token_address\n"
ADDR = "0x" + "1" * 40
ADDR2 = "0x" + "2" * 40


def _registry(tmp_path, *rows):
    path = tmp_path / "networks.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return load_networks(str(path))


def _row(denom="1", decimals="18", addr=ADDR, chain="1", net="testnet"):
    return "{},{},E,E,{},{},{},\n".format(net, chain, denom, addr, decimals)


@pytest.mark.parametrize("denom", ["nan", "NaN", "inf", "-inf", "0", "-5", "abc"])
def test_an_unusable_denomination_is_rejected(tmp_path, capsys, denom):
    nets = _registry(tmp_path, _row(denom=denom))
    assert "testnet" not in nets
    assert "denomination" in capsys.readouterr().err


@pytest.mark.parametrize("decimals", ["999", "-3", "abc", str(MAX_DECIMALS + 1)])
def test_an_unusable_decimals_is_rejected(tmp_path, capsys, decimals):
    nets = _registry(tmp_path, _row(decimals=decimals))
    assert "testnet" not in nets
    assert "decimals" in capsys.readouterr().err


def test_a_non_numeric_chain_id_is_rejected_not_a_traceback(tmp_path, capsys):
    nets = _registry(tmp_path, _row(chain="abc"))
    assert "testnet" not in nets
    assert "chain_id" in capsys.readouterr().err


def test_a_malformed_pool_address_is_rejected(tmp_path, capsys):
    nets = _registry(tmp_path, _row(addr="0xZZZZ" + "1" * 36))
    assert "testnet" not in nets
    assert "pool_address" in capsys.readouterr().err


def test_a_duplicate_pool_address_keeps_the_first_and_says_so(tmp_path, capsys):
    """by_address keeps one; the other pool would be unreachable in silence."""
    nets = _registry(tmp_path, _row(denom="1"), _row(denom="99"))
    pools = nets["testnet"].pools
    assert [p.denom for p in pools] == [1.0]
    assert nets["testnet"].by_address[ADDR].denom == 1.0
    assert "already declared" in capsys.readouterr().err


def test_two_pools_at_one_denomination_are_still_allowed(tmp_path):
    """Avalanche really does run two 10 AVAX contracts - different addresses."""
    nets = _registry(tmp_path, _row(denom="10", addr=ADDR), _row(denom="10", addr=ADDR2))
    assert sorted(p.key for p in nets["testnet"].pools) == ["10 E", "10 E#2"]


def test_a_good_row_still_loads(tmp_path):
    nets = _registry(tmp_path, _row(denom="0.1", decimals="6"))
    pool = nets["testnet"].pools[0]
    assert (pool.denom, pool.decimals) == (0.1, 6)


def test_the_shipped_registry_has_no_rejected_rows(capsys):
    nets = load_networks()
    assert sum(len(n.pools) for n in nets.values()) == 55
    assert "unusable row" not in capsys.readouterr().err


def test_why_a_nan_denomination_had_to_be_rejected():
    """Documents the failure the validation prevents, using Pool directly."""
    pool = Pool(ADDR, float("nan"), "ETH", 18, None)
    assert math.isnan(pool.denom)
    # The tolerance test that decides "is this transaction a deposit"
    assert not abs(0.0001 - pool.denom) > pool.denom * 0.005
    net = Network("bad", 1, "ETH", [pool])
    txs = [
        {
            "from": "0xdead",
            "to": ADDR,
            "value": str(10**14),
            "timeStamp": "1",
            "blockNumber": "1",
            "hash": "0xh1",
            "gasPrice": "1",
            "isError": "0",
        }
    ]
    # Unrelated dust would have been recorded as a Tornado deposit.
    assert len(detect_deposits(None, "0xdead", network=net, txs=txs)) == 1
