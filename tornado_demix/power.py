"""Detectability of a planted lead in a decoy window (power check for the placebo test).

A decoy window with zero leads says nothing about chance if even a real early
direct link planted in it would not have been found there. This module plants one
synthetic withdrawal, to an existing counterparty of the wallet and one hour after
a voucher's last deposit, into an already collected run and re-scores it with the
code that scored the run (:func:`tornado_demix.demix.score_collected`). The window
is detectable when that recipient then reaches the ``moderate`` band or better.
No explorer calls: the run's collected withdrawals are all that is read.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable

from .demix import score_collected
from .heuristics import BAND_ORDER, ranked_candidates

PLANT_DELAY_SECONDS = 3600
PLANT_HASH = "0x" + "0" * 24 + "plant" + "0" * 35
REASONS = ("detected", "missed", "no_counterparty", "no_voucher")


def plant_early_link(data: dict, wallet: str, pool_key: str, recipient: str, ts: int) -> dict:
    """Return a copy of ``data`` with one synthetic withdrawal to ``recipient`` at ``ts``.

    The withdrawal is added to the pool's recipient detail and counts; the collected
    totals (``total_qualifying_withdrawals``) are left as they were. ``wallet`` is
    accepted for symmetry with :func:`window_detectable` and is not used.
    """
    out = copy.deepcopy(data)
    res = out["denoms"][pool_key]
    recipient = recipient.lower()
    rec = {
        "value": round(res["denom"] * 0.99, 6),
        "ts": int(ts),
        "hash": PLANT_HASH,
        "relayer": None,
        "self_relayed": False,
        "gas_price": None,
    }
    if recipient not in res["detail"]:
        res["unique_recipients"] = res.get("unique_recipients", len(res["detail"])) + 1
    res["detail"].setdefault(recipient, []).append(rec)
    res["counts"][recipient] = res["counts"].get(recipient, 0) + 1
    return out


def _band(data: dict, address: str, pool_key: str) -> str | None:
    for row in ranked_candidates(data):
        if row["address"] == address and row["pool_key"] == pool_key:
            return row["band"]
    return None


def window_detectability(
    data: dict,
    wallet: str,
    counterparties: Iterable[str] | None = None,
    is_contract: Callable[[str], bool | None] | None = None,
    heuristics_args: dict | None = None,
) -> dict:
    """Plant an early direct link in the run and say whether it would be found.

    Returns ``{"detectable": bool, "reason": one of REASONS, "recipient", "pool_key",
    "self_detectable": bool | None}``.
    The recipient is the first (sorted) counterparty that is neither the wallet nor a
    contract, planted in the pool of the earliest voucher. ``no_counterparty`` is a
    blind spot of the wallet (nowhere to plant a direct link); ``missed`` means a
    link was planted and the pipeline did not rate it ``moderate`` or better.
    ``self_detectable`` is set only for ``no_counterparty``: whether a withdrawal back
    to the (non-contract) wallet itself would reach ``moderate``; it does not change
    ``detectable``, which keeps the counterparty meaning.
    """
    wallet = wallet.lower()
    parties = data.get("counterparty_addresses", ()) if counterparties is None else counterparties
    parties = {p.lower() for p in parties}
    # Lazy: the contract check may be a network call, so stop at the first hit.
    recipient = next(
        (
            a
            for a in sorted(parties)
            if a != wallet and not (is_contract is not None and is_contract(a))
        ),
        None,
    )
    vouchers = [v for v in data.get("vouchers", []) if v["pool_key"] in data.get("denoms", {})]
    if not vouchers:
        return {
            "detectable": False,
            "reason": "no_voucher",
            "recipient": None,
            "pool_key": None,
            "self_detectable": None,
        }
    v = min(vouchers, key=lambda x: x.get("first_ts", 0))
    pool_key = v["pool_key"]
    last = v.get("last_ts", v.get("first_ts"))
    args = dict(heuristics_args or {}, is_contract=is_contract)

    def rated_moderate(target: str) -> bool:
        planted = plant_early_link(data, wallet, pool_key, target, last + PLANT_DELAY_SECONDS)
        score_collected(planted, parties, args)
        band = _band(planted, target, pool_key)
        return band is not None and BAND_ORDER[band] >= BAND_ORDER["moderate"]

    if recipient is None:
        # No direct link to a counterparty can be observed. A withdrawal back to the
        # depositor is the one place left to plant; it is reported apart, because a
        # wallet that never gets money back from Tornado is blind in practice.
        self_ok = not (is_contract is not None and is_contract(wallet)) and rated_moderate(wallet)
        return {
            "detectable": False,
            "reason": "no_counterparty",
            "recipient": None,
            "pool_key": None,
            "self_detectable": self_ok,
        }
    ok = rated_moderate(recipient)
    return {
        "detectable": ok,
        "reason": "detected" if ok else "missed",
        "recipient": recipient,
        "pool_key": pool_key,
        "self_detectable": None,
    }


def window_detectable(data: dict, wallet: str, **kwargs) -> bool:
    """True when a planted early direct link would reach the ``moderate`` band."""
    return window_detectability(data, wallet, **kwargs)["detectable"]
