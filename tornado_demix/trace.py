"""Multi-hop tracing of withdrawn funds (after MixGuard, Algorithm 1).

Starting from an exit address and the amount it received, the funds are
followed forward hop by hop. Each address's outgoing transfers after the funds
arrived are attributed first in, first out until the amount is used up. A call
into a contract that pays the sender back in the same transaction (a DEX swap)
continues with the swap output. The walk stops at a labelled service, at the
hop limit, at a contract that pays nothing back, at an address with no further
outgoing transfers, or when the attributable amount is spent.

A deposit into a bridge the package can decode (Across) is followed to the
destination chain by its on-chain deposit id: the walk continues from the fill
recipient with the amount the fill paid out, and the hop count carries on. When
no fill is found the trace stops there ("bridge, fill not found").

FIFO attribution is a convention, not a fact: funds in one account are
fungible, so every edge is a lead to corroborate. Next to the FIFO amount each
edge carries ``lower`` and ``upper``: how much of the transfer can be the traced
funds under ANY ordering convention, from the sender's own history of that asset
(see allocation.py). lower > 0 means "certainly at least this much"; lower 0
means the transfer is only possible.
"""

from __future__ import annotations

from collections import deque

from . import allocation as _allocation
from . import bridges as _bridges
from .errors import ApiKeyError, TornadoDemixError
from .etherscan import MAX_PAGES as _MAX_PAGES
from .etherscan import PAGE_SIZE as _PAGE_SIZE

_HISTORY_CAP = _MAX_PAGES * _PAGE_SIZE  # a fetch this long may have been cut off
MAX_HOPS = 4
MAX_NODES = 40  # addresses expanded per trace; bounds the API calls
DUST_FRACTION = 0.01  # amounts below this share of the start are not followed


def _value(row, token):
    decimals = int(row.get("tokenDecimal") or 18) if token else 18
    return int(row.get("value") or 0) / 10**decimals


def _outgoing(client, addr, token, block, rows=None):
    """Outgoing transfers of one asset from ``addr`` at or after ``block``, oldest first.

    ``rows`` are the explorer rows already fetched for this address and asset.
    """
    if rows is None:
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
        self.internal_cache = {}

    def internal(self, addr):
        """Internal transactions touching ``addr``, fetched once."""
        if addr not in self.internal_cache:
            self.internal_cache[addr] = self.client.internal_txs(addr)
        return self.internal_cache[addr]

    def swap_output(self, addr, tx_hash, spent_token):
        if addr not in self.cache:
            internal = self.internal(addr)
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


def _order(row):
    try:
        return int(row["transactionIndex"])
    except (KeyError, TypeError, ValueError):
        return None


def _history(ctx, addr, token, rows):
    """Chain-ordered in/out events of one asset on ``addr`` (see allocation.bounds).

    ``rows`` are the rows already fetched for the asset: token transfers touching
    the address, or its normal transactions. A native asset adds the internal
    transactions (one cached call). Returns (events, complete).
    """
    complete = len(rows) < _HISTORY_CAP
    sources = [(r, True) for r in rows]
    if not token:
        try:
            sources += [(r, False) for r in ctx["incoming"].internal(addr)]
        except ApiKeyError:
            raise
        except TornadoDemixError:
            complete = False
    events = []
    for row, normal in sources:
        if row.get("isError") == "1" or not row.get("to"):
            continue
        value = _value(row, token)
        if value <= 0:
            continue
        sender, receiver = (row.get("from") or "").lower(), row["to"].lower()
        base = {
            "hash": (row.get("hash") or "").lower(),
            "block": int(row.get("blockNumber") or 0),
            "order": _order(row) if normal else None,
            "value": value,
        }
        if receiver == addr:
            events.append({**base, "kind": "in", "from": sender})
        if sender == addr:
            events.append({**base, "kind": "out", "to": receiver})
    return events, complete


def _find_arrival(events, amount, block):
    """The inflow a start address's traced amount came with (no hash is known).

    Returns (tx hash or None, "exact" | "ambiguous" | "synthetic"). Exactly one
    transaction at or after ``block`` carrying an inflow of the amount is "exact";
    several (equal Tornado denominations) is "ambiguous" and none is "synthetic",
    both without a hash: the bounds then place the amount at the start block, which
    only widens them.
    """
    hashes = {
        e["hash"]
        for e in events
        if e["kind"] == "in" and e["block"] >= block and abs(e["value"] - amount) <= 1e-6 * amount
    }
    if len(hashes) == 1:
        return next(iter(hashes)), "exact"
    return None, "ambiguous" if hashes else "synthetic"


def arrival_text(result: dict) -> str:
    """One line on how the start address's arrival was fixed ("" for an old result)."""
    how = result.get("arrival_matched")
    if not how:
        return ""
    if how == "exact":
        return f"arrival transaction {result.get('arrival_tx')} (bounds measured from it)"
    if how == "ambiguous":
        return (
            "arrival not identified: several inflows match the amount, so the bounds "
            "place it at the start block (widest); give the arrival transaction to narrow them"
        )
    if how == "explicit_not_found":
        return (
            "the given arrival transaction is not in this address's history; "
            "bounds use the widest case"
        )
    return (
        "arrival not found in the history: the bounds place the amount at the start "
        "block (widest); give the arrival transaction to narrow them"
    )


def share_text(edge: dict, dash: str = "–") -> str:
    """``FIFO 3 [1 – 3]`` for an edge: the FIFO amount and its bounds under any ordering.

    Empty bounds (an edge from an older result) give just the FIFO amount.
    ``?`` marks an incomplete history, where the bounds rest on missing data.
    """
    text = f"FIFO {edge['attributed']}"
    if edge.get("lower") is not None and edge.get("upper") is not None:
        text += f" [{edge['lower']} {dash} {edge['upper']}]"
        if edge.get("history_incomplete"):
            text += " ?"
    return text


def share_status(edge: dict) -> str:
    """ "certain at least <lower>", "possible", "bounds unavailable", or "" (no bounds field)."""
    if edge.get("bounds_available") is False:
        return "bounds unavailable"
    if edge.get("lower") is None:
        return ""
    return f"certain at least {edge['lower']}" if edge["lower"] > 0 else "possible"


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
    chain_id: int | None = None,
    destinations=None,
    bridge_table: dict | None = None,
    arrival_tx: str | None = None,
) -> dict:
    """Follow ``amount`` of one asset forward from ``start``.

    ``token`` is an ERC-20 contract address, or None for the native currency.
    Returns {start, amount, asset, edges, terminals, nodes_expanded}. An edge is
    {hop, chain_id, from, to, tx_hash, ts, asset, value, attributed, lower, upper,
    history_incomplete, kind}; ``attributed`` is the FIFO amount, ``lower``/``upper`` the
    bounds under any ordering (allocation.py);
    ``kind`` is transfer, swap or bridge (a bridge edge also carries ``bridge``:
    protocol, destination chain, deposit id, fill transaction). A terminal is
    {address, chain_id, asset, amount, hop, reason, label}.

    Bridge continuation is on when ``chain_id`` (the chain of ``client``) and
    ``destinations`` are given. ``destinations`` is a callable (or dict) from a
    chain id to {"client", "labels", "currency", "is_contract"} or None for a
    chain that cannot be read.

    ``arrival_tx`` is the transaction that brought ``amount`` to ``start``; given, it
    overrides the amount match. The result carries ``arrival_tx`` and
    ``arrival_matched`` ("exact", "ambiguous", "synthetic" or "explicit_not_found"). An edge has
    ``bounds_available`` False (and no lower/upper) when its allocation row is
    missing or FIFO falls outside it, e.g. the transfer precedes the arrival.
    """
    labels = labels or {}
    start = start.lower()
    token = token.lower() if token else None
    dust = amount * DUST_FRACTION
    dest_for = destinations.get if isinstance(destinations, dict) else destinations
    follow = chain_id is not None and dest_for is not None
    table = (
        (bridge_table if bridge_table is not None else _bridges.load_bridges()) if follow else {}
    )
    contexts = {
        chain_id: {
            "client": client,
            "labels": labels,
            "currency": currency,
            "is_contract": is_contract,
            "incoming": _Incoming(client),
        }
    }

    def context(chain):
        if chain not in contexts:
            dest = dest_for(chain) if dest_for else None
            if dest is None:
                contexts[chain] = None
            else:
                ctx = dict(dest, incoming=_Incoming(dest["client"]))
                ctx.setdefault("labels", {})
                ctx.setdefault("currency", "ETH")
                ctx.setdefault("is_contract", None)
                contexts[chain] = ctx
        return contexts[chain]

    names = {(chain_id, None): currency}
    if token:
        names[(chain_id, token)] = token
    first = (arrival_tx or "").lower() or None
    queue = deque([(start, token, amount, start_block, 0, chain_id, first)])
    arrival = {"tx": None, "matched": "synthetic"}
    edges, terminals, expanded = [], [], 0

    def name(chain, asset):
        if asset is None and (chain, None) not in names:
            ctx = context(chain)
            names[(chain, None)] = ctx["currency"] if ctx else "native"
        return names.get((chain, asset), asset)

    def stop(addr, asset, amt, hop, reason, chain):
        ctx = contexts.get(chain)
        terminals.append(
            {
                "address": addr,
                "asset": name(chain, asset),
                "amount": round(amt, 6),
                "hop": hop,
                "reason": reason,
                "label": ctx["labels"].get(addr) if ctx else None,
            }
        )
        if chain is not None:
            terminals[-1]["chain_id"] = chain

    def cross_bridge(edge, out, addr, asset, used, chain, hop):
        """Continue across an Across deposit. True when the edge was handled."""
        deposits = _bridges.decode_deposits(
            contexts[chain]["client"], out["hash"], chain, table, ts=out["ts"]
        )
        source_token = asset or _bridges.WRAPPED_NATIVE.get(chain)
        mine = [d for d in deposits if d["depositor"] == addr and d["input_token"] == source_token]
        if not mine:
            row = table.get((chain, out["to"])) or {}
            reason = f"bridge {row.get('protocol', '')}, deposit event not decoded"
            if "v2" in row.get("role", "").lower():
                reason += " (v2 not decoded)"
            stop(out["to"], asset, used, hop + 1, reason, chain)
            return True
        deposit = mine[0]
        dest_chain = deposit["dest_chain_id"]
        dest = context(dest_chain)
        info = {
            "protocol": deposit["protocol"],
            "dest_chain_id": dest_chain,
            "deposit_id": deposit["deposit_id"],
            "recipient": deposit["recipient"],
        }
        edge["kind"] = "bridge"
        edge["bridge"] = info
        if dest is None:
            reason = f"bridge, destination chain {dest_chain} not supported"
            stop(out["to"], asset, used, hop + 1, reason, chain)
            return True
        try:
            fill = _bridges.find_fill(dest["client"], deposit, table)
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            why = str(exc).splitlines()[0][:80] if str(exc) else type(exc).__name__
            stop(out["to"], asset, used, hop + 1, f"bridge, fill lookup failed: {why}", chain)
            return True
        if fill is None:
            stop(out["to"], asset, used, hop + 1, "bridge, fill not found", chain)
            return True
        info.update(
            fill_tx=fill["tx_hash"],
            fill_block=fill["block"],
            fill_recipient=fill["recipient"],
            output_token=fill["output_token"],
            sped_up=fill["sped_up"],
        )
        # the share of the deposit this trace owns, after the bridge fee
        owned = (
            used * fill["output_amount"] / deposit["input_amount"]
            if deposit["input_amount"]
            else used
        )
        wrapped = _bridges.WRAPPED_NATIVE.get(dest_chain)
        paid_native = wrapped == fill["output_token"] and dest_chain not in _bridges.TOKEN_WRAPPED
        new_asset = None if paid_native else fill["output_token"]
        queue.append(
            (
                fill["recipient"],
                new_asset,
                owned,
                fill["block"],
                hop + 1,
                dest_chain,
                fill["tx_hash"],
            )
        )
        return True

    while queue:
        addr, asset, amt, block, hop, chain, arrived_tx = queue.popleft()
        ctx = context(chain)
        if amt < dust:
            continue
        if hop and ctx["labels"].get(addr):
            stop(addr, asset, amt, hop, "labelled address", chain)
            continue
        if hop >= max_hops:
            stop(addr, asset, amt, hop, "hop limit", chain)
            continue
        if expanded >= max_nodes:
            stop(addr, asset, amt, hop, "trace budget", chain)
            continue
        expanded += 1
        remaining = amt
        rows = (
            ctx["client"].token_transfers(addr, contract=asset)
            if asset
            else ctx["client"].outgoing_txs(addr)
        )
        events, complete = _history(ctx, addr, asset, rows)
        if hop == 0 and chain == chain_id:
            if arrived_tx is not None:
                known = any(e["kind"] == "in" and e["hash"] == arrived_tx for e in events)
                arrived_tx = arrived_tx if known else None
                arrival.update(tx=arrived_tx, matched="exact" if known else "explicit_not_found")
            else:
                arrived_tx, how = _find_arrival(events, amt, block)
                arrival.update(tx=arrived_tx, matched=how)
        alloc = _allocation.bounds(events, arrived_tx, traced_amount=amt, start_block=block)
        shares = {}
        for r in alloc["outflows"]:
            shares.setdefault((r["hash"], r["to"], round(r["value"], 9)), []).append(r)
        incomplete = alloc["history_incomplete"] or not complete
        for out in _outgoing(ctx["client"], addr, asset, block, rows):
            if remaining < dust:
                break
            used = min(out["value"], remaining)
            remaining -= used
            if asset and out["symbol"]:
                names.setdefault((chain, asset), out["symbol"])
            edge = {
                "hop": hop + 1,
                "from": addr,
                "to": out["to"],
                "tx_hash": out["hash"],
                "ts": out["ts"],
                "asset": name(chain, asset),
                "value": round(out["value"], 6),
                "attributed": round(used, 6),
                "kind": "transfer",
                "history_incomplete": incomplete,
                "bounds_available": False,
            }
            pending = shares.get((out["hash"], out["to"], round(out["value"], 9)))
            if pending:
                share = pending.pop(0)
                low, high = round(share["lower"], 6), round(share["upper"], 6)
                if low - 1e-6 <= edge["attributed"] <= high + 1e-6:
                    edge.update(lower=low, upper=high, bounds_available=True)
            if chain is not None:
                edge["chain_id"] = chain
            edges.append(edge)
            if follow and _bridges.is_bridge(table, chain, out["to"]):
                if cross_bridge(edge, out, addr, asset, used, chain, hop):
                    continue
            if ctx["labels"].get(out["to"]) or not _looks_like_contract(
                out, asset, ctx["is_contract"]
            ):
                queue.append((out["to"], asset, used, out["block"], hop + 1, chain, out["hash"]))
                continue
            swap = ctx["incoming"].swap_output(addr, out["hash"], asset)
            if swap is None:
                stop(out["to"], asset, used, hop + 1, "contract without a payment back", chain)
                continue
            new_asset, received, symbol = swap
            if new_asset and symbol:
                names.setdefault((chain, new_asset), symbol)
            edge["kind"] = "swap"
            edge["swapped_to"] = name(chain, new_asset)
            # The swap output is scaled by the share of the input this trace owns.
            owned = received * used / out["value"]
            queue.append((addr, new_asset, owned, out["block"] + 1, hop + 1, chain, out["hash"]))
        if remaining >= dust:
            stop(addr, asset, remaining, hop, "not moved on (held or untraced)", chain)

    return {
        "start": start,
        "amount": amount,
        "asset": name(chain_id, token),
        "edges": edges,
        "terminals": terminals,
        "nodes_expanded": expanded,
        "arrival_tx": arrival["tx"],
        "arrival_matched": arrival["matched"],
    }
