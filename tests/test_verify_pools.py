"""RPC-based pool verification."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import verify_pools  # noqa: E402

from tornado_demix.rpc import SELECTORS  # noqa: E402


class FakeRpc:
    """Answers the four getters from a canned dict."""

    def __init__(self, uints, addresses=None, erc20=None):
        self.uints = uints
        self.addresses = addresses or {}
        self.erc20 = erc20 or {}

    def call_uint(self, to, selector):
        if to in self.erc20 and selector == "0x313ce567":
            return self.erc20[to]["decimals"]
        return self.uints.get(selector)

    def call_address(self, to, selector):
        return self.addresses.get(selector)

    def call_symbol(self, to):
        return self.erc20.get(to, {}).get("symbol")


def test_native_pool_verifies():
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 10**17,
            SELECTORS["levels"]: 20,
            SELECTORS["nextIndex"]: 64837,
        },
        addresses={SELECTORS["token"]: None},
    )
    result = verify_pools.verify_via_rpc(client, "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc")
    assert result["ok"]
    assert result["denom"] == 0.1
    assert result["decimals"] == 18
    assert result["token"] is None
    assert result["asset"] is None  # caller supplies the native ticker
    assert result["deposits"] == 64837


def test_token_pool_reports_symbol_and_decimals():
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 1000 * 10**6,
            SELECTORS["levels"]: 20,
            SELECTORS["nextIndex"]: 512,
        },
        addresses={SELECTORS["token"]: usdc},
        erc20={usdc: {"decimals": 6, "symbol": "USDC"}},
    )
    result = verify_pools.verify_via_rpc(client, "0x4736dcf1b7a3d580672cce6e7c65cd5cc9cfba9d")
    assert result["ok"]
    assert result["denom"] == 1000.0
    assert result["decimals"] == 6
    assert result["asset"] == "USDC"
    assert result["token"] == usdc


def test_non_pool_is_rejected():
    client = FakeRpc(uints={}, addresses={})
    result = verify_pools.verify_via_rpc(client, "0x" + "1" * 40)
    assert not result["ok"]
    assert "denomination()" in result["reason"]


def test_implausible_denomination_is_rejected():
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 137,  # 1.37e-16 ETH
            SELECTORS["levels"]: 20,
            SELECTORS["nextIndex"]: 3,
        },
        addresses={SELECTORS["token"]: None},
    )
    result = verify_pools.verify_via_rpc(client, "0x" + "2" * 40)
    assert not result["ok"]
    assert "implausible" in result["reason"]


def test_unused_pool_verifies_but_is_flagged():
    """Base has the contracts deployed with almost no deposits."""
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 10**18,
            SELECTORS["levels"]: 20,
            SELECTORS["nextIndex"]: 0,
        },
        addresses={SELECTORS["token"]: None},
    )
    result = verify_pools.verify_via_rpc(client, "0x" + "3" * 40)
    assert result["ok"]
    assert result["deposits"] == 0
    assert "unused" in result["reason"]


def test_csv_row_is_schema_v2():
    result = {
        "address": "0x84443cfd09a48af6ef360c6976c5392ac5023a1f",
        "denom": 0.1,
        "decimals": 18,
        "token": None,
        "asset": None,
        "deposits": 87185,
        "ok": True,
        "reason": "",
    }
    row = verify_pools.csv_row("bsc", 56, "BNB", result)
    assert row == ("bsc,56,BNB,BNB,0.1,0x84443cfd09a48af6ef360c6976c5392ac5023a1f,18,,,,,,,")


def test_csv_row_does_not_use_scientific_notation():
    """cDAI runs a real 5,000,000 pool; '{:g}' wrote it as 5e+06."""
    result = {
        "address": "0x" + "1" * 40,
        "denom": 5000000.0,
        "decimals": 8,
        "token": "0x" + "2" * 40,
        "asset": "cDAI",
        "deposits": 114,
        "ok": True,
        "reason": "",
    }
    assert ",5000000," in verify_pools.csv_row("ethereum", 1, "ETH", result)


def test_large_denomination_is_not_rejected_by_float_error():
    """100000 at 18 decimals divides to 99999.99999999999 without rounding."""
    dai = "0x6b175474e89094c44da98b954eedeac495271d0f"
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 100000 * 10**18,
            SELECTORS["levels"]: 20,
            SELECTORS["nextIndex"]: 41,
        },
        addresses={SELECTORS["token"]: dai},
        erc20={dai: {"decimals": 18, "symbol": "DAI"}},
    )
    result = verify_pools.verify_via_rpc(client, "0x" + "4" * 40)
    assert result["ok"], result["reason"]
    assert result["denom"] == 100000.0


def test_five_hundred_denomination_is_plausible():
    """Avalanche runs a real 500 AVAX pool; PLAUSIBLE omitted it."""
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 500 * 10**18,
            SELECTORS["levels"]: 20,
            SELECTORS["nextIndex"]: 167,
        },
        addresses={SELECTORS["token"]: None},
    )
    result = verify_pools.verify_via_rpc(client, "0x6ceb170e3ec0fafae3be5a02fefb81f524fe85c5")
    assert result["ok"], result["reason"]
    assert result["denom"] == 500.0


def test_wrong_tree_depth_is_rejected():
    """levels() is 20 for every genuine deployment, so it must gate.

    The README lists levels() as one of the four checks. It was read and
    stored but never consulted, so a contract with a Tornado-shaped interface
    and a plausible denomination was accepted on a check that never ran.
    """
    client = FakeRpc(
        uints={
            SELECTORS["denomination"]: 10**18,
            SELECTORS["levels"]: 16,
            SELECTORS["nextIndex"]: 4210,
        },
        addresses={SELECTORS["token"]: None},
    )
    result = verify_pools.verify_via_rpc(client, "0x" + "5" * 40)
    assert not result["ok"]
    assert "levels()" in result["reason"]
    assert "16" in result["reason"]
    assert "20" in result["reason"]


def test_unanswered_levels_is_rejected():
    """A contract that will not answer levels() has not been verified."""
    client = FakeRpc(
        uints={SELECTORS["denomination"]: 10**18, SELECTORS["nextIndex"]: 4210},
        addresses={SELECTORS["token"]: None},
    )
    result = verify_pools.verify_via_rpc(client, "0x" + "6" * 40)
    assert not result["ok"]
    assert "levels()" in result["reason"]


def test_rejection_reasons_name_the_failing_check():
    """Each rejection says which of the four getters decided it."""
    plausible = FakeRpc(
        uints={SELECTORS["denomination"]: 137, SELECTORS["levels"]: 20},
        addresses={SELECTORS["token"]: None},
    )
    assert "denomination" in verify_pools.verify_via_rpc(plausible, "0x" + "2" * 40)["reason"]
    assert "levels()" not in verify_pools.verify_via_rpc(plausible, "0x" + "2" * 40)["reason"]


def test_legacy_emit_csv_produces_a_parseable_v2_row(tmp_path):
    """The documented `--discover --emit-csv >> networks.csv` must not no-op.

    A 5-column row would be read by the parser as asset=denom,
    denomination=address, pool_address="", and dropped in silence.
    """
    from tornado_demix.networks import load_networks

    row = verify_pools.csv_row(
        "polygon",
        137,
        "MATIC",
        verify_pools.legacy_result("0x1E34A77868E19A6647b1f2F47B51ed72dEDE95DD", 100.0),
    )
    path = tmp_path / "networks.csv"
    path.write_text(
        "network,chain_id,currency,asset,denomination,pool_address,decimals,"
        "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx,"
        "router_address\n" + row + "\n"
    )

    polygon = load_networks(str(path))["polygon"]
    assert [p.key for p in polygon.pools] == ["100 MATIC"]
    assert polygon.pools[0].address == "0x1e34a77868e19a6647b1f2f47b51ed72dede95dd"
    assert polygon.pools[0].is_native


class RawRpc:
    """No call_symbol, so _read_symbol runs its real decoding path."""

    def __init__(self, payload):
        self.payload = payload

    def eth_call(self, to, data):
        return self.payload


def test_read_symbol_decodes_an_abi_dynamic_string():
    body = (
        "0" * 62
        + "20"  # offset = 32
        + "0" * 62
        + "04"  # length = 4
        + "55534443"
        + "0" * 56
    )  # "USDC", right-padded
    assert verify_pools._read_symbol(RawRpc("0x" + body), "0x" + "1" * 40) == "USDC"


def test_read_symbol_decodes_a_bytes32_symbol():
    """Some older tokens return a raw bytes32 rather than a string."""
    word = "4d4b52" + "0" * 58  # "MKR", zero-padded
    assert verify_pools._read_symbol(RawRpc("0x" + word), "0x" + "1" * 40) == "MKR"


def test_read_symbol_returns_none_when_undecodable():
    """An unreadable symbol must not abort verification of a valid pool."""
    assert verify_pools._read_symbol(RawRpc("0xzzzz"), "0x" + "1" * 40) is None
    assert verify_pools._read_symbol(RawRpc(None), "0x" + "1" * 40) is None
