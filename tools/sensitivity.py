#!/usr/bin/env python3
"""How much the candidate ranking depends on the signal weights.

The weights in ``tornado_demix.heuristics.SIGNAL_WEIGHTS`` are expert choices.
This tool re-scores saved demix results (``tornado-demix demix --json``) with
randomly perturbed weights and reports how the ranking moves. No API calls are
made.

The evidence band is computed from which signals hold, not from the weights, so
it cannot change under any weighting; the tool checks that and reports only
movements of the order. For each result it prints:

* the reference candidate (the one given on the command line, otherwise the
  top candidate at nominal weights) and its nominal rank;
* how often it keeps that rank, how often the top candidate stays the same;
* the median and worst rank it reaches;
* the mean Kendall tau between the nominal and the perturbed order.

Usage
-----
    python tools/sensitivity.py dai.json avax.json:0x905b6e02... --samples 1000 --spread 0.5
"""

import argparse
import copy
import json
import os
import random
import statistics
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix import heuristics  # noqa: E402
from tornado_demix.heuristics import SIGNAL_WEIGHTS, _score, ranked_candidates  # noqa: E402

MAX_WEIGHT = 0.95


@contextmanager
def weights(overrides):
    """Temporarily replace SIGNAL_WEIGHTS in place, so _score uses them."""
    saved = dict(SIGNAL_WEIGHTS)
    SIGNAL_WEIGHTS.update(overrides)
    try:
        yield
    finally:
        SIGNAL_WEIGHTS.clear()
        SIGNAL_WEIGHTS.update(saved)


def perturbed(rng, spread):
    """Each weight scaled by an independent factor from U(1 - spread, 1 + spread)."""
    return {
        name: min(MAX_WEIGHT, max(0.0, w * rng.uniform(1 - spread, 1 + spread)))
        for name, w in heuristics.SIGNAL_WEIGHTS.items()
    }


def rescore(data):
    """Recompute every stored confidence with the current SIGNAL_WEIGHTS."""
    for res in data.get("denoms", {}).values():
        for addr, signals in res.get("signals", {}).items():
            res["confidence"][addr] = _score(set(signals), res["discrimination"].get(addr, 0.0))


def order(data):
    return [(r["pool_key"], r["address"]) for r in ranked_candidates(data)]


def bands(data):
    return {(r["pool_key"], r["address"]): r["band"] for r in ranked_candidates(data)}


def kendall_tau(reference, other):
    """Kendall tau over the items of ``reference`` (all present in ``other``)."""
    pos = {item: i for i, item in enumerate(other)}
    n = len(reference)
    if n < 2:
        return 1.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if pos[reference[i]] < pos[reference[j]]:
                concordant += 1
            else:
                discordant += 1
    return (concordant - discordant) / (n * (n - 1) / 2)


def analyse(data, reference_address=None, samples=1000, spread=0.5, seed=1):
    """Return the sensitivity summary for one demix result."""
    data = copy.deepcopy(data)
    rescore(data)
    nominal = order(data)
    nominal_bands = bands(data)
    if not nominal:
        return {"candidates": 0}
    if reference_address:
        ref = next((item for item in nominal if item[1] == reference_address.lower()), None)
        if ref is None:
            raise ValueError(f"{reference_address} is not among the ranked candidates")
    else:
        ref = nominal[0]
    ref_rank = nominal.index(ref) + 1

    rng = random.Random(seed)
    ranks, taus, same_top = [], [], 0
    for _ in range(samples):
        with weights(perturbed(rng, spread)):
            rescore(data)
            current = order(data)
            if bands(data) != nominal_bands:
                raise AssertionError("a band changed with the weights")
        ranks.append(current.index(ref) + 1)
        taus.append(kendall_tau(nominal, current))
        same_top += current[0] == nominal[0]
    return {
        "candidates": len(nominal),
        "reference": ref[1],
        "pool_key": ref[0],
        "band": nominal_bands[ref],
        "nominal_rank": ref_rank,
        "keeps_rank": sum(r == ref_rank for r in ranks) / samples,
        "same_top": same_top / samples,
        "median_rank": statistics.median(ranks),
        "worst_rank": max(ranks),
        "mean_tau": statistics.fmean(taus),
    }


def load(path):
    """A demix result from a ``--json`` file (the raw result lives under "result")."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("result", doc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="+", help="demix JSON files, optionally path:address")
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--spread", type=float, default=0.5, help="relative weight change")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    print(
        f"weights x U({1 - args.spread:.2f}, {1 + args.spread:.2f}), "
        f"{args.samples} samples, seed {args.seed}\n"
    )
    print(
        "| result | candidates | reference | band | rank | keeps rank | same top | "
        "median / worst rank | mean tau |"
    )
    print("|---|---:|---|---|---:|---:|---:|---|---:|")
    for spec in args.results:
        path, address = spec, ""
        head, sep, tail = spec.rpartition(":")
        if sep and tail.lower().startswith("0x"):
            path, address = head, tail
        s = analyse(load(path), address or None, args.samples, args.spread, args.seed)
        name = os.path.basename(path)
        if not s["candidates"]:
            print(f"| {name} | 0 | - | - | - | - | - | - | - |")
            continue
        print(
            f"| {name} | {s['candidates']} | {s['reference'][:10]}... ({s['pool_key']}) | "
            f"{s['band']} | {s['nominal_rank']} | {s['keeps_rank']:.1%} | {s['same_top']:.1%} | "
            f"{s['median_rank']:g} / {s['worst_rank']} | {s['mean_tau']:.3f} |"
        )


if __name__ == "__main__":
    main()
