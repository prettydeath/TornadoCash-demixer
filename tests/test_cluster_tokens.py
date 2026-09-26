"""Cluster forward-hops across native and token pools."""

from tornado_demix import cluster
from tornado_demix.pools import Pool

DAI_TOKEN = "0x6b175474e89094c44da98b954eedeac495271d0f"
CAND = "0xcccc000000000000000000000000000000000001"
Z = "0xzzzz000000000000000000000000000000000001".replace("z", "d")


class FakeCache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value


class FakeClient:
    def __init__(self, native=None, token=None):
        self.native = native or []
        self.token = token or []
        self.calls = []

    def outgoing_in_window(self, addr, start_ts, end_ts):
        self.calls.append("native")
        return list(self.native)

    def outgoing_token_in_window(self, addr, contract, start_ts, end_ts):
        self.calls.append(("token", contract))
        return list(self.token)


def _native_pool():
    return Pool("0x" + "a" * 40, 1.0, "ETH", 18, None)


def _dai_pool():
    return Pool("0x" + "b" * 40, 100, "DAI", 18, DAI_TOKEN)


def test_a_native_pool_uses_native_forwards():
    client = FakeClient(native=[{"to": Z, "value": 0.9, "ts": 10, "hash": "0x1"}])
    out = cluster._forwards(client, FakeCache(), CAND, 0, set(), _native_pool(), 0.01)
    assert client.calls == ["native"]
    assert out == [{"to": Z, "value": 0.9, "ts": 10, "hash": "0x1", "asset": "ETH"}]


def test_a_token_pool_queries_that_token():
    client = FakeClient(token=[{"to": Z, "raw": 90 * 10**18, "ts": 10, "hash": "0x1"}])
    out = cluster._forwards(client, FakeCache(), CAND, 0, set(), _dai_pool(), 0.01)
    assert client.calls == [("token", DAI_TOKEN)]
    assert out[0]["value"] == 90.0
    assert out[0]["asset"] == "DAI"


def test_dust_threshold_scales_with_the_denomination():
    """0.05 ETH is meaningless as a floor for a 100000 USDT pool."""
    usdt = Pool("0x" + "c" * 40, 100000, "USDT", 6, "0x" + "d" * 40)
    below = 999 * 10**6  # 0.999% of 100000
    above = 1001 * 10**6  # 1.001% of 100000
    client = FakeClient(
        token=[
            {"to": Z, "raw": below, "ts": 10, "hash": "0xsmall"},
            {"to": Z, "raw": above, "ts": 11, "hash": "0xbig"},
        ]
    )
    out = cluster._forwards(client, FakeCache(), CAND, 0, set(), usdt, 0.01)
    assert [f["hash"] for f in out] == ["0xbig"]


def test_excluded_addresses_are_dropped_for_tokens_too():
    client = FakeClient(
        token=[
            {"to": Z, "raw": 90 * 10**18, "ts": 10, "hash": "0xkeep"},
            {"to": "0x" + "e" * 40, "raw": 90 * 10**18, "ts": 11, "hash": "0xdrop"},
        ]
    )
    out = cluster._forwards(client, FakeCache(), CAND, 0, {"0x" + "e" * 40}, _dai_pool(), 0.01)
    assert [f["hash"] for f in out] == ["0xkeep"]


def test_the_cache_key_separates_assets():
    """The same address forwarding ETH and DAI must not share a cache entry."""
    cache = FakeCache()
    native_client = FakeClient(native=[{"to": Z, "value": 0.9, "ts": 10, "hash": "0xn"}])
    token_client = FakeClient(token=[{"to": Z, "raw": 90 * 10**18, "ts": 11, "hash": "0xt"}])
    cluster._forwards(native_client, cache, CAND, 0, set(), _native_pool(), 0.01)
    out = cluster._forwards(token_client, cache, CAND, 0, set(), _dai_pool(), 0.01)
    assert [f["hash"] for f in out] == ["0xt"]
    assert len(cache.data) == 2


def test_cache_separates_pools_of_the_same_asset_different_denomination():
    """Mainnet runs DAI pools at 100 and 10000; their dust floors differ,
    so the same address in both layers must not share a cache entry."""
    cache = FakeCache()
    dai_100 = Pool("0x" + "b" * 40, 100, "DAI", 18, DAI_TOKEN)
    dai_10k = Pool("0x" + "c" * 40, 10000, "DAI", 18, DAI_TOKEN)
    # a forward of 50 DAI: below 1% of 10000 (100), above 1% of 100 (1)
    client = FakeClient(token=[{"to": Z, "raw": 50 * 10**18, "ts": 10, "hash": "0x5"}])
    keep = cluster._forwards(client, cache, CAND, 0, set(), dai_100, 0.01)
    drop = cluster._forwards(client, cache, CAND, 0, set(), dai_10k, 0.01)
    assert [f["hash"] for f in keep] == ["0x5"]  # 50 >= 1 (100 DAI floor)
    assert drop == []  # 50 < 100 (10000 DAI floor)
    assert len(cache.data) == 2  # two distinct cache entries
