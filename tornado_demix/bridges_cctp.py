"""Circle CCTP (Cross-Chain Transfer Protocol), v1 and v2: USDC burned on one chain and
minted on another.

Pure decoders over calldata and receipt logs plus the destination mint search. Written
from Circle's contract interfaces (circlefin/evm-cctp-contracts, Apache-2.0; no code is
copied); selectors and topics are keccak-256 of the signature strings and
tests/test_bridges_cctp.py re-derives every one. Contract addresses are in
data/bridges.csv, taken from Circle's EVM contracts pages (v1 and v2). Destination
domains come from ``bridges_named.CCTP_DOMAINS`` (the one table of CCTP domains).

How a deposit is made and found again:

* ``TokenMessenger.depositForBurn(amount, destinationDomain, mintRecipient, burnToken)``
  and ``depositForBurnWithCaller(..., destinationCaller)`` (v1), ``depositForBurn(...,
  destinationCaller, maxFee, minFinalityThreshold)`` and ``depositForBurnWithHook(...,
  hookData)`` (v2) pull the tokens from the caller into the TokenMinter (the transfer
  the trace sees) and burn them there. The TokenMessenger logs ``DepositForBurn``: v1
  ``(uint64 indexed nonce, address indexed burnToken, uint256 amount, address indexed
  depositor, bytes32 mintRecipient, uint32 destinationDomain, bytes32
  destinationTokenMessenger, bytes32 destinationCaller)``; v2 ``(address indexed
  burnToken, uint256 amount, address indexed depositor, bytes32 mintRecipient, uint32
  destinationDomain, bytes32 destinationTokenMessenger, bytes32 destinationCaller,
  uint256 maxFee, uint32 indexed minFinalityThreshold, bytes hookData)``.
* On the destination the MessageTransmitter's ``receiveMessage`` logs ``MessageReceived``
  (v1 ``(address indexed caller, uint32 sourceDomain, uint64 indexed nonce, bytes32
  sender, bytes messageBody)``; v2 the nonce is a bytes32 and ``uint32 indexed
  finalityThresholdExecuted`` is added) and the TokenMessenger mints to the recipient,
  logging ``MintAndWithdraw(address indexed mintRecipient, uint256 amount, address
  indexed mintToken)`` (v2 adds ``uint256 feeCollected``). The message body (burn
  message) repeats burnToken, mintRecipient, amount and messageSender (v2 also maxFee
  and feeExecuted).

``find_mint`` reads the recipient's ``MintAndWithdraw`` logs of the destination
messenger the deposit names, and takes one only when the same transaction holds a
``MessageReceived`` of a listed transmitter from the deposit's source domain whose body
is the deposit's (burn token, recipient, amount, sender; and the nonce for v1, where the
source chain knows it; v2 assigns the nonce off chain, so the body alone ties it).
"""

from __future__ import annotations

import time

from .bridges import MAX_FILL_WAIT, _address, _end_block, _words, listed
from .bridges_named import CCTP_DOMAINS, _bytes_at, _log_index, _ok, _recipient

CCTP = "cctp"

TOPIC_DEPOSIT_V1 = "0x2fa9ca894982930190727e75500a97d8dc500233a5065e0f3126c48fbe0343c0"
TOPIC_DEPOSIT_V2 = "0x0c8c1cbdc5190613ebd485511d4e2812cfa45eecb79d845893331fedad5130a5"
TOPIC_MINT_V1 = "0x1b2a7ff080b8cb6ff436ce0372e399692bbfb6d4ae5766fd8d58a7b8cc6142e6"
TOPIC_MINT_V2 = "0x50c55e915134d457debfa58eb6f4342956f8b0616d51a89a3659360178e1ab63"
TOPIC_RECEIVED_V1 = "0x58200b4c34ae05ee816d710053fff3fb75af4395915d3d2a771b24aa10e3cc5d"
TOPIC_RECEIVED_V2 = "0xff48c13eda96b1cceacc6b9edeedc9e9db9d6226afbc30146b720c19d3addb1c"
MINT_TOPICS = {1: TOPIC_MINT_V1, 2: TOPIC_MINT_V2}

# selector -> (function, CCTP version)
CALLS = {
    "0x6fd3504e": ("depositForBurn", 1),
    "0xf856ddb6": ("depositForBurnWithCaller", 1),
    "0x8e0250ee": ("depositForBurn", 2),
    "0x779b432d": ("depositForBurnWithHook", 2),
}

# End states (edge["bridge"]["state"])
MINTED = "minted"  # the mint was found: the trace goes on there
MINT_NOT_FOUND = "mint_not_found"
# a destination without an EVM chain id in the domain table: not an EVM chain (Solana,
# Noble, ...) or a newer domain whose chain id is not taken from a Circle page
MINTED_OFF_EVM = "minted_off_evm"
STATES = {
    MINTED: "the mint on the destination was found; the trace goes on from the recipient",
    MINT_NOT_FOUND: "no mint of this message to the recipient within the search window",
    MINTED_OFF_EVM: "the destination domain has no EVM chain id here (Solana, Noble and "
    "other non-EVM or newer domains): recipient shown",
}

_ROLES = (("TokenMessenger", "messenger"), ("MessageTransmitter", "transmitter"))
_ROLES += (("TokenMinter", "minter"),)


def contract_role(row: dict) -> tuple[str, int] | None:
    """(messenger | transmitter | minter, version) of a CCTP row of the bridge table."""
    if row.get("protocol") != CCTP:
        return None
    role = row.get("role") or ""
    for prefix, kind in _ROLES:
        if role.startswith(prefix):
            return kind, 2 if role[len(prefix) :].startswith("V2") else 1
    return None


def _is(table, chain_id, address, kind, version=None):
    row = listed(table, chain_id, address)
    role = contract_role(row) if row else None
    return role is not None and role[0] == kind and version in (None, role[1])


def domain_chain(domain: int) -> tuple[str, int | None]:
    """(name, EVM chain id or None) of a CCTP domain; ("domain <n>", None) when unknown."""
    found = CCTP_DOMAINS.get(domain)
    return found if found is not None else (f"domain {domain}", None)


def origin_domain(chain_id) -> int | None:
    """The CCTP domain of an EVM chain, or None."""
    for domain, (_name, chain) in CCTP_DOMAINS.items():
        if chain is not None and chain == chain_id:
            return domain
    return None


def _shown(word: int, domain: int) -> str:
    chain = CCTP_DOMAINS.get(domain)
    return _recipient(word, chain)


def decode_call(data: str) -> dict | None:
    """A TokenMessenger deposit call: {function, version, amount, dest_domain, recipient,
    burn_token, destination_caller (None when zero), max_fee, min_finality}, or None."""
    data = (data or "").lower()
    found = CALLS.get(data[:10])
    if found is None:
        return None
    name, version = found
    w = _words("0x" + data[10:])
    try:
        out = {
            "function": name,
            "version": version,
            "amount": w[0],
            "dest_domain": w[1],
            "recipient": _shown(w[2], w[1]),
            "burn_token": _address(w[3]),
            "destination_caller": None,
            "max_fee": None,
            "min_finality": None,
        }
        if name == "depositForBurnWithCaller" or version == 2:
            out["destination_caller"] = "0x{:064x}".format(w[4]) if w[4] else None
        if version == 2:
            out["max_fee"], out["min_finality"] = w[5], w[6]
    except IndexError:
        return None
    return out


def decode_deposits(receipt, chain_id, table) -> list[dict]:
    """``DepositForBurn`` logs of TokenMessengers the table lists on ``chain_id``.

    [{version, messenger, nonce (v1; None for v2), burn_token, amount, depositor,
    recipient_word, recipient (address on an EVM domain, base58 on Solana, else hex),
    dest_domain, dest_chain (name), dest_chain_id (EVM chain id or None),
    dest_messenger (address or None), destination_caller, max_fee, min_finality,
    log_index}]
    """
    if not _ok(receipt):
        return []
    out = []
    for log in receipt.get("logs") or []:
        emitter = (log.get("address") or "").lower()
        topics = [t.lower() for t in log.get("topics") or []]
        if not topics or not _is(table, chain_id, emitter, "messenger"):
            continue
        w = _words(log.get("data"))
        if topics[0] == TOPIC_DEPOSIT_V1 and len(topics) >= 4 and len(w) >= 5:
            version, nonce = 1, int(topics[1], 16)
            burn_token, depositor = _address(topics[2]), _address(topics[3])
            max_fee = min_finality = None
        elif topics[0] == TOPIC_DEPOSIT_V2 and len(topics) >= 4 and len(w) >= 6:
            version, nonce = 2, None
            burn_token, depositor = _address(topics[1]), _address(topics[2])
            max_fee, min_finality = w[5], int(topics[3], 16)
        else:
            continue
        domain = w[2]
        name, dest_chain = domain_chain(domain)
        out.append(
            {
                "version": version,
                "messenger": emitter,
                "nonce": nonce,
                "burn_token": burn_token,
                "amount": w[0],
                "depositor": depositor,
                "recipient_word": w[1],
                "recipient": _shown(w[1], domain),
                "dest_domain": domain,
                "dest_chain": name,
                "dest_chain_id": dest_chain,
                "dest_messenger": _address(w[3]) if dest_chain is not None else None,
                "destination_caller": "0x{:064x}".format(w[4]) if w[4] else None,
                "max_fee": max_fee,
                "min_finality": min_finality,
                "log_index": _log_index(log),
            }
        )
    return out


def _body(raw: bytes, version: int) -> dict | None:
    """Fields of a burn message body (version 4 bytes, then 32-byte fields)."""
    need = 196 if version == 2 else 132
    if raw is None or len(raw) < need:
        return None

    def word(at):
        return int.from_bytes(raw[at : at + 32], "big")

    out = {
        "burn_token": _address(word(4)),
        "recipient_word": word(36),
        "amount": word(68),
        "message_sender": _address(word(100)),
        "max_fee": None,
        "fee_executed": 0,
    }
    if version == 2:
        out["max_fee"], out["fee_executed"] = word(132), word(164)
    return out


def decode_received(receipt, chain_id, table) -> list[dict]:
    """``MessageReceived`` logs of MessageTransmitters the table lists on ``chain_id``,
    with the burn message decoded: [{version, transmitter, source_domain, nonce (int for
    v1, hex for v2), burn_token, recipient_word, amount, message_sender, max_fee,
    fee_executed, log_index}]."""
    if not _ok(receipt):
        return []
    out = []
    for log in receipt.get("logs") or []:
        emitter = (log.get("address") or "").lower()
        topics = [t.lower() for t in log.get("topics") or []]
        if not topics or not _is(table, chain_id, emitter, "transmitter"):
            continue
        if topics[0] == TOPIC_RECEIVED_V1 and len(topics) >= 3:
            version, nonce = 1, int(topics[2], 16)
        elif topics[0] == TOPIC_RECEIVED_V2 and len(topics) >= 3:
            version, nonce = 2, topics[2]
        else:
            continue
        raw = (log.get("data") or "")[2:]
        w = _words(log.get("data"))
        body = _body(_bytes_at(raw, 2), version) if len(w) >= 3 else None
        if body is None:
            continue
        out.append(
            dict(
                body,
                version=version,
                transmitter=emitter,
                source_domain=w[0],
                nonce=nonce,
                log_index=_log_index(log),
            )
        )
    return out


def _mint(log, version):
    topics = [t.lower() for t in log.get("topics") or []]
    w = _words(log.get("data"))
    if len(topics) < 3 or topics[0] != MINT_TOPICS[version] or not w:
        return None
    if version == 2 and len(w) < 2:
        return None
    return {
        "version": version,
        "recipient": _address(topics[1]),
        "amount": w[0],
        "token": _address(topics[2]),
        "fee": w[1] if version == 2 else 0,
        "log_index": _log_index(log),
    }


def decode_mints(receipt, chain_id, table) -> list[dict]:
    """``MintAndWithdraw`` logs of TokenMessengers the table lists on ``chain_id``:
    [{version, recipient, amount (minted), token, fee (v2 feeCollected), log_index}]."""
    if not _ok(receipt):
        return []
    out = []
    for log in receipt.get("logs") or []:
        emitter = (log.get("address") or "").lower()
        if not _is(table, chain_id, emitter, "messenger"):
            continue
        for version in (1, 2):
            found = _mint(log, version)
            if found is not None:
                out.append(found)
    return out


def messenger_listed(table, chain_id, address, version) -> bool:
    """True when the table lists ``address`` as a TokenMessenger of ``version`` there."""
    return _is(table, chain_id, address, "messenger", version)


def _same_message(msg, deposit, source_domain):
    if msg["version"] != deposit["version"] or msg["source_domain"] != source_domain:
        return False
    if deposit["version"] == 1 and msg["nonce"] != deposit["nonce"]:
        return False
    return (
        msg["burn_token"] == deposit["burn_token"]
        and msg["recipient_word"] == deposit["recipient_word"]
        and msg["amount"] == deposit["amount"]
        and msg["message_sender"] == deposit["depositor"]
    )


def find_mint(client, deposit, source_domain, ts, table, now=None, claimed=()) -> dict | None:
    """The destination mint of a CCTP ``deposit``, or None.

    Reads the recipient's ``MintAndWithdraw`` logs of the deposit's destination
    messenger from ``ts`` (less a minute) up to MAX_FILL_WAIT later (never past now).
    A log counts when minted amount plus fee equals the deposit (the fee at most the
    deposit's maxFee), it is not in ``claimed`` ((chain, tx, log index) of mints given to
    earlier deposits), and its transaction holds a listed transmitter's
    ``MessageReceived`` from ``source_domain`` whose burn message is the deposit's (the
    nonce too for v1; for v2 the fee executed equals the fee collected). The earliest
    such log is taken. Returns {chain_id, tx_hash, block, ts, recipient, amount, token,
    fee, log_index}. Explorer errors propagate.
    """
    now = int(time.time()) if now is None else now
    dest = deposit["dest_chain_id"]
    version = deposit["version"]
    recipient = _address(deposit["recipient_word"])
    if dest is None or recipient is None or deposit.get("dest_messenger") is None:
        return None
    start = client.block_by_time(ts - 60, "before")
    end = _end_block(client, max(ts, min(ts + MAX_FILL_WAIT, now)), now)
    topics = {1: "0x{:064x}".format(int(recipient, 16))}
    logs = client.get_logs(deposit["dest_messenger"], MINT_TOPICS[version], start, end, topics)

    def order(log):
        try:
            return (int(log["blockNumber"], 16), int(log["logIndex"], 16))
        except (KeyError, TypeError, ValueError):
            return (0, 0)

    for log in sorted(logs, key=order):
        mint = _mint(log, version)
        tx_hash = (log.get("transactionHash") or "").lower()
        if mint is None or mint["recipient"] != recipient:
            continue
        if (dest, tx_hash, mint["log_index"]) in claimed:
            continue
        if mint["amount"] + mint["fee"] != deposit["amount"]:
            continue
        if version == 2 and mint["fee"] > (deposit.get("max_fee") or 0):
            continue
        receipt = client.tx_receipt(tx_hash)
        msgs = [
            m
            for m in decode_received(receipt, dest, table)
            if _same_message(m, deposit, source_domain) and m["fee_executed"] == mint["fee"]
        ]
        if not msgs:
            continue
        block, _index = order(log)
        try:
            when = int(log["timeStamp"], 16)
        except (KeyError, TypeError, ValueError):
            when = None
        return dict(mint, chain_id=dest, tx_hash=tx_hash, block=block, ts=when)
    return None
