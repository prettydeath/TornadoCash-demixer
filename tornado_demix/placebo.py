"""Placebo (target-decoy) check for one wallet.

A decoy window is a search window that ends a day before the wallet's first real
deposit, so no withdrawal in it can spend one of the wallet's notes. A lead the
pipeline finds there is a decoy-window lead; the window can contain the depositor's
own earlier activity, so it is not guaranteed to be pure chance. Running the same
pipeline on the real and the decoy window shows how many leads of each band a
wallet of this size gets in a window that holds none of its notes.

The decoy is the real pipeline with every deposit shifted back in time (see the
``deposit_shift_seconds`` parameter of :func:`tornado_demix.demix.run_demix`):
the wallet's history, counterparties, deposit gas prices and funders are real.
"""

from __future__ import annotations

from .demix import run_demix
from .heuristics import ranked_candidates
from .power import window_detectability
from .rpc import make_contract_check

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
    noun = "candidate" if band == "weak" else "lead"
    counts = (
        f"{t} {band} {noun if t == 1 else noun + 's'} in the real window, {d} in the decoy window"
    )
    if t_rate is None or d_rate is None:
        return f"{counts}: a window with no withdrawals cannot be compared"
    if not t and not d:
        return f"No {band} {noun}s in either window"
    counts += f" ({t_rate:g} vs {d_rate:g} per 1000 withdrawals searched)"
    if t_rate > d_rate and t > d:
        return f"{counts}: the real window shows more than the decoy"
    return f"{counts}: indistinguishable from chance"


def _decoy_window(decoy: dict) -> list[int] | None:
    spans = [w for res in decoy.get("denoms", {}).values() for w in res.get("windows", [])]
    if not spans:
        return None
    return [min(w["first_ts"] for w in spans), max(w["end_ts"] for w in spans)]


BLIND_VERDICT = (
    "decoy window cannot show a planted lead; no chance estimate for this wallet's direct "
    "links to third-party addresses (it has no non-contract counterparty, so none can be "
    "observed in the real window either). A withdrawal back to the depositor itself can be "
    "observed and does occur; early-profile leads are reported normally."
)
BLIND_SUFFIX = " (no direct link to a third-party address can be observed for this wallet)"
MISSED_VERDICT = (
    "decoy window cannot show a planted lead; no chance estimate for this wallet's direct "
    "links (a link planted at a counterparty was not rated moderate). "
    "Early-profile leads are reported normally."
)
NO_VOUCHER_VERDICT = (
    "decoy window holds no deposit to plant a lead after; no chance estimate for this "
    "wallet's direct links. Early-profile leads are reported normally."
)
UNKNOWN_CONTRACT_VERDICT = (
    "decoy window cannot show a planted lead; no chance estimate for this wallet's direct "
    "links (contract status unknown: no network to tell a contract counterparty from an "
    "ordinary address). Early-profile leads are reported normally."
)
VERDICTS = {
    "contract_unknown": UNKNOWN_CONTRACT_VERDICT,
    "no_counterparty": BLIND_VERDICT,
    "no_voucher": NO_VOUCHER_VERDICT,
    "missed": MISSED_VERDICT,
}


def _direct_link(direct_link: dict | None) -> dict:
    """Observability fields of the summary from a :func:`window_detectability` result."""
    if direct_link is None:
        return {
            "decoy_detectable": None,
            "direct_link_observable": None,
            "direct_link_reason": None,
            "direct_link_verdict": None,
        }
    ok = bool(direct_link["detectable"])
    reason = direct_link["reason"]
    verdict = None if ok else VERDICTS.get(reason, MISSED_VERDICT)
    return {
        "decoy_detectable": ok,
        "direct_link_observable": ok,
        "direct_link_reason": reason,
        "direct_link_verdict": verdict,
    }


def _failed_lookups(data: dict) -> int | None:
    """Failed lookups a run recorded (tornado_demix.incomplete); None without the field."""
    return len(data.get("errors") or []) if "errors" in data else None


def summarize(target: dict, decoy: dict, direct_link: dict | None = None) -> dict:
    """Compare a real run with its decoy run: leads per band, leads per 1000
    withdrawals searched, and a plain-English verdict per band.

    ``direct_link`` is the :func:`tornado_demix.power.window_detectability` result for
    the wallet. When a planted direct link would not be seen, the wallet's decoy count
    says nothing about chance for direct links (its real window is blind to them as
    well): the summary then carries ``direct_link_verdict`` and the moderate and
    strong verdicts say so. Counts stay, since early-profile leads still fire."""
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
    t_failed, d_failed = _failed_lookups(target), _failed_lookups(decoy)
    if t_failed or d_failed:
        caveats.append(
            f"{d_failed or 0} {'lookup' if d_failed == 1 else 'lookups'} failed in the decoy "
            f"run ({t_failed or 0} in the real run): a failed lookup can change that side's "
            "counts."
        )

    flags = _direct_link(direct_link)
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
        # None for a run written before failed lookups were recorded
        "target_failed_lookups": t_failed,
        "decoy_failed_lookups": d_failed,
        **per_1000,
        **flags,
        "verdicts": {
            b: _verdict(
                b,
                t_counts[b],
                d_counts[b],
                per_1000["target_per_1000"][b],
                per_1000["decoy_per_1000"][b],
            )
            + (
                BLIND_SUFFIX
                if flags["direct_link_reason"] == "no_counterparty" and b != "weak"
                else ""
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
    # The wallet's counterparties are the same in its real and decoy windows.
    check = make_contract_check(network.rpc_url) if network is not None else None
    if check is None:
        # Without a contract check a contract counterparty could be taken for an ordinary
        # address and a planted link counted as detected: unknown is not detectable.
        direct_link = {
            "detectable": False,
            "reason": "contract_unknown",
            "recipient": None,
            "pool_key": None,
            "self_detectable": None,
        }
    else:
        direct_link = window_detectability(
            decoy,
            wallet,
            counterparties=decoy.get("counterparty_addresses", ()),
            is_contract=check,
        )
        failed = list(getattr(check, "errors", None) or [])
        if failed:
            # a counterparty whose contract status failed could be a contract: a planted
            # link there is not known to be seen, so detectability is unknown
            decoy = dict(decoy, errors=list(decoy.get("errors") or []) + failed)
            decoy["error_count"] = len(decoy["errors"])
        if getattr(check, "failed", None) or failed:
            direct_link = {
                "detectable": False,
                "reason": "contract_unknown",
                "recipient": None,
                "pool_key": None,
                "self_detectable": None,
            }
    return summarize(target, decoy, direct_link)
