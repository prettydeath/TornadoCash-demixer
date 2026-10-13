#!/usr/bin/env python3
"""Same-time decoy: search the depositor's real windows in pools it never used.

The shifted decoy of tools/placebo_eval.py moves the windows back in time, so period, pool load
and the depositor's activity differ between target and decoy. This decoy keeps the real windows
and instead re-assigns each of the wallet's native-pool deposits to the nearest-denomination pool
of the same chain that the wallet never deposited into. Withdrawals there cannot spend the wallet's
notes; everything else (dates, the wallet's history, counterparties, deposit addresses) is the
same as in the target run. Token-pool deposits are dropped; a wallet that used every native pool
has no free pool and is skipped (listed).

The decoy runs are written to <dir>_st/decoy/ and the existing target runs are copied to
<dir>_st/target/, so tools/holdout_report.py and tools/decoy_rates.py work unchanged.

Usage: python tools/placebo_sametime.py holdout2 [--network ethereum]
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ens_labels import _log, api_keys  # noqa: E402

import tornado_demix.demix as demix  # noqa: E402
from tornado_demix.attribution import load_attribution  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.report_json import _json_default  # noqa: E402

BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "labels", "placebo"
)
_ORIG = demix.detect_deposits
_TLS = threading.local()


def pool_map(network, used_keys):
    """Map each used native pool to the nearest-denomination unused native pool (one-to-one)."""
    native = sorted((p for p in network.pools if p.is_native), key=lambda p: p.denom)
    free = [p for p in native if p.key not in used_keys]
    out = {}
    for key in sorted(used_keys, key=lambda k: network.by_key[k].denom):
        if not free:
            return None
        src = network.by_key[key]
        best = min(free, key=lambda p: (abs(p.denom / src.denom - 1) if src.denom else 0, p.denom))
        out[key] = best
        free.remove(best)
    return out


def _remapped(client, wallet, network=None, **kw):
    deps = _ORIG(client, wallet, network=network, **kw)
    mapping = getattr(_TLS, "mapping", None)
    if mapping is None:
        return deps
    out = []
    for d in deps:
        p = mapping.get(d["pool_key"])
        if p is None:
            continue  # token-pool deposit: dropped
        out.append(dict(d, pool_key=p.key, denom=p.denom, asset=p.asset, to=p.address))
    return out


demix.detect_deposits = _remapped


def run_one(client, network, wallet, target, out_path, labels, window_days):
    used = {
        d["pool_key"]
        for d in target.get("deposits", [])
        if network.by_key.get(d["pool_key"]) and network.by_key[d["pool_key"]].is_native
    }
    mapping = pool_map(network, used)
    if not mapping:
        return "skipped"
    _TLS.mapping = mapping
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            data = demix.run_demix(
                client, wallet, window_days=window_days, network=network, labels=labels
            )
    finally:
        _TLS.mapping = None
    data["sametime_mapping"] = {k: v.key for k, v in mapping.items()}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=_json_default)
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--network", default="ethereum")
    ap.add_argument("--window-days", type=int, default=30)
    a = ap.parse_args()
    network = get_network(a.network)
    src = os.path.join(BASE, a.dir, "target")
    dst = os.path.join(BASE, a.dir + "_st")
    os.makedirs(os.path.join(dst, "target"), exist_ok=True)
    os.makedirs(os.path.join(dst, "decoy"), exist_ok=True)
    targets = sorted(glob.glob(os.path.join(src, "*.json")))
    labels = load_attribution(a.network)
    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]
    _log(f"[*] {len(targets)} depositors, {len(clients)} key(s), same-time decoy -> {dst}")
    skipped, failed = [], []

    def job(i, ft):
        w = os.path.basename(ft)[:-5]
        out = os.path.join(dst, "decoy", w + ".json")
        if os.path.exists(out):
            return w, "cached"
        target = json.load(open(ft, encoding="utf-8"))
        st = run_one(clients[i % len(clients)], network, w, target, out, labels, a.window_days)
        if st == "ok":
            shutil.copyfile(ft, os.path.join(dst, "target", w + ".json"))
        return w, st

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futs = {pool.submit(job, i, ft): ft for i, ft in enumerate(targets)}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                w, st = fut.result()
                if st == "skipped":
                    skipped.append(w)
            except Exception as exc:  # reported, not dropped silently
                failed.append((os.path.basename(futs[fut]), str(exc)[:120]))
            if n % 10 == 0:
                _log(f"  {n} / {len(targets)}")
    for ft in targets:  # cached decoys need their target copy too
        w = os.path.basename(ft)
        if os.path.exists(os.path.join(dst, "decoy", w)) and not os.path.exists(
            os.path.join(dst, "target", w)
        ):
            shutil.copyfile(ft, os.path.join(dst, "target", w))
    json.dump(
        {"skipped_no_free_pool": skipped, "failed": failed},
        open(os.path.join(dst, "sametime_meta.json"), "w"),
        indent=1,
    )
    _log(f"[*] done; skipped (no free pool) {len(skipped)}, failed {len(failed)}")


if __name__ == "__main__":
    main()
