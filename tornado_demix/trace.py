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
paid out, and the hop count carries on (a deposit of a token the contract bought first is
tied by who paid and what reached the contract, a fill paid to the Across MulticallHandler
goes on from the handler's payments, and an ERC-4337 smart account is the sender of its
user operations' calls). When it is not followed the branch ends
with a distinct state (fill_not_found, deposit_expired_possible_refund,
destination_chain_not_supported, destination_pool_not_listed, lookup_failed); a
listed bridge without a decoder ends as "bridge <protocol> (not followed)". An
Arbitrum Inbox message (retryable ticket or ETH deposit) tied to the transfer is
followed on Arbitrum from its L2 recipient once its L2 credit is found ("bridge
arbitrum, l2 credit not found" otherwise), and so is an ERC-20 deposit through an
Arbitrum token gateway, in the L2 token; a ZKsync deposit (Bridgehub, native or ERC-20,
or a legacy Era request) is decoded and ends as "bridge zksync (recipient <address>,
chain not supported)". See bridges_canonical.py. Relay, Mayan, THORChain, Maya,
Layerswap, NEAR Intents, Unit and Hyperliquid deposits are tied the same way and end
with what the chain shows and why they are not followed (a Mayan same-chain payout is
followed like a swap); see bridges_named.py. A Circle CCTP burn (v1 or v2) tied the same
way is followed to its mint on an EVM destination of the registry, in the minted token
(see bridges_cctp.py). A transfer into CoW Protocol's GPv2Settlement is tied to the
owner's Trade event and continued as a swap from the buy-token payout to the order's
receiver (see swaps_cow.py).

Order of work. Histories are fetched breadth first: the start address, then
every address the current replay sends traced funds to, a whole level at a time,
in a fixed order (hop, time of the first arrival, address). After each level the
replay runs again over all fetched histories, merged in time order (timestamp,
then block, transaction index and the place inside the transaction: top-level value,
internal traceId, token logIndex; chains interleave by timestamp, which keeps a
bridge fill after its deposit). An address reached again by a later branch is
therefore simply replayed with the merged ledger; nothing is attributed twice. Only
addresses that hold traced funds (or have some pending) are replayed event by event;
the others are caught up from a cache when funds reach them (see _Replay), and a
history is sorted again only when a newly read one changes its order (order_all).

The next address is credited with the FIFO amount for FIFO and with the edge's
bounds [lower, upper] for its own bounds, so a hop is certain only when the
edges before it are. An internal transfer without a transaction index of its own
borrows one from any fetched row of the same transaction; when none exists its
order inside the block is unknown: FIFO does not let an outflow take a lot not
known to have arrived before it, and the address is marked incomplete. Inside one
transaction an equal index is no order: an outflow takes a lot of its own
transaction only when both places are known, comparable and the lot's is earlier;
otherwise the amount stays and ends as order_unknown ("order inside one transaction
unknown", ORDER_SAME_TX).

FIFO attribution is a convention, not a fact: funds in one account are
fungible, so every edge is a lead to corroborate. Next to the FIFO amount each
edge carries ``lower`` and ``upper``: how much of the transfer can be the traced
funds under ANY ordering convention, from the sender's own history of that asset
(see allocation.py), from the same computation as the FIFO amount. lower > 0
means "certainly at least this much" unless the history is incomplete; lower 0
means the transfer is only possible.
"""

from __future__ import annotations

import bisect
import heapq
import math

from . import allocation as _allocation
from . import bridges as _bridges
from . import bridges_canonical as _canonical
from . import bridges_cctp as _cctp
from . import bridges_named as _named
from . import swaps_cow as _cow
from .errors import ApiKeyError, TornadoDemixError
from .etherscan import MAX_PAGES as _MAX_PAGES
from .etherscan import PAGE_SIZE as _PAGE_SIZE
from .incomplete import attach, lookup_failed, mark, truncated_since
from .txstatus import failed as _failed

# a fetch this long may have been cut off (only for clients without ``truncated``)
_HISTORY_CAP = _MAX_PAGES * _PAGE_SIZE
MAX_HOPS = 4
MAX_NODES = 40  # addresses expanded per trace; bounds the API calls
DUST_FRACTION = 0.01  # amounts below this share of the start are not followed
NOISE_FRACTION = 1e-9  # end amounts below this share of the start are rounding noise

# End states of a branch (terminal ``reason``) added for failed lookups and small or
# re-mixed amounts; the others are written out where they are recorded.
LOOKUP_FAILED = "lookup_failed"  # a history (or swap / bridge lookup) could not be read
REFUND_ONLY = "refund_only"  # an aggregator paid back only a native refund (_Tracer.payback)
BELOW_THRESHOLD = "below_threshold"  # under DUST_FRACTION of the start: not followed
TORNADO_REDEPOSIT = "tornado_redeposit"  # sent into a Tornado pool or router again
# traced funds that a same-block outflow of unknown order may have taken (_Replay.run)
ORDER_UNKNOWN = "order_unknown"
# why an order_unknown outflow is unordered when it shares the transaction of the arrival
ORDER_SAME_TX = "order inside one transaction unknown"
# arrival_matched: the arrival transaction holds several inflows that can be the traced one
AMBIGUOUS_IN_TX = "ambiguous_in_tx"
POSSIBLE_ROWS = 25  # possible outflows of incomplete histories listed; the rest summarised
# an Across deposit in the transaction that is not this transfer (see _tie)
NOT_TIED = "bridge across (deposit not tied to this transfer)"
# an Across deposit made after a swap whose fill amount could not be read in units
SWAPPED_UNREAD = "bridge across (swapped before the deposit, fill amount not read)"
# a fill paid to the Across MulticallHandler whose onward payments are not this fill
HANDLER_NOT_TIED = "bridge across (multicall handler, onward transfer not tied)"
HANDLER_REST = "bridge across (multicall handler, rest not paid on)"
TIE_FEE = 0.10  # most a router may keep of a transfer before it deposits the rest
END_STATES_NOTE = (
    "lookup_failed: a history (or a swap or bridge lookup) could not be read, so the "
    "amount stops there and the other branches go on; below_threshold: under "
    f"{DUST_FRACTION:.0%} of the start amount, not followed; tornado_redeposit: sent "
    "into a Tornado pool again, whatever the amount; order_unknown: traced funds still at "
    "the address under FIFO that an outgoing transfer in the same block may have taken, "
    "its order against their arrival not known (an internal transfer without a "
    "transaction index; or, inside one transaction, an internal transfer without a "
    "readable traceId or a token transfer without a logIndex: order inside one "
    "transaction unknown), the transfers shown. The end amounts add up to the "
    "start amount unless a swap or bridge changed the asset. On an incomplete history an "
    "outgoing transfer that FIFO gives none of the traced funds but that may still hold "
    "some is listed as a possible outflow (history incomplete), not followed; the first "
    f"{POSSIBLE_ROWS} are listed and the rest counted."
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
    "supported)'; its refund recipient gets unused fees only and is shown separately. "
    "An ERC-20 deposit through an Arbitrum token gateway (outboundTransfer or "
    "outboundTransferCustomRefund, the gateway's DepositInitiated confirmed by its Inbox "
    "message) of the traced token, amount and sender is followed on Arbitrum in the L2 "
    "token (the router's calculateL2TokenAddress) once the L2 credit is found: a mint, or "
    "a payment by the L2 gateway, of the same amount to the recipient; otherwise 'bridge "
    "arbitrum, l2 credit not found'. A fee-only retryable (l2CallValue 0) is never followed "
    "by itself. A ZKsync ERC-20 deposit (requestL2TransactionTwoBridges through the "
    "listed second bridge, same token, amount and sender) and a legacy ZKsync Era "
    "requestL2Transaction (l2Value at most the transfer, the rest paying the L2 base cost) "
    "end as 'bridge zksync (recipient <address>, chain not supported)', token shown. "
    "Recognised and decoded but not followed, each tied by emitter, sender, token and "
    "amount and taken once: a Relay deposit ends as 'bridge relay (request <id>, "
    "destination not on chain)' (the destination and recipient are only in the off-chain "
    "quote); a Mayan order as 'bridge mayan (to <chain>, recipient <address>)' (the "
    "destination fulfil is keyed by an order hash or CCTP nonce not computed here), "
    "while a Mayan MonoChainSwap payout on the same chain is followed from its payee "
    "like a swap; a THORChain or Maya router deposit as 'bridge thorchain (to <chain>, "
    "recipient <address>)' ('to THORChain (<asset>)' for a synth, trade or secured "
    "asset, 'pays the originator' for an empty destination) or with the memo's action (the "
    "outbound is a vault payout not tied on-chain to the memo), memo shown; a transfer to the Layerswap maker, the NEAR "
    "Intents deposit wallet or a Unit treasury as matched off chain; native USDC sent to "
    "Hyperliquid Bridge2 on Arbitrum as 'bridge hyperliquid (recipient <sender>, credited "
    "on HyperCore)'. A Circle CCTP burn (v1 or v2 depositForBurn: the USDC goes to the "
    "TokenMinter, the TokenMessenger logs DepositForBurn) of the traced token and amount, "
    "made by the traced address, is followed on an EVM destination of the registry from "
    "the mint recipient once the mint is found (the recipient's MintAndWithdraw whose "
    "transaction holds the MessageReceived of this burn: source domain, burn token, "
    "recipient, amount, sender, and the nonce for v1), in the minted amount (a v2 fee is "
    "kept by Circle); otherwise 'bridge cctp (to <chain>, recipient <address>, mint not "
    "found)' or '..., chain not supported)'; a domain without an EVM chain id "
    "here (Solana, Noble and other non-EVM or newer domains) ends as 'bridge cctp (to <domain>, recipient <address>)', the "
    "recipient decoded (base58 on Solana). Across edge cases: a contract that deposits in "
    "its own name is taken only when the traced address called it directly (sent the "
    "transaction to it, or a user operation of the traced address holds the deposit) and "
    "no other inflow of the token reached it in that call; otherwise 'bridge across "
    "(deposit not tied to this transfer)'. A deposit of another token than the traced one "
    "(swapped before the deposit by a periphery, router or aggregator) is tied when it is "
    "the only such deposit, made by the traced address (or by the contract on the same "
    "stronger tie), the traced transfer is the only inflow of the traced token into "
    "the contract in that call, and the contract paid the deposit (a Transfer of the "
    "deposited token from it to the SpokePool of exactly the input amount; for a native "
    "deposit, the traced address sent the transaction to the contract, or, inside a user "
    "operation, the deposit is in the contract's name); the whole transfer is then "
    "followed as the amount the fill "
    "paid, in the filled token ('bridge across (swapped before the deposit, fill amount "
    "not read)' when the payout row that gives its decimals is not listed). A fill paid to "
    "an Across MulticallHandler (both listed deployments) is followed from its payments "
    "in the fill "
    "transaction (token transfers after the fill, and native payments when the fill is in "
    "the wrapped native coin), each payee with its share; 'bridge across (multicall "
    "handler, onward transfer not tied)' when there is none, they exceed the fill or the "
    "transaction holds several fills to the handler, and 'bridge across (multicall handler, "
    "rest not paid on)' for what the handler kept. An ERC-4337 smart account counts as the "
    "sender of the calls of its successful user operations: the logs between the previous "
    "UserOperationEvent of a listed EntryPoint (v0.6, v0.7, v0.8) and its own (the traced "
    "transfer must be in the same operation as the deposit). At a listed "
    "aggregator only a material payment back makes the transfer a swap: a payment in "
    "another token, or native coin above the value the sender attached to the "
    "transaction; a native refund within that value without a tied deposit or a bridge "
    "event ends as 'aggregator <protocol> (bridge or swap unknown)'."
)
SWAP_STATES_NOTE = (
    "CoW Protocol: a transfer into GPv2Settlement is the traced owner's order when a Trade "
    "of the settlement in the same transaction has that owner, sell token and exactly that "
    "sell amount (each Trade taken once); it is followed as a swap from the buy-token "
    "payout of exactly the order's buy amount (the owner's own when the owner has one per "
    "such trade, else the only one when no other trade has that buy token and amount: "
    "the receiver is shown as 'paid out by cow to'). Otherwise it ends as 'swap cow (trade not "
    "tied to this transfer)' or 'swap cow (order <uid>, payout not found)'. A settlement "
    "is a batch: other users' orders and the solver's liquidity share the transaction, "
    "so the buy tokens may come from another user's sell; the payout is the order's "
    "output, not a path through the batch."
)
END_STATES_NOTE += " " + BRIDGE_STATES_NOTE + " " + SWAP_STATES_NOTE

_AFTER = 10**12
_LAZY = True  # see _Replay (False queues every event: the reference order)
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

    def rows(self, addr):
        """(row, token or None) of every internal and token transfer of ``addr``, read once."""
        if addr not in self.cache:
            internal = self.internal(addr)
            tokens = self.client.token_transfers(addr)
            self.cache[addr] = [(r, None) for r in internal] + [
                (r, (r.get("contractAddress") or "").lower()) for r in tokens
            ]
        return self.cache[addr]

    def payments(self, addr, tx_hash, token, raw, payer):
        """[(value, symbol)] of the payments of exactly ``raw`` of ``token`` (None: native)
        from ``payer`` to ``addr`` in ``tx_hash`` (a row without a sender counts; ``payer``
        None: from anyone)."""
        out = []
        for row, row_token in self.rows(addr):
            if (row.get("hash") or "").lower() != tx_hash or row_token != token:
                continue
            if (row.get("to") or "").lower() != addr or _failed(row):
                continue
            if payer and row.get("from") and row["from"].lower() != payer:
                continue
            if _raw(row) == raw:
                out.append((_value(row, token), row.get("tokenSymbol") or ""))
        return out

    def swap_outputs(self, addr, tx_hash, spent_token):
        """[(token or None, value, symbol)] of every payment to ``addr`` in ``tx_hash`` in
        another asset than ``spent_token``, in row order."""
        out = []
        for row, token in self.rows(addr):
            if (row.get("hash") or "").lower() != tx_hash:
                continue
            if (row.get("to") or "").lower() != addr or token == spent_token:
                continue
            if _failed(row):
                continue
            value = _value(row, token)
            if value > 0:
                out.append((token, value, row.get("tokenSymbol") or ""))
        return out

    def swap_output(self, addr, tx_hash, spent_token):
        for row, token in self.rows(addr):
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


def outflow_groups(terminal: dict) -> list[tuple[str, list[dict]]]:
    """The outflows listed under an order_unknown end point, as (why, outflows): those in
    the transaction of the arrival they may have spent (order inside one transaction
    unknown) apart from the other same-block ones (order against the arrival unknown)."""
    outflows = terminal.get("outflows") or []
    same = [o for o in outflows if o.get("same_tx")]
    other = [o for o in outflows if not o.get("same_tx")]
    groups = [(ORDER_SAME_TX, same), ("order against the arrival unknown", other)]
    return [(why, group) for why, group in groups if group]


def _pick_inflow(hist, tx_hash, sender=None, sub=None, amount=None):
    """(event index or None, ambiguous) of the inflow of ``tx_hash`` that carries the
    traced funds. One inflow of that transaction is it; of several, the ones from
    ``sender``, then at place ``sub`` (the same explorer row as the outflow that paid
    it), then of ``amount`` are kept while any match. Still several: the earliest, with
    ``ambiguous`` True (which one is traced is not known)."""
    found = hist["inflows"].get(tx_hash) or []
    if len(found) <= 1:
        return (found[0] if found else None), False
    events = hist["events"]
    tests = []
    if sender:
        tests.append(lambda i: events[i]["from"] == sender)
    if sub is not None and sub[1] is not None:
        tests.append(lambda i: events[i].get("sub") == sub)
    if amount:
        tests.append(lambda i: abs(events[i]["value"] - amount) <= 1e-6 * amount)
    for keep in tests:
        narrowed = [i for i in found if keep(i)]
        found = narrowed or found
        if len(found) == 1:
            return found[0], False
    return found[0], True


def _sub(row, normal, token):
    """Position of a row inside its transaction (see allocation.sub_order): the
    top-level value of a normal transaction, an internal transfer's traceId path (or,
    without one, Blockscout's flat trace ``index``), a token transfer's logIndex; the place
    is None when the row does not carry it."""
    if token:
        try:
            return (_allocation.LOG, int(row["logIndex"]))
        except (KeyError, TypeError, ValueError):
            return (_allocation.LOG, None)
    if normal:
        return (_allocation.TOP, ())
    path = _allocation.trace_path(row.get("traceId"))
    if path is None:
        # Blockscout lists internal calls with a flat trace index instead of a traceId
        try:
            return (_allocation.FLAT, int(row["index"]))
        except (KeyError, TypeError, ValueError):
            pass
    return (_allocation.INTERNAL, path)


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
        if _failed(row) or not row.get("to"):
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
            "sub": _sub(row, normal, token),
            "value": value,
            "raw": _raw(row),
            "input": row.get("input") or "",
            "symbol": row.get("tokenSymbol") or "",
        }
        if receiver == addr:
            events.append({**base, "kind": "in", "from": sender})
        if sender == addr:
            events.append({**base, "kind": "out", "to": receiver})
    _unmix_places(events)
    return events, complete, index


def _unmix_places(events):
    """A transaction whose internal rows place their calls in more than one way (traceId
    paths, flat trace indexes, or no place at all) has no order between them that a sort
    can keep (an unplaced outflow sorts last only among its own kind): all its internal
    places become unknown (order inside one transaction unknown)."""
    internal = (_allocation.INTERNAL, _allocation.FLAT)
    kinds = {}
    for ev in events:
        if ev["sub"][0] in internal:
            kind = None if ev["sub"][1] is None else ev["sub"][0]
            kinds.setdefault(ev["hash"], set()).add(kind)
    mixed = {tx for tx, seen in kinds.items() if len(seen) > 1}
    for ev in events:
        if ev["hash"] in mixed and ev["sub"][0] in internal:
            ev["sub"] = (_allocation.INTERNAL, None)


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
    if how == AMBIGUOUS_IN_TX:
        return (
            f"arrival transaction {result.get('arrival_tx')} holds several inflows that can "
            "carry the traced amount and which one is the traced one is not known: its place "
            "inside the transaction is unknown and nothing at the start address reads as certain"
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
    if info.get("dest_recipient") and addresses:
        parts.append(f"recipient {info['dest_recipient']}")
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
    """Name of the destination transaction: "L2 tx" for a canonical credit, "mint tx" for
    a CCTP mint, else "fill"."""
    if info.get("state") == _canonical.CREDITED:
        return "L2 tx"
    return "mint tx" if info.get("state") == _cctp.MINTED else "fill"


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
    elif info.get("dest_chain"):
        head += f" {arrow} {info['dest_chain']}"  # a chain outside the networks registry
    parts = []
    if info.get("request_id"):
        parts.append(f"request {info['request_id']}")
    if info.get("memo"):
        parts.append(f"memo {info['memo']}")
    if info.get("maker"):
        parts.append(f"to {info['maker']}")
    if info.get("reference"):
        parts.append(f"reference {info['reference']}")
    if info.get("deposit_id") is not None:
        parts.append(f"deposit id {info['deposit_id']}")
    if info.get("message_num") is not None:
        parts.append(f"message {info['message_num']}")
    if info.get("nonce") is not None:
        parts.append(f"nonce {info['nonce']}")
    if info.get("via"):
        parts.append(f"via {info['via']}")
    if info.get("swapped_before"):
        swapped = info["swapped_before"]
        token = swapped["input_token"]
        if swapped.get("input_symbol"):
            token = f"{swapped['input_symbol']} ({token})"
        parts.append(f"swapped to {token} before the deposit")
    if info.get("handler"):
        paid = info.get("paid_on") or []
        text = f"paid to the multicall handler {info['handler']}"
        if paid:
            text += ", paid on to " + ", ".join(p["to"] for p in paid)
        parts.append(text)
    if info.get("l1_token"):
        token = f"token {info['l1_token']}"
        if info.get("l2_token"):
            token += f" (L2 {info['l2_token']})"
        parts.append(token)
    parts += _recipient_parts(info, addresses=recipients)
    state = info.get("state") if state else None
    if state in (_bridges.FILLED, _canonical.CREDITED, _cctp.MINTED):
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


def _sent_by(d, addr, receipt):
    """True when ``addr`` made the call behind deposit ``d``: it sent the transaction, or
    the deposit lies in a successful ERC-4337 user operation of ``addr`` (see
    bridges.user_operations)."""
    if d.get("tx_from") == addr:
        return True
    return _bridges.operation_of(receipt, addr, d.get("log_index")) is not None


def _span(d, addr, receipt):
    """(start, end) log-index bounds of the call behind ``d``: the whole transaction when
    ``addr`` sent it, else the user operation of ``addr`` holding the deposit, else None."""
    if d.get("tx_from") == addr:
        return -1, None
    op = _bridges.operation_of(receipt, addr, d.get("log_index"))
    return (op["start"], op["end"]) if op else None


def _sole_inflow(d, addr, token, raw, to, receipt, native=False):
    """No ``token`` reached ``to`` in the call behind ``d`` except the traced transfer.

    The call is the whole transaction, or the user operation of ``addr`` holding the
    deposit. The traced transfer's own Transfer log (from ``addr``, exactly ``raw``) is
    allowed once; any other inflow of that token into ``to`` fails the check. Inside a
    user operation the traced token transfer (``native`` False) must itself be logged in
    that operation, so a deposit in another operation of the same account is not taken."""
    op = (
        None
        if d.get("tx_from") == addr
        else _bridges.operation_of(receipt, addr, d.get("log_index"))
    )
    start, end = (op["start"], op["end"]) if op else (-1, None)
    inflows = _bridges.token_inflows(receipt, token, to, start, end)
    own = [i for i in inflows if i[0] == addr and i[1] == raw]
    if own:
        inflows.remove(own[0])
    elif op is not None and not native:
        return False
    return not inflows


def _handled_by(d, addr, to, receipt, paid_native=None):
    """The contract ``to`` made deposit ``d``'s payment into the SpokePool.

    A Transfer of the deposit's input token from ``to`` to the SpokePool of exactly the
    input amount, inside the call behind the deposit (_span). A deposit of the native
    coin (no Transfer of the wrapped token into the SpokePool at all) leaves no such log,
    so it is accepted only when ``to`` is known to have made it: ``addr`` sent the
    transaction to ``to``, or, inside a user operation of ``addr`` (a batch that may also
    hold the account's own deposit beside an unrelated payment to ``to``), the deposit is
    in ``to``'s name or ``to`` paid the SpokePool exactly the input amount by an internal
    transfer in the transaction (``paid_native(to, spoke, amount)``; internal transfers
    carry no log index, so this is per transaction, not per user operation)."""
    span = _span(d, addr, receipt)
    if span is None:
        return False
    token = d["input_token"]
    paid = _bridges.token_inflows(receipt, token, d["spoke"], *span)
    if any(frm == to and amount == d["input_amount"] for frm, amount, _i in paid):
        return True
    native = _bridges.WRAPPED_NATIVE.get(d.get("origin_chain_id"))
    if paid or token != native:
        return False
    if d.get("tx_from") == addr:
        return d.get("tx_to") == to
    if d.get("depositor") == to:
        return True
    return bool(paid_native and paid_native(to, d["spoke"], d["input_amount"]))


def _router_tie(d, addr, token, raw, to, receipt, native=False):
    """The stronger tie for a deposit the contract ``to`` made in its own name.

    ``addr`` called ``to`` itself (it sent the transaction and ``to`` is the
    transaction's ``to``, or the deposit is in a successful user operation of ``addr``)
    and no other ``token`` reached ``to`` in that call (see _sole_inflow)."""
    if d.get("tx_from") == addr:
        if d.get("tx_to") != to:
            return False
    elif _bridges.operation_of(receipt, addr, d.get("log_index")) is None:
        return False
    return _sole_inflow(d, addr, token, raw, to, receipt, native)


def _tie(deposits, addr, token, ev, consumed, receipt=None, native=False):
    """The Across deposit that IS the transfer ``ev`` of ``addr``: (deposit or None, untied).

    Same input token as the traced asset, not taken by an earlier transfer of this
    replay (``consumed``: (tx hash, log index)), and tied by amount:

    * a transfer straight to the SpokePool: the depositor is ``addr`` and the deposit's
      input amount equals the transfer;
    * through a periphery, router, aggregator or unlisted contract: the input amount is
      at most the transfer and at least (1 - TIE_FEE) of it (the contract may keep a
      fee first; the stored fixtures show 1.0 % for LI.FI and 0 % for the periphery),
      and the depositor is ``addr`` or the contract the transfer went to. A contract
      depositing in its own name is taken only on the stronger tie of _router_tie:
      ``addr`` called it directly (sent the transaction to it, or a user operation of
      ``addr`` holds the deposit) and no other inflow of the token reached it in that
      call. A deposit made in the name of a third address is never taken: the sender
      may be a relayer or batcher for someone else.

    ``receipt`` is the transaction's receipt (user operations and token inflows).
    ``untied`` is True when the transaction holds a deposit of this token by ``addr``
    (depositor, sender, or a user operation of ``addr``) that is not tied to this
    transfer.
    """
    raw, to = ev.get("raw") or 0, ev["to"]
    mine = [
        d
        for d in deposits
        if d["input_token"] == token and (d["depositor"] == addr or _sent_by(d, addr, receipt))
    ]

    def tied(d):
        if (d["tx_hash"], d["log_index"]) in consumed:
            return False
        if to == d["spoke"]:
            return d["depositor"] == addr and d["input_amount"] == raw
        if not 0 < d["input_amount"] <= raw or raw - d["input_amount"] > TIE_FEE * raw:
            return False
        if d["depositor"] == addr:
            return True
        return d["depositor"] == to and _router_tie(d, addr, token, raw, to, receipt, native)

    ok = [d for d in mine if tied(d)]
    if ok:
        return min(ok, key=lambda d: raw - d["input_amount"]), False
    return None, bool(mine)


def _tie_swapped(deposits, addr, token, ev, consumed, receipt=None, native=False, paid_native=None):
    """An Across deposit of ANOTHER token made from this transfer: (deposit or None, untied).

    A periphery, router or aggregator may swap the traced token first and deposit what
    it bought (swap-and-bridge). The amounts are in different tokens and cannot be
    compared, so the tie is by who and what instead:

    * the transfer went to a contract, not to the SpokePool;
    * exactly one deposit in the transaction has an input token other than the traced
      one, is not taken yet, and is in the name of ``addr`` or of that contract (or was
      sent by ``addr``);
    * its depositor is ``addr``, or the contract with the stronger tie of _router_tie;
    * the traced transfer is the only inflow of the traced token into that contract in
      the call (_sole_inflow): the swap spent this transfer and nothing else;
    * that contract paid the deposit (_handled_by): a Transfer of the deposited token
      from it to the SpokePool of exactly the input amount in the same call, or, for a
      native-coin deposit, the traced address sent the transaction to that contract or,
      inside its user operation, the deposit is in the contract's name. A deposit the
      address made itself beside an unrelated payment in one batch is therefore not
      taken.

    The whole transfer is then taken as the deposit, and the trace goes on in the token
    and amount the fill paid. ``untied`` is True when such a deposit exists but fails a
    check (several, a third depositor, another inflow, or the call not made by ``addr``).
    """
    raw, to = ev.get("raw") or 0, ev["to"]
    found = [
        d
        for d in deposits
        if d["input_token"] != token
        and to != d["spoke"]
        and (d["depositor"] in (addr, to) or _sent_by(d, addr, receipt))
    ]
    free = [d for d in found if (d["tx_hash"], d["log_index"]) not in consumed]
    if not found:
        return None, False
    if len(free) != 1:
        return None, True
    d = free[0]
    if d["depositor"] == addr:
        ok = _sole_inflow(d, addr, token, raw, to, receipt, native)
    else:
        ok = d["depositor"] == to and _router_tie(d, addr, token, raw, to, receipt, native)
    ok = ok and _handled_by(d, addr, to, receipt, paid_native)
    return (d, False) if ok else (None, True)


def _looks_like_contract(edge, token, is_contract):
    known = is_contract(edge["to"]) if is_contract else None
    if known is not None:
        return known
    # Without an endpoint: a native transfer carrying call data is a contract
    # call; a token transfer is checked for a same-transaction payment back.
    return bool(token) or edge["input"] not in ("", "0x")


def _position(chain, ev, order, ts):
    """Time-order key of one event: timestamp, chain, block, index, position inside the
    transaction (allocation.sub_key), inflow first, hash.

    ``ts`` never decreases with the block inside one chain (see ``order_all``), so
    inside a chain this is the order of allocation.sort_key; chains interleave by
    timestamp.
    """
    if order is None:
        order = -1 if ev["kind"] == "in" else _AFTER
    kind = 0 if ev["kind"] == "in" else 1
    sub = _allocation.sub_key(ev.get("sub"), ev["kind"])
    return (ts, chain if chain is not None else -1, ev["block"], order, sub, kind, ev["hash"])


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


def _precedes(out_at, credit_at) -> bool:
    """An outflow at ``out_at`` is known to come before a credit at ``credit_at``
    (positions (block, index or None, hash, sub); None: unknown; see
    allocation.compare: inside one transaction only comparable positions order)."""
    if out_at is None or credit_at is None:
        return False
    return _allocation.compare(out_at, credit_at) == -1


def _held_before(claims, number):
    """Credits held by messages numbered below ``number``: the ones it may not take."""
    return {tx for tx, (n, _key) in claims.items() if n < number}


def _at(position):
    """(block, transaction index or None, hash, sub) of a heap position, for FIFO order."""
    order = position[3]
    index = None if order in (-2, -1, _AFTER) else order
    return (position[2], index, position[6] or None, _allocation.unsub(position[4]))


def _credit_position(ts, chain, block, tx_hash):
    """A credit on a destination chain: the start of its block, place unknown."""
    return (ts, chain, block, -1, _allocation.sub_key(None, "in"), 0, tx_hash)


def _landed(position, ev):
    """Where an output paid back in the transaction of ``ev`` lands: its place inside the
    transaction is not known (a swap or a same-chain fill)."""
    return (*position[:4], _allocation.sub_key(None, "in"), 0, ev["hash"])


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

    def __init__(self, tracer, lazy=None):
        self.t = tracer
        self.edges = []  # (edge, ledger key, raw bounds)
        self.ends = {}  # (chain, address, asset, reason) -> end point (raw amount)
        self.frontier = {}  # ledger key -> {"amount", "hop", "at"}
        self.ledgers = {}
        self.pending = {}  # (key, event index) -> [(amount, lo, hi, tag)]
        self.waiting = {}  # key -> heap of event indexes with pending traced funds
        self.credits = {}  # key -> {block of the inflow: most traced funds credited}
        self.hops = {}  # key -> hop of the shortest branch credited to it
        self.credit_at = {}  # key -> [(block, index or None) or None] of every credit
        self.shared = set()  # (key, event index) of outflows FIFO gave traced funds
        self.done = {}  # key -> events processed
        self.sched = {}  # key -> the event index queued for it (None: not queued)
        self.heap = []
        self.seq = 0
        self.now = ()  # order key of the item being replayed
        self.consumed = set()  # Across deposits taken by a transfer: (tx, log index)
        # (key, event index) of traced inflows that are one of several of their transaction
        # that can carry the funds (_pick_inflow): place unknown, the ledger incomplete
        self.unsure = set()
        # lazy: an address holding no traced funds is not queued; its events are only
        # caught up (cached arithmetic: no outflow of it can carry traced funds) when
        # traced funds reach it or the replay ends. Same results as queueing every event.
        self.lazy = _LAZY if lazy is None else lazy
        self.stats = {"queued": 0, "caught_up": 0}

    def _push(self, position, item):
        """Queue a synthetic credit (after every event at an equal position)."""
        self.seq += 1
        heapq.heappush(self.heap, ((position, (1, self.seq)), item))

    def _schedule(self, key, index):
        """Queue event ``index`` of ``key`` unless an earlier one is queued already."""
        hist = self.t.histories[key]
        if index >= len(hist["events"]):
            return
        queued = self.sched.get(key)
        if queued is not None and queued <= index:
            return
        self.sched[key] = index
        heapq.heappush(self.heap, (hist["keys"][index], ("event", key, index)))

    def _next(self, key):
        """Queue what ``key`` needs next: its next event while it holds traced funds
        (always, when not lazy), else the first event that has traced funds pending."""
        if not self.lazy or self.ledgers[key].lots:
            self._schedule(key, self.done[key])
            return
        waiting = self.waiting.get(key)
        while waiting and waiting[0] < self.done[key]:
            heapq.heappop(waiting)
        if waiting:
            self._schedule(key, waiting[0])

    def catch_up(self, key, until=None):
        """Replay the events of ``key`` ordered before ``until`` (all: None) that the lazy
        queue skipped. The address holds no traced funds and none are pending for these
        events, so each is plain arithmetic on its own ledger (cached per state)."""
        hist = self.t.histories[key]
        done = self.done[key]
        end = len(hist["events"]) if until is None else bisect.bisect_left(hist["keys"], until)
        if end <= done:
            return
        ledger = self.ledgers[key]
        state = (ledger.balance, ledger.t_lo, ledger.t_hi, ledger.traced_total, ledger.incomplete)
        cache = self.t.catch_up_cache.setdefault(key, {})
        found = cache.get((done, end, state))
        if found is None:
            for i in range(done, end):
                ev = hist["events"][i]
                if ev["block"] in hist["ambiguous"] and ledger.traced_total > 0:
                    ledger.mark_incomplete()
                if ev["kind"] == "in":
                    ledger.inflow(ev["value"])
                else:
                    ledger.outflow(ev["value"], at=ev["at"])
            found = (
                ledger.balance,
                ledger.t_lo,
                ledger.t_hi,
                ledger.traced_total,
                ledger.incomplete,
            )
            cache[(done, end, state)] = found
        else:
            ledger.balance, ledger.t_lo, ledger.t_hi, ledger.traced_total = found[:4]
            ledger.incomplete = found[4]
        self.stats["caught_up"] += end - done
        self.done[key] = end

    # -- end states (the one place they are recorded) -----------------------------

    def stop(self, addr, asset, amount, hop, reason, chain, bridge=None, outflows=None):
        if amount <= self.t.noise:
            return  # rounding noise of the FIFO arithmetic, not funds
        key = (chain, addr, asset, reason)
        end = self.ends.get(key)
        if outflows:
            seen = end.setdefault("outflows", []) if end else []
            seen += [o for o in outflows if o not in seen]
        if end is None:
            self.ends[key] = {
                "address": addr,
                "chain": chain,
                "asset": asset,
                "amount": amount,
                "hop": hop,
                "reason": reason,
            }
            if outflows:
                self.ends[key]["outflows"] = seen
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
        sub = _allocation.unsub(position[4]) if len(position) > 6 else None
        index, unsure = _pick_inflow(hist, tx_hash, sender=tag_from, sub=sub)
        if unsure:
            self.unsure.add((key, index))
        self.catch_up(key, self.now)
        if index is None:
            # the inflow is not in the recipient's history: count it where it happened
            self._push(position, ("synthetic", key, lots, _at(position)))
        elif index < self.done[key]:
            self.late_credit(key, index, lots)
            self._next(key)
        else:
            self.pending.setdefault((key, index), []).extend(lots)
            heapq.heappush(self.waiting.setdefault(key, []), index)
            self._next(key)

    def late_credit(self, key, index, lots):
        """Credit an inflow the replay already passed (same position, or out of order).

        When an outflow of the address was replayed in between, or the credit makes
        the traced pool exceed the balance, the order is not known: the ledger is
        marked incomplete, so nothing on it reads as certain.
        """
        hist, ledger = self.t.histories[key], self.ledgers[key]
        ev = hist["events"][index]
        at = self.arrived_at(key, index)
        self.credit(key, ev["block"], lots, at)
        skipped = any(e["kind"] == "out" for e in hist["events"][index + 1 : self.done[key]])
        if skipped or ev["block"] in hist["ambiguous"] or (key, index) in self.unsure:
            ledger.mark_incomplete()
        for amount, lo, hi, tag in lots:
            ledger.add_traced(amount, tag, lo=lo, hi=hi, at=at)
        if ledger.t_lo > ledger.balance + 1e-9 * max(1.0, ledger.balance):
            ledger.mark_incomplete()

    def arrived_at(self, key, index):
        """Where traced funds credited to inflow ``index`` of ``key`` sit for FIFO: the
        inflow's position, its place inside the transaction unknown when it was one of
        several inflows of that transaction that can carry them (see _pick_inflow)."""
        at = self.t.histories[key]["events"][index]["at"]
        return (*at[:3], None) if (key, index) in self.unsure else at

    def credit(self, key, block, lots, at=None):
        """Remember the traced funds credited at ``block``, for widening (see run), the
        shortest branch that reached ``key`` and where the credit sits (``at``: (block,
        index or None); None: position unknown)."""
        self.credit_at.setdefault(key, []).append(at)
        blocks = self.credits.setdefault(key, {})
        blocks[block] = blocks.get(block, 0.0) + sum(lot[2] for lot in lots)
        hop = min(lot[3][0] for lot in lots)
        self.hops[key] = min(self.hops.get(key, hop), hop)

    # -- the replay -------------------------------------------------------------

    def run(self):
        t = self.t
        for key, hist in t.histories.items():
            complete = hist["complete"] and key not in t.incomplete_keys
            self.ledgers[key] = _allocation.Ledger(complete=complete)
            self.done[key] = 0
            self._next(key)
        start = t.start_key
        t.locate_arrival()  # order_all may have re-sorted the start history since
        seed = [(t.amount, t.amount, t.amount, (0, t.arrival["tx"], t.arrival["from"]))]
        if t.arrival.get("unsure"):
            self.unsure.add((start, t.arrival["index"]))
        if t.arrival["index"] is not None:
            self.pending[(start, t.arrival["index"])] = seed
            heapq.heappush(self.waiting.setdefault(start, []), t.arrival["index"])
            self._next(start)
        else:
            self._push(t.synthetic_position(), ("synthetic", start, seed, None))
        while self.heap:
            self.now, item = heapq.heappop(self.heap)
            if item[0] == "synthetic":
                _kind, key, lots, at = item
                self.catch_up(key, self.now)
                self.credit(key, at[0] if at else t.start_block, lots, at)
                ledger = self.ledgers[key]
                ledger.balance += sum(lot[2] for lot in lots)
                for amount, lo, hi, tag in lots:
                    ledger.add_traced(amount, tag, lo=lo, hi=hi, at=at)
                self._next(key)
                continue
            _kind, key, i = item
            if self.sched.get(key) != i or i < self.done[key]:
                continue  # replaced by an earlier queued event of this address
            self.sched[key] = None
            self.catch_up(key, self.now)
            self.stats["queued"] += 1
            self.event(key, i)
            self._next(key)
        for key in t.histories:
            self.catch_up(key)
        for key, ledger in self.ledgers.items():
            left = ledger.traced_left
            if left > 0 and ledger.lots:
                hop = min(lot[1][0] for lot in ledger.lots)
                # what a same-block outflow of unknown order may have taken: named, with
                # those outflows (FIFO could not give it to them, see allocation.outflow)
                maybe, refs = ledger.unordered()
                maybe = min(maybe, left)
                if maybe > t.noise:
                    events = t.histories[key]["events"]
                    # an outflow in the transaction of a lot it may have taken: the order
                    # inside that transaction is what is not known
                    same = {
                        r for lot in ledger.lots for r in lot[4] if lot[1][1] == events[r]["hash"]
                    }
                    shown = []
                    for i in refs:
                        o = {
                            "to": events[i]["to"],
                            "tx_hash": events[i]["hash"],
                            "value": round(events[i]["value"], 6),
                        }
                        if i in same:
                            o["same_tx"] = True
                        shown.append(o)
                    self.stop(key[1], key[2], maybe, hop, ORDER_UNKNOWN, key[0], outflows=shown)
                    if same:
                        self.ends[(key[0], key[1], key[2], ORDER_UNKNOWN)]["order_note"] = (
                            ORDER_SAME_TX
                        )
                    left -= maybe
                why = "not moved on (held or untraced)" if left >= t.dust else BELOW_THRESHOLD
                self.stop(key[1], key[2], left, hop, why, key[0])
        for key, seen in self.frontier.items():
            self.stop(key[1], key[2], seen["amount"], seen["hop"], "trace budget", key[0])
        return self

    def event(self, key, i):
        """Replay event ``i`` of ``key``: credit its traced funds or spend them FIFO."""
        t = self.t
        hist = t.histories[key]
        ev = hist["events"][i]
        self.done[key] = i + 1
        ledger = self.ledgers[key]
        unknown_order = ev["block"] in hist["ambiguous"]
        if ev["kind"] == "in":
            lots = self.pending.pop((key, i), [])
            at = self.arrived_at(key, i)
            if lots:
                self.credit(key, ev["block"], lots, at)
                if (key, i) in self.unsure:
                    ledger.mark_incomplete()  # which inflow carried them is not known
            ledger.inflow(ev["value"])
            if sum(lot[2] for lot in lots) > ev["value"] * (1 + 1e-9) + 1e-12:
                ledger.mark_incomplete()  # more traced than the inflow carried
            for amount, lo, hi, tag in lots:
                ledger.add_traced(amount, tag, lo=lo, hi=hi, at=at)
            if unknown_order and ledger.traced_total > 0:
                ledger.mark_incomplete()
            return
        if unknown_order and ledger.traced_total > 0:
            ledger.mark_incomplete()
        pool = ledger.traced_left
        row = ledger.outflow(ev["value"], at=ev["at"], ref=i)
        if row["fifo"] <= 0:
            return
        self.shared.add((key, i))
        if pool >= t.dust or t.is_tornado(key[0], ev["to"]):
            self.outflow(key, ev, row, hist["positions"][i])
        else:
            # what is left here is under the threshold: not followed, but kept
            hop = min(tag[0] for tag, _a in row["taken"]) + 1
            self.stop(ev["to"], key[2], row["fifo"], hop, BELOW_THRESHOLD, key[0])

    def outflow(self, key, ev, row, position):
        """One traced outgoing transfer: an edge, then where its funds go."""
        t = self.t
        chain, addr, asset = key
        used = row["fifo"]
        hop = min(tag[0] for tag, _a in row["taken"]) + 1
        if asset and ev["symbol"] and t.names.get((chain, asset), asset) == asset:
            t.names[(chain, asset)] = ev["symbol"]  # the symbol replaces the bare address
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
            "asset_address": asset,  # the token contract (None: the native coin)
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
            # Only a payment of material size makes it a swap (see _Tracer.payback): a
            # native payment back up to the value the sender attached is a refund, and
            # then, unless an Across deposit is tied, the branch ends as "aggregator
            # <protocol> (bridge or swap unknown)".
            unknown = False
            if listed["kind"] == "aggregator":
                swap = t.payback(chain, addr, ev, asset)
                if swap is not None and swap is not LOOKUP_FAILED:
                    receipt = t.receipt(chain, ev["hash"])
                    if receipt is LOOKUP_FAILED:
                        swap = LOOKUP_FAILED
                    elif _bridges.bridge_emitters(receipt, chain, t.table, exclude=(to,)):
                        swap = None
                    elif swap == REFUND_ONLY:
                        swap, unknown = None, True
                if swap is LOOKUP_FAILED:
                    self.stop(to, asset, used, hop, LOOKUP_FAILED, chain)
                    return
                if swap is not None:
                    self.swap(edge, ev, key, row, position, swap)
                    return
            self.bridge(edge, ev, key, used, hop, row, listed, position, unknown=unknown)
            return
        if _cow.is_settlement(chain, to):
            # a CoW Protocol settlement: the owner's trade in the receipt, then its payout
            self.cow(edge, ev, key, row, position, used, hop)
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
            if t.follow and self.bridge(edge, ev, key, used, hop, row, None, position):
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
        edge["swapped_to_address"] = new_asset
        # The swap output is scaled by the share of the input this trace owns.
        scaled = _lots_by_hop(row, received / ev["value"])
        landed = _landed(position, ev)
        self.arrive((chain, addr, new_asset), scaled, ev["hash"], addr, ev["hash"], landed)

    def cow(self, edge, ev, key, row, position, used, hop):
        """A transfer into CoW Protocol's GPv2Settlement, read as the owner's order.

        Tied by a ``Trade`` of the settlement in the same receipt whose owner is the traced
        address, whose sell token is the traced token and whose sell amount is exactly the
        transfer, not taken by an earlier transfer of this replay. The order's output is
        the buy-token payout of exactly ``buyAmount`` from the settlement (see
        swaps_cow.payout_for; a native payout: a payment from the settlement to the owner,
        each one taken once), read in the receiver's history: followed as
        a swap from the receiver. Otherwise the branch ends at the settlement by name.
        """
        t = self.t
        chain, addr, asset = key
        to, raw = ev["to"], ev.get("raw") or 0
        receipt = t.receipt(chain, ev["hash"], what="swap receipt (cow settlement)")
        if receipt is LOOKUP_FAILED:
            self.stop(to, asset, used, hop, LOOKUP_FAILED, chain)
            return
        trades = _cow.decode_trades(receipt, to)
        info = {"protocol": _cow.COW, "trades": len(trades)}
        tied = None
        for tr in trades if asset is not None else []:
            if (_cow.COW, ev["hash"], tr["log_index"]) in self.consumed:
                continue
            if tr["owner"] == addr and tr["sell_token"] == asset and tr["sell_amount"] == raw:
                tied = tr
                break
        if tied is None:
            info["state"] = _cow.NOT_TIED
            edge["swap"] = info
            self.stop(to, asset, used, hop, "swap cow (trade not tied to this transfer)", chain)
            return
        self.consumed.add((_cow.COW, ev["hash"], tied["log_index"]))
        info.update({k: tied[k] for k in ("owner", "order_uid", "sell_token", "buy_token")})
        info.update({k: tied[k] for k in ("sell_amount", "buy_amount", "fee_amount")})
        buy, pay, mark = tied["buy_token"], None, None
        if buy == _cow.NATIVE:
            # paid by an internal transaction, not in the receipt: only the owner's is read
            new_asset, receiver = None, addr
        else:
            new_asset = buy
            taken = {m[3] for m in self.consumed if m[:3] == (_cow.COW, ev["hash"], "payout")}
            pay = _cow.payout_for(tied, trades, _cow.payouts(receipt, to, buy), addr, taken)
            receiver = pay["to"] if pay else None
        found = None
        if receiver is not None:
            seen = t.payment_lookup(chain, receiver, ev["hash"], new_asset, tied["buy_amount"], to)
            if seen is LOOKUP_FAILED:
                self.stop(to, asset, used, hop, LOOKUP_FAILED, chain)
                return
            if pay is not None:
                found = seen[0] if seen else None
                mark = (_cow.COW, ev["hash"], "payout", pay["log_index"])
            else:
                # each native payment pays one trade
                for i, item in enumerate(seen):
                    native = (_cow.COW, ev["hash"], "native", receiver, tied["buy_amount"], i)
                    if native not in self.consumed:
                        found, mark = item, native
                        break
        if found is None:
            info["state"] = _cow.PAYOUT_NOT_FOUND
            edge["swap"] = info
            reason = f"swap cow (order {tied['order_uid']}, payout not found)"
            self.stop(to, asset, used, hop, reason, chain)
            return
        self.consumed.add(mark)
        received, symbol = found
        if new_asset and symbol:
            t.names.setdefault((chain, new_asset), symbol)
        info.update(state=_cow.SETTLED, receiver=receiver)
        edge["kind"] = "swap"
        edge["swapped_to"] = t.name(chain, new_asset)
        edge["swapped_to_address"] = new_asset
        edge["swap"] = info
        if receiver != addr:
            edge["paid_to"] = receiver
        scaled = _lots_by_hop(row, received / ev["value"])
        landed = _landed(position, ev)
        dest_key = (chain, receiver, new_asset)
        self.arrive(dest_key, scaled, ev["hash"], addr, ev["hash"], landed)

    def bridge(self, edge, ev, key, used, hop, row, listed, position=None, unknown=False):
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
        if listed is not None and listed["protocol"] == _cctp.CCTP:
            # a Circle CCTP burn: followed to the mint on an EVM destination
            if self.cctp(edge, ev, key, row, end):
                return True
            return end(*_not_followed(listed))
        if listed is not None and listed["protocol"] in _named.PROTOCOLS:
            # recognised and decoded, ended with its own state (Mayan's same-chain payout
            # goes on); nothing tied to this transfer: named only
            if self.named(edge, ev, key, row, listed, end, position):
                return True
            return end(*_not_followed(listed))
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
        receipt = t.receipt(chain, ev["hash"])  # cached: read by receipt_deposits
        source_token = asset or _bridges.WRAPPED_NATIVE.get(chain)
        native = asset is None
        deposit, untied = _tie(deposits, addr, source_token, ev, self.consumed, receipt, native)
        swapped = False
        if deposit is None and not untied:
            # a swap before the deposit: the deposit is in the token the contract bought
            deposit, untied = _tie_swapped(
                deposits,
                addr,
                source_token,
                ev,
                self.consumed,
                receipt,
                native,
                lambda frm, spoke, amount: t.paid_native(chain, frm, ev["hash"], spoke, amount),
            )
            swapped = deposit is not None
        if deposit is None:
            if listed is None:
                # a token with its own (unlisted) Arbitrum gateway, confirmed by the
                # listed Inbox's message; else the plain end of an unlisted contract
                return asset is not None and self.gateway(edge, ev, key, row, end)
            if untied:
                # an Across deposit of this address is in the transaction, but it is not
                # this transfer (another amount, already taken, or made for someone else)
                return end(_bridge_info(_bridges.ACROSS, None, _bridges.NOT_TIED), NOT_TIED)
            if unknown:
                # a listed aggregator that paid back only a native refund and logged no
                # bridge event of the table: it may have bridged through a bridge not
                # listed here, or swapped; neither is known
                info = _bridge_info(listed["protocol"], None, _bridges.BRIDGE_OR_SWAP_UNKNOWN)
                info["role"] = listed.get("role", "")
                return end(info, f"aggregator {listed['protocol']} (bridge or swap unknown)")
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
        if swapped:
            info["swapped_before"] = {
                "from_token": source_token,
                "from_symbol": t.symbol(chain, source_token),
                "input_token": deposit["input_token"],
                "input_symbol": t.symbol(chain, deposit["input_token"]),
                "input_amount": deposit["input_amount"],
            }
        if found.get("reason"):
            return end(info, found["reason"])
        dest_chain, fill = found["dest_chain"], found["fill"]
        pays = None
        if _bridges.is_multicall_handler(dest_chain, fill["recipient"]):
            info["handler"] = fill["recipient"]
            pays = t.handler_onward(dest_chain, fill, found["asset"])
        if swapped and pays is LOOKUP_FAILED:
            return end(info, LOOKUP_FAILED)
        if swapped:
            # the whole transfer became the deposit: the trace goes on in the filled token,
            # read in units of the first payee's row (the native coin has 18 decimals)
            first = pays[0] if isinstance(pays, list) and pays else None
            if first is not None:
                payee, raw_paid, token_paid, payer = (
                    first["to"],
                    first["raw"],
                    first["asset"],
                    fill["recipient"],
                )
            else:
                payee, raw_paid, token_paid, payer = (
                    fill["recipient"],
                    fill["output_amount"],
                    found["asset"],
                    None,
                )
            unit = t.unit(dest_chain, payee, fill["tx_hash"], token_paid, raw_paid, payer)
            if unit is LOOKUP_FAILED:
                return end(info, LOOKUP_FAILED)
            if unit is None:
                return end(info, SWAPPED_UNREAD)
            factor = fill["output_amount"] * unit / ev["value"] if ev["value"] else 0.0
        else:
            # The share of the transfer that reached the destination: the fill's output
            # over the larger of the deposit and the transfer (a router or aggregator may
            # keep a fee before it deposits, at most TIE_FEE of the transfer).
            base = max(deposit["input_amount"], ev.get("raw") or 0)
            factor = fill["output_amount"] / base if base else 1.0
        edge["kind"] = "bridge"
        edge["bridge"] = info
        landed = _credit_position(
            fill.get("ts") or ev["ts"], dest_chain, fill["block"], fill["tx_hash"]
        )
        if "handler" in info:
            self.handler(edge, info, key, row, hop, used, found, pays, factor, landed)
            return True
        scaled = _lots_by_hop(row, factor)
        dest_key = (dest_chain, fill["recipient"], found["asset"])
        self.arrive(dest_key, scaled, fill["tx_hash"], addr, fill["tx_hash"], landed)
        return True

    def handler(self, edge, info, key, row, hop, used, found, pays, factor, landed):
        """A fill paid to the Across MulticallHandler: continue from the handler's payees.

        ``pays`` are the handler's payments in the fill transaction (_Tracer.handler_onward):
        token Transfer logs after the fill and, for a fill in the wrapped native coin, the
        handler's native payments. Each payee gets the share of the fill it was paid;
        what the handler did not pay on in these assets ends at the handler
        (HANDLER_REST). No payment, payments above the fill, or several fills to the
        handler in the transaction end the branch at the handler (HANDLER_NOT_TIED).
        """
        _chain, addr, _asset = key
        dest_chain, fill = found["dest_chain"], found["fill"]
        handler, total = fill["recipient"], fill["output_amount"]
        traced = used * factor
        if pays is LOOKUP_FAILED:
            self.stop(handler, found["asset"], traced, hop, LOOKUP_FAILED, dest_chain, bridge=info)
            return
        paid = sum(p["raw"] for p in pays or [])
        if not pays or paid > total:
            self.stop(
                handler, found["asset"], traced, hop, HANDLER_NOT_TIED, dest_chain, bridge=info
            )
            return
        info["paid_on"] = [{"to": p["to"], "amount": p["raw"], "asset": p["asset"]} for p in pays]
        for p in pays:
            scaled = _lots_by_hop(row, factor * p["raw"] / total)
            dest_key = (dest_chain, p["to"], p["asset"])
            self.arrive(dest_key, scaled, fill["tx_hash"], addr, fill["tx_hash"], landed)
        if paid < total:
            rest = traced * (total - paid) / total
            self.stop(handler, found["asset"], rest, hop, HANDLER_REST, dest_chain, bridge=info)

    def named(self, edge, ev, key, row, listed, end, position):
        """A Relay, Mayan, THORChain, Maya, Layerswap, NEAR Intents, Unit or Hyperliquid
        deposit that IS this transfer, ended with its own state (bridges_named.py).

        Plain transfers to a listed maker or treasury (Layerswap, NEAR Intents, Unit) are
        the deposit themselves. Hyperliquid Bridge2: native USDC on Arbitrum, credited to
        the sender. The others are read from the receipt and tied like an Across deposit:
        the log is emitted by the contract the transfer went to, the traced address sent
        the transaction (or is the Relay depositor), the token and the amount are the
        transfer's (a forwarded native call or a Relay receiver payment carries no amount
        in its log: the payment is the transfer itself), and each log is taken once per
        replay. Returns False when nothing is tied (the caller names the bridge).
        """
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        proto, to = listed["protocol"], ev["to"]
        if raw <= 0:
            return False
        if proto in _named.MAKER_PROTOCOLS:
            info = _bridge_info(proto, None, _named.OFF_CHAIN_MATCHING)
            info["maker"] = to
            ref = _named.reference(ev.get("input")) if asset is None else None
            if ref:
                info["reference"] = ref
            if proto == _named.UNIT:
                info["dest_chain"] = "Hyperliquid"
                reason = "bridge unit (to Hyperliquid, recipient assigned off chain)"
            elif proto == _named.LAYERSWAP:
                reason = f"bridge layerswap (maker {to}, matched off chain)"
            else:
                reason = f"bridge near-intents (deposit to {to}, matched off chain)"
            return end(info, reason)
        if proto == _named.HYPERLIQUID:
            if chain != _named.HYPERLIQUID_CHAIN or asset != _named.HYPERLIQUID_USDC:
                return False
            info = _bridge_info(proto, None, _named.CREDITED_OFF_EVM)
            info.update(dest_chain="HyperCore", dest_recipient=addr, amount=raw)
            if raw < _named.HYPERLIQUID_MIN_RAW:
                info["below_minimum"] = True  # under 5 USDC: not credited by the bridge
            return end(info, f"bridge hyperliquid (recipient {addr}, credited on HyperCore)")
        receipt = t.receipt(chain, ev["hash"])
        if receipt is LOOKUP_FAILED:
            return end(_bridge_info(proto, None, _bridges.LOOKUP_FAILED), LOOKUP_FAILED)
        sender = ((receipt or {}).get("from") or "").lower()
        if proto == _named.RELAY:
            return self.relay(ev, key, receipt, sender, end)
        if proto in _named.ROUTER_PROTOCOLS:
            return self.router(ev, key, receipt, sender, end)
        if proto == _named.MAYAN:
            return self.mayan(edge, ev, key, row, receipt, sender, end, position)
        return False

    def relay(self, ev, key, receipt, sender, end):
        """A Relay deposit (depository log or receiver payment) of this transfer."""
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        for d in _named.decode_relay_deposits(receipt, chain, self.t.table):
            mark = (_named.RELAY, ev["hash"], d["log_index"])
            if mark in self.consumed or d["emitter"] != ev["to"]:
                continue
            if d["kind"] == "depository":
                if (
                    d["token"] != asset
                    or d["amount"] != raw
                    or addr not in (d["depositor"], sender)
                ):
                    continue
            elif asset is not None or sender != addr:
                continue
            self.consumed.add(mark)
            info = _bridge_info(_named.RELAY, None, _named.DEST_NOT_ON_CHAIN)
            info.update(kind=d["kind"], request_id=d["request_id"])
            if d["depositor"]:
                info["depositor"] = d["depositor"]
            shown = d["request_id"] or "not decoded"
            return end(info, f"bridge relay (request {shown}, destination not on chain)")
        return False

    def router(self, ev, key, receipt, sender, end):
        """A THORChain or Maya router deposit of this transfer, with its memo decoded."""
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        for d in _named.decode_router_deposits(receipt, chain, self.t.table):
            mark = (d["protocol"], ev["hash"], d["log_index"])
            if mark in self.consumed or d["router"] != ev["to"]:
                continue
            if d["asset"] != asset or d["amount"] != raw or sender != addr:
                continue
            self.consumed.add(mark)
            memo = d["parsed"]
            info = _bridge_info(d["protocol"], _named.memo_dest_chain_id(memo), _named.VAULT_PAYOUT)
            info.update(memo=d["memo"], memo_action=memo["action"], vault=d["vault"])
            pool = _named.memo_pool_fields(memo)
            if pool is not None:
                info.update(pool)  # a pool action pays nothing out: no destination, no recipient
            else:
                if memo["chain"]:
                    info["dest_chain"] = _named.CHAIN_NAMES.get(memo["chain"], memo["chain"])
                if memo["asset"]:
                    info["dest_asset"] = memo["asset"]
                if memo["recipient"]:
                    info["dest_recipient"] = memo["recipient"]
            if memo["refund"]:
                info["memo_refund"] = memo["refund"]
            if memo["affiliate"]:
                info["memo_affiliate"] = memo["affiliate"]
            proto, rec = d["protocol"], memo["recipient"]
            if memo["action"] == "SWAP":
                where = _named.memo_target(memo) or "unknown"
                if rec is None:
                    # an empty destination: the protocol pays the originator (the sender)
                    info["pays_originator"] = True
                    return end(info, f"bridge {proto} (to {where}, pays the originator)")
                return end(info, f"bridge {proto} (to {where}, recipient {rec})")
            if pool is not None:
                return end(info, f"bridge {proto} ({_named.memo_pool_text(memo)})")
            what = memo["action"] or "memo not decoded"
            if memo["recipient"]:
                return end(info, f"bridge {proto} ({what}, recipient {rec})")
            return end(info, f"bridge {proto} ({what})")
        return False

    def mayan(self, edge, ev, key, row, receipt, sender, end, position):
        """What a Mayan forwarder did with this transfer: an order to another chain (named,
        with destination and recipient), or a same-chain MonoChainSwap payout (followed)."""
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        forwards = _named.decode_mayan_forwards(receipt, chain, t.table)
        for d in forwards:
            mark = (_named.MAYAN, ev["hash"], d["log_index"])
            if mark in self.consumed or d["forwarder"] != ev["to"] or sender != addr:
                continue
            if d["token_in"] != asset or d["amount_in"] not in (None, raw):
                continue
            if d["amount_in"] is None and asset is not None:
                continue
            self.consumed.add(mark)
            call = d["call"]
            info = _bridge_info(_named.MAYAN, None, _named.ORDER_NOT_DECODED)
            info.update(event=d["event"], mayan_protocol=d["mayan_protocol"])
            if d["middle_token"]:
                info.update(middle_token=d["middle_token"], middle_amount=d["middle_amount"])
            if call is None:
                info["call_selector"] = d["call_selector"]
                return end(info, f"bridge mayan (via {d['mayan_protocol']}, order not decoded)")
            info["function"] = call["function"]
            if call["same_chain"]:
                # the forward logged before this one in the receipt bounds its payout
                earlier = [
                    f["log_index"]
                    for f in forwards
                    if None not in (f["log_index"], d["log_index"])
                    and f["log_index"] < d["log_index"]
                ]
                d = dict(d, previous_index=max(earlier) if earlier else None)
                return self.mayan_same_chain(edge, ev, key, row, receipt, d, info, end, position)
            info.update(
                state=_named.FULFIL_NOT_FOLLOWED,
                dest_chain_id=call["dest_chain_id"],
                dest_chain=call["dest_chain"],
                dest_recipient=call["recipient"],
            )
            if call.get("token_out"):
                info["token_out"] = call["token_out"]
            if call.get("min_amount_out") is not None:
                info["min_amount_out"] = call["min_amount_out"]
            return end(
                info, f"bridge mayan (to {call['dest_chain']}, recipient {call['recipient']})"
            )
        return False

    def mayan_same_chain(self, edge, ev, key, row, receipt, fwd, info, end, position):
        """A MonoChainSwap payout in the same transaction: continue from its recipient."""
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        call = fwd["call"]
        out = [
            p
            for p in _named.mono_payouts(receipt, fwd["mayan_protocol"])
            if p["to"] == call["recipient"]
            and (_named.MAYAN, ev["hash"], "payout", p["log_index"]) not in self.consumed
        ]
        info["dest_recipient"] = call["recipient"]
        pay = _named.payout_for(out, fwd["log_index"], fwd.get("previous_index"))
        if pay is None:
            info["state"] = _named.PAYOUT_NOT_FOUND
            return end(info, f"bridge mayan (same chain to {call['recipient']}, payout not found)")
        if pay["token"] == asset:
            new_asset, scale, symbol = asset, pay["amount"] / raw, None
        else:
            found = t.swap_lookup(chain, call["recipient"], ev["hash"], asset)
            if found is LOOKUP_FAILED:
                info["state"] = _bridges.LOOKUP_FAILED
                return end(info, LOOKUP_FAILED)
            if found is None or found[0] != pay["token"]:
                info["state"] = _named.PAYOUT_NOT_FOUND
                return end(
                    info, f"bridge mayan (same chain to {call['recipient']}, payout not read)"
                )
            new_asset, received, symbol = found
            scale = received / ev["value"]
        if new_asset and symbol:
            t.names.setdefault((chain, new_asset), symbol)
        # each payout pays one forward (two forwards to the same payee in one transaction)
        self.consumed.add((_named.MAYAN, ev["hash"], "payout", pay["log_index"]))
        info["state"] = _named.SAME_CHAIN
        edge["bridge"] = dict(info)
        edge["paid_to"] = call["recipient"]
        if new_asset != asset:
            edge["kind"] = "swap"
            edge["swapped_to"] = t.name(chain, new_asset)
            edge["swapped_to_address"] = new_asset
        landed = _landed(position, ev)
        dest_key = (chain, call["recipient"], new_asset)
        self.arrive(dest_key, _lots_by_hop(row, scale), ev["hash"], addr, ev["hash"], landed)
        return True

    def cctp(self, edge, ev, key, row, end):
        """A Circle CCTP deposit that IS this transfer, followed to its mint.

        The transfer went to a listed TokenMinter (depositForBurn pulls the tokens there
        and burns them), and the receipt holds a ``DepositForBurn`` of a listed
        TokenMessenger of the same version with the traced address as depositor, the
        traced token as burn token and exactly the transfer's amount, not taken by an
        earlier transfer of this replay. An EVM destination of the networks registry is
        followed from the mint recipient in the minted token (the amount minted: a v2
        fee is kept by Circle); any other destination ends with the recipient shown.
        Returns False when nothing is tied (the caller names the bridge).
        """
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        minter = _bridges.listed(t.table, chain, ev["to"])
        role = _cctp.contract_role(minter) if minter else None
        if raw <= 0 or asset is None or role is None or role[0] != "minter":
            return False
        receipt = t.receipt(chain, ev["hash"])
        if receipt is LOOKUP_FAILED:
            return end(_bridge_info(_cctp.CCTP, None, _bridges.LOOKUP_FAILED), LOOKUP_FAILED)
        dep = None
        for d in _cctp.decode_deposits(receipt, chain, t.table):
            mark = (_cctp.CCTP, ev["hash"], d["log_index"])
            if mark in self.consumed or d["version"] != role[1]:
                continue
            if d["depositor"] == addr and d["burn_token"] == asset and d["amount"] == raw:
                dep = d
                break
        if dep is None:
            return False
        self.consumed.add((_cctp.CCTP, ev["hash"], dep["log_index"]))
        found = t.follow_cctp(chain, dep, ev)
        info = dict(found["info"])
        tx = t.tx_info(chain, ev["hash"])
        call = _cctp.decode_call(tx.get("input")) if isinstance(tx, dict) else None
        if call is not None:
            info["function"] = call["function"]
        if found.get("reason"):
            return end(info, found["reason"])
        edge["kind"] = "bridge"
        edge["bridge"] = info
        mint = found["mint"]
        dest_chain = dep["dest_chain_id"]
        shown = t.name(chain, asset)
        if shown != asset:
            # CCTP mints the currency it burned (USDC to USDC): the symbol carries over
            t.names.setdefault((dest_chain, mint["token"]), shown)
        landed = _credit_position(
            mint["ts"] or ev["ts"], dest_chain, mint["block"], mint["tx_hash"]
        )
        dest_key = (dest_chain, mint["recipient"], mint["token"])
        scaled = _lots_by_hop(row, mint["amount"] / raw)
        self.arrive(dest_key, scaled, mint["tx_hash"], addr, mint["tx_hash"], landed)
        return True

    def canonical(self, edge, ev, key, row, listed, end):
        """An Arbitrum or ZKsync deposit that IS this transfer.

        Tied like an Across deposit: the decoded deposit carries exactly the transfer's
        amount (a legacy ZKsync request: at most it, the rest paying the L2 base cost),
        the traced address is the L1 sender of the message (or sent the transaction;
        for ZKsync the calldata is the traced address's own transaction), and each
        message or call is taken by one transfer at most. A token transfer is an
        Arbitrum gateway deposit (see gateway) or a ZKsync ERC-20 deposit. Returns
        False when nothing is tied (the caller names the bridge).
        """
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        if raw <= 0:
            return False
        if listed["protocol"] == _canonical.ZKSYNC:
            return self.zksync(ev, key, end)
        if listed["protocol"] != _canonical.ARBITRUM:
            return False
        if asset is not None:
            return self.gateway(edge, ev, key, row, end)
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
        landed = _credit_position(credit["ts"], dest_chain, credit["block"], credit["tx_hash"])
        dest_key = (dest_chain, msg["recipient"], None)
        self.arrive(dest_key, scaled, credit["tx_hash"], addr, credit["tx_hash"], landed)
        return True

    def zksync(self, ev, key, end):
        """A ZKsync Bridgehub request, legacy Era request or ERC-20 deposit, named.

        Native: ``requestL2TransactionDirect`` / ``TwoBridges`` whose msg.value is the
        transfer, or a legacy ``requestL2Transaction`` whose l2Value is at most the
        transfer. A token transfer: the transaction (read for its calldata) is the
        traced address's own ``requestL2TransactionTwoBridges`` through a listed second
        bridge, depositing exactly this token and amount. ZKsync chains are not in the
        networks registry: the branch ends with the recipient and the token shown.
        """
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        # one call per transaction and asset (an ERC-20 deposit also carries its ETH fee)
        mark = (_canonical.ZKSYNC, ev["hash"], asset)
        if mark in self.consumed:
            return False
        if asset is None:
            call = _canonical.decode_bridgehub_call(ev["input"])
            if call is not None:
                if call["msg_value"] != raw:
                    return False
            else:
                call = _canonical.decode_legacy_request(ev["input"])
                if call is None or not 0 < call["l2_value"] <= raw:
                    return False
        else:
            tx = t.tx_info(chain, ev["hash"])
            if tx is LOOKUP_FAILED:
                failed = _bridge_info(_canonical.ZKSYNC, None, _bridges.LOOKUP_FAILED)
                return end(failed, LOOKUP_FAILED)
            call = _canonical.decode_bridgehub_call((tx or {}).get("input"))
            second = _bridges.listed(t.table, chain, (call or {}).get("second_bridge"))
            if (
                call is None
                or (tx.get("from") or "").lower() != addr
                or second is None
                or second["protocol"] != _canonical.ZKSYNC
                or call["l1_token"] != asset
                or call["amount"] != raw
            ):
                return False
        self.consumed.add(mark)
        info = _bridge_info(_canonical.ZKSYNC, call["chain_id"], _bridges.DEST_CHAIN_NOT_SUPPORTED)
        info.update(
            function=call["function"],
            l2_recipient=call["l2_recipient"],
            refund_recipient=call["refund_recipient"],
            l2_value=call["l2_value"],
        )
        if "mint_value" in call:
            info["mint_value"] = call["mint_value"]
        if asset is not None:
            info.update(l1_token=call["l1_token"], amount=call["amount"])
        shown = call["l2_recipient"] or "not decoded"
        return end(info, f"bridge zksync (recipient {shown}, chain not supported)")

    def gateway(self, edge, ev, key, row, end):
        """An ERC-20 deposit to Arbitrum through a token gateway that IS this transfer.

        The receipt's gateway deposit (DepositInitiated confirmed by its Inbox message,
        see bridges_canonical.decode_gateway_deposits) is of the traced token, by the
        traced address, of exactly the transfer's amount, and the transfer went to that
        gateway; each message is taken once. Followed on Arbitrum from the L2 recipient
        in the L2 token once the L2 credit is found. Returns False when nothing is tied.
        """
        t = self.t
        chain, addr, asset = key
        raw = ev.get("raw") or 0
        receipt = t.receipt(chain, ev["hash"])
        if receipt is LOOKUP_FAILED:
            failed = _bridge_info(_canonical.ARBITRUM, None, _bridges.LOOKUP_FAILED)
            return end(failed, LOOKUP_FAILED)
        dep = None
        for d in _canonical.decode_gateway_deposits(receipt, ev["hash"], chain, t.table):
            mark = (_canonical.ARBITRUM, d["tx_hash"], d["message_num"])
            if mark in self.consumed or d["gateway"] != ev["to"]:
                continue
            if d["l1_token"] == asset and d["from"] == addr and d["amount"] == raw:
                dep = d
                break
        if dep is None:
            return False
        if _bridges.listed(t.table, chain, dep["gateway"]) is None:
            # an unlisted emitter could log DepositInitiated and send the retryable
            # itself: it counts only when the router names it as the token's gateway
            known = t.router_gateway(chain, dep["l1_token"])
            if known is LOOKUP_FAILED:
                failed = _bridge_info(_canonical.ARBITRUM, None, _bridges.LOOKUP_FAILED)
                return end(failed, LOOKUP_FAILED)
            if known != dep["gateway"]:
                return False
        self.consumed.add((_canonical.ARBITRUM, dep["tx_hash"], dep["message_num"]))
        found = t.follow_token_deposit(chain, dep, ev)
        info = dict(found["info"])
        call = t.gateway_call(chain, ev["hash"])
        if call is not None:
            info["function"] = call["function"]
            if call["refund_to"] is not None:
                info["refund_recipient"] = call["refund_to"]
        if found.get("reason"):
            return end(info, found["reason"])
        edge["kind"] = "bridge"
        edge["bridge"] = info
        credit = found["credit"]
        dest_chain, l2_token = _canonical.ARBITRUM_ONE, found["asset"]
        if credit.get("symbol"):
            t.names.setdefault((dest_chain, l2_token), credit["symbol"])
        landed = _credit_position(credit["ts"], dest_chain, credit["block"], credit["tx_hash"])
        dest_key = (dest_chain, dep["recipient"], l2_token)
        # the gateway carries the amount over unchanged: the L2 fees are paid in ETH
        lots = _lots_by_hop(row)
        self.arrive(dest_key, lots, credit["tx_hash"], addr, credit["tx_hash"], landed)
        return True


class _Tracer:
    """Fetches histories level by level and replays them until nothing new is reached."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.histories = {}  # ledger key -> {"events", "positions", "match", "complete"}
        self.contract_cache = {}
        self.bridge_cache = {}
        # L2 credits given to Arbitrum messages: credit tx -> (message number, cache key).
        # A message may take only credits held by no message or by a later-numbered one
        # (which is then resolved again), so message 40 never loses its credit to 41
        # whatever order the replays reach them in (see claim)
        self.claimed_credits = {}
        self.claimed_token_credits = {}  # the same for gateway deposits (L2 token rows)
        self.reclaims = 0  # credits taken back from a later message (a replay re-runs)
        self.claimed_mints = set()  # CCTP mints given to a deposit: (chain, tx, log index)
        self.incomplete_keys = set()  # ledgers a replay proved incomplete
        self.catch_up_cache = {}  # ledger key -> {(from, to, state): state} (_Replay.catch_up)
        # what order_all built so far: borrowed indexes, block timestamps (raw and running
        # maximum), the histories sorted, and which histories each block or tx touches
        self.order_state = {
            "index": {},
            "raw": {},
            "clock": {},
            "blocks": {},  # chain -> sorted block numbers
            "seen": set(),
            "by_block": {},
            "by_tx": {},
        }
        self.prev_credits = {}  # the previous replay's credits per ledger
        self.prev_credit_at = {}  # and where they sat (_possible_rows)
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

    def symbol(self, chain, token):
        """The symbol of ``token`` on ``chain`` if known (seen in a row, or a listed real
        token), else None."""
        name = self.names.get((chain, token))
        if name and name != token:
            return name
        if chain is None or token is None:
            return None
        from .networks import real_tokens

        return real_tokens(chain).get(token, {}).get("symbol")

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
        Incremental: a history already sorted is sorted again only when a newly read
        history gives one of its unindexed rows an index or moves one of its blocks'
        timestamps; the result is the same as sorting everything again.
        """
        st = self.order_state
        index, raw, clock = st["index"], st["raw"], st["clock"]
        new = [k for k in self.histories if k not in st["seen"]]
        dirty, chains = set(new), {}
        for key in new:
            chain, hist = key[0], self.histories[key]
            for tx, order in hist["index"].items():
                if (chain, tx) not in index:
                    index[(chain, tx)] = order
                    dirty |= st["by_tx"].get((chain, tx), set())
            blocks = raw.setdefault(chain, {})
            by_block = st["by_block"].setdefault(chain, {})
            for ev in hist["events"]:
                block = ev["block"]
                if block not in blocks or ev["ts"] > blocks[block]:
                    blocks[block] = max(blocks.get(block, 0), ev["ts"])
                    chains.setdefault(chain, set()).add(block)
                by_block.setdefault(block, set()).add(key)
                if ev["order"] is None:
                    st["by_tx"].setdefault((chain, ev["hash"]), set()).add(key)
            st["seen"].add(key)
        for chain, touched in chains.items():
            # the running maximum can change only from a touched block on; past it, it
            # stops changing at the first block whose value stays the same
            order = st["blocks"].setdefault(chain, [])
            times, by_block = clock.setdefault(chain, {}), st["by_block"][chain]
            for block in sorted(touched):
                at = bisect.bisect_left(order, block)
                if at == len(order) or order[at] != block:
                    order.insert(at, block)
                running = times[order[at - 1]] if at else 0
                for b in order[at:]:
                    value = max(running, raw[chain][b])
                    if b != block and times.get(b) == value:
                        break
                    if times.get(b) != value:
                        times[b] = value
                        dirty |= by_block[b]
                    running = value
        for number, ((chain, _a, _s), hist) in enumerate(self.histories.items()):
            if (chain, _a, _s) not in dirty:
                continue
            keyed = []
            for ev in hist["events"]:
                if ev["value"] <= 0:
                    continue
                order = ev["order"]
                if order is None:
                    order = index.get((chain, ev["hash"]))
                ev["at"] = (ev["block"], order, ev["hash"], ev.get("sub"))
                keyed.append((_position(chain, ev, order, clock[chain][ev["block"]]), ev))
            keyed.sort(key=lambda pair: pair[0])
            positions = [p for p, _e in keyed]
            if positions != hist.get("positions"):
                self.catch_up_cache.pop((chain, _a, _s), None)  # order changed: recompute
            hist["positions"] = positions
            hist["events"] = [e for _p, e in keyed]
            # replay order: position, then address in fetch order, then event (_Replay)
            hist["keys"] = [(p, (0, number, i)) for i, p in enumerate(positions)]
            inflows = {}
            for i, ev in enumerate(hist["events"]):
                if ev["kind"] == "in":
                    inflows.setdefault(ev["hash"], []).append(i)
            hist["inflows"] = inflows
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
        self.arrival = {"tx": tx, "matched": how, "index": None, "from": None}
        self.locate_arrival()

    def locate_arrival(self):
        """Where the arrival transaction sits in the start history now: order_all can
        re-sort it after a later history gives a row its index or moves a block's time,
        so the index is looked up again before every replay."""
        hist = self.histories[self.start_key]
        tx = self.arrival["tx"]
        index, unsure = _pick_inflow(hist, tx, amount=self.amount) if tx else (None, False)
        # several inflows of the arrival transaction that can be the traced one
        self.arrival["unsure"] = unsure
        if unsure and self.arrival["matched"] == "exact":
            self.arrival["matched"] = AMBIGUOUS_IN_TX
        self.arrival["index"] = index
        self.arrival["from"] = hist["events"][index]["from"] if index is not None else None

    def synthetic_position(self):
        """Start of ``start_block`` in the start address's history (after all, if later)."""
        hist = self.histories[self.start_key]
        chain = self.start_key[0]
        later = [
            p for p, e in zip(hist["positions"], hist["events"]) if e["block"] >= self.start_block
        ]
        ts = later[0][0] if later else math.inf
        start = _allocation.sub_key(None, "in")
        return (ts, chain if chain is not None else -1, self.start_block, -2, start, 0, "")

    # -- bridge lookups (cached: the replay runs several times) -------------------

    def paid_native(self, chain, frm, tx_hash, to, raw):
        """True when ``frm`` paid ``to`` exactly ``raw`` wei by an internal transfer in
        ``tx_hash`` (cached internal list of ``frm``; a failed read counts as no evidence
        and is recorded)."""
        try:
            rows = self.context(chain)["incoming"].internal(frm)
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.note("internal transactions", exc, chain, frm)
            return False
        return any(
            (r.get("hash") or "").lower() == tx_hash
            and (r.get("from") or "").lower() == frm
            and (r.get("to") or "").lower() == to
            and not _failed(r)
            and _raw(r) == raw
            for r in rows
        )

    def payment_lookup(self, chain, addr, tx_hash, token, raw, payer):
        """[(value, symbol)] of the payments of exactly ``raw`` from ``payer`` to ``addr``
        in ``tx_hash`` (cached), or LOOKUP_FAILED."""
        cache_key = ("payment", chain, addr, tx_hash, token, raw, payer)
        if cache_key not in self.swap_cache:
            try:
                incoming = self.context(chain)["incoming"]
                found = incoming.payments(addr, tx_hash, token, raw, payer)
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note("swap output (internal and token transfers)", exc, chain, addr)
                found = LOOKUP_FAILED
            self.swap_cache[cache_key] = found
        return self.swap_cache[cache_key]

    def unit(self, chain, addr, tx_hash, token, raw, payer):
        """Value of one smallest unit of ``token`` (None: the native coin, 18 decimals),
        read from the row of the payment of exactly ``raw`` to ``addr`` in ``tx_hash``;
        None when that row is not listed, or LOOKUP_FAILED."""
        if token is None:
            return 1e-18
        if not raw:
            return None
        seen = self.payment_lookup(chain, addr, tx_hash, token, raw, payer)
        if seen is LOOKUP_FAILED:
            return LOOKUP_FAILED
        return seen[0][0] / raw if seen else None

    def payback(self, chain, addr, ev, asset):
        """What a listed aggregator paid back to ``addr`` in ``ev``'s transaction.

        Returns None (nothing), LOOKUP_FAILED, REFUND_ONLY, or the swap output
        (token or None, value, symbol). A payment in another token is a swap output,
        whatever its size. A native payment back is one only when it exceeds the native
        value ``addr`` attached to the transaction it sent (the transaction object's
        ``value``; 0 when ``addr`` did not send it or the client cannot read it): up to
        that value it is a refund of unused bridge fees or of the value sent. Known limit:
        a token payment has no size rule (without prices it cannot be compared with the
        transfer), so a dust refund of a third token still reads as a swap."""
        cache_key = ("outputs", chain, addr, ev["hash"], asset)
        if cache_key not in self.swap_cache:
            try:
                found = self.context(chain)["incoming"].swap_outputs(addr, ev["hash"], asset)
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note("swap output (internal and token transfers)", exc, chain, addr)
                found = LOOKUP_FAILED
            self.swap_cache[cache_key] = found
        outputs = self.swap_cache[cache_key]
        if outputs is LOOKUP_FAILED or not outputs:
            return outputs or None
        tokens = [o for o in outputs if o[0] is not None]
        if tokens:
            return tokens[0]
        native = sum(o[1] for o in outputs)
        sent = 0.0
        tx = self.tx_info(chain, ev["hash"])
        if tx is LOOKUP_FAILED:
            return LOOKUP_FAILED
        if isinstance(tx, dict) and (tx.get("from") or "").lower() == addr:
            value = tx.get("value") or 0
            sent = (int(value, 16) if isinstance(value, str) else int(value)) / 1e18
        if native > sent:
            return (None, native, outputs[0][2])
        return REFUND_ONLY

    def handler_onward(self, chain, fill, asset):
        """What the Across MulticallHandler paid on in a fill transaction (cached).

        Returns [{"to", "raw", "asset"}]: the fill token's Transfer logs from the handler
        after the fill log and, when the fill is paid as the native coin (``asset``
        None), the handler's native payments in the transaction (internal transactions of
        its block); None when the transaction holds more than one fill to the handler
        (their payments cannot be told apart); LOOKUP_FAILED (recorded once)."""
        cache_key = ("handler", chain, fill["tx_hash"])
        if cache_key in self.bridge_cache:
            return self.bridge_cache[cache_key]
        handler = fill["recipient"]
        what = "bridge fill receipt (multicall handler)"
        receipt = self.receipt(chain, fill["tx_hash"], what=what)
        if receipt is LOOKUP_FAILED or receipt is None:
            found = LOOKUP_FAILED if receipt is LOOKUP_FAILED else []
        elif _bridges.fills_to(receipt, handler, self.table, chain) != 1:
            found = None
        else:
            token = fill["output_token"]
            found = [
                {"to": to, "raw": raw, "asset": token}
                for to, raw, _i in _bridges.handler_payouts(
                    receipt, handler, token, fill.get("log_index") or -1
                )
            ]
            reader = getattr(self.contexts[chain]["client"], "internal_from", None)
            if asset is None and reader is None:
                # the handler's native payments cannot be read: recorded, and without a
                # token payment the handler's payments are a failed lookup
                self.note(
                    "bridge handler payments",
                    "this client cannot list internal transactions",
                    chain,
                    tx=fill["tx_hash"],
                )
                if not found:
                    found = LOOKUP_FAILED
            elif asset is None:
                try:
                    rows = reader(handler, fill["block"], fill["block"])
                except ApiKeyError:
                    raise
                except TornadoDemixError as exc:
                    self.note("bridge handler payments", exc, chain, tx=fill["tx_hash"])
                    rows = LOOKUP_FAILED
                if rows is LOOKUP_FAILED:
                    found = LOOKUP_FAILED
                else:
                    for r in rows:
                        to = (r.get("to") or "").lower()
                        if (r.get("hash") or "").lower() != fill["tx_hash"] or not to:
                            continue
                        if _failed(r) or to == handler or _raw(r) <= 0:
                            continue
                        found.append({"to": to, "raw": _raw(r), "asset": None})
        self.bridge_cache[cache_key] = found
        return found

    def receipt(self, chain, tx_hash, what="bridge deposit receipt"):
        """The receipt of ``tx_hash`` (cached), or LOOKUP_FAILED (recorded once)."""
        cache_key = ("receipt", chain, tx_hash)
        if cache_key not in self.bridge_cache:
            try:
                found = self.contexts[chain]["client"].tx_receipt(tx_hash)
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note(what, exc, chain, tx=tx_hash)
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

    def claim(self, claims, credit_tx, number, cache_key):
        """Give ``credit_tx`` to message ``number`` (cached under ``cache_key``).

        A later-numbered message that held it is forgotten (its cached lookup dropped),
        so it is resolved again without this credit, and the replay runs once more."""
        held = claims.get(credit_tx)
        if held is not None and held[1] != cache_key:
            self.bridge_cache.pop(held[1], None)
            self.reclaims += 1
        claims[credit_tx] = (number, cache_key)

    def follow_message(self, chain, msg, ev):
        """Where one Arbitrum Inbox message was credited on L2 (cached per message).

        Returns {"info", "reason"} when the branch ends, or {"info", "credit"} to
        continue on Arbitrum from the L2 recipient.
        """
        cache_key = ("message", chain, msg["tx_hash"], msg["message_num"])
        if cache_key not in self.bridge_cache:
            found = self._follow_message(msg, ev)
            if found.get("credit"):
                tx = found["credit"]["tx_hash"]
                self.claim(self.claimed_credits, tx, msg["message_num"], cache_key)
            self.bridge_cache[cache_key] = found
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
            held = _held_before(self.claimed_credits, msg["message_num"])
            credit = _canonical.find_l2_credit(dest["client"], msg, ev["ts"], claimed=held)
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.note("bridge l2 credit", exc, dest_chain, address=recipient, tx=ev["hash"])
            return ended(_bridges.LOOKUP_FAILED)
        if credit is None:
            return ended(_canonical.L2_CREDIT_NOT_FOUND, not_found)
        info.update(
            state=_canonical.CREDITED, fill_tx=credit["tx_hash"], fill_block=credit["block"]
        )
        return {"info": info, "credit": credit}

    def follow_cctp(self, chain, dep, ev):
        """Where one CCTP deposit was minted (cached per deposit).

        Returns {"info", "reason"} when the branch ends, or {"info", "mint"} to continue
        on the destination chain from the mint recipient.
        """
        cache_key = ("cctp", chain, ev["hash"], dep["log_index"])
        if cache_key not in self.bridge_cache:
            self.bridge_cache[cache_key] = self._follow_cctp(chain, dep, ev)
        return self.bridge_cache[cache_key]

    def _follow_cctp(self, chain, dep, ev):
        dest_chain, name, rec = dep["dest_chain_id"], dep["dest_chain"], dep["recipient"]
        info = _bridge_info(_cctp.CCTP, dest_chain, None)
        info.update(
            version=dep["version"],
            depositor=dep["depositor"],
            burn_token=dep["burn_token"],
            amount=dep["amount"],
            dest_domain=dep["dest_domain"],
            dest_chain=name,
            dest_recipient=rec,
        )
        if dep["nonce"] is not None:
            info["nonce"] = dep["nonce"]
        if dep["max_fee"] is not None:
            info.update(max_fee=dep["max_fee"], min_finality=dep["min_finality"])
        if dep["destination_caller"]:
            info["destination_caller"] = dep["destination_caller"]

        def ended(state, reason=None):
            info["state"] = state
            return {"info": info, "reason": reason or state}

        if dest_chain is None:
            return ended(_cctp.MINTED_OFF_EVM, f"bridge cctp (to {name}, recipient {rec})")
        dest = self.context(dest_chain)
        if dest is None:
            return ended(
                _bridges.DEST_CHAIN_NOT_SUPPORTED,
                f"bridge cctp (to {name}, recipient {rec}, chain not supported)",
            )
        listed = _cctp.messenger_listed(
            self.table, dest_chain, dep["dest_messenger"], dep["version"]
        )
        if not listed:
            return ended(_bridges.DEST_POOL_NOT_LISTED)
        try:
            mint = _cctp.find_mint(
                dest["client"],
                dep,
                _cctp.origin_domain(chain),
                ev["ts"],
                self.table,
                claimed=self.claimed_mints,
            )
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.note("bridge mint (cctp)", exc, dest_chain, address=rec, tx=ev["hash"])
            return ended(_bridges.LOOKUP_FAILED)
        if mint is None:
            return ended(
                _cctp.MINT_NOT_FOUND, f"bridge cctp (to {name}, recipient {rec}, mint not found)"
            )
        self.claimed_mints.add((dest_chain, mint["tx_hash"], mint["log_index"]))
        info.update(
            state=_cctp.MINTED,
            fill_tx=mint["tx_hash"],
            fill_block=mint["block"],
            mint_token=mint["token"],
            minted=mint["amount"],
            fee=mint["fee"],
        )
        return {"info": info, "mint": mint}

    def tx_info(self, chain, tx_hash):
        """The transaction object of ``tx_hash`` (cached), None when unknown or the client
        cannot read it, or LOOKUP_FAILED (recorded once)."""
        cache_key = ("tx", chain, tx_hash)
        if cache_key not in self.bridge_cache:
            reader = getattr(self.contexts[chain]["client"], "tx_info", None)
            try:
                found = reader(tx_hash) if reader is not None else None
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note("bridge deposit transaction", exc, chain, tx=tx_hash)
                found = LOOKUP_FAILED
            self.bridge_cache[cache_key] = found
        return self.bridge_cache[cache_key]

    def gateway_call(self, chain, tx_hash):
        """The decoded router or gateway call of ``tx_hash``, or None (informational:
        the deposit itself comes from the receipt)."""
        tx = self.tx_info(chain, tx_hash)
        if not isinstance(tx, dict):
            return None
        return _canonical.decode_gateway_call(tx.get("input"))

    def _router(self, chain, call, l1_token, what, empty):
        """An address the L1GatewayRouter returns for ``l1_token`` (cached), or
        LOOKUP_FAILED (recorded once). Read at the latest block: the explorer's eth_call
        takes no block number."""
        cache_key = ("router", chain, call, l1_token)
        if cache_key not in self.bridge_cache:
            caller = getattr(self.contexts[chain]["client"], "eth_call", None)
            found = LOOKUP_FAILED
            try:
                if caller is None:
                    self.note(what, "this client cannot make eth_call", chain, address=l1_token)
                else:
                    data = getattr(_canonical, call)(l1_token)
                    raw = caller(_canonical.ARBITRUM_ROUTER, data)
                    found = _canonical.address_result(raw) or LOOKUP_FAILED
                    if found is LOOKUP_FAILED:
                        self.note(what, empty, chain, address=l1_token)
            except ApiKeyError:
                raise
            except TornadoDemixError as exc:
                self.note(what, exc, chain, address=l1_token)
            self.bridge_cache[cache_key] = found
        return self.bridge_cache[cache_key]

    def l2_token(self, chain, l1_token):
        """The Arbitrum token of ``l1_token`` (router ``calculateL2TokenAddress``)."""
        return self._router(
            chain,
            "l2_token_call",
            l1_token,
            "bridge l2 token (calculateL2TokenAddress)",
            "the router returned no L2 token",
        )

    def router_gateway(self, chain, l1_token):
        """The gateway the router uses for ``l1_token`` (router ``getGateway``)."""
        return self._router(
            chain,
            "gateway_of_call",
            l1_token,
            "bridge token gateway (getGateway)",
            "the router returned no gateway",
        )

    def follow_token_deposit(self, chain, dep, ev):
        """Where one Arbitrum gateway deposit was credited on L2 (cached per message).

        Returns {"info", "reason"} when the branch ends, or {"info", "credit", "asset"}
        to continue on Arbitrum from the L2 recipient in the L2 token.
        """
        cache_key = ("token_deposit", chain, dep["tx_hash"], dep["message_num"])
        if cache_key not in self.bridge_cache:
            found = self._follow_token_deposit(chain, dep, ev)
            if found.get("credit"):
                tx = found["credit"]["tx_hash"]
                self.claim(self.claimed_token_credits, tx, dep["message_num"], cache_key)
            self.bridge_cache[cache_key] = found
        return self.bridge_cache[cache_key]

    def _follow_token_deposit(self, chain, dep, ev):
        dest_chain = _canonical.ARBITRUM_ONE
        recipient = dep["recipient"]
        info = _bridge_info(_canonical.ARBITRUM, dest_chain, None)
        info.update(
            message_num=dep["message_num"],
            message_kind=dep["kind"],
            gateway=dep["gateway"],
            l2_recipient=recipient,
            l1_token=dep["l1_token"],
            amount=dep["amount"],
        )

        def ended(state, reason=None):
            info["state"] = state
            return {"info": info, "reason": reason or state}

        dest = self.context(dest_chain)
        if dest is None:
            return ended(
                _bridges.DEST_CHAIN_NOT_SUPPORTED,
                f"bridge arbitrum (recipient {recipient}, chain not supported)",
            )
        l2_token = self.l2_token(chain, dep["l1_token"])
        if l2_token is LOOKUP_FAILED:
            return ended(_bridges.LOOKUP_FAILED)
        info["l2_token"] = l2_token
        try:
            held = _held_before(self.claimed_token_credits, dep["message_num"])
            credit = _canonical.find_l2_token_credit(
                dest["client"], dep, l2_token, ev["ts"], claimed=held
            )
        except ApiKeyError:
            raise
        except TornadoDemixError as exc:
            self.note("bridge l2 credit", exc, dest_chain, address=recipient, tx=ev["hash"])
            return ended(_bridges.LOOKUP_FAILED)
        if credit is None:
            return ended(_canonical.L2_CREDIT_NOT_FOUND, "bridge arbitrum, l2 credit not found")
        info.update(
            state=_canonical.CREDITED, fill_tx=credit["tx_hash"], fill_block=credit["block"]
        )
        return {"info": info, "credit": credit, "asset": l2_token}

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
            reclaims = self.reclaims
            replay = _Replay(self).run()
            found = {k for k, ledger in replay.ledgers.items() if ledger.incomplete}
            # credits only widen incomplete ledgers, so only theirs must settle; an L2
            # credit taken back from a later message may have been used already
            same = (
                found <= self.incomplete_keys
                and all(replay.credits.get(k) == self.prev_credits.get(k) for k in found)
                and self.reclaims == reclaims
            )
            self.incomplete_keys |= found
            self.prev_credits = replay.credits
            self.prev_credit_at = replay.credit_at
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


def _possible_rows(tracer, replay) -> list[dict]:
    """Outgoing transfers of incomplete histories that may hold traced funds but are no
    edge (FIFO gave them none), in time order.

    Each may carry up to the traced funds credited to its address up to its block (the
    bound of an incomplete history, see _Replay.outflow); only those above rounding
    noise are kept. They are not followed: what they carried, if anything, is unknown.
    """
    rows = []
    for key, ledger in replay.ledgers.items():
        if not ledger.incomplete:
            continue
        chain, addr, asset = key
        edged = {(e["tx_hash"], e["to"]) for e, k, _r in replay.edges if k == key}
        hop = replay.hops.get(key, 0) + 1
        credits_at = replay.credit_at.get(key, []) + tracer.prev_credit_at.get(key, [])
        for i, ev in enumerate(tracer.histories[key]["events"]):
            if ev["kind"] != "out" or (ev["hash"], ev["to"]) in edged:
                continue
            if (key, i) in replay.shared:
                continue  # FIFO gave it traced funds (ended below the threshold)
            if credits_at and all(_precedes(ev["at"], c) for c in credits_at):
                continue  # known to come before every traced arrival
            reach = max(
                _credited(replay.credits.get(key), ev["block"]),
                _credited(tracer.prev_credits.get(key), ev["block"]),
            )
            upper = min(ev["value"], reach)
            if upper <= tracer.noise:
                continue
            _low, high = _allocation.rounded_bounds(0.0, upper)
            row = {
                "hop": hop,
                "from": addr,
                "to": ev["to"],
                "tx_hash": ev["hash"],
                "ts": ev["ts"],
                "asset": tracer.name(chain, asset),
                "asset_address": asset,
                "value": round(ev["value"], 6),
                "attributed": 0.0,
                "kind": "possible",
                "history_incomplete": True,
                "bounds_available": True,
                "lower": 0.0,
                "upper": high,
            }
            if chain is not None:
                row["chain_id"] = chain
            row["status"] = share_status(row)
            rows.append(((ev["ts"], ev["block"]), row))
    rows.sort(key=lambda pair: pair[0])
    return [r for _k, r in rows]


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
    Returns {start, amount, asset, asset_address, edges, terminals, nodes_expanded}.
    ``asset`` is the display name (the token's symbol once a transfer has shown it) and
    ``asset_address`` the token contract (None for the native coin); edges and terminals
    carry both too. An edge is
    {hop, chain_id, from, to, tx_hash, ts, asset, asset_address, value, attributed,
    lower, upper, history_incomplete, bounds_available, sources, kind}; ``attributed`` is
    the FIFO amount, ``lower``/``upper`` the bounds under any ordering (allocation.py, lower
    rounded down and upper up); ``sources`` splits the FIFO amount back across the
    incoming branches it came from ({tx_hash, from, amount}: the arrival
    transaction and sender of each branch, oldest first); ``hop`` is one more than
    the shortest of those branches. ``kind`` is transfer, swap or bridge (a bridge
    edge also carries ``bridge``: protocol, destination chain (None when not
    decoded), state (see bridges.py), and for a decoded deposit the deposit id,
    depositor, recipient, ``via`` (the periphery or router it went through) and the
    fill transaction; for an Arbitrum or ZKsync hop ``l2_recipient``, the message
    number or ``refund_recipient``, for a token deposit ``l1_token`` (and on Arbitrum
    ``l2_token``), and for a confirmed Arbitrum credit the L2 transaction as
    ``fill_tx``; for a named bridge of bridges_named.py what it decoded: ``request_id``,
    ``memo``, ``dest_chain``, ``dest_recipient``, ``maker``, ...; a Mayan same-chain
    payout is a swap edge with ``paid_to``; a CCTP hop carries ``nonce`` (v1),
    ``dest_domain``, ``dest_recipient``, ``mint_token`` and the mint as ``fill_tx``; a CoW
    order is a swap edge with ``swap``: protocol, state, order uid, tokens, amounts,
    receiver, and ``paid_to`` when the receiver is not the owner). Each outgoing transaction is one edge,
    whatever the number of branches that reached its sender. A terminal is {address, chain_id, asset,
    amount, hop, reason, label}, plus ``bridge`` when it ends at a bridge; end points
    at the same address for the same reason are merged.

    Bridge continuation is on when ``chain_id`` (the chain of ``client``) and
    ``destinations`` are given. ``destinations`` is a callable (or dict) from a
    chain id to {"client", "labels", "currency", "is_contract"} or None for a
    chain that cannot be read.

    ``arrival_tx`` is the transaction that brought ``amount`` to ``start``; given, it
    overrides the amount match. The result carries ``arrival_tx`` and
    ``arrival_matched`` ("exact", "ambiguous", "ambiguous_in_tx", "synthetic" or
    "explicit_not_found").
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
            "asset_address": end["asset"],
            "amount": round(end["amount"], 6),
            "hop": end["hop"],
            "reason": end["reason"],
            "label": ctx["labels"].get(end["address"]) if ctx else None,
        }
        if end.get("bridge") is not None:
            terminal["bridge"] = end["bridge"]
        if len(end.get("bridges") or []) > 1:
            terminal["bridges"] = end["bridges"]
        if end.get("outflows"):
            terminal["outflows"] = end["outflows"]
        if end.get("order_note"):
            terminal["order_note"] = end["order_note"]
        if chain is not None:
            terminal["chain_id"] = chain
        terminals.append(terminal)
    result = {
        "start": start,
        "amount": amount,
        "asset": tracer.name(chain_id, token),
        "asset_address": token,
        "edges": edges,
        "terminals": terminals,
        "nodes_expanded": len(tracer.histories),
        "arrival_tx": tracer.arrival["tx"],
        "arrival_matched": tracer.arrival["matched"],
    }
    possible = _possible_rows(tracer, replay)
    result["possible"] = possible[:POSSIBLE_ROWS]
    if len(possible) > POSSIBLE_ROWS:
        rest = possible[POSSIBLE_ROWS:]
        result["possible_more"] = {
            "count": len(rest),
            "value": round(sum(r["value"] for r in rest), 6),
        }
    errors, truncated = _run_notes(tracer)
    return attach(result, errors, truncated)
