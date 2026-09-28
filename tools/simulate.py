#!/usr/bin/env python3
"""Synthetic benchmark: how well the demix method finds a known exit.

Deposit -> withdrawal pairs with a known answer are not public, so this tool
generates them. A fake explorer replays depositors' deposits and the pools'
Withdrawal logs through the real pipeline (``multi.correlate`` and
``run_demix``). The depositor's true exits are known; every other recipient is
an unrelated withdrawal. No network is used.

Experiments (``--experiment``):

* ``ablation``    - configurations A-F, each adding one component to the last;
* ``controls``    - negative controls: wallets with no findable exit in the window;
* ``counter``     - counter-measures, one at a time;
* ``intensity``   - detection against the strength of a counter-measure;
* ``operators``   - operator links between two wallets, with and without the
  window-overlap suppression (configuration G);
* ``sensitivity`` - the same measures under other assumptions about the field.

Pair-level metrics are pooled over trials. Every recipient of a searched window
is one instance, the depositor's exits are the positives and a listed candidate
is a positive prediction: precision, recall, F1, false-positive rate, the same
for "band >= moderate", top-1 / top-5 hit rate (a true exit among the first k
rows) and PR-AUC (average precision of the band-then-score order).

The field is a modelling assumption: each unrelated recipient gets a geometric
number of withdrawals (most get one), some use several pools, a share of
withdrawals is self-relayed, and a few reuse a depositor's gas price by chance.
Blocks are treated as pre-EIP-1559, so the gas-price signal is live. The numbers
describe the method under these assumptions, not its accuracy on real cases.

Usage
-----
    python tools/simulate.py --experiment all --trials 200 --seed 1
"""

import argparse
import contextlib
import io
import os
import random
import sys
from dataclasses import dataclass, field, replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix import demix  # noqa: E402
from tornado_demix.constants import TOPIC_WITHDRAWAL, ZERO_ADDRESS  # noqa: E402
from tornado_demix.heuristics import BAND_ORDER, _score, ranked_candidates  # noqa: E402
from tornado_demix.multi import correlate  # noqa: E402
from tornado_demix.networks import Network  # noqa: E402
from tornado_demix.pools import Pool  # noqa: E402

POOL_01 = "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc"
POOL_1 = "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936"
POOL_10 = "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf"
DENOM = {POOL_01: 0.1, POOL_1: 1.0, POOL_10: 10.0}
NETWORK = Network(
    "ethereum",
    1,
    "ETH",
    [Pool(address, denom, "ETH", 18, None) for address, denom in DENOM.items()],
)
RELAYER = "0x" + "cc" * 20
T0 = 1_600_000_000  # September 2020, before EIP-1559
BLOCK_SECONDS = 13
COMMON_GAS = [g * 10**9 for g in (20, 25, 30, 40, 55, 70)]

ABLATION = [
    ("A", "amount+timing, no gate", None),
    ("B", "+ discrimination gate", {"count_match"}),
    ("C", "+ self-relay", {"count_match", "self_relayed"}),
    ("D", "+ gas price", {"count_match", "self_relayed", "gas_price"}),
    ("E", "+ linked address", {"count_match", "self_relayed", "gas_price", "linked"}),
    (
        "F",
        "+ denomination profile",
        {"count_match", "self_relayed", "gas_price", "linked", "profile_match"},
    ),
]
FULL = ABLATION[-1][2]


@dataclass
class Field:
    """Unrelated withdrawals in each searched pool window."""

    size: int = 50  # recipients per pool
    p_more: float = 0.4  # chance of one more withdrawal to the same recipient
    p_cross: float = 0.1  # share of recipients that also appear in the other pools
    p_self: float = 0.15  # share of withdrawals sent without a relayer
    p_gas: float = 0.002  # chance a withdrawal reuses a depositor's gas price


@dataclass
class Case:
    """One depositor and what its exit leaves behind."""

    notes: dict = field(default_factory=lambda: {POOL_1: 3})
    window_hours: int = 24
    self_relay: bool = False
    gas_reuse: bool = False
    linked: bool = False
    delayed: int = 0  # notes withdrawn after the search window
    split: int = 1  # exit addresses the notes are spread over


class SimClient:
    """Serves generated transactions and Withdrawal logs like the explorer client."""

    def __init__(self):
        self.txs = {}
        self.logs = {pool: [] for pool in DENOM}
        self._nonce = 0

    def outgoing_txs(self, address):
        return self.txs.get(address.lower(), [])

    def internal_txs(self, address):
        return []

    def token_transfers(self, address, contract=None):
        return []

    def block_by_time(self, ts, closest="before"):
        return int(ts) // BLOCK_SECONDS

    def current_block(self):
        return 10**12

    def get_logs(self, address, topic0, start_block, end_block):
        # The explorer returns logs in block order, not in the order they were made.
        rows = [
            log
            for log in self.logs[address.lower()]
            if start_block <= int(log["blockNumber"], 16) <= end_block
        ]
        return sorted(rows, key=lambda log: int(log["blockNumber"], 16))

    def add_tx(self, sender, to, value_eth, ts, gas_price):
        self._nonce += 1
        self.txs.setdefault(sender, []).append(
            {
                "from": sender,
                "to": to,
                "value": str(int(round(value_eth * 10**18))),
                "timeStamp": str(ts),
                "blockNumber": str(ts // BLOCK_SECONDS),
                "hash": "0x%064x" % self._nonce,
                "gasPrice": str(gas_price),
                "isError": "0",
            }
        )

    def add_withdrawal(self, pool, to, ts, self_relayed, gas_price):
        self._nonce += 1
        relayer = ZERO_ADDRESS if self_relayed else RELAYER
        fee = 0 if self_relayed else int(DENOM[pool] * 0.003 * 10**18)
        nullifier = "%064x" % self._nonce
        self.logs[pool].append(
            {
                "data": "0x" + "0" * 24 + to[2:] + nullifier + "%064x" % fee,
                "topics": [TOPIC_WITHDRAWAL, "0x" + "0" * 24 + relayer[2:]],
                "gasPrice": hex(gas_price),
                "transactionHash": "0x" + nullifier,
                "blockNumber": hex(ts // BLOCK_SECONDS),
                "timeStamp": hex(ts),
            }
        )


def new_address(rng):
    return "0x%040x" % rng.getrandbits(160)


def unusual_gas(rng):
    """A gas price a user set by hand, not one of the common wallet defaults."""
    return rng.randint(12, 150) * 10**9 + rng.randint(1, 999) * 10**6


def add_field(rng, client, fld, t_start, t_end, deposit_gas):
    """``fld.size`` unrelated recipients per pool, withdrawing inside [t_start, t_end]."""
    shared = [new_address(rng) for _ in range(int(fld.size * fld.p_cross))]
    for pool in DENOM:
        recipients = shared + [new_address(rng) for _ in range(fld.size - len(shared))]
        for recipient in recipients:
            k = 1
            while rng.random() < fld.p_more and k < 8:
                k += 1
            for _ in range(k):
                gas = rng.choice(COMMON_GAS)
                if deposit_gas and rng.random() < fld.p_gas:
                    gas = rng.choice(deposit_gas)
                client.add_withdrawal(
                    pool, recipient, rng.randint(t_start, t_end), rng.random() < fld.p_self, gas
                )


def add_depositor(rng, client, case, wallet, t0, exits=None):
    """Deposits, exits and signals of one wallet.

    Returns (true exits that received a note in the window, window end, deposit gas).
    """
    gas_price = unusual_gas(rng)
    last = t0
    for p, pool in enumerate(sorted(case.notes)):
        for i in range(case.notes[pool]):
            last = t0 + p * 1200 + i * 600
            client.add_tx(wallet, pool, DENOM[pool], last, gas_price)
    window_end = last + case.window_hours * 3600
    exits = exits or [new_address(rng) for _ in range(case.split)]
    per_pool = case.split == len(case.notes) > 1  # each pool to its own exit
    in_window = set()
    note = 0
    gas_used = False
    for p, pool in enumerate(sorted(case.notes)):
        for _ in range(case.notes[pool]):
            to = exits[(p if per_pool else note) % len(exits)]
            if note < case.delayed:
                ts = window_end + rng.randint(3600, 30 * 86400)
            else:
                ts = rng.randint(t0 + 60, window_end)
                in_window.add(to)
            gas = rng.choice(COMMON_GAS)
            if case.gas_reuse and not gas_used and note >= case.delayed:
                gas, gas_used = gas_price, True
            client.add_withdrawal(pool, to, ts, case.self_relay, gas)
            note += 1
    if case.linked:
        client.add_tx(wallet, exits[0], 0.05, t0 - 86400, rng.choice(COMMON_GAS))
    return in_window, window_end, gas_price


@contextlib.contextmanager
def _pre_london():
    """Every block counts as pre-EIP-1559; no counterparty is a contract."""
    gate, check = demix.make_gas_price_gate, demix.make_contract_check
    demix.make_gas_price_gate = lambda url, log=None: lambda block: True
    demix.make_contract_check = lambda url: lambda address: False
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        demix.make_gas_price_gate, demix.make_contract_check = gate, check


def run(client, wallets, window_hours):
    with _pre_london():
        return correlate(
            client,
            wallets,
            window_days=30,
            gap_hours=24,
            network=NETWORK,
            exit_window_hours=window_hours,
        )


def single_trial(rng, case, fld):
    client = SimClient()
    wallet = new_address(rng)
    t0 = T0 + rng.randint(0, 86400 * 300)
    truth, window_end, gas = add_depositor(rng, client, case, wallet, t0)
    add_field(rng, client, fld, t0, window_end, [gas])
    data = run(client, [wallet], case.window_hours)["results"][wallet]
    return data, truth


def restricted(data, allowed):
    """``data`` as if only the ``allowed`` signals existed."""
    denoms = {}
    for pool_key, res in data["denoms"].items():
        signals = {a: [s for s in sig if s in allowed] for a, sig in res["signals"].items()}
        confidence = {
            a: _score(set(sig), res["discrimination"].get(a, 0.0)) for a, sig in signals.items()
        }
        denoms[pool_key] = {**res, "signals": signals, "confidence": confidence}
    return {**data, "denoms": denoms}


def ranking(data, allowed, rng):
    """[(address, pool_key, band, sort score)] best first, for one configuration."""
    if allowed is None:  # A: every voucher-sized count, in no particular order
        rows = [
            (addr, pool_key, "weak", 1.0)
            for pool_key, res in data["denoms"].items()
            for addr, hits in res["counts"].items()
            if hits in set(res.get("target_counts", []))
        ]
        rng.shuffle(rows)
        return rows
    rows = ranked_candidates(restricted(data, allowed))
    return [
        (r["address"], r["pool_key"], r["band"], BAND_ORDER[r["band"]] * 10 + r["confidence"])
        for r in rows
    ]


class Score:
    """Pooled pair-level and trial-level counters for one configuration."""

    def __init__(self):
        self.pairs = []  # (sort score or None, is positive)
        self.tp = self.fp = self.fn = self.tn = 0
        self.tp_mod = self.fp_mod = 0
        self.trials = self.top1 = self.top5 = self.found = self.no_lead = 0
        self.bystander_band = {"strong": 0, "moderate": 0, "weak": 0}

    def add(self, data, truth, rows):
        listed = {(a, pk): (band, s) for a, pk, band, s in rows}
        for pool_key, res in data["denoms"].items():
            for addr in res["counts"]:
                positive = addr in truth
                hit = listed.get((addr, pool_key))
                self.pairs.append((hit[1] if hit else None, positive))
                if hit:
                    self.tp += positive
                    self.fp += not positive
                    if BAND_ORDER[hit[0]] >= BAND_ORDER["moderate"]:
                        self.tp_mod += positive
                        self.fp_mod += not positive
                else:
                    self.fn += positive
                    self.tn += not positive
        self.trials += 1
        addrs = [a for a, _pk, _b, _s in rows]
        self.top1 += bool(addrs) and addrs[0] in truth
        self.top5 += any(a in truth for a in addrs[:5])
        self.found += any(a in truth for a in addrs)
        self.no_lead += not rows
        bystanders = [b for a, _pk, b, _s in rows if a not in truth]
        if bystanders:
            self.bystander_band[max(bystanders, key=BAND_ORDER.get)] += 1

    def metrics(self):
        def div(a, b):
            return a / b if b else 0.0

        p, r = div(self.tp, self.tp + self.fp), div(self.tp, self.tp + self.fn)
        pm, rm = div(self.tp_mod, self.tp_mod + self.fp_mod), div(self.tp_mod, self.tp + self.fn)
        return {
            "precision": p,
            "recall": r,
            "f1": div(2 * p * r, p + r),
            "fpr": div(self.fp, self.fp + self.tn),
            "precision_mod": pm,
            "recall_mod": rm,
            "top1": div(self.top1, self.trials),
            "top5": div(self.top5, self.trials),
            "found": div(self.found, self.trials),
            "no_lead": div(self.no_lead, self.trials),
            "pr_auc": average_precision(self.pairs),
            **{f"bystander_{b}": div(n, self.trials) for b, n in self.bystander_band.items()},
        }


def average_precision(pairs):
    """Area under the precision-recall curve of a ranking; unlisted pairs rank last."""
    total = sum(pos for _s, pos in pairs)
    if not total:
        return 0.0
    listed = sorted((p for p in pairs if p[0] is not None), key=lambda p: -p[0])
    ap, tp, seen, i = 0.0, 0, 0, 0
    while i < len(listed):
        j = i
        while j < len(listed) and listed[j][0] == listed[i][0]:
            j += 1
        gained = sum(pos for _s, pos in listed[i:j])
        tp += gained
        seen += j - i
        ap += (gained / total) * (tp / seen)
        i = j
    return ap


def mixed_case(rng, window_hours=24):
    """A population of depositors: 2-5 notes, half in two pools, random extra signals."""
    notes = {POOL_1: rng.choice([2, 3, 5])}
    if rng.random() < 0.5:
        notes[POOL_01] = rng.choice([2, 3, 4])
    return Case(
        notes=notes,
        window_hours=window_hours,
        self_relay=rng.random() < 0.5,
        gas_reuse=rng.random() < 0.3,
        linked=rng.random() < 0.2,
    )


def experiment_ablation(trials, seed, sizes, fld=None):
    out = {}
    for size in sizes:
        rng = random.Random(seed)
        scores = {name: Score() for name, _label, _allowed in ABLATION}
        for _ in range(trials):
            data, truth = single_trial(rng, mixed_case(rng), replace(fld or Field(), size=size))
            for name, _label, allowed in ABLATION:
                scores[name].add(data, truth, ranking(data, allowed, rng))
        out[size] = {name: s.metrics() for name, s in scores.items()}
    return out


def experiment_controls(trials, seed, size, fld=None):
    """No findable exit in the window: every listed candidate is a false lead."""
    variants = {
        "exit after the window": lambda c: replace(c, delayed=sum(c.notes.values())),
        "one note per fresh address": lambda c: replace(c, split=sum(c.notes.values())),
    }
    out = {}
    for label, make in variants.items():
        rng = random.Random(seed)
        scores = {"A": Score(), "B": Score(), "F": Score()}
        for _ in range(trials):
            case = make(replace(mixed_case(rng), self_relay=False, gas_reuse=False, linked=False))
            data, truth = single_trial(rng, case, replace(fld or Field(), size=size))
            for name, _label, allowed in ABLATION:
                if name in scores:
                    scores[name].add(data, truth, ranking(data, allowed, rng))
        out[label] = {f"config {name}": s.metrics() for name, s in scores.items()}
    return out


def experiment_counter(trials, seed, size):
    """One counter-measure at a time, against the full model (configuration F)."""
    base = Case(notes={POOL_1: 3})
    two_pools = {POOL_1: 2, POOL_01: 3}
    sweeps = {
        "delay": [(f"{d} of 3 notes after the window", replace(base, delayed=d)) for d in range(4)],
        "fresh addresses": [(f"{s} exit address(es)", replace(base, split=s)) for s in (1, 2, 3)],
        "relayer": [
            ("self-relayed", replace(base, self_relay=True)),
            ("through a relayer", base),
        ],
        "gas strategy": [
            ("deposit gas price reused", replace(base, gas_reuse=True)),
            ("wallet default gas", base),
        ],
        "denominations": [
            ("both pools to one exit", Case(notes=two_pools)),
            ("each pool to its own exit", Case(notes=two_pools, split=2)),
        ],
    }
    out = {}
    for sweep, settings in sweeps.items():
        out[sweep] = {}
        for label, case in settings:
            rng = random.Random(seed)
            score = Score()
            for _ in range(trials):
                data, truth = single_trial(rng, case, Field(size=size))
                score.add(data, truth, ranking(data, FULL, rng))
            out[sweep][label] = score.metrics()
    return out


INTENSITY_NOTES = 6
INTENSITY_EXITS = {
    "amount+timing only": Case(notes={POOL_1: INTENSITY_NOTES}),
    "with a direct transfer": Case(notes={POOL_1: INTENSITY_NOTES}, linked=True),
}


def experiment_intensity(trials, seed, size):
    """Detection against the strength of a counter-measure, for a six-note voucher.

    Two sweeps: notes withdrawn after the window (0..6) and exit addresses the
    notes are spread over (1..6), each for an exit that leaves only amount and
    timing and for one that also transacted directly with the depositor.
    """
    out = {}
    for exit_label, base in INTENSITY_EXITS.items():
        for sweep, values, make in (
            ("delayed notes", range(INTENSITY_NOTES + 1), lambda b, v: replace(b, delayed=v)),
            ("exit addresses", range(1, INTENSITY_NOTES + 1), lambda b, v: replace(b, split=v)),
        ):
            for v in values:
                rng = random.Random(seed)
                score = Score()
                case = make(base, v)
                for _ in range(trials):
                    data, truth = single_trial(rng, case, Field(size=size))
                    score.add(data, truth, ranking(data, FULL, rng))
                out[(exit_label, sweep, v)] = score.metrics()
    return out


def print_intensity(result):
    print("\n### Counter-measure intensity, six-note voucher (full model)\n")
    cols = ["found", "recall", "top-1", "bystander strong", "bystander moderate"]
    keys = ["found", "recall", "top1", "bystander_strong", "bystander_moderate"]
    print(_row(["exit", "sweep", "value"] + cols))
    print(_row(["---"] * (len(cols) + 3)))
    for (exit_label, sweep, v), m in result.items():
        print(_row([exit_label, sweep, str(v)] + [f"{m[k]:.2f}" for k in keys]))


OPERATOR_KINDS = (
    "unrelated, identical fingerprints, same hour",
    "unrelated, distinct fingerprints, same hour",
    "one operator, distinct fingerprints, days apart",
)


def operator_trial(rng, size, kind):
    """Two wallets; returns (merged naively, merged by the suppressed graph)."""
    client = SimClient()
    w1, w2 = new_address(rng), new_address(rng)
    t1 = T0 + rng.randint(0, 86400 * 300)
    if kind == OPERATOR_KINDS[0]:
        c1 = c2 = Case(notes={POOL_1: 2})
        t2, shared_exit = t1 + rng.randint(60, 3000), None
    elif kind == OPERATOR_KINDS[1]:
        c1, c2 = Case(notes={POOL_1: 2, POOL_01: 3}), Case(notes={POOL_01: 3, POOL_10: 2})
        t2, shared_exit = t1 + rng.randint(60, 3000), None
    else:
        c1, c2 = Case(notes={POOL_1: 2, POOL_01: 3}), Case(notes={POOL_01: 3, POOL_10: 2})
        t2, shared_exit = t1 + 3 * 86400, [new_address(rng)]
    _, end1, gas1 = add_depositor(rng, client, c1, w1, t1, shared_exit)
    _, end2, gas2 = add_depositor(rng, client, c2, w2, t2, shared_exit)
    add_field(rng, client, Field(size=size), min(t1, t2), max(end1, end2), [gas1, gas2])
    corr = run(client, [w1, w2], 24)
    naive = bool(corr["strong"]) or bool(corr["cross_profile"])
    suppressed = any({w1, w2} <= set(c["wallets"]) for c in corr.get("operator_clusters", []))
    return naive, suppressed


def experiment_operators(trials, seed, size):
    out = {}
    for kind in OPERATOR_KINDS:
        rng = random.Random(seed)
        naive = suppressed = 0
        for _ in range(trials):
            n, s = operator_trial(rng, size, kind)
            naive += n
            suppressed += s
        out[kind] = {"merged_naive": naive / trials, "merged_suppressed": suppressed / trials}
    return out


def experiment_sensitivity(trials, seed, size):
    """Full-model results when the field is noisier or quieter than assumed."""
    variants = {
        "baseline": Field(),
        "no chance gas reuse": replace(Field(), p_gas=0.0),
        "chance gas reuse x5 (p=0.01)": replace(Field(), p_gas=0.01),
        "self-relay 5 %": replace(Field(), p_self=0.05),
        "self-relay 30 %": replace(Field(), p_self=0.30),
        "busier recipients (p_more=0.6)": replace(Field(), p_more=0.6),
    }
    out = {}
    for label, fld in variants.items():
        ab = experiment_ablation(trials, seed, [size], fld)[size]["F"]
        ctl = experiment_controls(trials, seed, size, fld)["exit after the window"]["config F"]
        out[label] = {
            "top1": ab["top1"],
            "pr_auc": ab["pr_auc"],
            "ctl_strong": ctl["bystander_strong"],
            "ctl_moderate": ctl["bystander_moderate"],
            "ctl_no_lead": ctl["no_lead"],
        }
    return out


def _row(cells):
    return "| " + " | ".join(cells) + " |"


def print_ablation(result):
    cols = ["P", "R", "F1", "FPR", "P >=mod", "R >=mod", "Top-1", "Top-5", "PR-AUC"]
    keys = ["precision", "recall", "f1", "fpr", "precision_mod", "recall_mod", "top1", "top5"]
    for size, rows in result.items():
        print(f"\n### Ablation, {size} unrelated recipients per pool window\n")
        print(_row(["Config", "Components"] + cols))
        print(_row(["---"] * (len(cols) + 2)))
        for name, label, _allowed in ABLATION:
            m = rows[name]
            cells = [f"{m[k]:.3f}" for k in keys] + [f"{m['pr_auc']:.3f}"]
            print(_row([name, label] + cells))


def print_trials(title, result, first_col):
    print(f"\n### {title}\n")
    cols = ["found", "top-1", "no lead", "bystander strong", "bystander moderate", "bystander weak"]
    keys = ["found", "top1", "no_lead", "bystander_strong", "bystander_moderate", "bystander_weak"]
    print(_row([first_col, "setting"] + cols))
    print(_row(["---"] * (len(cols) + 2)))
    for group, rows in result.items():
        for label, m in rows.items():
            print(_row([group, label] + [f"{m[k]:.2f}" for k in keys]))


def print_operators(result):
    print("\n### Operator links between two wallets (configuration G)\n")
    print(_row(["Wallets", "merged without suppression", "merged with suppression"]))
    print(_row(["---"] * 3))
    for kind, m in result.items():
        print(_row([kind, f"{m['merged_naive']:.2f}", f"{m['merged_suppressed']:.2f}"]))


def print_sensitivity(result):
    print("\n### Sensitivity to the field assumptions (full model)\n")
    cols = ["Top-1", "PR-AUC", "control: strong", "control: moderate", "control: no lead"]
    keys = ["top1", "pr_auc", "ctl_strong", "ctl_moderate", "ctl_no_lead"]
    print(_row(["Field"] + cols))
    print(_row(["---"] * (len(cols) + 1)))
    for label, m in result.items():
        print(_row([label] + [f"{m[k]:.2f}" for k in keys]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--experiment",
        choices=["ablation", "controls", "counter", "intensity", "operators", "sensitivity", "all"],
        default="all",
    )
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 50, 200])
    args = parser.parse_args(argv)
    todo = (
        ["ablation", "controls", "counter", "intensity", "operators", "sensitivity"]
        if args.experiment == "all"
        else [args.experiment]
    )
    size = 50
    print(f"Synthetic benchmark: {args.trials} trials per setting, seed {args.seed}.")
    if "ablation" in todo:
        print_ablation(experiment_ablation(args.trials, args.seed, args.sizes))
    if "controls" in todo:
        print_trials(
            f"Negative controls, {size} unrelated recipients per pool window",
            experiment_controls(args.trials, args.seed, size),
            "control",
        )
    if "counter" in todo:
        print_trials(
            f"Counter-measures against the full model, {size} unrelated recipients",
            experiment_counter(args.trials, args.seed, size),
            "counter-measure",
        )
    if "intensity" in todo:
        print_intensity(experiment_intensity(args.trials, args.seed, size))
    if "operators" in todo:
        print_operators(experiment_operators(args.trials, args.seed, size))
    if "sensitivity" in todo:
        print_sensitivity(experiment_sensitivity(args.trials, args.seed, size))


if __name__ == "__main__":
    main()
