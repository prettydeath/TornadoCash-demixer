#!/usr/bin/env python3
"""Placebo measurement of the Merkle-root exclusion on cached runs (offline on the runs).

For every cached target (real window) and decoy run of a directory under
``.cache/labels/placebo``, the withdrawals of the ranked candidates are classified by
the root they prove against, exactly as ``tornado_demix.root_exclusion`` does in
``run_demix(root_check=True)``: the wallet's leaves come from ``wallet_leaves`` (the real
leaf of each deposit in a target run; the emulated leaf, insertions up to the shifted
deposit's block, in a decoy run), the root from the withdrawal's calldata and its leaf
count from the pool's cached root index.

The runs are NOT re-run and NOT re-scored: a candidate counts as *fully excluded* when
every one of its withdrawals in the pool is excluded, *partly excluded* when some are.
Candidates of every band are read for the first ``--depositors`` depositors that have one
(sample order = sorted file names); weak candidates only for the first ``--weak-depositors``
depositors, since a weak candidate is a recipient of a voucher-sized count and there are
thousands. A decoy run is also classified with the REAL leaves as a sanity row: every
decoy withdrawal predates them, so nearly everything is excluded, which is why the decoy
leaf is emulated.

Usage
-----
    python tools/root_exclusion_eval.py --network ethereum --dir holdout2 --weak-depositors 40
    python tools/root_exclusion_eval.py --network polygon --dir polygon2 --weak-depositors 40

Root indexes must be cached in .cache/merkle (built by ``cached_root_index``, about 30
minutes for a large pool); a pool without one is skipped and counted.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tornado_demix.heuristics import ranked_candidates  # noqa: E402
from tornado_demix.registry import record_safe as record  # noqa: E402
from tornado_demix.root_exclusion import (  # noqa: E402
    _hex,
    _own_withdrawals,
    classify,
    wallet_leaves,
    wallet_notes,
)
from tornado_demix.withdraw_root import root_from_input  # noqa: E402

BANDS = ("strong", "moderate", "weak")


def _load_runs(runs_dir, limit):
    """{(kind, wallet): data} for the first ``limit`` depositors present in both kinds."""
    names = sorted(
        os.path.basename(p) for p in glob.glob(os.path.join(runs_dir, "target", "*.json"))
    )
    names = [n for n in names if os.path.exists(os.path.join(runs_dir, "decoy", n))]
    if limit:
        names = names[:limit]
    return names


def _read(runs_dir, kind, name):
    with open(os.path.join(runs_dir, kind, name), encoding="utf-8") as fh:
        return json.load(fh)


def _shard(items, n):
    out = [[] for _ in range(n)]
    for i, item in enumerate(items):
        out[i % n].append(item)
    return out


def _parallel(items, fn, clients):
    """Run ``fn(client, item)`` over ``items``, one thread per client; {item: result}."""
    results, lock = {}, threading.Lock()

    def work(client, shard):
        for item in shard:
            try:
                value = fn(client, item)
            except Exception as exc:  # a provider hiccup loses one item, not the run
                print(f"  [!] {item!r:.60}: {exc}", file=sys.stderr, flush=True)
                continue
            with lock:
                results[item] = value

    with ThreadPoolExecutor(max_workers=len(clients)) as ex:
        list(ex.map(lambda a: work(*a), zip(clients, _shard(items, len(clients)))))
    return results


def _record_summary(summary):
    """One register record per kind (target / decoy) of a summary."""
    for kind, res in summary["kinds"].items():
        record(
            f"root_exclusion.{kind}",
            __file__,
            {"network": summary.get("network"), "dir": summary.get("dir")},
            res,
        )


def _render(out_dir):
    """RESULTS.md from the summary_<network>.json files in ``out_dir``."""
    lines = [
        "# Merkle-root exclusion: placebo measurement",
        "",
        "Tool: tools/root_exclusion_eval.py, offline on the cached runs (holdout2 Ethereum, "
        "polygon2 Polygon); the runs are not re-run and not re-scored.",
        "Candidates are the ranked candidates of each cached run (all bands). Weak candidates "
        "only for the first N depositors in sorted file order (N below); moderate and strong "
        "candidates for all depositors.",
        "Target = real window, wallet leaves are the real leaf indices. Decoy = decoy window, "
        "wallet leaves emulated (insertions up to the shifted deposit's block).",
        "Fully excluded = every withdrawal of the candidate in its pool is excluded "
        "(n_root <= min wallet leaf). Partly = some. Note-partial withdrawals "
        "(min < n_root <= max leaf) are context and not removed.",
        "",
    ]
    for network in ("ethereum", "polygon"):
        path = os.path.join(out_dir, f"summary_{network}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            sm = json.load(fh)
        lines += [
            f"## {network} ({sm['dir']}, {sm['depositors']} depositors, weak candidates of the "
            f"first {sm['weak_depositors']})",
            "",
            "| | target | decoy |",
            "|---|---|---|",
        ]
        t, d = sm["kinds"]["target"], sm["kinds"]["decoy"]

        def row(label, a, b, out=lines):
            out.append(f"| {label} | {a} | {b} |")

        for band in BANDS:
            row(
                f"{band} candidates read",
                t["candidates"].get(band, 0),
                d["candidates"].get(band, 0),
            )
        row("candidate withdrawals checked", t["withdrawals_checked"], d["withdrawals_checked"])
        row(
            "decoy gap, not checked",
            t.get("decoy_gap_not_checked", 0),
            d.get("decoy_gap_not_checked", 0),
        )
        row("roots found in the index", t["roots_found"], d["roots_found"])
        row("roots not found", t["roots_not_found"], d["roots_not_found"])
        row("withdrawals excluded", t["withdrawals_excluded"], d["withdrawals_excluded"])
        row(
            "withdrawals note-partial (context)",
            t["withdrawals_note_partial"],
            d["withdrawals_note_partial"],
        )
        for band in BANDS:
            row(
                f"{band} candidates fully excluded",
                t["candidates_fully_excluded"].get(band, 0),
                d["candidates_fully_excluded"].get(band, 0),
            )
        for band in BANDS:
            row(
                f"{band} candidates partly excluded",
                t["candidates_partly_excluded"].get(band, 0),
                d["candidates_partly_excluded"].get(band, 0),
            )
        for key, label in (
            ("withdrawals_excluded", "old rule (no gap exemption): withdrawals excluded"),
            ("of_which_in_gap", "  of which in the gap (before the emulated note's insertion)"),
            ("of_which_n_root_equals_emulated_leaf", "  of which n_root == emulated leaf"),
            ("n_root_equals_leaf_and_in_gap", "  n_root == leaf and in the gap"),
        ):
            row(
                label,
                t.get("rule_without_gap_exemption", {}).get(key, "-"),
                d.get("rule_without_gap_exemption", {}).get(key, "-"),
            )
        row(
            "sanity: withdrawals excluded with the REAL leaves",
            t["real_leaf_sanity"]["withdrawals_excluded"],
            d["real_leaf_sanity"]["withdrawals_excluded"],
        )
        skipped = sm["skipped_candidate_rows"]
        lines += [
            "",
            "Candidate rows skipped (no root index, or a leaf lookup failed): "
            + (", ".join(f"{k} {n}" for k, n in skipped.items()) or "none"),
            "",
        ]
    with open(os.path.join(out_dir, "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--network", default="ethereum")
    ap.add_argument("--dir", required=True, help="run directory under .cache/labels/placebo")
    ap.add_argument("--depositors", type=int, default=0, help="first N depositors (0: all)")
    ap.add_argument("--weak-depositors", type=int, default=40, help="weak candidates: first N")
    ap.add_argument("--merkle-dir", default=None)
    ap.add_argument(
        "--render", action="store_true", help="only write RESULTS.md from the summaries"
    )
    args = ap.parse_args(argv)

    from ens_labels import CACHE, api_keys

    from tornado_demix.etherscan import EtherscanClient
    from tornado_demix.networks import get_network

    network = get_network(args.network)
    runs_dir = os.path.join(CACHE, "placebo", args.dir)
    out_dir = os.path.join(CACHE, "placebo", "root_exclusion")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    merkle_dir = args.merkle_dir or os.path.join(repo, ".cache", "merkle")
    os.makedirs(out_dir, exist_ok=True)
    if args.render:
        _render(out_dir)
        return 0
    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]
    names = _load_runs(runs_dir, args.depositors)
    weak_names = set(names[: args.weak_depositors])

    # 1. what to read: candidates of the sampled runs
    runs = {}  # (kind, name) -> data
    items = []  # (kind, name, pool_key, addr, band, [(tx, nullifier, pos)])
    for name in names:
        for kind in ("target", "decoy"):
            data = _read(runs_dir, kind, name)
            runs[(kind, name)] = data
            for row in ranked_candidates(data):
                if row["band"] == "weak" and name not in weak_names:
                    continue
                res = data["denoms"][row["pool_key"]]
                pairs = _own_withdrawals(res, row["address"])
                if not pairs or len(pairs) != len(res["detail"][row["address"]]):
                    continue
                items.append(
                    (
                        kind,
                        name,
                        row["pool_key"],
                        row["address"],
                        row["band"],
                        tuple((w["tx_hash"], w["nullifier"], w["block"]) for _pos, w in pairs),
                    )
                )
    print(f"{len(names)} depositors, {len(items)} candidate rows", file=sys.stderr, flush=True)

    # 2. root indexes (cached only)
    indexes = {}
    for pool_key in sorted({i[2] for i in items}):
        pool = network.by_key[pool_key]
        path = os.path.join(merkle_dir, f"{getattr(clients[0], 'chain_id', 1)}_{pool.address}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                indexes[pool_key] = json.load(fh)["roots"]
        else:
            print(f"  [!] no root index for {pool_key}: skipped", file=sys.stderr, flush=True)

    # 3. roots of the withdrawals (disk cache keyed by tx:nullifier)
    roots_path = os.path.join(out_dir, f"roots_{args.network}.json")
    roots = json.load(open(roots_path, encoding="utf-8")) if os.path.exists(roots_path) else {}
    todo = sorted(
        {
            (tx, nu)
            for i in items
            if i[2] in indexes
            for tx, nu, _b in i[5]
            if f"{tx}:{nu}" not in roots
        }
    )
    print(f"{len(todo)} transaction lookups", file=sys.stderr, flush=True)

    def read_root(client, item):
        tx, nu = item
        info = client.tx_info(tx)
        root = root_from_input((info or {}).get("input") or "", nu) if info else None
        return "" if root is None else _hex(root)

    got = _parallel(todo, read_root, clients)
    for (tx, nu), root in got.items():
        roots[f"{tx}:{nu}"] = root
    with open(roots_path, "w", encoding="utf-8") as fh:
        json.dump(roots, fh)

    # 4. wallet leaves per (kind, run, pool): real and, for decoys, emulated
    keys = sorted({(i[0], i[1], i[2]) for i in items if i[2] in indexes})

    def leaves_for(client, key):
        kind, name, pool_key = key
        data = runs[(kind, name)]
        deps = [d for d in data["deposits"] if d["pool_key"] == pool_key]
        pool = network.by_key[pool_key]
        real = wallet_leaves(client, pool, deps, shifted=False)
        if kind == "decoy":
            notes = wallet_notes(client, pool, deps, shifted=True)
            used, since = (None, None) if notes is None else notes
        else:
            used, since = real, None
        return real, used, since

    leaves = _parallel(keys, leaves_for, clients)

    # 5. classify
    def tally():
        return {
            "candidates": Counter(),
            "withdrawals": 0,
            "found": 0,
            "not_found": 0,
            "excl_w": 0,
            "partial_w": 0,
            "full": Counter(),
            "some": Counter(),
            "real_leaf_full": Counter(),
            "real_leaf_excl_w": 0,
            "gap": 0,
            "old_excl_w": 0,
            "old_in_gap": 0,
            "old_eq_leaf": 0,
            "old_eq_leaf_in_gap": 0,
            "old_full": Counter(),
        }

    table = defaultdict(tally)
    skipped = Counter()
    for kind, name, pool_key, _addr, band, wds in items:
        t = table[kind]
        if pool_key not in indexes:
            skipped[(kind, band)] += 1
            continue
        got_leaves = leaves.get((kind, name, pool_key))
        if not got_leaves or got_leaves[1] is None or got_leaves[0] is None:
            skipped[(kind, band)] += 1
            continue
        real, used, since = got_leaves
        t["candidates"][band] += 1
        excl = real_excl = old_excl = 0
        for tx, nu, block in wds:
            root = roots.get(f"{tx}:{nu}")
            n_root = indexes[pool_key].get(root) if root else None
            in_gap = since is not None and block < since
            if n_root is not None and classify(n_root, used) == "excluded":
                # what the rule without the gap exemption would have excluded
                old_excl += 1
                t["old_excl_w"] += 1
                t["old_in_gap"] += in_gap
                if n_root == min(used):
                    t["old_eq_leaf"] += 1
                    t["old_eq_leaf_in_gap"] += in_gap
            if in_gap:
                t["gap"] += 1
                continue
            t["withdrawals"] += 1
            if n_root is None:
                t["not_found"] += 1
                continue
            t["found"] += 1
            kind_w = classify(n_root, used)
            if kind_w == "excluded":
                excl += 1
            elif kind_w == "partial":
                t["partial_w"] += 1
            if classify(n_root, real) == "excluded":
                real_excl += 1
        t["excl_w"] += excl
        t["real_leaf_excl_w"] += real_excl
        if excl == len(wds):
            t["full"][band] += 1
        elif excl:
            t["some"][band] += 1
        if real_excl == len(wds):
            t["real_leaf_full"][band] += 1
        if old_excl == len(wds):
            t["old_full"][band] += 1

    summary = {
        "network": args.network,
        "dir": args.dir,
        "depositors": len(names),
        "weak_depositors": len(weak_names),
        "skipped_candidate_rows": {f"{k}/{b}": n for (k, b), n in sorted(skipped.items())},
        "kinds": {
            kind: {
                "candidates": dict(t["candidates"]),
                "withdrawals_checked": t["withdrawals"],
                "roots_found": t["found"],
                "roots_not_found": t["not_found"],
                "decoy_gap_not_checked": t["gap"],
                "withdrawals_excluded": t["excl_w"],
                "withdrawals_note_partial": t["partial_w"],
                "candidates_fully_excluded": dict(t["full"]),
                "candidates_partly_excluded": dict(t["some"]),
                "rule_without_gap_exemption": {
                    "withdrawals_excluded": t["old_excl_w"],
                    "of_which_in_gap": t["old_in_gap"],
                    "of_which_n_root_equals_emulated_leaf": t["old_eq_leaf"],
                    "n_root_equals_leaf_and_in_gap": t["old_eq_leaf_in_gap"],
                    "candidates_fully_excluded": dict(t["old_full"]),
                },
                "real_leaf_sanity": {
                    "withdrawals_excluded": t["real_leaf_excl_w"],
                    "candidates_fully_excluded": dict(t["real_leaf_full"]),
                },
            }
            for kind, t in sorted(table.items())
        },
    }
    print(json.dumps(summary, indent=1))
    with open(os.path.join(out_dir, f"summary_{args.network}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    _render(out_dir)
    _record_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
