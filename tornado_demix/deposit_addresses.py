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
  exchange, or ordinary (non-contract) addresses with at least ``HOT_WALLET_TXS``
  transactions to which ``X`` forwards amounts it has just received (within
  ``FORWARD_BLOCKS``, short by at most 0.01 ETH or 1 % of a token amount). An
  exchange sweeps to several hot wallets, so the share is summed over them; a
  token, DEX router or Tornado contract is busy but not an exchange.

Only the depositor's own outgoing counterparties are examined, busiest first and at
most ``MAX_CANDIDATES`` of them, so the cost is bounded: two history queries per
examined address and one busy check per sweep target.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

from .errors import ApiError, ApiKeyError

MAX_CANDIDATES = 25  # depositor counterparties examined, busiest first
MAX_DEPOSIT_TXS = 1000  # a deposit address is quiet; more rows: not one
MAX_DEPOSIT_SENDERS = 50  # distinct senders a per-customer address can have
MIN_SWEEP_SHARE = 0.8  # share of outgoing transfers that go to hot wallets
MAX_SWEEP_TARGETS = 3  # most frequent destinations checked for a hot wallet
HOT_WALLET_TXS = 10000  # a sweep target this busy is taken for a hot wallet
FORWARD_BLOCKS = 3200  # a sweep follows the incoming transfer within this many blocks
FORWARD_ETH_WEI = 10**16  # ...and forwards it less at most 0.01 ETH (Victor, FC 2020)
FORWARD_TOKEN_SHARE = 0.01  # tokens: forwards at least 99 % of the incoming amount


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


def _asset(row: dict) -> str:
    return (row.get("contractAddress") or "").lower() or "eth"


def forwards_to(rows: list[dict], address: str, target: str) -> bool:
    """True when ``address`` sent ``target`` an amount it had just received: an
    incoming transfer of the same asset at most FORWARD_BLOCKS earlier, and the
    sweep short of it by at most 0.01 ETH (ETH) or 1 % (tokens). This is how an
    exchange deposit address behaves (the forwarding test of Victor, FC 2020, used
    by Tutela); a personal wallet that merely pays a busy address does not."""
    incoming = [
        r
        for r in rows
        if (r.get("to") or "").lower() == address and str(r.get("value", "0")).isdigit()
    ]
    for out in rows:
        if (out.get("from") or "").lower() != address or (out.get("to") or "").lower() != target:
            continue
        try:
            ob, ov = int(out.get("blockNumber", 0)), int(out.get("value", 0))
        except ValueError:
            continue
        if ov <= 0:
            continue
        for inc in incoming:
            if _asset(inc) != _asset(out):
                continue
            ib, iv = int(inc.get("blockNumber", 0)), int(inc["value"])
            if not 0 <= ob - ib <= FORWARD_BLOCKS or iv < ov:
                continue
            if _asset(out) == "eth":
                if iv - ov <= FORWARD_ETH_WEI:
                    return True
            elif iv - ov <= iv * FORWARD_TOKEN_SHARE:
                return True
    return False


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
        if _is_exchange(labels, target):
            hot.append((target, n))
        elif (
            busy is not None
            and not (is_contract is not None and is_contract(target))
            and forwards_to(rows, address, target)
            and busy(target)
        ):
            # Without a label, a hot wallet is an ordinary (non-contract) busy address
            # that receives forwarded deposits: a token, a DEX router or a Tornado
            # contract is busy too but is not an exchange, and a wallet that merely
            # pays a busy address does not forward what it received.
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
    errors: list | None = None,
) -> list[dict]:
    """Deposit addresses the wallet sent funds to (see the module docstring).

    Explorer errors on single addresses are not fatal (the address is skipped); when
    ``errors`` is a list, each skip is appended to it as ``(address, message)`` so a
    caller that needs complete results (an evaluation) can retry instead.
    """
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
            try:
                busy_cache[target] = bool(has and has(target, HOT_WALLET_TXS))
            except ApiKeyError:
                raise
            except ApiError as exc:
                # Some explorers refuse a 10,000-row page for a busy address
                # (Routescan answers with an HTML error page). An unchecked
                # target is not taken for a hot wallet: the signal is lost for
                # this address, the run is not.
                busy_cache[target] = False
                if errors is not None:
                    errors.append((target, str(exc)[:200]))
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
        try:
            info = classify_deposit_address(client, addr, is_contract, labels, busy)
        except ApiKeyError:
            raise
        except ApiError as exc:
            if errors is not None:
                errors.append((addr, str(exc)[:200]))
            continue  # history unreadable: this counterparty is not examined
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
