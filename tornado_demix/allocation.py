"""Bounds on how much of an outgoing transfer can be the traced funds.

Funds in one account are fungible, so "how much of this transfer is the traced
amount" has no single answer; it depends on the order convention. FIFO (the
traced funds leave first) is one convention and the upper end of a range. This
module computes the range that holds under ANY convention, from the account's
own history of one asset. Pure functions, no I/O.

Model. At any moment the balance B of the asset is split into a traced pool T
and an other pool O = B - T. An outflow of size o takes x from T and o - x from
O, for any x with 0 <= x <= T and o - x <= O, i.e.

    max(0, o - O) <= x <= min(o, T).

T is not known exactly after the first outflow, only an interval [t_lo, t_hi]
(B is known exactly). Recurrence, per event in chain order:

    traced inflow (value v, traced share a):  t_lo = t_hi = a ; B += v
    other inflow (value v):                   B += v
    outflow o (balance B, O ranges over [B - t_hi, B - t_lo]):
        lower = max(0, o - (B - t_lo))        # most other funds, fewest traced
        upper = min(o, t_hi)                  # most traced funds
        t_lo' = max(0, t_lo - o)              # the outflow took traced funds first
        t_hi' = min(t_hi, B - o)              # ... or took other funds first
        B'    = B - o

Both bounds and both updates are tight: the new T is T - x, and its extremes
max(0, T - o) and min(T, B - o) are non-decreasing in T, so they are reached at
the ends of the old interval. Before the traced inflow every outflow spends
other funds only. The FIFO point estimate (the convention trace.py uses:
traced funds leave first, min(o, traced left)) follows the t_lo path, so
lower <= fifo <= upper always holds.

If an outflow exceeds B (everything known), the history is missing something
(internal transfers not fetched, a capped page). That is never clamped: the
shortfall is treated as unknown OTHER funds that were present, which is the
conservative choice for both bounds, and ``history_incomplete`` is set.

Event order inside a block: events sort by (block, order) where ``order`` is the
transaction index when the explorer row has one (normal and token transfers do,
internal transfers do not). A row without an index counts as the first of its
block when it is an inflow and the last when it is an outflow, so an
unordered inflow is never spent before it arrived; at an equal position inflows
come before outflows, then by hash. Gas fees are not subtracted, which can only
widen the interval (the balance is overstated, never understated).
"""

from __future__ import annotations

_AFTER = 10**12


def _tol(x: float) -> float:
    return 1e-9 * max(1.0, abs(x))


def _sort_key(event: dict):
    order = event.get("order")
    if order is None:
        order = -1 if event["kind"] == "in" else _AFTER
    return (event["block"], order, 0 if event["kind"] == "in" else 1, event.get("hash") or "")


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

    Returns {"outflows": [{hash, to, value, lower, upper, fifo}], "history_incomplete",
    "traced_found"}; outflows are the ones at or after the traced inflow.
    """
    ordered = sorted((e for e in events if e["value"] > 0), key=_sort_key)
    tx = (traced_tx or "").lower()
    arrival = next(
        (i for i, e in enumerate(ordered) if e["kind"] == "in" and tx and e["hash"].lower() == tx),
        None,
    )
    incomplete = False
    balance = t_lo = t_hi = fifo_left = 0.0
    arrived = False
    results = []

    def arrive(value, share):
        nonlocal balance, t_lo, t_hi, fifo_left, arrived, incomplete
        share = value if share is None else share
        if share > value + _tol(value):
            incomplete = True  # the traced amount is more than the inflow that carried it
        t_lo = t_hi = fifo_left = share
        balance += value
        arrived = True

    if arrival is None:
        # place a synthetic arrival at the start of start_block
        split = next((i for i, e in enumerate(ordered) if e["block"] >= start_block), len(ordered))
        share = traced_amount or 0.0
        pending = (split, share)
    else:
        pending = None
    for i, e in enumerate(ordered):
        if pending is not None and i == pending[0]:
            arrive(pending[1], pending[1])
            pending = None
        if e["kind"] == "in":
            if i == arrival:
                arrive(e["value"], traced_amount)
            else:
                balance += e["value"]
            continue
        o = e["value"]
        if o > balance + _tol(o):
            incomplete = True
            balance = o  # the missing history is unknown other funds
        lower = max(0.0, o - (balance - t_lo))
        upper = min(o, t_hi)
        fifo = min(o, fifo_left)
        fifo_left -= fifo
        t_lo, t_hi = max(0.0, t_lo - o), min(t_hi, balance - o)
        balance -= o
        if arrived:
            results.append(
                {
                    "hash": e["hash"],
                    "to": e.get("to"),
                    "value": o,
                    "lower": lower,
                    "upper": upper,
                    "fifo": fifo,
                }
            )
    if pending is not None:  # synthetic arrival after every event
        arrive(pending[1], pending[1])
    return {
        "outflows": results,
        "history_incomplete": incomplete,
        "traced_found": arrival is not None,
    }
