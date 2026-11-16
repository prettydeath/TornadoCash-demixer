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
* at most ``MAX_DEPOSIT_SENDERS`` (300) distinct senders, counting only those whose
  asset ``X`` later sweeps (see "Senders" below);
* at least ``MIN_SWEEP_SHARE`` of its outgoing transfers go to hot wallets (with more
  than ``FEW_SENDERS`` senders, at least ``HUB_SWEEP_SHARE``): among
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

Which rows are counted. A failed transaction (``isError`` 1 or ``txreceipt_status`` 0,
see :mod:`tornado_demix.txstatus`) moved nothing: its rows never count as a transfer, in
either direction. The failed transactions an examined address sent are counted apart
(``failed_sends`` on its result), never as movement. The same transfer is listed by
``txlist`` (the call to the token contract, value 0) and by ``tokentx`` (the token
movement); only the second is counted, so one token transfer is one transfer:
``txlist`` rows with value 0 are dropped, ETH-value rows are kept. Address poisoning is filtered in a limited,
offline way: zero-value transfers (of ETH or of any token) are dropped, and a
``tokentx`` transfer out of ``X`` is kept only when its transaction is one of ``X``'s
own ``txlist`` rows: sent by ``X``, or sent TO ``X`` (an EIP-7702 account whose sponsor
pays the gas calls the account itself; a call to a plain EOA runs no code, so nothing
can move out of it there). Anything else is a ``transferFrom`` spoof and is dropped.
When ``txlist`` does not list such sponsored calls, the forwarder check finds them by
their transaction target (``to`` = ``X``) and the sweep test is run again with them
(``sponsored: True``). The depositor's own counterparties are filtered the same way,
so spoofed "from the depositor" transfers to look-alike addresses never take one of
the ``MAX_CANDIDATES`` slots.

Senders. An incoming transfer makes its "from" a sender only when it reaches the dust
floor of its asset (``Network.sender_floors``: the native coin at the network's floor, a
listed real token at its floor; an unlisted token needs a non-zero amount) and ``X``
later sweeps the same asset. A fake token is never swept, so the "from" it forges (any
address, even an exchange hot wallet) is not a sender, and a dust spoof or a small
top-up is below the floor, so it is not one either, even when ``X`` sweeps that asset.
Up to ``FEW_SENDERS`` (50) senders the usual sweep share applies; a per-customer address
can have a few hundred one-off senders (observed exchange deposit addresses with
56-142), so up to ``MAX_DEPOSIT_SENDERS`` (300) are admitted when at least
``HUB_SWEEP_SHARE`` (95 %) of the outgoing transfers are sweeps: a deposit address
forwards what it receives, a busy hub pays out to many destinations. An address with
more senders is not classified (taken as a hub). Judgement calls, not measured on a
labelled set. Limit: a spoof routed through a contract that ``X`` itself called is not
detected.

Kinds. A result carries ``kind``: ``exchange_deposit`` (a sweep target is labelled
as an exchange, receives a swept balance (``sweeps_balance_to``: at least 90 % of what
arrived since the previous outgoing transfer of the asset, within 30 days) and none has
another category, or receives forwarded amounts (``forwards_to``); a label names the
exchange, it is not evidence of a sweep; unlabelled targets need ``forwards_to``), ``paid_exchange`` (it reaches the sweep share only by
payments to labelled exchange wallets that do not sweep what it received:
a customer paying an exchange, not a deposit address; no senders, no sweep target, never
scored, never linking; context), ``service_deposit`` (a labelled sweep
target has another category, e.g. gambling or bridge; never scored, ``service`` names
it), ``deposit_like`` (swept only to unlabelled busy addresses: whose deposit address it
is, is not known, ``service`` is "unknown"; context) or ``forwarder`` (a per-account
forwarder whose token outflows are executed by an operator, or ``smart_account`` when
that operator is an ERC-4337 EntryPoint; see ``_forwarder``). Only ``exchange_deposit``
and ``deposit_like`` can share a deposit address between two addresses, and only an
``exchange_deposit`` swept to labelled exchange wallets (``evidence``) counts beyond
context. Exchange names are case-folded and deduplicated. Exchange, service and
deposit-like results also carry ``gas_funder``: the sender of a small native transfer
that preceded the first token sweep (context, not scored; the threshold is per network,
``networks.GAS_FUND_WEI``), and ``incomplete: True`` when the token history hit the
``MAX_DEPOSIT_TXS`` cap. :func:`classify_address` classifies one given address (CLI
``classify-address``, the web UI and ``characterize``).

Only the depositor's own outgoing counterparties are examined, busiest first and at
most ``MAX_CANDIDATES`` of them, so the cost is bounded: two history queries per
examined address and one busy check per sweep target.
"""

from __future__ import annotations

import csv
import functools
import os
import re
from collections import Counter
from typing import Callable

from .config import PACKAGE_DATA
from .errors import ApiError, ApiKeyError
from .incomplete import lookup_failed
from .networks import sender_floors
from .txstatus import failed

MAX_CANDIDATES = 25  # depositor counterparties examined, busiest first
MAX_DEPOSIT_TXS = 1000  # a deposit address is quiet; more rows: not one
# Senders. A per-customer exchange deposit address can have a few hundred one-off
# senders (exchange withdrawals, payroll, airdrop claimers paying the customer); what
# tells it from a busy hub is that it forwards what it receives. Up to FEW_SENDERS the
# usual sweep share applies; above it, up to MAX_DEPOSIT_SENDERS, nearly every outgoing
# transfer must be a sweep (HUB_SWEEP_SHARE), and more senders is a hub. Judgement
# calls, not measured on a labelled set.
FEW_SENDERS = 50
MAX_DEPOSIT_SENDERS = 300
HUB_SWEEP_SHARE = 0.95
MIN_SWEEP_SHARE = 0.8  # share of outgoing transfers that go to hot wallets
MAX_SWEEP_TARGETS = 3  # most frequent destinations checked for a hot wallet
HOT_WALLET_TXS = 10000  # a sweep target this busy is taken for a hot wallet
# A sweep follows the incoming transfer within this time: 3200 Ethereum blocks of
# 12 s, kept as a duration so that chains with other block times are not distorted.
FORWARD_SECONDS = 38400
_FALLBACK_BLOCKS = 3200  # the same window in blocks, only for rows without timeStamp
FORWARD_ETH_WEI = 10**16  # ...and forwards it less at most 0.01 ETH (Victor, FC 2020)
FORWARD_TOKEN_SHARE = 0.01  # tokens: forwards at least 99 % of the incoming amount
# A labelled exchange wallet: exchanges batch their sweeps (daily or less often) and may
# leave a fee or dust behind, so a transfer to a target labelled as an exchange counts as a
# sweep when it moves at least LABELLED_SWEEP_SHARE of what the address received in that
# asset since its previous outgoing transfer of that asset, within LABELLED_SWEEP_SECONDS
# of the latest of those receipts (see ``sweeps_balance_to``); a transfer that passes the
# forwarding test (``forwards_to``) counts as well. Exploratory judgement calls, not
# measured on a labelled set. Unlabelled targets keep the forwarding test only.
LABELLED_SWEEP_SHARE = 0.9
LABELLED_SWEEP_SECONDS = 30 * 24 * 3600
_LABELLED_FALLBACK_BLOCKS = LABELLED_SWEEP_SECONDS // 12  # rows without timeStamp
# A native transfer below this before the first sweep funds gas. Default 0.01 coin; the
# per-network values live in ``networks.GAS_FUND_WEI`` (a top-up on Polygon is 0.01-2 POL).
GAS_FUND_WEI = 10**16
# Public ERC-4337 EntryPoints, verified against the release notes of
# https://github.com/eth-infinitism/account-abstraction/releases (v0.6, v0.7, v0.8).
ENTRY_POINTS = frozenset(
    {
        "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789",  # v0.6
        "0x0000000071727de22e5e9d8baf0edac6f37da032",  # v0.7
        "0x4337084d9e255ff0702461cf8895ce9e3b5ff108",  # v0.8
    }
)
# Public DEX routers and aggregators (verified contracts, one source URL per row). A call
# through one is a swap or an approval of the address's user, never an operator.
DEX_ROUTERS_CSV = os.path.join(PACKAGE_DATA, "dex_routers.csv")
MAX_EXECUTOR_LOOKUPS = 3  # tx_info calls per address to find the executor of a forwarder


@functools.lru_cache(maxsize=1)
def dex_routers() -> frozenset[str]:
    """Lower-case addresses of ``data/dex_routers.csv``. Matched by address on every
    chain: these are fixed public deployments (vanity or CREATE2 addresses)."""
    with open(DEX_ROUTERS_CSV, newline="", encoding="utf-8") as fh:
        return frozenset(r["address"].strip().lower() for r in csv.DictReader(fh))


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
    rows = rows if isinstance(rows, list) else []
    if action != "txlist" and len(rows) >= MAX_DEPOSIT_TXS:
        # A capped token list changes the outcome, so say it is cut. A txlist at the
        # cap is the intended "busy address" probe (not a deposit address): no note.
        note = getattr(client, "truncated", None)
        if isinstance(note, list):
            note.append(
                {
                    "action": action,
                    "address": address,
                    "block": None,
                    "limit": MAX_DEPOSIT_TXS,
                    "reason": "truncated",
                }
            )
    return rows


def _label(labels: dict | None, address: str) -> dict:
    """The label the classifier may use: a Blockscout-sourced tag counts only when it
    says ``exchange``; its ``deposit`` / ``other`` tags are ignored here (they would hide
    a customer address or turn any sweep target into a service). Attribution labels count
    as they are."""
    lab = (labels or {}).get(address) or {}
    origin = lab.get("category_source") or lab.get("source")
    if origin == "blockscout" and lab.get("category") != "exchange":
        return {}
    return lab


_DEPOSIT_WORD = re.compile(r"\bdeposit\b(?!\s*funder)", re.IGNORECASE)


def _says_deposit(lab: dict) -> bool:
    """True when a label names a deposit address ("ShapeShift: Binance Deposit"): such a
    counterparty is a customer address to classify, not a service's own wallet. The
    words count, not the category: an attribution row of category ``deposit`` without
    them stays a service's own wallet."""
    text = " ".join(str(lab.get(k) or "") for k in ("label", "tag", "entity"))
    return bool(_DEPOSIT_WORD.search(text))


def _names(values) -> str:
    """Entity names case-folded and deduplicated ("Binance, binance" -> "Binance"); the
    first spelling with a capital letter is kept."""
    seen: dict[str, str] = {}
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        key = v.casefold()
        if key not in seen or (seen[key] == seen[key].lower() and v != v.lower()):
            seen[key] = v
    return ", ".join(seen[k] for k in sorted(seen))


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


def own_hashes(address: str, txs: list[dict]) -> set[str]:
    """Transactions of ``address``: sent by it, or sent TO it (an EIP-7702 account whose
    sponsor pays the gas calls the account itself; a call to a plain EOA runs no code, so
    no token can move out of it in such a transaction)."""
    return {
        _addr(r, "hash")
        for r in txs
        if (_addr(r, "from") == address or _addr(r, "to") == address)
        and _addr(r, "hash")
        and not failed(r)
    }


def transfers(
    address: str, txs: list[dict], tokens: list[dict], extra_own: set[str] = frozenset()
) -> list[dict]:
    """The real, non-zero transfers of ``address``: successful ``txlist`` rows with value
    (the value-0 calls to token contracts are the same movement as the ``tokentx`` row
    and are not counted twice; a failed transaction moved nothing) plus ``tokentx`` rows
    with value, minus outgoing token transfers whose transaction is not one of
    ``address``'s own (:func:`own_hashes`, plus ``extra_own``): address poisoning, see
    the module docstring for the limits."""
    own = own_hashes(address, txs) | set(extra_own)
    rows = [r for r in txs if _value(r) > 0 and not failed(r)]
    for r in tokens:
        if _value(r) <= 0 or failed(r):
            continue
        if _addr(r, "from") == address and _addr(r, "hash") not in own:
            continue
        rows.append(r)
    return rows


def own_token_transfers(address: str, txs: list[dict], tokens: list[dict]) -> list[dict]:
    """The token rows of ``address`` that it really took part in, for its direct
    counterparties (``linked``). Anyone can make a token contract emit a Transfer that
    names any "from" and "to" (a fake token, or a zero-value ``transferFrom`` of a real
    one), in their own transaction and at any time, so a raw ``tokentx`` row proves
    nothing. Kept:

    * an outgoing row with value whose transaction is one of ``address``'s own
      (:func:`own_hashes`: sent by it, or a sponsored EIP-7702 call to it);
    * an incoming row with value of a token that ``address`` later moves in one of its
      own transactions (at or after the incoming row). A victim never moves a fake
      token; a real token it received and then spent was really held.

    The incoming rule loses a genuine token payment the address never spent (or spent
    only through a contract that pulled it): that link is not seen. Native ``txlist``
    and internal rows need no filter, they need the sender's key or contract code."""
    own = own_hashes(address, txs)
    out = [
        r
        for r in tokens
        if _addr(r, "from") == address and _value(r) > 0 and _addr(r, "hash") in own
    ]
    last_move: dict[str, tuple[int, int]] = {}
    for r in out:
        a = _asset(r)
        last_move[a] = max(last_move.get(a, (-1, -1)), _order(r))
    kept, out_ids = [], {id(r) for r in out}
    for r in tokens:
        if id(r) in out_ids:
            kept.append(r)
        elif (
            _addr(r, "to") == address
            and _addr(r, "from") != address
            and _value(r) > 0
            and _asset(r) in last_move
            and _order(r) <= last_move[_asset(r)]
        ):
            kept.append(r)
    return kept


def swept_senders(address: str, rows: list[dict], floors: dict[str, int] | None = None) -> set[str]:
    """Senders whose asset ``address`` later sweeps: an incoming transfer counts only when
    it is at least the asset's dust floor and an outgoing transfer of the same asset
    follows it (or is in the same block). ``floors`` maps ``"eth"`` (the native coin) and
    listed real tokens to their smallest raw amount (``Network.sender_floors``; default
    the Ethereum floors); a token without a floor needs a non-zero amount. A fake token is
    never swept and dust (a 1-wei spoof, a small top-up) is below the floor, so neither
    makes its "from" (which a fake token can forge freely) a sender, even when ``address``
    later sweeps that asset."""
    floors = sender_floors(1) if floors is None else floors
    last_out: dict[str, tuple[int, int]] = {}
    for r in rows:
        if _addr(r, "from") == address and _addr(r, "to") and _value(r) > 0:
            a = _asset(r)
            last_out[a] = max(last_out.get(a, (-1, -1)), _order(r))
    out = set()
    for r in rows:
        frm = _addr(r, "from")
        if _addr(r, "to") != address or not frm or frm == address or _value(r) <= 0:
            continue
        if _value(r) < floors.get(_asset(r), 1):
            continue
        last = last_out.get(_asset(r))
        if last is not None and _order(r) <= last:
            out.add(frm)
    return out


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


def _gap_ok(later: dict, earlier: dict, seconds: int, blocks: int) -> bool:
    """``earlier`` is not after ``later`` and at most ``seconds`` before it (by
    timeStamp; by ``blocks`` when a timestamp is missing)."""
    lt, et = _num(later.get("timeStamp")), _num(earlier.get("timeStamp"))
    if lt is not None and et is not None:
        return 0 <= lt - et <= seconds
    lb, eb = _num(later.get("blockNumber")), _num(earlier.get("blockNumber"))
    return lb is not None and eb is not None and 0 <= lb - eb <= blocks


def sweeps_balance_to(rows: list[dict], address: str, target: str) -> bool:
    """True when ``address`` swept a balance to ``target`` (the rule for a target labelled
    as an exchange): an outgoing transfer to ``target`` that moves at least
    LABELLED_SWEEP_SHARE of the value of that asset received since the previous outgoing
    transfer of that asset (to anyone), at most LABELLED_SWEEP_SECONDS after the latest
    of those receipts. Several deposits swept together and a fee or dust leftover pass;
    a small payment, or one long after the funds arrived, does not."""
    events = sorted(enumerate(rows), key=lambda e: (_order(e[1]), e[0]))
    received: dict[str, list[dict]] = {}
    for _i, r in events:
        frm, to, v = _addr(r, "from"), _addr(r, "to"), _value(r)
        if v <= 0:
            continue
        a = _asset(r)
        if to == address and frm != address:
            received.setdefault(a, []).append(r)
        elif frm == address and to:
            since = received.pop(a, [])
            if to != target or not since:
                continue
            total = sum(_value(x) for x in since)
            latest = since[-1]
            if v >= total * LABELLED_SWEEP_SHARE and _gap_ok(
                r, latest, LABELLED_SWEEP_SECONDS, _LABELLED_FALLBACK_BLOCKS
            ):
                return True
    return False


def kind_label(info: dict) -> str:
    """Short text for the kind of a result, for the reports."""
    kind = info.get("kind", "exchange_deposit")
    cut = ", token history cut at 1,000 rows" if info.get("incomplete") else ""
    if kind == "service_deposit":
        return f"service deposit ({info.get('service') or 'service'}){cut}"
    if kind == DEPOSIT_LIKE:
        return f"deposit-like (service {info.get('service') or 'unknown'}){cut}"
    if kind in ("forwarder", "smart_account"):
        name = "forwarder" if kind == "forwarder" else "smart account"
        return f"{name} (operator {info.get('operator') or 'unknown'})"
    if kind == UNKNOWN_KIND:
        return "unknown (a lookup failed)"
    if kind == NOT_DEPOSIT:
        return "not a deposit address"
    if kind == PAID_EXCHANGE:
        return f"paid an exchange ({info.get('exchange') or 'exchange'}), not a deposit address"
    return f"exchange deposit{cut}"


def target_label(info: dict) -> str:
    """Where a result sweeps to, for the reports (context kinds are never scored)."""
    kind = info.get("kind", "exchange_deposit")
    if kind in ("forwarder", "smart_account"):
        return "executed by an operator (context, not scored)"
    if kind == "service_deposit":
        return (info.get("service") or "a service wallet") + " (context, not scored)"
    if kind == DEPOSIT_LIKE:
        return "an unlabelled busy address (context, not scored)"
    if kind in (UNKNOWN_KIND, NOT_DEPOSIT):
        return info.get("error") or "-"
    if kind == PAID_EXCHANGE:
        return (
            "nothing; paid a labelled exchange, did not forward or sweep what it received (context)"
        )
    if info.get("evidence", True):
        return info.get("exchange") or "a hot wallet"
    return "an unlabelled busy address (context, not scored)"


def check_text(info: dict) -> str:
    """One line for a deposit-address check result (CLI, HTML report, web UI)."""
    kind = info.get("kind", "exchange_deposit")
    text = kind_label(info)
    n = info.get("failed_sends") or 0
    failed_note = f"{n} failed transaction(s) sent, moved nothing, not counted" if n else ""
    if kind == NOT_DEPOSIT:
        return f"{text}; {failed_note}" if failed_note else text
    if kind == UNKNOWN_KIND:
        text = f"{text}: {info.get('error') or ''}".rstrip(": ")
        return f"{text}; {failed_note}" if failed_note else text
    if kind == PAID_EXCHANGE:
        text += (
            "; it paid the labelled exchange but did not forward or sweep the value it received"
            " (context, not scored)"
        )
        return f"{text}; {failed_note}" if failed_note else text
    where = target_label(info)
    parts = [text, where if kind in ("forwarder", "smart_account") else f"swept to {where}"]
    parts.append(f"{len(info.get('senders') or [])} sender(s)")
    if info.get("gas_funder"):
        parts.append(f"gas funder {info['gas_funder']}")
    if info.get("sponsored"):
        parts.append("sweeps sent by a sponsor (EIP-7702)")
    if failed_note:
        parts.append(failed_note)
    return "; ".join(parts)


def _order(row: dict) -> tuple[int, int]:
    t = _num(row.get("timeStamp"))
    return (t if t is not None else 0, _block(row))


def _gas_funder(rows: list[dict], address: str, threshold: int = GAS_FUND_WEI) -> str | None:
    """Sender of the first native incoming transfer if it is below ``threshold`` (wei of
    the network's coin, see ``networks.GAS_FUND_WEI``) and precedes the first token sweep
    out of ``address``; no extra calls (context only)."""
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
    if _order(first) <= first_sweep and _value(first) < threshold:
        return _addr(first, "from")
    return None


_SELF_CALLED = "_self_called"  # internal: the "operator" is the address itself (EIP-7702)


def _incoming(address: str, rows: list[dict]) -> list[dict]:
    return [
        r
        for r in rows
        if _addr(r, "to") == address and _addr(r, "from") not in ("", address) and _value(r) > 0
    ]


def _forwarder(client, address: str, txs: list[dict], tokens: list[dict]) -> dict | None:
    """A forwarder or smart account: real-token outflows executed by an operator, not by
    the address. This looks at the outgoing token rows BEFORE the poisoning filter of
    ``transfers`` drops those not in the address's own transactions. Needs at least
    MIN_SWEEP_SHARE of its outgoing token transactions (non-zero transfers) executed in
    transactions it did not send, in at least two distinct transactions, at least one of the moved tokens
    received by the address (a poisoner's batch contract "moves" only fake tokens the
    victim never held), and one operator: the same ``to`` contract across up to
    MAX_EXECUTOR_LOOKUPS of those transactions. Kind ``smart_account`` when the operator
    is an ERC-4337 EntryPoint (the user's own account run through a bundler), else
    ``forwarder``; a public DEX router or aggregator (``data/dex_routers.csv``) is never
    the operator. When that ``to`` is the address itself, the transactions are its own
    sponsored EIP-7702 calls: an internal marker with their hashes is returned so the
    caller re-runs the sweep test with them. Senders are those who sent a token the
    operator moves."""
    info_of = getattr(client, "tx_info", None)
    if info_of is None:
        return None
    own = own_hashes(address, txs)
    out = [
        r for r in tokens if _addr(r, "from") == address and _value(r) > 0 and _asset(r) != "eth"
    ]
    ops = [r for r in out if _addr(r, "hash") not in own]
    # The share is of transactions, not transfers: one swap moves many tokens. The
    # address must not initiate (or be the target of) most of its outflow transactions.
    out_txs = {_addr(r, "hash") for r in out} - {""}
    op_txs = {_addr(r, "hash") for r in ops} - {""}
    if not out_txs or len(op_txs) / len(out_txs) < MIN_SWEEP_SHARE:
        return None
    moved = {_asset(r) for r in ops}
    received = _incoming(address, txs + tokens)
    if not moved & {_asset(r) for r in received}:
        return None
    hashes = list(dict.fromkeys(_addr(r, "hash") for r in ops if _addr(r, "hash")))
    if len(hashes) < 2:
        return None
    seen, failed, checked = [], [], set()
    for h in hashes[:MAX_EXECUTOR_LOOKUPS]:
        try:
            info = info_of(h)
        except ApiKeyError:
            raise
        except ApiError as exc:
            info = None
            failed.append((h, exc))
        if isinstance(info, dict):
            seen.append(info)
            checked.add(h)
    if len(seen) < 2:
        if failed:
            h, exc = failed[0]
            return _unknown(address, f"transaction {h} not read: {exc}")
        return None
    tos = {(i.get("to") or "").lower() for i in seen}
    if len(tos) != 1 or "" in tos:
        return None
    operator = tos.pop()
    if operator == address:
        # only the transactions actually read are taken as the address's own
        return {"kind": _SELF_CALLED, "hashes": checked}
    if operator in dex_routers():
        return None  # a swap through a router, not an operator moving tokens out
    if operator in moved:
        return None  # a spoof of a fake token is "executed" by the token itself
    senders = {_addr(r, "from") for r in received if _asset(r) in moved}
    if not senders or len(senders) > MAX_DEPOSIT_SENDERS:
        return None
    return {
        "address": address,
        "kind": "smart_account" if operator in ENTRY_POINTS else "forwarder",
        "operator": operator,
        "evidence": False,
        "senders": sorted(senders),
    }


UNKNOWN_KIND = "unknown"  # a lookup the outcome depends on failed
DEPOSIT_LIKE = "deposit_like"  # swept to an unlabelled busy address: service unknown
NOT_DEPOSIT = "not_deposit"  # classify_address only: the tests found no deposit address
PAID_EXCHANGE = "paid_exchange"  # paid a labelled exchange, swept nothing: context


def _unknown(address: str, why) -> dict:
    """The explicit "could not tell" result of the classifier."""
    return {"address": address, "kind": UNKNOWN_KIND, "error": str(why)[:200]}


def _check_failed(is_contract, address: str) -> bool:
    """True when ``is_contract`` answered None because its lookup failed (see
    ``rpc.make_contract_check``), not because no endpoint is configured."""
    return address in (getattr(is_contract, "failed", None) or ())


def classify_deposit_address(
    client,
    address: str,
    is_contract: Callable[[str], bool | None] | None = None,
    labels: dict | None = None,
    busy: Callable[[str], bool | None] | None = None,
    gas_fund_wei: int = GAS_FUND_WEI,
    floors: dict[str, int] | None = None,
) -> dict | None:
    """Return {address, kind, sweep_target, exchange, senders, ...} if ``address`` looks
    like an exchange or service deposit address or a forwarder (see the module
    docstring), None if it does not, and ``{address, kind: "unknown", error}`` when a
    lookup the answer depends on failed: the history, a busy check (``busy`` returns
    None), a contract check (``is_contract`` returns None and lists the address in its
    ``failed`` set) or a transaction read. An unknown is never "not a deposit
    address". An ``ApiKeyError`` is raised. ``gas_fund_wei`` is the network's
    gas-funder threshold (``Network.gas_fund_wei``), ``floors`` its sender dust floors
    (``Network.sender_floors``, see :func:`swept_senders`). A result whose token history hit
    the MAX_DEPOSIT_TXS cap carries ``incomplete: True``; one that needed sponsored
    EIP-7702 calls found by their transaction target carries ``sponsored: True``; one
    whose address sent failed transactions carries their number as ``failed_sends`` (they
    moved nothing and are not counted as transfers), an unknown result as well."""
    info, failed_sends = _classify(
        client, address.lower(), is_contract, labels, busy, gas_fund_wei, floors
    )
    if info is not None and failed_sends:
        info["failed_sends"] = failed_sends
    return info


def _classify(client, address, is_contract, labels, busy, gas_fund_wei, floors):
    """(result of :func:`classify_deposit_address` without ``failed_sends``, the number
    of failed transactions ``address`` sent)."""
    try:
        txs = _rows(client, "txlist", address)
        if len(txs) >= MAX_DEPOSIT_TXS:
            return None, 0
        tokens = _rows(client, "tokentx", address)
    except ApiKeyError:
        raise
    except ApiError as exc:
        return _unknown(address, f"history not read: {exc}"), 0
    cut = len(tokens) >= MAX_DEPOSIT_TXS
    # A failed transaction moved nothing: dropped from every test below (sweep, senders,
    # forwarding, gas funder, forwarder), only counted when ``address`` sent it.
    failed_sends = sum(1 for r in txs if failed(r) and _addr(r, "from") == address)
    txs = [r for r in txs if not failed(r)]
    tokens = [r for r in tokens if not failed(r)]

    def sweep(extra_own=frozenset()) -> dict | None:
        rows = transfers(address, txs, tokens, extra_own)
        senders = swept_senders(address, rows, floors)
        if not senders or len(senders) > MAX_DEPOSIT_SENDERS:
            return None
        # A contract can only be a smart account or a forwarder (an ERC-4337 account is
        # normally a contract); never an exchange or service deposit address. Asked
        # only when the answer matters.
        if is_contract is not None and is_contract(address):
            return None
        share = MIN_SWEEP_SHARE if len(senders) <= FEW_SENDERS else HUB_SWEEP_SHARE
        found = _sweep(address, rows, is_contract, labels, busy, share)
        if found is None or found.get("kind") == UNKNOWN_KIND:
            return found
        if found.get("kind") == PAID_EXCHANGE:
            # nothing was swept: no senders whose asset it sweeps, no gas funder
            found["senders"] = []
            return found
        if _check_failed(is_contract, address):
            # a contract is never an exchange deposit address, and this one's status
            # is not known
            return _unknown(address, f"contract check of {address} failed")
        found["senders"] = sorted(senders)
        found["gas_funder"] = _gas_funder(rows, address, gas_fund_wei)
        if cut:
            found["incomplete"] = True
        return found

    swept = sweep()
    if swept is not None and swept.get("kind") not in (UNKNOWN_KIND, PAID_EXCHANGE):
        return swept, failed_sends
    # an operator-executed forwarder outranks "paid an exchange" and "unknown"
    found = _forwarder(client, address, txs, tokens)
    if found is not None and found.get("kind") == _SELF_CALLED:
        again = sweep(found["hashes"])
        if again is not None and again.get("kind") not in (UNKNOWN_KIND, PAID_EXCHANGE):
            again["sponsored"] = True
        return (again if again is not None else swept), failed_sends
    return (found if found is not None else swept), failed_sends


def _sweep(address, rows, is_contract, labels, busy, min_share=MIN_SWEEP_SHARE) -> dict | None:
    """The exchange / service sweep test on the filtered rows; None when it fails,
    an unknown result when it fails only because a lookup on a target failed."""
    outgoing = Counter(_addr(r, "to") for r in rows if _addr(r, "from") == address and r.get("to"))
    if not outgoing:
        return None
    total = sum(outgoing.values())
    hot, unsure, paid = [], [], []
    for target, n in outgoing.most_common(MAX_SWEEP_TARGETS):
        if _is_exchange(labels, target):
            # A label names the exchange; it is not evidence of a sweep. The target
            # counts only when the address sweeps its balance there (most of what it
            # received, within 30 days) or forwards amounts as to an unlabelled hot
            # wallet (a label never makes the test harder); merely paying it is context.
            if sweeps_balance_to(rows, address, target) or forwards_to(rows, address, target):
                hot.append((target, n))
            else:
                paid.append((target, n))
            continue
        if busy is None or not forwards_to(rows, address, target):
            continue
        # Without a label, a hot wallet is an ordinary (non-contract) busy address
        # that receives forwarded deposits: a token, a DEX router or a Tornado
        # contract is busy too but is not an exchange, and a wallet that merely
        # pays a busy address does not forward what it received. An unknown
        # contract status (no endpoint) is not taken for an EOA; a failed lookup
        # leaves the target unsure.
        known = None if is_contract is None else is_contract(target)
        if is_contract is not None and known is not False:
            if known is None and _check_failed(is_contract, target):
                unsure.append((target, n, "contract check"))
            continue
        is_busy = busy(target)
        if is_busy is None:
            unsure.append((target, n, "busy check"))
        elif is_busy:
            hot.append((target, n))
    share = sum(n for _t, n in hot) / total
    if share < min_share:
        if unsure and share + sum(n for _t, n, _w in unsure) / total >= min_share:
            what = "; ".join(f"{w} of {t} failed" for t, _n, w in unsure)
            return _unknown(address, what)
        if paid and share + sum(n for _t, n in paid) / total >= min_share:
            # It pays a labelled exchange but does not forward or sweep the value it received: a
            # customer paying an exchange, not an address the exchange controls.
            return {
                "address": address,
                "kind": PAID_EXCHANGE,
                "exchange": _names(_label(labels, t).get("entity", "") for t, _n in paid),
                "paid_targets": [t for t, _n in paid],
                "evidence": False,
            }
        return None
    base = {
        "address": address,
        "sweep_target": hot[0][0],
        "sweep_targets": [t for t, _n in hot],
    }
    # A labelled target of another category (gambling, bridge, ...) makes the address a
    # service deposit address: shown, never scored, never linking two addresses.
    services = _names(
        _label(labels, t).get("entity") or _category(labels, t)
        for t, _n in hot
        if _category(labels, t) and not _is_exchange(labels, t)
    )
    if services:
        return dict(base, kind="service_deposit", exchange="", service=services, evidence=False)
    if not any(_is_exchange(labels, t) for t, _n in hot):
        # Swept to unlabelled busy addresses only: it behaves like a deposit address, but
        # nothing says whose; the word "exchange" would claim more than is known.
        return dict(base, kind=DEPOSIT_LIKE, exchange="", service="unknown", evidence=False)
    entities = _names(
        _label(labels, t).get("entity", "") for t, _n in hot if _is_exchange(labels, t)
    )
    labelled_share = sum(n for t, n in hot if _is_exchange(labels, t)) / total
    return dict(
        base,
        kind="exchange_deposit",
        exchange=entities,
        # Only sweeps to labelled exchange wallets stood above chance in the placebo
        # test; an address that qualifies partly by activity is shown, not scored.
        evidence=labelled_share >= min_share,
    )


def make_busy_check(client, errors: list | None = None) -> Callable[[str], bool | None]:
    """The hot-wallet busy check of a run: ``has_at_least_txs(target, HOT_WALLET_TXS)``,
    cached per target. A failed check answers None (unknown, not "not busy"), is
    recorded once per target in ``errors`` (``what`` "hot wallet busy check") and is not
    cached, so it is tried again for the next address that sweeps to the same target."""
    cache: dict[str, bool] = {}
    reported: set[str] = set()

    def busy(target: str) -> bool | None:
        if target in cache:
            return cache[target]
        has = getattr(client, "has_at_least_txs", None)
        if not has:
            return False
        try:
            cache[target] = bool(has(target, HOT_WALLET_TXS))
        except ApiKeyError:
            raise
        except ApiError as exc:
            # Some explorers refuse a 10,000-row page for a busy address
            # (Routescan answers with an HTML error page). Unknown, not "not busy".
            if errors is not None and target not in reported:
                reported.add(target)
                errors.append(lookup_failed("hot wallet busy check", exc, address=target))
            return None
        return cache[target]

    return busy


def depositor_deposit_addresses(
    client,
    wallet: str,
    txs: list[dict],
    is_contract: Callable[[str], bool | None] | None = None,
    labels: dict | None = None,
    exclude: set[str] | frozenset[str] = frozenset(),
    errors: list | None = None,
    gas_fund_wei: int = GAS_FUND_WEI,
    floors: dict[str, int] | None = None,
) -> list[dict]:
    """Deposit addresses the wallet sent funds to (see the module docstring).

    ``txs`` are the wallet's ``txlist`` rows plus its ``tokentx`` rows (those with a
    ``contractAddress``). Only transfers the wallet really made count as counterparties:
    non-zero, and a token transfer only in one of the wallet's own transactions (sent by
    it, or sent to it as a sponsored EIP-7702 call). Spoofed "from the wallet" transfers
    of a fake token to look-alike addresses therefore never take one of the
    MAX_CANDIDATES slots. A labelled counterparty is skipped as a service's own wallet
    unless its label names a deposit address.

    Explorer errors on single addresses are not fatal: the run goes on, but an
    address whose answer depended on a failed lookup is not reported as "not a
    deposit address". When ``errors`` is a list, each failure is appended to it as an
    :mod:`tornado_demix.incomplete` record: the busy check of a sweep target (``what``
    "hot wallet busy check") and the counterparty it left undecided ("deposit address
    check", with the classifier's reason). A failed busy check is not cached: it is
    tried again for the next counterparty that sweeps to the same target.
    """
    wallet = wallet.lower()
    plain = [t for t in txs if not t.get("contractAddress")]
    tokens = [t for t in txs if t.get("contractAddress")]
    sent = Counter(
        _addr(r, "to")
        for r in transfers(wallet, plain, tokens)
        if _addr(r, "from") == wallet and _addr(r, "to")
    )
    busy = make_busy_check(client, errors)

    found = []
    examined = 0
    for addr, _n in sent.most_common():
        if examined >= MAX_CANDIDATES:
            break
        if addr in exclude or addr == wallet:
            continue
        # A labelled address is a service's own wallet, not a customer address, unless
        # the label itself says it is a deposit address.
        lab = _label(labels, addr)
        if lab and not _says_deposit(lab):
            continue
        examined += 1
        info = classify_deposit_address(
            client, addr, is_contract, labels, busy, gas_fund_wei, floors
        )
        if info and info.get("kind") == UNKNOWN_KIND:
            if errors is not None:
                why = info["error"]
                if info.get("failed_sends"):
                    why += f"; {info['failed_sends']} failed transaction(s) sent, not counted"
                errors.append(lookup_failed("deposit address check", why, address=addr))
            continue
        if info:
            found.append(info)
    return found


def classify_address(
    client,
    address: str,
    labels: dict | None = None,
    is_contract: Callable[[str], bool | None] | None = None,
    gas_fund_wei: int = GAS_FUND_WEI,
    floors: dict[str, int] | None = None,
) -> tuple[dict, list[dict]]:
    """Classify one given address (``classify-address``, the web UI, ``characterize``).

    Returns ``(info, errors)``: ``info`` always has a ``kind``, ``not_deposit`` when the
    tests found no deposit address, ``unknown`` (with ``error``) when a lookup the answer
    depends on failed; ``errors`` lists the failed lookups as
    :mod:`tornado_demix.incomplete` records. The address's own label is not consulted:
    the classifier describes behaviour, a label states identity.
    """
    address = address.lower()
    errors: list[dict] = []
    info, failed_sends = _classify(
        client,
        address,
        is_contract,
        labels,
        make_busy_check(client, errors),
        gas_fund_wei,
        floors,
    )
    if info is None:
        info = {"address": address, "kind": NOT_DEPOSIT, "evidence": False}
    if info.get("kind") == UNKNOWN_KIND:
        errors.append(lookup_failed("deposit address check", info["error"], address=address))
    if failed_sends:
        info["failed_sends"] = failed_sends
    return info, errors


LINKING_KINDS = ("exchange_deposit", DEPOSIT_LIKE)


def shared_deposit_hits(
    deposit_addresses: list[dict], recipients, wallet: str, evidence_only: bool = True
) -> dict:
    """{recipient: [deposit addresses]} for recipients that also sent to one of them.

    With ``evidence_only`` (the default) only deposit addresses that sweep to
    labelled exchange wallets count; one found by activity alone (``deposit_like``, or an
    exchange deposit address swept partly to unlabelled wallets) is context.
    """
    wallet = wallet.lower()
    out: dict[str, list[str]] = {}
    recips = set(recipients)
    for info in deposit_addresses:
        if info.get("kind", "exchange_deposit") not in LINKING_KINDS:
            continue  # service deposit addresses and forwarders never link two addresses
        if evidence_only and not info.get("evidence", True):
            continue
        for s in info["senders"]:
            if s != wallet and s in recips:
                out.setdefault(s, []).append(info["address"])
    return out
