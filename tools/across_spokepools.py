#!/usr/bin/env python3
"""Check the Across SpokePools of data/bridges.csv against the Across hub contract.

The hub contract on Ethereum (``0xc186fa914353c44b2e33ebe05f21846f1048beda``) keeps,
for every chain it serves, the adapter and the SpokePool it pays relayers back on:
``crossChainContracts(uint256 chainId) -> (address adapter, address spokePool)``.
This tool reads that getter with one ``eth_call`` per chain of the networks registry
(no API key) and compares the answer with the SpokePool rows of the bridge table.

A destination chain without a listed SpokePool ends a bridge branch as
``destination_pool_not_listed``; a stale row searches the wrong contract for fills.
Run this after an Across upgrade and paste the printed rows for new chains into
``tornado_demix/data/bridges.csv`` (source: the hub contract page).

Usage
-----
    python tools/across_spokepools.py
    python tools/across_spokepools.py --chains 10,56,43114 --rpc https://ethereum-rpc.publicnode.com

Exit status 0 when every chain agrees, 1 otherwise.
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix.bridges import ACROSS, destination_pools, load_bridges  # noqa: E402

HUB = "0xc186fa914353c44b2e33ebe05f21846f1048beda"
HUB_URL = f"https://etherscan.io/address/{HUB}#readProxyContract"
SELECTOR = "0x09474ae2"  # keccak256("crossChainContracts(uint256)")[:4]
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"


def hub_spoke_pool(rpc, chain_id):
    """SpokePool the hub lists for ``chain_id``; "" for none; None when the call failed."""
    answer = rpc.eth_call(HUB, SELECTOR + "{:064x}".format(int(chain_id)))
    if not answer or len(answer) < 2 + 128:
        return None
    word = answer[2 + 64 : 2 + 128]
    return "" if int(word, 16) == 0 else "0x" + word[-40:].lower()


def compare(rpc, table, chain_ids):
    """One row per chain: {chain_id, hub, table, status}.

    ``status`` is "ok", "differs", "missing from the table", "not on the hub" (the hub
    serves no SpokePool there and the table lists none) or "call failed".
    """
    rows = []
    for chain in chain_ids:
        listed = destination_pools(table, chain)
        hub = hub_spoke_pool(rpc, chain)
        if hub is None:
            status = "call failed"
        elif hub == "":
            status = "not on the hub" if not listed else "differs"
        elif not listed:
            status = "missing from the table"
        else:
            status = "ok" if listed == [hub] else "differs"
        rows.append({"chain_id": chain, "hub": hub, "table": listed, "status": status})
    return rows


def csv_row(row, date):
    """A bridges.csv line for the hub's SpokePool of ``row``."""
    chain = row["chain_id"]
    return (
        f'{chain},{row["hub"]},{ACROSS},"SpokePool (crossChainContracts({chain}) on {date})",'
        f"contract,{HUB_URL}"
    )


def _registry_chains():
    from tornado_demix.networks import load_networks

    return sorted({net.chain_id for net in load_networks().values()})


def main(argv=None, rpc=None, table=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chains", default="", help="comma-separated chain ids (default: registry)")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="Ethereum JSON-RPC endpoint")
    args = ap.parse_args(argv)
    if rpc is None:
        from tornado_demix.rpc import RpcClient

        rpc = RpcClient(args.rpc)
    table = load_bridges() if table is None else table
    chains = [int(c) for c in args.chains.split(",") if c.strip()] or _registry_chains()
    date = datetime.date.today().isoformat()
    rows = compare(rpc, table, chains)
    for r in rows:
        listed = ", ".join(r["table"]) or "-"
        print(f"{r['chain_id']:>6}  {r['status']:<24}  hub {r['hub'] or '-'}  table {listed}")
        if r["status"] in ("missing from the table", "differs") and r["hub"]:
            print("        " + csv_row(r, date))
    return 0 if all(r["status"] in ("ok", "not on the hub") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
