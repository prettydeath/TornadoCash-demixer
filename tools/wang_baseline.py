#!/usr/bin/env python3
"""Wang et al. (WWW 2023) validation protocol on the ENS-labelled pairs.

Wang et al. validate their linking heuristics against side-channel labels
(airdrop, ENS) at the address level: test pairs are every labelled depositor
times every labelled withdrawer; a heuristic's predicted pairs among them are
compared with the labelled pairs, and anything predicted but unlabelled counts
as a false positive. Their reported average F1 of 0.55 comes almost entirely
from H3 (a direct transfer between the two addresses).

This script applies the same protocol to the pairs from tools/ens_labels.py,
for their heuristics re-implemented from the paper and for this tool:

* H2 improper withdrawal sender: the depositor sent the withdrawal to the withdrawer;
* H3 related pair: the two addresses transacted directly (ETH, internal or token);
* H5 cross-pool deposit: both use the same m > 1 pools, the depositor's deposit
  count equals the withdrawer's withdrawal count in each, and every withdrawal
  follows one of the depositor's deposits in that pool;
* demix: candidates of the cached run_demix results (tools/evaluate_labels.py),
  moderate or stronger, with and without the linked signal.

H1 (address reuse) is omitted, as in the paper: it does not link two addresses.
Unlike the paper, H3 is not cut at a time t; it looks at the whole history.

Usage
-----
    python tools/wang_baseline.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ens_labels import CACHE, _load, _log, api_keys  # noqa: E402
from placebo_windows import narrow  # noqa: E402

from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import confidence_band, ranked_candidates  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402

STATE = os.path.join(CACHE, "wang_state.json")


def _state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"cps": {}, "senders": {}}


def _save(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def metrics(pred, truth, deps, wds):
    pred = {(d, w) for d, w in pred if d in deps and w in wds and d != w}
    tp = len(pred & truth)
    fp = len(pred - truth)
    fn = len(truth - pred)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(p, 2),
        "recall": round(r, 2),
        "f1": round(f1, 2),
    }


def main():
    pairs = _load("pairs.json")
    uni = _load("universe.json")
    deps = {p["depositor"] for p in pairs}
    wds = {p["recipient"] for p in pairs}
    truth = {(p["depositor"], p["recipient"]) for p in pairs}
    network = get_network("ethereum")
    client = EtherscanClient(api_keys()[-1], pause=0.8, **network.client_kwargs())
    state = _state()
    tornado = {p.address for p in network.pools} | set(network.routers)

    # H3: direct counterparties of each labelled depositor.
    for d in sorted(deps):
        if d in state["cps"]:
            continue
        rows = client.outgoing_txs(d) + client.internal_txs(d) + client.token_transfers(d)
        cps = (
            {(t.get(side) or "").lower() for t in rows for side in ("from", "to")}
            - {d, ""}
            - tornado
        )
        state["cps"][d] = sorted(cps & wds)
        _save(state)
    h3 = {(d, w) for d, ws in state["cps"].items() for w in ws}

    # H2: who sent each withdrawal paid to a labelled withdrawer.
    wd_rows = [r for r in uni["withdrawals"] if r[1] in wds]
    _log(f"[*] {len(wd_rows)} withdrawals to labelled withdrawers")
    for _pool, _w, _ts, h in wd_rows:
        if h not in state["senders"]:
            state["senders"][h] = (client.tx_sender(h) or "").lower()
            if len(state["senders"]) % 50 == 0:
                _save(state)
    _save(state)
    h2 = {(state["senders"][h], w) for _p, w, _t, h in wd_rows if state["senders"].get(h) in deps}

    # H5: cross-pool deposit profile.
    dep_ts = defaultdict(lambda: defaultdict(list))
    for pool, a, ts, _h in uni["deposits"]:
        if a in deps:
            dep_ts[a][pool].append(ts)
    wd_ts = defaultdict(lambda: defaultdict(list))
    for pool, a, ts, _h in uni["withdrawals"]:
        if a in wds:
            wd_ts[a][pool].append(ts)
    h5 = set()
    for d in deps:
        for w in wds:
            pd, pw = dep_ts[d], wd_ts[w]
            if len(pd) < 2 or set(pd) != set(pw):
                continue
            if all(len(pd[p]) == len(pw[p]) and all(t > min(pd[p]) for t in pw[p]) for p in pd):
                h5.add((d, w))

    # This tool: cached run_demix results.
    ours = {"moderate+": set(), "moderate+ without linked": set(), "all bands": set()}
    res_dir = os.path.join(CACHE, "demix")
    for d in deps:
        path = os.path.join(res_dir, d + ".json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        # Re-apply the current heuristics to the cached run (see evaluate_labels._rows).
        data = narrow(data, 30 * 24)
        for r in ranked_candidates(data):
            ours["all bands"].add((d, r["address"]))
            if r["band"] in ("strong", "moderate"):
                ours["moderate+"].add((d, r["address"]))
            sig = set(r["signals"]) - {"linked", "linked_late"}
            if confidence_band(sig) in ("strong", "moderate"):
                ours["moderate+ without linked"].add((d, r["address"]))

    out = {
        "test_pairs": f"{len(deps)} x {len(wds)}",
        "labelled_pairs": len(truth),
        "Wang H2": metrics(h2, truth, deps, wds),
        "Wang H3": metrics(h3, truth, deps, wds),
        "Wang H5": metrics(h5, truth, deps, wds),
        "Wang H2+H3+H5": metrics(h2 | h3 | h5, truth, deps, wds),
    }
    for k, v in ours.items():
        out["demix " + k] = metrics(v, truth, deps, wds)
    with open(os.path.join(CACHE, "wang_baseline.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
