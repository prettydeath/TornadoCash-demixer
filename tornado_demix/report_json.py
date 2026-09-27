"""The machine-readable demix result."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .demix import NATIVE_DENOM_TOLERANCE
from .heuristics import (
    MAX_GAS_PRICE_SHARE,
    MIN_COUNT_DISCRIMINATION,
    MIN_FIELD_SIZE,
    ranked_candidates,
)
from .report_csv import (
    _fmt_hours,
    _ts,
)


def analysis_assumptions(data: dict) -> dict:
    """The parameters and search ranges behind a demix result.

    Returns {"params": [(label, value)], "windows": [{pool_key, blocks, dates,
    vouchers, withdrawals, recipients}]}, so a report states the assumptions its
    candidates depend on and the exact block ranges that were read.
    """
    from . import __version__

    params = data.get("params", {})
    if params.get("mode", "events") == "events":
        source = "Withdrawal events of each pool (recipient, relayer and fee exact)"
    else:
        lo, hi = params.get("fee_window", [None, None])
        source = f"internal transfers from the pool, {lo}-{hi} of the denomination"
    exit_h = params.get("exit_window_hours")
    window = (
        f"{_fmt_hours(exit_h)} h after each voucher's last deposit"
        if exit_h
        else f"{params.get('window_days', 30)} days after each voucher's last deposit"
    )
    rows = [
        ("Tool", f"tornado-demix {__version__}"),
        ("Network", params.get("network", "")),
        ("Withdrawal data", source),
        (
            "Voucher gap",
            f"{_fmt_hours(params.get('gap_hours', 24))} h between deposits into one pool",
        ),
        ("Search window", window),
        ("Deposit amount", f"native within {NATIVE_DENOM_TOLERANCE:.1%}; ERC-20 exact"),
        (
            "Count match",
            f"counts only at disc >= {MIN_COUNT_DISCRIMINATION:.2f} "
            f"in a field of at least {MIN_FIELD_SIZE} recipients",
        ),
        (
            "Gas-price reuse",
            f"price shared by at most {MAX_GAS_PRICE_SHARE} withdrawals, blocks without "
            "an EIP-1559 base fee only",
        ),
    ]
    windows = []
    for pool_key, res in data.get("denoms", {}).items():
        for w in res.get("windows", []):
            windows.append(
                {
                    "pool_key": pool_key,
                    "blocks": f"{w.get('start_block')}..{w.get('end_block')}",
                    "dates": f"{_ts(w['first_ts'])} .. {_ts(w['end_ts'])}",
                    "vouchers": w.get("voucher_count", 1),
                    "withdrawals": res.get("total_qualifying_withdrawals", 0),
                    "recipients": res.get("unique_recipients", 0),
                }
            )
    return {"params": rows, "windows": windows}


def demix_json(data: dict, attribution: dict[str, dict] | None = None) -> str:
    """A demix result as JSON: parameters, ranked candidates and the raw result.

    Meant for archiving with a case and for re-analysis (tools/sensitivity.py)
    without repeating the API calls.
    """
    from . import __version__

    doc = {
        "tool": f"tornado-demix {__version__}",
        "generated_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assumptions": analysis_assumptions(data),
        "candidates": ranked_candidates(data, attribution=attribution),
        "result": data,
    }
    return json.dumps(doc, indent=1, default=_json_default, sort_keys=True)


def write_demix_json(data: dict, path: str, attribution: dict[str, dict] | None = None) -> str:
    """Write :func:`demix_json` to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(demix_json(data, attribution=attribution))
    return path


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")
