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
        "relayers": dict(sorted(relayers.items(), key=lambda kv: -kv[1]["withdrawals"])),
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
    sender_of = getattr(client, "tx_sender", None)
    if sender_of is None:
        return 0

    resolved, seen = 0, {}
    for lead in leads[:limit]:
        tx_hash = lead.get("tx_hash")
        if not tx_hash:
            continue
        if tx_hash not in seen:
            seen[tx_hash] = sender_of(tx_hash)
        sender = seen[tx_hash]
        if not sender:
            continue
        lead["broadcaster"] = sender
        lead["broadcaster_status"] = (
            "self" if sender.lower() == (lead.get("to") or "").lower() else "other"
        )
        resolved += 1
    return resolved
