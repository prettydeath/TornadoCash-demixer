"""The Pool value object and its key assignment."""

from tornado_demix.pools import Pool, assign_keys


def test_address_and_token_are_lowercased():
    p = Pool("0xA160cdAB225685dA1d56aa342Ad8841c3b53f291", 100, "ETH")
    assert p.address == "0xa160cdab225685da1d56aa342ad8841c3b53f291"
    assert p.token is None
    assert p.is_native

    t = Pool(
        "0xD4B88Df4D29F5CEDD6857912842CFF3B20C8CFA3",
        100,
        "DAI",
        decimals=18,
        token="0x6B175474E89094C44Da98b954EedeAC495271d0F",
    )
    assert t.token == "0x6b175474e89094c44da98b954eedeac495271d0f"
    assert not t.is_native


def test_raw_denomination_respects_decimals():
    eth = Pool("0x" + "1" * 40, 0.1, "ETH", decimals=18)
    assert eth.raw_denom == 100000000000000000

    usdc = Pool("0x" + "2" * 40, 1000, "USDC", decimals=6)
    assert usdc.raw_denom == 1000000000

    wbtc = Pool("0x" + "3" * 40, 0.1, "WBTC", decimals=8)
    assert wbtc.raw_denom == 10000000


def test_to_raw_is_exact_for_large_denominations():
    """100000 DAI at 18 decimals is 10^23, which a float cannot represent."""
    from tornado_demix.pools import Pool

    dai = Pool("0x" + "1" * 40, 100000, "DAI", 18, "0x" + "2" * 40)
    assert dai.raw_denom == 10**23
    assert dai.to_raw(100000) == 100000 * 10**18


def test_to_raw_exact_across_shipped_decimals():
    from tornado_demix.pools import Pool

    assert Pool("0x" + "1" * 40, 0.1, "ETH", 18).to_raw(0.1) == 10**17
    assert Pool("0x" + "1" * 40, 1000, "USDC", 6, "0x" + "2" * 40).to_raw(1000) == 1000 * 10**6
    assert Pool("0x" + "1" * 40, 0.1, "WBTC", 8, "0x" + "2" * 40).to_raw(0.1) == 10**7
    assert (
        Pool("0x" + "1" * 40, 5000000, "cDAI", 8, "0x" + "2" * 40).to_raw(5000000)
        == 5000000 * 10**8
    )


def test_unit_conversion_round_trips():
    usdt = Pool("0x" + "4" * 40, 100, "USDT", decimals=6)
    assert usdt.to_raw(100) == 100000000
    assert usdt.to_units(100000000) == 100.0
    assert usdt.to_units(99500000) == 99.5


def test_label_is_compact():
    assert Pool("0x" + "5" * 40, 1.0, "ETH").label == "1 ETH"
    assert Pool("0x" + "6" * 40, 0.1, "ETH").label == "0.1 ETH"
    assert Pool("0x" + "7" * 40, 10000, "DAI").label == "10000 DAI"


def test_label_does_not_use_scientific_notation():
    """cDAI runs a real 5,000,000 pool; '{:g}' would render it '5e+06'."""
    assert Pool("0x" + "8" * 40, 5000000, "cDAI").label == "5000000 cDAI"
    assert Pool("0x" + "9" * 40, 500000, "cDAI").label == "500000 cDAI"
    assert Pool("0x" + "a" * 40, 0.1, "WBTC", decimals=8).label == "0.1 WBTC"


def test_large_nearby_denominations_do_not_share_a_key():
    """Under '{:g}' these three all rendered '5e+06' and were merged."""
    a = Pool("0x" + "1" * 40, 4999999, "cDAI")
    b = Pool("0x" + "2" * 40, 5000000, "cDAI")
    c = Pool("0x" + "3" * 40, 5000001, "cDAI")
    assign_keys([a, b, c])
    assert len({a.key, b.key, c.key}) == 3
    assert "#" not in a.key + b.key + c.key


def test_keys_are_unique_and_stable():
    """Two contracts at the same denomination each get a distinct key.

    Avalanche really does have two live 10 AVAX pools; the old
    {denomination: address} map could hold only one of them.
    """
    a = Pool("0x330bdFADE01eE9bF63C209Ee33102DD334618e0a", 10, "AVAX")
    b = Pool("0xe1376deF383d1656f5A40B6ba31f8c035bfc26AA", 10, "AVAX")
    c = Pool("0x6CEB170E3ec0faFaE3be5a02FeFB81f524fE85c5", 500, "AVAX")

    assign_keys([a, b, c])
    assert {a.key, b.key, c.key} == {"10 AVAX", "10 AVAX#2", "500 AVAX"}
    assert c.key == "500 AVAX"

    # Stable regardless of input order: the lower address keeps the bare key.
    a2 = Pool("0x330bdFADE01eE9bF63C209Ee33102DD334618e0a", 10, "AVAX")
    b2 = Pool("0xe1376deF383d1656f5A40B6ba31f8c035bfc26AA", 10, "AVAX")
    assign_keys([b2, a2])
    assert a2.key == a.key
    assert b2.key == b.key


def test_same_denomination_different_asset_does_not_collide():
    dai = Pool("0x" + "a" * 40, 100, "DAI")
    usdc = Pool("0x" + "b" * 40, 100, "USDC", decimals=6)
    usdt = Pool("0x" + "c" * 40, 100, "USDT", decimals=6)
    assign_keys([dai, usdc, usdt])
    assert {dai.key, usdc.key, usdt.key} == {"100 DAI", "100 USDC", "100 USDT"}


# Exact arithmetic in both directions
# to_raw was moved to Decimal because 100000.0 * 10**18 rounds to
# 99999999999999991611392 and an exact-match deposit check would miss every
# deposit into that pool. to_units kept dividing in float, which put the same
# hole on the output side: the 100000 DAI pool rendered payouts and fees as
# 99999.99999999999.
import csv  # noqa: E402
import os  # noqa: E402
from decimal import Decimal  # noqa: E402

from tornado_demix.config import PACKAGE_DATA  # noqa: E402


def test_to_units_is_exact_for_the_denomination_that_broke_to_raw():
    pool = Pool("0x" + "1" * 40, 100000, "DAI", 18, "0x" + "2" * 40)
    assert pool.raw_denom == 10**23
    assert pool.to_units(10**23) == 100000.0
    assert pool.to_units(pool.raw_denom) == pool.denom


def test_every_shipped_pool_round_trips_exactly():
    """to_units(to_raw(denom)) == denom, for all 55."""
    path = os.path.join(PACKAGE_DATA, "networks.csv")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 55
    for row in rows:
        pool = Pool(
            row["pool_address"],
            float(row["denomination"]),
            row["asset"],
            int(row["decimals"]),
            row["token_address"] or None,
        )
        exact = int(Decimal(row["denomination"]) * 10 ** int(row["decimals"]))
        assert pool.raw_denom == exact, row["asset"] + " " + row["denomination"]
        assert pool.to_units(pool.raw_denom) == pool.denom, "{} {} round-trips to {!r}".format(
            row["asset"], row["denomination"], pool.to_units(pool.raw_denom)
        )


def test_a_partial_amount_still_converts_sensibly():
    """Fees are not whole denominations."""
    usdc = Pool("0x" + "1" * 40, 100, "USDC", 6, "0x" + "2" * 40)
    assert usdc.to_units(2_500_000) == 2.5
    wbtc = Pool("0x" + "3" * 40, 1, "WBTC", 8, "0x" + "4" * 40)
    assert wbtc.to_units(12_345_678) == 0.12345678
