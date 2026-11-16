"""Canonical rollup bridges: Arbitrum Inbox retryables and token gateways, ZKsync
Bridgehub requests (native and ERC-20) and legacy ZKsync Era requests.

Pure decoders over calldata and receipt logs, plus the one explorer query that
confirms an Arbitrum L2 credit. Written from the public ABIs of the verified
contracts (Arbitrum Inbox and Bridge on Etherscan, ZKsync L1Bridgehub on Etherscan);
no contract source is copied. Selectors and topics are keccak-256 of the signature
strings; tests/test_bridges_canonical.py re-derives every one.

Arbitrum. A call to the Inbox (``createRetryableTicket``,
``unsafeCreateRetryableTicket``, ``depositEth``) emits ``InboxMessageDelivered(uint256
indexed messageNum, bytes data)`` from the Inbox and ``MessageDelivered(...)`` from the
Bridge with the same message number, the message kind (9 retryable, 12 ETH deposit)
and the L2 sender. The trace reads the receipt, not the calldata, so a call made by a
contract on the traced address's behalf is decoded the same way.

* Retryable data: 32-byte words ``to, l2CallValue, deposit (msg.value),
  maxSubmissionCost, excessFeeRefundAddress, callValueRefundAddress, gasLimit,
  maxFeePerGas, data.length, data``. The L2 recipient is ``to`` and it receives
  ``l2CallValue``; the rest of the deposit pays the L2 fees, and what is left of it is
  refunded to ``excessFeeRefundAddress`` (not followed).
* ETH deposit data: ``abi.encodePacked(dest, msg.value)`` (20 + 32 bytes). ``dest`` is
  the caller when an EOA calls the Inbox directly, otherwise the caller's aliased
  address (alias rule below); the message holds the address actually credited, so the
  trace takes it from there instead of applying the rule itself.
* Alias rule: L2 address = L1 address + 0x1111000000000000000000000000000000001111
  (mod 2**160). The Bridge's ``MessageDelivered`` carries the aliased caller as the
  sender; on L2 the credit is a transaction from that aliased address to the recipient
  with the credited value (an auto-redeemed retryable or an ETH deposit transaction;
  both appear in the recipient's normal transaction list on Arbiscan, checked on the
  fixtures). ``find_l2_credit`` searches for it: same recipient, same value, from the
  aliased sender, within 7 days after the L1 transaction (the retryable lifetime; the
  first 2 hours are read first), not already claimed by an earlier message.

ZKsync. ``requestL2TransactionDirect(L2TransactionRequestDirect)``: the L2 recipient is
``l2Contract`` with ``l2Value``; ``mintValue`` is the base token minted on L2 (value plus
fees); ``refundRecipient`` receives unused fees only and is not the recipient (zero means
the sender). ``requestL2TransactionTwoBridges(L2TransactionRequestTwoBridgesOuter)``: the
recipient is the L2 receiver inside ``secondBridgeCalldata``: legacy encoding
``abi.encode(l1Token, amount, l2Receiver)``, or version byte 0x01 followed by
``abi.encode(assetId, transferData)`` with ``transferData = abi.encode(amount,
l2Receiver, token)``; for an ERC-20 deposit the token and amount come from the same
encoding. The legacy Era ``requestL2Transaction`` (Era DiamondProxy) gives
``_contractL2`` and ``_l2Value``. ZKsync chains are not in the networks registry, so
these are decoded and shown, not followed.

Arbitrum token gateways: see the section of that name below.
"""

from __future__ import annotations

import time

from .bridges import MAX_FILL_WAIT, _address, _end_block, _words, listed
from .txstatus import failed as _failed

ARBITRUM = "arbitrum"
ZKSYNC = "zksync"
ARBITRUM_ONE = 42161
ARBITRUM_INBOX = "0x4dbd4fc535ac27206064b68ffcf827b0a60bab3f"
ALIAS_OFFSET = 0x1111000000000000000000000000000000001111
_MOD = 1 << 160

INBOX_SELECTORS = {
    "0x679b6ded": "createRetryableTicket",
    "0x6e6e8a6a": "unsafeCreateRetryableTicket",
    "0x439370b1": "depositEth",
}
# L1GatewayRouter (or a gateway called directly): ERC-20 deposits to Arbitrum
GATEWAY_SELECTORS = {
    "0xd2ce7d65": "outboundTransfer",
    "0x4fb1a07b": "outboundTransferCustomRefund",
}
SELECTOR_FINALIZE_INBOUND = "0x2e567b36"  # finalizeInboundTransfer on the L2 gateway
SELECTOR_CALCULATE_L2_TOKEN = "0xa7e28d48"  # calculateL2TokenAddress(address)
SELECTOR_GET_GATEWAY = "0xbda009fe"  # getGateway(address) on the L1GatewayRouter
ARBITRUM_ROUTER = "0x72ce9c846789fdb6fc1f34ac4ad25dd9ef7031ef"  # L1GatewayRouter
TOPIC_DEPOSIT_INITIATED = "0xb8910b9960c443aac3240b98585384e3a6f109fbf6969e264c3f183d69aba7e1"
ZERO_ADDRESS = "0x" + "0" * 40
BRIDGEHUB_SELECTORS = {
    "0xd52471c1": "requestL2TransactionDirect",
    "0x24fd57fb": "requestL2TransactionTwoBridges",
}
# ZKsync Era Mailbox on the Era DiamondProxy, from before the Bridgehub
LEGACY_MAILBOX_SELECTORS = {"0xeb672419": "requestL2Transaction"}
ZKSYNC_ERA = 324
TOPIC_INBOX_MESSAGE = "0xff64905f73a67fb594e0f940a8075a860db489ad991e032f48c81123eb52d60b"
TOPIC_BRIDGE_MESSAGE = "0x5e3c1311ea442664e8b1611bfabef659120ea7a0a2cfc0667700bebc69cbffe1"
FIRST_WINDOW = 2 * 3600  # seconds: the first L2 read (credits land ~10 min after L1)
KIND_RETRYABLE = 9  # L1MessageType_submitRetryableTx
KIND_ETH_DEPOSIT = 12  # L1MessageType_ethDeposit
_KINDS = {KIND_RETRYABLE: "retryable", KIND_ETH_DEPOSIT: "eth_deposit"}

# States of a canonical bridge hop (edge["bridge"]["state"]), besides those of bridges.py
CREDITED = "l2_credited"  # the L2 credit was found: the trace goes on there
L2_CREDIT_NOT_FOUND = "l2_credit_not_found"

# Address pages of L2 explorers for chains outside the networks registry
# (ZKsync Era: https://docs.zksync.io/zksync-network/zksync-era/environment).
L2_EXPLORERS = {
    ARBITRUM_ONE: "https://arbiscan.io/address/",
    324: "https://explorer.zksync.io/address/",
}


def l1_to_l2_alias(address: str) -> str:
    """The L2 address of an L1 contract (or of a caller the Inbox aliases)."""
    return "0x{:040x}".format((int(address, 16) + ALIAS_OFFSET) % _MOD)


def l2_to_l1_alias(address: str) -> str:
    """Undo ``l1_to_l2_alias``."""
    return "0x{:040x}".format((int(address, 16) - ALIAS_OFFSET) % _MOD)


def _body(data, selectors):
    data = (data or "").lower()
    name = selectors.get(data[:10])
    return (name, data[10:]) if name else (None, None)


def _word_list(hexdata):
    return [int(hexdata[i : i + 64], 16) for i in range(0, len(hexdata) - len(hexdata) % 64, 64)]


def _bytes_at(hexdata, offset):
    """ABI ``bytes`` whose head (offset word) is at ``offset`` bytes, as hex (no 0x)."""
    start = offset * 2
    length = int(hexdata[start : start + 64], 16)
    body = hexdata[start + 64 : start + 64 + length * 2]
    if len(body) != length * 2:
        raise ValueError("bytes cut short")
    return body


# --- Arbitrum ---------------------------------------------------------------------------


def decode_inbox_call(data: str) -> dict | None:
    """The Inbox call in calldata ``data``, or None (another call, or cut short).

    Retryables: {function, to, l2_call_value, max_submission_cost,
    excess_fee_refund_address, call_value_refund_address, gas_limit, max_fee_per_gas,
    data}; ``depositEth``: {function} (the credited address follows from the caller,
    see the module doc).
    """
    name, body = _body(data, INBOX_SELECTORS)
    if name is None:
        return None
    if name == "depositEth":
        return {"function": name}
    w = _word_list(body)
    if len(w) < 8:
        return None
    try:
        payload = _bytes_at(body, w[7])
    except (ValueError, IndexError):
        return None
    return {
        "function": name,
        "to": _address(w[0]),
        "l2_call_value": w[1],
        "max_submission_cost": w[2],
        "excess_fee_refund_address": _address(w[3]),
        "call_value_refund_address": _address(w[4]),
        "gas_limit": w[5],
        "max_fee_per_gas": w[6],
        "data": "0x" + payload,
    }


def _is_role(table, chain_id, address, role):
    row = listed(table, chain_id, address)
    return row is not None and row["protocol"] == ARBITRUM and row["role"] == role


def decode_inbox_messages(receipt, tx_hash, chain_id, table) -> list[dict]:
    """Retryable and ETH-deposit messages delivered in one transaction's receipt.

    A message counts only when its ``InboxMessageDelivered`` log was emitted by the
    table's Arbitrum Inbox on ``chain_id``. Returns [{protocol, kind, message_num,
    tx_hash, log_index, inbox, recipient, l2_call_value, deposit, l2_sender, l1_sender,
    tx_from, excess_fee_refund_address, call_value_refund_address,
    max_submission_cost, l2_data}]; ``l2_data`` is a retryable's L2 calldata (hex);
    ``l2_sender`` (the aliased caller, from the Bridge log) and
    ``l1_sender`` are None when the Bridge log is missing, and the kind then follows
    from the length of the data (52 bytes: ETH deposit). Amounts are wei.
    """
    if not receipt or receipt.get("status") == "0x0":
        return []
    logs = receipt.get("logs") or []
    headers = {}
    for log in logs:
        topics = [t.lower() for t in log.get("topics") or []]
        if len(topics) < 2 or topics[0] != TOPIC_BRIDGE_MESSAGE:
            continue
        if not _is_role(table, chain_id, log.get("address"), "Bridge"):
            continue
        w = _words(log.get("data"))
        if len(w) >= 3:
            headers[int(topics[1], 16)] = {"kind": w[1], "sender": _address(w[2])}
    tx_from = (receipt.get("from") or "").lower() or None
    out = []
    for log in logs:
        topics = [t.lower() for t in log.get("topics") or []]
        if len(topics) < 2 or topics[0] != TOPIC_INBOX_MESSAGE:
            continue
        inbox = (log.get("address") or "").lower()
        if not _is_role(table, chain_id, inbox, "Inbox"):
            continue
        num = int(topics[1], 16)
        hexdata = (log.get("data") or "")[2:]
        try:
            payload = _bytes_at(hexdata, int(hexdata[:64], 16))
        except (ValueError, IndexError):
            continue
        header = headers.get(num)
        if header is not None:
            kind = _KINDS.get(header["kind"])
        else:
            kind = "eth_deposit" if len(payload) == 104 else "retryable"
        msg = {
            "protocol": ARBITRUM,
            "kind": kind,
            "message_num": num,
            "tx_hash": (tx_hash or "").lower(),
            "log_index": int(log["logIndex"], 16) if log.get("logIndex") else None,
            "inbox": inbox,
            "l2_sender": header["sender"] if header else None,
            "l1_sender": l2_to_l1_alias(header["sender"]) if header else None,
            "tx_from": tx_from,
            "excess_fee_refund_address": None,
            "call_value_refund_address": None,
            "max_submission_cost": None,
            "l2_data": None,
        }
        if kind == "eth_deposit" and len(payload) == 104:
            value = int(payload[40:104], 16)
            msg.update(recipient="0x" + payload[:40], l2_call_value=value, deposit=value)
        elif kind == "retryable":
            w = _word_list(payload)
            if len(w) < 9:
                continue
            data = payload[9 * 64 : 9 * 64 + w[8] * 2]
            msg.update(
                l2_data="0x" + data if len(data) == w[8] * 2 else None,
                recipient=_address(w[0]),
                l2_call_value=w[1],
                deposit=w[2],
                max_submission_cost=w[3],
                excess_fee_refund_address=_address(w[4]),
                call_value_refund_address=_address(w[5]),
            )
        else:
            continue  # another message kind: not a transfer of value to an L2 address
        out.append(msg)
    return out


def _credits(rows, message, ts, claimed):
    """(ts, block, hash) of the rows that can be the L2 credit of ``message``."""
    recipient, sender = message["recipient"], message.get("l2_sender")
    found = []
    for row in rows:
        if (row.get("to") or "").lower() != recipient or _failed(row):
            continue
        if sender is not None and (row.get("from") or "").lower() != sender:
            continue
        try:
            value, row_ts = int(row.get("value") or 0), int(row.get("timeStamp") or 0)
        except (TypeError, ValueError):
            continue
        tx_hash = (row.get("hash") or "").lower()
        if value != message["l2_call_value"] or row_ts < ts or tx_hash in claimed:
            continue
        found.append((row_ts, int(row.get("blockNumber") or 0), tx_hash))
    return found


def _search(client, action, recipient, ts, now, pick, extra=None):
    """Rows of ``recipient`` that ``pick`` keeps, read in two windows after ``ts``.

    First from ``ts`` (less a minute) up to FIRST_WINDOW later and, only when nothing
    matches, on to MAX_FILL_WAIT later (never past now). Explorer errors propagate.
    """
    start = client.block_by_time(ts - 60, "before")
    first_ts, last_ts = min(ts + FIRST_WINDOW, now), min(ts + MAX_FILL_WAIT, now)
    end = _end_block(client, first_ts, now)
    found = pick(client.fetch_all(action, recipient, start, end, extra=extra))
    if not found and last_ts > first_ts:
        later = _end_block(client, last_ts, now)
        if later > end:
            found = pick(client.fetch_all(action, recipient, end + 1, later, extra=extra))
    return found


def find_l2_credit(client, message, ts, now=None, claimed=()) -> dict | None:
    """The Arbitrum transaction that credited ``message`` on L2, or None.

    Reads the recipient's normal transactions from ``ts`` (the L1 transaction's time,
    less a minute), first up to FIRST_WINDOW later and, only when nothing matches, on
    to 7 days later (never past now). Takes the earliest one to the recipient with
    exactly ``l2_call_value`` wei, from the aliased L1 sender when that is known, not
    failed, and not in ``claimed`` (credits already given to an earlier message).
    Returns {tx_hash, block, ts, value}. Explorer errors propagate.
    """
    now = int(time.time()) if now is None else now
    claimed = set(claimed)
    found = _search(
        client,
        "txlist",
        message["recipient"],
        ts,
        now,
        lambda rows: _credits(rows, message, ts, claimed),
    )
    if not found:
        return None
    row_ts, block, tx_hash = min(found)
    return {"tx_hash": tx_hash, "block": block, "ts": row_ts, "value": message["l2_call_value"]}


# --- Arbitrum token gateways ------------------------------------------------------------
#
# ``outboundTransfer(token, to, amount, maxGas, gasPriceBid, data)`` and
# ``outboundTransferCustomRefund(token, refundTo, to, amount, maxGas, gasPriceBid, data)``
# on the L1GatewayRouter move the tokens from the caller into the token's L1 gateway,
# which emits ``DepositInitiated(address l1Token, address indexed from, address indexed
# to, uint256 indexed sequenceNumber, uint256 amount)`` and creates a retryable through
# the Inbox: to = the L2 gateway, l2CallValue 0 (the ETH sent pays the L2 fees), data =
# ``finalizeInboundTransfer(l1Token, from, to, amount, data)``. The sequence number is
# the Inbox message number. On L2 the gateway mints the L2 token to ``to`` (a WETH-style
# gateway pays it out from itself instead); the L2 token of an L1 token is what the
# router's ``calculateL2TokenAddress`` returns.


def decode_gateway_call(data: str) -> dict | None:
    """The gateway call in calldata ``data``, or None.

    Returns {function, token, refund_to (None for outboundTransfer), to, amount,
    max_gas, gas_price_bid}.
    """
    name, body = _body(data, GATEWAY_SELECTORS)
    if name is None:
        return None
    w = _word_list(body)
    custom = name == "outboundTransferCustomRefund"
    if len(w) < (7 if custom else 6):
        return None
    if custom:
        token, refund, to, amount, gas, bid = w[:6]
    else:
        (token, to, amount, gas, bid), refund = w[:5], None
    return {
        "function": name,
        "token": _address(token),
        "refund_to": _address(refund) if refund is not None else None,
        "to": _address(to),
        "amount": amount,
        "max_gas": gas,
        "gas_price_bid": bid,
    }


def decode_finalize_inbound(data) -> dict | None:
    """``finalizeInboundTransfer(token, from, to, amount, data)`` in L2 calldata, or None."""
    data = (data or "").lower()
    if data[:10] != SELECTOR_FINALIZE_INBOUND:
        return None
    w = _word_list(data[10:])
    if len(w) < 4:
        return None
    return {"token": _address(w[0]), "from": _address(w[1]), "to": _address(w[2]), "amount": w[3]}


def decode_gateway_deposits(receipt, tx_hash, chain_id, table) -> list[dict]:
    """ERC-20 deposits to Arbitrum through a token gateway in one receipt.

    A ``DepositInitiated`` log counts only when the table's Inbox delivered a retryable
    with its sequence number, sent by the contract that emitted the log (the Bridge
    log's L1 sender, so the Bridge log must be there), whose L2 calldata is
    ``finalizeInboundTransfer`` of the same token, sender, recipient and amount. So an
    unlisted gateway counts too: the Inbox message confirms it. Returns [{protocol,
    kind ("token_deposit"), message_num, tx_hash, log_index, gateway, l2_gateway,
    l1_token, from, recipient, amount, tx_from}].
    """
    if not receipt or receipt.get("status") == "0x0":
        return []
    messages = {
        m["message_num"]: m
        for m in decode_inbox_messages(receipt, tx_hash, chain_id, table)
        if m["kind"] == "retryable"
    }
    out = []
    for log in receipt.get("logs") or []:
        topics = [t.lower() for t in log.get("topics") or []]
        if len(topics) < 4 or topics[0] != TOPIC_DEPOSIT_INITIATED:
            continue
        w = _words(log.get("data"))
        if len(w) < 2:
            continue
        gateway = (log.get("address") or "").lower()
        num = int(topics[3], 16)
        event = {
            "token": _address(w[0]),
            "from": _address(int(topics[1], 16)),
            "to": _address(int(topics[2], 16)),
            "amount": w[1],
        }
        msg = messages.get(num)
        if msg is None or msg["l1_sender"] != gateway:
            continue
        if decode_finalize_inbound(msg["l2_data"]) != event:
            continue  # the L2 calldata must say the same as the event
        out.append(
            {
                "protocol": ARBITRUM,
                "kind": "token_deposit",
                "message_num": num,
                "tx_hash": (tx_hash or "").lower(),
                "log_index": int(log["logIndex"], 16) if log.get("logIndex") else None,
                "gateway": gateway,
                "l2_gateway": msg["recipient"],
                "l1_token": event["token"],
                "from": event["from"],
                "recipient": event["to"],
                "amount": event["amount"],
                "tx_from": msg["tx_from"],
            }
        )
    return out


def l2_token_call(l1_token: str) -> str:
    """Calldata of ``calculateL2TokenAddress(l1_token)`` (for the L1GatewayRouter)."""
    return SELECTOR_CALCULATE_L2_TOKEN + l1_token.lower()[2:].rjust(64, "0")


def gateway_of_call(l1_token: str) -> str:
    """Calldata of ``getGateway(l1_token)`` (for the L1GatewayRouter): the gateway the
    router uses for that token."""
    return SELECTOR_GET_GATEWAY + l1_token.lower()[2:].rjust(64, "0")


def address_result(result) -> str | None:
    """The address an ``eth_call`` returned, or None (empty, short or zero)."""
    result = result or ""
    body = result[2:] if result.startswith("0x") else ""
    if len(body) < 64:
        return None
    value = int(body[:64], 16)
    return _address(value) if value else None


def _token_credits(rows, deposit, l2_token, ts, claimed):
    out = []
    payers = {ZERO_ADDRESS, deposit.get("l2_gateway")}
    for row in rows:
        if (row.get("to") or "").lower() != deposit["recipient"]:
            continue
        if (row.get("contractAddress") or "").lower() != l2_token:
            continue
        if (row.get("from") or "").lower() not in payers:
            continue
        try:
            value, row_ts = int(row.get("value") or 0), int(row.get("timeStamp") or 0)
        except (TypeError, ValueError):
            continue
        tx_hash = (row.get("hash") or "").lower()
        if value != deposit["amount"] or row_ts < ts or tx_hash in claimed:
            continue
        out.append((row_ts, int(row.get("blockNumber") or 0), tx_hash, row))
    return out


def find_l2_token_credit(client, deposit, l2_token, ts, now=None, claimed=()) -> dict | None:
    """The Arbitrum transaction that credited a gateway ``deposit`` in ``l2_token``, or None.

    Reads the recipient's transfers of ``l2_token`` in the windows of find_l2_credit and
    takes the earliest one to the recipient of exactly the deposited amount, minted
    (from the zero address) or paid by the deposit's L2 gateway, not in ``claimed``
    (credits already given to an earlier deposit). Returns {tx_hash, block, ts, value,
    symbol}. Explorer errors propagate.
    """
    now = int(time.time()) if now is None else now
    l2_token, claimed = l2_token.lower(), set(claimed)
    found = _search(
        client,
        "tokentx",
        deposit["recipient"],
        ts,
        now,
        lambda rows: _token_credits(rows, deposit, l2_token, ts, claimed),
        extra={"contractaddress": l2_token},
    )
    if not found:
        return None
    row_ts, block, tx_hash, row = min(found, key=lambda f: f[:3])
    return {
        "tx_hash": tx_hash,
        "block": block,
        "ts": row_ts,
        "value": deposit["amount"],
        "symbol": row.get("tokenSymbol") or "",
    }


# --- ZKsync -----------------------------------------------------------------------------


def _second_bridge_transfer(blob):
    """(l1 token, amount, L2 receiver) in the second bridge's calldata (hex, no 0x).

    Each is None when the encoding is not known (the token also when the new encoding
    carries none).
    """
    if len(blob) == 192 and blob[:2] == "00":  # legacy: (l1Token, amount, l2Receiver)
        w = _word_list(blob)
        return _address(w[0]), w[1], _address(w[2])
    if blob[:2] == "01":  # new: 0x01 ++ abi.encode(assetId, transferData)
        rest = blob[2:]
        try:
            transfer = _bytes_at(rest, int(rest[64:128], 16))
        except (ValueError, IndexError):
            return None, None, None
        w = _word_list(transfer)  # transferData: (amount, l2Receiver, token)
        if len(w) < 2:
            return None, None, None
        return (_address(w[2]) if len(w) >= 3 else None), w[0], _address(w[1])
    return None, None, None


def decode_bridgehub_call(data: str) -> dict | None:
    """The Bridgehub request in calldata ``data``, or None.

    Returns {function, chain_id, mint_value, l2_value, l2_recipient, refund_recipient,
    second_bridge, second_bridge_value, msg_value, l1_token, amount}; ``msg_value`` is
    the ETH the call must carry on an ETH-based chain (mintValue, plus secondBridgeValue
    for two bridges); ``l1_token`` and ``amount`` are the token deposit through the
    second bridge (None for a direct request); ``l2_recipient`` (and they) are None when
    the second bridge's encoding is not known.
    """
    name, body = _body(data, BRIDGEHUB_SELECTORS)
    if name is None:
        return None
    w = _word_list(body)
    try:
        s = w[0] // 32  # the struct is dynamic: word 0 is its offset
        if name == "requestL2TransactionDirect":
            f = w[s : s + 9]
            if len(f) < 9:
                return None
            return {
                "function": name,
                "chain_id": f[0],
                "mint_value": f[1],
                "l2_recipient": _address(f[2]),
                "l2_value": f[3],
                "refund_recipient": _address(f[8]),
                "second_bridge": None,
                "second_bridge_value": 0,
                "msg_value": f[1],
                "l1_token": None,
                "amount": None,
            }
        f = w[s : s + 9]
        if len(f) < 9:
            return None
        blob = _bytes_at(body, w[0] + f[8])
    except (IndexError, ValueError):
        return None
    token, amount, receiver = _second_bridge_transfer(blob)
    return {
        "function": name,
        "chain_id": f[0],
        "mint_value": f[1],
        "l2_value": f[2],
        "l2_recipient": receiver,
        "refund_recipient": _address(f[5]),
        "second_bridge": _address(f[6]),
        "second_bridge_value": f[7],
        "msg_value": f[1] + f[7],
        "l1_token": token,
        "amount": amount,
    }


def decode_legacy_request(data: str) -> dict | None:
    """The ZKsync Era Mailbox ``requestL2Transaction`` in calldata ``data``, or None.

    ``requestL2Transaction(address _contractL2, uint256 _l2Value, bytes _calldata,
    uint256 _l2GasLimit, uint256 _l2GasPerPubdataByteLimit, bytes[] _factoryDeps,
    address _refundRecipient)`` on the Era DiamondProxy: the L2 recipient is
    ``_contractL2`` with ``_l2Value``; the rest of msg.value pays the L2 base cost, and
    ``_refundRecipient`` gets unused fees only (zero means the sender). Returns
    {function, chain_id (324), l2_recipient, l2_value, refund_recipient, gas_limit}.
    """
    name, body = _body(data, LEGACY_MAILBOX_SELECTORS)
    if name is None:
        return None
    w = _word_list(body)
    if len(w) < 7:
        return None
    return {
        "function": name,
        "chain_id": ZKSYNC_ERA,
        "l2_recipient": _address(w[0]),
        "l2_value": w[1],
        "refund_recipient": _address(w[6]),
        "gas_limit": w[3],
    }


# --- links ------------------------------------------------------------------------------


def recipient_url(chain_id, address, destinations=None) -> str:
    """Explorer page of an L2 recipient: the networks registry, then L2_EXPLORERS, or ""."""
    net = destinations.network(chain_id) if destinations is not None and chain_id else None
    if net is not None:
        return net.addr_url(address)
    base = L2_EXPLORERS.get(chain_id)
    return base + address if base else ""


def recipient_links(info, destinations=None) -> list[dict]:
    """[{label, address, url}] for the L2 recipient and a non-zero refund recipient.

    ``url`` is "" when no explorer of the destination chain is known; the address is
    shown in full either way.
    """
    chain = info.get("dest_chain_id")
    out = []
    if info.get("dest_recipient"):
        # a named bridge's recipient (bridges_named.py): linked only on a known EVM chain;
        # a Bitcoin, Solana or THORChain address is shown in full without a link
        addr = info["dest_recipient"]
        evm = chain is not None and addr.startswith("0x") and len(addr) == 42
        out.append(
            {
                "label": "recipient",
                "address": addr,
                "url": recipient_url(chain, addr, destinations) if evm else "",
            }
        )
    if info.get("l2_recipient"):
        addr = info["l2_recipient"]
        out.append(
            {
                "label": "L2 recipient",
                "address": addr,
                "url": recipient_url(chain, addr, destinations),
            }
        )
    refund = info.get("refund_recipient")
    if refund and int(refund, 16) != 0:
        out.append(
            {
                "label": "refund recipient (unused fees only)",
                "address": refund,
                "url": recipient_url(chain, refund, destinations),
            }
        )
    return out
