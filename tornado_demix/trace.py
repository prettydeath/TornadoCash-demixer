"""Multi-hop tracing of withdrawn funds (after MixGuard, Algorithm 1).

Starting from an exit address and the amount it received, the funds are
followed forward hop by hop. Each address's outgoing transfers after the funds
arrived are attributed first in, first out until the amount is used up. A call
into a contract that pays the sender back in the same transaction (a DEX swap)
continues with the swap output. The walk stops at a labelled service, at the
hop limit, at a contract that pays nothing back, at an address with no further
outgoing transfers, or when the attributable amount is spent.

FIFO attribution is a convention, not a fact: funds in one account are
fungible, so every edge is a lead to corroborate.
"""

from __future__ import annotations

from collections import deque

MAX_HOPS = 4
MAX_NODES = 40  # addresses expanded per trace; bounds the API calls
DUST_FRACTION = 0.01  # amounts below this share of the start are not followed


def _value(row, token):
    decimals = int(row.get("tokenDecimal") or 18) if token else 18
    return int(row.get("value") or 0) / 10**decimals


def _outgoing(client, addr, token, block):
    """Outgoing transfers of one asset from ``addr`` at or after ``block``, oldest first."""
    rows = client.token_transfers(addr, contract=token) if token else client.outgoing_txs(addr)
    out = []
    for row in rows:
        if (row.get("from") or "").lower() != addr or row.get("isError") == "1":
            continue
        if not row.get("to") or int(row.get("blockNumber") or 0) < block:
            continue
        value = _value(row, token)
        if value <= 0:
            continue
        out.append(
            {
                "to": row["to"].lower(),
                "hash": (row.get("hash") or "").lower(),
                "block": int(row.get("blockNumber") or 0),
                "ts": int(row.get("timeStamp") or 0),
                "value": value,
                "input": row.get("input") or "",
                "symbol": row.get("tokenSymbol") or "",
            }
        )
    out.sort(key=lambda r: (r["block"], r["ts"], r["hash"]))
    return out


class _Incoming:
    """Same-transaction payments back to an address, read once per address."""

    def __init__(self, client):
        self.client = client
        self.cache = {}

    def swap_output(self, addr, tx_hash, spent_token):
        if addr not in self.cache:
            internal = self.client.internal_txs(addr)
            tokens = self.client.token_transfers(addr)
            self.cache[addr] = [(r, None) for r in internal] + [
                (r, (r.get("contractAddress") or "").lower()) for r in tokens
            ]
        for row, token in self.cache[addr]:
            if (row.get("hash") or "").lower() != tx_hash:
                continue
            if (row.get("to") or "").lower() != addr or token == spent_token:
                continue
            value = _value(row, token)
            if value > 0:
                return token, value, row.get("tokenSymbol") or ""
        return None


def _looks_like_contract(edge, token, is_contract):
    known = is_contract(edge["to"]) if is_contract else None
    if known is not None:
        return known
    # Without an endpoint: a native transfer carrying call data is a contract
    # call; a token transfer is checked for a same-transaction payment back.
    return bool(token) or edge["input"] not in ("", "0x")


def trace_funds(
    client,
    start: str,
    amount: float,
    start_block: int = 0,
    token: str | None = None,
    currency: str = "ETH",
    max_hops: int = MAX_HOPS,
    max_nodes: int = MAX_NODES,
    labels: dict[str, dict] | None = None,
    is_contract=None,
) -> dict:
    """Follow ``amount`` of one asset forward from ``start``.

    ``token`` is an ERC-20 contract address, or None for the native currency.
    Returns {start, amount, asset, edges, terminals, nodes_expanded}. An edge is
    {hop, from, to, tx_hash, ts, asset, value, attributed, kind}; ``kind`` is
    transfer or swap. A terminal is {address, asset, amount, hop, reason, label}.
    """
    labels = labels or {}
    start = start.lower()
    token = token.lower() if token else None
    dust = amount * DUST_FRACTION
    incoming = _Incoming(client)
    names = {None: currency}
    if token:
        names[token] = token
    queue = deque([(start, token, amount, start_block, 0)])
    edges, terminals, expanded = [], [], 0

    def stop(addr, asset, amt, hop, reason):
        terminals.append(
            {
                "address": addr,
                "asset": names.get(asset, asset),
                "amount": round(amt, 6),
                "hop": hop,
                "reason": reason,
                "label": labels.get(addr),
            }
        )

    while queue:
        addr, asset, amt, block, hop = queue.popleft()
        if amt < dust:
            continue
        if hop and labels.get(addr):
            stop(addr, asset, amt, hop, "labelled address")
            continue
        if hop >= max_hops:
            stop(addr, asset, amt, hop, "hop limit")
            continue
        if expanded >= max_nodes:
            stop(addr, asset, amt, hop, "trace budget")
            continue
        expanded += 1
        remaining = amt
        for out in _outgoing(client, addr, asset, block):
            if remaining < dust:
                break
            used = min(out["value"], remaining)
            remaining -= used
            if asset and out["symbol"]:
                names.setdefault(asset, out["symbol"])
            edge = {
                "hop": hop + 1,
                "from": addr,
                "to": out["to"],
                "tx_hash": out["hash"],
                "ts": out["ts"],
                "asset": names.get(asset, asset),
                "value": round(out["value"], 6),
                "attributed": round(used, 6),
                "kind": "transfer",
            }
            edges.append(edge)
            if labels.get(out["to"]) or not _looks_like_contract(out, asset, is_contract):
                queue.append((out["to"], asset, used, out["block"], hop + 1))
                continue
            swap = incoming.swap_output(addr, out["hash"], asset)
            if swap is None:
                stop(out["to"], asset, used, hop + 1, "contract without a payment back")
                continue
            new_asset, received, symbol = swap
            if new_asset and symbol:
                names.setdefault(new_asset, symbol)
            edge["kind"] = "swap"
            edge["swapped_to"] = names.get(new_asset, new_asset)
            # The swap output is scaled by the share of the input this trace owns.
            owned = received * used / out["value"]
            queue.append((addr, new_asset, owned, out["block"] + 1, hop + 1))
        if remaining >= dust:
            stop(addr, asset, remaining, hop, "not moved on (held or untraced)")

    return {
        "start": start,
        "amount": amount,
        "asset": names.get(token, token),
        "edges": edges,
        "terminals": terminals,
        "nodes_expanded": expanded,
    }
