"""Bridge hops in a trace: every listed bridge named, Across found in any outgoing
transaction's receipt, and the distinct end states of an Across deposit.

Real-log fixtures (tests/fixtures/bridges/), both public Ethereum transactions:

* across_optimism.json: the first FundsDeposited log of the Ethereum SpokePool
  0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5 at or after block 26,000,000 whose
  destinationChainId is 10. Deposit tx
  0x99761650dc3ad05aa5255f32c20c8a7714fd3e54c0a277eddcfe64cbaf4b4f74: the sender pays
  4.634347 USDC to the LI.FI diamond 0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae, which
  deposits 4.588004 USDC into Across (deposit id 4601060). Fill on Optimism:
  0x297e28107d017ce3e5e18afb191424751b642603d0a24ac5574355dc5e66471d.
* across_periphery_arbitrum.json: the first transaction sent to SpokePoolPeriphery
  0x97ccdbea4632140639ad5ea9b944aa034eb15fd4 at or after block 26,000,000 (txlist,
  ascending) whose Across deposit goes to Arbitrum (42161). Deposit tx
  0xaf534ada41c7cb0b0c1d1175e83123e2aa2635de53f748ebe95620e33668352d: 0.0014 ETH, deposit
  id 4600717. Fill on Arbitrum:
  0x0b9b58de7b1b66bbe67ecafcf45ff52d76bf1f6cd0e1b11266550e0d81cd9708.

The end-state tests use fake clients over the older across_new_arbitrum.json fixture
(a direct wallet -> SpokePool deposit).
"""

import json
import os

import pytest

from tornado_demix.bridges import load_bridges
from tornado_demix.errors import ApiError
from tornado_demix.trace import trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()
EXCHANGE = "0x" + "8" * 40


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _token_row(frm, to, units, block, h, token, ts, decimals="6", symbol="USDC"):
    return {
        "from": frm,
        "to": to,
        "value": str(units),
        "blockNumber": str(block),
        "timeStamp": str(ts),
        "hash": h,
        "input": "0x",
        "isError": "0",
        "contractAddress": token,
        "tokenDecimal": decimals,
        "tokenSymbol": symbol,
    }


class Origin:
    """Ethereum side: one outgoing row of ``user`` and the receipt of its transaction."""

    def __init__(self, fx, user, row, native=False, receipt=None):
        self.fx, self.user, self.row, self.native = fx, user, row, native
        self.receipt = fx["receipt"] if receipt is None else receipt
        self.receipts = 0

    def token_transfers(self, addr, contract=None, **kw):
        return [] if self.native or addr != self.user else [self.row]

    def outgoing_txs(self, addr):
        return [self.row] if self.native and addr == self.user else []

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        self.receipts += 1
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt if tx_hash == self.fx["tx_hash"] else None


class Destination:
    """Destination chain over a fixture's fill logs; the recipient moves on to EXCHANGE."""

    def __init__(self, fx, fills=True, token_rows=(), native_rows=()):
        self.fx, self.fills = fx, fills
        self.token_rows, self.native_rows = list(token_rows), list(native_rows)

    def block_by_time(self, ts, closest="before"):
        return self.fx["dest_window"][0 if closest == "before" else 1]

    def current_block(self):
        return self.fx["dest_window"][1]

    def get_logs(self, address, topic0, start, end, topics=None):
        if not self.fills:
            return []
        return [
            r
            for r in self.fx["fills"]
            if r["address"].lower() == address.lower()
            and r["topics"][0] == topic0
            and all(r["topics"][i] == t for i, t in (topics or {}).items())
        ]

    def token_transfers(self, addr, contract=None, **kw):
        return [r for r in self.token_rows if addr in (r["from"], r["to"])]

    def outgoing_txs(self, addr):
        return [r for r in self.native_rows if r["from"] == addr]

    def internal_txs(self, addr):
        return []


def _dests(chain_id, client):
    def dest(chain):
        if chain != chain_id:
            return None
        return {
            "client": client,
            "labels": {EXCHANGE: {"entity": "Some exchange"}},
            "currency": "ETH",
            "is_contract": None,
        }

    return dest


# --- real fixtures ------------------------------------------------------------------

OP_USER = "0xaca0313434bc1cfbe63ba42f8d423035805bd413"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
OP_USDCE = "0x7f5c764cbc14f9669b88837ca1490cca17c31607"
LIFI = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"


def _op_run(**kw):
    fx = _fx("across_optimism.json")
    block = int(fx["receipt"]["blockNumber"], 16)
    row = _token_row(OP_USER, LIFI, 4634347, block, fx["tx_hash"], USDC, fx["ts"])
    fill_ts = int(fx["fills"][0]["timeStamp"], 16)
    onward = _token_row(OP_USER, EXCHANGE, 4587300, 157050000, "0xee", OP_USDCE, fill_ts + 60)
    dest = Destination(fx, token_rows=[onward])
    result = trace_funds(
        Origin(fx, OP_USER, row),
        OP_USER,
        4.634347,
        start_block=block - 5,
        token=USDC,
        chain_id=1,
        destinations=_dests(10, dest),
        **kw,
    )
    return fx, result


def test_across_deposit_made_through_an_aggregator_is_followed_to_optimism():
    fx, result = _op_run()
    bridge, after = result["edges"]
    assert bridge["kind"] == "bridge" and bridge["to"] == LIFI
    info = bridge["bridge"]
    assert info["protocol"] == "across" and info["state"] == "filled"
    assert info["dest_chain_id"] == 10 and info["deposit_id"] == 4601060
    assert info["fill_tx"] == fx["fills"][0]["transactionHash"]
    assert info["via"] == LIFI
    assert after["chain_id"] == 10 and after["from"] == OP_USER and after["to"] == EXCHANGE
    # the fee the aggregator kept before the deposit is not carried over
    assert abs(after["attributed"] - 4.5873) < 1e-6
    (end,) = result["terminals"]
    assert end["chain_id"] == 10 and end["reason"] == "labelled address"


PERIPHERY = "0x97ccdbea4632140639ad5ea9b944aa034eb15fd4"
P_USER = "0x0a4902709530468f504bb004353db32af2bf4eb3"
P_RECIPIENT = "0x0f7ae28de1c8532170ad4ee566b5801485c13a0e"


def test_native_deposit_through_the_spokepool_periphery_is_followed():
    fx = _fx("across_periphery_arbitrum.json")
    assert fx["receipt"]["to"] == PERIPHERY
    block = int(fx["receipt"]["blockNumber"], 16)
    row = {
        "from": P_USER,
        "to": PERIPHERY,
        "value": "1400000000000000",
        "blockNumber": str(block),
        "timeStamp": str(fx["ts"]),
        "hash": fx["tx_hash"],
        "input": "0x609ea081",
        "isError": "0",
        "transactionIndex": "5",
    }
    result = trace_funds(
        Origin(fx, P_USER, row, native=True),
        P_USER,
        0.0014,
        start_block=block - 5,
        chain_id=1,
        destinations=_dests(42161, Destination(fx)),
    )
    edge = result["edges"][0]
    assert edge["kind"] == "bridge" and edge["to"] == PERIPHERY
    info = edge["bridge"]
    assert info["state"] == "filled" and info["dest_chain_id"] == 42161
    assert info["deposit_id"] == 4600717 and info["via"] == PERIPHERY
    assert info["fill_tx"] == "0x0b9b58de7b1b66bbe67ecafcf45ff52d76bf1f6cd0e1b11266550e0d81cd9708"
    # paid out as ETH on Arbitrum (the fill's WETH is the wrapped native coin)
    (end,) = result["terminals"]
    assert end["chain_id"] == 42161 and end["address"] == P_RECIPIENT
    assert end["asset"] == "ETH"
    assert end["amount"] == round(0.0014 * 1393830501964500 / 1400000000000000, 6)


# --- end states over the direct-deposit fixture ----------------------------------------

FX = _fx("across_new_arbitrum.json")
USER = "0xb9f84f474e178aee1434c854337a2d9b2d8a48fa"
SPOKE = "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5"
BLOCK = int(FX["receipt"]["blockNumber"], 16)


def _receipt_with_deadline(deadline):
    """The fixture's receipt with the deposit's fillDeadline word replaced."""
    logs = []
    for log in FX["receipt"]["logs"]:
        log = dict(log)
        if log["address"].lower() == SPOKE:
            pos = 2 + 5 * 64
            log["data"] = log["data"][:pos] + "{:064x}".format(deadline) + log["data"][pos + 64 :]
        logs.append(log)
    return dict(FX["receipt"], logs=logs)


def _direct(dests, to=SPOKE, receipt=None, **kw):
    row = _token_row(USER, to, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"])
    origin = Origin(FX, USER, row, receipt=receipt)
    result = trace_funds(
        origin,
        USER,
        3261.597488,
        start_block=BLOCK - 5,
        token=USDC,
        chain_id=1,
        destinations=dests,
        **kw,
    )
    return origin, result


def _end(result):
    (end,) = result["terminals"]
    return end


def test_no_fill_before_the_deadline_is_fill_not_found():
    import time

    receipt = _receipt_with_deadline(int(time.time()) + 3600)
    _o, result = _direct(_dests(42161, Destination(FX, fills=False)), receipt=receipt)
    end = _end(result)
    assert end["reason"] == "fill_not_found" and end["address"] == SPOKE
    assert end["bridge"]["protocol"] == "across" and end["bridge"]["dest_chain_id"] == 42161
    assert result["edges"][0]["bridge"]["state"] == "fill_not_found"


def test_no_fill_after_the_deadline_is_a_possible_refund():
    _o, result = _direct(_dests(42161, Destination(FX, fills=False)))
    end = _end(result)
    assert end["reason"] == "deposit_expired_possible_refund"
    assert end["bridge"]["state"] == "deposit_expired_possible_refund"


def test_destination_outside_the_registry_is_destination_chain_not_supported():
    _o, result = _direct(lambda chain: None)
    end = _end(result)
    assert end["reason"] == "destination_chain_not_supported"
    assert end["bridge"]["dest_chain_id"] == 42161


def test_destination_without_a_listed_spokepool_is_destination_pool_not_listed():
    table = {k: v for k, v in TABLE.items() if k[0] != 42161}
    _o, result = _direct(_dests(42161, Destination(FX)), bridge_table=table)
    assert _end(result)["reason"] == "destination_pool_not_listed"


def test_a_failed_fill_lookup_is_lookup_failed_and_recorded():
    class Failing(Destination):
        def get_logs(self, *a, **k):
            raise ApiError("explorer down")

    _o, result = _direct(_dests(42161, Failing(FX)))
    end = _end(result)
    assert end["reason"] == "lookup_failed" and end["bridge"]["state"] == "lookup_failed"
    assert result["error_count"] == 1
    assert result["errors"][0]["what"] == "bridge fill"


def test_a_failed_receipt_read_is_lookup_failed():
    _o, result = _direct(_dests(42161, Destination(FX)), receipt=ApiError("no receipt"))
    end = _end(result)
    assert end["reason"] == "lookup_failed"
    assert end["bridge"]["protocol"] == "across"
    assert result["error_count"] == 1


STARGATE_USDC = "0xc026395860db2d07ee33e05fe50ed7bd583189c7"


@pytest.mark.parametrize("follow", [True, False])
def test_a_listed_bridge_without_a_decoder_is_named_not_followed(follow):
    dests = _dests(42161, Destination(FX)) if follow else None
    receipt = dict(FX["receipt"], logs=[])
    _o, result = _direct(dests, to=STARGATE_USDC, receipt=receipt)
    end = _end(result)
    assert end["reason"] == "bridge stargate (not followed)"
    assert end["address"] == STARGATE_USDC
    assert end["bridge"] == result["edges"][0]["bridge"]
    assert end["bridge"]["state"] == "not_followed" and end["bridge"]["dest_chain_id"] is None
    assert result["edges"][0]["kind"] == "bridge"


def test_without_destinations_a_listed_across_pool_is_named_and_no_receipt_read():
    origin, result = _direct(None)
    assert _end(result)["reason"] == "bridge across (not followed)"
    assert origin.receipts == 0


ROUTER = "0x" + "77" * 20


def test_an_unlisted_router_whose_receipt_holds_a_deposit_sent_by_the_address_is_followed():
    """Depositor is the router; the transaction was sent by the traced address."""
    logs = []
    for log in FX["receipt"]["logs"]:
        log = dict(log)
        if log["address"].lower() == SPOKE:
            log["topics"] = log["topics"][:3] + ["0x" + ROUTER[2:].rjust(64, "0")]
        logs.append(log)
    receipt = dict(FX["receipt"], logs=logs, **{"from": USER})

    class Fills(Destination):
        def get_logs(self, address, topic0, start, end, topics=None):
            rows = super().get_logs(address, topic0, start, end, topics)
            # the fill repeats the deposit's depositor (word 8)
            out = []
            for r in rows:
                pos = 2 + 8 * 64
                data = r["data"][:pos] + ROUTER[2:].rjust(64, "0") + r["data"][pos + 64 :]
                out.append(dict(r, data=data))
            return out

    _o, result = _direct(_dests(42161, Fills(FX)), to=ROUTER, receipt=receipt)
    edge = result["edges"][0]
    assert edge["kind"] == "bridge" and edge["bridge"]["state"] == "filled"
    assert edge["bridge"]["via"] == ROUTER


def test_an_unlisted_contract_without_a_deposit_keeps_the_old_end():
    receipt = dict(FX["receipt"], logs=[])
    _o, result = _direct(_dests(42161, Destination(FX)), to=ROUTER, receipt=receipt)
    assert _end(result)["reason"] == "contract without a payment back"
    assert result["edges"][0]["kind"] == "transfer"


def test_a_deposit_of_someone_else_in_the_receipt_is_not_taken():
    logs = []
    for log in FX["receipt"]["logs"]:
        log = dict(log)
        if log["address"].lower() == SPOKE:
            log["topics"] = log["topics"][:3] + ["0x" + "12" * 20 + "00" * 12]
        logs.append(log)
    receipt = dict(FX["receipt"], logs=logs, **{"from": "0x" + "34" * 20})
    _o, result = _direct(_dests(42161, Destination(FX)), to=ROUTER, receipt=receipt)
    assert _end(result)["reason"] == "contract without a payment back"


# --- the deposit must be tied to the traced transfer ---------------------------------


class Many:
    """Ethereum side with several rows and receipts: {tx hash: receipt}."""

    def __init__(self, user, rows, receipts):
        self.user, self.rows, self.receipts = user, rows, receipts

    def token_transfers(self, addr, contract=None, **kw):
        return [
            r
            for r in self.rows
            if addr in (r["from"], r["to"]) and contract in (None, r["contractAddress"])
        ]

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        return self.receipts.get(tx_hash)


def _many(user, rows, receipts, amount, dests):
    return trace_funds(
        Many(user, rows, receipts),
        user,
        amount,
        start_block=BLOCK - 5,
        token=USDC,
        chain_id=1,
        destinations=dests,
    )


NOT_TIED = "bridge across (deposit not tied to this transfer)"


def test_a_tip_in_the_deposit_transaction_is_not_carried_across():
    """The traced address pays 5 USDC to an unlisted contract in the transaction that
    also deposits 3261 USDC into Across: the tip is not the deposit."""
    tip = "0x" + "99" * 20
    row = _token_row(USER, tip, 5000000, BLOCK, FX["tx_hash"], USDC, FX["ts"])
    result = _many(USER, [row], {FX["tx_hash"]: FX["receipt"]}, 5.0, _dests(42161, Destination(FX)))
    (edge,) = result["edges"]
    assert edge.get("bridge", {}).get("state") != "filled"
    assert not any(e.get("chain_id") == 42161 for e in result["edges"])
    (end,) = result["terminals"]
    # an unlisted contract that is not the deposit keeps the plain contract end
    assert end["reason"] == "contract without a payment back" and end["address"] == tip
    assert edge["kind"] == "transfer"


def test_two_transfers_and_one_deposit_follow_the_deposit_once():
    rows = [
        _token_row(USER, SPOKE, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"]),
        _token_row(USER, PERIPHERY, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"]),
    ]
    result = _many(
        USER, rows, {FX["tx_hash"]: FX["receipt"]}, 2 * 3261.597488, _dests(42161, Destination(FX))
    )
    filled = [e for e in result["edges"] if e.get("bridge", {}).get("state") == "filled"]
    assert len(filled) == 1 and filled[0]["to"] == SPOKE
    reasons = sorted(t["reason"] for t in result["terminals"])
    assert NOT_TIED in reasons
    arrived = sum(t["amount"] for t in result["terminals"] if t.get("chain_id") == 42161)
    assert arrived <= 3261.266981 + 1e-6


def test_a_relayer_sending_another_users_deposit_is_not_followed():
    relayer = "0x" + "56" * 20
    row = _token_row(relayer, PERIPHERY, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"])
    receipt = dict(FX["receipt"], **{"from": relayer})  # depositor stays the fixture's user
    result = _many(
        relayer, [row], {FX["tx_hash"]: receipt}, 3261.597488, _dests(42161, Destination(FX))
    )
    (end,) = result["terminals"]
    assert end["reason"] == NOT_TIED
    assert not any(e.get("chain_id") == 42161 for e in result["edges"])


def test_a_deposit_more_than_ten_percent_below_the_transfer_is_not_tied():
    row = _token_row(USER, PERIPHERY, 3700000000, BLOCK, FX["tx_hash"], USDC, FX["ts"])
    result = _many(
        USER, [row], {FX["tx_hash"]: FX["receipt"]}, 3700.0, _dests(42161, Destination(FX))
    )
    assert _end(result)["reason"] == NOT_TIED


def test_a_same_chain_swap_through_a_listed_aggregator_continues_as_a_swap():
    dai = "0x6b175474e89094c44da98b954eedeac495271d0f"
    h = "0x" + "5a" * 32
    rows = [
        _token_row(USER, LIFI, 1000000000, BLOCK, h, USDC, FX["ts"]),
        _token_row(LIFI, USER, 999 * 10**18, BLOCK, h, dai, FX["ts"], "18", "DAI"),
    ]
    result = _many(USER, rows, {h: dict(FX["receipt"], logs=[])}, 1000.0, _dests(42161, None))
    edge = result["edges"][0]
    assert edge["kind"] == "swap" and edge["swapped_to"] == "DAI"
    assert all("not followed" not in t["reason"] for t in result["terminals"])


def test_merged_end_points_keep_every_deposit():
    second = "0x" + "2b" * 32
    logs = []
    for log in FX["receipt"]["logs"]:
        log = dict(log)
        if log["address"].lower() == SPOKE:
            t0, t1, _id, t3 = log["topics"]
            log["topics"] = [t0, t1, "0x{:064x}".format(42), t3]  # another deposit id
        logs.append(log)
    rows = [
        _token_row(USER, SPOKE, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"]),
        _token_row(USER, SPOKE, 3261597488, BLOCK + 1, second, USDC, FX["ts"] + 12),
    ]
    receipts = {FX["tx_hash"]: FX["receipt"], second: dict(FX["receipt"], logs=logs)}
    result = _many(USER, rows, receipts, 2 * 3261.597488, lambda chain: None)
    (end,) = result["terminals"]
    assert end["reason"] == "destination_chain_not_supported"
    assert sorted(b["deposit_id"] for b in end["bridges"]) == [42, 3497136]


# --- swap or bridge at a listed contract ----------------------------------------------


class Refund(Many):
    """Many, plus internal transactions (a native payment back)."""

    def __init__(self, user, rows, receipts, internal):
        super().__init__(user, rows, receipts)
        self.internal = internal

    def internal_txs(self, addr):
        return [r for r in self.internal if addr in (r["from"], r["to"])]


def test_a_bridge_send_with_a_small_native_fee_refund_is_still_the_bridge():
    """3261.6 USDC into the Stargate USDC pool, 0.0001 ETH of unused messaging fee paid
    back in the same transaction: a bridge-role row is never read as a swap."""
    row = _token_row(USER, STARGATE_USDC, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"])
    refund = {
        "from": STARGATE_USDC,
        "to": USER,
        "value": "100000000000000",
        "hash": FX["tx_hash"],
        "blockNumber": str(BLOCK),
        "timeStamp": str(FX["ts"]),
        "isError": "0",
        "input": "",
        "type": "call",
    }
    # the receipt keeps the fixture's Across deposit of the same amount: a Stargate pool
    # does not deposit into Across, so it must not be taken either
    origin = Refund(USER, [row], {FX["tx_hash"]: FX["receipt"]}, [refund])
    result = trace_funds(
        origin,
        USER,
        3261.597488,
        start_block=BLOCK - 5,
        token=USDC,
        chain_id=1,
        destinations=_dests(42161, Destination(FX)),
    )
    (edge,) = result["edges"]
    assert edge["kind"] == "bridge" and "swapped_to" not in edge
    assert _end(result)["reason"] == "bridge stargate (not followed)"
    assert not any(e.get("chain_id") == 42161 for e in result["edges"])


def test_an_aggregator_payback_with_a_bridge_event_in_the_receipt_is_the_bridge():
    """USDC to the LI.FI diamond, ETH paid back, and the Stargate pool logged an event in
    the transaction: the aggregator bridged, the payback is a refund."""
    h = "0x" + "5b" * 32
    row = _token_row(USER, LIFI, 1000000000, BLOCK, h, USDC, FX["ts"])
    refund = {
        "from": LIFI,
        "to": USER,
        "value": "100000000000000",
        "hash": h,
        "blockNumber": str(BLOCK),
        "timeStamp": str(FX["ts"]),
        "isError": "0",
        "input": "",
    }
    log = {"address": STARGATE_USDC, "topics": ["0x" + "11" * 32], "data": "0x"}
    origin = Refund(USER, [row], {h: dict(FX["receipt"], logs=[log])}, [refund])
    result = trace_funds(
        origin,
        USER,
        1000.0,
        start_block=BLOCK - 5,
        token=USDC,
        chain_id=1,
        destinations=_dests(42161, Destination(FX)),
    )
    assert result["edges"][0]["kind"] == "bridge"
    assert _end(result)["reason"] == "bridge lifi (not followed)"


def test_a_real_same_chain_swap_through_the_lifi_diamond_continues_as_a_swap():
    """Real logs: the first transaction sent to the LI.FI diamond at or after block
    26,000,000 that pays its sender back in the native coin and whose receipt holds no
    log of another bridge-table contract (txlist, ascending):
    0x155dab6b97906382622bfaa8a9a51eaecdbf001313ec15e1448d2de18585d7b5,
    swapTokensMultipleV3ERC20ToNative, 65.939348961101432330 INJ in, 0.149247708354314240
    ETH back to the sender."""
    with open(os.path.join(FIXTURES, "lifi_swap.json"), encoding="utf-8") as fh:
        fx = json.load(fh)
    assert fx["tx_hash"] == "0x155dab6b97906382622bfaa8a9a51eaecdbf001313ec15e1448d2de18585d7b5"
    sender = fx["token_transfers"][0]["from"]
    inj = fx["token_transfers"][0]["contractAddress"]
    origin = Refund(sender, fx["token_transfers"], {fx["tx_hash"]: fx["receipt"]}, fx["internal"])
    result = trace_funds(
        origin,
        sender,
        65.93934896110143,
        start_block=25999990,
        token=inj,
        chain_id=1,
        destinations=_dests(42161, Destination(FX)),
    )
    edge = result["edges"][0]
    assert edge["kind"] == "swap" and edge["swapped_to"] == "ETH"
    assert all("not followed" not in t["reason"] for t in result["terminals"])
