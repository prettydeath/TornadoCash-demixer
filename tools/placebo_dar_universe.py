#!/usr/bin/env python3
"""Placebo test of the shared exchange-deposit-address signal at scale.

Unlike tools/placebo_dar.py, which reuses full demix runs, this reads the
search windows offline from the ENS universe (tools/ens_labels.py: every
deposit and withdrawal of the four Ethereum ETH pools): per random depositor,
vouchers are its deposits per pool grouped with a 24-hour gap, the target
windows run 30 days after each voucher, and the decoy windows are the same
shifted back so that they end a day before the first deposit. Recipients and
withdrawals searched come from the universe; only the depositor's exchange
deposit addresses need the explorer, and they are cached under
.cache/labels/placebo/dar/.

Two definitions are reported: ``loose`` (the module defaults) and ``strict``
(sweeps to a single labelled exchange or to unlabelled hot wallets only, and at
most 10 senders).

Usage
-----
    python tools/placebo_dar_universe.py --sample 1000 --seed 2
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import _load, _log, api_keys  # noqa: E402
from placebo_dar import DAR, deposit_addresses  # noqa: E402
from placebo_eval import END_TS, MAX_SPAN_DAYS  # noqa: E402

from tornado_demix.attribution import load_attribution  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.rpc import make_contract_check  # noqa: E402

DAY = 86400
WINDOW = 30 * DAY
GAP = 24 * 3600
STRICT_MAX_SENDERS = 10


def vouchers(deps):
    """[(pool, first_ts, last_ts)] grouping one pool's deposits with a 24 h gap."""
    out = []
    by_pool = defaultdict(list)
    for pool, ts in deps:
        by_pool[pool].append(ts)
    for pool, stamps in by_pool.items():
        stamps.sort()
        first = last = stamps[0]
        for t in stamps[1:]:
            if t - last > GAP:
                out.append((pool, first, last))
                first = t
            last = t
        out.append((pool, first, last))
    return out


def window_recipients(wd_index, vs, shift):
    """Recipients and withdrawal count in the (shifted) windows."""
    recips = set()
    n = 0
    for pool, first, last in vs:
        ts, addrs = wd_index[pool]
        lo, hi = first - shift, last + WINDOW - shift
        i, j = bisect.bisect_left(ts, lo), bisect.bisect_right(ts, hi)
        recips.update(addrs[i:j])
        n += j - i
    return recips, n


def strict(info):
    entities = [e for e in (info.get("exchange") or "").split(", ") if e]
    return len(entities) <= 1 and len(info["senders"]) <= STRICT_MAX_SENDERS


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument(
        "--no-labels",
        action="store_true",
        help="ignore the attribution set: hot wallets are recognised by activity only",
    )
    args = ap.parse_args(argv)

    uni = _load("universe.json")
    first_w = {}
    wd = defaultdict(list)
    for pool, addr, ts, _h in uni["withdrawals"]:
        wd[pool].append((ts, addr))
        first_w[pool] = min(first_w.get(pool, ts), ts)
    wd_index = {}
    for pool, rows in wd.items():
        rows.sort()
        wd_index[pool] = ([t for t, _a in rows], [a for _t, a in rows])
    by_dep = defaultdict(list)
    for pool, addr, ts, _h in uni["deposits"]:
        by_dep[addr].append((pool, ts))

    ok = []
    for addr, deps in by_dep.items():
        ts = [t for _p, t in deps]
        span = max(ts) - min(ts)
        if span > MAX_SPAN_DAYS * DAY or max(ts) + WINDOW > END_TS:
            continue
        offset = span + WINDOW + DAY
        if all(t - offset > first_w.get(p, 1 << 62) + DAY for p, t in deps):
            ok.append(addr)
    ok.sort()
    sample = sorted(random.Random(args.seed).sample(ok, min(args.sample, len(ok))))

    network = get_network("ethereum")
    clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]
    labels = {} if args.no_labels else load_attribution("ethereum")
    cache = os.path.join(os.path.dirname(DAR), "dar_nolabels") if args.no_labels else DAR
    is_contract = make_contract_check(network.rpc_url)
    _log(f"[*] {len(sample)} depositors of {len(ok)} eligible, {len(clients)} key(s)")

    rows = []

    def one(i, wallet):
        deps = by_dep[wallet]
        vs = vouchers(deps)
        ts = [t for _p, t in deps]
        offset = max(ts) - min(ts) + WINDOW + DAY
        dep = deposit_addresses(
            clients[i % len(clients)], network, wallet, labels, is_contract, cache
        )
        tr, tn = window_recipients(wd_index, vs, 0)
        dr, dn = window_recipients(wd_index, vs, offset)
        out = {
            "wallet": wallet,
            "target_withdrawals": tn,
            "decoy_withdrawals": dn,
            "deposit_addresses": len(dep),
        }
        for name, keep in (("loose", lambda x: True), ("strict", strict)):
            senders = {s for x in dep if keep(x) for s in x["senders"]} - {wallet}
            out[name] = {"target": sorted(senders & tr), "decoy": sorted(senders & dr)}
        return out

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futs = {pool.submit(one, i, w): w for i, w in enumerate(sample)}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                rows.append(fut.result())
            except Exception as exc:  # reported, not dropped silently
                _log(f"[!] {futs[fut]}: {str(exc)[:120]}")
            if n % 50 == 0:
                _log(f"  {n} / {len(sample)}")

    def est(rs, name):
        t = sum(len(r[name]["target"]) for r in rs)
        d = sum(len(r[name]["decoy"]) for r in rs)
        te = sum(r["target_withdrawals"] for r in rs)
        de = sum(r["decoy_withdrawals"] for r in rs)
        return ((d / de) / (t / te) if t and te and de else None), t, d, te, de

    rng = random.Random(6)
    summary = {
        "depositors": len(rows),
        "with_deposit_address": sum(1 for r in rows if r["deposit_addresses"]),
    }
    for name in ("loose", "strict"):
        v, t, d, te, de = est(rows, name)
        bs = sorted(
            x
            for x in (
                est([rows[rng.randrange(len(rows))] for _ in rows], name)[0] for _ in range(2000)
            )
            if x is not None
        )
        summary[name] = {
            "target_hits": t,
            "decoy_hits": d,
            "target_withdrawals": te,
            "decoy_withdrawals": de,
            "depositors_with_target_hit": sum(1 for r in rows if r[name]["target"]),
            "fdr": round(v, 3) if v is not None else None,
            "fdr_95ci": [round(bs[int(0.025 * len(bs))], 3), round(bs[int(0.975 * len(bs)) - 1], 3)]
            if bs
            else None,
        }
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        ".cache",
        "labels",
        "placebo",
        "dar_universe_nolabels.json" if args.no_labels else "dar_universe.json",
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
