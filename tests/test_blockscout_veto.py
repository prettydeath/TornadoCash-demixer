"""Blockscout tags: a brand name alone never makes an exploit, coin, product or contract an
exchange wallet; an exchange needs a wallet word."""

import pytest

from tornado_demix import blockscout_labels as bl
from tornado_demix import deposit_addresses as da

A = "0x" + "1" * 40


@pytest.mark.parametrize(
    "name,entity",
    [
        ("Binance: Hot Wallet 20", "Binance"),
        ("Binance: Hot Wallet", "Binance"),
        ("Binance 14", "Binance"),
        ("Coinbase 16", "Coinbase"),
        ("Coinbase10", "Coinbase"),
        ("Coinbase Exchange: Hot Wallet", "Coinbase"),
        ("Bybit: Hot Wallet 1", "Bybit"),
        ("Bybit: Wallet 131", "Bybit"),
        ("Okx 7", "OKX"),
        ("MEXC: Hot Wallet", "MEXC"),
        ("Kraken: Cold Wallet", "Kraken"),
        ("binance: cold", "Binance"),
        ("KuCoin: Hot", "KuCoin"),
        ("Binance: Deposit Address", "Binance"),
        ("Gate.io 1", "Gate"),
        ("Huobi 12", "HTX"),
    ],
)
def test_exchange_wallet_tags(name, entity):
    assert bl._classify(name, "name") == ("exchange", entity)


@pytest.mark.parametrize(
    "name",
    [
        "Bybit Exploiter 1",
        "Bybit exploit 21Feb2025",
        "Bybit Hack 21feb25",
        "Bybit Hacker 2",
        "Binance Coin",
        "Binance: BNB Token",
        "Coinbase Smart Wallet",
        "Coinbasesmartwalletfactory",
        "Bitget Wallet",
        "OKX DEX: Aggregator",
        "OKX: Router 2",
        "Binance: Bridge",
        "Bybit: Contract 2",
        "Coinbase: Deployer 3",
        "Kraken Phishing Scam",
        "Binance Scam Lookalike",
        "Binance: Hot Wallet Drainer",
        "Binance",
        "OKX",
        "Gate.io: Wallet",
        "Bitget Wallet 2",
        "OKX Web3 Wallet 3",
        "Coinbase Wallet 5",
        "Coinbase Commerce: Deposit",
        "Binance: Staking Wallet 2",
    ],
)
def test_non_wallet_tags_are_never_an_exchange(name):
    category, _entity = bl._classify(name, "name")
    assert category == "other", name


def test_an_exploiter_tagged_with_a_brand_is_not_an_exchange_label():
    labels = bl.to_labels(
        {
            A: [
                {"name": "Bybit exploit 21Feb2025", "tagType": "generic"},
                {"name": "Bybit Exploiter 1", "tagType": "name"},
                {"name": "Phish / Hack", "tagType": "generic"},
            ]
        }
    )
    assert labels[A]["category"] == "other"


def test_a_stale_cache_row_is_reclassified_from_its_tag(tmp_path):
    good = "0x" + "2" * 40
    bl.write_cache(
        137,
        {
            A: {"category": "exchange", "entity": "Bybit", "tag": "Bybit Exploiter 1"},
            good: {"category": "exchange", "entity": "Binance", "tag": "Binance 14"},
        },
        directory=str(tmp_path),
    )
    cached = bl.load_cache(137, directory=str(tmp_path))
    assert (cached[A]["category"], cached[A]["entity"]) == ("other", "Bybit Exploiter 1")
    assert (cached[good]["category"], cached[good]["entity"]) == ("exchange", "Binance")


def test_a_category_filled_from_blockscout_keeps_its_origin():
    base = {A: {"entity": "", "category": "", "source": "etherscan-labels"}}
    extra = {A: {"category": "other", "entity": "Some Casino", "source": "blockscout"}}
    merged = bl.merge_labels(base, extra)
    assert merged[A]["category_source"] == "blockscout"
    # the classifier ignores a non-exchange category that came from Blockscout
    assert da._label(merged, A) == {}
    base = {A: {"entity": "", "category": "", "source": "etherscan-labels"}}
    extra = {A: {"category": "exchange", "entity": "Binance", "source": "blockscout"}}
    assert da._is_exchange(bl.merge_labels(base, extra), A)
