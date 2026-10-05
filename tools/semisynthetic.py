#!/usr/bin/env python3
"""Semi-synthetic benchmark: planted exits on a real Tornado background.

tools/simulate.py generates both the depositor and the field. Here the field is
real and only the depositor is planted, so precision is measured against real
background behaviour (busy recipients, real counts, real self-relay and gas
habits, real chance links), and recall against planted exits whose operational
errors are drawn at rates that may differ strongly from what the model assumes.
No explorer calls: everything comes from the cached placebo runs.

Background
----------
One background unit is one cached run of tools/placebo_eval.py (152 depositors
from 2019-2022 under ``placebo/``, 150 after 2022 under ``placebo/post2022/``).
Two kinds of window are used, both real:

* ``target`` - the pool windows after the real wallet's deposits (as requested:
  real withdrawals and recipients exactly as cached);
* ``decoy``  - the same wallet's windows shifted to before its first deposit.

The planted depositor *takes over the real wallet's identity*: its address, its
direct counterparties, the withdrawal senders and exchange deposit addresses
found for it, and its deposit gas prices. Its deposits, however, are synthetic
(the real deposits are removed), so the count targets, the early-exit windows
and the early multi-pool profile are those of the planted voucher. The inherited
link evidence is what makes the background realistic for the lead signals:
without it no background recipient could ever earn a lead and the false-lead
rate would be zero by construction. In a ``decoy`` window every inherited link
hit is chance (the windows cannot hold the wallet's notes), so the ``decoy``
false strong / moderate rate is a clean chance rate. In a ``target`` window a
background recipient with an inherited link may be one of the real wallet's
genuine exits; it still counts as a negative here (it did not receive a planted
note), so ``target`` precision is a conservative lower bound.

The real wallet's own gas-price matches are kept only where the full run's gate
passed (as tools/placebo_windows.narrow does); every other background gas price
is dropped, so a planted gas price cannot be diluted or credited by prices the
run never checked. ``profile_match`` (a multi-wallet signal of
``multi.correlate``) is not used: every run is a single wallet, like the
placebo runs.

Planted depositor
-----------------
* Profile: a deposit session drawn from the universe (``universe.json``; all
  Ethereum Tornado deposits): deposits of one sender clustered with the
  project's own ``demix.cluster_vouchers`` rule (24 h gap) per pool, sessions
  split at a 24 h gap across pools, notes per pool capped at
  :data:`MAX_NOTES`. Its pools are mapped onto the run's pools (at most as many
  as the run has); a run with one pool cannot host a multi-pool session, which
  is then reduced to its largest pool.
* Deposits: in each pool at the start of the run's first window there, 10
  minutes apart, with one of the real wallet's deposit gas prices.
* Exits: ``single`` (all notes to one fresh address) or ``spread`` (k exit
  addresses, k uniform in 1..notes). Each exit draws a base delay after the
  pool's last deposit (log-normal: ``fast`` median 2 h, ``moderate`` median 24 h,
  ``heavy`` median 72 h with sigma 2.5, i.e. about half later than 72 h and a
  fifth beyond the 30-day window); each note adds an exponential jitter of mean
  1 h. A withdrawal after the search window is not observed, but its exit still
  counts as a positive (a miss).
* Operational errors per exit, independently: self-relayed (``p_self``); a
  self-relayed withdrawal reuses a deposit gas price (``p_gas``); direct
  transaction with the depositor (``p_link``; whether it is a lead then depends
  on the delay, since ``linked`` needs a first withdrawal within 72 h); a
  withdrawal sent by the depositor (``p_sender``); a shared exchange deposit
  address (``p_shared``); an exit that is an existing, busy background
  recipient instead of a fresh address (``p_reuse``). The early multi-pool
  profile is not planted: it fires on its own when one exit receives a wide
  enough profile within 72 h.

Error grid (``ERRORS``): ``none`` (no operational error at all), ``rare``
(about ten times below the model-like level), ``model`` (the rates
tools/simulate.py assumes: link 0.2, self-relay 0.5, gas 0.3, plus sender 0.05
and shared 0.1) and ``frequent`` (three times or more above it). Crossed with
the three delay laws and the two split modes, plus one ``reuse`` scenario.

Scoring and metrics
-------------------
After injection, counts, the field size, ``target_counts`` and the count gate
are recomputed, and the current ``heuristics.apply_heuristics`` and
``ranked_candidates`` score and rank the window (``count_discrimination`` is
routed through an exact cached equivalent for speed; a test checks it gives
identical signals and scores). One instance per (pool,
recipient) pair; planted exit pairs are positives, all other recipients
negatives. Per scenario, pooled over replicates: for ``strong``, ``>=
moderate`` and ``any candidate`` (and per exact band) precision and recall;
top-1 / top-5 hit rate (a planted exit among the first k rows); PR-AUC of the
band-then-score order (tools/simulate.average_precision); false strong and
false >= moderate per 1,000 background pairs and the share of replicates with
at least one; and per signal the precision / recall of "pair has the signal".
95 % CIs: percentile bootstrap over background runs (replicates on the same run
are resampled together).

What it can and cannot show: the background is real, so false-lead rates and
the dilution of count matches by real busy recipients are measured, not
modelled. The positives are still planted by our own model of operational
errors (independent Bernoulli errors, log-normal delays, fresh exits), so
recall is only as realistic as that model; the grid shows how recall moves
when the error rates are far from the assumed ones, not what they are on real
cases. The inherited counterparty graph is one real wallet's; an exit linked
to the depositor by a path the pipeline does not look for is not modelled.

Outputs: ``.cache/labels/placebo/semisynthetic/summary.json`` and
``SEMISYNTHETIC.md`` next to it.

Usage
-----
    python tools/semisynthetic.py --replicates 4 --seed 1
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE  # noqa: E402

import tornado_demix.heuristics as heuristics  # noqa: E402
from tornado_demix.demix import cluster_vouchers  # noqa: E402
from tornado_demix.heuristics import (  # noqa: E402
    BAND_ORDER,
    EARLY_EXIT_HOURS,
    apply_heuristics,
    count_is_evidence,
    early_profile_applies,
    ranked_candidates,
)

OUT_BASE = os.path.join(CACHE, "placebo")
OUT = os.path.join(OUT_BASE, "semisynthetic")
SETS = (("old152", OUT_BASE), ("post150", os.path.join(OUT_BASE, "post2022")))
MODES = ("target", "decoy")
WINDOW_HOURS = 30 * 24  # the cached runs' full search window (pipeline default)
MAX_NOTES = 50  # notes per pool in a planted session (a few whales go to hundreds)
DEPOSIT_GAP = 600  # seconds between planted deposits
JITTER_HOURS = 1.0  # mean exponential jitter between one exit's notes
RELAYER = "0x" + "cc" * 20
SIGNALS = (
    "count_match",
    "self_relayed",
    "gas_price",
    "linked",
    "linked_late",
    "linked_sender",
    "shared_deposit",
    "early_profile",
)
LEVELS = {"strong": 3, "moderate+": 2, "any": 1}


@dataclass(frozen=True)
class Behaviour:
    """How the planted depositor exits."""

    delay: str = "moderate"  # key of DELAYS
    split: str = "single"  # "single" or "spread"
    p_self: float = 0.5
    p_gas: float = 0.3
    p_link: float = 0.2
    p_sender: float = 0.05
    p_shared: float = 0.1
    p_reuse: float = 0.0


# Log-normal base delay per exit: (median hours, sigma).
DELAYS = {"fast": (2.0, 1.0), "moderate": (24.0, 1.5), "heavy": (72.0, 2.5)}

ERRORS = {
    "none": dict(p_self=0.0, p_gas=0.0, p_link=0.0, p_sender=0.0, p_shared=0.0),
    "rare": dict(p_self=0.05, p_gas=0.03, p_link=0.02, p_sender=0.005, p_shared=0.01),
    "model": dict(p_self=0.5, p_gas=0.3, p_link=0.2, p_sender=0.05, p_shared=0.1),
    "frequent": dict(p_self=0.8, p_gas=0.6, p_link=0.6, p_sender=0.3, p_shared=0.4),
}


def default_grid():
    """[(name, Behaviour)]: errors x delays x split, plus a busy-exit scenario."""
    grid = []
    for err, rates in ERRORS.items():
        for delay in DELAYS:
            for split in ("single", "spread"):
                grid.append((f"{err}/{delay}/{split}", Behaviour(delay, split, **rates)))
    grid.append(("model/moderate/single+reuse0.3", replace(grid_model(), p_reuse=0.3)))
    return grid


def grid_model():
    return Behaviour("moderate", "single", **ERRORS["model"])


def delay_beyond(delay, hours):
    """P(base delay > hours) under a delay law (for the docs and tests)."""
    median, sigma = DELAYS[delay]
    z = (math.log(hours) - math.log(median)) / sigma
    return 0.5 * math.erfc(z / math.sqrt(2))


# ---------------------------------------------------------------- speed

_HIST = {}


def fast_count_discrimination(res, hits):
    """heuristics.count_discrimination with the share count read from a cached
    histogram of ``res["counts"]`` (the rule is unchanged; the original scans
    every recipient for every recipient, quadratic in a 3,000-recipient window).
    Valid only while ``res["counts"]`` is not mutated, which holds inside
    apply_heuristics."""
    counts = res.get("counts", {})
    unique = res.get("unique_recipients") or len(counts)
    if unique <= 1:
        return 0.0
    hist = _HIST.get(id(counts))
    if hist is None or hist[0] is not counts:
        hist = (counts, Counter(counts.values()))
        _HIST[id(counts)] = hist
    sharing = hist[1][hits]
    if sharing >= unique:
        return 0.0
    if sharing <= 0:
        return 1.0
    return 1.0 - (float(sharing) - 1.0) / (float(unique) - 1.0)


@contextlib.contextmanager
def fast_discrimination():
    """Temporarily route the heuristics' count_discrimination through the cache."""
    original = heuristics.count_discrimination
    heuristics.count_discrimination = fast_count_discrimination
    try:
        yield
    finally:
        heuristics.count_discrimination = original
        _HIST.clear()


# ---------------------------------------------------------------- profiles


def session_profiles(deposits, gap_hours=24, cap=MAX_NOTES):
    """Counter of deposit-session profiles (note counts per pool, largest first).

    ``deposits``: universe rows [pool_key, sender, ts, hash]. A sender's deposits
    are split into sessions at gaps over ``gap_hours`` (across pools); inside a
    session notes are counted per pool with demix.cluster_vouchers' rule.
    """
    by_sender = defaultdict(list)
    for pool_key, sender, ts, _h in deposits:
        by_sender[sender].append((ts, pool_key))
    out = Counter()
    gap = gap_hours * 3600
    for rows in by_sender.values():
        rows.sort()
        session = [rows[0]]
        for row in rows[1:]:
            if row[0] - session[-1][0] > gap:
                out[_profile_of(session, gap_hours, cap)] += 1
                session = [row]
            else:
                session.append(row)
        out[_profile_of(session, gap_hours, cap)] += 1
    return out


def _profile_of(session, gap_hours, cap):
    deps = [
        {"pool_key": pk, "ts": ts, "denom": 0, "asset": "", "block": 0, "hash": ""}
        for ts, pk in session
    ]
    per_pool = Counter()
    for v in cluster_vouchers(deps, gap_hours=gap_hours):
        per_pool[v["pool_key"]] = max(per_pool[v["pool_key"]], v["count"])
    return tuple(sorted((min(n, cap) for n in per_pool.values()), reverse=True))


def load_profiles():
    """Session-profile distribution from universe.json, cached under OUT."""
    path = os.path.join(OUT, "profiles.json")
    if os.path.exists(path):
        rows = json.load(open(path, encoding="utf-8"))["profiles"]
        return [(tuple(p), n) for p, n in rows]
    uni = json.load(open(os.path.join(CACHE, "universe.json"), encoding="utf-8"))
    dist = session_profiles(uni["deposits"])
    os.makedirs(OUT, exist_ok=True)
    rows = sorted(dist.items(), key=lambda kv: -kv[1])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"source": "universe.json", "cap": MAX_NOTES, "profiles": rows}, fh)
    return rows


class ProfileSampler:
    def __init__(self, rows):
        self.profiles = [p for p, _n in rows]
        self.cum = []
        total = 0
        for _p, n in rows:
            total += n
            self.cum.append(total)
        self.total = total

    def draw(self, rng):
        return self.profiles[bisect.bisect_right(self.cum, rng.random() * self.total)]


def map_profile(sizes, pools, rng):
    """{pool_key: notes}: a session's pool sizes onto the run's pools."""
    pools = list(pools)
    rng.shuffle(pools)
    return {pk: n for pk, n in zip(pools, sizes)}


# ---------------------------------------------------------------- background


def slim_run(data):
    """What the injection needs from one cached run (keeps memory low)."""
    h = data.get("heuristics", {})
    gas_ok = {m[3].lower() for m in h.get("gas_price_matches", [])}
    denoms = {}
    for pk, res in data.get("denoms", {}).items():
        detail = {
            addr: [
                {
                    "value": r["value"],
                    "ts": r["ts"],
                    "hash": r.get("hash"),
                    "block": r.get("block"),
                    "self_relayed": bool(r.get("self_relayed")),
                    "gas_price": r.get("gas_price")
                    if (r.get("hash") or "").lower() in gas_ok
                    else None,
                }
                for r in recs
            ]
            for addr, recs in res["detail"].items()
        }
        starts = [w["first_ts"] for w in res.get("windows", [])] or [res["window_ts"][0]]
        denoms[pk] = {
            "pool_key": pk,
            "denom": res["denom"],
            "asset": res["asset"],
            "start": min(starts),
            "detail": detail,
        }
    shared = {}
    for _pk, addr, addrs in h.get("shared_deposits", []):
        shared.setdefault(addr, list(addrs))
    return {
        "wallet": (data.get("wallet") or "").lower(),
        "denoms": denoms,
        "counterparties": {a for _p, a in h.get("linked_addresses", [])},
        "senders": {s[3].lower(): s[2] for s in h.get("linked_senders", [])},
        "shared": shared,
        "deposit_gas": sorted(
            {d["gas_price"] for d in data.get("deposits", []) if d.get("gas_price")}
        ),
    }


def iter_backgrounds(sets=SETS, limit=None):
    """Yield (set name, wallet, {mode: slim run}) one depositor at a time."""
    from review4_common import _inject_shared

    for name, base in sets:
        files = sorted(
            set(os.listdir(os.path.join(base, "target")))
            & set(os.listdir(os.path.join(base, "decoy")))
        )
        for f in files[:limit]:
            runs = {}
            for mode in MODES:
                data = json.load(open(os.path.join(base, mode, f), encoding="utf-8"))
                if name == "old152":
                    _inject_shared(data, data["wallet"])
                runs[mode] = slim_run(data)
            yield name, f[:-5], runs


# ---------------------------------------------------------------- injection


def _addr(rng):
    return "0x%040x" % rng.getrandbits(160)


def _hash(rng):
    return "0x%064x" % rng.getrandbits(256)


def _delay_hours(rng, delay):
    median, sigma = DELAYS[delay]
    return median * math.exp(rng.gauss(0.0, sigma))


def plant(run, profile, beh, rng, window_hours=WINDOW_HOURS):
    """Inject one planted depositor into a slim background run.

    Returns (data scored by the current heuristics, positives {(pool_key, addr)},
    info). ``profile`` is {pool_key: notes}; pools not in it are not searched.
    """
    _HIST.clear()
    wallet = run["wallet"]
    gas = run["deposit_gas"] or [rng.randint(12, 150) * 10**9 + rng.randint(1, 999) * 10**6]
    deposits = []
    for pk, n in profile.items():
        res = run["denoms"][pk]
        t0 = res["start"] + 60
        for i in range(n):
            deposits.append(
                {
                    "pool_key": pk,
                    "denom": res["denom"],
                    "asset": res["asset"],
                    "ts": t0 + i * DEPOSIT_GAP,
                    "block": None,
                    "hash": _hash(rng),
                    "gas_price": rng.choice(gas),
                }
            )
    vouchers = cluster_vouchers(deposits)
    last = {v["pool_key"]: v["last_ts"] for v in vouchers}
    spans = {
        v["pool_key"]: (v["first_ts"], v["last_ts"] + int(window_hours * 3600)) for v in vouchers
    }

    # Background, narrowed to the planted voucher's search window.
    detail = {}
    for pk in profile:
        lo, hi = spans[pk]
        d = {}
        for addr, recs in run["denoms"][pk]["detail"].items():
            keep = [r for r in recs if lo <= r["ts"] <= hi]
            if keep:
                d[addr] = keep
        detail[pk] = d

    # Exits and the notes they receive.
    notes = [pk for pk in sorted(profile) for _ in range(profile[pk])]
    k = 1 if beh.split == "single" else rng.randint(1, len(notes))
    owner = list(range(k)) + [rng.randrange(k) for _ in range(len(notes) - k)]
    rng.shuffle(owner)
    exits = []
    for _ in range(k):
        addr = _addr(rng)
        if beh.p_reuse and rng.random() < beh.p_reuse:
            pool = [a for pk in profile for a in detail[pk] if a != wallet]
            if pool:
                addr = rng.choice(sorted(pool))
        exits.append(
            {
                "address": addr,
                "delay": _delay_hours(rng, beh.delay),
                "self": rng.random() < beh.p_self,
                "gas": rng.random() < beh.p_gas,
                "link": rng.random() < beh.p_link,
                "sender": rng.random() < beh.p_sender,
                "shared": rng.random() < beh.p_shared,
                "observed": [],
            }
        )
    positives = set()
    planted = 0
    for pk, i in zip(notes, owner):
        ex = exits[i]
        res = run["denoms"][pk]
        positives.add((pk, ex["address"]))
        ts = int(last[pk] + (ex["delay"] + rng.expovariate(1.0 / JITTER_HOURS)) * 3600)
        lo, hi = spans[pk]
        if not lo <= ts <= hi:
            continue
        fee = 0.0 if ex["self"] else res["denom"] * 0.003
        rec = {
            "value": res["denom"] - fee,
            "ts": ts,
            "hash": _hash(rng),
            "block": None,
            "self_relayed": ex["self"],
            "gas_price": None,
        }
        if ex["self"] and ex["gas"] and not any(r["gas_price"] for _p, r in ex["observed"]):
            rec["gas_price"] = rng.choice([d["gas_price"] for d in deposits])
        detail[pk][ex["address"]] = detail[pk].get(ex["address"], []) + [rec]
        ex["observed"].append((pk, rec))
        planted += 1

    counterparties = set(run["counterparties"])
    senders = dict(run["senders"])
    shared = {a: list(v) for a, v in run["shared"].items()}
    for ex in exits:
        if ex["link"]:
            counterparties.add(ex["address"])
        if ex["sender"] and ex["observed"]:
            senders[ex["observed"][0][1]["hash"].lower()] = wallet
        if ex["shared"]:
            shared.setdefault(ex["address"], []).append(_addr(rng))

    data = {"wallet": wallet, "deposits": deposits, "vouchers": vouchers, "denoms": {}}
    for pk in sorted(profile):
        res = run["denoms"][pk]
        d = detail[pk]
        out = {
            "pool_key": pk,
            "denom": res["denom"],
            "asset": res["asset"],
            "detail": d,
            "counts": {a: len(r) for a, r in d.items()},
            "unique_recipients": len(d),
        }
        sizes = [v["count"] for v in vouchers if v["pool_key"] == pk]
        out["target_counts"] = sorted(set(sizes) | ({sum(sizes)} if len(sizes) > 1 else set()))
        out["candidates_by_count"] = {
            n: [a for a, c in out["counts"].items() if c == n] if count_is_evidence(out, n) else []
            for n in out["target_counts"]
        }
        data["denoms"][pk] = out
    apply_heuristics(
        data,
        counterparties,
        gas_gate=None,
        is_contract=None,
        withdrawal_senders=senders,
        shared_deposits=shared,
    )
    info = {
        "notes": len(notes),
        "pools": len(profile),
        "exits": k,
        "planted_observed": planted,
        "early_link": sum(
            1
            for ex in exits
            if ex["link"]
            and ex["observed"]
            and min(r["ts"] for _p, r in ex["observed"]) - last[ex["observed"][0][0]]
            <= EARLY_EXIT_HOURS * 3600
        ),
    }
    return data, positives, info


def stratum(profile):
    n = sum(profile.values())
    if n == 1:
        return "1 note"
    if early_profile_applies(profile):
        return ">=10 notes, >=2 pools"
    return "2+ notes"


# ---------------------------------------------------------------- metrics


def new_counter():
    return {
        "reps": 0,
        "pos": 0,
        "neg": 0,
        "pos_observed": 0,
        "tp": Counter(),  # level -> planted pairs listed at or above it
        "fp": Counter(),
        "tp_band": Counter(),  # exact band
        "fp_band": Counter(),
        "sig_tp": Counter(),
        "sig_fp": Counter(),
        "top1": 0,
        "top5": 0,
        "found": 0,
        "rep_false": Counter(),  # level -> replicates with >= 1 false pair at or above it
        "scored": Counter(),  # (sort score, positive) -> listed pairs
        "unlisted_pos": 0,
    }


def evaluate(data, positives, counter, *more):
    """Add one scored replicate to ``counter`` (and to every counter in ``more``)."""
    delta = new_counter()
    rows = ranked_candidates(data)
    listed = {
        (r["pool_key"], r["address"]): (r["band"], BAND_ORDER[r["band"]] * 10 + r["confidence"])
        for r in rows
    }
    delta["reps"] += 1
    false_level = Counter()
    pairs = {(pk, a) for pk, res in data["denoms"].items() for a in res["detail"]}
    for pk, a in pairs | positives:
        pos = (pk, a) in positives
        delta["pos" if pos else "neg"] += 1
        res = data["denoms"].get(pk, {})
        if pos and a in res.get("detail", {}):
            delta["pos_observed"] += 1
        for s in res.get("signals", {}).get(a, []):
            delta["sig_tp" if pos else "sig_fp"][s] += 1
        hit = listed.get((pk, a))
        if not hit:
            delta["unlisted_pos"] += pos
            continue
        band, score = hit
        delta["scored"][(score, pos)] += 1
        delta["tp_band" if pos else "fp_band"][band] += 1
        for level, need in LEVELS.items():
            if BAND_ORDER[band] >= need:
                delta["tp" if pos else "fp"][level] += 1
                if not pos:
                    false_level[level] = 1
    for level in false_level:
        delta["rep_false"][level] += 1
    top = [(r["pool_key"], r["address"]) for r in rows]
    delta["top1"] += bool(top) and top[0] in positives
    delta["top5"] += any(t in positives for t in top[:5])
    delta["found"] += any(t in positives for t in top)
    for c in (counter, *more):
        add_into(c, delta)
    return counter


def add_into(dst, src):
    """Add counter ``src`` into ``dst`` in place."""
    for k, v in src.items():
        if isinstance(v, Counter):
            dst[k].update(v)
        elif isinstance(v, list):
            dst[k].extend(v)
        else:
            dst[k] += v
    return dst


def merge(counters):
    out = new_counter()
    for c in counters:
        add_into(out, c)
    return out


def average_precision_hist(scored, unlisted_pos=0):
    """simulate.average_precision over a histogram {(score, positive): pairs}.

    Unlisted positives rank last (they add to the total, never to the curve);
    tied scores form one step, as in simulate.average_precision.
    """
    by_score = defaultdict(lambda: [0, 0])  # score -> [positives, pairs]
    for (score, pos), n in scored.items():
        by_score[score][0] += n if pos else 0
        by_score[score][1] += n
    total = sum(p for p, _n in by_score.values()) + unlisted_pos
    if not total:
        return 0.0
    ap, tp, seen = 0.0, 0, 0
    for score in sorted(by_score, reverse=True):
        gained, n = by_score[score]
        tp += gained
        seen += n
        ap += (gained / total) * (tp / seen)
    return ap


def _div(a, b):
    return a / b if b else None


def metrics(c):
    """Rates from a (pooled) counter. None where undefined."""
    m = {
        "replicates": c["reps"],
        "positives": c["pos"],
        "negatives": c["neg"],
        "observed_share": _div(c["pos_observed"], c["pos"]),
        "top1": _div(c["top1"], c["reps"]),
        "top5": _div(c["top5"], c["reps"]),
        "found": _div(c["found"], c["reps"]),
        "pr_auc": average_precision_hist(c["scored"], c["unlisted_pos"]) if c["pos"] else None,
    }
    for level in LEVELS:
        tp, fp = c["tp"][level], c["fp"][level]
        m[f"precision_{level}"] = _div(tp, tp + fp)
        m[f"recall_{level}"] = _div(tp, c["pos"])
        m[f"false_per_1000_{level}"] = _div(1000.0 * fp, c["neg"])
        m[f"rep_false_{level}"] = _div(c["rep_false"][level], c["reps"])
    for band in BAND_ORDER:
        tp, fp = c["tp_band"][band], c["fp_band"][band]
        m[f"band_precision_{band}"] = _div(tp, tp + fp)
        m[f"band_recall_{band}"] = _div(tp, c["pos"])
    m["signals"] = {
        s: {
            "tp": c["sig_tp"][s],
            "fp": c["sig_fp"][s],
            "precision": _div(c["sig_tp"][s], c["sig_tp"][s] + c["sig_fp"][s]),
            "recall": _div(c["sig_tp"][s], c["pos"]),
        }
        for s in SIGNALS
    }
    return m


CI_KEYS = (
    "precision_strong",
    "recall_strong",
    "precision_moderate+",
    "recall_moderate+",
    "precision_any",
    "recall_any",
    "top1",
    "top5",
    "pr_auc",
    "false_per_1000_strong",
    "false_per_1000_moderate+",
    "rep_false_moderate+",
)


def bootstrap(per_run, rng, n=300, keys=CI_KEYS):
    """Percentile 95 % CIs, resampling background runs (clusters of replicates)."""
    runs = list(per_run)
    if not runs:
        return {}
    draws = defaultdict(list)
    for _ in range(n):
        m = metrics(merge(per_run[runs[rng.randrange(len(runs))]] for _ in runs))
        for k in keys:
            if m[k] is not None:
                draws[k].append(m[k])
    out = {}
    for k in keys:
        xs = sorted(draws[k])
        out[k] = [xs[int(0.025 * len(xs))], xs[max(int(0.975 * len(xs)) - 1, 0)]] if xs else None
    return out


# ---------------------------------------------------------------- driver


def run_grid(backgrounds, grid, sampler, replicates, seed, window_hours=WINDOW_HOURS, log=None):
    """{mode: {scenario: {run_id: counter}}} and per-stratum counters."""
    per = {mode: {name: {} for name, _b in grid} for mode in MODES}
    strata = {mode: {name: defaultdict(new_counter) for name, _b in grid} for mode in MODES}
    for i, (set_name, wallet, runs) in enumerate(backgrounds):
        run_id = f"{set_name}:{wallet}"
        for s_idx, (name, beh) in enumerate(grid):
            # Same planted draws in target and decoy (same seed per run/scenario/rep).
            for mode in MODES:
                bg = runs[mode]
                if not bg["denoms"]:
                    continue
                c = per[mode][name].setdefault(run_id, new_counter())
                for rep in range(replicates):
                    rng = random.Random(f"{seed}:{run_id}:{s_idx}:{rep}")
                    profile = map_profile(sampler.draw(rng), sorted(bg["denoms"]), rng)
                    with fast_discrimination():
                        data, positives, _info = plant(bg, profile, beh, rng, window_hours)
                    evaluate(data, positives, c, strata[mode][name][stratum(profile)])
        if log:
            log(i + 1)
    return per, strata


def summarize(per, strata, grid, seed, boot):
    rng = random.Random(seed)
    out = {}
    for mode in MODES:
        out[mode] = {}
        for name, beh in grid:
            runs = per[mode][name]
            pooled = metrics(merge(runs.values()))
            out[mode][name] = {
                "behaviour": asdict(beh),
                "background_runs": len(runs),
                **pooled,
                "ci95": bootstrap(runs, rng, boot),
                "strata": {k: metrics(v) for k, v in sorted(strata[mode][name].items())},
            }
    return out


def _f(x, digits=2):
    return "-" if x is None else f"{x:.{digits}f}"


def _ci(m, key, digits=2):
    ci = m["ci95"].get(key)
    v = _f(m[key], digits)
    return v if not ci else f"{v} [{_f(ci[0], digits)}, {_f(ci[1], digits)}]"


def markdown(summary, meta):
    lines = [
        "# Semi-synthetic benchmark (real background, planted exits)",
        "",
        f"Generated by `tools/semisynthetic.py` ({meta['generated']}), seed {meta['seed']}, "
        f"{meta['replicates']} planted depositor(s) per background run and scenario, "
        f"{meta['runs']} background depositors (old152 + post150), search window "
        f"{meta['window_hours']:g} h, bootstrap {meta['boot']} over background runs. "
        "Current heuristics (v" + meta["version"] + ").",
        "",
        "Positives: (pool, address) pairs of planted exits, including exits whose notes "
        "came after the window (misses). Negatives: every other recipient in the "
        "planted voucher's window. Precision / recall at `strong`, `>= moderate` and "
        "`any` candidate; false leads per 1,000 background pairs; `rep>=mod` = share "
        "of replicates with at least one false >= moderate lead. Delay laws: fast "
        "(median 2 h), moderate (24 h), heavy (72 h, sigma 2.5). Errors: none / rare / "
        "model / frequent (see the module docstring).",
        "",
        "`target` uses the real wallet's own windows (a background recipient with an "
        "inherited link may be the real wallet's genuine exit, so precision is a lower "
        "bound); `decoy` uses its shifted windows, where every inherited link is chance.",
    ]
    for mode in MODES:
        lines += [
            "",
            f"## {mode} windows",
            "",
            "| scenario | P strong | R strong | P >=mod | R >=mod | P any | R any | top-1 | "
            "top-5 | PR-AUC | false strong /1k | false >=mod /1k | rep>=mod |",
            "|" + "---|" * 13,
        ]
        for name, m in summary[mode].items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        name,
                        _ci(m, "precision_strong"),
                        _ci(m, "recall_strong"),
                        _ci(m, "precision_moderate+"),
                        _ci(m, "recall_moderate+"),
                        _f(m["precision_any"]),
                        _f(m["recall_any"]),
                        _ci(m, "top1"),
                        _f(m["top5"]),
                        _ci(m, "pr_auc"),
                        _f(m["false_per_1000_strong"], 3),
                        _ci(m, "false_per_1000_moderate+", 2),
                        _f(m["rep_false_moderate+"]),
                    ]
                )
                + " |"
            )
    lines += [
        "",
        "## Per signal (decoy windows, model/moderate/single and frequent/heavy/spread)",
        "",
        "| scenario | signal | TP | FP | precision | recall |",
        "|---|---|---|---|---|---|",
    ]
    for name in ("model/moderate/single", "frequent/heavy/spread"):
        m = summary["decoy"].get(name)
        if not m:
            continue
        for s, v in m["signals"].items():
            lines.append(
                f"| {name} | {s} | {v['tp']} | {v['fp']} | {_f(v['precision'], 3)} | "
                f"{_f(v['recall'], 3)} |"
            )
    lines += [
        "",
        "## By planted profile (decoy windows, model/moderate/single)",
        "",
        "| profile | replicates | R >=mod | R any | P any | top-1 | PR-AUC |",
        "|---|---|---|---|---|---|---|",
    ]
    m = summary["decoy"].get("model/moderate/single")
    for k, v in (m or {}).get("strata", {}).items():
        lines.append(
            f"| {k} | {v['replicates']} | {_f(v['recall_moderate+'])} | {_f(v['recall_any'])} | "
            f"{_f(v['precision_any'])} | {_f(v['top1'])} | {_f(v['pr_auc'])} |"
        )
    lines += ["", "## Limits", ""] + [f"- {x}" for x in meta["limits"]]
    return "\n".join(lines) + "\n"


LIMITS = [
    "The background (recipients, counts, busy addresses, self-relay and gas habits, "
    "chance links of a real counterparty graph) is real; the positives are planted by "
    "our own model of operational errors (independent Bernoulli errors per exit, "
    "log-normal delays, fresh exit addresses unless `reuse`).",
    "Recall therefore shows how detection moves when the error rates differ from the "
    "assumed ones; it is not the recall on real cases, whose error rates are unknown.",
    "The planted depositor inherits one real wallet's counterparties, withdrawal "
    "senders and exchange deposit addresses; in target windows some 'false' leads "
    "are the real wallet's genuine exits (conservative precision).",
    "Single-wallet runs: the multi-wallet profile_match signal is not exercised; "
    "pre-London gas gating is taken from the full run (gas prices outside its "
    "matches are dropped).",
    "Planted notes are spent after the voucher's last deposit only; the background "
    "is narrowed to the planted voucher's window, which starts at the real wallet's "
    "first window in that pool.",
]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--replicates", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--boot", type=int, default=300)
    ap.add_argument("--hours", type=float, default=WINDOW_HOURS)
    ap.add_argument("--limit", type=int, default=None, help="background runs per set")
    ap.add_argument("--scenarios", nargs="*", default=None, help="subset of scenario names")
    args = ap.parse_args(argv)
    from tornado_demix import __version__

    grid = default_grid()
    if args.scenarios:
        grid = [g for g in grid if g[0] in set(args.scenarios)]
    sampler = ProfileSampler(load_profiles())
    t0 = time.time()

    def log(i):
        if i % 10 == 0:
            print(f"  {i} background depositors, {time.time() - t0:.0f} s", flush=True)

    per, strata = run_grid(
        iter_backgrounds(limit=args.limit),
        grid,
        sampler,
        args.replicates,
        args.seed,
        args.hours,
        log,
    )
    runs = len(next(iter(per["target"].values())))
    summary = summarize(per, strata, grid, args.seed, args.boot)
    meta = {
        "generated": time.strftime("%Y-%m-%d"),
        "version": __version__,
        "seed": args.seed,
        "replicates": args.replicates,
        "runs": runs,
        "window_hours": args.hours,
        "boot": args.boot,
        "seconds": round(time.time() - t0),
        "profiles_top": [
            [list(p), n] for p, n in zip(sampler.profiles[:10], _counts(sampler)[:10])
        ],
        "delay_beyond_72h": {d: round(delay_beyond(d, 72), 3) for d in DELAYS},
        "delay_beyond_window": {d: round(delay_beyond(d, args.hours), 3) for d in DELAYS},
        "limits": LIMITS,
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "results": summary}, fh, indent=1)
    md = markdown(summary, meta)
    with open(os.path.join(OUT, "SEMISYNTHETIC.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)


def _counts(sampler):
    prev = 0
    out = []
    for c in sampler.cum:
        out.append(c - prev)
        prev = c
    return out


if __name__ == "__main__":
    main()
