"""Bounds on how much of an outgoing transfer can be the traced funds.

Funds in one account are fungible, so "how much of this transfer is the traced
amount" has no single answer; it depends on the order convention. FIFO (the
traced funds leave first) is one convention and the upper end of a range. This
module computes the range that holds under ANY convention, from the account's
own history of one asset. No I/O.

Model. At any moment the balance B of the asset is split into a traced pool T
and an other pool O = B - T. An outflow of size o takes x from T and o - x from
O, for any x with 0 <= x <= T and o - x <= O, i.e.

    max(0, o - O) <= x <= min(o, T).

T is not known exactly after the first outflow, only an interval [t_lo, t_hi]
(B is known exactly). Recurrence, per event in chain order:

    traced inflow (value v, traced share a):  t_lo += a ; t_hi += a ; B += v
    other inflow (value v):                   B += v
    outflow o (balance B, O ranges over [B - t_hi, B - t_lo]):
        lower = max(0, o - (B - t_lo))        # most other funds, fewest traced
        upper = min(o, t_hi)                  # most traced funds
        t_lo' = max(0, t_lo - o)              # the outflow took traced funds first
        t_hi' = min(t_hi, B - o)              # ... or took other funds first
        B'    = B - o

Both bounds and both updates are tight: the new T is T - x, and its extremes
max(0, T - o) and min(T, B - o) are non-decreasing in T, so they are reached at
the ends of the old interval. A traced inflow adds exactly a to T, so it moves
both ends by a; several traced inflows (branches of one trace meeting at one
address) add up. Before the first traced inflow every outflow spends other
funds only. The FIFO point estimate (traced funds leave first, in the order
they arrived: min(o, traced left)) follows the t_lo path, so
lower <= fifo <= upper always holds. ``Ledger.outflow`` also returns which
traced inflows (lots) the FIFO amount came from.

Overdraft. If an outflow exceeds B (everything known), the history is missing
something (internal transfers not fetched, a capped page, an inflow the
explorer does not list). From that event on ``history_incomplete`` is set and
lower = 0: the outflow, and every later one, may consist of unseen other funds.
upper = min(o, t_hi) with t_hi reset to the total traced amount that arrived and
no longer capped by B: unseen funds may have paid the earlier outflows too, so
the traced funds may all still be there. Nothing is clamped to make the history
fit. The flag covers the whole address: an outflow before the overdraft rests on
the same missing data, so ``bounds`` (and trace.py) report it with lower 0 and
upper min(o, traced amount arrived so far) as well. Per-event rows keep
``incomplete`` False before the overdraft, to show where it was found. A ledger told up front that its
history is incomplete (``complete=False``) behaves as if overdrawn from the start.

Event order inside a block: events sort by (block, order, sub) where ``order`` is the
transaction index when the explorer row has one (normal and token transfers do,
internal transfers borrow it from their transaction when it is seen) and ``sub`` the
position inside the transaction (see ``sub_order``): the top-level value of a normal
transaction moves before every internal transfer of it, internal transfers follow
their ``traceId`` path component-wise (a call's value before its sub-calls; without
one, Blockscout's flat trace ``index``, never compared with a path), token
transfers their ``logIndex``. A row without an index counts as the first of its
block when it is an inflow and the last when it is an outflow, so an unordered
inflow is never spent before it arrived; inside one transaction a position that is
missing or not comparable (an internal transfer without a readable ``traceId``, a
token transfer without a ``logIndex``, native against token) sorts the inflow first
too, but FIFO does not spend it on that outflow: the order is unknown (see
``_order_unknown``). An equal transaction index alone never means "arrived before".
Gas fees are not subtracted, which can only widen the interval (the balance is
overstated, never understated).
"""

from __future__ import annotations

import math

_AFTER = 10**12


def _tol(x: float) -> float:
    return 1e-9 * max(1.0, abs(x))


# Spaces of a position inside one transaction (``sub``): (space, place).
TOP, INTERNAL, LOG = 0, 1, 2  # top-level value (place ()), traceId path, logIndex
FLAT = 3  # an internal transfer's flat trace index (Blockscout ``index``) without a traceId


def trace_path(trace_id) -> tuple | None:
    """The traceId of an internal transfer as integers ("0_1_12" -> (0, 1, 12); a
    non-numeric prefix such as "call_" is dropped). None when missing or unreadable."""
    parts = str(trace_id or "").strip().split("_")
    while parts and parts[0] and not parts[0].isdigit():
        parts = parts[1:]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def sub_order(a, b) -> int | None:
    """-1 when position ``a`` comes before ``b`` inside one transaction, 1 when after,
    None when not known: a position missing, equal, or in index spaces that are not
    comparable (native against token, a traceId path against a flat trace index). The
    top-level value moves before every internal transfer; traceId paths compare
    component-wise, a call before its sub-calls; flat trace indexes (Blockscout numbers
    the calls of a transaction in trace order) compare as numbers."""
    if a is None or b is None:
        return None
    native = (TOP, INTERNAL, FLAT)
    if a[0] in native and b[0] in native:
        if a[0] != b[0]:
            if TOP not in (a[0], b[0]):
                return None
            return -1 if a[0] == TOP else 1
        if a[0] == TOP:
            return None
    elif a[0] != b[0]:
        return None
    if a[1] is None or b[1] is None or a[1] == b[1]:
        return None
    return -1 if a[1] < b[1] else 1


def sub_key(sub, kind: str):
    """Sort key of a position inside a transaction: an unknown one sorts an inflow
    first and an outflow last (in its space when that is known)."""
    if sub is None:
        return (-1,) if kind == "in" else (_AFTER,)
    if sub[1] is None:
        marker = -1 if kind == "in" else _AFTER
        return (sub[0], (marker,) if sub[0] == INTERNAL else marker)
    return sub


def unsub(key):
    """The position inside a transaction a ``sub_key`` stands for (None: unknown)."""
    if key in ((-1,), (_AFTER,)):
        return None
    if key[1] in (-1, _AFTER, (-1,), (_AFTER,)):
        return (key[0], None)
    return key


def sort_key(event: dict):
    """Chain order of one address's events (see the module doc)."""
    order = event.get("order")
    if order is None:
        order = -1 if event["kind"] == "in" else _AFTER
    return (
        event["block"],
        order,
        sub_key(event.get("sub"), event["kind"]),
        0 if event["kind"] == "in" else 1,
        event.get("hash") or "",
    )


def rounded_bounds(lower: float, upper: float, digits: int = 6) -> tuple[float, float]:
    """``lower`` rounded down and ``upper`` up to ``digits`` decimals, lower <= upper.

    A value within float noise (a few ulps, far below one grid step) of a
    grid point counts as on it, so 3.0 stays 3.0 instead of becoming 2.999999.
    """
    scale = 10**digits

    def snap(v):
        return max(1e-9, abs(v) * 4e-16)

    lo = math.floor(lower * scale + snap(lower * scale)) / scale
    hi = math.ceil(upper * scale - snap(upper * scale)) / scale
    lo = max(lo, 0.0)
    return min(lo, hi), hi


def compare(a, b) -> int | None:
    """-1 when position ``a`` is known to come before ``b``, 1 after, None unknown.

    Positions are (block, transaction index or None[, transaction hash, sub]). Different
    blocks order by block. Inside one transaction (same hash, or the same index when a
    hash is missing) the order is ``sub_order``: an equal index alone is not an order.
    Between transactions of one block both indexes must be known.
    """
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    ia, ib = a[1], b[1]
    ta = a[2] if len(a) > 2 else None
    tb = b[2] if len(b) > 2 else None
    same = ta == tb if ta and tb else ia is not None and ia == ib
    if same:
        return sub_order(a[3] if len(a) > 3 else None, b[3] if len(b) > 3 else None)
    if ia is None or ib is None or ia == ib:
        return None
    return -1 if ia < ib else 1


def _known_before(lot_at, out_at) -> bool:
    """Whether a lot that arrived at ``lot_at`` is known to precede ``out_at``.

    None means "position not known" and counts as before (a lot placed at the start of
    a block, an outflow fed without a position); otherwise see ``compare``.
    """
    if lot_at is None or out_at is None:
        return True
    return compare(lot_at, out_at) == -1


def _order_unknown(lot_at, out_at) -> bool:
    """A lot and an outflow in the same block whose order is not known (an index missing,
    or the same transaction without comparable positions inside it)."""
    return (
        lot_at is not None
        and out_at is not None
        and lot_at[0] == out_at[0]
        and compare(lot_at, out_at) is None
    )


class Ledger:
    """Running bounds and FIFO lots of one asset on one address, event by event.

    Feed events in chain order: ``inflow`` (with its traced share, if any),
    ``add_traced`` (a traced share for an inflow already fed), ``outflow``.
    """

    def __init__(self, complete: bool = True):
        self.balance = 0.0
        self.t_lo = self.t_hi = 0.0
        self.traced_total = 0.0  # the most traced funds that can have arrived
        # [amount, tag, at, unordered, refs] in arrival order: ``unordered`` is how much of
        # the lot an outflow of unknown order (same block, an index missing) may have
        # taken, ``refs`` those outflows (see outflow)
        self.lots: list = []
        self.incomplete = not complete

    @property
    def traced_left(self) -> float:
        """Traced amount not yet spent under FIFO."""
        return sum(lot[0] for lot in self.lots)

    def mark_incomplete(self) -> None:
        """The history misses something: lower 0 from here on, t_hi uncapped."""
        if not self.incomplete:
            self.incomplete = True
            self.t_hi = self.traced_total

    def inflow(self, value: float, traced: float = 0.0, tag=None) -> None:
        self.balance += value
        if traced > 0:
            if traced > value + _tol(value):
                self.mark_incomplete()  # more traced than the inflow that carried it
            self.add_traced(traced, tag)

    def add_traced(self, amount: float, tag=None, lo=None, hi=None, at=None) -> None:
        """A traced share: FIFO ``amount``, and [``lo``, ``hi``] for the bounds.

        ``lo``/``hi`` default to ``amount`` (an exactly known share); a share that
        is itself only bounded (the bounds of the upstream edge) moves t_lo by
        ``lo`` and t_hi by ``hi``, which keeps the bounds sound because the
        recurrence is monotone in T. ``at`` is the arrival position for FIFO
        (see ``outflow``).
        """
        lo = amount if lo is None else lo
        hi = amount if hi is None else hi
        self.t_lo += lo
        self.t_hi += hi
        self.traced_total += hi
        self.lots.append([amount, tag, at, 0.0, []])

    def unordered(self) -> tuple[float, list]:
        """(amount, refs): how much of the traced funds left FIFO may have gone in outflows
        of unknown order, and those outflows' ``ref`` values, first seen first."""
        amount, refs = 0.0, []
        for lot in self.lots:
            amount += min(lot[0], lot[3])
            refs += [r for r in lot[4] if r not in refs]
        return amount, refs

    def outflow(self, o: float, at=None, ref=None) -> dict:
        """Bounds and FIFO share of one outflow of size ``o`` at position ``at``.

        Returns {lower, upper, fifo, taken: [(tag, amount)], incomplete}; ``taken``
        lists the traced lots the FIFO amount came from, oldest first. A lot not
        known to have arrived before ``at`` (same block, an index unknown) is
        skipped; the part of it this outflow may have taken is recorded on the lot
        with ``ref`` (see unordered). Bounds that cross mean the history or its
        order is off: the ledger is then marked incomplete rather than clamped.
        """
        if o > self.balance + _tol(o):
            self.mark_incomplete()
        if not self.incomplete:
            lower = max(0.0, o - (self.balance - self.t_lo))
            upper = min(o, self.t_hi)
            if lower > upper + _tol(o):
                self.mark_incomplete()
        if self.incomplete:
            lower, upper, new_hi = 0.0, min(o, self.t_hi), self.t_hi
        else:
            new_hi = min(self.t_hi, self.balance - o)
        need, taken, kept = o, [], []
        maybe = o  # what is left of the outflow for lots of unknown order
        for lot in self.lots:
            if need > _tol(o) and _known_before(lot[2], at):
                used = min(need, lot[0])
                taken.append((lot[1], used))
                need -= used
                maybe -= used
                lot[0] -= used
            elif maybe > _tol(o) and _order_unknown(lot[2], at):
                part = min(maybe, lot[0])
                lot[3] += part
                maybe -= part
                if ref is not None and ref not in lot[4]:
                    lot[4].append(ref)
            if lot[0] > _tol(o):
                kept.append(lot)
        self.lots = kept
        fifo = o - need if taken else 0.0
        if fifo > upper + _tol(o):  # FIFO took lots the bounds cannot hold: data is off
            self.mark_incomplete()
            upper = min(o, self.t_hi)
            lower, new_hi = 0.0, self.t_hi
        self.t_lo = max(0.0, self.t_lo - o)
        self.t_hi = max(0.0, new_hi)
        self.balance = max(0.0, self.balance - o)
        return {
            "lower": lower,
            "upper": upper,
            "fifo": fifo,
            "taken": taken,
            "incomplete": self.incomplete,
        }


def bounds(
    events: list[dict],
    traced_tx: str | None,
    traced_amount: float | None = None,
    start_block: int = 0,
) -> dict:
    """Bounds of the traced share of every outflow after the traced inflow.

    ``events`` are {"kind": "in"|"out", "hash", "block", "order", "value", "to"/"from"}
    of ONE asset on ONE address. The traced inflow is the "in" event whose hash is
    ``traced_tx``; ``traced_amount`` is the traced share of it (default: all of
    it). When no such event exists the traced amount is placed at the start of
    ``start_block`` and ``traced_found`` is False.

    Returns {"outflows": [{hash, to, value, lower, upper, fifo, incomplete}],
    "history_incomplete", "traced_found"}; outflows are the ones at or after the
    traced inflow.
    """
    ordered = sorted((e for e in events if e["value"] > 0), key=sort_key)
    tx = (traced_tx or "").lower()
    arrival = next(
        (i for i, e in enumerate(ordered) if e["kind"] == "in" and tx and e["hash"].lower() == tx),
        None,
    )
    split = None
    if arrival is None:  # a synthetic arrival at the start of start_block
        split = next((i for i, e in enumerate(ordered) if e["block"] >= start_block), len(ordered))
    ledger = Ledger()
    arrived = False
    results = []
    for i, e in enumerate(ordered):
        if i == split:
            share = traced_amount or 0.0
            ledger.inflow(share, traced=share)
            arrived = True
        if e["kind"] == "in":
            if i == arrival:
                share = e["value"] if traced_amount is None else traced_amount
                ledger.inflow(e["value"], traced=share)
                arrived = True
            else:
                ledger.inflow(e["value"])
            continue
        unseen = min(e["value"], ledger.traced_total)
        row = ledger.outflow(e["value"])
        if arrived:
            results.append(
                {
                    "hash": e["hash"],
                    "to": e.get("to"),
                    "value": e["value"],
                    "lower": row["lower"],
                    "upper": row["upper"],
                    "fifo": row["fifo"],
                    "incomplete": row["incomplete"],
                    "_unseen": unseen,
                }
            )
    for r in results:
        unseen = r.pop("_unseen")
        if ledger.incomplete:  # the flag covers the whole address (module doc)
            r["lower"], r["upper"] = 0.0, max(r["upper"], unseen)
    return {
        "outflows": results,
        "history_incomplete": ledger.incomplete,
        "traced_found": arrival is not None,
    }
