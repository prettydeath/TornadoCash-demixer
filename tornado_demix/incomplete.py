"""Lookups that failed during a run, and lists the explorer cut off.

A lookup that failed is never read as "nothing found": it is recorded, the run
goes on, and every report says how many lookups failed. One record per failure::

    {"kind": "lookup_failed", "what": "history", "address": "0x…", "error": "…"}

``what`` names the lookup (``history``, ``deposit address check``, ``linked_sender
busy check``, ...); ``address`` or ``tx`` names what was looked up (either may be
absent); ``error`` is the provider's message, cut to ``ERROR_TEXT`` characters.
A few records carry another ``kind`` (a trace note such as ``replay_unsettled``).

Run results (``run_demix``, ``multi.correlate``, ``characterize_address``,
``trace_funds``) carry ``errors`` (the records), ``error_count`` and ``truncated``:
the lists the explorer client cut off during that run (see
``EtherscanClient.truncated``), which make the results incomplete as well.
"""

from __future__ import annotations

ERROR_TEXT = 200  # characters of the provider's message kept per record

# ``truncated`` entries with one of these reasons are notices, not cut-off lists: the
# list is complete but was read from a JSON-RPC node, whose log rows carry no gas price
# (the unique-gas-price signal cannot match on them).
NOTICE_REASONS = ("rpc_fallback_no_gas_price",)


def is_notice(entry: dict) -> bool:
    """True for a ``truncated`` entry that is a notice (see ``NOTICE_REASONS``)."""
    return entry.get("reason") in NOTICE_REASONS


def notice_only(result: dict) -> bool:
    """True when the result has notices and nothing failed or was cut off."""
    cut = [t for t in result.get("truncated") or [] if not is_notice(t)]
    return bool(result.get("truncated")) and not (result.get("errors") or cut)


def lookup_failed(what: str, error, address: str | None = None, tx: str | None = None, **extra):
    """One error record (see the module docstring)."""
    text = str(error).strip() or type(error).__name__
    rec = {"kind": "lookup_failed", "what": what}
    if address:
        rec["address"] = address
    if tx:
        rec["tx"] = tx
    rec["error"] = text.splitlines()[0][:ERROR_TEXT] if text else ""
    rec.update(extra)
    return rec


def mark(client) -> int:
    """Position in ``client.truncated`` at the start of a run (0 without the list)."""
    notes = getattr(client, "truncated", None)
    return len(notes) if isinstance(notes, list) else 0


def truncated_since(client, start: int) -> list[dict]:
    """The cut-off lists ``client`` recorded since ``mark`` returned ``start``."""
    notes = getattr(client, "truncated", None)
    return [dict(n) for n in notes[start:]] if isinstance(notes, list) else []


def attach(result: dict, errors, truncated) -> dict:
    """Set ``errors``, ``error_count`` and ``truncated`` on a run result."""
    result["errors"] = list(errors)
    result["error_count"] = len(result["errors"])
    result["truncated"] = list(truncated)
    return result


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary(result: dict, dash: str = "—") -> str:
    """The one-line notice ("N lookups failed — results may be incomplete"), or ""."""
    errors = result.get("errors") or []
    failed = sum(1 for e in errors if e.get("kind", "lookup_failed") == "lookup_failed")
    other = len(errors) - failed
    entries = result.get("truncated") or []
    cut = sum(1 for t in entries if not is_notice(t))
    notices = len(entries) - cut
    parts = []
    if failed:
        parts.append(_plural(failed, "lookup failed", "lookups failed"))
    if cut:
        parts.append(_plural(cut, "list cut off by the explorer", "lists cut off by the explorer"))
    if other:
        parts.append(_plural(other, "other note", "other notes"))
    if notices:
        parts.append(
            _plural(notices, "list", "lists")
            + " read from the RPC node (gas-price signal unavailable)"
        )
    if not parts:
        return ""
    tail = "results may be incomplete" if (failed or cut or other) else "notice"
    return ", ".join(parts) + f" {dash} {tail}"


def lines(result: dict) -> list[str]:
    """One readable line per failed lookup and per cut-off list."""
    out = []
    for e in result.get("errors") or []:
        where = e.get("address") or e.get("tx") or ""
        prefix = f"[{e['wallet']}] " if e.get("wallet") else ""
        chain = f" (chain {e['chain_id']})" if e.get("chain_id") is not None else ""
        text = f"{prefix}{e.get('what', 'lookup')}"
        if where:
            text += f" {where}"
        out.append(f"{text}{chain}: {e.get('error', '')}")
    for t in result.get("truncated") or []:
        if is_notice(t):
            out.append(
                f"{t.get('action', 'list')} of {t.get('address', '')} read from the RPC node: "
                "its rows carry no gas price, so the gas-price signal cannot match them"
            )
            continue
        block = f" at block {t['block']}" if t.get("block") is not None else ""
        limit = f" at {t['limit']} rows" if t.get("limit") else ""
        out.append(f"{t.get('action', 'list')} of {t.get('address', '')} cut off{limit}{block}")
    return out
