"""CoW Protocol settlements: a sell order filled by GPv2Settlement, read from its receipt.

GPv2Settlement settles a batch of signed orders in one transaction. For each order it
pulls the sell tokens from the owner into the settlement contract (the transfer the trace
sees), logs ``Trade(address indexed owner, IERC20 sellToken, IERC20 buyToken, uint256
sellAmount, uint256 buyAmount, uint256 feeAmount, bytes orderUid)`` (sellAmount is the
amount pulled, fee included) and pays the buy tokens out to the order's receiver (the
owner unless the order names another address). Written from the public event ABI of the
verified contract (cowprotocol/contracts, ``GPv2Settlement.sol``); no code is copied.

The receiver is not in the event (it is in the signed order inside the settle calldata),
so the payout is found in the same receipt: a ``Transfer`` of the buy token from the
settlement of exactly ``buyAmount`` (the owner's own when there is one, else the only
one, and only when no other trade of the receipt has the same buy token and amount; see
``payout_for``). A native buy token (``0xeeee...``) is paid by an internal transaction,
which the receipt does not show: only a payment to the owner, sent by the settlement, can
be read, and each such payment pays one trade.

Batch caveat: the other orders of the batch, and the liquidity the solver used, share the
transaction. The buy tokens of an order can come from another user's sell (a coincidence
of wants) rather than from a pool: the payout is the owner's order output, not a path
through the transaction, and an equal payout of another order to another address makes
the receiver ambiguous (the branch then ends with the order shown).
"""

from __future__ import annotations

from .bridges import _address, _words

COW = "cow"
SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
# the chains of the networks registry where docs.cow.fi lists GPv2Settlement at the same
# address (Arbitrum One, Avalanche, Base, BNB, Ethereum, Gnosis, Optimism, Polygon; the
# page also lists Ink, Linea and Plasma, which are not in the registry)
CHAINS = frozenset({1, 10, 56, 100, 137, 8453, 42161, 43114})
SOURCE_URL = "https://docs.cow.fi/cow-protocol/reference/contracts/core"
NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

TOPIC_TRADE = "0xa07a543ab8a018198e99ca0184c93fe9050a79400a0a723441f84de1d972cc17"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# States (edge["swap"]["state"])
SETTLED = "settled"  # the owner's trade and its payout were found: followed
NOT_TIED = "trade_not_tied"  # no trade of the traced owner, token and amount
PAYOUT_NOT_FOUND = "payout_not_found"  # the trade is tied, its payout is not identified
STATES = {
    SETTLED: "the owner's trade and its buy-token payout were found; followed as a swap",
    NOT_TIED: "no Trade of the traced owner with this sell token and amount",
    PAYOUT_NOT_FOUND: "the trade is tied but its payout to the receiver is not identified",
}


def is_settlement(chain_id, address) -> bool:
    return chain_id in CHAINS and (address or "").lower() == SETTLEMENT


def _ok(receipt):
    return bool(receipt) and receipt.get("status") != "0x0"


def _index(log):
    try:
        return int(log["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None


def decode_trades(receipt, settlement) -> list[dict]:
    """``Trade`` logs of ``settlement`` in a receipt: [{owner, sell_token, buy_token,
    sell_amount, buy_amount, fee_amount, order_uid (hex), log_index}]."""
    if not _ok(receipt):
        return []
    out = []
    for log in receipt.get("logs") or []:
        topics = [t.lower() for t in log.get("topics") or []]
        if (log.get("address") or "").lower() != settlement or len(topics) < 2:
            continue
        if topics[0] != TOPIC_TRADE:
            continue
        raw = (log.get("data") or "")[2:]
        w = _words(log.get("data"))
        if len(w) < 6:
            continue
        try:
            off = w[5] * 2
            length = int(raw[off : off + 64], 16)
            uid = raw[off + 64 : off + 64 + length * 2]
        except ValueError:
            continue
        out.append(
            {
                "owner": _address(topics[1]),
                "sell_token": _address(w[0]),
                "buy_token": _address(w[1]),
                "sell_amount": w[2],
                "buy_amount": w[3],
                "fee_amount": w[4],
                "order_uid": "0x" + uid,
                "log_index": _index(log),
            }
        )
    return out


def payouts(receipt, settlement, token) -> list[dict]:
    """ERC-20 ``Transfer`` logs of ``token`` from ``settlement``: [{to, amount, log_index}]."""
    out = []
    for log in (receipt or {}).get("logs") or []:
        topics = [t.lower() for t in log.get("topics") or []]
        if (log.get("address") or "").lower() != token or len(topics) < 3:
            continue
        if topics[0] != TOPIC_TRANSFER or _address(topics[1]) != settlement:
            continue
        w = _words(log.get("data"))
        if w:
            out.append({"to": _address(topics[2]), "amount": w[0], "log_index": _index(log)})
    return out


def payout_for(tied, trades, payouts, owner, taken=()):
    """The payout of the ``tied`` trade among the settlement's buy-token ``payouts``, or
    None when it cannot be told from another order's.

    Only payouts of exactly the trade's buy amount count. Payouts to the owner are taken
    only when there are as many of them as the owner has trades of that buy token and
    amount (each one then pays one of those trades; ``taken``: log indexes already
    given). With none to the owner, the only such payout is taken only when the tied
    trade is the only trade of the receipt with that buy token and amount: otherwise it
    may be another order's receiver.
    """
    key = (tied["buy_token"], tied["buy_amount"])
    same = [t for t in trades if (t["buy_token"], t["buy_amount"]) == key]
    equal = [p for p in payouts if p["amount"] == tied["buy_amount"]]
    mine = [p for p in equal if p["to"] == owner]
    if mine:
        if len(mine) != len([t for t in same if t["owner"] == owner]):
            return None
        free = [p for p in mine if p["log_index"] not in taken]
        return free[0] if free else None
    if len(same) == 1 and len(equal) == 1 and equal[0]["log_index"] not in taken:
        return equal[0]
    return None
