#!/usr/bin/env python3
"""Power of the decoy windows: how many would miss a planted early direct link.

For every cached decoy run of a hold-out (``.cache/labels/placebo/<dir>/decoy``) one
synthetic withdrawal is planted to an existing counterparty of the wallet and the run
is re-scored offline (see :mod:`tornado_demix.power`). The cached runs do not keep the
wallet's counterparties, so they are read once from the explorer (transaction lists of
the wallet, the same call run_demix makes) and cached under ``power/``.

Usage
-----
    python tools/power_measure.py --network ethereum --dir holdout2
    python tools/power_measure.py --network polygon --dir polygon2

Writes ``power/counterparties_<dir>.json`` and ``power/summary_<dir>.json`` and prints
the counts. Collecting counterparties is resumable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE, _log, api_keys  # noqa: E402

from tornado_demix.demix import wallet_counterparties  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.power import REASONS, window_detectability  # noqa: E402
from tornado_demix.registry import record_safe as record  # noqa: E402
from tornado_demix.rpc import make_contract_check  # noqa: E402

OUT = os.path.join(CACHE, "placebo")
POWER = os.path.join(OUT, "power")


def _load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def collect(network, wallets, path, workers_keys):
    """{wallet: sorted counterparties or None when unreadable}, resumable."""
    cache = _load_json(path, {})
    todo = [w for w in wallets if w not in cache]
    _log(f"[*] counterparties: {len(cache)} cached, {len(todo)} to read")
    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in workers_keys]
    native_only = all(p.is_native for p in network.pools)

    def one(i_w):
        i, w = i_w
        client = clients[i % len(clients)]
        try:
            txs = client.outgoing_txs(w) + client.internal_txs(w)
            if not native_only:
                txs = txs + client.token_transfers(w)
            return w, sorted(wallet_counterparties(w, txs))
        except Exception as exc:  # noqa: BLE001 - one unreadable wallet must not stop the run
            _log(f"  {w}: {type(exc).__name__}")
            return w, None

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        for n, (w, parties) in enumerate(pool.map(one, enumerate(todo)), 1):
            if parties is not None:
                cache[w] = parties
            if n % 25 == 0 or n == len(todo):
                _log(f"  {n} / {len(todo)}")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)
    return cache


def _record_summary(summary):
    record(
        "power.not_detectable_share",
        __file__,
        {"network": summary.get("network"), "dir": summary.get("dir")},
        summary,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--network", required=True)
    ap.add_argument("--dir", required=True, help="hold-out directory, e.g. holdout2")
    ap.add_argument("--networks-csv", default=None)
    args = ap.parse_args(argv)
    network = get_network(args.network, args.networks_csv)
    decoy_dir = os.path.join(OUT, args.dir, "decoy")
    names = sorted(os.listdir(decoy_dir))
    os.makedirs(POWER, exist_ok=True)
    runs = {}
    for n in names:
        d = _load_json(os.path.join(decoy_dir, n), None)
        if d and d.get("wallet"):
            runs[d["wallet"].lower()] = n
    parties = collect(
        network, sorted(runs), os.path.join(POWER, f"counterparties_{args.dir}.json"), api_keys()
    )
    is_contract = make_contract_check(network.rpc_url)
    reasons = Counter()
    per_wallet = {}
    skipped = Counter()
    for w, n in sorted(runs.items()):
        data = _load_json(os.path.join(decoy_dir, n), None)
        if not data.get("vouchers") or not data.get("denoms"):
            skipped["no_decoy_run"] += 1  # no deposits detected or every pool unresolved
            continue
        if w not in parties:
            skipped["counterparties_unreadable"] += 1
            continue
        got = window_detectability(data, w, counterparties=parties[w], is_contract=is_contract)
        reasons[got["reason"]] += 1
        per_wallet[w] = got
    total = sum(reasons.values())
    # Blind wallets (no non-contract counterparty): would a withdrawal back to the wallet
    # itself be found? Counted apart; it does not change "not detectable".
    self_seen = sum(1 for g in per_wallet.values() if g.get("self_detectable"))
    summary = {
        "network": args.network,
        "dir": args.dir,
        "windows": total,
        "reasons": {r: reasons[r] for r in REASONS},
        "not_detectable": total - reasons["detected"],
        "not_detectable_share": round((total - reasons["detected"]) / total, 4) if total else None,
        "no_counterparty_self_detectable": self_seen,
        "not_detectable_even_with_self": reasons["no_counterparty"] - self_seen + reasons["missed"],
        "skipped": dict(skipped),
    }
    with open(os.path.join(POWER, f"summary_{args.dir}.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "wallets": per_wallet}, fh, indent=1)
    _record_summary(summary)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
