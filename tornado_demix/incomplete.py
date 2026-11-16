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
``EtherscanClient.truncated``), which make the results incomplete as well. An
entry with reason ``partial_indexing`` (``txstatus.PARTIAL_INDEXING``) is a list the
explorer answered while part of its range was still being indexed: it counts like a
cut-off list (results may be incomplete, nothing built on it is certain) but is named
apart.
"""

from __future__ import annotations

from .errors import ArchiveUnavailableError, ProviderBlockedError
from .txstatus import PARTIAL_INDEXING

ERROR_TEXT = 200  # characters of the provider's message kept per record

# ``truncated`` entries with one of these reasons are notices, not cut-off lists: the
# list is complete but was read from a JSON-RPC node, whose log rows carry no gas price
# (the unique-gas-price signal cannot match on them).
NOTICE_REASONS = ("rpc_fallback_no_gas_price",)


def is_notice(entry: dict) -> bool:
    """True for a ``truncated`` entry that is a notice (see ``NOTICE_REASONS``)."""
    return entry.get("reason") in NOTICE_REASONS


def is_partial(entry: dict) -> bool:
    """True for a ``truncated`` entry of a partly indexed answer (``PARTIAL_INDEXING``)."""
    return entry.get("reason") == PARTIAL_INDEXING


def notice_only(result: dict) -> bool:
    """True when the result has notices and nothing failed or was cut off."""
    cut = [t for t in result.get("truncated") or [] if not is_notice(t)]
    has = bool(result.get("truncated")) or bool(early_profile_note(result))
    return has and not (result.get("errors") or cut)


def early_profile_note(result: dict) -> str:
    """The recorded reason the early profile was not applied (``heuristics.early_profile_note``)."""
    return (result.get("heuristics") or {}).get("early_profile_note") or ""


RPC_NOTICE_TEXT = (
    "Nothing is missing: the lists are complete, but a node log has no gas price, so the "
    "unique-gas-price signal is off for them."
)
# What each early-profile note means for the reader (keys: ``heuristics`` notes).
EARLY_PROFILE_TEXT = {
    "early profile not applied: window shorter than 72 h": (
        "The early-withdrawal profile needs the 72 h after each deposit, and this search "
        "window is shorter (or those 72 h have not passed yet), so no recipient was matched "
        "on it; the other signals are unaffected."
    ),
    "early profile not applied: pool uncertain": (
        "The early-withdrawal profile is off because a deposit's pool is undecided: a "
        "recipient could match part of the profile where the whole would not match."
    ),
    "early profile not applied: withdrawal history incomplete": (
        "The early-withdrawal profile is off because a pool's withdrawal history was not "
        "read in full: a recipient could hold the profile in the part that was read and not "
        "in the whole."
    ),
}


def notice_lines(result: dict) -> list[str]:
    """One explanatory sentence per kind of notice the result carries (RPC fallback,
    early-profile note); [] when it has none."""
    out = []
    if any(is_notice(t) for t in result.get("truncated") or []):
        out.append(RPC_NOTICE_TEXT)
    note = early_profile_note(result)
    if note:
        out.append(EARLY_PROFILE_TEXT.get(note, f"The early-withdrawal profile is off ({note})."))
    return out


def reason_of(error) -> str | None:
    """Machine-readable reason named by the exception class, or None."""
    if isinstance(error, ProviderBlockedError):
        return "provider_blocked"
    if isinstance(error, ArchiveUnavailableError):
        return "archive_unavailable"
    return None


def pool_ambiguous(tx: str, candidates, why: str) -> dict:
    """A deposit whose pool could not be settled among twin pools: it is listed but
    not searched (see ``cluster_vouchers``)."""
    keys = sorted(candidates)
    return {
        "kind": "pool_ambiguous",
        "what": "deposit pool",
        "tx": tx,
        "candidates": keys,
        "reason": "pool_ambiguous",
        "error": "pool uncertain between {} ({}); deposit not searched".format(
            ", ".join(keys), why
        ),
    }


def lookup_failed(what: str, error, address: str | None = None, tx: str | None = None, **extra):
    """One error record (see the module docstring)."""
    text = str(error).strip() or type(error).__name__
    rec = {"kind": "lookup_failed", "what": what}
    if address:
        rec["address"] = address
    if tx:
        rec["tx"] = tx
    rec["error"] = text.splitlines()[0][:ERROR_TEXT] if text else ""
    reason = reason_of(error)
    if reason:
        rec["reason"] = reason
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
    ambiguous = sum(1 for e in errors if e.get("kind") == "pool_ambiguous")
    other = len(errors) - failed - ambiguous
    entries = result.get("truncated") or []
    partial = sum(1 for t in entries if is_partial(t))
    cut = sum(1 for t in entries if not is_notice(t)) - partial
    notices = len(entries) - cut - partial
    parts = []
    if failed:
        parts.append(_plural(failed, "lookup failed", "lookups failed"))
    if ambiguous:
        parts.append(
            _plural(
                ambiguous,
                "deposit with an uncertain pool (not searched)",
                "deposits with an uncertain pool (not searched)",
            )
        )
    if cut:
        parts.append(_plural(cut, "list cut off by the explorer", "lists cut off by the explorer"))
    if partial:
        parts.append(
            _plural(partial, "list", "lists")
            + " still being indexed by the explorer (partial indexing)"
        )
    if other:
        parts.append(_plural(other, "other note", "other notes"))
    if notices:
        parts.append(
            _plural(notices, "list", "lists")
            + " read from the RPC node (gas-price signal unavailable)"
        )
    note = early_profile_note(result)
    if note:
        parts.append(note)
    if not parts:
        return ""
    tail = (
        "results may be incomplete"
        if (failed or cut or partial or other or ambiguous)
        else "notice"
    )
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
        message = e.get("error", "")
        reason = e.get("reason")
        if reason and reason not in message:
            message = f"{message} [{reason}]"
        out.append(f"{text}{chain}: {message}")
    for t in result.get("truncated") or []:
        if is_notice(t):
            out.append(
                f"{t.get('action', 'list')} of {t.get('address', '')} read from the RPC node: "
                "its rows carry no gas price, so the gas-price signal cannot match them"
            )
            continue
        if is_partial(t):
            out.append(
                f"{t.get('action', 'list')} of {t.get('address', '')}: partial indexing, the "
                "explorer had not processed the whole range yet (newest rows may be missing)"
            )
            continue
        block = f" at block {t['block']}" if t.get("block") is not None else ""
        limit = f" at {t['limit']} rows" if t.get("limit") else ""
        out.append(f"{t.get('action', 'list')} of {t.get('address', '')} cut off{limit}{block}")
    return out
