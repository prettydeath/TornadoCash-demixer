"""Multi-hop tracing of withdrawn funds (after MixGuard, Algorithm 1).

Starting from an exit address and the amount it received, the funds are
followed forward hop by hop. Every (chain, address, asset) the funds reach has
ONE ledger: all traced inflows into it are merged, each tagged with the branch
it came by, and its events are replayed once in chain order. Each outgoing
transfer takes the traced funds first in, first out, starting at the first
traced arrival; a transfer before that carries none. The traced share of a
transfer is split back across the incoming branches by the lots FIFO consumed
(``sources``), so a transfer reached by several branches is one edge with the
merged amount, never one edge per branch.

A call into a contract that pays the sender back in the same transaction (a DEX
swap) continues with the swap output. The walk stops at a labelled service, at
the hop limit, at a contract that pays nothing back, at an address that did not
move the funds on, or when the address budget is spent.

Every transfer to an address of the bridge table is a bridge hop, checked before
the swap rule. An Across deposit is found in the receipt of the transaction (also
when it went through a periphery, router or aggregator, or to an unlisted contract
that paid nothing back) and followed to the destination chain by its on-chain
deposit id: the walk continues from the fill recipient with the amount the fill
paid out, and the hop count carries on. When it is not followed the branch ends
with a distinct state (fill_not_found, deposit_expired_possible_refund,
destination_chain_not_supported, destination_pool_not_listed, lookup_failed); a
listed bridge without a decoder ends as "bridge <protocol> (not followed)". An
Arbitrum Inbox message (retryable ticket or ETH deposit) tied to the transfer is
followed on Arbitrum from its L2 recipient once its L2 credit is found ("bridge
arbitrum, l2 credit not found" otherwise); a ZKsync Bridgehub request is decoded and
ends as "bridge zksync (recipient <address>, chain not supported)". See
bridges_canonical.py.

Order of work. Histories are fetched breadth first: the start address, then
every address the current replay sends traced funds to, a whole level at a time,
in a fixed order (hop, time of the first arrival, address). After each level the
replay runs again over all fetched histories, merged in time order (timestamp,
then block and transaction index; chains interleave by timestamp, which keeps a
bridge fill after its deposit). An address reached again by a later branch is
therefore simply replayed with the merged ledger; nothing is attributed twice.

The next address is credited with the FIFO amount for FIFO and with the edge's
bounds [lower, upper] for its own bounds, so a hop is certain only when the
edges before it are. An internal transfer without a transaction index of its own
borrows one from any fetched row of the same transaction; when none exists its
order inside the block is unknown: FIFO does not let an outflow take a lot not
known to have arrived before it, and the address is marked incomplete.

FIFO attribution is a convention, not a fact: funds in one account are
fungible, so every edge is a lead to corroborate. Next to the FIFO amount each
edge carries ``lower`` and ``upper``: how much of the transfer can be the traced
funds under ANY ordering convention, from the sender's own history of that asset
(see allocation.py), from the same computation as the FIFO amount. lower > 0
means "certainly at least this much" unless the history is incomplete; lower 0
means the transfer is only possible.
"""

from __future__ import annotations

import heapq
import math

from . import allocation as _allocation
from . import bridges as _bridges
from . import bridges_canonical as _canonical
from .errors import ApiKeyError, TornadoDemixError
from .etherscan import MAX_PAGES as _MAX_PAGES
from .etherscan import PAGE_SIZE as _PAGE_SIZE
from .incomplete import attach, lookup_failed, mark, truncated_since

# a fetch this long may have been cut off (only for clients without ``truncated``)
_HISTORY_CAP = _MAX_PAGES * _PAGE_SIZE
MAX_HOPS = 4
MAX_NODES = 40  # addresses expanded per trace; bounds the API calls
DUST_FRACTION = 0.01  # amounts below this share of the start are not followed
NOISE_FRACTION = 1e-9  # end amounts below this share of the start are rounding noise

# End states of a branch (terminal ``reason``) added for failed lookups and small or
# re-mixed amounts; the others are written out where they are recorded.
LOOKUP_FAILED = "lookup_failed"  # a history (or swap / bridge lookup) could not be read
BELOW_THRESHOLD = "below_threshold"  # under DUST_FRACTION of the start: not followed
TORNADO_REDEPOSIT = "tornado_redeposit"  # sent into a Tornado pool or router again
# an Across deposit in the transaction that is not this transfer (see _tie)
NOT_TIED = "bridge across (deposit not tied to this transfer)"
TIE_FEE = 0.10  # most a router may keep of a transfer before it deposits the rest
END_STATES_NOTE = (
    "lookup_failed: a history (or a swap or bridge lookup) could not be read, so the "
    "amount stops there and the other branches go on; below_threshold: under "
    f"{DUST_FRACTION:.0%} of the start amount, not followed; tornado_redeposit: sent "
    "into a Tornado pool again, whatever the amount. The end amounts add up to the "
    "start amount unless a swap or bridge changed the asset."
)
BRIDGE_STATES_NOTE = (
    "Bridges: a transfer to a listed bridge whose deposits are not decoded ends as "
    "'bridge <protocol> (not followed)'; at a listed aggregator a payment back in the same "
    "transaction makes it a swap instead, unless a bridge contract logged an event in it. An Across deposit (direct, or made through a periphery, router or "
    "aggregator in the same transaction) is followed to its fill when it is tied to the "
    "transfer by amount (equal for a direct deposit; through a contract at most "
    f"{TIE_FEE:.0%} kept as a fee) and made in the traced address's name (or in the name "
    "of the contract it paid); otherwise the branch ends as 'bridge across (deposit not "
    "tied to this transfer)'. A tied deposit whose fill is not followed ends as "
    "fill_not_found (no fill within the search window), "
    "deposit_expired_possible_refund (no fill and the fill deadline has passed: Across "
    "refunds such a deposit, so the funds may be back with the depositor), "
    "destination_chain_not_supported (the destination chain is not in the networks "
    "registry), destination_pool_not_listed (no SpokePool listed for that chain) or "
    "lookup_failed (the receipt or the fill could not be read). An Arbitrum Inbox deposit "
    "(retryable ticket or depositEth) of the traced amount, sent by the traced address, "
    "is followed on Arbitrum from its L2 recipient once the L2 credit is found (a "
    "transaction from the aliased L1 sender with the credited value); otherwise it ends "
    "as 'bridge arbitrum, l2 credit not found', with the recipient shown. A ZKsync "
    "Bridgehub request ends as 'bridge zksync (recipient <address>, chain not "
    "supported)'; its refund recipient gets unused fees only and is shown separately."
)
END_STATES_NOTE += " " + BRIDGE_STATES_NOTE

_AFTER = 10**12
_NATIVE_ACTIONS = ("txlist", "txlistinternal")
_TOKEN_ACTIONS = ("tokentx",)


def _raw(row):
    """The row's value in the asset's smallest unit (0 when unreadable)."""
    try:
        return int(row.get("value") or 0)
    except (TypeError, ValueError):
        return 0


def _value(row, token):
    decimals = int(row.get("tokenDecimal") or 18) if token else 18
    return int(row.get("value") or 0) / 10**decimals


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


def _truncated(client, addr, token):
    """True/False from the client's record of cut-off lists, None if it keeps none."""
    marks = getattr(client, "truncated", None)
    if not isinstance(marks, list):
        return None
    actions = _TOKEN_ACTIONS if token else _NATIVE_ACTIONS
    return any(
        (m.get("address") or "").lower() == addr and m.get("action") in actions for m in marks
    )


def _history(ctx, addr, token, rows, failed=None):
    """Chain-ordered in/out events of one asset on ``addr`` (see allocation.bounds).

    ``rows`` are the rows already fetched for the asset: token transfers touching
    the address, or its normal transactions. A native asset adds the internal
    transactions (one cached call). Returns (events, complete, index); ``complete``
    is False when the explorer client recorded one of the lists as cut off (or, for
    a client that keeps no such record, when a list reached the page cap) or the
    internal transactions could not be read. ``index`` maps every transaction hash
    seen in an indexed row (zero-value and failed rows included, such as the
    zero-value call that makes a contract wallet pay) to its transaction index.
    ``failed(what, exc)`` is called when the internal transactions cannot be read.
    """
    complete = True
    sources = [(r, True) for r in rows]
    if not token:
        try:
            sources += [(r, False) for r in ctx["incoming"].internal(addr)]
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            complete = False
            if failed is not None:
                failed("internal transactions", exc)
    cut = _truncated(ctx["client"], addr, token)
    if cut is None:
        cut = len(rows) >= _HISTORY_CAP
    complete = complete and not cut
    events, index = [], {}
    for row, normal in sources:
        if normal and _order(row) is not None:
            index.setdefault((row.get("hash") or "").lower(), _order(row))
        if row.get("isError") == "1" or not row.get("to"):
            continue
        value = _value(row, token)
        if value <= 0:
            continue
        sender, receiver = (row.get("from") or "").lower(), row["to"].lower()
        base = {
            "hash": (row.get("hash") or "").lower(),
            "block": int(row.get("blockNumber") or 0),
            "ts": int(row.get("timeStamp") or 0),
            "order": _order(row) if normal else None,
            "value": value,
            "raw": _raw(row),
            "input": row.get("input") or "",
            "symbol": row.get("tokenSymbol") or "",
        }
        if receiver == addr:
            events.append({**base, "kind": "in", "from": sender})
        if sender == addr:
            events.append({**base, "kind": "out", "to": receiver})
    return events, complete, index


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
    if how == LOOKUP_FAILED:
        return "the start address's history could not be read: nothing was traced"
    if how == "explicit_not_found":
        return (
            "the given arrival transaction is not in this address's history; "
            "bounds use the widest case"
        )
    return (
        "arrival not found in the history: the bounds place the amount at the start "
        "block (widest); give the arrival transaction to narrow them"
    )


def _recipient_parts(info: dict, addresses: bool = True) -> list[str]:
    """The L2 recipient and refund recipient of a canonical bridge hop, as text.

    ``addresses`` False leaves out the parts that carry an address (a renderer that
    links them, see bridges_canonical.recipient_links).
    """
    parts = []
    if "l2_recipient" in info:
        if info["l2_recipient"]:
            if addresses:
                parts.append(f"L2 recipient {info['l2_recipient']}")
        else:
            parts.append("L2 recipient not decoded")
    if "refund_recipient" in info:
        refund = info["refund_recipient"]
        if not refund or int(refund, 16) == 0:
            parts.append("refund recipient: the sender (zero in the call)")
        elif addresses:
            parts.append(f"refund recipient {refund} (unused fees only)")
    return parts


def recipient_text(info: dict) -> str:
    """The L2 recipient (and refund recipient) of a bridge hop as one line, or ""."""
    return ", ".join(_recipient_parts(info))


def bridge_tx_label(info: dict) -> str:
    """Name of the destination transaction: "L2 tx" for a canonical credit, else "fill"."""
    return "L2 tx" if info.get("state") == _canonical.CREDITED else "fill"


def bridge_text(
    info: dict,
    chain_name=None,
    arrow: str = "→",
    fill: bool = True,
    state: bool = True,
    recipients: bool = True,
) -> str:
    """One line for a bridge hop: protocol, destination chain, deposit id, way in, state.

    ``chain_name`` maps a chain id to a name (default "chain <id>"). ``fill`` False
    leaves out the fill (or L2) transaction (a renderer that links it); ``state`` False
    leaves out the state (shown next to it as the end reason); ``recipients`` False
    leaves out the L2 and refund recipient addresses (a renderer that links them).
    """
    name = chain_name or (lambda chain: f"chain {chain}")
    head = f"bridge {info.get('protocol') or 'unknown'}"
    if info.get("dest_chain_id") is not None:
        head += f" {arrow} {name(info['dest_chain_id'])}"
    parts = []
    if info.get("deposit_id") is not None:
        parts.append(f"deposit id {info['deposit_id']}")
    if info.get("message_num") is not None:
        parts.append(f"message {info['message_num']}")
    if info.get("via"):
        parts.append(f"via {info['via']}")
    parts += _recipient_parts(info, addresses=recipients)
    state = info.get("state") if state else None
    if state in (_bridges.FILLED, _canonical.CREDITED):
        if fill and info.get("fill_tx"):
            parts.append(f"{bridge_tx_label(info)} {info['fill_tx']}")
    elif state == _bridges.NOT_FOLLOWED:
        parts.append("not followed")
    elif state:
        parts.append(state)
    return head + (", " + ", ".join(parts) if parts else "")


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
    """How sure the edge is.

    "certain at least <lower>", "possible", "possible (history incomplete)",
    "bounds unavailable", or "" (no bounds field). An incomplete history is never
    called certain: the lower bound rests on data the explorer did not return.
    """
    if edge.get("bounds_available") is False:
        return "bounds unavailable"
    if edge.get("lower") is None:
        return ""
    if edge.get("history_incomplete"):
        return "possible (history incomplete)"
    return f"certain at least {edge['lower']}" if edge["lower"] > 0 else "possible"


def _bridge_info(protocol, dest_chain, state):
    return {"protocol": protocol, "dest_chain_id": dest_chain, "state": state}


def _not_followed(listed):
    """(info, reason) at a listed bridge whose deposits are not decoded or not followed."""
    info = _bridge_info(listed["protocol"], None, _bridges.NOT_FOLLOWED)
    info["role"] = listed.get("role", "")
    return info, f"bridge {listed['protocol']} (not followed)"


def _tie(deposits, addr, token, ev, consumed):
    """The Across deposit that IS the transfer ``ev`` of ``addr``: (deposit or None, untied).

    Same input token as the traced asset, not taken by an earlier transfer of this
    replay (``consumed``: (tx hash, log index)), and tied by amount:

    * a transfer straight to the SpokePool: the depositor is ``addr`` and the deposit's
      input amount equals the transfer;
    * through a periphery, router, aggregator or unlisted contract: the input amount is
      at most the transfer and at least (1 - TIE_FEE) of it (the contract may keep a
      fee first; the stored fixtures show 1.0 % for LI.FI and 0 % for the periphery),
      and the depositor is ``addr`` or, when ``addr`` only sent the transaction, the
      contract the transfer went to (it deposited in its own name, funded by ``addr``).
      A deposit made in the name of a third address is never taken: the sender may be
      a relayer or batcher for someone else.

    ``untied`` is True when the transaction holds a deposit of this token by ``addr``
    (depositor or sender) that is not tied to this transfer.
    """
    raw, to = ev.get("raw") or 0, ev["to"]
    mine = [
        d
        for d in deposits
        if d["input_token"] == token and addr in (d["depositor"], d.get("tx_from"))
    ]

    def tied(d):
        if (d["tx_hash"], d["log_index"]) in consumed:
            return False
        if to == d["spoke"]:
            return d["depositor"] == addr and d["input_amount"] == raw
        if not 0 < d["input_amount"] <= raw or raw - d["input_amount"] > TIE_FEE * raw:
            return False
        return d["depositor"] in (addr, to)

    ok = [d for d in mine if tied(d)]
    if ok:
        return min(ok, key=lambda d: raw - d["input_amount"]), False
    return None, bool(mine)


def _looks_like_contract(edge, token, is_contract):
    known = is_contract(edge["to"]) if is_contract else None
    if known is not None:
        return known
    # Without an endpoint: a native transfer carrying call data is a contract
    # call; a token transfer is checked for a same-transaction payment back.
    return bool(token) or edge["input"] not in ("", "0x")


def _position(chain, ev, order, ts):
    """Time-order key of one event: timestamp, chain, block, index, inflow first, hash.

    ``ts`` never decreases with the block inside one chain (see ``order_all``), so
    inside a chain this is the order of allocation.sort_key; chains interleave by
    timestamp.
    """
    if order is None:
        order = -1 if ev["kind"] == "in" else _AFTER
    kind = 0 if ev["kind"] == "in" else 1
    return (ts, chain if chain is not None else -1, ev["block"], order, kind, ev["hash"])


def _lots_by_hop(row, scale=1.0):
    """{hop of the next address: [fifo, lo, hi]} from the lots an outflow took.

    The FIFO amount follows the lots; the edge's bounds [lower, upper] are shared
    out in proportion, so the next address is credited with the interval the
    traced share is known to lie in, not with the FIFO amount alone.
    """
    out = {}
    fifo = row["fifo"]
    for (hop, _tx, _frm), amount in row["taken"]:
        part = amount / fifo if fifo else 0.0
        lot = out.setdefault(hop + 1, [0.0, 0.0, 0.0])
        lot[0] += amount * scale
        lot[1] += row["lower"] * part * scale
        lot[2] += row["upper"] * part * scale
    return out


def _credited(credits, block):
    """Most traced funds credited to inflows at or before ``block`` (one ledger)."""
    return sum(hi for b, hi in (credits or {}).items() if b <= block)


def _at(position):
    """(block, transaction index or None) of a heap position, for FIFO order."""
    order = position[3]
    return (position[2], None if order in (-2, -1, _AFTER) else order)


_REGISTRY = {}


def _registry_tornado(chain):
    """Pools and routers of ``chain`` from the shipped networks registry (cached)."""
    if chain is None:
        return frozenset()
    if chain not in _REGISTRY:
        found = set()
        try:
            from .networks import load_networks

            for net in load_networks().values():
                if net.chain_id == chain:
                    found |= net.entry_points
        except (TornadoDemixError, OSError):
            pass
        _REGISTRY[chain] = frozenset(found)
    return _REGISTRY[chain]


def _mark_context(ctx):
    """Remember where the client's cut-off list and the contract check's error list
    stood when this chain was first used, so a result lists only this run's."""
    ctx["_mark"] = (
        mark(ctx["client"]),
        len(getattr(ctx.get("is_contract"), "errors", None) or []),
    )


def _run_notes(tracer):
    """(errors, truncated) of one trace: the tracer's notes, the contract checks'
    failures and the cut-off lists, each client once."""
    errors = list(tracer.notes.values())
    truncated, seen_clients, seen_checks = [], set(), set()
    for ctx in tracer.contexts.values():
        if not ctx:
            continue
        cut_at, err_at = ctx.get("_mark", (0, 0))
        if id(ctx["client"]) not in seen_clients:
            seen_clients.add(id(ctx["client"]))
            truncated += truncated_since(ctx["client"], cut_at)
        check = ctx.get("is_contract")
        if check is not None and id(check) not in seen_checks:
            seen_checks.add(id(check))
            errors += list((getattr(check, "errors", None) or [])[err_at:])
    return errors, truncated


class _Replay:
    """One replay of every fetched history in time order (see the module doc).

    Pure over the fetched histories except for the cached contract, swap and
    bridge lookups of ``tracer``. Collects edges, end points and the frontier:
    addresses that received traced funds but have no history fetched yet.
    """

    def __init__(self, tracer):
        self.t = tracer
        self.edges = []  # (edge, ledger key, raw bounds)
        self.ends = {}  # (chain, address, asset, reason) -> end point (raw amount)
        self.frontier = {}  # ledger key -> {"amount", "hop", "at"}
        self.ledgers = {}
        self.pending = {}  # (key, event index) -> [(amount, lo, hi, tag)]
        self.credits = {}  # key -> {block of the inflow: most traced funds credited}
        self.done = {}  # key -> events processed
        self.heap = []
        self.seq = 0
        self.consumed = set()  # Across deposits taken by a transfer: (tx, log index)

    def _push(self, position, item):
        self.seq += 1
        heapq.heappush(self.heap, (position, self.seq, item))

    # -- end states (the one place they are recorded) -----------------------------

    def stop(self, addr, asset, amount, hop, reason, chain, bridge=None):
        if amount <= self.t.noise:
            return  # rounding noise of the FIFO arithmetic, not funds
        key = (chain, addr, asset, reason)
        end = self.ends.get(key)
        if end is None:
            self.ends[key] = {
                "address": addr,
                "chain": chain,
                "asset": asset,
                "amount": amount,
                "hop": hop,
                "reason": reason,
            }
            if bridge is not None:
                self.ends[key]["bridge"] = dict(bridge)
        else:
            end["amount"] += amount
            end["hop"] = min(end["hop"], hop)
            if bridge is not None:
                # several deposits ending the same way here: keep each of them
                seen = end.setdefault("bridges", [end["bridge"]] if "bridge" in end else [])
                if bridge not in seen:
                    seen.append(dict(bridge))
                end.setdefault("bridge", dict(bridge))

    def arrive(self, key, by_hop, tag_tx, tag_from, tx_hash, position):
        """Traced funds reach ``key`` in ``tx_hash``: follow them or end them here.

        ``by_hop`` is {hop: [fifo, lo, hi]}.
        """
        chain, addr, asset = key
        total = sum(lot[0] for lot in by_hop.values())
        hop = min(by_hop)
        if self.t.is_tornado(chain, addr):
            self.stop(addr, asset, total, hop, TORNADO_REDEPOSIT, chain)
            return
        if total < self.t.dust:
            self.stop(addr, asset, total, hop, BELOW_THRESHOLD, chain)  # DUST_FRACTION
            return
        ctx = self.t.context(chain)
        if ctx["labels"].get(addr):
            self.stop(addr, asset, total, hop, "labelled address", chain)
            return
        lots = []
        for h, (amount, lo, hi) in sorted(by_hop.items()):
            if h >= self.t.max_hops:
                self.stop(addr, asset, amount, h, "hop limit", chain)
            elif key in self.t.failed:
                # the history could not be read: this branch ends here, amount kept
                self.stop(addr, asset, amount, h, LOOKUP_FAILED, chain)
            else:
                lots.append((amount, lo, hi, (h, tag_tx, tag_from)))
        if not lots:
            return
        if key not in self.t.histories:
            hops = min(tag[0] for *_v, tag in lots)
            seen = self.frontier.setdefault(key, {"amount": 0.0, "hop": hops, "at": position})
            seen["amount"] += sum(lot[0] for lot in lots)
            seen["hop"] = min(seen["hop"], hops)
            return
        hist = self.t.histories[key]
        index = hist["match"].get(tx_hash)
        if index is None:
            # the inflow is not in the recipient's history: count it where it happened
            self._push(position, ("synthetic", key, lots, _at(position)))
        elif index < self.done[key]:
            self.late_credit(key, index, lots)
        else:
            self.pending.setdefault((key, index), []).extend(lots)

    def late_credit(self, key, index, lots):
        """Credit an inflow the replay already passed (same position, or out of order).

        When an outflow of the address was replayed in between, or the credit makes
        the traced pool exceed the balance, the order is not known: the ledger is
        marked incomplete, so nothing on it reads as certain.
        """
        hist, ledger = self.t.histories[key], self.ledgers[key]
        ev = hist["events"][index]
        self.credit(key, ev["block"], lots)
        skipped = any(e["kind"] == "out" for e in hist["events"][index + 1 : self.done[key]])
        if skipped or ev["block"] in hist["ambiguous"]:
            ledger.mark_incomplete()
        for amount, lo, hi, tag in lots:
            ledger.add_traced(amount, tag, lo=lo, hi=hi, at=ev["at"])
        if ledger.t_lo > ledger.balance + 1e-9 * max(1.0, ledger.balance):
            ledger.mark_incomplete()

    def credit(self, key, block, lots):
        """Remember the traced funds credited at ``block``, for widening (see run)."""
        blocks = self.credits.setdefault(key, {})
        blocks[block] = blocks.get(block, 0.0) + sum(lot[2] for lot in lots)

    # -- the replay -------------------------------------------------------------

    def run(self):
        t = self.t
        for key, hist in t.histories.items():
            complete = hist["complete"] and key not in t.incomplete_keys
            self.ledgers[key] = _allocation.Ledger(complete=complete)
            self.done[key] = 0
            for i, position in enumerate(hist["positions"]):
                self._push(position, ("event", key, i))
        start = t.start_key
        seed = [(t.amount, t.amount, t.amount, (0, t.arrival["tx"], t.arrival["from"]))]
        if t.arrival["index"] is not None:
            self.pending[(start, t.arrival["index"])] = seed
        else:
            self._push(t.synthetic_position(), ("synthetic", start, seed, None))
        while self.heap:
            _pos, _seq, item = heapq.heappop(self.heap)
            if item[0] == "synthetic":
                _kind, key, lots, at = item
                self.credit(key, at[0] if at else t.start_block, lots)
                ledger = self.ledgers[key]
                ledger.balance += sum(lot[2] for lot in lots)
                for amount, lo, hi, tag in lots:
                    ledger.add_traced(amount, tag, lo=lo, hi=hi, at=at)
                continue
            _kind, key, i = item
            hist = t.histories[key]
            ev = hist["events"][i]
            self.done[key] = i + 1
            ledger = self.ledgers[key]
            unknown_order = ev["block"] in hist["ambiguous"]
            if ev["kind"] == "in":
                lots = self.pending.pop((key, i), [])
                if lots:
                    self.credit(key, ev["block"], lots)
                ledger.inflow(ev["value"])
                if sum(lot[2] for lot in lots) > ev["value"] * (1 + 1e-9) + 1e-12:
                    ledger.mark_incomplete()  # more traced than the inflow carried
                for amount, lo, hi, tag in lots:
                    ledger.add_traced(amount, tag, lo=lo, hi=hi, at=ev["at"])
                if unknown_order and ledger.traced_total > 0:
                    ledger.mark_incomplete()
                continue
            if unknown_order and ledger.traced_total > 0:
                ledger.mark_incomplete()
            pool = ledger.traced_left
            row = ledger.outflow(ev["value"], at=ev["at"])
            if row["fifo"] <= 0:
                continue
            if pool >= t.dust or t.is_tornado(key[0], ev["to"]):
                self.outflow(key, ev, row, hist["positions"][i])
            else:
                # what is left here is under the threshold: not followed, but kept
                hop = min(tag[0] for tag, _a in row["taken"]) + 1
                self.stop(ev["to"], key[2], row["fifo"], hop, BELOW_THRESHOLD, key[0])
        for key, ledger in self.ledgers.items():
            left = ledger.traced_left
            if left > 0 and ledger.lots:
                hop = min(lot[1][0] for lot in ledger.lots)
                why = "not moved on (held or untraced)" if left >= t.dust else BELOW_THRESHOLD
                self.stop(key[1], key[2], left, hop, why, key[0])
        for key, seen in self.frontier.items():
            self.stop(key[1], key[2], seen["amount"], seen["hop"], "trace budget", key[0])
        return self

    def outflow(self, key, ev, row, position):
        """One traced outgoing transfer: an edge, then where its funds go."""
        t = self.t
        chain, addr, asset = key
        used = row["fifo"]
        hop = min(tag[0] for tag, _a in row["taken"]) + 1
        if asset and ev["symbol"]:
            t.names.setdefault((chain, asset), ev["symbol"])
        if self.ledgers[key].incomplete:
            # unseen funds or an unknown order: all traced funds credited up to this
            # block may still be there, including credits replayed late (the previous
            # replay's total covers those, see _Tracer.replay)
            reach = max(
                _credited(self.credits.get(key), ev["block"]),
                _credited(t.prev_credits.get(key), ev["block"]),
            )
            row = dict(row, lower=0.0, upper=max(row["upper"], min(ev["value"], reach)))
        raw = (ev["block"], ev["value"], row["upper"])
        low, high = _allocation.rounded_bounds(row["lower"], row["upper"])
        sources = []
        for (_h, tx, frm), amount in row["taken"]:
            if sources and (sources[-1]["tx_hash"], sources[-1]["from"]) == (tx, frm):
                sources[-1]["amount"] += amount
            else:
                sources.append({"tx_hash": tx, "from": frm, "amount": amount})
        for s in sources:
            s["amount"] = round(s["amount"], 6)
        edge = {
            "hop": hop,
            "from": addr,
            "to": ev["to"],
            "tx_hash": ev["hash"],
            "ts": ev["ts"],
            "asset": t.name(chain, asset),
            "value": round(ev["value"], 6),
            "attributed": round(used, 6),
            "kind": "transfer",
            "history_incomplete": False,  # set from the whole ledger after the replay
            "bounds_available": True,
            "lower": low,
            "upper": high,
            "sources": sources,
        }
        if chain is not None:
            edge["chain_id"] = chain
        self.edges.append((edge, key, raw))
        by_hop = _lots_by_hop(row)
        to = ev["to"]
        ctx = t.context(chain)
        if t.is_tornado(chain, to):
            self.stop(to, asset, used, hop, TORNADO_REDEPOSIT, chain)
            return
        listed = _bridges.listed(t.table, chain, to)
        if listed is not None and _bridges.is_inbound(ev["input"][:10]):
            listed = None  # a call that brings funds in from the other chain, not an exit
        if listed is not None:
            # every listed bridge before the contract rule: named even when not followed.
            # An aggregator (kind "aggregator") may also swap on the same chain: a payment
            # back makes it a swap unless a bridge contract of the table logged an event
            # in the same transaction (then it bridged and the payment back is a refund
            # of unused fees or dust). A bridge row (kind contract or maker_eoa) is never
            # read as a swap: bridges refund messaging fees in the native coin.
            if listed["kind"] == "aggregator":
                swap = t.swap_lookup(chain, addr, ev["hash"], asset)
                if swap is not None and swap is not LOOKUP_FAILED:
                    receipt = t.receipt(chain, ev["hash"])
                    if receipt is LOOKUP_FAILED:
                        swap = LOOKUP_FAILED
                    elif _bridges.bridge_emitters(receipt, chain, t.table, exclude=(to,)):
                        swap = None
                if swap is LOOKUP_FAILED:
                    self.stop(to, asset, used, hop, LOOKUP_FAILED, chain)
                    return
                if swap is not None:
                    self.swap(edge, ev, key, row, position, swap)
                    return
            self.bridge(edge, ev, key, used, hop, row, listed)
            return
        if ctx["labels"].get(to) or not t.is_contract_call(chain, ev, asset):
            self.arrive((chain, to, asset), by_hop, ev["hash"], addr, ev["hash"], position)
            return
        swap = t.swap_lookup(chain, addr, ev["hash"], asset)
        if swap is LOOKUP_FAILED:
            self.stop(to, asset, used, hop, LOOKUP_FAILED, chain)
            return
        if swap is None:
            # an unlisted contract that pays nothing back: a router or periphery may
            # still have made an Across deposit for this transfer in the transaction
            if t.follow and self.bridge(edge, ev, key, used, hop, row, None):
                return
            self.stop(to, asset, used, hop, "contract without a payment back", chain)
            return
        self.swap(edge, ev, key, row, position, swap)

    def swap(self, edge, ev, key, row, position, swap):
        """Continue with the output of a same-transaction swap."""
        t = self.t
        chain, addr, _asset = key
        new_asset, received, symbol = swap
        if new_asset and symbol:
            t.names.setdefault((chain, new_asset), symbol)
        edge["kind"] = "swap"
        edge["swapped_to"] = t.name(chain, new_asset)
        # The swap output is scaled by the share of the input this trace owns.
        scaled = _lots_by_hop(row, received / ev["value"])
        landed = (position[0], position[1], position[2], position[3], 0, ev["hash"])
        self.arrive((chain, addr, new_asset), scaled, ev["hash"], addr, ev["hash"], landed)

    def bridge(self, edge, ev, key, used, hop, row, listed):
        """A bridge hop: continue across a decoded deposit or end the branch by name.

        ``listed`` is the bridge-table row of the recipient, or None for an unlisted
        contract (then only an Across deposit tied to this transfer counts, see
        _tie). Returns False when nothing bridge-like was found (unlisted contract
        only). Receipt and fill lookups are cached on the tracer; which transfer a
        deposit belongs to is decided per replay, each deposit at most once.
        """
        t = self.t
        chain, addr, asset = key

        def end(info, reason):
            if info is not None:
                edge["kind"] = "bridge"
                edge["bridge"] = dict(info)
            self.stop(ev["to"], asset, used, hop, reason, chain, bridge=info)
            return True

        if not t.follow:
            return False if listed is None else end(*_not_followed(listed))
        if (
            listed is not None
            and listed["protocol"] not in _bridges.SUPPORTED
            and listed["kind"] != "aggregator"
        ):
            # a bridge of another protocol does not deposit into Across: its own
            # decoder (Arbitrum Inbox, ZKsync Bridgehub), else named
            if self.canonical(edge, ev, key, row, listed, end):
                return True
            return end(*_not_followed(listed))
        deposits = t.receipt_deposits(chain, ev)
        if deposits is LOOKUP_FAILED:
            info = None
            if listed is not None:
                info = _bridge_info(listed["protocol"], None, _bridges.LOOKUP_FAILED)
            return end(info, LOOKUP_FAILED)
        source_token = asset or _bridges.WRAPPED_NATIVE.get(chain)
        deposit, untied = _tie(deposits, addr, source_token, ev, self.consumed)
        if deposit is None:
            if listed is None:
                return False  # an unlisted contract that is not a deposit: the plain end
            if untied:
                # an Across deposit of this address is in the transaction, but it is not
                # this transfer (another amount, already taken, or made for someone else)
                return end(_bridge_info(_bridges.ACROSS, None, _bridges.NOT_TIED), NOT_TIED)
            if listed["protocol"] not in _bridges.SUPPORTED:
                return end(*_not_followed(listed))
            reason = f"bridge {listed['protocol']}, deposit event not decoded"
            if "v2" in listed.get("role", "").lower():
                reason += " (v2 not decoded)"
            return end(_bridge_info(listed["protocol"], None, _bridges.NOT_DECODED), reason)
        self.consumed.add((deposit["tx_hash"], deposit["log_index"]))
        found = t.follow_deposit(chain, deposit, ev["hash"])
        info = dict(found["info"])
        if ev["to"] != deposit["spoke"]:
            info["via"] = ev["to"]  # periphery, router or aggregator
        if found.get("reason"):
            return end(info, found["reason"])
        edge["kind"] = "bridge"
        edge["bridge"] = info
        fill = found["fill"]
        # The share of the transfer that reached the destination: the fill's output
        # over the larger of the deposit and the transfer (a router or aggregator may
        # keep a fee before it deposits, at most TIE_FEE of the transfer).
        base = max(deposit["input_amount"], ev.get("raw") or 0)
        scaled = _lots_by_hop(row, fill["output_amount"] / base if base else 1.0)
        dest_chain, fill = found["dest_chain"], found["fill"]
        landed = (
            fill.get("ts") or ev["ts"],
            dest_chain,
            fill["block"],
            -1,
            0,
            fill["tx_hash"],
        )
        dest_key = (dest_chain, fill["recipient"], found["asset"])
        self.arrive(dest_key, scaled, fill["tx_hash"], addr, fill["tx_hash"], landed)
        return True

    def canonical(self, edge, ev, key, row, listed, end):
        """An Arbitrum Inbox or ZKsync Bridgehub deposit that IS this transfer.

        Tied like an Across deposit: a native transfer, the decoded deposit carries
        exactly its amount, the traced address is the L1 sender of the message (or
        sent the transaction; for ZKsync the calldata is the traced address's own
        transaction), and each message or call is taken by one transfer at most.
        Returns False when nothing is tied (the caller names the bridge).
        """
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        if asset is not None or raw <= 0:
            return False  # only native deposits are decoded
        if listed["protocol"] == _canonical.ZKSYNC:
            call = _canonical.decode_bridgehub_call(ev["input"])
            mark = (_canonical.ZKSYNC, ev["hash"])
            if call is None or call["msg_value"] != raw or mark in self.consumed:
                return False
            self.consumed.add(mark)
            info = _bridge_info(
                _canonical.ZKSYNC, call["chain_id"], _bridges.DEST_CHAIN_NOT_SUPPORTED
            )
            info.update(
                function=call["function"],
                l2_recipient=call["l2_recipient"],
                refund_recipient=call["refund_recipient"],
                mint_value=call["mint_value"],
                l2_value=call["l2_value"],
            )
            shown = call["l2_recipient"] or "not decoded"
            return end(info, f"bridge zksync (recipient {shown}, chain not supported)")
        if listed["protocol"] != _canonical.ARBITRUM:
            return False
        receipt = t.receipt(chain, ev["hash"])
        if receipt is LOOKUP_FAILED:
            failed = _bridge_info(_canonical.ARBITRUM, None, _bridges.LOOKUP_FAILED)
            return end(failed, LOOKUP_FAILED)
        msg = None
        for m in _canonical.decode_inbox_messages(receipt, ev["hash"], chain, t.table):
            mark = (_canonical.ARBITRUM, m["tx_hash"], m["message_num"])
            if m["deposit"] != raw or mark in self.consumed or m["l2_call_value"] <= 0:
                # l2CallValue 0: a fee-only retryable (a gateway's token deposit), whose
                # ``to`` is the L2 gateway and not where the funds go
                continue
            # the Bridge log names the caller; the transaction sender counts only
            # when that log is missing
            sender = m["l1_sender"] if m["l1_sender"] is not None else m["tx_from"]
            if sender == addr:
                msg = m
                break
        if msg is None:
            return False
        self.consumed.add((_canonical.ARBITRUM, msg["tx_hash"], msg["message_num"]))
        found = t.follow_message(chain, msg, ev)
        info = dict(found["info"])
        call = _canonical.decode_inbox_call(ev["input"])
        if call is not None:
            info["function"] = call["function"]
        if found.get("reason"):
            return end(info, found["reason"])
        edge["kind"] = "bridge"
        edge["bridge"] = info
        credit = found["credit"]
        # what reaches L2 is l2CallValue: the L2 fees in the deposit are not carried over
        scaled = _lots_by_hop(row, msg["l2_call_value"] / raw)
        dest_chain = _canonical.ARBITRUM_ONE
        landed = (credit["ts"], dest_chain, credit["block"], -1, 0, credit["tx_hash"])
        dest_key = (dest_chain, msg["recipient"], None)
        self.arrive(dest_key, scaled, credit["tx_hash"], addr, credit["tx_hash"], landed)
        return True


class _Tracer:
    """Fetches histories level by level and replays them until nothing new is reached."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.histories = {}  # ledger key -> {"events", "positions", "match", "complete"}
        self.contract_cache = {}
        self.bridge_cache = {}
        # L2 credits already given to an Arbitrum message: messages are resolved once
        # each (cached), in replay order, which is L1 time and so message number order
        self.claimed_credits = set()
        self.incomplete_keys = set()  # ledgers a replay proved incomplete
        self.prev_credits = {}  # the previous replay's credits per ledger
        self.arrival = {"tx": None, "matched": "synthetic", "index": None, "from": None}
        self.failed = {}  # ledger key -> error text: history not read (lookup_failed)
        self.swap_cache = {}
        self.notes = {}  # (what, chain, address or tx) -> error record, first seen

    def context(self, chain):
        if chain not in self.contexts:
            dest = self.dest_for(chain) if self.dest_for else None
            if dest is None:
                self.contexts[chain] = None
            else:
                ctx = dict(dest, incoming=_Incoming(dest["client"]))
                ctx.setdefault("labels", {})
                ctx.setdefault("currency", "ETH")
                ctx.setdefault("is_contract", None)
                _mark_context(ctx)
                self.contexts[chain] = ctx
        return self.contexts[chain]

    # -- failed lookups (recorded once each, whatever the number of replays) ---------

    def note(self, what, exc, chain, address=None, tx=None, kind=None):
        key = (what, chain, address, tx)
        if key not in self.notes:
            rec = lookup_failed(what, exc, address=address, tx=tx)
            if chain is not None:
                rec["chain_id"] = chain
            if kind:
                rec["kind"] = kind
            self.notes[key] = rec

    def attempts(self):
        """Histories read or tried: what the address budget counts."""
        return len(self.histories) + len(self.failed)

    def is_tornado(self, chain, addr):
        """True for a Tornado pool or router of ``chain`` (a re-deposit ends there)."""
        ctx = self.contexts.get(chain)
        known = ctx.get("tornado") if ctx else None
        if known is None:
            known = _registry_tornado(chain)
        return addr in known

    def swap_lookup(self, chain, addr, tx_hash, asset):
        """The swap output of ``tx_hash`` (cached), or LOOKUP_FAILED."""
        cache_key = (chain, addr, tx_hash, asset)
        if cache_key not in self.swap_cache:
            try:
                found = self.context(chain)["incoming"].swap_output(addr, tx_hash, asset)
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note("swap output (internal and token transfers)", exc, chain, addr)
                found = LOOKUP_FAILED
            self.swap_cache[cache_key] = found
        return self.swap_cache[cache_key]

    def name(self, chain, asset):
        if asset is None and (chain, None) not in self.names:
            ctx = self.context(chain)
            self.names[(chain, None)] = ctx["currency"] if ctx else "native"
        return self.names.get((chain, asset), asset)

    def is_contract_call(self, chain, ev, asset):
        cache_key = (chain, ev["to"], bool(asset), ev["input"] not in ("", "0x"))
        if cache_key not in self.contract_cache:
            ctx = self.context(chain)
            self.contract_cache[cache_key] = _looks_like_contract(ev, asset, ctx["is_contract"])
        return self.contract_cache[cache_key]

    # -- histories --------------------------------------------------------------

    def fetch(self, key):
        """Read one history. An explorer error ends the branches that reach this
        address (lookup_failed, amount kept); every other branch goes on. A rejected
        API key still stops the trace."""
        chain, addr, asset = key
        ctx = self.context(chain)
        client = ctx["client"]
        try:
            if asset:
                rows = client.token_transfers(addr, contract=asset)
            else:
                rows = client.outgoing_txs(addr)
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.failed[key] = str(exc)
            self.note("token transfers" if asset else "history", exc, chain, addr)
            return

        def failed(what, exc):
            self.note(what, exc, chain, addr)

        events, complete, index = _history(ctx, addr, asset, rows, failed)
        self.histories[key] = {"events": events, "complete": complete, "index": index}

    def order_all(self):
        """Sort every history by time; an unindexed row borrows its transaction's index.

        The timestamp of a block is the largest one seen at or before it on that
        chain, so a row without a timestamp cannot move ahead of earlier blocks.
        """
        index, clock = {}, {}
        for (chain, _a, _s), hist in self.histories.items():
            blocks = clock.setdefault(chain, {})
            for tx, order in hist["index"].items():
                index.setdefault((chain, tx), order)
            for ev in hist["events"]:
                blocks[ev["block"]] = max(blocks.get(ev["block"], 0), ev["ts"])
        for blocks in clock.values():
            running = 0
            for block in sorted(blocks):
                running = blocks[block] = max(running, blocks[block])
        for (chain, _a, _s), hist in self.histories.items():
            keyed = []
            for ev in hist["events"]:
                if ev["value"] <= 0:
                    continue
                order = ev["order"]
                if order is None:
                    order = index.get((chain, ev["hash"]))
                ev["at"] = (ev["block"], order)
                keyed.append((_position(chain, ev, order, clock[chain][ev["block"]]), ev))
            keyed.sort(key=lambda pair: pair[0])
            hist["positions"] = [p for p, _e in keyed]
            hist["events"] = [e for _p, e in keyed]
            match = {}
            for i, ev in enumerate(hist["events"]):
                if ev["kind"] == "in":
                    match.setdefault(ev["hash"], i)
            hist["match"] = match
            # blocks where this address's order is not known: an unindexed event
            # next to an event of another transaction
            txs, unknown = {}, set()
            for ev in hist["events"]:
                txs.setdefault(ev["block"], set()).add(ev["hash"])
                if ev["at"][1] is None:
                    unknown.add(ev["block"])
            hist["ambiguous"] = {b for b in unknown if len(txs[b]) > 1}

    def find_arrival(self):
        events = self.histories[self.start_key]["events"]
        given = self.arrival_tx
        if given is not None:
            known = any(e["kind"] == "in" and e["hash"] == given for e in events)
            tx, how = (given, "exact") if known else (None, "explicit_not_found")
        else:
            tx, how = _find_arrival(events, self.amount, self.start_block)
        index = self.histories[self.start_key]["match"].get(tx) if tx else None
        sender = events[index]["from"] if index is not None else None
        self.arrival = {"tx": tx, "matched": how, "index": index, "from": sender}

    def synthetic_position(self):
        """Start of ``start_block`` in the start address's history (after all, if later)."""
        hist = self.histories[self.start_key]
        chain = self.start_key[0]
        later = [
            p for p, e in zip(hist["positions"], hist["events"]) if e["block"] >= self.start_block
        ]
        ts = later[0][0] if later else math.inf
        return (ts, chain if chain is not None else -1, self.start_block, -2, 0, "")

    # -- bridge lookups (cached: the replay runs several times) -------------------

    def receipt(self, chain, tx_hash):
        """The receipt of ``tx_hash`` (cached), or LOOKUP_FAILED (recorded once)."""
        cache_key = ("receipt", chain, tx_hash)
        if cache_key not in self.bridge_cache:
            try:
                found = self.contexts[chain]["client"].tx_receipt(tx_hash)
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note("bridge deposit receipt", exc, chain, tx=tx_hash)
                found = LOOKUP_FAILED
            self.bridge_cache[cache_key] = found
        return self.bridge_cache[cache_key]

    def receipt_deposits(self, chain, ev):
        """Across deposits decoded from the receipt of ``ev``'s transaction, or
        LOOKUP_FAILED when the receipt could not be read."""
        receipt = self.receipt(chain, ev["hash"])
        if receipt is LOOKUP_FAILED:
            return LOOKUP_FAILED
        return _bridges.decode_receipt_deposits(receipt, ev["hash"], chain, self.table, ev["ts"])

    def follow_deposit(self, chain, deposit, tx_hash):
        """Where one Across deposit went (cached per deposit).

        Returns {"info", "reason"} when the branch ends with one of the five states,
        or {"info", "fill", "dest_chain", "asset"} to continue on the destination chain.
        """
        cache_key = ("deposit", chain, deposit["tx_hash"], deposit["log_index"])
        if cache_key not in self.bridge_cache:
            self.bridge_cache[cache_key] = self._follow_deposit(deposit, tx_hash)
        return self.bridge_cache[cache_key]

    def _follow_deposit(self, deposit, tx_hash):
        table = self.table
        dest_chain = deposit["dest_chain_id"]
        info = _bridge_info(deposit["protocol"], dest_chain, None)
        info.update(
            deposit_id=deposit["deposit_id"],
            depositor=deposit["depositor"],
            recipient=deposit["recipient"],
            fill_deadline=deposit["fill_deadline"],
        )

        def ended(state):
            info["state"] = state
            return {"info": info, "reason": state}

        dest = self.context(dest_chain)
        if dest is None:
            return ended(_bridges.DEST_CHAIN_NOT_SUPPORTED)
        if not _bridges.destination_pools(table, dest_chain):
            return ended(_bridges.DEST_POOL_NOT_LISTED)
        try:
            fill = _bridges.find_fill(dest["client"], deposit, table)
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.note("bridge fill", exc, dest_chain, tx=tx_hash)
            return ended(_bridges.LOOKUP_FAILED)
        if fill is None:
            return ended(_bridges.no_fill_state(deposit))
        info.update(
            state=_bridges.FILLED,
            fill_tx=fill["tx_hash"],
            fill_block=fill["block"],
            fill_recipient=fill["recipient"],
            output_token=fill["output_token"],
            sped_up=fill["sped_up"],
        )
        wrapped = _bridges.WRAPPED_NATIVE.get(dest_chain)
        paid_native = wrapped == fill["output_token"] and dest_chain not in _bridges.TOKEN_WRAPPED
        return {
            "info": info,
            "fill": fill,
            "dest_chain": dest_chain,
            "asset": None if paid_native else fill["output_token"],
        }

    def follow_message(self, chain, msg, ev):
        """Where one Arbitrum Inbox message was credited on L2 (cached per message).

        Returns {"info", "reason"} when the branch ends, or {"info", "credit"} to
        continue on Arbitrum from the L2 recipient.
        """
        cache_key = ("message", chain, msg["tx_hash"], msg["message_num"])
        if cache_key not in self.bridge_cache:
            self.bridge_cache[cache_key] = self._follow_message(msg, ev)
        return self.bridge_cache[cache_key]

    def _follow_message(self, msg, ev):
        dest_chain = _canonical.ARBITRUM_ONE
        recipient = msg["recipient"]
        info = _bridge_info(_canonical.ARBITRUM, dest_chain, None)
        info.update(
            message_num=msg["message_num"],
            message_kind=msg["kind"],
            l2_recipient=recipient,
            l2_call_value=msg["l2_call_value"],
            l2_sender=msg["l2_sender"],
        )

        def ended(state, reason=None):
            info["state"] = state
            return {"info": info, "reason": reason or state}

        not_found = "bridge arbitrum, l2 credit not found"
        dest = self.context(dest_chain)
        if dest is None:
            return ended(
                _bridges.DEST_CHAIN_NOT_SUPPORTED,
                f"bridge arbitrum (recipient {recipient}, chain not supported)",
            )
        try:
            credit = _canonical.find_l2_credit(
                dest["client"], msg, ev["ts"], claimed=self.claimed_credits
            )
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.note("bridge l2 credit", exc, dest_chain, address=recipient, tx=ev["hash"])
            return ended(_bridges.LOOKUP_FAILED)
        if credit is None:
            return ended(_canonical.L2_CREDIT_NOT_FOUND, not_found)
        self.claimed_credits.add(credit["tx_hash"])
        info.update(
            state=_canonical.CREDITED, fill_tx=credit["tx_hash"], fill_block=credit["block"]
        )
        return {"info": info, "credit": credit}

    # -- the walk ---------------------------------------------------------------

    def replay(self):
        """Replay until no further ledger turns out incomplete.

        A ledger found incomplete (overdraft, unknown order) has every edge widened,
        so the credits it passed downstream must be the widened ones too: the
        replay runs again with it incomplete from the start and with the previous
        replay's credits (which include credits that arrived late). Both only grow
        and flow forward in time, so this settles; it is capped at one replay per
        ledger plus three; a replay that has not settled at the cap is used as it
        is and recorded as a note (``replay_unsettled``).
        """
        for _ in range(len(self.histories) + 3):
            replay = _Replay(self).run()
            found = {k for k, ledger in replay.ledgers.items() if ledger.incomplete}
            # credits only widen incomplete ledgers, so only theirs must settle
            same = found <= self.incomplete_keys and all(
                replay.credits.get(k) == self.prev_credits.get(k) for k in found
            )
            self.incomplete_keys |= found
            self.prev_credits = replay.credits
            if same:
                break
        else:
            self.note(
                "trace replay",
                "bounds of incomplete histories did not settle; the last replay is shown",
                self.start_key[0],
                kind="replay_unsettled",
            )
        return replay

    def run(self):
        start = self.start_key
        if self.max_hops <= 0 or self.max_nodes <= 0:
            reason = "hop limit" if self.max_hops <= 0 else "trace budget"
            replay = _Replay(self)
            replay.stop(start[1], start[2], self.amount, 0, reason, start[0])
            return replay
        self.fetch(start)
        if start in self.failed:
            self.arrival["matched"] = LOOKUP_FAILED
            replay = _Replay(self)
            replay.stop(start[1], start[2], self.amount, 0, LOOKUP_FAILED, start[0])
            return replay
        self.order_all()
        self.find_arrival()
        while True:
            replay = self.replay()
            level = sorted(
                replay.frontier,
                key=lambda k: (replay.frontier[k]["hop"], replay.frontier[k]["at"], str(k)),
            )
            if not level or self.attempts() >= self.max_nodes:
                return replay
            for key in level:
                if self.attempts() >= self.max_nodes:
                    break
                self.fetch(key)
            self.order_all()


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
    tornado=None,
) -> dict:
    """Follow ``amount`` of one asset forward from ``start``.

    ``token`` is an ERC-20 contract address, or None for the native currency.
    Returns {start, amount, asset, edges, terminals, nodes_expanded}. An edge is
    {hop, chain_id, from, to, tx_hash, ts, asset, value, attributed, lower, upper,
    history_incomplete, bounds_available, sources, kind}; ``attributed`` is the FIFO
    amount, ``lower``/``upper`` the bounds under any ordering (allocation.py, lower
    rounded down and upper up); ``sources`` splits the FIFO amount back across the
    incoming branches it came from ({tx_hash, from, amount}: the arrival
    transaction and sender of each branch, oldest first); ``hop`` is one more than
    the shortest of those branches. ``kind`` is transfer, swap or bridge (a bridge
    edge also carries ``bridge``: protocol, destination chain (None when not
    decoded), state (see bridges.py), and for a decoded deposit the deposit id,
    depositor, recipient, ``via`` (the periphery or router it went through) and the
    fill transaction; for an Arbitrum or ZKsync hop ``l2_recipient``, the message
    number or ``refund_recipient``, and for a confirmed Arbitrum credit the L2
    transaction as ``fill_tx``). Each outgoing transaction is one edge, whatever the number of
    branches that reached its sender. A terminal is {address, chain_id, asset,
    amount, hop, reason, label}, plus ``bridge`` when it ends at a bridge; end points
    at the same address for the same reason are merged.

    Bridge continuation is on when ``chain_id`` (the chain of ``client``) and
    ``destinations`` are given. ``destinations`` is a callable (or dict) from a
    chain id to {"client", "labels", "currency", "is_contract"} or None for a
    chain that cannot be read.

    ``arrival_tx`` is the transaction that brought ``amount`` to ``start``; given, it
    overrides the amount match. The result carries ``arrival_tx`` and
    ``arrival_matched`` ("exact", "ambiguous", "synthetic" or "explicit_not_found").
    FIFO starts at that arrival (at ``start_block`` when it is not known): a
    transfer before it carries none of the traced funds.

    Every traced unit ends exactly once, so the terminal amounts add up to ``amount``
    (rounding aside) when no swap or bridge changed the asset. Besides the reasons
    above a branch ends as ``lookup_failed`` (its history, or a swap or bridge lookup,
    could not be read; the amount is kept and the other branches go on; a rejected
    API key still raises), ``below_threshold`` (under DUST_FRACTION of the start, not
    followed) or ``tornado_redeposit`` (sent into a Tornado pool or router, whatever
    the amount). ``tornado`` is the set of pool and router addresses of ``chain_id``;
    by default it comes from the networks registry. The result also carries
    ``errors``, ``error_count`` and ``truncated`` (see :mod:`tornado_demix.incomplete`).
    """
    labels = labels or {}
    start = start.lower()
    token = token.lower() if token else None
    dest_for = destinations.get if isinstance(destinations, dict) else destinations
    follow = chain_id is not None and dest_for is not None
    # loaded whenever the chain is known: a listed bridge is named even when not followed
    table = (
        (bridge_table if bridge_table is not None else _bridges.load_bridges())
        if chain_id is not None
        else {}
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
    if tornado is not None:
        contexts[chain_id]["tornado"] = {a.lower() for a in tornado}
    _mark_context(contexts[chain_id])
    names = {(chain_id, None): currency}
    if token:
        names[(chain_id, token)] = token
    tracer = _Tracer(
        start_key=(chain_id, start, token),
        amount=amount,
        start_block=start_block,
        arrival_tx=(arrival_tx or "").lower() or None,
        dust=amount * DUST_FRACTION,
        noise=amount * NOISE_FRACTION,
        max_hops=max_hops,
        max_nodes=max_nodes,
        dest_for=dest_for,
        follow=follow,
        table=table,
        contexts=contexts,
        names=names,
    )
    replay = tracer.run()

    edges = []
    for edge, key, (block, value, upper) in replay.edges:
        ledger = replay.ledgers.get(key)
        edge["history_incomplete"] = bool(ledger and ledger.incomplete)
        if edge["history_incomplete"]:
            # the whole address: an outflow before the overdraft rests on the same
            # missing data, so it gets the bounds that hold whatever was unseen
            reach = min(value, _credited(replay.credits.get(key), block))
            edge["lower"], edge["upper"] = _allocation.rounded_bounds(0.0, max(upper, reach))
        edge["status"] = share_status(edge)
        edges.append(edge)
    edges.sort(key=lambda e: e["hop"])  # stable: time order inside a hop
    terminals = []
    for end in replay.ends.values():
        chain = end["chain"]
        ctx = tracer.contexts.get(chain)
        terminal = {
            "address": end["address"],
            "asset": tracer.name(chain, end["asset"]),
            "amount": round(end["amount"], 6),
            "hop": end["hop"],
            "reason": end["reason"],
            "label": ctx["labels"].get(end["address"]) if ctx else None,
        }
        if end.get("bridge") is not None:
            terminal["bridge"] = end["bridge"]
        if len(end.get("bridges") or []) > 1:
            terminal["bridges"] = end["bridges"]
        if chain is not None:
            terminal["chain_id"] = chain
        terminals.append(terminal)
    result = {
        "start": start,
        "amount": amount,
        "asset": tracer.name(chain_id, token),
        "edges": edges,
        "terminals": terminals,
        "nodes_expanded": len(tracer.histories),
        "arrival_tx": tracer.arrival["tx"],
        "arrival_matched": tracer.arrival["matched"],
    }
    errors, truncated = _run_notes(tracer)
    return attach(result, errors, truncated)
