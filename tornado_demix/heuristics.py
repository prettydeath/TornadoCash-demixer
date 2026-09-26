"""Signals, score and evidence bands for demix candidates.

Besides the amount+timing count match: a withdrawal that reuses a rare deposit
gas price (only where the sender chose the price, i.e. no EIP-1559 base fee),
and a candidate that transacts directly with the depositor outside Tornado.
The score orders candidates; the band describes the structure of the evidence.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

from .attribution import format_label, label_of

# A deposit gas price reused by more withdrawals than this in one window is a
# common wallet default, not a fingerprint.
MAX_GAS_PRICE_SHARE = 3

# Signal weights (documented, tunable). Higher = stronger evidence.
SIGNAL_WEIGHTS = {
    "count_match": 0.30,  # received exactly a voucher's worth of notes
    "self_relayed": 0.25,  # paid own gas -> funded address tied to exit
    "gas_price": 0.25,  # exact deposit gas price reused on withdrawal
    "linked": 0.40,  # directly transacts with the depositor
    "profile_match": 0.35,  # received the full pool fingerprint
}


# Below this discrimination a count match is not a signal at all (no score, no
# band, no candidate); above it the weight scales with disc. A judgement call.
MIN_COUNT_DISCRIMINATION = 0.5

# disc is relative: with two or three recipients in the window a unique count
# reaches 1.0 although a chance match is still likely. Below this field size a
# count match is not evidence at all.
MIN_FIELD_SIZE = 5


def count_discrimination(res, hits):
    """How much of the field a hit count of ``hits`` eliminates, in [0, 1].

    0.0 when every recipient shares the count (a single-note voucher), 1.0 when
    the count is unique.
    """
    unique = res.get("unique_recipients") or len(res.get("counts", {}))
    if unique <= 1:
        return 0.0
    sharing = sum(1 for c in res["counts"].values() if c == hits)
    if sharing >= unique:
        return 0.0
    if sharing <= 0:
        # No recipient has this count. Unreachable from the pipeline, where
        # hits comes from counts[addr]; without it the formula would exceed 1.
        return 1.0
    return 1.0 - (float(sharing) - 1.0) / (float(unique) - 1.0)


def count_is_evidence(res: dict, hits: int) -> bool:
    """True when a hit count narrows a large enough field to count as a signal."""
    field = res.get("unique_recipients") or len(res.get("counts", {}))
    return field >= MIN_FIELD_SIZE and count_discrimination(res, hits) >= MIN_COUNT_DISCRIMINATION


def apply_heuristics(
    data: dict,
    counterparties: set[str],
    gas_gate: Callable[[int], bool] | None = None,
    is_contract: Callable[[str], bool | None] | None = None,
) -> dict:
    """Add ``signals`` and ``confidence`` to every recipient of a run_demix() result.

    Signals are keyed by (pool_key, address), so a signal earned in one pool does
    not follow the address into another. ``gas_gate(block)`` says whether the
    sender chose that block's gas price (None: ungated). ``is_contract(address)``
    returns True, False or None; a contract counterparty does not earn ``linked``.
    """
    deposits = data.get("deposits", [])
    deposit_gas = {d["gas_price"] for d in deposits if d.get("gas_price")}

    gas_matches = []  # (pool_key, recipient, gas_price, tx_hash)
    linked_hits = []  # (pool_key, recipient)
    linked_contracts = set()  # direct counterparties that are contracts
    signals = {}  # (pool_key, address) -> set of signal names
    discriminations = {}  # (pool_key, address) -> count discrimination in [0, 1]

    for pool_key, res in data.get("denoms", {}).items():
        gp_counts = Counter()
        for recs in res["detail"].values():
            for r in recs:
                if r.get("gas_price"):
                    gp_counts[r["gas_price"]] += 1

        target_counts = set(res.get("target_counts", []))
        for addr, recs in res["detail"].items():
            sig = signals.setdefault((pool_key, addr), set())
            hits = res["counts"][addr]

            discrimination = count_discrimination(res, hits)
            if hits in target_counts and count_is_evidence(res, hits):
                sig.add("count_match")
            discriminations[(pool_key, addr)] = discrimination
            if any(r.get("self_relayed") for r in recs):
                sig.add("self_relayed")

            # A withdrawal reusing one of the wallet's deposit gas prices, where
            # that price is rare in the window (not a common auto-fee value).
            # gas_gate rejects blocks whose price the sender did not choose.
            for r in recs:
                gp = r.get("gas_price")
                if gp and gp in deposit_gas and gp_counts[gp] <= MAX_GAS_PRICE_SHARE:
                    if gas_gate is not None and not gas_gate(r.get("block")):
                        continue
                    sig.add("gas_price")
                    gas_matches.append((pool_key, addr, gp, r["hash"]))
                    break

            if addr in counterparties:
                if is_contract is not None and is_contract(addr):
                    linked_contracts.add(addr)
                else:
                    sig.add("linked")
                    linked_hits.append((pool_key, addr))

    for pool_key, res in data.get("denoms", {}).items():
        res["signals"] = {}
        res["confidence"] = {}
        res["discrimination"] = {}
        for addr in res["detail"]:
            sig = signals.get((pool_key, addr), set())
            disc = discriminations.get((pool_key, addr), 0.0)
            res["signals"][addr] = sorted(sig)
            res["discrimination"][addr] = round(disc, 4)
            res["confidence"][addr] = _score(sig, disc)

    data["heuristics"] = {
        "deposit_gas_prices": sorted(deposit_gas),
        "gas_price_matches": gas_matches,
        "linked_addresses": linked_hits,
        "linked_contracts": sorted(linked_contracts),
    }
    return data


SIGNAL_LABEL = {
    "count_match": "Amount + timing (count match)",
    "self_relayed": "Self-relayed exit (paid own gas)",
    "gas_price": "Unique gas-price reuse",
    "linked": "Linked address (direct counterparty)",
    "profile_match": "Denomination-profile match",
}


def credit_profile_match(data: dict, pool_keys: list[str], address: str) -> None:
    """Record that ``address`` received this wallet's whole fingerprint.

    Called from :func:`tornado_demix.multi.correlate`. A single pool is not a
    fingerprint (it is the count match again), so it is not credited.
    """
    if len(pool_keys) < 2:
        return
    for pool_key in pool_keys:
        res = data.get("denoms", {}).get(pool_key)
        if not res or address not in res.get("signals", {}):
            continue
        signals = set(res["signals"][address])
        if "profile_match" in signals:
            continue
        signals.add("profile_match")
        res["signals"][address] = sorted(signals)
        res["confidence"][address] = _score(
            signals, res.get("discrimination", {}).get(address, 0.0)
        )


def _score(signals, discrimination):
    """Noisy-OR across evidence families; within a family only the strongest
    signal counts, so dependent signals are not added twice. count_match is
    scaled by how much the count actually narrowed the field."""
    strongest = {}
    for signal in signals:
        weight = SIGNAL_WEIGHTS.get(signal, 0.0)
        if signal == "count_match":
            weight *= discrimination
        family = SCORE_FAMILY.get(signal, signal)
        strongest[family] = max(strongest.get(family, 0.0), weight)
    product = 1.0
    for weight in strongest.values():
        product *= 1 - weight
    return round(1 - product, 4)


def candidate_reason(row: dict) -> str:
    """Return a short, human sentence explaining why an address was chosen."""
    bits = []
    if "count_match" in row["signals"]:
        bits.append(
            f"received exactly {row['hits']} withdrawals, matching a {row['hits']}-note voucher"
        )
    else:
        bits.append(f"received {row['hits']} qualifying withdrawal(s)")
    if row.get("self_relayed"):
        bits.append(f"{row['self_relayed']} of them self-relayed (recipient paid gas)")
    if "gas_price" in row["signals"]:
        bits.append("reused one of the wallet's deposit gas prices")
    if "linked" in row["signals"]:
        bits.append("transacts directly with the depositor outside Tornado")
    text = "; ".join(bits)
    return text[0].upper() + text[1:] + "."


EVIDENCE_LABEL = {
    "count_match": "amount + timing",
    "self_relayed": "self-relay (same family as amount + timing)",
    "gas_price": "gas price",
    "linked": "linked address",
    "profile_match": "denomination profile",
}


def _linked_detail(data, signals, address):
    if "linked" in signals:
        return "transacts directly with the depositor outside Tornado"
    if address in data.get("heuristics", {}).get("linked_contracts", []):
        return "transacts with the depositor, but is a contract (router, DEX, service): not counted"
    return "no direct transaction with the depositor"


def candidate_evidence(data: dict, pool_key: str, address: str) -> list[dict]:
    """Every evidence line for one candidate, including those that do not hold.

    Each item: {signal, label, family, holds, detail}. Attribution is not evidence.
    """
    res = data["denoms"][pool_key]
    signals = set(res.get("signals", {}).get(address, []))
    recs = res["detail"][address]
    hits = res["counts"][address]
    targets = sorted(res.get("target_counts", []))
    field = res.get("unique_recipients") or len(res.get("counts", {}))
    sharing = sum(1 for c in res["counts"].values() if c == hits)
    disc = res.get("discrimination", {}).get(address, 0.0)

    if hits in targets:
        count_detail = (
            f"{hits} withdrawal(s) = a {hits}-note voucher; {sharing} of {field} "
            f"recipients in the window share this count; disc {disc:.2f} "
            f"(counted from {MIN_COUNT_DISCRIMINATION:.2f})"
        )
        if field < MIN_FIELD_SIZE:
            count_detail += f"; a field of {field} is too small to count (needs {MIN_FIELD_SIZE})"
    else:
        count_detail = (
            f"{hits} withdrawal(s); voucher size(s) {', '.join(map(str, targets)) or '-'}"
        )

    n_self = sum(1 for r in recs if r.get("self_relayed"))
    gas = [
        gp
        for pk, addr, gp, _tx in data.get("heuristics", {}).get("gas_price_matches", [])
        if pk == pool_key and addr == address
    ]
    lines = [
        ("count_match", count_detail),
        (
            "self_relayed",
            f"{n_self} of {hits} withdrawal(s) sent without a relayer"
            if n_self
            else "every withdrawal went through a relayer",
        ),
        (
            "gas_price",
            f"withdrawal gas price {gas[0] / 1e9:.3f} gwei equals a deposit's"
            if gas
            else "no deposit gas price reused (or not checkable after EIP-1559)",
        ),
        ("linked", _linked_detail(data, signals, address)),
    ]
    if "profile_match" in signals:
        lines.append(("profile_match", "received the wallet's full multi-pool fingerprint"))
    return [
        {
            "signal": signal,
            "label": EVIDENCE_LABEL[signal],
            "family": METHOD_FAMILY.get(signal, "profile"),
            "holds": signal in signals,
            "detail": detail,
        }
        for signal, detail in lines
    ]


def method_breakdown(data: dict) -> list[dict]:
    """Group candidate addresses by the signal that flagged them: [{method, label, rows}]."""
    order = ["count_match", "self_relayed", "gas_price", "linked"]
    per = {s: [] for s in order}
    for pool_key, res in data.get("denoms", {}).items():
        target = set(res.get("target_counts", []))
        for addr, sig in res.get("signals", {}).items():
            recs = res["detail"][addr]
            hits = res["counts"][addr]
            n_self = sum(1 for r in recs if r.get("self_relayed"))
            is_candidate = hits in target
            include = {
                "count_match": "count_match" in sig and is_candidate,
                "self_relayed": "self_relayed" in sig and "count_match" in sig,
                "gas_price": "gas_price" in sig,
                "linked": "linked" in sig,
            }
            for s in order:
                if not include[s]:
                    continue
                reason = {
                    "count_match": f"hit {hits}x = voucher size",
                    "self_relayed": f"{n_self}/{hits} withdrawals self-relayed",
                    "gas_price": "gas price equals a deposit's",
                    "linked": "direct counterparty of the depositor",
                }[s]
                per[s].append(
                    {
                        "address": addr,
                        "pool_key": pool_key,
                        "denom": res["denom"],
                        "asset": res["asset"],
                        "hits": hits,
                        "reason": reason,
                        "confidence": res.get("confidence", {}).get(addr, 0.0),
                    }
                )
    out = []
    for s in order:
        rows = sorted(per[s], key=lambda r: (-r["confidence"], -r["hits"]))
        out.append({"method": s, "label": SIGNAL_LABEL[s], "rows": rows})
    return out


# Evidence families. count_match and self_relayed read the same withdrawals, so
# they are one family: two families need a gas-price reuse or a linked address.
METHOD_FAMILY = {
    "count_match": "amount+timing",
    "self_relayed": "amount+timing",
    "gas_price": "gas price",
    "linked": "linked address",
}


# A profile match reads the same withdrawal counts as count_match.
SCORE_FAMILY = {**METHOD_FAMILY, "profile_match": "amount+timing"}


# The score is uncalibrated and a percentage reads as a probability, so results
# are presented by band: how many independent families support the address.
BAND_ORDER = {"strong": 3, "moderate": 2, "weak": 1}


def confidence_band(signals):
    """Return "strong", "moderate" or "weak" from the candidate's signal set.

    strong: two or more families; moderate: one family with a structural tie
    (count_match + self_relayed is one family, so moderate); weak: count only.
    """
    families = {METHOD_FAMILY[s] for s in signals if s in METHOD_FAMILY}
    if len(families) >= 2:
        return "strong"
    if signals & {"linked", "gas_price", "self_relayed"}:
        return "moderate"
    return "weak"


def band_rationale(band: str, signals: set[str]) -> str:
    """Plain-language reason for the band; never claims a signal the candidate lacks."""
    if band == "strong":
        return "corroborated by two or more independent families of evidence"
    if band == "weak":
        return "amount and timing only - no independent corroboration"
    if "count_match" in signals:
        return "an amount+timing match plus a corroborating tie"
    return "a structural signal (linked address or gas price) on its own"


def cross_method(data: dict, min_methods: int = 2) -> list[dict]:
    """Addresses selected by two or more *independent* methods.

    Independence is by family (:data:`METHOD_FAMILY`), not by signal name.
    ``n_methods`` counts families; ``methods`` still lists every individual
    method that fired, so the report can show all of them.
    """
    agg = {}
    for m in method_breakdown(data):
        for r in m["rows"]:
            key = (r["address"], r["pool_key"])
            e = agg.setdefault(
                key,
                {
                    "methods": [],
                    "reasons": [],
                    "families": set(),
                    "conf": 0.0,
                    "hits": r["hits"],
                    "denom": r["denom"],
                    "asset": r["asset"],
                },
            )
            e["methods"].append(m["label"])
            e["families"].add(METHOD_FAMILY.get(m["method"], m["method"]))
            e["reasons"].append(f"{m['label']}: {r['reason']}")
            e["conf"] = max(e["conf"], r.get("confidence", 0.0))
    out = []
    for (addr, pool_key), e in agg.items():
        if len(e["families"]) >= min_methods:
            out.append(
                {
                    "address": addr,
                    "pool_key": pool_key,
                    "denom": e["denom"],
                    "asset": e["asset"],
                    "methods": e["methods"],
                    "reasons": e["reasons"],
                    "n_methods": len(e["families"]),
                    "families": sorted(e["families"]),
                    "hits": e["hits"],
                    "confidence": e["conf"],
                }
            )
    out.sort(key=lambda r: (-r["n_methods"], -r["confidence"]))
    return out


def conclusion(data: dict) -> str:
    """Summarise the outcome as labelled lines: the top candidate, the evidence
    families behind it, the candidate count per band, and the limits."""
    cands = ranked_candidates(data)
    lines = []
    if cands:
        top = cands[0]
        families = sorted({SCORE_FAMILY.get(s, s) for s in top["signals"]})
        lines.append(
            f"Top candidate: {top['address']} ({top['pool_key']}), band {top['band']}, "
            f"score {top['confidence']:.2f}."
        )
        lines.append(f"Evidence families: {', '.join(families)} ({len(families)} independent).")
        lines.append(f"Basis: {candidate_reason(top)}")
        per_band = [
            f"{sum(c['band'] == band for c in cands)} {band}"
            for band in sorted(BAND_ORDER, key=BAND_ORDER.get, reverse=True)
        ]
        lines.append(f"Candidates: {', '.join(per_band)}.")
    else:
        lines.append("Top candidate: none; no count match narrows its field enough to count.")
    single = [v for v in data.get("vouchers", []) if v["count"] == 1]
    if single:
        lines.append(
            f"Single-note vouchers: {len(single)}. A count of one does not narrow the "
            f"field; trace the deposit funding source instead."
        )
    lines.append(
        "Limitations: leads, not proof. The score is uncalibrated and only orders "
        "candidates within a band; confirm any address with independent evidence "
        "(funding source, exchange KYC, further hops)."
    )
    return "\n".join(lines)


def ranked_candidates(
    data: dict,
    min_confidence: float = 0.0,
    limit: int | None = None,
    attribution: dict[str, dict] | None = None,
) -> list[dict]:
    """Return exit-address candidates, strongest band first, then by score.

    Each item: {address, pool_key, denom, asset, hits, confidence, signals,
    band, self_relayed, total_value, first_ts, last_ts, discrimination,
    field_size, evidence, attribution}. ``confidence`` is the noisy-OR score
    used for ordering; ``band`` and ``evidence`` (see
    :func:`candidate_evidence`) describe the evidence.

    ``attribution`` is an optional ``{address_lower: label_dict}`` map (see
    :func:`tornado_demix.attribution.load_attribution`). When given, each row's
    ``attribution`` is the one-line label of a known address (exchange, bridge,
    mixer, sanctioned...) and empty when the address is not in the set. Without a
    set every row's ``attribution`` is empty, so the field is always present and
    consumers need no special-casing.
    """
    rows = []
    for pool_key, res in data.get("denoms", {}).items():
        for addr, recs in res["detail"].items():
            hits = res["counts"][addr]
            sig = res.get("signals", {}).get(addr, [])
            conf = res.get("confidence", {}).get(addr, 0.0)
            # Admit an address only on a wallet-specific signal or a voucher-sized
            # count that narrows the field. Self-relaying is a property of the
            # withdrawal, not a link to this wallet, so it never admits alone.
            disc = res.get("discrimination", {}).get(addr, 0.0)
            discriminating = "linked" in sig or "gas_price" in sig or "count_match" in sig
            if not discriminating or conf < min_confidence:
                continue
            rows.append(
                {
                    "address": addr,
                    "pool_key": pool_key,
                    "denom": res["denom"],
                    "asset": res["asset"],
                    "hits": hits,
                    "confidence": conf,
                    "signals": sig,
                    "band": confidence_band(set(sig)),
                    "self_relayed": sum(1 for r in recs if r.get("self_relayed")),
                    "total_value": round(sum(r["value"] for r in recs), 6),
                    "first_ts": min(r["ts"] for r in recs),
                    "last_ts": max(r["ts"] for r in recs),
                    "discrimination": round(disc, 4),
                    "field_size": res.get("unique_recipients") or len(res["counts"]),
                    "evidence": candidate_evidence(data, pool_key, addr),
                    "attribution": format_label(label_of(attribution, addr)) if attribution else "",
                }
            )
    # Band first: the score orders leads within a band but cannot lift a
    # single-family lead above a corroborated one.
    rows.sort(key=lambda r: (-BAND_ORDER[r["band"]], -r["confidence"], -r["hits"]))
    return rows[:limit] if limit else rows
