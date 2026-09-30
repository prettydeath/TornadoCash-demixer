#!/usr/bin/env python3
"""Evaluate demix against the ENS-labelled pairs from tools/ens_labels.py.

For every labelled depositor the real pipeline (run_demix, default settings)
runs once; results are cached under .cache/labels/demix/. For each labelled
pair (pool, depositor, recipient) it records whether the recipient's
withdrawal fell inside the searched window (reachable), and whether and in
which band the recipient came out as a candidate.

Reported, per band and cumulatively (strong, strong+moderate, all):

* recall: labelled pairs found / labelled pairs (and / reachable pairs);
* precision (a lower bound): candidates that are a labelled partner of their
  depositor / all candidates. The labels are incomplete, so an unlabelled
  true exit counts against the tool.

The same numbers are repeated with the ``linked`` signal removed, because an
ENS pair that also transacted directly is seen by ``linked`` and by the label
alike, which would flatter the result.

Several explorer keys (api.csv rows etherscan, etherscan2, ...) run in parallel,
one depositor per worker.

Usage
-----
    python tools/evaluate_labels.py                 # every labelled depositor
    python tools/evaluate_labels.py --limit 100     # a random sample of 100
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE, _load, _log, _save, api_keys  # noqa: E402

from tornado_demix.attribution import load_attribution  # noqa: E402
from tornado_demix.demix import run_demix  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import confidence_band, ranked_candidates  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.report_json import _json_default  # noqa: E402

RESULTS = os.path.join(CACHE, "demix")
BANDS = ("strong", "moderate", "weak")


def _run(client, network, depositor, window_days, labels=None):
    path = os.path.join(RESULTS, depositor + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    with contextlib.redirect_stderr(io.StringIO()):
        data = run_demix(client, depositor, window_days=window_days, network=network, labels=labels)
    os.makedirs(RESULTS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=_json_default)
    return data


def _rows(data, drop_linked):
    """Ranked candidates, optionally re-banded without the linked signal."""
    rows = []
    for r in ranked_candidates(data):
        signals = set(r["signals"])
        if drop_linked:
            signals.discard("linked")
            if not signals:
                continue  # linked was what admitted it
        rows.append((r["pool_key"], r["address"], confidence_band(signals), r["signals"]))
    return rows


def evaluate(results, pairs, drop_linked):
    partners = defaultdict(set)
    for p in pairs:
        partners[p["depositor"]].add(p["recipient"])
    found = {b: 0 for b in BANDS}
    reachable = 0
    evaluated = 0
    cand = Counter()
    hit = Counter()
    linked_hits = 0
    for p in pairs:
        data = results.get(p["depositor"])
        if data is None:
            continue
        evaluated += 1
        res = data.get("denoms", {}).get(p["pool_key"], {})
        if p["recipient"] in res.get("counts", {}):
            reachable += 1
        for pool, addr, band, signals in _rows(data, drop_linked):
            if pool == p["pool_key"] and addr == p["recipient"]:
                found[band] += 1
                linked_hits += "linked" in signals
                break
    for dep, data in results.items():
        for _pool, addr, band, _s in _rows(data, drop_linked):
            cand[band] += 1
            hit[band] += addr in partners[dep]

    def cumulative(counter, upto):
        return sum(counter[b] for b in BANDS[: BANDS.index(upto) + 1])

    out = {"pairs": evaluated, "reachable": reachable, "found_with_linked": linked_hits}
    for upto in BANDS:
        n_found = sum(found[b] for b in BANDS[: BANDS.index(upto) + 1])
        n_cand = cumulative(cand, upto)
        n_hit = cumulative(hit, upto)
        out[upto] = {
            "found": n_found,
            "recall": round(n_found / evaluated, 4) if evaluated else None,
            "recall_of_reachable": round(n_found / reachable, 4) if reachable else None,
            "candidates": n_cand,
            "labelled_among_candidates": n_hit,
            "precision_lower_bound": round(n_hit / n_cand, 4) if n_cand else None,
        }
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=0, help="random sample of depositors")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    pairs = _load("pairs.json")
    if not pairs:
        raise SystemExit("no labelled pairs; run tools/ens_labels.py all first")
    depositors = sorted({p["depositor"] for p in pairs})
    if args.limit and args.limit < len(depositors):
        depositors = sorted(random.Random(args.seed).sample(depositors, args.limit))
    pairs = [p for p in pairs if p["depositor"] in set(depositors)]

    network = get_network("ethereum")
    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]
    labels = load_attribution("ethereum")
    _log(
        f"[*] {len(depositors)} depositors, {len(pairs)} pairs, {len(clients)} key(s), "
        f"{len(labels)} labelled addresses"
    )

    results, failed = {}, []
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = {
            pool.submit(_run, clients[i % len(clients)], network, d, args.window_days, labels): d
            for i, d in enumerate(depositors)
        }
        for n, fut in enumerate(as_completed(futures), 1):
            dep = futures[fut]
            try:
                results[dep] = fut.result()
            except Exception as exc:  # a failed run is reported, not dropped silently
                failed.append((dep, str(exc)[:120]))
            if n % 10 == 0:
                _log(f"  {n} / {len(depositors)} depositors")

    summary = {
        "depositors": len(depositors),
        "failed_runs": len(failed),
        "window_days": args.window_days,
        "with_linked": evaluate(results, pairs, drop_linked=False),
        "without_linked": evaluate(results, pairs, drop_linked=True),
    }
    _save("evaluation.json", summary)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
