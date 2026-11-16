"""Bridges seen in real Tornado exits that can be recognised and decoded, but not
followed on-chain: Relay, Mayan, THORChain, Maya, Layerswap, NEAR Intents, Unit and the
Hyperliquid bridge on Arbitrum.

Pure decoders over receipt logs and calldata. Written from the public ABIs of the
verified contracts on Etherscan (RelayDepository, RelayReceiver, MayanForwarder2,
SwiftSource, FastMCTP, MayanCircle, MonoChainSwap, THORChain_Router, MAYAChain_Router);
no contract source is copied. Selectors and topics are keccak-256 of the signature
strings; tests/test_bridges_named.py re-derives every one. Contract and maker
addresses are in data/bridges.csv, each with the page it was taken from.

Why none of them is followed to the destination (each ends the branch with its own
state, see ``STATES``):

* Relay. ``RelayDepository.depositNative(depositor, id)`` / ``depositErc20`` log
  ``RelayNativeDeposit(from, amount, id)`` / ``RelayErc20Deposit(from, token, amount,
  id)``; a payment to a ``RelayReceiver`` logs ``FundsForwardedWithData(data)``. The
  request id is all the chain shows: the destination chain and recipient live in the
  off-chain quote, so the fill cannot be joined on-chain (``destination_not_on_chain``).
* Mayan. ``MayanForwarder2`` logs what it forwarded (``ForwardedEth``, ``ForwardedERC20``,
  ``SwapAndForwardedEth``, ``SwapAndForwardedERC20``) with the Mayan contract and its
  call. The call gives the destination chain (Wormhole chain id for Swift and MayanCircle
  orders, Circle CCTP domain for FastMCTP and MayanCircle bridges) and the recipient.
  The destination fulfil is keyed by an order hash or a CCTP nonce that is not computed
  here, so it is not identified (``fulfil_not_followed``). ``MonoChainSwap`` pays out on
  the same chain (``EthTransferred`` / ``TokenTransferred`` in the same receipt): that is
  followed like a swap whose output goes to the call's ``to``. The Mayan contract a
  forward names is not looked up in the table: its call is decoded by selector, which is
  safe because MayanForwarder2 only forwards to protocols it whitelists
  (``mayanProtocols``), so a forward cannot reach an arbitrary contract.
* THORChain and Maya. The router's ``Deposit(to, asset, amount, memo)`` carries the memo
  (``SWAP:CHAIN.ASSET:DEST:LIMIT...``, short forms ``=`` and ``s``). The outbound is a
  payment from a vault that is not tied on-chain to the memo (``vault_payout_not_followed``).
* Layerswap, NEAR Intents, Unit. Deposits are plain transfers to a maker or treasury
  address; the swap they belong to is matched off chain (``off_chain_matching``).
* Hyperliquid Bridge2 (Arbitrum). Native USDC sent to the bridge is credited to the
  sender's account on HyperCore, which is not an EVM chain (``credited_off_evm``).
"""

from __future__ import annotations

from .bridges import _address, _words, listed

RELAY = "relay"
MAYAN = "mayan"
THORCHAIN = "thorchain"
MAYA = "maya"
LAYERSWAP = "layerswap"
NEAR_INTENTS = "near-intents"
UNIT = "unit"
HYPERLIQUID = "hyperliquid"
PROTOCOLS = frozenset({RELAY, MAYAN, THORCHAIN, MAYA, LAYERSWAP, NEAR_INTENTS, UNIT, HYPERLIQUID})
# deposits that are plain transfers to a listed address (no event to decode)
MAKER_PROTOCOLS = frozenset({LAYERSWAP, NEAR_INTENTS, UNIT})

# End states (edge["bridge"]["state"]) of these bridges
DEST_NOT_ON_CHAIN = "destination_not_on_chain"
FULFIL_NOT_FOLLOWED = "fulfil_not_followed"
VAULT_PAYOUT = "vault_payout_not_followed"
OFF_CHAIN_MATCHING = "off_chain_matching"
CREDITED_OFF_EVM = "credited_off_evm"
ORDER_NOT_DECODED = "order_not_decoded"
SAME_CHAIN = "same_chain_payout"  # Mayan MonoChainSwap: followed on the same chain
PAYOUT_NOT_FOUND = "payout_not_found"  # a MonoChainSwap call whose payout was not read
STATES = {
    DEST_NOT_ON_CHAIN: "the request id is on-chain, the destination and recipient are not",
    FULFIL_NOT_FOLLOWED: "the destination fulfil is keyed by an order hash or CCTP nonce "
    "that is not computed here",
    VAULT_PAYOUT: "the outbound is a vault payout not tied on-chain to the memo",
    OFF_CHAIN_MATCHING: "a plain transfer to a maker or treasury, matched off chain",
    CREDITED_OFF_EVM: "credited to the sender on HyperCore (not an EVM chain)",
    ORDER_NOT_DECODED: "the forwarded call is not one of the decoded Mayan calls",
    PAYOUT_NOT_FOUND: "a same-chain Mayan call whose payout is not in the receipt or "
    "could not be read",
}

# --- Relay ------------------------------------------------------------------------------
TOPIC_RELAY_NATIVE = "0x8032066556caf3967d8fec4ad22a2d9e1e9576556b2903a0fcd5b1fd201e3477"
TOPIC_RELAY_ERC20 = "0x49fed1d0b752ce30eee63c7a81133f3363b532fec5d4d7dd1ccfd005de4555e1"
TOPIC_RELAY_FORWARDED = "0x936c2ca3b35d2d0b24057b0675c459e4515f48fe132d138e213ae59ffab7f53e"

# --- THORChain / Maya router ------------------------------------------------------------
TOPIC_ROUTER_DEPOSIT = "0xef519b7eb82aaf6ac376a6df2d793843ebfd593de5f1a0601d3cc6ab49ebb395"
ROUTER_PROTOCOLS = (THORCHAIN, MAYA)

# --- Mayan ------------------------------------------------------------------------------
TOPIC_MAYAN_FORWARDED_ETH = "0xb8543d214cab9591941648db8d40126a163bfd0db4a865678320b921e1398043"
TOPIC_MAYAN_FORWARDED_ERC20 = "0xbf150db6b4a14b084f7346b4bc300f552ce867afe55be27bce2d6b37e3307cda"
TOPIC_MAYAN_SWAP_ETH = "0x7cbff921ae1f3ea71284120d2aabde13587df067f2bb5c831ea6e35d7a9242ac"
TOPIC_MAYAN_SWAP_ERC20 = "0x23278f58875126c795a4072b98b5851fe9b21cea19895b02a6224fefbb1e3298"
TOPIC_MONO_ETH = "0xcec1f18c3ab8ddaaa107a1591e3c369667eec613626611a8deaedef43069fcdd"
TOPIC_MONO_TOKEN = "0x9c8515990fd8c61431c4ac8db9b81475f90c292a1dda77731e56c22e64fc7643"

# Wormhole chain ids (wormhole-foundation/wormhole sdk/vaa/structs.go):
# id -> (name, EVM chain id or None)
WORMHOLE_CHAINS = {
    1: ("Solana", None),
    2: ("Ethereum", 1),
    4: ("BNB Chain", 56),
    5: ("Polygon", 137),
    6: ("Avalanche", 43114),
    21: ("Sui", None),
    22: ("Aptos", None),
    23: ("Arbitrum", 42161),
    24: ("Optimism", 10),
    30: ("Base", 8453),
    38: ("Linea", 59144),
    44: ("Unichain", 130),
    47: ("HyperEVM", 999),
    48: ("Monad", 143),
}
# Circle CCTP domains (developers.circle.com/cctp/cctp-supported-blockchains). A domain
# without an EVM chain id here is shown by name only and its recipient as bytes32 hex
# (base58 on Solana): Noble, Sui, Aptos, Starknet and Stellar are not EVM chains, and the
# chain ids of the other newer domains are not taken from a Circle page.
CCTP_DOMAINS = {
    0: ("Ethereum", 1),
    1: ("Avalanche", 43114),
    2: ("Optimism", 10),
    3: ("Arbitrum", 42161),
    4: ("Noble", None),
    5: ("Solana", None),
    6: ("Base", 8453),
    7: ("Polygon", 137),
    8: ("Sui", None),
    9: ("Aptos", None),
    10: ("Unichain", 130),
    11: ("Linea", 59144),
    12: ("Codex", None),
    13: ("Sonic", None),
    14: ("World Chain", None),
    15: ("Monad", 143),
    16: ("Sei", None),
    17: ("BNB Chain", 56),
    18: ("XDC", None),
    19: ("HyperEVM", 999),
    21: ("Ink", None),
    22: ("Plume", None),
    25: ("Starknet", None),
    26: ("Arc", None),
    27: ("Stellar", None),
    28: ("EDGE", None),
    29: ("Injective", None),
    30: ("Morph", None),
    31: ("Pharos", None),
    32: ("Cronos", None),
    33: ("Plasma", None),
    37: ("X Layer", None),
}

# Calls a Mayan forwarder passes on: selector -> (function, decoder name). Word layouts
# from the verified ABIs (every argument before the dynamic ``bytes`` is static, so the
# order tuples sit inline).
MAYAN_CALLS = {
    "0xa3a30834": "createOrderWithToken",  # SwiftSource (Wormhole destination)
    "0x2337e236": "createOrder",  # FastMCTP (CCTP domain)
    "0xf58b6de8": "bridge",  # FastMCTP (CCTP domain)
    "0x2072197f": "bridgeWithFee",  # MayanCircle (CCTP domain)
    "0x9be95bb4": "bridgeWithLockedFee",  # MayanCircle (CCTP domain)
    "0x1c59b7fc": "createOrder",  # MayanCircle (Wormhole destination)
    "0x6eb183d0": "transferEth",  # MonoChainSwap (same chain)
    "0x29d8eb0a": "transferToken",  # MonoChainSwap (same chain)
}

# --- Hyperliquid Bridge2 (Arbitrum) ----------------------------------------------------
HYPERLIQUID_CHAIN = 42161
HYPERLIQUID_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"  # native USDC on Arbitrum
HYPERLIQUID_MIN_RAW = 5 * 10**6  # less than 5 USDC is not credited (Hyperliquid docs)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58(raw: bytes) -> str:
    """Base58 (Bitcoin alphabet) of ``raw``: how Solana writes a 32-byte address."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\0"))
    return "1" * pad + out


def _log_index(log):
    try:
        return int(log["logIndex"], 16)
    except (KeyError, TypeError, ValueError):
        return None


def _bytes_at(raw_hex: str, offset_word: int) -> bytes | None:
    """The ``bytes`` / ``string`` value whose offset is in word ``offset_word`` of the data."""
    try:
        off = int(raw_hex[offset_word * 64 : offset_word * 64 + 64], 16) * 2
        length = int(raw_hex[off : off + 64], 16)
        body = raw_hex[off + 64 : off + 64 + length * 2]
        if len(body) != length * 2:
            return None
        return bytes.fromhex(body)
    except ValueError:
        return None


def _ok(receipt):
    return bool(receipt) and receipt.get("status") != "0x0"


def _protocol_logs(receipt, chain_id, table, protocols):
    for log in (receipt or {}).get("logs") or []:
        row = listed(table, chain_id, log.get("address"))
        topics = [t.lower() for t in log.get("topics") or []]
        if row is not None and row["protocol"] in protocols and topics:
            yield row, (log.get("address") or "").lower(), topics, log


# --- Relay ------------------------------------------------------------------------------


def decode_relay_deposits(receipt, chain_id, table) -> list[dict]:
    """Relay deposits in a receipt, from contracts the table lists as Relay.

    [{kind: "depository" | "receiver", emitter, depositor, token (None: native), amount,
    request_id, log_index}]; a receiver payment has no depositor and no amount in its
    log (the payment itself is the deposit) and its ``request_id`` is the whole data it
    forwarded, as hex.
    """
    if not _ok(receipt):
        return []
    out = []
    for _row, emitter, topics, log in _protocol_logs(receipt, chain_id, table, {RELAY}):
        raw = (log.get("data") or "")[2:]
        w = _words(log.get("data"))
        item = {"emitter": emitter, "log_index": _log_index(log)}
        if topics[0] == TOPIC_RELAY_NATIVE and len(w) >= 3:
            item.update(
                kind="depository",
                depositor=_address(w[0]),
                token=None,
                amount=w[1],
                request_id="0x{:064x}".format(w[2]),
            )
        elif topics[0] == TOPIC_RELAY_ERC20 and len(w) >= 4:
            item.update(
                kind="depository",
                depositor=_address(w[0]),
                token=_address(w[1]),
                amount=w[2],
                request_id="0x{:064x}".format(w[3]),
            )
        elif topics[0] == TOPIC_RELAY_FORWARDED:
            data = _bytes_at(raw, 0)
            item.update(
                kind="receiver",
                depositor=None,
                token=None,
                amount=None,
                request_id=("0x" + data.hex()) if data else None,
            )
        else:
            continue
        out.append(item)
    return out


# --- THORChain / Maya -------------------------------------------------------------------

# THORChain shortened asset names (dev.thorchain.org, memo length reduction)
THOR_SHORT = {
    "r": "THOR.RUNE",
    "rune": "THOR.RUNE",
    "a": "AVAX.AVAX",
    "b": "BTC.BTC",
    "c": "BCH.BCH",
    "d": "DOGE.DOGE",
    "e": "ETH.ETH",
    "f": "BASE.ETH",
    "g": "GAIA.ATOM",
    "l": "LTC.LTC",
    "p": "POL.POL",
    "s": "BSC.BNB",
    "tr": "TRON.TRX",
    "x": "XRP.XRP",
}
# swap and limit-swap forms (dev.thorchain.org memos and memo length reduction)
_SWAP = {"swap", "s", "=", "=<", "=>"}
_ACTIONS = {
    "add": "ADD",
    "+": "ADD",
    "a": "ADD",
    "withdraw": "WITHDRAW",
    "wd": "WITHDRAW",
    "-": "WITHDRAW",
    "donate": "DONATE",
    "d": "DONATE",
}
# deposits into the protocol's own accounts, documented as ACTION:ADDR
_ACCOUNT_ACTIONS = {"TRADE+", "SECURE+"}
# the protocol's own chain, where synth (/), trade (~) and secured (-) assets live
NATIVE_CHAIN = {THORCHAIN: "THOR", MAYA: "MAYA"}
CHAIN_NAMES = {"THOR": "THORChain", "MAYA": "Maya"}
# Chains of an asset on the router's own chain: a recipient there is an EVM address
_EVM_ASSET_CHAINS = {"ETH": 1, "ARB": 42161, "BASE": 8453, "BSC": 56, "AVAX": 43114}


def _asset_split(asset: str) -> tuple[str | None, str | None]:
    """(CHAIN, separator) of CHAIN.ASSET (layer 1), CHAIN/ASSET (synth), CHAIN~ASSET
    (trade) or CHAIN-ASSET (secured); (None, None) when it is not written as an asset."""
    for i, ch in enumerate(asset):
        if ch in "./~-":
            return (asset[:i].upper() or None), ch
    return None, None


def _asset_chain(asset: str) -> str | None:
    return _asset_split(asset)[0]


def _native(recipient: str | None) -> str | None:
    if recipient and recipient.startswith("thor1"):
        return "THOR"
    if recipient and recipient.startswith("maya1"):
        return "MAYA"
    return None


def parse_memo(memo: str, protocol: str = THORCHAIN) -> dict:
    """What a THORChain / Maya memo says, without guessing.

    Returns {action, asset, chain, recipient, refund, limit, affiliate, affiliate_fee,
    memo}:

    * a swap (``SWAP``, ``s``, ``=``, limit swaps ``=<`` and ``=>``): ``asset`` is the target
      asset (THORChain short names expanded), ``chain`` the chain it is paid on and
      ``recipient`` the destination (``DEST/REFUND`` gives the refund address). A synth
      (``/``), trade (``~``) or secured (``-``) asset, or a thor1 / maya1 recipient, stays
      on THORChain / Maya: ``chain`` is THOR / MAYA. An empty destination leaves
      ``recipient`` None (the protocol pays the originator);
    * ADD, WITHDRAW, DONATE: the pool asset (and ADD's paired address);
    * TRADE+ / SECURE+ (documented as ``ACTION:ADDR``): the account address;
    * any other action: only ``action``; the memo is shown as it is, nothing is read
      from its other fields.
    """
    parts = (memo or "").split(":")
    head = parts[0].strip()
    out = {
        "action": None,
        "asset": None,
        "chain": None,
        "recipient": None,
        "refund": None,
        "limit": None,
        "affiliate": None,
        "affiliate_fee": None,
        "memo": memo,
    }

    def field(n):
        return (parts[n].strip() or None) if len(parts) > n else None

    low = head.lower()
    if low in _SWAP:
        out["action"] = "SWAP"
        asset = field(1) or ""
        if protocol == THORCHAIN:
            asset = THOR_SHORT.get(asset.lower(), asset)
        dest = field(2) or ""
        if "/" in dest:
            dest, refund = dest.split("/", 1)
            out["refund"] = refund or None
        out["asset"] = asset or None
        out["recipient"] = dest or None
        chain, sep = _asset_split(asset)
        if sep in ("/", "~", "-") or _native(out["recipient"]):
            chain = _native(out["recipient"]) or NATIVE_CHAIN.get(protocol)
        out["chain"] = chain
        out["limit"] = field(3)
        out["affiliate"] = field(4)
        out["affiliate_fee"] = field(5)
        return out
    out["action"] = _ACTIONS.get(low, head.upper() or None)
    if out["action"] in ("ADD", "WITHDRAW", "DONATE"):
        out["asset"] = field(1)
        if out["asset"] and protocol == THORCHAIN:
            out["asset"] = THOR_SHORT.get(out["asset"].lower(), out["asset"])
        out["chain"] = _asset_chain(out["asset"]) if out["asset"] else None
        if out["action"] == "ADD":
            out["recipient"] = field(2)
    elif out["action"] in _ACCOUNT_ACTIONS:
        out["recipient"] = field(1)
        out["chain"] = _native(out["recipient"])
    return out


def memo_target(parsed: dict) -> str | None:
    """Where a swap memo pays out, as text: "BTC", or "THORChain (BTC~BTC)" for an asset
    that stays on the protocol's own chain."""
    chain = parsed.get("chain")
    if chain in CHAIN_NAMES:
        asset = parsed.get("asset")
        return f"{CHAIN_NAMES[chain]} ({asset})" if asset else CHAIN_NAMES[chain]
    return chain or parsed.get("asset")


POOL_ACTIONS = ("ADD", "WITHDRAW", "DONATE")


def memo_pool_fields(parsed: dict) -> dict | None:
    """Fields of an ADD, WITHDRAW or DONATE memo for the edge: the pool and, for ADD, the
    paired address. None for any other action. They name a liquidity pool, not a place the
    funds are paid out to, so no destination chain or recipient is set."""
    if parsed.get("action") not in POOL_ACTIONS:
        return None
    out = {"pool": parsed.get("asset")}
    if parsed["action"] == "ADD" and parsed.get("recipient"):
        out["paired_address"] = parsed["recipient"]
    return out


def memo_pool_text(parsed: dict) -> str:
    """ "ADD BTC.BTC pool, paired address bc1q..." (the end reason's text in brackets)."""
    text = parsed["action"] + (f" {parsed['asset']} pool" if parsed.get("asset") else "")
    if parsed["action"] == "ADD" and parsed.get("recipient"):
        text += f", paired address {parsed['recipient']}"
    return text


def memo_dest_chain_id(parsed: dict) -> int | None:
    """EVM chain id of a swap's target chain when the recipient is an EVM address."""
    rec = parsed.get("recipient") or ""
    if parsed.get("action") == "SWAP" and rec.startswith("0x") and len(rec) == 42:
        return _EVM_ASSET_CHAINS.get(parsed.get("chain") or "")
    return None


def decode_router_deposits(receipt, chain_id, table) -> list[dict]:
    """THORChain / Maya router deposits in a receipt (``Deposit(to, asset, amount, memo)``).

    [{protocol, router, vault, asset (None: native), amount, memo, parsed, log_index}],
    from routers the table lists.
    """
    if not _ok(receipt):
        return []
    out = []
    for row, emitter, topics, log in _protocol_logs(receipt, chain_id, table, ROUTER_PROTOCOLS):
        if topics[0] != TOPIC_ROUTER_DEPOSIT or len(topics) < 3:
            continue
        raw = (log.get("data") or "")[2:]
        w = _words(log.get("data"))
        if len(w) < 2:
            continue
        memo_bytes = _bytes_at(raw, 1)
        memo = memo_bytes.decode("utf-8", "replace") if memo_bytes is not None else ""
        asset = _address(topics[2])
        out.append(
            {
                "protocol": row["protocol"],
                "router": emitter,
                "vault": _address(topics[1]),
                "asset": None if asset is None or int(asset, 16) == 0 else asset,
                "amount": w[0],
                "memo": memo,
                "parsed": parse_memo(memo, row["protocol"]),
                "log_index": _log_index(log),
            }
        )
    return out


# --- Mayan ------------------------------------------------------------------------------


def _recipient(word: int, chain: tuple | None) -> str:
    """A bytes32 recipient: an address on an EVM chain, base58 on Solana, else hex."""
    raw = word.to_bytes(32, "big")
    if chain is not None and chain[1] is not None:
        return _address(word) or "0x" + raw.hex()
    if chain is not None and chain[0] == "Solana":
        return base58(raw)
    return "0x" + raw.hex()


def _token_out(word, chain):
    """The output token of an order: "native" for zero (the destination's own coin)."""
    if word is None:
        return None
    return "native" if word == 0 else _recipient(word, chain)


def _destination(table, number):
    found = table.get(number)
    if found is None:
        return None, f"chain {number}"
    return found, found[0]


def decode_mayan_call(data: str) -> dict | None:
    """The Mayan contract call a forwarder passed on, or None when not decoded.

    {function, dest_chain (name), dest_chain_id (EVM chain id or None), recipient,
    token_out ("native" for the destination's own coin), min_amount_out, same_chain}.
    A MonoChainSwap call pays out on the same
    chain: ``same_chain`` True, ``recipient`` its ``to`` (and ``token`` for transferToken).
    """
    data = (data or "").lower()
    name = MAYAN_CALLS.get(data[:10])
    if name is None:
        return None
    w = _words("0x" + data[10:])

    def order(dest_table, number, dest_word, token_word=None, min_word=None):
        chain, label = _destination(dest_table, number)
        return {
            "function": name,
            "dest_chain": label,
            "dest_chain_id": chain[1] if chain else None,
            "recipient": _recipient(dest_word, chain),
            "token_out": _token_out(token_word, chain),
            "min_amount_out": min_word,
            "same_chain": False,
        }

    try:
        sel = data[:10]
        if sel == "0xa3a30834":  # tokenIn, amountIn, (payloadType, trader, destAddr, destChainId,
            # referrerAddr, tokenOut, minAmountOut, ...), customPayload
            return order(WORMHOLE_CHAINS, w[5], w[4], w[7], w[8])
        if sel == "0x2337e236":  # tokenIn, amountIn, circleMaxFee, destDomain, minFinality,
            # (payloadType, destAddr, tokenOut, amountOutMin, ...)
            return order(CCTP_DOMAINS, w[3], w[6], w[7], w[8])
        if sel == "0xf58b6de8":  # tokenIn, amountIn, redeemFee, circleMaxFee, gasDrop, destAddr,
            # destDomain, ...
            return order(CCTP_DOMAINS, w[6], w[5])
        if sel == "0x2072197f":  # tokenIn, amountIn, redeemFee, gasDrop, destAddr, destDomain, ...
            return order(CCTP_DOMAINS, w[5], w[4])
        if sel == "0x9be95bb4":  # tokenIn, amountIn, gasDrop, redeemFee, destDomain, destAddr
            return order(CCTP_DOMAINS, w[4], w[5])
        if sel == "0x1c59b7fc":  # (tokenIn, amountIn, gasDrop, destAddr, destChain, tokenOut,
            # minAmountOut, ...)
            return order(WORMHOLE_CHAINS, w[4], w[3], w[5], w[6])
        if sel == "0x6eb183d0":  # transferEth(to, referrerAddr, referrerBps)
            return {
                "function": name,
                "same_chain": True,
                "recipient": _address(w[0]),
                "token": None,
            }
        if sel == "0x29d8eb0a":  # transferToken(token, amount, to, referrerAddr, referrerBps)
            return {
                "function": name,
                "same_chain": True,
                "recipient": _address(w[2]),
                "token": _address(w[0]),
            }
    except IndexError:
        return None
    return None


def decode_mayan_forwards(receipt, chain_id, table) -> list[dict]:
    """What Mayan forwarders listed in the table forwarded in a receipt.

    [{event, forwarder, token_in (None: native), amount_in (None for ForwardedEth, whose
    log has no amount: the payment is msg.value), middle_token, middle_amount,
    mayan_protocol, call (decode_mayan_call or None), call_selector, log_index}].
    """
    if not _ok(receipt):
        return []
    out = []
    for _row, emitter, topics, log in _protocol_logs(receipt, chain_id, table, {MAYAN}):
        raw = (log.get("data") or "")[2:]
        w = _words(log.get("data"))
        item = {"forwarder": emitter, "log_index": _log_index(log)}
        try:
            if topics[0] == TOPIC_MAYAN_FORWARDED_ETH:
                item.update(event="ForwardedEth", token_in=None, amount_in=None)
                item.update(middle_token=None, middle_amount=None)
                protocol, data = _address(w[0]), _bytes_at(raw, 1)
            elif topics[0] == TOPIC_MAYAN_FORWARDED_ERC20:
                item.update(event="ForwardedERC20", token_in=_address(w[0]), amount_in=w[1])
                item.update(middle_token=None, middle_amount=None)
                protocol, data = _address(w[2]), _bytes_at(raw, 3)
            elif topics[0] == TOPIC_MAYAN_SWAP_ETH:
                item.update(event="SwapAndForwardedEth", token_in=None, amount_in=w[0])
                item.update(middle_token=_address(w[2]), middle_amount=w[3])
                protocol, data = _address(w[4]), _bytes_at(raw, 5)
            elif topics[0] == TOPIC_MAYAN_SWAP_ERC20:
                item.update(event="SwapAndForwardedERC20", token_in=_address(w[0]), amount_in=w[1])
                item.update(middle_token=_address(w[3]), middle_amount=w[4])
                protocol, data = _address(w[5]), _bytes_at(raw, 6)
            else:
                continue
        except IndexError:
            continue
        hexdata = "0x" + data.hex() if data else ""
        item["mayan_protocol"] = protocol
        item["call_selector"] = hexdata[:10] or None
        item["call"] = decode_mayan_call(hexdata)
        out.append(item)
    return out


def mono_payouts(receipt, emitter) -> list[dict]:
    """MonoChainSwap payouts (``EthTransferred`` / ``TokenTransferred``) logged by ``emitter``.

    [{token (None: native), to, amount, log_index}]
    """
    out = []
    for log in (receipt or {}).get("logs") or []:
        if (log.get("address") or "").lower() != (emitter or "").lower():
            continue
        topics = [t.lower() for t in log.get("topics") or []]
        w = _words(log.get("data"))
        if topics and topics[0] == TOPIC_MONO_ETH and len(w) >= 2:
            out.append(
                {"token": None, "to": _address(w[0]), "amount": w[1], "log_index": _log_index(log)}
            )
        elif topics and topics[0] == TOPIC_MONO_TOKEN and len(w) >= 3:
            out.append(
                {
                    "token": _address(w[0]),
                    "to": _address(w[1]),
                    "amount": w[2],
                    "log_index": _log_index(log),
                }
            )
    return out


def payout_for(payouts, forward_index, previous=None):
    """The payout that belongs to the forward logged at ``forward_index``, or None.

    The forwarder logs right after the call it made, so the payout is the nearest one
    before the forward's log and after ``previous``, the log of the forward before it in
    the same receipt (whose call came first). A forward with no payout in that span has
    none: a payout after it belongs to a later forward and is never taken.
    """
    if forward_index is None:
        return payouts[0] if len(payouts) == 1 else None
    low = -1 if previous is None else previous
    span = [
        p for p in payouts if p["log_index"] is not None and low < p["log_index"] < forward_index
    ]
    return max(span, key=lambda p: p["log_index"]) if span else None


# --- shown text -------------------------------------------------------------------------


def reference(data: str) -> str | None:
    """The call data sent with a plain transfer to a maker (a deposit reference), or None."""
    data = (data or "").lower()
    return data if len(data) > 2 else None
