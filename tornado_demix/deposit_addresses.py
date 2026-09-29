"""Exchange deposit addresses shared by a depositor and a withdrawal recipient.

A centralised exchange gives each customer their own deposit address and sweeps
what arrives there to a hot wallet. Two addresses that both sent funds to the
same deposit address are, with few exceptions, one customer: the same shape as
the address-reuse (DAR) heuristic of Tutela (Béres et al.). It is a linked-address
signal reached through an intermediary instead of a direct transfer.

An address ``X`` counts as a deposit address when all of the following hold:

* it is not a contract and has fewer than ``MAX_DEPOSIT_TXS`` transactions;
* at most ``MAX_DEPOSIT_SENDERS`` distinct addresses sent it funds;
* at least ``MIN_SWEEP_SHARE`` of its outgoing transfers go to hot wallets: among
  its ``MAX_SWEEP_TARGETS`` most frequent destinations, those labelled as an
  exchange or with at least ``HOT_WALLET_TXS`` transactions (an exchange sweeps
  to several hot wallets, so the share is summed over them).

Only the depositor's own outgoing counterparties are examined, busiest first and at
most ``MAX_CANDIDATES`` of them, so the cost is bounded: two history queries per
examined address and one busy check per sweep target.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

MAX_CANDIDATES = 25  # depositor counterparties examined, busiest first
MAX_DEPOSIT_TXS = 1000  # a deposit address is quiet; more rows: not one
MAX_DEPOSIT_SENDERS = 50  # distinct senders a per-customer address can have
MIN_SWEEP_SHARE = 0.8  # share of outgoing transfers that go to hot wallets
MAX_SWEEP_TARGETS = 3  # most frequent destinations checked for a hot wallet
HOT_WALLET_TXS = 10000  # a sweep target this busy is taken for a hot wallet


def _rows(client, action: str, address: str) -> list[dict]:
    """Up to MAX_DEPOSIT_TXS rows of one account list, oldest first."""
    call = getattr(client, "call", None)  # a stub client without raw queries finds none
    if call is None:
        return []
    rows = call(
        {
            "module": "account",
            "action": action,
            "address": address,
            "page": 1,
            "offset": MAX_DEPOSIT_TXS,
            "sort": "asc",
        }
    )
    return rows if isinstance(rows, list) else []


def _is_exchange(labels: dict | None, address: str) -> bool:
    return bool(labels) and (labels.get(address) or {}).get("category") == "exchange"


def classify_deposit_address(
    client,
    address: str,
    is_contract: Callable[[str], bool | None] | None = None,
    labels: dict | None = None,
    busy: Callable[[str], bool] | None = None,
) -> dict | None:
    """Return {address, sweep_target, exchange, senders} if ``address`` looks like
    an exchange deposit address, else None."""
    address = address.lower()
    if is_contract is not None and is_contract(address):
        return None
    txs = _rows(client, "txlist", address)
    if len(txs) >= MAX_DEPOSIT_TXS:
        return None
    tokens = _rows(client, "tokentx", address)
    rows = txs + tokens
    senders = {
        (r.get("from") or "").lower()
        for r in rows
        if (r.get("to") or "").lower() == address and r.get("from")
    } - {address}
    if not senders or len(senders) > MAX_DEPOSIT_SENDERS:
        return None
    outgoing = Counter(
        (r.get("to") or "").lower()
        for r in rows
        if (r.get("from") or "").lower() == address and r.get("to")
    )
    if not outgoing:
        return None
    total = sum(outgoing.values())
    hot = []
    for target, n in outgoing.most_common(MAX_SWEEP_TARGETS):
        if _is_exchange(labels, target) or (busy is not None and busy(target)):
            hot.append((target, n))
    if not hot or sum(n for _t, n in hot) / total < MIN_SWEEP_SHARE:
        return None
    target = hot[0][0]
    entities = sorted(
        {(labels or {}).get(t, {}).get("entity", "") for t, _n in hot if _is_exchange(labels, t)}
        - {""}
    )
    return {
        "address": address,
        "sweep_target": target,
        "sweep_targets": [t for t, _n in hot],
        "exchange": ", ".join(entities),
        "senders": sorted(senders),
    }


def depositor_deposit_addresses(
    client,
    wallet: str,
    txs: list[dict],
    is_contract: Callable[[str], bool | None] | None = None,
    labels: dict | None = None,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    """Deposit addresses the wallet sent funds to (see the module docstring)."""
    wallet = wallet.lower()
    sent = Counter(
        (t.get("to") or "").lower()
        for t in txs
        if (t.get("from") or "").lower() == wallet and t.get("to")
    )
    busy_cache: dict[str, bool] = {}

    def busy(target: str) -> bool:
        if target not in busy_cache:
            has = getattr(client, "has_at_least_txs", None)
            busy_cache[target] = bool(has and has(target, HOT_WALLET_TXS))
        return busy_cache[target]

    found = []
    examined = 0
    for addr, _n in sent.most_common():
        if examined >= MAX_CANDIDATES:
            break
        if addr in exclude or addr == wallet:
            continue
        # A labelled address is a service's own wallet, not a customer address.
        if labels and addr in labels:
            continue
        examined += 1
        info = classify_deposit_address(client, addr, is_contract, labels, busy)
        if info:
            found.append(info)
    return found


def shared_deposit_hits(deposit_addresses: list[dict], recipients, wallet: str) -> dict:
    """{recipient: [deposit addresses]} for recipients that also sent to one of them."""
    wallet = wallet.lower()
    out: dict[str, list[str]] = {}
    recips = set(recipients)
    for info in deposit_addresses:
        for s in info["senders"]:
            if s != wallet and s in recips:
                out.setdefault(s, []).append(info["address"])
    return out
