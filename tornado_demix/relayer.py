"""Relayer analysis over Tornado.Cash Withdrawal events.

Withdrawals are usually broadcast by a relayer, who pays the gas and takes a
fee. A self-relayed withdrawal (no relayer in the proof) was sent from an address
that already held gas money, a funded identity next to the exit. That is an
inference until :func:`verify_broadcaster` reads the sender from the transaction:
``withdraw()`` may be called by anyone. A named relayer taking a zero fee is not
self-relaying (``zero_fee_relayed`` in the decoded event).

The module does not report "nullifier double spends": the pool contract enforces
nullifier uniqueness, so a repeated nullifier can only be a data-source artefact.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .constants import ZERO_ADDRESS
from .etherscan import EtherscanClient


def analyze_relayers(withdrawals: list[dict]) -> dict:
    """Summarise relayer usage across decoded Withdrawal events.

    Fees are in the pool's own asset. ``asset`` records which one; when a
    relayer served pools of different assets the total is not meaningful, so
    the field reports the first asset seen rather than implying a sum.

    Returns a dict with per-relayer counts, fee statistics, recipient diversity,
    and the list of self-relayed withdrawals.
    """
    if not withdrawals:
        return {
            "total": 0,
            "unique_relayers": 0,
            "self_relayed": [],
            "relayers": {},
        }

    counts = Counter()
    fees = defaultdict(list)
    assets = {}
    recipients = defaultdict(set)
    self_relayed = []

    for w in withdrawals:
        if w.get("self_relayed"):
            self_relayed.append(w)
            continue
        relayer = w.get("relayer") or ZERO_ADDRESS
        counts[relayer] += 1
        fees[relayer].append(w.get("fee", 0.0))
        assets.setdefault(relayer, w.get("asset", ""))
        if w.get("to"):
            recipients[relayer].add(w["to"])

    relayers = {}
    for relayer, n in counts.items():
        fee_list = fees[relayer]
        relayers[relayer] = {
            "withdrawals": n,
            "avg_fee": round(sum(fee_list) / len(fee_list), 8) if fee_list else 0.0,
            "total_fee": round(sum(fee_list), 8),
            "asset": assets.get(relayer, ""),
            "unique_recipients": len(recipients[relayer]),
        }

    return {
        "total": len(withdrawals),
        "unique_relayers": len(counts),
        "self_relayed": self_relayed,
        "relayers": dict(sorted(relayers.items(), key=lambda kv: (-kv[1]["withdrawals"], kv[0]))),
    }


def collect_withdrawals(data: dict) -> list[dict]:
    """Gather all decoded Withdrawal events from a run_demix() result."""
    out = []
    for res in data.get("denoms", {}).values():
        out.extend(res.get("withdrawals", []))
    return out


def self_relayed_candidates(data: dict) -> list[dict]:
    """Return self-relayed withdrawals that are also count-matched candidates.

    These are the highest-value leads: the recipient both matches the voucher
    size and paid their own gas, tying a funded address to the exit.

    Each lead is a decoded Withdrawal event plus the pool it came out of:
    ``pool_key`` ('1 ETH'), ``denom`` (a float) and ``asset``.
    """
    leads = []
    for pool_key, res in data.get("denoms", {}).items():
        candidate_set = set()
        for n in res.get("target_counts", []):
            candidate_set.update(res.get("candidates_by_count", {}).get(n, []))
        for w in res.get("withdrawals", []):
            if w.get("self_relayed") and w.get("to") in candidate_set:
                leads.append(
                    {**w, "pool_key": pool_key, "denom": res["denom"], "asset": res["asset"]}
                )
    return leads


# Fee settings of these other withdrawals are read only to compare them with the
# leads; the cap bounds the extra explorer calls.
MAX_FINGERPRINT_LOOKUPS = 150
# At most this many withdrawals are read per other recipient.
FINGERPRINT_PER_RECIPIENT = 3


def fingerprint_context(
    data: dict, leads: list[dict], min_hits: int = 2, limit: int = MAX_FINGERPRINT_LOOKUPS
) -> list[dict]:
    """Self-relayed withdrawals of other multi-note recipients in the windows.

    A lead's fee settings say more when the same settings turn up at another
    recipient that took several notes (one tool splitting a voucher over exits).
    Returns lead-shaped items with ``context: True`` - up to
    ``FINGERPRINT_PER_RECIPIENT`` withdrawals per recipient, larger pools first,
    then recipients with more notes - at most ``limit``.
    """
    have = {lead.get("tx_hash") for lead in leads}
    lead_to = {(lead.get("to") or "").lower() for lead in leads}
    items = []
    for pool_key, res in data.get("denoms", {}).items():
        for addr, recs in res.get("detail", {}).items():
            if len(recs) < min_hits or addr.lower() in lead_to:
                continue
            own = [r for r in recs if r.get("self_relayed") and r.get("hash") not in have]
            for r in sorted(own, key=lambda r: r["ts"])[:FINGERPRINT_PER_RECIPIENT]:
                items.append(
                    {
                        "to": addr,
                        "tx_hash": r["hash"],
                        "pool_key": pool_key,
                        "ts": r["ts"],
                        "context": True,
                        "_rank": (-float(res.get("denom") or 0), -len(recs)),
                    }
                )
    # Larger pools first (their exits matter most), then recipients with more notes.
    items.sort(key=lambda x: (x["_rank"], x["to"], x["ts"]))
    return items[:limit]


# A cap on how many broadcaster lookups one run will make. Leads are already
# narrowed to count-matched candidates, so this is a backstop against a pool
# window that produced an unexpectedly large set - not an expected limit.
MAX_BROADCASTER_LOOKUPS = 200


def verify_broadcaster(
    client: EtherscanClient, leads: list[dict], limit: int = MAX_BROADCASTER_LOOKUPS
) -> int:
    """Resolve who actually broadcast each self-relayed lead.

    Mutates each lead in place, setting ``broadcaster`` to the sending address
    and ``broadcaster_status`` to one of:

    * ``"self"``      - the sender is the recipient. The claim the report makes
      about self-relayed exits is then established, not inferred.
    * ``"other"``     - someone else sent it. Still a lead, and often a better
      one: that address funded the gas and is a separate identity to pull on.
    * ``"unverified"``- the provider could not serve the transaction. Left
      explicit so an unchecked lead never reads as a checked one.

    Returns the number of leads actually resolved. Requires a client with
    ``tx_sender``; anything else leaves every lead unverified rather than
    failing the run.
    """
    info_of = getattr(client, "tx_info", None)
    sender_of = getattr(client, "tx_sender", None)
    if info_of is None and sender_of is None:
        return 0

    resolved, seen = 0, {}
    for lead in leads[:limit]:
        tx_hash = lead.get("tx_hash")
        if not tx_hash:
            continue
        if tx_hash not in seen:
            seen[tx_hash] = info_of(tx_hash) if info_of is not None else sender_of(tx_hash)
        got = seen[tx_hash]
        if isinstance(got, dict):
            # The same transaction object carries the fee settings: read them here
            # rather than spend a second lookup.
            lead["fee_profile"] = fee_profile(got)
            sender = (got.get("from") or "").lower() or None
        else:
            sender = got
        if not sender:
            continue
        lead["broadcaster"] = sender
        lead["broadcaster_status"] = (
            "self" if sender.lower() == (lead.get("to") or "").lower() else "other"
        )
        resolved += 1
    return resolved


def _hex_int(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def fee_profile(tx: dict) -> dict:
    """Fee settings of a transaction: type, priority fee, max fee, gas price (wei), gas limit.

    The sender's wallet or script chooses these. Identical settings across
    withdrawals to different addresses suggest one tool or one operator; they are
    context, never a lead (many users share wallet defaults).
    """
    return {
        "type": tx.get("type"),
        "priority_fee_wei": _hex_int(tx.get("maxPriorityFeePerGas")),
        "max_fee_wei": _hex_int(tx.get("maxFeePerGas")),
        "gas_price_wei": _hex_int(tx.get("gasPrice")),
        "gas_limit": _hex_int(tx.get("gas")),
    }


# Gas limits of one tool vary by a few dozen units between proofs, so they are
# compared in buckets of this size.
GAS_LIMIT_BUCKET = 1000


def fee_key(profile: dict | None) -> tuple | None:
    """Comparable fee fingerprint: (kind, fee in wei, gas-limit bucket), or None."""
    if not profile or profile.get("gas_limit") is None:
        return None
    if profile.get("priority_fee_wei") is not None:
        kind, fee = "priority", profile["priority_fee_wei"]
    elif profile.get("gas_price_wei") is not None:
        kind, fee = "legacy", profile["gas_price_wei"]
    else:
        return None
    return (kind, fee, profile["gas_limit"] // GAS_LIMIT_BUCKET)


def _gwei(wei: int) -> str:
    return ("%.4f" % (wei / 1e9)).rstrip("0").rstrip(".")


def fee_label(key: tuple | None) -> str:
    """Human label of a :func:`fee_key`, e.g. ``priority 3 gwei, gas ~428k``."""
    if not key:
        return "-"
    kind, fee, bucket = key
    return f"{kind} {_gwei(fee)} gwei, gas ~{bucket}k"


def fee_fingerprints(leads: list[dict]) -> list[dict]:
    """Group self-relayed leads by identical fee settings.

    Returns one item per fingerprint, most recipients first:
    {key, label, recipients: [...], pools: [...], withdrawals: n, shared: bool}.
    ``shared`` marks a fingerprint seen at two or more recipients, at least one of
    them a lead (items with ``context: True`` come from :func:`fingerprint_context`)
    - a hint that one tool or operator withdrew to all of them. Context only.
    """
    groups: dict[tuple, dict] = {}
    for lead in leads:
        key = fee_key(lead.get("fee_profile"))
        if key is None:
            continue
        g = groups.setdefault(
            key, {"recipients": set(), "pools": set(), "withdrawals": 0, "lead": False}
        )
        g["lead"] = g["lead"] or not lead.get("context")
        g["recipients"].add((lead.get("to") or "").lower())
        g["pools"].add(lead.get("pool_key"))
        g["withdrawals"] += 1
    out = [
        {
            "key": list(key),
            "label": fee_label(key),
            "recipients": sorted(g["recipients"]),
            "pools": sorted(p for p in g["pools"] if p),
            "withdrawals": g["withdrawals"],
            "shared": len(g["recipients"]) > 1 and g["lead"],
        }
        for key, g in groups.items()
    ]
    out.sort(key=lambda g: (-len(g["recipients"]), -g["withdrawals"], g["label"]))
    return out


def record_fee_profiles(data: dict, leads: list[dict]) -> list[dict]:
    """Store fee profiles of verified leads in ``data`` (for the reports) and return
    the fingerprint groups.

    Sets ``data["fee_profiles"]`` ({tx_hash: profile}) and
    ``data["fee_fingerprints"]`` (see :func:`fee_fingerprints`).
    """
    data["fee_profiles"] = {
        lead["tx_hash"]: lead["fee_profile"] for lead in leads if lead.get("fee_profile")
    }
    data["fee_fingerprints"] = fee_fingerprints(leads)
    return data["fee_fingerprints"]
