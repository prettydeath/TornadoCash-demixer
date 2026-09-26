"""Registry parsing: v1 back-compatibility, v2 columns, duplicate rows."""

import pytest

from tornado_demix.errors import RegistryError
from tornado_demix.networks import Network, get_network, load_networks
from tornado_demix.pools import Pool

V1_ROWS = (
    "network,chain_id,currency,denomination,pool_address\n"
    "ethereum,1,ETH,0.1,0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc\n"
    "polygon,137,MATIC,100.0,0x1E34A77868E19A6647b1f2F47B51ed72dEDE95DD\n"
)

V2_ROWS = (
    "network,chain_id,currency,asset,denomination,pool_address,decimals,"
    "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx\n"
    "ethereum,1,ETH,USDC,1000,0x4736dcf1b7a3d580672cce6e7c65cd5cc9cfba9d,6,"
    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48,,,,,\n"
    "avalanche,43114,AVAX,AVAX,10,0x330bdFADE01eE9bF63C209Ee33102DD334618e0a,18,"
    ",https://example.test/api,compat,https://rpc.example.test,,\n"
    "avalanche,43114,AVAX,AVAX,10,0xe1376deF383d1656f5A40B6ba31f8c035bfc26AA,18,"
    ",https://example.test/api,compat,https://rpc.example.test,,\n"
)


def _write(tmp_path, text):
    path = tmp_path / "networks.csv"
    path.write_text(text)
    return str(path)


def test_v1_csv_still_parses(tmp_path):
    nets = load_networks(_write(tmp_path, V1_ROWS))
    polygon = nets["polygon"]
    assert [p.label for p in polygon.pools] == ["100 MATIC"]
    pool = polygon.pools[0]
    assert pool.asset == "MATIC"  # defaults to currency
    assert pool.decimals == 18  # defaults to 18
    assert pool.is_native  # no token_address column at all


def test_v2_token_columns_are_read(tmp_path):
    nets = load_networks(_write(tmp_path, V2_ROWS))
    usdc = nets["ethereum"].by_key["1000 USDC"]
    assert usdc.decimals == 6
    assert usdc.token == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert usdc.raw_denom == 1000000000
    assert not usdc.is_native


def test_duplicate_denomination_creates_two_pools(tmp_path):
    """The v1 map silently dropped one of these; the registry must keep both."""
    nets = load_networks(_write(tmp_path, V2_ROWS))
    avax = nets["avalanche"]
    tens = [p for p in avax.pools if p.denom == 10]
    assert len(tens) == 2
    assert sorted(p.key for p in tens) == ["10 AVAX", "10 AVAX#2"]
    assert len(avax.by_address) == 2


def test_builtin_ethereum_survives_an_unrelated_csv(tmp_path):
    nets = load_networks(_write(tmp_path, "network,chain_id,currency,denomination,pool_address\n"))
    assert sorted(p.label for p in nets["ethereum"].pools) == [
        "0.1 ETH",
        "1 ETH",
        "10 ETH",
        "100 ETH",
    ]


def test_csv_rows_extend_a_builtin_network(tmp_path):
    nets = load_networks(_write(tmp_path, V2_ROWS))
    labels = sorted(p.label for p in nets["ethereum"].pools)
    assert "1000 USDC" in labels  # added by CSV
    assert "1 ETH" in labels  # builtin retained


def test_assets_list_drives_the_ui_selector(tmp_path):
    nets = load_networks(_write(tmp_path, V2_ROWS))
    assert nets["ethereum"].assets == ["ETH", "USDC"]
    assert nets["avalanche"].assets == ["AVAX"]
    assert [p.key for p in nets["ethereum"].pools_for_asset("USDC")] == ["1000 USDC"]


def test_explorer_urls_are_per_network():
    eth = Network("ethereum", 1, "ETH", [Pool("0x" + "1" * 40, 1, "ETH")])
    bsc = Network("bsc", 56, "BNB", [Pool("0x" + "2" * 40, 1, "BNB")])
    assert eth.addr_url("0xABC").startswith("https://etherscan.io/address/")
    assert bsc.addr_url("0xABC").startswith("https://bscscan.com/address/")
    assert bsc.tx_url("0xdef").startswith("https://bscscan.com/tx/")


def test_explorer_override_from_csv_wins(tmp_path):
    text = (
        "network,chain_id,currency,asset,denomination,pool_address,decimals,"
        "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx\n"
        "gnosis,100,xDAI,xDAI,100,0x1E34A77868E19A6647b1f2F47B51ed72dEDE95DD,18,"
        ",,,,https://custom.test/a/,https://custom.test/t/\n"
    )
    nets = load_networks(_write(tmp_path, text))
    assert nets["gnosis"].addr_url("0xABC") == "https://custom.test/a/0xabc"


def test_malformed_rows_are_skipped(tmp_path):
    text = (
        "network,chain_id,currency,denomination,pool_address\n"
        ",1,ETH,1.0,0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc\n"
        "foo,1,ETH,,0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc\n"
        "foo,1,ETH,1.0,not-an-address\n"
        "foo,1,ETH,notanumber,0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc\n"
        "foo,1,ETH,1.0,0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936\n"
    )
    nets = load_networks(_write(tmp_path, text))
    assert [p.address for p in nets["foo"].pools] == ["0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936"]


def test_router_address_column_is_read(tmp_path):
    """A chain must be able to declare its deposit proxy, not just its pools.

    detect_deposits gates the router path on ``to in network.routers``, so a
    chain with no way to express a router detects direct-to-pool deposits only.
    """
    router = "0xD90e2f925DA726b50C4Ed8D0Fb90Ad053324F31b"
    text = (
        "network,chain_id,currency,asset,denomination,pool_address,decimals,"
        "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx,"
        "router_address\n"
        "polygon,137,MATIC,MATIC,100,0x1E34A77868E19A6647b1f2F47B51ed72dEDE95DD,"
        "18,,,,,,," + router + "\n"
        "polygon,137,MATIC,MATIC,1000,0xdf231d99Ff8b6c6CBF4E9B9a945CBAcEF9339178,"
        "18,,,,,,,\n"
    )
    nets = load_networks(_write(tmp_path, text))
    polygon = nets["polygon"]
    assert polygon.routers == {router.lower()}
    assert router.lower() in polygon.entry_points
    assert len(polygon.pools) == 2


def test_router_column_is_optional(tmp_path):
    """The seven non-Ethereum chains ship with no verified router."""
    nets = load_networks(_write(tmp_path, V2_ROWS))
    assert nets["avalanche"].routers == set()
    assert nets["avalanche"].entry_points == set(nets["avalanche"].by_address)


def test_csv_routers_extend_a_builtin_network(tmp_path):
    """Ethereum's built-in routers survive a CSV that adds another."""
    extra = "0x1111111111111111111111111111111111111111"
    text = (
        "network,chain_id,currency,asset,denomination,pool_address,decimals,"
        "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx,"
        "router_address\n"
        "ethereum,1,ETH,ETH,1,0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936,18,"
        ",,,,,," + extra + "\n"
    )
    nets = load_networks(_write(tmp_path, text))
    routers = nets["ethereum"].routers
    assert extra in routers
    assert "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b" in routers


def test_malformed_router_addresses_are_ignored(tmp_path):
    text = (
        "network,chain_id,currency,asset,denomination,pool_address,decimals,"
        "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx,"
        "router_address\n"
        "polygon,137,MATIC,MATIC,100,0x1E34A77868E19A6647b1f2F47B51ed72dEDE95DD,"
        "18,,,,,,,not-an-address\n"
    )
    nets = load_networks(_write(tmp_path, text))
    assert nets["polygon"].routers == set()


def test_get_network_error_lists_available(tmp_path):
    with pytest.raises(RegistryError) as exc:
        get_network("nosuchchain", _write(tmp_path, V1_ROWS))
    assert "ethereum" in str(exc.value)
    assert "nosuchchain" in str(exc.value)
