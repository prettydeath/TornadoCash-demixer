#!/usr/bin/env python3
"""Build depositor -> exit pairs labelled through ENS (a side-channel ground truth).

A Tornado Cash deposit and withdrawal cannot be matched from the protocol, so
there is no public labelled set. ENS gives a partial one, as in Béres et al.
(2021), Tutela (Wu et al., 2022) and Wang et al. (WWW 2023): when the ENS name
set as the primary name of one address is controlled by another address, both
belong to one user. A depositor and a withdrawal recipient of the same pool
linked this way are a labelled pair.

Steps (each cached under .cache/labels/, so a rerun continues):

1. Universe: every deposit into the Ethereum ETH pools (sender of the deposit
   transaction, sent to a pool or a Tornado router) and every withdrawal
   (recipient from the Withdrawal event) between --start and --end.
2. Primary ENS names of all those addresses (ENS ReverseRecords.getNames,
   which returns only names that resolve back to the address).
3. Controllers of each name: the registry owner of the name and of its
   second-level .eth name, the .eth registrant, and the NameWrapper owner.
4. Pairs: a depositor and a recipient of one pool where one controls the
   other's primary name, or both primary names sit under the same second-level
   .eth name (names under a subdomain service shared by many addresses are
   skipped). Only pairs where the recipient withdrew after the depositor's
   deposit are kept.

Limits, stated with every result: names are read as they are today, not at the
time of the deposit; the set covers careless users only (an ENS name on both
sides); a pair links two addresses, not a deposit to a withdrawal.

The output links named people to Tornado Cash use. It stays local
(.cache/labels/, ignored by git); publish aggregate metrics only.

Needs: an Etherscan key (config/api.csv; every row whose service starts with
"etherscan" is used in turn), an Ethereum RPC endpoint, and the optional
packages pycryptodome and eth-abi (pip install pycryptodome eth-abi).

Usage
-----
    python tools/ens_labels.py universe
    python tools/ens_labels.py names
    python tools/ens_labels.py pairs
    python tools/ens_labels.py all
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix import config  # noqa: E402
from tornado_demix.constants import TOPIC_WITHDRAWAL  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.events import decode_withdrawal  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.rpc import RpcClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache", "labels")

REVERSE_RECORDS = "0x3671aE578E63FdF66ad4F3E12CC0c0d71Ac7510C"
ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"
BASE_REGISTRAR = "0x57f1887a8BF19b14fC0dF6Fd9B2acc9Af147eA85"
NAME_WRAPPER = "0xD4416b13d2b3a9aBAE7AcD5D6C2BbDBE25686401"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# A second-level name whose subnames are primary names of more addresses than
# this is a subdomain service (a wallet or community handing out names), not
# one person.
MAX_SHARED_PARENT = 5
BLOCK_STEP = 50_000  # blocks per explorer query range in the universe scan


# ---------------------------------------------------------------- helpers
def _keccak(data: bytes) -> bytes:
    from Crypto.Hash import keccak

    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def namehash(name: str) -> bytes:
    node = b"\x00" * 32
    if name:
        for label in reversed(name.split(".")):
            node = _keccak(node + _keccak(label.encode("utf-8")))
    return node


def _selector(signature: str) -> bytes:
    return _keccak(signature.encode())[:4]


def _load(name, default=None):
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _save(name, data):
    os.makedirs(CACHE, exist_ok=True)
    tmp = os.path.join(CACHE, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    # On Windows a reader holding the file open blocks the replace for a moment.
    for attempt in range(10):
        try:
            os.replace(tmp, os.path.join(CACHE, name))
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    os.replace(tmp, os.path.join(CACHE, name))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def api_keys():
    """Every Etherscan key in api.csv (service etherscan, etherscan2, ...)."""
    path = config.resolve(config.DEFAULT_API_CSV)
    keys = []
    if path and os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                service = (row.get("service") or "").strip().lower()
                key = (row.get("api_key") or "").strip()
                if service.startswith("etherscan") and key:
                    keys.append(key)
    return keys or [config.load_api_key()]


class KeyPool:
    """Round-robin over several explorer clients, one per key."""

    def __init__(self, network, pause=0.36):
        self.clients = [
            EtherscanClient(k, pause=pause, **network.client_kwargs()) for k in api_keys()
        ]
        self._cycle = itertools.cycle(self.clients)
        _log(f"[*] {len(self.clients)} explorer key(s)")

    def next(self):
        return next(self._cycle)


def _block_by_time(pool, ts, closest):
    return pool.next().block_by_time(ts, closest)


# ---------------------------------------------------------------- 1. universe
def build_universe(
    start: str,
    end: str,
    network: str = "ethereum",
    extra: tuple = (),
    step: int = BLOCK_STEP,
    out: str = "universe.json",
):
    """Deposits and withdrawals of the native pools of ``network`` in [start, end].

    ``extra`` adds deposit entry points missing from the registry (on L2 chains
    deposits go through the Tornado proxy, 0x0D55...9b17); ``step`` is the block
    range per explorer query (L2 chains have far more, emptier blocks).
    """
    net = get_network(network)
    keys = KeyPool(net)
    pools = [p for p in net.pools if p.is_native]
    targets = {p.address: p for p in pools}
    routers = set(net.routers) | {a.lower() for a in extra}
    t0 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    t1 = int(datetime.fromisoformat(end).replace(tzinfo=timezone.utc).timestamp())
    b0, b1 = _block_by_time(keys, t0, "after"), _block_by_time(keys, t1, "before")

    state = _load(out) or {
        "start": start,
        "end": end,
        "blocks": [b0, b1],
        "deposits": [],
        "withdrawals": [],
        "done": [],
    }
    done = set(state["done"])
    # A later --end extends the scanned span; the new blocks form a segment of
    # their own so the ranges already done keep their boundaries.
    segments = state.setdefault("segments", [list(state["blocks"])])
    if b1 > segments[-1][1]:
        segments.append([segments[-1][1] + 1, b1])
        state["blocks"][1] = b1
        state["end"] = end

    def ranges():
        for seg_lo, seg_hi in segments:
            lo = seg_lo
            while lo <= seg_hi:
                hi = min(lo + step - 1, seg_hi)
                yield lo, hi
                lo = hi + 1

    # Deposits: transactions sent to a pool or router whose value is a pool's
    # denomination; the sender is the depositor. Deposits made from contract
    # wallets (internal transfers) are not in this set.
    for addr in sorted(set(targets) | routers):
        for lo, hi in ranges():
            tag = f"dep:{addr}:{lo}"
            if tag in done:
                continue
            rows = keys.next().fetch_all("txlist", addr, lo, hi)
            for tx in rows:
                if tx.get("isError") == "1" or (tx.get("to") or "").lower() != addr:
                    continue
                value = int(tx.get("value") or 0) / 1e18
                pool = targets.get(addr) or next(
                    (p for p in pools if abs(value - p.denom) <= p.denom * 0.005), None
                )
                if pool is None or abs(value - pool.denom) > pool.denom * 0.005:
                    continue
                state["deposits"].append(
                    [pool.key, tx["from"].lower(), int(tx["timeStamp"]), tx["hash"].lower()]
                )
            done.add(tag)
            state["done"] = sorted(done)
            _save(out, state)
        _log(f"  deposits via {addr}: {len(state['deposits'])} so far")

    # Withdrawals: the recipient named in the pool's Withdrawal event.
    for pool in pools:
        for lo, hi in ranges():
            tag = f"wd:{pool.address}:{lo}"
            if tag in done:
                continue
            for log in keys.next().get_logs(pool.address, TOPIC_WITHDRAWAL, lo, hi):
                w = decode_withdrawal(log)
                state["withdrawals"].append([pool.key, w["to"], w["ts"], w["tx_hash"].lower()])
            done.add(tag)
            state["done"] = sorted(done)
            _save(out, state)
        _log(f"  withdrawals from {pool.key}: {len(state['withdrawals'])} so far")

    _log(
        f"[*] universe: {len(state['deposits'])} deposits, {len(state['withdrawals'])} withdrawals"
    )
    return state


# ---------------------------------------------------------------- 2. names
def _rpc():
    return RpcClient(get_network("ethereum").rpc_url, timeout=40, retries=4)


def _eth_call(rpc, to, data: bytes, attempts=5):
    for attempt in range(attempts):
        result = rpc.eth_call(to, "0x" + data.hex())
        if result is not None:
            return bytes.fromhex(result[2:])
        time.sleep(1.5 * (attempt + 1))
    return None


def primary_names(addresses, batch=200):
    from eth_abi import decode, encode

    names = _load("names.json", {})
    todo = [a for a in addresses if a not in names]
    rpc = _rpc()
    sel = _selector("getNames(address[])")

    def fetch(chunk):
        # One address with a broken resolver reverts the whole batch; split it.
        raw = _eth_call(rpc, REVERSE_RECORDS, sel + encode(["address[]"], [chunk]), attempts=2)
        if raw is not None:
            (result,) = decode(["string[]"], raw)
            for addr, name in zip(chunk, result):
                names[addr] = name.lower() if name else ""
        elif len(chunk) == 1:
            names[chunk[0]] = ""
            _log(f"  [!] no name readable for {chunk[0]}")
        else:
            half = len(chunk) // 2
            fetch(chunk[:half])
            fetch(chunk[half:])

    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        fetch(chunk)
        if (i // batch) % 20 == 0:
            _save("names.json", names)
            _log(f"  names: {i + len(chunk)} / {len(todo)}")
    _save("names.json", names)
    return names


def _second_level(name):
    parts = name.split(".")
    if len(parts) < 2 or parts[-1] != "eth":
        return None
    return ".".join(parts[-2:])


def controllers(names, batch=150):
    """{name: sorted controller addresses} for every primary name and its .eth parent."""
    from eth_abi import decode, encode

    ctrl = _load("controllers.json", {})
    wanted = set()
    for name in names.values():
        if name:
            wanted.add(name)
            parent = _second_level(name)
            if parent:
                wanted.add(parent)
    todo = sorted(n for n in wanted if n not in ctrl)
    rpc = _rpc()
    owner_sel = _selector("owner(bytes32)")
    owner_of_sel = _selector("ownerOf(uint256)")
    agg_sel = _selector("aggregate3((address,bool,bytes)[])")
    wrapper = NAME_WRAPPER.lower()

    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        calls = []
        for name in chunk:
            node = namehash(name)
            calls.append((ENS_REGISTRY, True, owner_sel + node))
            calls.append((NAME_WRAPPER, True, owner_of_sel + node))
            label = name.split(".")[0] if _second_level(name) == name else None
            calls.append(
                (BASE_REGISTRAR, True, owner_of_sel + (_keccak(label.encode()) if label else node))
            )
        raw = _eth_call(rpc, MULTICALL3, agg_sel + encode(["(address,bool,bytes)[]"], [calls]))
        if raw is None:
            _log(f"  [!] multicall failed for a batch at {i}; skipped")
            continue
        (results,) = decode(["(bool,bytes)[]"], raw)
        for j, name in enumerate(chunk):
            owners = set()
            for k, (ok, data) in enumerate(results[3 * j : 3 * j + 3]):
                if k == 2 and _second_level(name) != name:
                    continue  # the registrar only knows second-level names
                if ok and len(data) >= 32:
                    owner = "0x" + data[12:32].hex()
                    if int(owner, 16) and owner != wrapper:
                        owners.add(owner)
            ctrl[name] = sorted(owners)
        if (i // batch) % 10 == 0:
            _save("controllers.json", ctrl)
            _log(f"  controllers: {i + len(chunk)} / {len(todo)}")
    _save("controllers.json", ctrl)
    return ctrl


# ---------------------------------------------------------------- 3. pairs
def build_pairs(universe, names, ctrl):
    first_dep = {}  # (pool, depositor) -> earliest deposit ts
    for pool, sender, ts, _h in universe["deposits"]:
        key = (pool, sender)
        first_dep[key] = min(first_dep.get(key, ts), ts)
    withdrawn = defaultdict(list)  # (pool, recipient) -> timestamps
    for pool, to, ts, _h in universe["withdrawals"]:
        withdrawn[(pool, to)].append(ts)
    depositors = {a for (_p, a) in first_dep}
    recipients = {a for (_p, a) in withdrawn}

    # Addresses per second-level name, to recognise subdomain services.
    by_parent = defaultdict(set)
    for addr, name in names.items():
        parent = _second_level(name) if name else None
        if parent:
            by_parent[parent].add(addr)

    def owners_of(addr, name):
        parent = _second_level(name)
        owners = set(ctrl.get(name, []))
        if parent:
            owners |= set(ctrl.get(parent, []))
        owners.discard(addr)
        return owners

    # An owner controlling the primary names of many addresses is a service
    # (a subdomain registrar, a wallet provider), not a person.
    controlled = defaultdict(int)
    for addr, name in names.items():
        for owner in owners_of(addr, name) if name else ():
            controlled[owner] += 1

    links = defaultdict(set)  # (a, b) -> reasons, unordered pair
    for addr, name in names.items():
        if not name:
            continue
        parent = _second_level(name)
        for owner in owners_of(addr, name):
            if controlled[owner] <= MAX_SHARED_PARENT:
                links[tuple(sorted((addr, owner)))].add("controls the other's primary name")
        if parent and 2 <= len(by_parent[parent]) <= MAX_SHARED_PARENT:
            for other in by_parent[parent]:
                if other != addr:
                    links[tuple(sorted((addr, other)))].add(f"names under one .eth name ({parent})")

    pairs = []
    for (a, b), reasons in links.items():
        for dep, rec in ((a, b), (b, a)):
            if dep not in depositors or rec not in recipients:
                continue
            for (pool, d), t_dep in first_dep.items():
                if d != dep:
                    continue
                later = [t for t in withdrawn.get((pool, rec), []) if t > t_dep]
                if later:
                    pairs.append(
                        {
                            "pool_key": pool,
                            "depositor": dep,
                            "recipient": rec,
                            "first_deposit_ts": t_dep,
                            "first_withdrawal_after_ts": min(later),
                            "days_between": round((min(later) - t_dep) / 86400, 1),
                            "reasons": sorted(reasons),
                        }
                    )
    pairs.sort(key=lambda p: (p["pool_key"], p["depositor"], p["recipient"]))
    _save("pairs.json", pairs)
    named = sum(1 for n in names.values() if n)
    _log(
        f"[*] {len(depositors)} depositors, {len(recipients)} recipients, {named} with a "
        f"primary name; {len(links)} ENS links; {len(pairs)} labelled pool pairs "
        f"({len({p['depositor'] for p in pairs})} depositors)"
    )
    return pairs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=["universe", "names", "pairs", "all"])
    parser.add_argument("--start", default="2019-12-16")
    parser.add_argument("--end", default="2022-08-08")
    parser.add_argument("--network", default="ethereum", help="universe step only")
    parser.add_argument("--extra", nargs="*", default=[], help="extra deposit entry points")
    parser.add_argument("--block-step", type=int, default=BLOCK_STEP)
    parser.add_argument("--out", default="universe.json", help="universe file under the cache")
    args = parser.parse_args(argv)
    if args.network != "ethereum" and args.step != "universe":
        raise SystemExit("names and pairs are Ethereum-only (ENS)")

    universe = (
        build_universe(
            args.start, args.end, args.network, tuple(args.extra), args.block_step, args.out
        )
        if args.step in ("universe", "all")
        else None
    )
    if args.step == "universe":
        return
    universe = universe or _load("universe.json")
    if universe is None:
        raise SystemExit("run the universe step first")
    addresses = sorted(
        {r[1] for r in universe["deposits"]} | {r[1] for r in universe["withdrawals"]}
    )
    if args.step in ("names", "all"):
        names = primary_names(addresses)
        controllers(names)
    if args.step in ("pairs", "all"):
        build_pairs(universe, _load("names.json", {}), _load("controllers.json", {}))


if __name__ == "__main__":
    main()
