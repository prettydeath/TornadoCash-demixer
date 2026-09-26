"""Cluster tracing: cache scoping and downstream filtering.

Both concerns here are evidence-integrity concerns, not performance ones. A
cache that is not scoped to the network hands one chain's tx hashes to another
chain's report, and a downstream filter built from Ethereum's contracts lets
the mixer itself be reported as an operator's consolidation point.
"""

from tornado_demix import cluster
from tornado_demix.networks import Network
from tornado_demix.pools import Pool

WALLET = "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"

ETH_POOL = "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936"
BNB_POOL = "0x84443cfd09a48af6ef360c6976c5392ac5023a1f"
BNB_ROUTER = "0x0d5550d52428e7e3175bfc9550207e4ad3859b17"

ETH_EXIT = "0xaaaa000000000000000000000000000000000001"
BNB_EXIT = "0xbbbb000000000000000000000000000000000002"
DOWNSTREAM = "0xcccc000000000000000000000000000000000003"


def _ethereum():
    return Network("ethereum", 1, "ETH", [Pool(ETH_POOL, 1.0, "ETH")])


def _bsc():
    return Network("bsc", 56, "BNB", [Pool(BNB_POOL, 1.0, "BNB")], routers=[BNB_ROUTER])


def _demix_data(network, exit_addr):
    """A minimal run_demix result: one 2-note voucher, one exact candidate.

    ``_demix_layers`` recomputes candidates from raw ``counts`` (need is the
    *sum* of this pool's voucher counts, which need not be a key of
    run_demix's own ``candidates_by_count``) and gates them with
    ``count_discrimination``. ``unique_recipients`` is set above 1 so this
    single, otherwise-unremarkable candidate reads as genuinely discriminating
    rather than tripping the "count shared by the whole window" floor these
    tests are not exercising.
    """
    pool = network.pools[0]
    return {
        "wallet": WALLET,
        "vouchers": [{"pool_key": pool.key, "count": 2, "first_ts": 1000}],
        "denoms": {
            pool.key: {
                "counts": {exit_addr: 2},
                "detail": {exit_addr: [{"ts": 1000}, {"ts": 1100}]},
                "unique_recipients": 5,
            }
        },
    }


class NoForwards:
    """A client whose addresses never send anything onward."""

    def __init__(self):
        self.seen = []

    def outgoing_in_window(self, addr, start_ts, end_ts):
        self.seen.append(addr)
        return []


def test_layer_cache_is_scoped_by_network(tmp_path, monkeypatch):
    """Tracing a wallet on one chain must not answer for another chain.

    Without network scoping the second call reads the first chain's cached
    layers, so a BSC run silently reports Ethereum pool keys, candidates and
    tx hashes - hyperlinked to bscscan, where none of them exist.
    """
    monkeypatch.setattr(cluster, "PAUSE_BETWEEN", 0)
    calls = []

    def fake_run_demix(client, wallet, **params):
        network = params["network"]
        calls.append(network.name)
        exit_addr = ETH_EXIT if network.name == "ethereum" else BNB_EXIT
        return _demix_data(network, exit_addr)

    monkeypatch.setattr(cluster, "run_demix", fake_run_demix)

    cache_dir = str(tmp_path / "cache")
    on_eth = cluster.trace_wallet(NoForwards(), WALLET, cache_dir, network=_ethereum())
    on_bsc = cluster.trace_wallet(NoForwards(), WALLET, cache_dir, network=_bsc())

    assert calls == ["ethereum", "bsc"], "the BSC run reused the Ethereum cache"
    assert list(on_eth["fingerprint"]) == ["1 ETH"]
    assert list(on_bsc["fingerprint"]) == ["1 BNB"]
    assert list(on_bsc["layers"]) == ["1 BNB"]
    assert BNB_EXIT in on_bsc["layers"]["1 BNB"]
    assert ETH_EXIT not in on_bsc["layers"]["1 BNB"]


def test_layer_cache_is_scoped_by_the_registry_fingerprint(tmp_path, monkeypatch):
    """Adding a pool can shift an existing key, so the cache must not survive it.

    ``assign_keys`` numbers duplicates relative to the whole pool set: adding a
    second 10 AVAX contract renames the other one from '10 AVAX' to
    '10 AVAX#2'. A cache keyed only by chain name would keep serving the old
    key set under the new registry.
    """
    monkeypatch.setattr(cluster, "PAUSE_BETWEEN", 0)
    calls = []

    def fake_run_demix(client, wallet, **params):
        calls.append(len(params["network"].pools))
        return _demix_data(params["network"], ETH_EXIT)

    monkeypatch.setattr(cluster, "run_demix", fake_run_demix)

    one_pool = Network("ethereum", 1, "ETH", [Pool(ETH_POOL, 1.0, "ETH")])
    two_pools = Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(ETH_POOL, 1.0, "ETH"),
            Pool("0x" + "9" * 40, 1.0, "ETH"),
        ],
    )

    cache_dir = str(tmp_path / "cache")
    cluster.trace_wallet(NoForwards(), WALLET, cache_dir, network=one_pool)
    cluster.trace_wallet(NoForwards(), WALLET, cache_dir, network=two_pools)

    assert calls == [1, 2], "the wider registry reused the narrower cache"


class OneForward:
    """Every address forwards once, to ``target``."""

    def __init__(self, target, tx_hash):
        self.target = target
        self.tx_hash = tx_hash
        self.seen = []

    def outgoing_in_window(self, addr, start_ts, end_ts):
        self.seen.append(addr)
        return [{"to": self.target, "value": 1.0, "ts": start_ts + 60, "hash": self.tx_hash}]


def test_forward_cache_is_scoped_by_network(tmp_path, monkeypatch):
    """A forward looked up on one chain must not be replayed on another.

    The candidate addresses can coincide across chains (deterministic
    deployment and address reuse both make that ordinary), so an unscoped
    address-keyed cache emits the first chain's forward tx hash under the
    second chain's report.
    """
    monkeypatch.setattr(cluster, "PAUSE_BETWEEN", 0)
    shared_exit = ETH_EXIT

    def fake_run_demix(client, wallet, **params):
        return _demix_data(params["network"], shared_exit)

    monkeypatch.setattr(cluster, "run_demix", fake_run_demix)

    cache_dir = str(tmp_path / "cache")
    eth_client = OneForward(DOWNSTREAM, "0xeee1")
    bsc_client = OneForward(DOWNSTREAM, "0xbbb1")
    cluster.trace_wallet(eth_client, WALLET, cache_dir, network=_ethereum())
    cluster.trace_wallet(bsc_client, WALLET, cache_dir, network=_bsc())

    assert bsc_client.seen == [shared_exit], (
        "the BSC run served its forwards from the Ethereum cache"
    )


def _two_layer_network(name, chain_id, currency, pool_a, pool_b, routers=None):
    return Network(
        name,
        chain_id,
        currency,
        [Pool(pool_a, 1.0, currency), Pool(pool_b, 10.0, currency)],
        routers=routers,
    )


def _two_layer_data(network, exits):
    """Two usable pool layers, one candidate each.

    ``unique_recipients`` is set above 1 (see ``_demix_data``) so each
    single candidate reads as discriminating; these fixtures exercise
    downstream/cache logic, not the discrimination gate itself.
    """
    denoms = {}
    vouchers = []
    for pool, exit_addr in zip(network.pools, exits):
        vouchers.append({"pool_key": pool.key, "count": 1, "first_ts": 1000})
        denoms[pool.key] = {
            "counts": {exit_addr: 1},
            "detail": {exit_addr: [{"ts": 1000}]},
            "unique_recipients": 5,
        }
    return {"wallet": WALLET, "vouchers": vouchers, "denoms": denoms}


def test_the_pool_itself_is_never_a_reconvergence_point(tmp_path, monkeypatch):
    """A re-deposit into the mixer must not be reported as a consolidation.

    NON_DOWNSTREAM holds Ethereum's four pools and three routers only, so on
    every other chain a candidate re-depositing into Tornado landed in
    ``downstream[pool_address]``. Two such layers made the pool contract the
    top-ranked cluster - an invented operator finding in analyst output.
    """
    monkeypatch.setattr(cluster, "PAUSE_BETWEEN", 0)
    network = _two_layer_network("bsc", 56, "BNB", BNB_POOL, "0x" + "7" * 40, routers=[BNB_ROUTER])

    def fake_run_demix(client, wallet, **params):
        return _two_layer_data(params["network"], [ETH_EXIT, BNB_EXIT])

    monkeypatch.setattr(cluster, "run_demix", fake_run_demix)

    trace = cluster.trace_wallet(
        OneForward(BNB_POOL, "0xf1"), WALLET, str(tmp_path / "a"), network=network
    )
    assert [c["z"] for c in trace["clusters"]] == []

    trace = cluster.trace_wallet(
        OneForward(BNB_ROUTER, "0xf2"), WALLET, str(tmp_path / "b"), network=network
    )
    assert [c["z"] for c in trace["clusters"]] == []

    # A genuine downstream address is still reported.
    trace = cluster.trace_wallet(
        OneForward(DOWNSTREAM, "0xf3"), WALLET, str(tmp_path / "c"), network=network
    )
    assert [c["z"] for c in trace["clusters"]] == [DOWNSTREAM]


def test_the_zero_address_is_never_a_reconvergence_point(tmp_path, monkeypatch):
    monkeypatch.setattr(cluster, "PAUSE_BETWEEN", 0)
    network = _two_layer_network("bsc", 56, "BNB", BNB_POOL, "0x" + "7" * 40)

    def fake_run_demix(client, wallet, **params):
        return _two_layer_data(params["network"], [ETH_EXIT, BNB_EXIT])

    monkeypatch.setattr(cluster, "run_demix", fake_run_demix)

    burn = "0x0000000000000000000000000000000000000000"
    trace = cluster.trace_wallet(
        OneForward(burn, "0xf4"), WALLET, str(tmp_path / "z"), network=network
    )
    assert [c["z"] for c in trace["clusters"]] == []


# Forward-cache keying
# The cache is on disk by design so an interrupted run can resume. That makes a
# key that omits part of the question worse than no cache at all: the wrong
# answer survives across wallets and across runs, silently.
def test_the_forward_cache_key_separates_different_windows(tmp_path):
    from tornado_demix.cluster import ForwardCache, _forwards
    from tornado_demix.pools import Pool

    pool = Pool("0x" + "1" * 40, 1.0, "ETH", 18, None)

    class Client:
        def __init__(self):
            self.windows = []

        def outgoing_in_window(self, addr, start_ts, end_ts):
            self.windows.append((start_ts, end_ts))
            return [
                {
                    "to": "0x" + "e" * 40,
                    "value": 1.0,
                    "ts": start_ts + 10,
                    "hash": "0xh%d" % start_ts,
                }
            ]

    client = Client()
    cache = ForwardCache(str(tmp_path / "fwd.json"))
    addr = "0x" + "a" * 40

    first = _forwards(client, cache, addr, 1000, set(), pool, 0.01)
    second = _forwards(client, cache, addr, 9_000_000, set(), pool, 0.01)

    assert len(client.windows) == 2, "the second window was served from cache"
    assert first[0]["hash"] != second[0]["hash"]


def test_the_same_window_is_served_from_cache(tmp_path):
    from tornado_demix.cluster import ForwardCache, _forwards
    from tornado_demix.pools import Pool

    pool = Pool("0x" + "1" * 40, 1.0, "ETH", 18, None)

    class Client:
        def __init__(self):
            self.calls = 0

        def outgoing_in_window(self, addr, start_ts, end_ts):
            self.calls += 1
            return []

    client = Client()
    cache = ForwardCache(str(tmp_path / "fwd.json"))
    addr = "0x" + "a" * 40
    _forwards(client, cache, addr, 1000, set(), pool, 0.01)
    _forwards(client, cache, addr, 1000, set(), pool, 0.01)
    assert client.calls == 1


def test_a_different_dust_floor_is_a_different_question(tmp_path):
    from tornado_demix.cluster import ForwardCache, _forwards
    from tornado_demix.pools import Pool

    pool = Pool("0x" + "1" * 40, 1.0, "ETH", 18, None)

    class Client:
        def __init__(self):
            self.calls = 0

        def outgoing_in_window(self, addr, start_ts, end_ts):
            self.calls += 1
            return [{"to": "0x" + "e" * 40, "value": 0.02, "ts": start_ts, "hash": "0xh"}]

    client = Client()
    cache = ForwardCache(str(tmp_path / "fwd.json"))
    addr = "0x" + "a" * 40
    loose = _forwards(client, cache, addr, 1000, set(), pool, 0.01)
    tight = _forwards(client, cache, addr, 1000, set(), pool, 0.5)
    assert client.calls == 2
    assert len(loose) == 1 and len(tight) == 0


def test_a_corrupt_forward_cache_file_is_treated_as_empty(tmp_path):
    from tornado_demix.cluster import ForwardCache

    path = tmp_path / "fwd.json"
    path.write_text("{not json", encoding="utf-8")
    cache = ForwardCache(str(path))
    assert cache.get("anything") is None
    cache.put("k", [])
    assert ForwardCache(str(path)).get("k") == []


def test_the_layer_cache_is_keyed_by_the_analysis_parameters(tmp_path, monkeypatch):
    from tornado_demix import cluster
    from tornado_demix.networks import load_networks

    eth = load_networks()["ethereum"]
    seen = []

    def fake_run_demix(client, wallet, **params):
        seen.append(params["window_days"])
        return {"vouchers": [], "denoms": {}}

    monkeypatch.setattr(cluster, "run_demix", fake_run_demix)
    scope = cluster.cache_scope(eth)
    wallet = "0x" + "a" * 40
    for days in (30, 3, 30):
        cluster._demix_layers(
            None, wallet, {"window_days": days, "network": eth}, str(tmp_path), 35, scope
        )
    assert seen == [30, 3]  # the third call is served from the first call's cache
