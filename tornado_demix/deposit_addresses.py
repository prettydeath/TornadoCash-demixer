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

Kinds. A result carries ``kind``: ``exchange_deposit`` (every labelled sweep target is
an exchange), ``service_deposit`` (a labelled sweep target has another category, e.g.
gambling or bridge; never scored, ``service`` names it) or ``forwarder`` (a per-account
forwarder whose token outflows are executed by an operator, or ``smart_account`` when
that operator is an ERC-4337 EntryPoint; see ``_forwarder``). Only
``exchange_deposit`` links two addresses. Exchange and service results also carry
``gas_funder``: the sender of a small native transfer that preceded the first token
sweep (context, not scored).

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
GAS_FUND_WEI = 10**16  # a native transfer below 0.01 coin before the first sweep funds gas
# Public ERC-4337 EntryPoints, verified against the release notes of
# https://github.com/eth-infinitism/account-abstraction/releases (v0.6, v0.7, v0.8).
ENTRY_POINTS = frozenset(
    {
        "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789",  # v0.6
        "0x0000000071727de22e5e9d8baf0edac6f37da032",  # v0.7
        "0x4337084d9e255ff0702461cf8895ce9e3b5ff108",  # v0.8
    }
)
MAX_EXECUTOR_LOOKUPS = 3  # tx_info calls per address to find the executor of a forwarder


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


def _label(labels: dict | None, address: str) -> dict:
    """The label the classifier may use: a Blockscout-sourced tag counts only when it
    says ``exchange``; its ``deposit`` / ``other`` tags are ignored here (they would hide
    a customer address or turn any sweep target into a service). Attribution labels count
    as they are."""
    lab = (labels or {}).get(address) or {}
    if lab.get("source") == "blockscout" and lab.get("category") != "exchange":
        return {}
    return lab


def _category(labels: dict | None, address: str) -> str:
    return _label(labels, address).get("category") or ""


def _is_exchange(labels: dict | None, address: str) -> bool:
    return _label(labels, address).get("category") == "exchange"


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


def kind_label(info: dict) -> str:
    """Short text for the kind of a result, for the reports."""
    kind = info.get("kind", "exchange_deposit")
    if kind == "service_deposit":
        return f"service deposit ({info.get('service') or 'service'})"
    if kind in ("forwarder", "smart_account"):
        name = "forwarder" if kind == "forwarder" else "smart account"
        return f"{name} (operator {info.get('operator') or 'unknown'})"
    return "exchange deposit"


def target_label(info: dict) -> str:
    """Where a result sweeps to, for the reports (context kinds are never scored)."""
    kind = info.get("kind", "exchange_deposit")
    if kind in ("forwarder", "smart_account"):
        return "executed by an operator (context, not scored)"
    if kind == "service_deposit":
        return (info.get("service") or "a service wallet") + " (context, not scored)"
    if info.get("evidence", True):
        return info.get("exchange") or "a hot wallet"
    return "an unlabelled busy address (context, not scored)"


def _order(row: dict) -> tuple[int, int]:
    t = _num(row.get("timeStamp"))
    return (t if t is not None else 0, _block(row))


def _gas_funder(rows: list[dict], address: str) -> str | None:
    """Sender of the first native incoming transfer if it is small and precedes the first
    token sweep out of ``address``; no extra calls (context only)."""
    sweeps = [r for r in rows if _addr(r, "from") == address and r.get("contractAddress")]
    if not sweeps:
        return None
    first_sweep = min(_order(r) for r in sweeps)
    native = [
        r
        for r in rows
        if _addr(r, "to") == address
        and _addr(r, "from") not in ("", address)
        and not r.get("contractAddress")
    ]
    if not native:
        return None
    first = min(native, key=_order)
    if _order(first) <= first_sweep and _value(first) < GAS_FUND_WEI:
        return _addr(first, "from")
    return None


def _forwarder(client, address: str, txs: list[dict], tokens: list[dict], senders) -> dict | None:
    """A forwarder or smart account: real-token outflows executed by an operator, not by
    the address. This looks at the outgoing token rows BEFORE the poisoning filter of
    ``transfers`` drops those not sent by ``address``. Needs at least MIN_SWEEP_SHARE of
    its non-zero outgoing token transfers executed in transactions it did not send, in at
    least two distinct transactions, and one operator: the same ``to`` contract across up
    to MAX_EXECUTOR_LOOKUPS of those transactions. Kind ``smart_account`` when the
    operator is an ERC-4337 EntryPoint (the user's own account run through a bundler),
    else ``forwarder``."""
    info_of = getattr(client, "tx_info", None)
    if info_of is None:
        return None
    sent = {_addr(r, "hash") for r in txs if _addr(r, "from") == address}
    out = [
        r for r in tokens if _addr(r, "from") == address and _value(r) > 0 and _asset(r) != "eth"
    ]
    ops = [r for r in out if _addr(r, "hash") not in sent]
    if not out or len(ops) / len(out) < MIN_SWEEP_SHARE:
        return None
    hashes = list(dict.fromkeys(_addr(r, "hash") for r in ops if _addr(r, "hash")))
    if len(hashes) < 2:
        return None
    seen = []
    for h in hashes[:MAX_EXECUTOR_LOOKUPS]:
        try:
            info = info_of(h)
        except ApiKeyError:
            raise
        except ApiError:
            info = None
        if isinstance(info, dict):
            seen.append(info)
    if len(seen) < 2:
        return None
    tos = {(i.get("to") or "").lower() for i in seen}
    if len(tos) != 1 or "" in tos or address in tos:
        return None
    operator = tos.pop()
    if operator in {_asset(r) for r in ops}:
        return None  # a spoof of a fake token is "executed" by the token itself
    return {
        "address": address,
        "kind": "smart_account" if operator in ENTRY_POINTS else "forwarder",
        "operator": operator,
        "evidence": False,
        "senders": sorted(senders),
    }


def classify_deposit_address(
    client,
    address: str,
    is_contract: Callable[[str], bool | None] | None = None,
    labels: dict | None = None,
    busy: Callable[[str], bool] | None = None,
) -> dict | None:
    """Return {address, kind, sweep_target, exchange, senders, ...} if ``address`` looks
    like an exchange or service deposit address or a forwarder (see the module
    docstring), else None."""
    address = address.lower()
    # A contract can only be a smart account or a forwarder (an ERC-4337 account is
    # normally a contract); never an exchange or service deposit address.
    contract = bool(is_contract is not None and is_contract(address))
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
    swept = None if contract else _sweep(address, rows, is_contract, labels, busy)
    if swept is None:
        return _forwarder(client, address, txs, tokens, senders)
    swept["senders"] = sorted(senders)
    swept["gas_funder"] = _gas_funder(rows, address)
    return swept


def _sweep(address, rows, is_contract, labels, busy) -> dict | None:
    """The exchange / service sweep test on the filtered rows; None when it fails."""
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
            and (is_contract is None or is_contract(target) is False)
            and forwards_to(rows, address, target)
            and busy(target)
        ):
            # Without a label, a hot wallet is an ordinary (non-contract) busy address
            # that receives forwarded deposits: a token, a DEX router or a Tornado
            # contract is busy too but is not an exchange, and a wallet that merely
            # pays a busy address does not forward what it received. An unknown
            # contract status (lookup failed) is not taken for an EOA.
            hot.append((target, n))
    if not hot or sum(n for _t, n in hot) / total < MIN_SWEEP_SHARE:
        return None
    target = hot[0][0]
    # A labelled target of another category (gambling, bridge, ...) makes the address a
    # service deposit address: shown, never scored, never linking two addresses.
    services = sorted(
        {
            _label(labels, t).get("entity") or _category(labels, t)
            for t, _n in hot
            if _category(labels, t) and not _is_exchange(labels, t)
        }
    )
    if services:
        return {
            "address": address,
            "kind": "service_deposit",
            "sweep_target": target,
            "sweep_targets": [t for t, _n in hot],
            "exchange": "",
            "service": ", ".join(services),
            "evidence": False,
        }
    entities = sorted(
        {_label(labels, t).get("entity", "") for t, _n in hot if _is_exchange(labels, t)} - {""}
    )
    labelled_share = sum(n for t, n in hot if _is_exchange(labels, t)) / total
    return {
        "address": address,
        "kind": "exchange_deposit",
        "sweep_target": target,
        "sweep_targets": [t for t, _n in hot],
        "exchange": ", ".join(entities),
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
        if _label(labels, addr):
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
        if info.get("kind", "exchange_deposit") != "exchange_deposit":
            continue  # service deposit addresses and forwarders never link two addresses
        if evidence_only and not info.get("evidence", True):
            continue
        for s in info["senders"]:
            if s != wallet and s in recips:
                out.setdefault(s, []).append(info["address"])
    return out
