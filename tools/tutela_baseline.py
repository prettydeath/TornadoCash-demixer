#!/usr/bin/env python3
"""Tutela / Beres et al. Tornado Cash heuristics under the placebo (target-decoy) design.

The question is which published baseline heuristics produce more leads in the
real (target) withdrawal windows than in decoy windows that cannot contain the
depositor's own withdrawals (see tools/placebo_eval.py). Each heuristic is
re-implemented from the primary sources below and applied, per depositor and
per window, to that window's withdrawal recipients. A lead is a distinct
(pool, recipient) pair, as in tools/placebo_windows.py.

Sources (read for this implementation, 2026-10-05)
--------------------------------------------------
[B] Beres F., Seres I. A., Benczur A. A., Quintyne-Collins M. "Blockchain is
    Watching You: Profiling and Deanonymizing Ethereum Users", IEEE DAPPS 2021,
    arXiv:2005.14051v2, Section 7.1 "Heuristics for linking mixer deposits and
    withdraws":
      Heuristic 1 - an address that made a deposit and also a withdraw;
      Heuristic 2 - a deposit-withdraw pair with "unique and manually set" gas
                    prices (manually set = the last 9 digits, i.e. the sub-Gwei
                    part, are non-zero);
      Heuristic 3 - a transaction between deposit address d and withdraw
                    address w (or vice versa).
[T] Wu M., McTighe W., Wang K., Seres I. A., Bax N., Puebla M., Mendez M.,
    Carrone F., De Mattey T., Demaestri H. O., Nicolini M., Fontana P. "Tutela:
    An Open-Source Tool for Assessing User-Privacy on Ethereum and Tornado
    Cash", arXiv:2201.06811, Section 6 "Tornado Cash Heuristics":
      6.1 Address Match, 6.2 Unique Gas Price, 6.3 Linked ETH Addresses,
      6.4 Multiple Denomination, 6.5 TORN Mining.
[C] The Tutela code, github.com/TutelaLabs/tutela-app at commit 5a33bfbf,
    scripts/tornadocash/: run_exact_match_heuristic.py, run_gas_price_heuristic.py,
    run_linked_tx_heuristic.py, run_same_num_txs_heuristic.py,
    run_torn_mine_heuristic.py; live/tornadocash/data.py (BigQuery selection).

Implemented rules
-----------------
address_match   [T 6.1], [C exact_match_heuristic]: the recipient equals the
                depositor and one of the depositor's deposits precedes the
                withdrawal (any pool, the code's default, not --by-pool).
beres_h1        [B H1]: the recipient equals the depositor (no time condition).
gas_tutela      [T 6.2], [C same_gas_price_heuristic]: the withdrawal was sent by
                its own recipient (tx sender == recipient: the code's relayer
                filter), its gas price equals the gas price of an earlier deposit
                of the depositor, and that price is unique among deposits
                (value_counts == 1).
gas_beres       [B H2]: the gas price is shared by the withdrawal and one of the
                depositor's deposits, is unique among deposits, and is manually
                set (gas_price % 1e9 != 0). [B] names no relayer filter, so none.
beres_h3        [B H3]: the depositor and the recipient transacted directly. Uses
                the counterparty set the cached run already holds (normal,
                internal and ERC-20 transfers, either direction, whole history;
                tornado_demix.demix.wallet_counterparties), contracts included.
linked_tutela   [T 6.3], [C first_neighbors_heuristic]: at least three normal
                transactions in one direction between depositor and recipient
                (the code groups by (from, to) and keeps size >= 3), and the
                depositor deposited in the same pool before the withdrawal.
                Needs the depositor's normal-transaction list: --with-linked.
multi_denom     [T 6.4], [C same_num_of_transactions_heuristic]: the recipient's
                withdrawal portfolio (count per pool of its withdrawals in the 24 h
                ending at an examined withdrawal, that withdrawal included) equals
                exactly the portfolio of the depositor's deposits in the 24 h
                ending at one of its deposits; the portfolio must span >= 2 pools
                and >= 3 transactions.
any_tutela      union of the Tutela rules computed in the run (address_match,
                gas_tutela, multi_denom, plus linked_tutela with --with-linked).
any_beres       union of beres_h1, gas_beres, beres_h3.

Deviations and assumptions (all apply to target and decoy alike)
----------------------------------------------------------------
* Universe. .cache/labels/universe.json holds the four ETH pools only, so
  multi_denom portfolios are ETH-pool portfolios on both sides and cannot fire in
  a token pool window (token pools are 0.1 % of the examined withdrawals).
* Multi-denomination thresholds follow the paper, not the code: the code keeps
  any portfolio over >= 2 pools (so >= 2 transactions, not 3) and builds the
  deposit portfolio from the deposits *before* the current one (excluding it),
  while the withdrawal portfolio includes the current withdrawal; that
  asymmetry looks unintended and is not reproduced.
* Gas uniqueness population. [C] counts the price over its whole deposit table.
  Here a deposit is any successful call of deposit(bytes32) 0xb214faa5 on an ETH
  pool, deposit(address,bytes32,bytes) 0x13d98d13 or deposit(address,bytes32)
  0xb9e1aa03 on a Tornado router/proxy, or a universe deposit hash, found by
  crawling those seven contracts' transaction lists around the deposit block in
  widening tiles (+-1 day, then +-30 days; whole history only with --gas-full).
  A price seen on two deposits is non-unique for certain; a price seen once in
  the scanned range is "unique within the scanned range" (summary field
  gas_uniqueness). Direct calls to token pools are not scanned. Gas prices are
  the explorer's gasPrice (the effective price after EIP-1559), so Beres's
  "manually set" test is only meaningful for pre-London transactions.
* beres_h3 uses demix's counterparty set (normal + internal + token) where [B]
  says "a transaction"; linked_tutela uses normal transactions only, like [C]
  (BigQuery crypto_ethereum.transactions).
* Deposit-before-withdrawal checks use the run's deposit timestamps, which the
  decoy run shifts back by construction; the gas-uniqueness lookup uses the real
  deposit block.
* TORN mining [T 6.5] is omitted. It matches the block distance implied by the
  anonymity points a Miner (0x746aebc0...) withdraw converts (rate per pool,
  [C MINE_POOL_RATES]) to a deposit-withdraw block gap. The decoy run shifts
  deposit timestamps, not blocks, so a block-exact rule has no well-defined decoy
  counterpart; it also needs every recipient's Miner withdraw calls decoded from
  zk-proof calldata, and applies only to the 15 mining pools during anonymity
  mining (2021). [T 7.2] reports 358 of 97.3k deposits (0.4 %) for it.

Ratio
-----
R = (L_decoy / N_decoy) / (L_target / N_target), leads per examined withdrawal,
the convention of tools/review4_common.boot_ratio (R ~ 1: chance level; R << 1:
the heuristic fires above chance in real windows). 95 % CI: percentile bootstrap
over depositors (2000 resamples, fixed seed), and the exact conditional
(Clopper-Pearson) interval, which is the one to read when a side has < 5 leads.
Depositors enter when both windows have withdrawals.

Network use (never with --offline-only; keys from tools/ens_labels.api_keys at
run time only): gas-uniqueness tiles, withdrawal senders by hash and, with
--with-linked, the normal-transaction list of each depositor that has a direct
counterparty among its window recipients (the only depositors linked_tutela can
fire for, since >= 3 normal transactions imply a direct counterparty). All of it
is cached under .cache/labels/placebo/tutela/ and resumable.

Usage
-----
    python tools/tutela_baseline.py --offline-only
    python tools/tutela_baseline.py --dir .cache/labels/placebo .cache/labels/placebo/post2022
    python tools/tutela_baseline.py --dir ... --with-linked
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from review4_common import OUT, boot_ratio  # noqa: E402

ZERO = "0x" + "0" * 40
DAY = 86400
GWEI = 10**9
SEED = 5
BOOT = 2000
TUT = os.path.join(OUT, "tutela")
UNIVERSE = os.path.join(os.path.dirname(OUT), "universe.json")

DEPOSIT_SELECTORS = ("0xb214faa5", "0x13d98d13", "0xb9e1aa03")
ETH_POOLS = {
    "0.1 ETH": "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc",
    "1 ETH": "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",
    "10 ETH": "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",
    "100 ETH": "0xa160cdab225685da1d56aa342ad8841c3b53f291",
}
ROUTERS = (
    "0x905b63fff465b9ffbf41dea908ceb12478ec7601",
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
)
SCAN_CONTRACTS = ROUTERS + tuple(ETH_POOLS.values())
TILE = 50_000  # blocks per cached crawl tile (~7 days)
LEVELS = (7_200, 216_000)  # +-1 day, +-30 days of blocks
CRAWL_PAGE = 1_000  # explorers cap txlist pages at 1,000 rows (etherscan.PAGE_SIZE)
FULL = 10**9  # radius meaning "whole history"

HEURISTICS = (
    "address_match",
    "beres_h1",
    "gas_tutela",
    "gas_beres",
    "beres_h3",
    "linked_tutela",
    "multi_denom",
)
TUTELA = ("address_match", "gas_tutela", "linked_tutela", "multi_denom")
BERES = ("beres_h1", "gas_beres", "beres_h3")


# --------------------------------------------------------------------------- helpers


def is_eth_pool(pool_key):
    return pool_key in ETH_POOLS


def examined(data):
    """[(pool_key, recipient, records)] for every recipient of every window pool."""
    return [
        (pk, addr, recs)
        for pk, res in data.get("denoms", {}).items()
        for addr, recs in res.get("detail", {}).items()
    ]


def exposure(data):
    return sum(len(recs) for _pk, _a, recs in examined(data))


def portfolio(events, t, window=DAY):
    """Count per pool of ``events`` [(pool, ts)] with t - window <= ts <= t."""
    return Counter(p for p, ts in events if t - window <= ts <= t)


def _key(c):
    return frozenset(c.items())


# --------------------------------------------------------------------------- rules


def address_match_leads(data):
    """[T 6.1]/[C]: recipient == depositor, with an earlier deposit (any pool)."""
    wallet = data["wallet"].lower()
    dep_ts = [d["ts"] for d in data.get("deposits", [])]
    out = set()
    for pk, addr, recs in examined(data):
        if addr == wallet and any(any(t < r["ts"] for t in dep_ts) for r in recs):
            out.add((pk, addr))
    return out


def beres_h1_leads(data):
    """[B H1]: an address that deposited and also received a withdrawal."""
    wallet = data["wallet"].lower()
    return {(pk, a) for pk, a, _r in examined(data) if a == wallet}


def gas_candidates(data):
    """[(pool_key, recipient, record, gas_price)] whose price equals an earlier deposit's."""
    deps = [d for d in data.get("deposits", []) if d.get("gas_price")]
    out = []
    for pk, addr, recs in examined(data):
        for r in recs:
            g = r.get("gas_price")
            if g and any(d["gas_price"] == g and d["ts"] < r["ts"] for d in deps):
                out.append((pk, addr, r, g))
    return out


def gas_leads(data, unique, senders, variant):
    """Unique-gas-price leads.

    ``unique(price) -> bool`` says whether the price is unique among deposits;
    ``senders`` maps a withdrawal tx hash to its sender (tutela variant only).
    """
    out = set()
    for pk, addr, r, g in gas_candidates(data):
        if not unique(g):
            continue
        if variant == "tutela":
            if (senders.get((r.get("hash") or "").lower()) or "") != addr:
                continue
        elif variant == "beres":
            if g % GWEI == 0:
                continue
        else:
            raise ValueError(variant)
        out.add((pk, addr))
    return out


def beres_h3_leads(data):
    """[B H3]: a direct transaction between depositor and recipient (contracts too)."""
    wallet = data["wallet"].lower()
    h = data.get("heuristics", {})
    linked = {(pk, a) for pk, a in h.get("linked_addresses", []) if a != wallet}
    contracts = set(h.get("linked_contracts", []))
    linked |= {(pk, a) for pk, a, _r in examined(data) if a in contracts and a != wallet}
    return linked


def linked_candidates(data):
    """Recipients that are direct counterparties of the depositor (superset for linked_tutela)."""
    return {a for _pk, a in beres_h3_leads(data)}


def linked_tutela_leads(data, pair_counts, min_tx=3):
    """[T 6.3]/[C]: >= min_tx normal txs in one direction, same-pool earlier deposit.

    ``pair_counts`` maps a recipient to [txs depositor->recipient, txs recipient->depositor].
    """
    wallet = data["wallet"].lower()
    deps = defaultdict(list)
    for d in data.get("deposits", []):
        deps[d["pool_key"]].append(d["ts"])
    out = set()
    for pk, addr, recs in examined(data):
        if addr == wallet:
            continue
        n = pair_counts.get(addr) or [0, 0]
        if max(n) < min_tx:
            continue
        if any(any(t < r["ts"] for t in deps.get(pk, [])) for r in recs):
            out.add((pk, addr))
    return out


def deposit_portfolios(data, window=DAY):
    """Set of ETH-pool deposit portfolios, one per deposit (24 h ending at it)."""
    ev = [(d["pool_key"], d["ts"]) for d in data.get("deposits", []) if is_eth_pool(d["pool_key"])]
    return {_key(portfolio(ev, t, window)) for _p, t in ev}


def multi_denom_leads(data, withdrawals_of, min_pools=2, min_txs=3, window=DAY):
    """[T 6.4]: exact deposit/withdrawal portfolio match over >= min_pools, >= min_txs.

    ``withdrawals_of(addr)`` returns that address's universe withdrawals [(pool, ts, hash)].
    """
    dset = deposit_portfolios(data, window)
    dset = {p for p in dset if len(p) >= min_pools and sum(n for _k, n in p) >= min_txs}
    if not dset:
        return set()
    out = set()
    for pk, addr, recs in examined(data):
        if not is_eth_pool(pk):
            continue
        wds = withdrawals_of(addr)
        known = {h for _p, _t, h in wds}
        ev = [(p, t) for p, t, _h in wds]
        for r in recs:
            extra = [] if (r.get("hash") or "").lower() in known else [(pk, r["ts"])]
            if _key(portfolio(ev + extra, r["ts"], window)) in dset:
                out.add((pk, addr))
                break
    return out


def run_leads(data, ctx):
    """{heuristic: set of (pool_key, recipient)} for one run."""
    leads = {
        "address_match": address_match_leads(data),
        "beres_h1": beres_h1_leads(data),
        "gas_tutela": gas_leads(data, ctx["unique"], ctx["senders"], "tutela"),
        "gas_beres": gas_leads(data, ctx["unique"], ctx["senders"], "beres"),
        "beres_h3": beres_h3_leads(data),
        "multi_denom": multi_denom_leads(data, ctx["withdrawals_of"]),
    }
    pc = ctx.get("pair_counts")
    if pc is not None:
        leads["linked_tutela"] = linked_tutela_leads(data, pc.get(data["wallet"].lower(), {}))
    leads["any_tutela"] = set().union(*(leads[k] for k in TUTELA if k in leads))
    leads["any_beres"] = set().union(*(leads[k] for k in BERES))
    return leads


# --------------------------------------------------------------------------- statistics


def cp_ratio_ci(t, te, d, de):
    """Exact (Clopper-Pearson) interval for R, the counts treated as Poisson:
    conditional on t + d, d ~ Binomial(t + d, p) with p / (1 - p) = R * De / Te.
    Same formula as tools/review4_analyze.cp_ratio_ci."""
    from scipy.stats import beta

    n = t + d
    if n == 0:
        return None
    lo = 0.0 if d == 0 else beta.ppf(0.025, d, n - d + 1)
    hi = 1.0 if d == n else beta.ppf(0.975, d + 1, n - d)

    def f(p):
        return None if p >= 1 else round((p / (1 - p)) * (te / de), 3)

    return [f(lo), f(hi)]


def ratio_row(per, seed=SEED, boot=BOOT):
    """per: [(target leads, target withdrawals, decoy leads, decoy withdrawals)] per depositor."""
    per = [p for p in per if p[1] and p[3]]
    t = sum(p[0] for p in per)
    d = sum(p[2] for p in per)
    te = sum(p[1] for p in per)
    de = sum(p[3] for p in per)
    out = {
        "depositors": len(per),
        "target_leads": t,
        "decoy_leads": d,
        "target_withdrawals": te,
        "decoy_withdrawals": de,
        "R": None,
        "R_boot95": None,
        "R_exact95": cp_ratio_ci(t, te, d, de) if per else None,
    }
    if t and per:
        v, ci = boot_ratio(per, random.Random(seed), n=boot)
        out["R"] = None if v is None else round(v, 3)
        out["R_boot95"] = ci
    out["few"] = min(t, d) < 5
    return out


# --------------------------------------------------------------------------- data


def load_dir(base):
    """[(wallet, target_run, decoy_run)] for a placebo sample directory."""
    names = sorted(
        set(os.listdir(os.path.join(base, "target"))) & set(os.listdir(os.path.join(base, "decoy")))
    )
    rows = []
    for n in names:
        t, d = (
            json.load(open(os.path.join(base, k, n), encoding="utf-8")) for k in ("target", "decoy")
        )
        rows.append((t["wallet"].lower(), t, d))
    return rows


def load_universe(path=UNIVERSE):
    with open(path, encoding="utf-8") as fh:
        uni = json.load(fh)
    wds = defaultdict(list)
    for pool, addr, ts, h in uni["withdrawals"]:
        wds[addr.lower()].append((pool, ts, h.lower()))
    dep_hashes = {h.lower() for _p, _a, _t, h in uni["deposits"]}
    return wds, dep_hashes, tuple(uni["blocks"])


def _jload(name, default):
    path = os.path.join(TUT, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _jsave(name, obj):
    os.makedirs(os.path.dirname(os.path.join(TUT, name)), exist_ok=True)
    tmp = os.path.join(TUT, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, os.path.join(TUT, name))


# --------------------------------------------------------------------------- network


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Counts explorer calls made through a client and stops at a cap."""

    def __init__(self, client, cap):
        self.calls = 0
        self.cap = cap
        orig = client.call

        def counted(params):
            if self.calls >= self.cap:
                raise BudgetExceeded(f"explorer call budget of {self.cap} reached")
            self.calls += 1
            if self.calls % 25 == 0:
                print(f"[calls] {self.calls}", file=sys.stderr, flush=True)
            return orig(params)

        client.call = counted


def _log_calls(kind, n):
    """Cumulative explorer-call ledger across runs (calls.json)."""
    led = _jload("calls.json", {})
    led[kind] = led.get(kind, 0) + n
    _jsave("calls.json", led)


def tiles_for(block, radius):
    return list(range((block - radius) // TILE, (block + radius) // TILE + 1))


def tile_rows(client, contract, tile, dep_hashes):
    """Cached, compact rows [hash, from, gas_price, is_deposit] of one contract tile."""
    name = os.path.join("tiles", f"{contract}_{tile}.json")
    cached = _jload(name, None)
    if cached is not None:
        return cached
    lo, hi = tile * TILE, (tile + 1) * TILE - 1
    seen, rows = set(), []
    while True:
        chunk = client.call(
            {
                "module": "account",
                "action": "txlist",
                "address": contract,
                "startblock": lo,
                "endblock": hi,
                "page": 1,
                "offset": CRAWL_PAGE,
                "sort": "asc",
            }
        )
        chunk = chunk or []
        top = max((int(r["blockNumber"]) for r in chunk), default=lo)
        full = len(chunk) >= CRAWL_PAGE
        for r in chunk:
            h = (r.get("hash") or "").lower()
            if not h or h in seen or (full and int(r["blockNumber"]) >= top):
                continue
            seen.add(h)
            ok = r.get("isError", "0") == "0"
            sel = (r.get("input") or "")[:10].lower()
            dep = ok and (h in dep_hashes or sel in DEPOSIT_SELECTORS)
            rows.append([h, (r.get("from") or "").lower(), int(r.get("gasPrice") or 0), dep])
        if not full:
            break
        if top <= lo:
            raise RuntimeError(f"block {top} of {contract} holds more than {CRAWL_PAGE} txs")
        lo = top
    _jsave(name, rows)
    print(f"[tile] {contract} {tile}: {len(rows)} txs", file=sys.stderr, flush=True)
    return rows


class GasUniqueness:
    """Is a deposit gas price unique among Tornado deposits (within the scanned tiles)?

    Offline (``client`` None) only cached tiles are read; a level whose tiles are not
    all cached leaves the price unresolved.
    """

    def __init__(self, client, dep_hashes, levels=LEVELS, full=False, first=0, head=None):
        self.client = client
        self.dep_hashes = dep_hashes
        self.levels = list(levels) + ([FULL] if full else [])
        self.first_tile = first // TILE
        self.last_tile = None if head is None else head // TILE
        self.state = _jload("gas_unique.json", {})  # str(price) -> verdict

    def _rows(self, contract, tile):
        if self.client is None:
            return _jload(os.path.join("tiles", f"{contract}_{tile}.json"), None)
        return tile_rows(self.client, contract, tile, self.dep_hashes)

    def _tiles(self, block, radius):
        tiles = tiles_for(block, radius)
        return [
            t
            for t in tiles
            if t >= self.first_tile and (self.last_tile is None or t <= self.last_tile)
        ]

    def resolve(self, price, own, senders_out, max_radius=None):
        """own: {deposit hash: real block} of the sample deposits with this price.

        Widens the scan level by level up to ``max_radius`` (default: all levels)
        and stops at the first level that shows a second deposit at the price.
        """
        key = str(price)
        v = self.state.get(key)
        if v and (not v["unique"] or v["radius"] >= self.levels[-1]):
            return v
        for radius in self.levels:
            if max_radius is not None and radius > max_radius:
                break
            if v and v["radius"] >= radius:
                continue
            found = set(own)
            for c in SCAN_CONTRACTS:
                for b in sorted(set(own.values())):
                    for tile in self._tiles(b, radius):
                        rows = self._rows(c, tile)
                        if rows is None:
                            return v
                        for h, frm, g, dep in rows:
                            senders_out[h] = frm
                            if dep and g == price:
                                found.add(h)
            v = {"unique": len(found) <= 1, "radius": radius, "deposits_seen": len(found)}
            self.state[key] = v
            if self.client is not None:
                _jsave("gas_unique.json", self.state)
            if not v["unique"]:
                break
        return v


def make_client():
    from ens_labels import api_keys

    from tornado_demix.etherscan import EtherscanClient
    from tornado_demix.networks import get_network

    network = get_network("ethereum")
    return EtherscanClient(api_keys()[-1], pause=1.0, retries=8, **network.client_kwargs())


# --------------------------------------------------------------------------- main


def build_context(sets, args, wds_univ, dep_hashes, blocks):
    """Gas uniqueness, withdrawal senders and (optionally) pair counts for all runs."""
    client = None if args.offline_only else make_client()
    budget = Budget(client, args.max_calls) if client else None
    notes = {"api_calls": 0}

    # Deposit gas prices that some examined withdrawal reuses, with the real deposit blocks
    # (the same deposit transactions in target and decoy).
    price_blocks = defaultdict(dict)  # price -> {deposit hash: real block}
    cand_hashes = set()
    for rows in sets.values():
        for _w, t, d in rows:
            for data in (t, d):
                cands = gas_candidates(data)
                if not cands:
                    continue
                used = {g for *_x, g in cands}
                for dep in data.get("deposits", []):
                    if dep.get("gas_price") in used and dep.get("block"):
                        price_blocks[dep["gas_price"]][dep["hash"].lower()] = dep["block"]
                for *_x, r, _g in cands:
                    cand_hashes.add((r.get("hash") or "").lower())
    # Same price on two different deposits of the sample: non-unique without any lookup.
    seen_dep = defaultdict(set)
    for rows in sets.values():
        for _w, t, _d in rows:
            for dep in t.get("deposits", []):
                if dep.get("gas_price"):
                    seen_dep[dep["gas_price"]].add(dep["hash"].lower())

    senders = _jload("senders.json", {})
    lo, hi = blocks
    gu = GasUniqueness(client, dep_hashes, full=args.gas_full, first=lo, head=hi)
    verdicts = {}
    for price in price_blocks:
        if len(seen_dep[price]) > 1:
            verdicts[price] = {"unique": False, "radius": 0, "by": "sample"}
        else:
            verdicts[price] = {"unique": None, "radius": None}
    budget_hit = False
    try:
        # Level by level over all prices, so a call budget is spent on the cheap
        # narrow scans first.
        for radius in gu.levels:
            for price in sorted(p for p, v in verdicts.items() if v.get("by") != "sample"):
                if verdicts[price].get("unique") is False:
                    continue
                v = gu.resolve(price, price_blocks[price], senders, max_radius=radius)
                if v:
                    verdicts[price] = v
    except BudgetExceeded:
        budget_hit = True
    try:
        # Sender of every candidate withdrawal whose price may be unique (tutela variant).
        need = set()
        for rows in sets.values():
            for _w, t, d in rows:
                for data in (t, d):
                    for _pk, _a, r, g in gas_candidates(data):
                        h = (r.get("hash") or "").lower()
                        if verdicts.get(g, {}).get("unique") is not False and h not in senders:
                            need.add(h)
        if client is not None:
            for h in sorted(need):
                senders[h] = client.tx_sender(h) or ""
    except BudgetExceeded:
        budget_hit = True
    finally:
        if client is not None:
            _jsave("senders.json", {h: senders[h] for h in senders if h in cand_hashes})
        if budget:
            notes["api_calls"] += budget.calls
            _log_calls("gas", budget.calls)

    unresolved = [p for p, v in verdicts.items() if v.get("unique") is None]

    def unique(price):
        v = verdicts.get(price)
        if v is None:
            return False
        # Unresolved (offline, no cached tiles): counted as unique -> an upper bound.
        return v.get("unique") is not False

    pair_counts = None
    need_linked = sorted(
        {
            w
            for rows in sets.values()
            for w, t, d in rows
            if linked_candidates(t) | linked_candidates(d)
        }
    )
    notes["linked_crawl_depositors"] = len(need_linked)
    if args.with_linked:
        pair_counts = _jload("pair_counts.json", {})
        missing = [w for w in need_linked if w not in pair_counts]
        if missing and client is None:
            raise SystemExit(
                f"--with-linked: {len(missing)} depositors not cached; run without --offline-only"
            )
        cand_by_wallet = defaultdict(set)
        for rows in sets.values():
            for w, t, d in rows:
                cand_by_wallet[w] |= linked_candidates(t) | linked_candidates(d)
        start = budget.calls if budget else 0
        try:
            for w in missing:
                txs = client.outgoing_txs(w)  # txlist: both directions
                c = {a: [0, 0] for a in cand_by_wallet[w]}
                for tx in txs:
                    frm, to = (tx.get("from") or "").lower(), (tx.get("to") or "").lower()
                    if frm == w and to in c:
                        c[to][0] += 1
                    elif to == w and frm in c:
                        c[frm][1] += 1
                pair_counts[w] = c
                _jsave("pair_counts.json", pair_counts)
        except BudgetExceeded as exc:
            raise SystemExit(f"{exc}; linked crawl incomplete, rerun to resume") from exc
        finally:
            if budget:
                notes["api_calls"] += budget.calls - start
                notes["linked_api_calls"] = budget.calls - start
                _log_calls("linked", budget.calls - start)

    gas_info = {
        "candidate_prices": len(price_blocks),
        "nonunique_by_sample": sum(1 for v in verdicts.values() if v.get("by") == "sample"),
        "nonunique_by_scan": sum(
            1 for v in verdicts.values() if v.get("unique") is False and v.get("by") != "sample"
        ),
        "unique_within_scanned_range": {
            str(p): v.get("radius") for p, v in sorted(verdicts.items()) if v.get("unique") is True
        },
        "budget_exhausted": budget_hit,
        "scan_radius_blocks": (LEVELS[-1] if not args.gas_full else "full history"),
        "unresolved_counted_as_unique": len(unresolved),
        "candidate_withdrawals": len(cand_hashes),
        "senders_known": sum(1 for h in cand_hashes if h in senders),
    }
    ctx = {
        "unique": unique,
        "senders": senders,
        "withdrawals_of": lambda a: wds_univ.get(a, []),
        "pair_counts": pair_counts,
    }
    return ctx, notes, gas_info


def evaluate(rows, ctx, keys):
    per = {k: [] for k in keys}
    examples = defaultdict(list)
    for w, t, d in rows:
        lt, ld = run_leads(t, ctx), run_leads(d, ctx)
        te, de = exposure(t), exposure(d)
        for k in keys:
            per[k].append((len(lt.get(k, ())), te, len(ld.get(k, ())), de))
            for side, leads in (("target", lt), ("decoy", ld)):
                for pk, a in sorted(leads.get(k, ())):
                    if k not in ("any_tutela", "any_beres"):
                        examples[k].append([side, w, pk, a])
    return {k: ratio_row(per[k]) for k in keys}, examples


def _fmt_ci(ci):
    return "-" if not ci else f"[{ci[0]}, {ci[1] if ci[1] is not None else 'inf'}]"


def render_md(summary):
    lines = [
        "# Tutela / Beres et al. heuristics under the placebo design",
        "",
        "Generated by `tools/tutela_baseline.py` (see its docstring for the sources, "
        "section numbers, rules and deviations).",
        "",
        "R = (decoy leads / decoy withdrawals) / (target leads / target withdrawals): "
        "R ~ 1 is chance level, R << 1 means the heuristic fires above chance in the "
        "real windows. Leads are distinct (pool, recipient) pairs. Read the exact CI "
        "when a side has fewer than 5 leads (marked *).",
        "",
        f"Mode: {summary['mode']}. Explorer calls this run: {summary['api_calls']}.",
        "",
    ]
    for set_name, res in summary["sets"].items():
        first = next(iter(res.values()))
        lines += [
            f"## {set_name} ({first['depositors']} depositors; "
            f"{first['target_withdrawals']} target / {first['decoy_withdrawals']} decoy withdrawals)",
            "",
            "| heuristic | source | target leads | decoy leads | R | bootstrap 95% | exact 95% |",
            "|---|---|---:|---:|---:|---|---|",
        ]
        for k, r in res.items():
            lines.append(
                f"| {k}{' *' if r['few'] else ''} | {SOURCES.get(k, '')} | {r['target_leads']} | "
                f"{r['decoy_leads']} | {r['R'] if r['R'] is not None else '-'} | "
                f"{_fmt_ci(r['R_boot95'])} | {_fmt_ci(r['R_exact95'])} |"
            )
        lines.append("")
    g = summary["gas"]
    lines += [
        "## Notes",
        "",
        f"* Gas: {g['candidate_prices']} deposit gas prices reused by an examined withdrawal; "
        f"{g['nonunique_by_sample']} non-unique within the sample's own deposits, "
        f"{g['nonunique_by_scan']} by the explorer scan, "
        f"{len(g['unique_within_scanned_range'])} unique within +-{g['scan_radius_blocks']} blocks, "
        f"{g['unresolved_counted_as_unique']} unresolved (counted as unique, upper bound).",
        f"* linked_tutela needs {summary['linked_crawl_depositors']} depositor transaction lists "
        f"({'included' if summary['with_linked'] else 'not run: use --with-linked'}).",
        "* TORN mining [T 6.5] omitted (see the docstring).",
        "",
    ]
    return "\n".join(lines)


SOURCES = {
    "address_match": "T 6.1",
    "beres_h1": "B 7.1 H1",
    "gas_tutela": "T 6.2",
    "gas_beres": "B 7.1 H2",
    "beres_h3": "B 7.1 H3",
    "linked_tutela": "T 6.3",
    "multi_denom": "T 6.4",
    "any_tutela": "T 6.1-6.4",
    "any_beres": "B H1-H3",
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", nargs="+", default=[OUT], help="placebo sample dir(s)")
    ap.add_argument("--with-linked", action="store_true", help="Tutela linked ETH addresses")
    ap.add_argument("--offline-only", action="store_true", help="no explorer calls")
    ap.add_argument("--gas-full", action="store_true", help="scan whole history for gas")
    ap.add_argument("--max-calls", type=int, default=5000)
    args = ap.parse_args(argv)

    sets = {}
    for base in args.dir:
        name = os.path.relpath(os.path.abspath(base), os.path.dirname(OUT)).replace("\\", "/")
        sets[name] = load_dir(base)
    wds_univ, dep_hashes, blocks = load_universe()
    ctx, notes, gas_info = build_context(sets, args, wds_univ, dep_hashes, blocks)

    keys = [k for k in HEURISTICS if k != "linked_tutela" or args.with_linked]
    keys += ["any_tutela", "any_beres"]
    summary = {
        "mode": "offline-only" if args.offline_only else "network (cached)",
        "with_linked": args.with_linked,
        "api_calls": notes["api_calls"],
        "api_calls_cumulative": _jload("calls.json", {}),
        "linked_api_calls": notes.get("linked_api_calls"),
        "linked_crawl_depositors": notes["linked_crawl_depositors"],
        "gas": gas_info,
        "ratio": "R = (decoy leads / decoy withdrawals) / (target leads / target withdrawals)",
        "seed": SEED,
        "boot": BOOT,
        "sets": {},
        "leads": {},
    }
    all_rows = []
    for name, rows in sets.items():
        summary["sets"][name], ex = evaluate(rows, ctx, keys)
        summary["leads"][name] = ex
        all_rows += rows
    if len(sets) > 1:
        summary["sets"]["pooled"], _ = evaluate(all_rows, ctx, keys)
    os.makedirs(TUT, exist_ok=True)
    with open(os.path.join(TUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    md = render_md(summary)
    with open(os.path.join(TUT, "TUTELA.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)


if __name__ == "__main__":
    main()
