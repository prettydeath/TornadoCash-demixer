#!/usr/bin/env python3
"""Placebo test of the shared exchange-deposit-address signal.

For every depositor with cached target and decoy runs (tools/placebo_eval.py)
the depositor's exchange deposit addresses are found
(:mod:`tornado_demix.deposit_addresses`), and the recipients of the target and of
the decoy windows are checked for having sent funds to one of them. The ratio of
decoy to target hits per withdrawal searched estimates the chance share, as for
the other families. Deposit-address lookups are cached under
.cache/labels/placebo/dar/.

Usage
-----
    python tools/placebo_dar.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE, _log, api_keys  # noqa: E402

from tornado_demix.attribution import load_attribution  # noqa: E402
from tornado_demix.deposit_addresses import (  # noqa: E402
    depositor_deposit_addresses,
    shared_deposit_hits,
)
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.rpc import make_contract_check  # noqa: E402

OUT = os.path.join(CACHE, "placebo")
DAR = os.path.join(OUT, "dar")


def deposit_addresses(client, network, wallet, labels, is_contract):
    path = os.path.join(DAR, wallet + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    txs = client.outgoing_txs(wallet) + client.token_transfers(wallet)
    tornado = {p.address for p in network.pools} | set(network.routers)
    found = depositor_deposit_addresses(client, wallet, txs, is_contract, labels, exclude=tornado)
    os.makedirs(DAR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(found, fh)
    return found


def recipients(data):
    return {a for res in data.get("denoms", {}).values() for a in res.get("counts", {})}


def exposure(data):
    return sum(sum(res.get("counts", {}).values()) for res in data.get("denoms", {}).values())


def main():
    names = sorted(
        set(os.listdir(os.path.join(OUT, "target"))) & set(os.listdir(os.path.join(OUT, "decoy")))
    )
    network = get_network("ethereum")
    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]
    labels = load_attribution("ethereum")
    is_contract = make_contract_check(network.rpc_url)
    _log(f"[*] {len(names)} depositors, {len(clients)} key(s), {len(labels)} labels")

    rows = []

    def one(i, name):
        t = json.load(open(os.path.join(OUT, "target", name), encoding="utf-8"))
        d = json.load(open(os.path.join(OUT, "decoy", name), encoding="utf-8"))
        wallet = t["wallet"]
        dep = deposit_addresses(clients[i % len(clients)], network, wallet, labels, is_contract)
        th = shared_deposit_hits(dep, recipients(t), wallet)
        dh = shared_deposit_hits(dep, recipients(d), wallet)
        return {
            "wallet": wallet,
            "deposit_addresses": len(dep),
            "target_hits": sorted(th),
            "decoy_hits": sorted(dh),
            "target_exposure": exposure(t),
            "decoy_exposure": exposure(d),
        }

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futs = {pool.submit(one, i, n): n for i, n in enumerate(names)}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                rows.append(fut.result())
            except Exception as exc:  # reported, not dropped silently
                _log(f"[!] {futs[fut]}: {str(exc)[:120]}")
            if n % 10 == 0:
                _log(f"  {n} / {len(names)}")

    def fdr(rs):
        t = sum(len(r["target_hits"]) for r in rs)
        d = sum(len(r["decoy_hits"]) for r in rs)
        te = sum(r["target_exposure"] for r in rs)
        de = sum(r["decoy_exposure"] for r in rs)
        return ((d / de) / (t / te) if t and te and de else None), t, d

    v, t, d = fdr(rows)
    rng = random.Random(4)
    bs = sorted(
        x
        for x in (fdr([rows[rng.randrange(len(rows))] for _ in rows])[0] for _ in range(2000))
        if x is not None
    )
    summary = {
        "depositors": len(rows),
        "with_deposit_address": sum(1 for r in rows if r["deposit_addresses"]),
        "deposit_addresses": sum(r["deposit_addresses"] for r in rows),
        "target_hits": t,
        "decoy_hits": d,
        "depositors_with_target_hit": sum(1 for r in rows if r["target_hits"]),
        "fdr": round(v, 3) if v is not None else None,
        "fdr_95ci": [round(bs[int(0.025 * len(bs))], 3), round(bs[int(0.975 * len(bs)) - 1], 3)]
        if bs
        else None,
    }
    with open(os.path.join(OUT, "dar_summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
