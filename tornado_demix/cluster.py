"""Cluster forwarding tracer for split-exit wallets.

When a depositor does not consolidate to a single address, its notes are
withdrawn to several addresses. This module takes the per-pool count-matched
candidates, traces where each forwards funds (one hop), and looks for a common
downstream address ``Z`` fed by two or more pool layers.

Forward lookups and per-wallet demix layers are cached on disk so an interrupted
run can be restarted. Cache files are scoped to the network and to a digest of
its pool set: entries hold pool keys and tx hashes that mean nothing on another
chain, and adding a contract can re-key an existing pool ('10 AVAX' becomes
'10 AVAX#2').
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict

from .constants import ZERO_ADDRESS
from .demix import run_demix
from .etherscan import EtherscanClient
from .heuristics import count_is_evidence
from .multi import wallet_fingerprint
from .networks import ETHEREUM, Network

# Tunables.
LAYER_CAP = 35  # skip a pool layer with more candidates than this
FORWARD_DAYS = 21  # how long after a withdrawal to look for the forward hop
MIN_FORWARD_FRACTION = 0.01  # ignore forwards below 1% of the denomination
PAUSE_BETWEEN = 0.08  # small delay between forward lookups (politeness)


class ForwardCache:
    """JSON-backed cache of first-hop forwards (key format: see :func:`_forwards`)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, ValueError):
                # An unreadable cache is only a cache miss.
                self.data = {}

    def get(self, addr: str) -> list[dict] | None:
        return self.data.get(addr)

    def put(self, addr: str, forwards: list[dict]) -> None:
        self.data[addr] = forwards
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
        except OSError:
            pass  # a cache that cannot be written only costs a re-fetch


def cache_scope(network: Network) -> str:
    """Return the cache-name scope for ``network``: 'name_fingerprint'.

    The fingerprint digests the chain id, every pool's address and key, and the
    routers, so any registry change produces a fresh cache file.
    """
    parts = [network.name, str(network.chain_id)]
    parts.extend(sorted("{}={}".format(p.address, p.key) for p in network.pools))
    parts.extend(sorted(network.routers))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return "{}_{}".format(network.name, digest[:10])


def _demix_layers(client, wallet, params, cache_dir, layer_cap, scope):
    """Return (fingerprint, layers) for a wallet, using an on-disk cache.

    ``layers`` maps pool key -> {candidate_address: earliest_withdrawal_ts} or
    None when the layer is unusable (no exact-count candidate, or too noisy).

    ``scope`` comes from :func:`cache_scope`; the file name also digests the
    wallet and the analysis parameters, so a run with another window never reads
    an earlier run's layers.
    """
    param_key = json.dumps(
        {k: v for k, v in sorted(params.items()) if k != "network"}, sort_keys=True
    )
    digest = hashlib.sha256("{}|{}".format(wallet, param_key).encode("utf-8")).hexdigest()[:12]
    cache_path = os.path.join(cache_dir, "layers_v3_{}_{}.json".format(scope, digest))
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            return cached["fp"], cached["layers"]
        except (OSError, ValueError, KeyError, TypeError):
            pass  # unreadable or foreign cache file: recompute

    data = run_demix(client, wallet, **params)
    fingerprint = wallet_fingerprint(data["vouchers"])

    layers = {}
    for pool_key, res in data["denoms"].items():
        need = fingerprint[pool_key]
        # `need` is the total over all of this pool's vouchers (one address got
        # every note), not a single voucher's count, so candidates_by_count
        # cannot be reused. Recompute from raw counts with the same
        # discrimination floor demix applies.
        candidates = [addr for addr, hits in res["counts"].items() if hits == need]
        if candidates and not count_is_evidence(res, need):
            candidates = []
        if not candidates or len(candidates) > layer_cap:
            layers[pool_key] = None
            continue
        layers[pool_key] = {addr: min(x["ts"] for x in res["detail"][addr]) for addr in candidates}

    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump({"fp": fingerprint, "layers": layers}, fh)
    except OSError:
        pass
    return fingerprint, layers


def _forwards(client, cache, addr, after_ts, excluded, pool, min_frac):
    """First-hop sends from ``addr`` in the pool's own asset.

    A native pool's exits are traced through native transfers, a token pool's
    through transfers of that token. Cross-asset hops (exiting in DAI and
    forwarding ETH) are not followed, because attributing them needs swap
    awareness this toolkit does not have.

    ``excluded`` holds addresses that are never a consolidation point: the
    network's pools and routers, plus the burn address. The dust floor is a
    fraction of the denomination so it scales from 0.1 ETH to 100000 USDT.

    The cache key includes the window start and the dust floor as well as the
    pool and address, because the answer depends on all four.
    """
    cache_key = "{}:{}:{}:{:g}".format(pool.key, addr, int(after_ts), min_frac)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    end_ts = after_ts + FORWARD_DAYS * 86400
    floor = pool.denom * min_frac

    sends = []
    if pool.is_native:
        for f in client.outgoing_in_window(addr, after_ts, end_ts):
            if f["value"] >= floor and f["to"] and f["to"] not in excluded:
                sends.append(
                    {
                        "to": f["to"],
                        "value": f["value"],
                        "ts": f["ts"],
                        "hash": f["hash"],
                        "asset": pool.asset,
                    }
                )
    else:
        for f in client.outgoing_token_in_window(addr, pool.token, after_ts, end_ts):
            value = pool.to_units(f["raw"])
            if value >= floor and f["to"] and f["to"] not in excluded:
                sends.append(
                    {
                        "to": f["to"],
                        "value": value,
                        "ts": f["ts"],
                        "hash": f["hash"],
                        "asset": pool.asset,
                    }
                )

    cache.put(cache_key, sends)
    time.sleep(PAUSE_BETWEEN)
    return sends


def trace_wallet(
    client: EtherscanClient,
    wallet: str,
    cache_dir: str,
    window_days: int = 30,
    fee_lo: float = 0.90,
    fee_hi: float = 0.995,
    gap_hours: float = 24,
    layer_cap: int = LAYER_CAP,
    network: Network = ETHEREUM,
    min_forward_frac: float = MIN_FORWARD_FRACTION,
) -> dict:
    """Trace one wallet's split exits and return reconvergence clusters."""
    wallet = wallet.lower()
    os.makedirs(cache_dir, exist_ok=True)
    scope = cache_scope(network)
    cache = ForwardCache(os.path.join(cache_dir, "fwd_cache_v2_{}.json".format(scope)))
    excluded = set(network.entry_points) | {ZERO_ADDRESS}

    params = dict(
        window_days=window_days, fee_lo=fee_lo, fee_hi=fee_hi, gap_hours=gap_hours, network=network
    )
    fingerprint, layers = _demix_layers(client, wallet, params, cache_dir, layer_cap, scope)
    usable = {pool_key: members for pool_key, members in layers.items() if members}

    # downstream Z -> {pool_key -> [(source_candidate, value, ts, hash, asset)]}
    downstream = defaultdict(lambda: defaultdict(list))
    for pool_key, members in usable.items():
        pool = network.by_key[pool_key]
        for candidate, first_ts in members.items():
            for forward in _forwards(
                client, cache, candidate, first_ts, excluded, pool, min_forward_frac
            ):
                downstream[forward["to"]][pool_key].append(
                    (candidate, forward["value"], forward["ts"], forward["hash"], forward["asset"])
                )

    clusters = []
    for z_addr, by_pool in downstream.items():
        if len(by_pool) >= 2:  # fed by two or more pool layers
            totals = defaultdict(float)
            for lst in by_pool.values():
                for _src, value, _ts, _hash, asset in lst:
                    totals[asset] += value
            clusters.append(
                {
                    "z": z_addr,
                    "pools_covered": sorted(by_pool.keys()),
                    "n_layers": len(by_pool),
                    "totals": {a: round(v, 8) for a, v in totals.items()},
                    "by_pool": {k: lst for k, lst in by_pool.items()},
                }
            )
    # Sorted by how many pool layers converge, then by how many distinct
    # assets. Value cannot be a tiebreak: adding 1.8 ETH to 990 DAI produces
    # a number that means nothing.
    clusters.sort(key=lambda c: (-c["n_layers"], -len(c["totals"]), c["z"]))

    return {
        "wallet": wallet,
        "fingerprint": fingerprint,
        "layers": layers,
        "clusters": clusters,
    }
