"""Bridge contracts the trace can recognise, and (Part B) bridge deposit decoding.

``data/bridges.csv`` lists bridge entry points by chain and address, each with
the public page it was taken from. A contract is classified by address only; an
address that is not in the table is "other", never guessed.
"""

from __future__ import annotations

import csv
import os
import time

from .config import PACKAGE_DATA
from .errors import BlockLookupError

BRIDGES_CSV = os.path.join(PACKAGE_DATA, "bridges.csv")
COLUMNS = ("chain_id", "address", "protocol", "role", "kind", "source_url")
KINDS = ("contract", "maker_eoa", "aggregator")


def load_bridges(path: str | None = None) -> dict[tuple[int, str], dict]:
    """Return {(chain_id, address): row} from the bridge table.

    A row without a source URL, or with a malformed address, is rejected: the
    table is only as good as the page each address came from.
    """
    table = {}
    with open(path or BRIDGES_CSV, newline="", encoding="utf-8") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            addr = (row.get("address") or "").strip().lower()
            url = (row.get("source_url") or "").strip()
            if len(addr) != 42 or not addr.startswith("0x") or not url:
                raise ValueError(f"{path or BRIDGES_CSV} line {n}: bad address or no source_url")
            kind = (row.get("kind") or "contract").strip()
            if kind not in KINDS:
                raise ValueError(f"{path or BRIDGES_CSV} line {n}: unknown kind {kind!r}")
            table[(int(row["chain_id"]), addr)] = {
                "chain_id": int(row["chain_id"]),
                "address": addr,
                "protocol": row["protocol"].strip(),
                "role": row["role"].strip(),
                "kind": kind,
                "source_url": url,
            }
    return table


def classify(table: dict, chain_id: int, address: str) -> str:
    """Protocol name for a bridge address, or "other"."""
    row = table.get((int(chain_id), (address or "").lower()))
    return row["protocol"] if row else "other"


def listed(table: dict, chain_id, address: str) -> dict | None:
    """The table row of a bridge address, or None (also for an unknown chain)."""
    if chain_id is None:
        return None
    return table.get((int(chain_id), (address or "").lower()))


# Calls that bring funds IN from the other chain (4byte.directory signatures). A
# call to a bridge with one of these selectors is an arrival, not an exit.
INBOUND_SELECTORS = {
    "0x3805550f": "exit(bytes)",  # Polygon PoS RootChainManager: burn proof from Polygon
    "0xc6878519": "completeTransfer(bytes)",  # Wormhole Token Bridge redeem
    "0xff200cde": "completeTransferAndUnwrapETH(bytes)",  # Wormhole Token Bridge redeem
}


def is_inbound(selector: str) -> bool:
    return (selector or "").lower() in INBOUND_SELECTORS


# ---------------------------------------------------------------------------
# Across (SpokePool): deposit event on the origin chain, fill event on the
# destination chain, joined by (origin chain id, deposit id).
#
# Written from the public event ABI of the verified SpokePool implementation
# (https://etherscan.io/address/0x456ac26e5ec083ee9889eba0d1a0a582502b8e84#code).
# No code of the Across contracts repository is copied (it is BUSL-licensed).
#
# Deposit, non-indexed data words (both event versions):
#   0 inputToken, 1 outputToken, 2 inputAmount, 3 outputAmount, 4 quoteTimestamp,
#   5 fillDeadline, 6 exclusivityDeadline, 7 recipient, 8 exclusiveRelayer, 9 message
#   topics: [event, destinationChainId, depositId, depositor]
# Fill, non-indexed data words:
#   0 inputToken, 1 outputToken, 2 inputAmount, 3 outputAmount, 4 repaymentChainId,
#   5 fillDeadline, 6 exclusivityDeadline, 7 exclusiveRelayer, 8 depositor,
#   9 recipient, ...        topics: [event, originChainId, depositId, relayer]
# Tornado-era v2 deposits (2022 to early 2024) use other events and are not decoded.
# V3 events carry addresses; the current events carry bytes32 (left-padded
# addresses on EVM chains, anything else on others).
# ---------------------------------------------------------------------------

ACROSS = "across"
TOPIC_ACROSS_DEPOSIT_V3 = "0xa123dc29aebf7d0c3322c8eeb5b999e859f39937950ed31056532713d0de396f"
TOPIC_ACROSS_DEPOSIT = "0x32ed1a409ef04c7b0227189c3a103dc5ac10e775a15b785dcc510201f7c25ad3"
TOPIC_ACROSS_FILL_V3 = "0x571749edf1d5c9599318cdbc4e28a6475d65e87fd3b2ddbe1e9a8d5e7a0f0ff7"
TOPIC_ACROSS_FILL = "0x44b559f101f8fbcc8a0ea43fa91a05a729a5ea6e14a7c75aa750374690137208"
DEPOSIT_EVENTS = {
    TOPIC_ACROSS_DEPOSIT_V3: "V3FundsDeposited",
    TOPIC_ACROSS_DEPOSIT: "FundsDeposited",
}
HEAD_MARGIN = 120  # seconds: an end this close to now has no "after" block yet
MAX_FILL_WAIT = 7 * 86400  # a fill later than this after the deposit is not searched for

# Wrapped native token per chain: an Across fill of it is paid out as the native coin
# on the chains where it wraps the native coin (not on Polygon, whose native coin is POL
# and whose WETH is an ordinary token).
WRAPPED_NATIVE = {
    1: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    10: "0x4200000000000000000000000000000000000006",
    8453: "0x4200000000000000000000000000000000000006",
    42161: "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
    137: "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WPOL
}

SUPPORTED = {ACROSS}  # protocols whose deposits are decoded and followed

# State of a bridge hop (edge["bridge"]["state"]). Every state but "filled" ends the
# branch; the five Across lookup outcomes are also the branch's end reason.
FILLED = "filled"
NOT_FOLLOWED = "not_followed"  # a listed bridge without a decoder: named, not followed
NOT_DECODED = "deposit_not_decoded"  # a decoded protocol, but no deposit of this sender
NOT_TIED = "deposit_not_tied"  # an Across deposit in the transaction, not this transfer
FILL_NOT_FOUND = "fill_not_found"
DEST_CHAIN_NOT_SUPPORTED = "destination_chain_not_supported"
DEST_POOL_NOT_LISTED = "destination_pool_not_listed"
LOOKUP_FAILED = "lookup_failed"
DEPOSIT_EXPIRED = "deposit_expired_possible_refund"
TOKEN_WRAPPED = {137}  # chains whose wrapped coin is an ordinary token (Polygon: WPOL)


def _words(data):
    raw = (data or "")[2:]
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw) - len(raw) % 64, 64)]


def _address(word):
    """Address in a word or topic (int or hex string); None for a non-EVM bytes32."""
    value = int(word, 16) if isinstance(word, str) else word
    return "0x{:040x}".format(value) if value < 1 << 160 else None


def decode_across_deposits(logs, origin_chain_id=1, table=None):
    """Across deposits among the log rows of one transaction (receipt or getLogs rows).

    Returns [{protocol, event, origin_chain_id, spoke, deposit_id, dest_chain_id,
    depositor, recipient, input_token, output_token, input_amount, output_amount,
    quote_ts, fill_deadline, log_index}]; amounts are raw integers. A log counts
    only when the table lists its emitter as an Across contract on that chain.
    """
    table = load_bridges() if table is None else table
    out = []
    for log in logs:
        topics = [t.lower() for t in log.get("topics") or []]
        if len(topics) < 4 or topics[0] not in DEPOSIT_EVENTS:
            continue
        spoke = (log.get("address") or "").lower()
        if classify(table, origin_chain_id, spoke) != ACROSS:
            continue
        w = _words(log.get("data"))
        if len(w) < 9:
            continue
        out.append(
            {
                "protocol": ACROSS,
                "event": DEPOSIT_EVENTS[topics[0]],
                "origin_chain_id": int(origin_chain_id),
                "spoke": spoke,
                "tx_hash": (log.get("transactionHash") or "").lower(),
                "log_index": int(log["logIndex"], 16) if log.get("logIndex") else None,
                "deposit_id": int(topics[2], 16),
                "dest_chain_id": int(topics[1], 16),
                "depositor": _address(topics[3]),
                "recipient": _address(w[7]),
                "input_token": _address(w[0]),
                "output_token": _address(w[1]),
                "input_amount": w[2],
                "output_amount": w[3],
                "quote_ts": w[4],
                "fill_deadline": w[5],
            }
        )
    return out


def decode_deposits(client, tx_hash, origin_chain_id=1, table=None, ts=None):
    """Bridge deposits made in transaction ``tx_hash`` (receipt read through ``client``).

    ``ts`` is the transaction's timestamp; it bounds the later fill search. Each
    deposit also carries ``tx_from``, the sender of the transaction (the receipt's
    ``from``): through a periphery or router contract it can differ from the depositor.
    """
    return decode_receipt_deposits(client.tx_receipt(tx_hash), tx_hash, origin_chain_id, table, ts)


def decode_receipt_deposits(receipt, tx_hash, origin_chain_id=1, table=None, ts=None):
    """decode_deposits over a receipt already read (None or a failed one: no deposits)."""
    if not receipt or receipt.get("status") == "0x0":
        return []
    logs = [dict(log, transactionHash=tx_hash) for log in receipt.get("logs") or []]
    deposits = decode_across_deposits(logs, origin_chain_id, table)
    sender = (receipt.get("from") or "").lower() or None
    for d in deposits:
        d["ts"] = ts
        d["tx_from"] = sender
    return deposits


def bridge_emitters(receipt, chain_id, table, exclude=()):
    """Bridge contracts of the table (not aggregators) that emitted a log in ``receipt``.

    ``exclude`` lists addresses not to count (the contract the transfer went to).
    """
    skip = {a.lower() for a in exclude}
    found = set()
    for log in (receipt or {}).get("logs") or []:
        addr = (log.get("address") or "").lower()
        row = listed(table, chain_id, addr)
        if row is not None and row["kind"] != "aggregator" and addr not in skip:
            found.add(addr)
    return found


def destination_pools(table, chain_id):
    """Across SpokePools listed on ``chain_id`` (where fills are emitted), sorted.

    Rows whose role starts with "SpokePool (": not the v2 pool, not the periphery.
    """
    return sorted(
        r["address"]
        for (c, _a), r in table.items()
        if c == int(chain_id) and r["protocol"] == ACROSS and r["role"].startswith("SpokePool (")
    )


def no_fill_state(deposit, now=None):
    """End state of a deposit whose fill was not found.

    DEPOSIT_EXPIRED when its fill deadline has passed and lay inside the search window
    (an unfilled Across deposit is refunded after its deadline: the funds may be back
    with the depositor), FILL_NOT_FOUND otherwise.
    """
    now = time.time() if now is None else now
    ts, deadline = deposit.get("ts"), int(deposit.get("fill_deadline") or 0)
    if ts and deadline and deadline < now and deadline <= ts + MAX_FILL_WAIT:
        return DEPOSIT_EXPIRED
    return FILL_NOT_FOUND


def _execution_info(topic0, w):
    """(updatedRecipient, updatedOutputAmount) of a fill's relayExecutionInfo, or (None, None).

    FilledRelay carries the tuple inline (words 11 and 13); FilledV3Relay's tuple holds
    a bytes field, so word 11 is the offset of the tuple (words +0 and +2 from there).
    """
    try:
        if topic0 == TOPIC_ACROSS_FILL:
            return w[11], w[13]
        start = w[11] // 32
        return w[start], w[start + 2]
    except IndexError:
        return None, None


def _same_deposit(w, deposit):
    """The fill repeats the deposit's own terms: tokens, amounts, recipient, depositor."""
    if _address(w[8]) != deposit["depositor"] or _address(w[9]) != deposit["recipient"]:
        return False
    if _address(w[0]) != deposit["input_token"] or w[2] != deposit["input_amount"]:
        return False
    # an all-zero output token on the deposit means "the destination's equivalent token"
    out = deposit["output_token"]
    if out is not None and int(out, 16) != 0 and _address(w[1]) != out:
        return False
    return w[3] == deposit["output_amount"]


def _end_block(client, end_ts, now):
    """Last block to search: the block after ``end_ts``, or the head when that is not mined yet."""
    if end_ts >= now - HEAD_MARGIN:
        return client.current_block()
    try:
        return client.block_by_time(end_ts, "after")
    except BlockLookupError:
        return client.current_block()


def find_fill(dest_client, deposit, table=None):
    """The destination-chain fill of an Across ``deposit``, or None.

    Reads FilledRelay and FilledV3Relay logs of the destination SpokePool (from the
    table) whose origin chain and deposit id are the deposit's, between the deposit
    and its fill deadline (at most MAX_FILL_WAIT later, never past now). A log counts
    only when its original relay data (tokens, amounts, recipient, depositor) equal
    the deposit's. A sped-up fill is followed to its updated recipient and amount.

    Returns {chain_id, tx_hash, block, ts, recipient, output_amount, original_recipient,
    original_output_amount, sped_up, depositor, relayer, input_token, output_token,
    input_amount} or None when the explorer shows no such fill. Explorer errors
    propagate (TornadoDemixError).
    """
    table = load_bridges() if table is None else table
    dest = int(deposit["dest_chain_id"])
    pools = destination_pools(table, dest)
    if not pools:
        return None
    topics = {
        1: "0x{:064x}".format(deposit["origin_chain_id"]),
        2: "0x{:064x}".format(deposit["deposit_id"]),
    }
    ts = deposit.get("ts")
    if ts:
        now = int(time.time())
        deadline = int(deposit.get("fill_deadline") or 0) or ts + MAX_FILL_WAIT
        end_ts = min(max(ts, min(deadline, ts + MAX_FILL_WAIT)) + 60, now)
        start = dest_client.block_by_time(ts - 60, "before")
        end = _end_block(dest_client, max(end_ts, ts), now)
    else:
        start, end = 0, 999_999_999_999
    for pool in pools:
        for topic0 in (TOPIC_ACROSS_FILL, TOPIC_ACROSS_FILL_V3):
            for log in dest_client.get_logs(pool, topic0, start, end, topics=topics):
                w = _words(log.get("data"))
                if len(w) < 14 or not _same_deposit(w, deposit):
                    continue
                new_recipient, new_amount = _execution_info(topic0, w)
                recipient = _address(new_recipient) if new_recipient else None
                recipient = recipient or deposit["recipient"]
                amount = new_amount or w[3]
                return {
                    "chain_id": dest,
                    "tx_hash": (log.get("transactionHash") or "").lower(),
                    "block": int(log["blockNumber"], 16),
                    "ts": int(log["timeStamp"], 16) if log.get("timeStamp") else None,
                    "recipient": recipient,
                    "output_amount": amount,
                    "original_recipient": _address(w[9]),
                    "original_output_amount": w[3],
                    "sped_up": recipient != _address(w[9]) or amount != w[3],
                    "depositor": _address(w[8]),
                    "relayer": _address(log["topics"][3]) if len(log["topics"]) > 3 else None,
                    "input_token": _address(w[0]),
                    "output_token": _address(w[1]),
                    "input_amount": w[2],
                }
    return None


class Destinations:
    """Readers for the chains a bridge trace may continue on, built on first use.

    ``destinations(chain_id)`` returns {"client", "labels", "currency",
    "is_contract"} for a chain of the networks registry, or None. ``network(chain_id)``
    returns the registry entry (explorer links, name) or None.
    """

    def __init__(self, api_key, networks_csv=None, attribution_dir=None):
        from .networks import load_networks

        self.api_key = api_key
        self.attribution_dir = attribution_dir
        self._by_chain = {}
        for net in load_networks(networks_csv).values():
            self._by_chain.setdefault(net.chain_id, net)
        self._cache = {}

    def network(self, chain_id):
        return self._by_chain.get(chain_id)

    def __call__(self, chain_id):
        net = self._by_chain.get(chain_id)
        if net is None:
            return None
        if chain_id not in self._cache:
            from .attribution import load_attribution
            from .etherscan import EtherscanClient
            from .rpc import make_contract_check

            self._cache[chain_id] = {
                "client": EtherscanClient(self.api_key, **net.client_kwargs()),
                "labels": load_attribution(net.name, self.attribution_dir),
                "currency": net.currency,
                "is_contract": make_contract_check(net.rpc_url),
            }
        return self._cache[chain_id]
