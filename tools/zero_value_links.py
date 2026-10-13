"""Share of early direct links that rest only on zero-value transfers.

For every early direct link (signal `linked` of the current rule over the 30-day window, as
in tools/decoy_rates.py) and late direct link (`linked_late`) in the target and decoy runs of
the given placebo dirs (depositors with both runs), re-read the depositor's normal, internal and token transfers and
classify the link: "value" if any transfer between depositor and candidate moved a
non-zero amount, "zero" if every one of them moved nothing (the address-poisoning
pattern). Writes ZERO_VALUE_LINKS.md next to the dirs.

    python tools/zero_value_links.py holdout1 polygon1 holdout2 polygon2
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ens_labels import api_keys  # noqa: E402
from placebo_windows import OUT, narrow  # noqa: E402

from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import ranked_candidates  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402


def links(path):
    """(wallet, network, {signal: set of candidate addresses}) for linked and linked_late leads."""
    d = narrow(json.load(open(path, encoding="utf-8")), 720)
    out = {"linked": set(), "linked_late": set()}
    for r in ranked_candidates(d):
        for s in out:
            if s in r["signals"]:
                out[s].add((r["pool_key"], r["address"].lower()))
    return d["wallet"].lower(), d["params"]["network"], out


def classify(client, wallet, cands, memo):
    if wallet not in memo:
        txs = client.outgoing_txs(wallet) + client.internal_txs(wallet)
        try:
            txs += client.token_transfers(wallet)
        except Exception:  # noqa: BLE001 - a chain without token pools may refuse
            pass
        memo[wallet] = txs
    out = {}
    for c in cands:
        vals = [
            int(t.get("value") or 0)
            for t in memo[wallet]
            if {(t.get("from") or "").lower(), (t.get("to") or "").lower()} == {wallet, c}
        ]
        out[c] = "value" if any(v > 0 for v in vals) else ("zero" if vals else "unseen")
    return out


def main(dirs):
    lines = [
        "# Zero-value direct links",
        "",
        "Leads (pool, recipient) whose every depositor-candidate transfer moved a zero amount "
        "(value / zero / not found in the re-read history).",
        "",
        "| sample | signal | target: value / zero / unseen | decoy: value / zero / unseen |",
        "|---|---|---|---|",
    ]
    tot = {}
    clients = {}
    for name in dirs:
        base = os.path.join(OUT, name)
        memo, tally = {}, {}
        for f in sorted(os.listdir(os.path.join(base, "target"))):
            if not os.path.exists(os.path.join(base, "decoy", f)):
                continue
            for side in ("target", "decoy"):
                wallet, net, sigs = links(os.path.join(base, side, f))
                if not any(sigs.values()):
                    continue
                if net not in clients:
                    clients[net] = EtherscanClient(
                        api_keys()[0], pause=0.36, **get_network(net).client_kwargs()
                    )
                for sig, keys in sigs.items():
                    if not keys:
                        continue
                    kinds = classify(clients[net], wallet, {a for _, a in keys}, memo)
                    for _, a in keys:
                        tally.setdefault((sig, side), Counter())[kinds[a]] += 1
                        tot.setdefault((sig, side), Counter())[kinds[a]] += 1
        for sig in ("linked", "linked_late"):
            cells = [
                "{value} / {zero} / {unseen}".format(
                    **{k: tally.get((sig, sd), Counter())[k] for k in ("value", "zero", "unseen")}
                )
                for sd in ("target", "decoy")
            ]
            lines.append(f"| {name} | {sig} | {cells[0]} | {cells[1]} |")
        print(lines[-2], lines[-1], sep="\n", flush=True)
    for sig in ("linked", "linked_late"):
        cells = [
            "{value} / {zero} / {unseen}".format(
                **{k: tot.get((sig, sd), Counter())[k] for k in ("value", "zero", "unseen")}
            )
            for sd in ("target", "decoy")
        ]
        lines.append(f"| pooled | {sig} | {cells[0]} | {cells[1]} |")
    open(os.path.join(OUT, "ZERO_VALUE_LINKS.md"), "w", encoding="utf-8").write(
        "\n".join(lines) + "\n"
    )
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1:])
