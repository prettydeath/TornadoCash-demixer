"""Placebo (target-decoy) check for one wallet.

A decoy window is a search window that ends a day before the wallet's first real
deposit, so no withdrawal in it can spend one of the wallet's notes. Every lead
the pipeline finds there is chance by construction. Running the same pipeline on
the real and the decoy window shows how many leads of each band a wallet of this
size gets from chance alone.

The decoy is the real pipeline with every deposit shifted back in time (see the
``deposit_shift_seconds`` parameter of :func:`tornado_demix.demix.run_demix`):
the wallet's history, counterparties, deposit gas prices and funders are real.
"""

from __future__ import annotations

from .demix import run_demix
from .heuristics import ranked_candidates

BANDS = ("strong", "moderate", "weak")
DAY = 86400
# A deposit history longer than this pushes the decoy far back from the real window.
MAX_SPAN_DAYS = 60
# Tornado.Cash went live on Ethereum mainnet in December 2019; no pool is older.
TORNADO_LAUNCH_TS = 1576454400
MAX_DECOY_LEADS = 10

NOTE = (
    "A single wallet is a tiny sample: a few leads either way say little. The "
    "check shows what chance alone produces for this wallet, not how accurate "
    "the real leads are."
)


def decoy_offset(deposits: list[dict], window_days: int) -> int:
    """Seconds to shift every deposit back so each decoy window ends a day before
    the first real deposit. 0 when there are no deposits."""
    if not deposits:
        return 0
    ts = [d["ts"] for d in deposits]
    return int(max(ts) - min(ts) + (window_days + 1) * DAY)


def _withdrawals(data: dict) -> int:
    return sum(sum(res.get("counts", {}).values()) for res in data.get("denoms", {}).values())


def _band_counts(rows: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(BANDS, 0)
    for r in rows:
        counts[r["band"]] += 1
    return counts


def _per_1000(count: int, withdrawals: int) -> float | None:
    return round(count * 1000 / withdrawals, 2) if withdrawals else None


def _verdict(band: str, t: int, d: int, t_rate, d_rate) -> str:
    counts = f"{t} {band} lead(s) in the real window, {d} in the decoy window"
    if t_rate is None or d_rate is None:
        return f"{counts}: a window with no withdrawals cannot be compared"
    if not t and not d:
        return f"No {band} leads in either window"
    counts += f" ({t_rate:g} vs {d_rate:g} per 1000 withdrawals searched)"
    if t_rate > d_rate and t > d:
        return f"{counts}: the real window shows more than the decoy"
    return f"{counts}: indistinguishable from chance"


def _decoy_window(decoy: dict) -> list[int] | None:
    spans = [w for res in decoy.get("denoms", {}).values() for w in res.get("windows", [])]
    if not spans:
        return None
    return [min(w["first_ts"] for w in spans), max(w["end_ts"] for w in spans)]


def summarize(target: dict, decoy: dict) -> dict:
    """Compare a real run with its decoy run: leads per band, leads per 1000
    withdrawals searched, and a plain-English verdict per band."""
    t_rows, d_rows = ranked_candidates(target), ranked_candidates(decoy)
    t_counts, d_counts = _band_counts(t_rows), _band_counts(d_rows)
    t_w, d_w = _withdrawals(target), _withdrawals(decoy)
    offset = int(decoy.get("params", {}).get("deposit_shift_seconds") or 0)
    window = _decoy_window(decoy)

    caveats = []
    ts = [d["ts"] for d in target.get("deposits", [])]
    if ts and max(ts) - min(ts) > MAX_SPAN_DAYS * DAY:
        caveats.append(
            f"The deposits span more than {MAX_SPAN_DAYS} days, so the decoy window lies far "
            "from the real one and the wallet's activity may differ there."
        )
    if window and window[0] < TORNADO_LAUNCH_TS:
        caveats.append(
            "The decoy window starts before Tornado.Cash existed, so part of it holds no withdrawals."
        )
    if decoy.get("unresolved"):
        caveats.append(
            "Some pools could not be searched in the decoy window (block lookup failed)."
        )

    per_1000 = {
        "decoy_per_1000": {b: _per_1000(d_counts[b], d_w) for b in BANDS},
        "target_per_1000": {b: _per_1000(t_counts[b], t_w) for b in BANDS},
    }
    return {
        "offset_days": round(offset / DAY, 1),
        "decoy_window": window,
        "target": t_counts,
        "decoy": d_counts,
        "target_withdrawals": t_w,
        "decoy_withdrawals": d_w,
        **per_1000,
        "verdicts": {
            b: _verdict(
                b,
                t_counts[b],
                d_counts[b],
                per_1000["target_per_1000"][b],
                per_1000["decoy_per_1000"][b],
            )
            for b in BANDS
        },
        "decoy_leads": [
            {
                "address": r["address"],
                "pool_key": r["pool_key"],
                "band": r["band"],
                "signals": r["signals"],
            }
            for r in d_rows
            if r["band"] in ("strong", "moderate")
        ][:MAX_DECOY_LEADS],
        "caveats": caveats,
        "note": NOTE,
    }


def run_placebo(
    client,
    wallet: str,
    target: dict,
    window_days: int = 30,
    network=None,
    labels=None,
    exit_window_hours=None,
    **kwargs,
) -> dict:
    """Run the pipeline on the decoy window of ``wallet`` and compare it with
    ``target``, the real run. Extra keyword arguments go to ``run_demix`` so the
    decoy uses the same parameters as the real run."""
    offset = decoy_offset(target.get("deposits", []), window_days)
    decoy = run_demix(
        client,
        wallet,
        window_days=window_days,
        network=network,
        labels=labels,
        exit_window_hours=exit_window_hours,
        deposit_shift_seconds=offset,
        **kwargs,
    )
    return summarize(target, decoy)
