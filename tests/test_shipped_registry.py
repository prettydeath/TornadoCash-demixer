"""The registry that ships with the repository."""

import pytest

from tornado_demix.networks import load_networks

EXPECTED_POOL_COUNTS = {
    "ethereum": 28,
    "bsc": 4,
    "base": 4,
    "arbitrum": 4,
    "optimism": 4,
    "polygon": 3,
    "gnosis": 3,
    "avalanche": 5,
}


@pytest.fixture(scope="module")
def shipped():
    return load_networks()


def test_all_eight_networks_are_present(shipped):
    assert sorted(shipped) == sorted(EXPECTED_POOL_COUNTS)


def test_pool_counts_match(shipped):
    actual = {name: len(net.pools) for name, net in shipped.items()}
    assert actual == EXPECTED_POOL_COUNTS


def test_totals_split_between_native_and_token_pools(shipped):
    pools = [p for net in shipped.values() for p in net.pools]
    assert len(pools) == 55
    assert len([p for p in pools if p.is_native]) == 31
    assert len([p for p in pools if not p.is_native]) == 24


def test_every_token_pool_is_on_ethereum(shipped):
    for name, net in shipped.items():
        if name == "ethereum":
            continue
        assert all(p.is_native for p in net.pools), name


def test_ethereum_offers_every_token_asset(shipped):
    assert shipped["ethereum"].assets == [
        "ETH",
        "DAI",
        "USDC",
        "USDT",
        "WBTC",
        "cDAI",
        "cUSDC",
    ]


def test_token_pools_carry_their_contract_and_decimals(shipped):
    usdc = shipped["ethereum"].by_key["1000 USDC"]
    assert usdc.decimals == 6
    assert usdc.token == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert usdc.raw_denom == 1000 * 10**6

    wbtc = shipped["ethereum"].by_key["0.1 WBTC"]
    assert wbtc.decimals == 8
    assert wbtc.raw_denom == 10**7


def test_largest_dai_pool_has_an_exact_raw_denomination(shipped):
    """100000 DAI is 10^23, past the float-exact range (Finding 1). A float
    raw_denom would be 99999999999999991611392 and no on-chain deposit into
    this pool would ever match the exact-integer detection check."""
    assert shipped["ethereum"].by_key["100000 DAI"].raw_denom == 10**23


def test_duplicate_token_denominations_are_kept(shipped):
    keys = sorted(p.key for p in shipped["ethereum"].pools if p.asset == "DAI")
    assert keys == ["100 DAI", "1000 DAI", "1000 DAI#2", "10000 DAI", "100000 DAI"]
    cdai = sorted(p.key for p in shipped["ethereum"].pools if p.asset == "cDAI")
    assert "50000 cDAI" in cdai and "50000 cDAI#2" in cdai
    assert "500000 cDAI" in cdai and "500000 cDAI#2" in cdai


def test_avalanche_keeps_both_contracts_at_ten_and_at_hundred(shipped):
    avax = shipped["avalanche"]
    assert len([p for p in avax.pools if p.denom == 10]) == 2
    assert len([p for p in avax.pools if p.denom == 100]) == 2
    addresses = {p.address for p in avax.pools}
    assert "0xe1376def383d1656f5a40b6ba31f8c035bfc26aa" in addresses
    assert "0x7ce57f6a5a135eb1a8e9640af1eff9665ade00d9" in addresses


def test_bsc_is_present_with_bnb_as_its_asset(shipped):
    bsc = shipped["bsc"]
    assert bsc.chain_id == 56
    assert bsc.assets == ["BNB"]
    assert sorted(p.key for p in bsc.pools) == [
        "0.1 BNB",
        "1 BNB",
        "10 BNB",
        "100 BNB",
    ]


def test_every_network_has_an_explorer_and_an_rpc(shipped):
    for name, net in shipped.items():
        assert net.explorer_addr.startswith("https://"), name
        assert net.explorer_tx.startswith("https://"), name
        assert net.rpc_url.startswith("https://"), name


def test_explorer_urls_are_chain_appropriate(shipped):
    assert "bscscan.com" in shipped["bsc"].addr_url("0xabc")
    assert "polygonscan.com" in shipped["polygon"].addr_url("0xabc")
    assert "snowtrace.io" in shipped["avalanche"].addr_url("0xabc")
    assert "basescan.org" in shipped["base"].addr_url("0xabc")


POLYGON_ROUTER = "0x0d5550d52428e7e3175bfc9550207e4ad3859b17"
AVALANCHE_ROUTERS = {
    "0x171fb28ebffcb2737e530e1fd48cb4ef12e5031e",
    "0x0d5550d52428e7e3175bfc9550207e4ad3859b17",
}


def test_ethereum_and_verified_altchains_declare_routers(shipped):
    """The schema supports routers; the registry ships only verified ones.

    Ethereum's three proxies are built in. Polygon's and Avalanche's proxies
    were re-verified on-chain (each sampled deposit calls the router's
    ``deposit(_tornado, _commitment)`` [selector 0x13d98d13] with ``_tornado``
    equal to a known pool on that chain), so they ship. The remaining native
    chains have no verified proxy, so they ship none - a documented detection
    gap is honest, a guessed address is not.
    """
    assert len(shipped["ethereum"].routers) == 3
    assert shipped["polygon"].routers == {POLYGON_ROUTER}
    assert shipped["avalanche"].routers == AVALANCHE_ROUTERS
    for name in ("bsc", "arbitrum", "optimism", "base", "gnosis"):
        assert shipped[name].routers == set(), name


def test_verified_altchain_routers_are_entry_points(shipped):
    """A registered router must be a deposit entry point, or detection ignores it."""
    assert POLYGON_ROUTER in shipped["polygon"].entry_points
    for router in AVALANCHE_ROUTERS:
        assert router in shipped["avalanche"].entry_points


def test_pool_keys_are_unique_within_each_network(shipped):
    for name, net in shipped.items():
        keys = [p.key for p in net.pools]
        assert len(keys) == len(set(keys)), name


@pytest.mark.live
def test_every_shipped_pool_verifies_on_chain(shipped):
    """Opt-in: re-verify the whole registry against public RPCs.

    Run with: python -m pytest -m live tests/test_shipped_registry.py -v

    Ruling 9(b): verify_via_rpc cannot distinguish "not a pool" from "endpoint
    down" - both surface as a None from call_uint. Without a connectivity
    probe, a network outage would fail this test for every address on that
    network and read as "the shipped registry is wrong", which is the
    opposite of what it means. So each network's endpoint is probed once
    with a call that needs no arguments; if the probe fails, that network is
    skipped (visibly, by name) rather than failed.
    """
    import os
    import sys

    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    )
    import verify_pools

    from tornado_demix.rpc import RpcClient

    unreachable = []
    for name, net in shipped.items():
        client = RpcClient(net.rpc_url)
        if client.call("eth_blockNumber", []) is None:
            unreachable.append("{} ({})".format(name, net.rpc_url))
            continue
        for pool in net.pools:
            result = verify_pools.verify_via_rpc(client, pool.address)
            assert result["ok"], "{} {}: {}".format(name, pool.key, result["reason"])
            assert abs(result["denom"] - pool.denom) < 1e-9, (
                "{} {}: on-chain denomination is {}".format(name, pool.key, result["denom"])
            )
            assert result["token"] == pool.token, "{} {}: on-chain token is {}".format(
                name, pool.key, result["token"]
            )

    if unreachable:
        pytest.skip("RPC endpoint(s) unreachable, skipped: {}".format("; ".join(unreachable)))
