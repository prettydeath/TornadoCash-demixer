"""Decoding of Tornado.Cash pool events (exact, log-based extraction).

Reading contract logs beats inferring withdrawals from raw ETH transfers: the
``Withdrawal`` event carries the exact recipient, the relayer and the fee, so no
fee-window heuristic is needed.

Event ABIs::

    event Deposit(bytes32 indexed commitment, uint32 leafIndex, uint256 timestamp)
    event Withdrawal(address to, bytes32 nullifierHash, address indexed relayer,
                     uint256 fee)

For ``Withdrawal`` the only indexed parameter is ``relayer`` (topics[1]); ``to``,
``nullifierHash`` and ``fee`` are packed into the 96-byte data field.
"""

from __future__ import annotations

from .constants import TOPIC_DEPOSIT, TOPIC_WITHDRAWAL, ZERO_ADDRESS
from .etherscan import EtherscanClient


def _word(data_hex, index):
    """Return the 32-byte word at position ``index`` of a hex data blob."""
    start = index * 64
    return data_hex[start : start + 64]


def _addr_from_word(word):
    return "0x" + word[24:]


def _addr_from_topic(topic):
    return "0x" + topic[-40:]


def decode_withdrawal(log: dict) -> dict:
    """Decode one Withdrawal log into a dict.

    Returns: to, nullifier, relayer, fee_wei, self_relayed, gas_price,
    tx_hash, block, ts.

    ``fee_wei`` stays in the pool asset's base units: only the caller knows the
    pool's decimals (``Pool.to_units``).
    """
    data = (log.get("data") or "0x")[2:]
    topics = log.get("topics") or []
    to = _addr_from_word(_word(data, 0)).lower()
    nullifier = "0x" + _word(data, 1)
    fee_wei = int(_word(data, 2) or "0", 16)
    relayer = _addr_from_topic(topics[1]).lower() if len(topics) > 1 else ZERO_ADDRESS

    # No relayer named in the proof: whoever sent the transaction paid its gas
    # from an address they already controlled. `fee == 0` alone is not enough,
    # because a named relayer may take a zero fee and still pay the gas; that case
    # is zero_fee_relayed. Who actually broadcast is checked later by
    # relayer.verify_broadcaster.
    self_relayed = relayer == ZERO_ADDRESS
    zero_fee_relayed = relayer != ZERO_ADDRESS and fee_wei == 0

    return {
        "to": to,
        "nullifier": nullifier,
        "relayer": relayer,
        "fee_wei": fee_wei,
        "self_relayed": self_relayed,
        "zero_fee_relayed": zero_fee_relayed,
        # Filled in by relayer.verify_broadcaster.
        "broadcaster": None,
        "broadcaster_status": "unverified",
        # The logs endpoint returns gasPrice, so no extra tx fetch is needed.
        "gas_price": int(log.get("gasPrice", "0x0"), 16),
        "tx_hash": log.get("transactionHash"),
        "block": int(log.get("blockNumber", "0x0"), 16),
        "ts": int(log.get("timeStamp", "0x0"), 16),
    }


def decode_deposit(log: dict) -> dict:
    """Decode one Deposit log into a dict (commitment, leaf index, timestamp)."""
    data = (log.get("data") or "0x")[2:]
    topics = log.get("topics") or []
    commitment = topics[1] if len(topics) > 1 else None
    leaf_index = int(_word(data, 0) or "0", 16)
    ts_field = int(_word(data, 1) or "0", 16)
    return {
        "commitment": commitment,
        "leaf_index": leaf_index,
        "timestamp": ts_field,
        "tx_hash": log.get("transactionHash"),
        "block": int(log.get("blockNumber", "0x0"), 16),
        "ts": int(log.get("timeStamp", "0x0"), 16),
    }


def fetch_withdrawals(
    client: EtherscanClient, pool: str, start_block: int, end_block: int
) -> list[dict]:
    """Return decoded Withdrawal events for a pool within a block range."""
    logs = client.get_logs(pool, TOPIC_WITHDRAWAL, start_block, end_block)
    return [decode_withdrawal(log) for log in logs]


def fetch_deposits(
    client: EtherscanClient, pool: str, start_block: int, end_block: int
) -> list[dict]:
    """Return decoded Deposit events for a pool within a block range."""
    logs = client.get_logs(pool, TOPIC_DEPOSIT, start_block, end_block)
    return [decode_deposit(log) for log in logs]
