#!/usr/bin/env python3
"""Do Tornado exits go on through a bridge? Measured on the cached hold-out runs.

Takes the moderate and strong candidate exit addresses of the cached hold-out
target runs (Ethereum, ``.cache/labels/placebo/holdout1`` and ``holdout2``), at
most 300, in a fixed order (hold-out, pool, address). For each exit the first two
outgoing hops are read from the explorer:

* hop 1: every successful transaction the exit sent in the 30 days after its
  first withdrawal. A call to a bridge contract counts whatever its value (an
  ERC-20 bridge call carries none).
* hop 2: the same for the recipients of plain native transfers of at least 1 %
  of the exit's received amount (the five largest per exit).

A recipient is classified by address against ``tornado_demix/data/bridges.csv``;
every other contract is "other". The gate: if no protocol accounts for at least
5 % of the exits that hit a bridge, the next step (following the trace across a
bridge) is not built.

Calls that bring funds in from the other chain (Polygon exit(bytes), Wormhole
redeem) are not counted. Limits: a bridge call from an exit is not amount-attributed (it may move funds
the exit held for another reason); swaps before the bridge, token transfers at
hop 2 and bridges reached through an aggregator are not seen, so the share is a
lower bound.

Usage
-----
    python tools/bridge_exits.py [--limit 300] [--out .cache/bridge_exits]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tornado_demix import config  # noqa: E402
from tornado_demix.bridges import classify, is_inbound, load_bridges  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import ranked_candidates  # noqa: E402
from tornado_demix.registry import record_safe as record  # noqa: E402

PLACEBO = os.path.join(ROOT, ".cache", "labels", "placebo")
OUT = os.path.join(ROOT, ".cache", "bridge_exits")
HOLDOUTS = ("holdout1", "holdout2")
WINDOW_BLOCKS = 216_000  # about 30 days of Ethereum blocks
DUST_FRACTION = 0.01
HOP2_FANOUT = 5
BUSY_ROWS = 500  # a hop-2 recipient with this many txs in the window is skipped
GATE_SHARE = 0.05
CHAIN_ID = 1


def exit_candidates(limit=300, base=PLACEBO):
    """Moderate/strong exits of the cached target runs in a fixed order."""
    out = []
    for name in HOLDOUTS:
        folder = os.path.join(base, name, "target")
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            with open(os.path.join(folder, fn), encoding="utf-8") as fh:
                data = json.load(fh)
            for r in ranked_candidates(data):
                if r["band"] not in ("strong", "moderate"):
                    continue
                recs = data["denoms"][r["pool_key"]]["detail"].get(r["address"]) or []
                blocks = [int(x["block"]) for x in recs if x.get("block")]
                if not blocks:
                    continue
                out.append(
                    {
                        "holdout": name,
                        "pool_key": r["pool_key"],
                        "address": r["address"].lower(),
                        "band": r["band"],
                        "amount": float(r["total_value"]),
                        "asset": r["asset"],
                        "block": min(blocks),
                    }
                )
    out.sort(key=lambda e: (e["holdout"], e["pool_key"], e["address"]))
    return out[:limit]


def _filter_sent(rows, addr, start, end):
    return [
        r
        for r in rows
        if (r.get("from") or "").lower() == addr
        and r.get("isError") != "1"
        and r.get("to")
        and start <= int(r.get("blockNumber") or 0) <= end
    ]


def _sent(client, addr, start, end):
    return _filter_sent(client.fetch_all("txlist", addr, start, end), addr, start, end)


def _sent_bounded(client, addr, start, end):
    """Sent txs of a hop-2 recipient; None for a busy address (a service, not a person).

    Only the first BUSY_ROWS transactions of the window are read, so a hot wallet
    costs one call, not a ten-page crawl.
    """
    rows = client.first_txs(addr, start, end, BUSY_ROWS)
    if len(rows) >= BUSY_ROWS:
        return None
    return _filter_sent(rows, addr, start, end)


def _is_call(row):
    return (row.get("input") or "0x") not in ("", "0x")


def bridge_hops(client, table, exit_addr, amount, block, chain_id=CHAIN_ID, unknown=None):
    """Bridge calls within two hops of ``exit_addr`` after ``block``.

    Returns [{hop, protocol, to, tx_hash, via}]; ``via`` is the hop-1 recipient
    for a hop-2 hit and empty for hop 1.

    ``unknown``, when a list, collects the recipients the table cannot name:
    {address, hop, kind} with kind "contract" (a call to an unlisted address) or
    "busy" (a hop-2 recipient with BUSY_ROWS or more transactions in the window,
    skipped as a service).
    """
    exit_addr = exit_addr.lower()
    end = block + WINDOW_BLOCKS
    hits, mids = [], []
    for r in _sent(client, exit_addr, block, end):
        to = r["to"].lower()
        proto = classify(table, chain_id, to)
        if proto != "other" and is_inbound(r.get("input", "")[:10]):
            continue
        if proto != "other":
            hits.append(
                {
                    "hop": 1,
                    "protocol": proto,
                    "to": to,
                    "tx_hash": r.get("hash", ""),
                    "via": "",
                    "selector": (r.get("input") or "")[:10],
                }
            )
            continue
        if unknown is not None and _is_call(r):
            unknown.append({"address": to, "hop": 1, "kind": "contract"})
        value = int(r.get("value") or 0) / 10**18
        if r.get("input") in ("", "0x") and value >= amount * DUST_FRACTION:
            mids.append((value, to, int(r["blockNumber"])))
    mids.sort(key=lambda m: (-m[0], m[1]))
    seen = set()
    for _value, mid, mid_block in mids:
        if mid in seen or len(seen) >= HOP2_FANOUT:
            continue
        seen.add(mid)
        rows = _sent_bounded(client, mid, mid_block, mid_block + WINDOW_BLOCKS)
        if rows is None and unknown is not None:
            unknown.append({"address": mid, "hop": 1, "kind": "busy"})
        for r in rows or []:
            to = r["to"].lower()
            proto = classify(table, chain_id, to)
            if proto == "other" and unknown is not None and _is_call(r):
                unknown.append({"address": to, "hop": 2, "kind": "contract"})
            if proto != "other" and not is_inbound(r.get("input", "")[:10]):
                hits.append(
                    {
                        "hop": 2,
                        "protocol": proto,
                        "to": to,
                        "tx_hash": r.get("hash", ""),
                        "via": mid,
                        "selector": (r.get("input") or "")[:10],
                    }
                )
    return hits


def summarise(per_exit, gate_share=GATE_SHARE):
    """Counts per protocol over distinct exit addresses, and the gate verdict.

    An address that exits several pools appears once: the unit is the address.
    """
    merged = {}
    for i, e in enumerate(per_exit):
        merged.setdefault(e.get("address", i), set()).update(e["protocols"])
    by = Counter()
    for protos in merged.values():
        for p in protos:
            by[p] += 1
    bridge_exits = sum(1 for protos in merged.values() if protos)
    top = max(sorted(by), key=lambda p: by[p]) if by else None
    share = by[top] / bridge_exits if top else 0.0
    return {
        "exits": len(merged),
        "exit_rows": len(per_exit),
        "bridge_exits": bridge_exits,
        "by_protocol": dict(by),
        "top": top,
        "top_share_of_bridge_exits": share,
        "top_share_of_all_exits": by[top] / len(merged) if top else 0.0,
        "gate": bool(top) and share >= gate_share,
    }


class CachedClient:
    """Explorer client that keeps every answer on disk, so a rerun costs no calls."""

    def __init__(self, client, folder):
        self.client = client
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def fetch_all(self, action, address, start_block=0, end_block=0, extra=None):
        key = f"{action}|{address}|{start_block}|{end_block}"
        path = os.path.join(self.folder, hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        rows = self.client.fetch_all(action, address, start_block, end_block, extra=extra)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        return rows

    def first_txs(self, address, start_block, end_block, n):
        """The first ``n`` normal transactions of ``address`` in a block range."""
        key = f"first{n}|{address}|{start_block}|{end_block}"
        path = os.path.join(self.folder, hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        rows = self.client.call(
            {
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": start_block,
                "endblock": end_block,
                "page": 1,
                "offset": n,
                "sort": "asc",
            }
        )
        rows = rows if isinstance(rows, list) else []
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        return rows


def unknown_section(per_exit, top=15):
    """Recipients at hops 1-2 the table cannot name: counts and the most common ones."""
    by_addr = {}
    exits_contract, exits_busy = set(), set()
    for e in per_exit:
        for u in e.get("unknown", []):
            by_addr.setdefault((u["address"], u["kind"]), set()).add(e["address"])
            (exits_busy if u["kind"] == "busy" else exits_contract).add(e["address"])
    n = len({e["address"] for e in per_exit})
    lines = [
        "",
        "## Unrecognised recipients (hops 1-2)",
        "",
        f"Exits that called a contract not in the table: {len(exits_contract)} of {n}. "
        f"Exits with a busy hop-1 recipient (at least {BUSY_ROWS} transactions in the 30-day "
        f"window, skipped as a service): {len(exits_busy)} of {n}. The busy threshold is "
        f"{BUSY_ROWS} transactions in the window, not 10,000 in total: the explorer gives no "
        "cheap transaction count.",
        "",
        f"Top {top} unrecognised recipients by number of exit addresses:",
        "",
        "| address | kind | exit addresses |",
        "|---|---|---:|",
    ]
    ranked = sorted(by_addr.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:top]
    for (addr, kind), who in ranked:
        lines.append(f"| {addr} | {kind} | {len(who)} |")
    return lines


def render(summ, per_exit, table):
    rows = sorted(summ["by_protocol"].items(), key=lambda kv: (-kv[1], kv[0]))
    hop = Counter()
    for e in per_exit:
        for p in {(h["protocol"], h["hop"]) for h in e["hits"]}:
            hop[p] += 1
    n, b = summ["exits"], summ["bridge_exits"]
    lines = [
        "# Bridge exits after Tornado (exploratory)",
        "",
        f"Exits: {n} distinct addresses ({summ['exit_rows']} address-pool rows) among the "
        "moderate/strong candidates of the cached hold-out target runs "
        "(Ethereum; holdout1 + holdout2), first two outgoing hops each. "
        f"Bridge table: {len(table)} addresses (`tornado_demix/data/bridges.csv`).",
        "",
        f"Exits with a bridge call within 2 hops: **{b} of {n}** ({(b / n if n else 0):.1%}).",
        "",
        "| protocol | kind | exits | share of all exits | share of bridge exits | hop 1 | hop 2 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    kinds = {}
    for row in table.values():
        kinds.setdefault(row["protocol"], set()).add(row["kind"])
    for p, c in rows:
        kind = "/".join(sorted(kinds.get(p, {"?"})))
        lines.append(
            f"| {p} | {kind} | {c} | {c / n:.1%} | {c / b:.1%} | {hop[(p, 1)]} | {hop[(p, 2)]} |"
        )
    lines += [
        "",
        f"Gate (a protocol with at least {GATE_SHARE:.0%} of the bridge exits): "
        + ("PASSED" if summ["gate"] else "FAILED")
        + (f"; top protocol {summ['top']}." if summ["top"] else "; no bridge hit."),
        "",
        "Limits: bridge calls are not amount-attributed; swaps before a bridge and "
        "token transfers at hop 2 are not seen; an aggregator hit says the funds went into "
        "a router, not which bridge it used; maker-based bridges (EOA makers) are not in "
        "the table. The figures are lower bounds. Candidates are leads, not confirmed exits.",
    ]
    lines += unknown_section(per_exit)
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    table = load_bridges()
    exits = exit_candidates(args.limit)
    client = CachedClient(
        EtherscanClient(config.load_api_key(), pause=0.34), os.path.join(args.out, "api")
    )
    per_exit = []
    for i, e in enumerate(exits, 1):
        unknown = []
        hits = bridge_hops(client, table, e["address"], e["amount"], e["block"], unknown=unknown)
        per_exit.append(
            dict(e, hits=hits, unknown=unknown, protocols=sorted({h["protocol"] for h in hits}))
        )
        print(f"[{i}/{len(exits)}] {e['address']} {e['pool_key']}: {len(hits)} bridge hit(s)")
        sys.stdout.flush()
    summ = summarise(per_exit)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "per_exit.json"), "w", encoding="utf-8") as fh:
        json.dump(per_exit, fh, indent=1, sort_keys=True)
    with open(os.path.join(args.out, "RESULTS.md"), "w", encoding="utf-8") as fh:
        fh.write(render(summ, per_exit, table))
    print(json.dumps(summ, indent=1))
    record("bridge_exits.gate", __file__, {"limit": args.limit, "exits": len(exits)}, summ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
