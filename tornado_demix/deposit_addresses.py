"""Exchange deposit addresses shared by a depositor and a withdrawal recipient.

A centralised exchange gives each customer their own deposit address and sweeps
what arrives there to a hot wallet. Two addresses that both sent funds to the
same deposit address are, with few exceptions, one customer: the same shape as
the address-reuse (DAR) heuristic of Tutela (Béres et al.). It is a linked-address
signal reached through an intermediary instead of a direct transfer. Since rule
2.21 a match is context only (``heuristics.CONTEXT_SIGNALS``): in the second
hold-out it did not stand above chance. The classifier below decides which
addresses are listed as deposit addresses.

An address ``X`` counts as a deposit address when all of the following hold:

* it is not a contract (an EIP-7702 delegated account is an EOA, see
  ``rpc.make_contract_check``) and has fewer than ``MAX_DEPOSIT_TXS`` transactions;
* at most ``MAX_DEPOSIT_SENDERS`` distinct addresses sent it funds;
* at least ``MIN_SWEEP_SHARE`` of its outgoing transfers go to hot wallets: among
  its ``MAX_SWEEP_TARGETS`` most frequent destinations, those labelled as an
  exchange, or ordinary (non-contract) addresses with at least ``HOT_WALLET_TXS``
  transactions to which ``X`` forwards what it has just received. An exchange
  sweeps to several hot wallets, so the share is summed over them; a token, DEX
  router or Tornado contract is busy but not an exchange.

Forwarding test: an outgoing transfer forwards when an incoming transfer of the
same asset arrived at most ``FORWARD_SECONDS`` earlier (by ``timeStamp``; only when
a timestamp is missing, ``_FALLBACK_BLOCKS`` blocks) and the sweep is short of it by
at most 0.01 ETH (ETH) or 1 % (tokens); or, for an aggregated sweep, when the sum of
the incoming transfers of that asset received in the window and after the previous
outgoing transfer of that asset (to anyone) covers the outgoing value with the same
tolerance.

Which rows are counted. The same transfer is listed by ``txlist`` (the call to the
token contract, value 0) and by ``tokentx`` (the token movement); only the second
is counted, so one token transfer is one transfer: ``txlist`` rows with value 0 are
dropped, ETH-value rows are kept. Address poisoning is filtered in a limited,
offline way: zero-value transfers (of ETH or of any token) are dropped, and a
``tokentx`` transfer out of ``X`` whose transaction is not among the ``txlist`` rows
sent by ``X`` was not initiated by ``X`` (a ``transferFrom`` spoof) and is dropped.
Limits: a spoofed non-zero transfer *into* ``X`` from a fake token cannot be told
from a real one without token metadata, and a spoof routed through a contract that
``X`` itself called is not detected.

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
# A sweep follows the incoming transfer within this time: 3200 Ethereum blocks of
# 12 s, kept as a duration so that chains with other block times are not distorted.
FORWARD_SECONDS = 38400
_FALLBACK_BLOCKS = 3200  # the same window in blocks, only for rows without timeStamp
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


def _num(value) -> int | None:
    """An explorer number (decimal, or 0x hex in a few stubs); None if unreadable."""
    text = str(value).strip().lower()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


def _value(row: dict) -> int:
    v = _num(row.get("value", 0))
    return v if v is not None and v > 0 else 0


def _addr(row: dict, key: str) -> str:
    return (row.get(key) or "").lower()


def _block(row: dict) -> int:
    b = _num(row.get("blockNumber"))
    return b if b is not None else 0


def transfers(address: str, txs: list[dict], tokens: list[dict]) -> list[dict]:
    """The real, non-zero transfers of ``address``: ``txlist`` rows with value (the
    value-0 calls to token contracts are the same movement as the ``tokentx`` row and
    are not counted twice) plus ``tokentx`` rows with value, minus outgoing token
    transfers that ``address`` did not initiate (address poisoning; see the module
    docstring for the limits)."""
    sent = {_addr(r, "hash") for r in txs if _addr(r, "from") == address}
    rows = [r for r in txs if _value(r) > 0]
    for r in tokens:
        if _value(r) <= 0:
            continue
        if _addr(r, "from") == address and _addr(r, "hash") not in sent:
            continue
        rows.append(r)
    return rows


def _within(out: dict, inc: dict) -> bool:
    """``inc`` is not later than ``out`` and at most FORWARD_SECONDS before it (by
    timeStamp; by block, _FALLBACK_BLOCKS, when a timestamp is missing)."""
    ot, it = _num(out.get("timeStamp")), _num(inc.get("timeStamp"))
    if ot is not None and it is not None:
        return 0 <= ot - it <= FORWARD_SECONDS
    ob, ib = _num(out.get("blockNumber")), _num(inc.get("blockNumber"))
    return ob is not None and ib is not None and 0 <= ob - ib <= _FALLBACK_BLOCKS


def _covers(out: dict, received: int, sent: int) -> bool:
    """``received`` covers ``sent`` and exceeds it by at most the sweep tolerance."""
    if received < sent:
        return False
    if _asset(out) == "eth":
        return received - sent <= FORWARD_ETH_WEI
    return received - sent <= received * FORWARD_TOKEN_SHARE


def forwards_to(rows: list[dict], address: str, target: str) -> bool:
    """True when ``address`` sent ``target`` an amount it had just received: an
    incoming transfer of the same asset at most FORWARD_SECONDS earlier, and the
    sweep short of it by at most 0.01 ETH (ETH) or 1 % (tokens); or an aggregated
    sweep, where the incoming transfers of that asset received in the window and
    after the previous outgoing transfer of that asset (to anyone) add up to it with
    the same tolerance. This is how an exchange deposit address behaves (the
    forwarding test of Victor, FC 2020, used by Tutela); a personal wallet that
    merely pays a busy address does not."""
    indexed = list(enumerate(rows))
    incoming = [
        (i, r) for i, r in indexed if _addr(r, "to") == address and _addr(r, "from") != address
    ]
    outgoing = [(i, r) for i, r in indexed if _addr(r, "from") == address and _addr(r, "to")]
    for oi, out in outgoing:
        if _addr(out, "to") != target:
            continue
        ov = _value(out)
        if ov <= 0:
            continue
        same = [(i, r) for i, r in incoming if _asset(r) == _asset(out) and _value(r) > 0]
        # one incoming transfer
        if any(_within(out, r) and _covers(out, _value(r), ov) for _i, r in same):
            return True
        # several incoming transfers since the previous outgoing one
        previous = max(
            (i for i, r in outgoing if i < oi and _asset(r) == _asset(out)),
            default=-1,
        )
        pool = [
            r for i, r in same if previous < i < oi and _block(r) <= _block(out) and _within(out, r)
        ]
        if len(pool) > 1 and _covers(out, sum(_value(r) for r in pool), ov):
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
    rows = transfers(address, txs, tokens)
    senders = {_addr(r, "from") for r in rows if _addr(r, "to") == address and r.get("from")} - {
        address
    }
    if not senders or len(senders) > MAX_DEPOSIT_SENDERS:
        return None
    outgoing = Counter(_addr(r, "to") for r in rows if _addr(r, "from") == address and r.get("to"))
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
    labelled_share = sum(n for t, n in hot if _is_exchange(labels, t)) / total
    return {
        "address": address,
        "sweep_target": target,
        "sweep_targets": [t for t, _n in hot],
        "exchange": ", ".join(entities),
        "senders": sorted(senders),
        # Only sweeps to labelled exchange wallets stood above chance in the placebo
        # test; an address that qualifies by activity alone is shown, not scored.
        "evidence": labelled_share >= MIN_SWEEP_SHARE,
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


def shared_deposit_hits(
    deposit_addresses: list[dict], recipients, wallet: str, evidence_only: bool = True
) -> dict:
    """{recipient: [deposit addresses]} for recipients that also sent to one of them.

    With ``evidence_only`` (the default) only deposit addresses that sweep to
    labelled exchange wallets count; one found by activity alone is context.
    """
    wallet = wallet.lower()
    out: dict[str, list[str]] = {}
    recips = set(recipients)
    for info in deposit_addresses:
        if evidence_only and not info.get("evidence", True):
            continue
        for s in info["senders"]:
            if s != wallet and s in recips:
                out.setdefault(s, []).append(info["address"])
    return out
