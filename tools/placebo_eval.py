#!/usr/bin/env python3
"""Placebo (target-decoy) estimate of the false-lead rate on real data.

For a random sample of real Ethereum depositors the real pipeline runs twice:

* target: as usual, withdrawals in the window after each deposit;
* decoy: the same deposits shifted back in time so that every search window
  ends a day before the wallet's first real deposit. No withdrawal in a decoy
  window can spend one of the wallet's notes, so every decoy lead is a false
  note link.

Everything else is the real pipeline: the wallet's own history, counterparties,
deposit gas prices, funders. The ratio of decoy to target leads, per band and
per withdrawal searched, estimates the share of target leads that are chance
(a false-discovery rate); one minus it estimates precision. Confidence
intervals come from a bootstrap over depositors.

A decoy lead is a false *note* link, not necessarily a false *identity* link:
a linked address may still belong to the depositor (for instance, an earlier
withdrawal of a note deposited from another of its wallets). The per-family
table shows which signals fire in decoy windows.

Results are cached under .cache/labels/placebo/.

Usage
-----
    python tools/placebo_eval.py --sample 150
    python tools/placebo_eval.py --report-only
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import random
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE, _load, _log, api_keys  # noqa: E402

import tornado_demix.demix as demix  # noqa: E402
from tornado_demix.attribution import load_attribution  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import METHOD_FAMILY, ranked_candidates  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402
from tornado_demix.placebo import decoy_offset  # noqa: E402
from tornado_demix.report_json import _json_default  # noqa: E402

OUT = os.path.join(CACHE, "placebo")
BANDS = ("strong", "moderate", "weak")
DAY = 86400
MAX_SPAN_DAYS = 60  # longer deposit histories would push the decoy too far back
END_TS = 1785542400  # 2026-08-01: target windows must be complete


def universe_file(network="ethereum"):
    """Universe cache file name: universe.json for ethereum, universe_<network>.json otherwise."""
    return "universe.json" if network == "ethereum" else f"universe_{network}.json"


def eligible_depositors(window_days, after_ts=0, network="ethereum", end_ts=None):
    """Sorted depositors whose deposits span at most MAX_SPAN_DAYS and whose decoy
    windows fall after the pool's first withdrawal; with ``after_ts``, only those
    whose first deposit is at or after it."""
    end_ts = END_TS if end_ts is None else end_ts
    uni = _load(universe_file(network))
    if uni is None:
        raise SystemExit(f"missing universe file {universe_file(network)} in {CACHE}")
    first_w = {}
    for pool, _addr, ts, _h in uni["withdrawals"]:
        first_w[pool] = min(first_w.get(pool, ts), ts)
    by_dep = defaultdict(list)
    for pool, addr, ts, _h in uni["deposits"]:
        by_dep[addr].append((pool, ts))
    ok = []
    for addr, deps in by_dep.items():
        ts = [t for _p, t in deps]
        span = max(ts) - min(ts)
        if span > MAX_SPAN_DAYS * DAY or max(ts) + window_days * DAY > end_ts:
            continue
        if min(ts) < after_ts:
            continue
        offset = span + (window_days + 1) * DAY
        if all(t - offset > first_w.get(p, 1 << 62) + DAY for p, t in deps):
            ok.append(addr)
    ok.sort()
    return ok


def _is_addr(stem):
    return (
        len(stem) == 42
        and stem.startswith("0x")
        and all(c in "0123456789abcdefABCDEF" for c in stem[2:])
    )


def wallets_in_dir(base, sub):
    """Wallets that have a run in ``base/sub``: files in ``target/`` if it exists, else
    wallet-named ``<wallet>.json`` directly in the directory ('.' = the top-level sample)."""
    d = os.path.normpath(os.path.join(base, sub))
    t = os.path.join(d, "target")
    src = t if os.path.isdir(t) else d
    if not os.path.isdir(src):
        raise SystemExit(f"exclude dir not found: {src}")
    return {f[:-5].lower() for f in os.listdir(src) if f.endswith(".json") and _is_addr(f[:-5])}


def wallets_in_file(path):
    with open(path, encoding="utf-8") as fh:
        return {ln.strip().lower() for ln in fh if ln.strip() and not ln.startswith("#")}


def sample_hash(addresses):
    """sha256 over the sorted, newline-joined lower-case address list."""
    return hashlib.sha256("\n".join(sorted(a.lower() for a in addresses)).encode()).hexdigest()


def draw_sample(n, seed, window_days, after_ts=0, network="ethereum", end_ts=None, exclude=()):
    """(sample, stats): stats has eligible (before exclusion), excluded, pool, sampled, hash."""
    eligible = eligible_depositors(window_days, after_ts, network, end_ts)
    ex = {a.lower() for a in exclude}
    pool = [a for a in eligible if a.lower() not in ex]
    sample = sorted(random.Random(seed).sample(pool, min(n, len(pool))))
    stats = {
        "eligible": len(eligible),
        "excluded": len(eligible) - len(pool),
        "pool_after_exclusion": len(pool),
        "sampled": len(sample),
        "sample_sha256": sample_hash(sample),
    }
    return sample, stats


def sample_depositors(
    n, seed, window_days, after_ts=0, network="ethereum", end_ts=None, exclude=()
):
    """Random depositors (see ``eligible_depositors``), minus ``exclude``."""
    return draw_sample(n, seed, window_days, after_ts, network, end_ts, exclude)[0]


def _run(client, network, wallet, kind, window_days, labels, target=None):
    """One cached run. The decoy needs the target's deposits to compute its offset."""
    path = os.path.join(OUT, kind, wallet + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    shift = decoy_offset(target["deposits"], window_days) if kind == "decoy" else 0
    with contextlib.redirect_stderr(io.StringIO()):
        data = demix.run_demix(
            client,
            wallet,
            window_days=window_days,
            network=network,
            labels=labels,
            deposit_shift_seconds=shift,
        )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=_json_default)
    return data


def summarize(data):
    """Leads per band, leads per family, withdrawals searched."""
    bands, fams = Counter(), Counter()
    for r in ranked_candidates(data):
        bands[r["band"]] += 1
        if r["band"] in ("strong", "moderate"):
            for f in {METHOD_FAMILY.get(s) for s in r["signals"]} - {None}:
                fams[f] += 1
            for s in r["signals"]:
                fams["signal:" + s] += 1
    exposure = sum(sum(res.get("counts", {}).values()) for res in data.get("denoms", {}).values())
    return {"bands": bands, "families": fams, "exposure": exposure}


def estimate(pairs, upto):
    """FDR = decoy leads per withdrawal / target leads per withdrawal, bands up to ``upto``."""
    keep = BANDS[: BANDS.index(upto) + 1]
    t = sum(sum(p[0]["bands"][b] for b in keep) for p in pairs)
    d = sum(sum(p[1]["bands"][b] for b in keep) for p in pairs)
    te = sum(p[0]["exposure"] for p in pairs)
    de = sum(p[1]["exposure"] for p in pairs)
    if not t or not te or not de:
        return None, t, d, te, de
    return (d / de) / (t / te), t, d, te, de


def report(results, boot=2000, seed=7):
    pairs = [(summarize(v["target"]), summarize(v["decoy"])) for v in results.values()]
    pairs = [p for p in pairs if p[0]["exposure"] and p[1]["exposure"]]
    out = {"depositors": len(pairs)}
    rng = random.Random(seed)
    for upto in BANDS:
        fdr, t, d, te, de = estimate(pairs, upto)
        samples = []
        for _ in range(boot):
            s = [pairs[rng.randrange(len(pairs))] for _ in pairs]
            f = estimate(s, upto)[0]
            if f is not None:
                samples.append(f)
        samples.sort()
        ci = (
            [
                round(samples[int(0.025 * len(samples))], 3),
                round(samples[int(0.975 * len(samples)) - 1], 3),
            ]
            if samples
            else None
        )
        out["up_to_" + upto] = {
            "target_leads": t,
            "decoy_leads": d,
            "target_withdrawals": te,
            "decoy_withdrawals": de,
            "fdr": round(fdr, 3) if fdr is not None else None,
            "fdr_95ci": ci,
            "precision_estimate": round(max(0.0, 1 - fdr), 3) if fdr is not None else None,
        }
    fam_t, fam_d = Counter(), Counter()
    for tp, dp in pairs:
        fam_t.update(tp["families"])
        fam_d.update(dp["families"])
    out["strong_moderate_by_family"] = {
        k: {"target": fam_t[k], "decoy": fam_d[k]} for k in sorted(set(fam_t) | set(fam_d))
    }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument(
        "--after", default=None, help="only depositors whose first deposit is on or after this date"
    )
    ap.add_argument(
        "--dir",
        default="",
        help="sub-directory of the placebo cache for the runs and the summary (keeps samples apart)",
    )
    ap.add_argument("--network", choices=("ethereum", "polygon", "arbitrum"), default="ethereum")
    ap.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="exclude wallets with runs in placebo/<DIR> (repeatable; '.' = the top-level sample)",
    )
    ap.add_argument(
        "--exclude-file", default=None, help="file with one address per line to exclude"
    )
    ap.add_argument(
        "--end-date", default=None, help="YYYY-MM-DD, replaces the default END_TS (2026-08-01)"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print eligible/excluded/sampled counts and hash only",
    )
    args = ap.parse_args(argv)
    global OUT
    base = OUT
    OUT = os.path.join(OUT, args.dir) if args.dir else OUT
    end_ts = (
        int(datetime.fromisoformat(args.end_date).replace(tzinfo=timezone.utc).timestamp())
        if args.end_date
        else END_TS
    )
    exclude = set()
    for sub in args.exclude_dir:
        exclude |= wallets_in_dir(base, sub)
    if args.exclude_file:
        exclude |= wallets_in_file(args.exclude_file)
    after_ts = (
        int(datetime.fromisoformat(args.after).replace(tzinfo=timezone.utc).timestamp())
        if args.after
        else 0
    )

    depositors, stats = draw_sample(
        args.sample, args.seed, args.window_days, after_ts, args.network, end_ts, exclude
    )
    stats.update(
        network=args.network,
        seed=args.seed,
        window_days=args.window_days,
        end_ts=end_ts,
        exclude_dirs=args.exclude_dir,
        exclude_file=args.exclude_file,
        exclude_list_size=len(exclude),
    )
    _log(
        f"[*] eligible {stats['eligible']}, excluded {stats['excluded']}, "
        f"sampled {stats['sampled']}, sha256 {stats['sample_sha256']}"
    )
    if args.dry_run:
        print(json.dumps(stats, indent=1))
        return stats
    network = get_network(args.network)
    results = {}
    if args.report_only:
        for w in depositors:
            t, d = (os.path.join(OUT, k, w + ".json") for k in ("target", "decoy"))
            if os.path.exists(t) and os.path.exists(d):
                results[w] = {
                    k: json.load(open(p, encoding="utf-8"))
                    for k, p in (("target", t), ("decoy", d))
                }
    else:
        clients = [EtherscanClient(k, pause=0.36, **network.client_kwargs()) for k in api_keys()]
        labels = load_attribution(args.network)
        _log(f"[*] {len(depositors)} depositors, {len(clients)} key(s)")
        failed = []

        def both(i, w):
            c = clients[i % len(clients)]
            target = _run(c, network, w, "target", args.window_days, labels)
            decoy = _run(c, network, w, "decoy", args.window_days, labels, target)
            return {"target": target, "decoy": decoy}

        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            futs = {pool.submit(both, i, w): w for i, w in enumerate(depositors)}
            for n, fut in enumerate(as_completed(futs), 1):
                w = futs[fut]
                try:
                    results[w] = fut.result()
                except Exception as exc:  # reported, not dropped silently
                    failed.append((w, str(exc)[:120]))
                if n % 10 == 0:
                    _log(f"  {n} / {len(depositors)}")
        if failed:
            _log(f"[!] {len(failed)} failed: {failed[:5]}")
    summary = report(results)
    summary["sampling"] = stats
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
