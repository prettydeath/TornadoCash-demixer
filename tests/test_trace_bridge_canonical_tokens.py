"""Token deposits through canonical bridges in a trace: Arbitrum token gateways followed on
Arbitrum in the L2 token, ZKsync ERC-20 and legacy mailbox requests named with the
recipient. Fixtures and their selection rule: tests/test_bridges_canonical_tokens.py.
"""

import json
import os

from tornado_demix import bridges_canonical as bc
from tornado_demix.bridges import load_bridges
from tornado_demix.errors import ApiError
from tornado_demix.networks import get_network
from tornado_demix.report_html import build_trace_report
from tornado_demix.trace import BRIDGE_STATES_NOTE, bridge_text, trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()
STD_GATEWAY = "0xa3a7b6f88361f48403514059f1f16c8e78d60eec"
NTV = "0xbed1eb542f9a5aa6419ff3deb921a372681111f6"
DIAMOND = "0x32400084c286cf3e17e7b677ea9583e60a000324"
WEI = 10**18


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class Origin:
    """Ethereum: the sender's token transfers of the fixture, its receipt, the
    transaction and the router's token mapping."""

    def __init__(self, fx, rows, l2_token=None, tx=None, receipt=None, gateway=STD_GATEWAY):
        self.fx, self.rows = fx, rows
        self.l2_token = fx.get("l2_token") if l2_token is None else l2_token
        self.gateway = gateway  # what the router's getGateway answers
        self.tx = fx["tx"] if tx is None else tx
        self.receipt = fx["receipt"] if receipt is None else receipt
        self.calls = []

    def token_transfers(self, addr, contract=None, **kw):
        return [
            r
            for r in self.rows
            if "contractAddress" in r
            and addr in (r["from"], r["to"])
            and contract in (None, r["contractAddress"])
        ]

    def outgoing_txs(self, addr):
        return [r for r in self.rows if "contractAddress" not in r and r["from"] == addr]

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        return self.receipt if tx_hash == self.fx["tx_hash"] else None

    def tx_info(self, tx_hash):
        if isinstance(self.tx, Exception):
            raise self.tx
        return self.tx if tx_hash == self.fx["tx_hash"] else None

    def eth_call(self, to, data):
        self.calls.append((to, data))
        if data.startswith(bc.SELECTOR_GET_GATEWAY):
            if isinstance(self.gateway, Exception):
                raise self.gateway
            return "0x" + self.gateway[2:].rjust(64, "0")
        if isinstance(self.l2_token, Exception):
            raise self.l2_token
        return "0x" + self.l2_token[2:].rjust(64, "0")


class Arbitrum:
    """Arbitrum One over the fixture's L2 token rows."""

    def __init__(self, fx, rows=None, fail=False):
        self.fx, self.fail = fx, fail
        self.rows = fx.get("l2_rows", []) if rows is None else rows

    def block_by_time(self, ts, closest="before"):
        return self.fx["dest_window"][0]

    def current_block(self):
        return self.fx["dest_window"][1]

    def _rows(self, address, contract):
        return [
            r
            for r in self.rows
            if address in (r["from"], r["to"]) and contract in (None, r["contractAddress"])
        ]

    def fetch_all(self, action, address, start_block=0, end_block=0, extra=None):
        if self.fail:
            raise ApiError("arbiscan down")
        assert action == "tokentx"
        return self._rows(address, (extra or {}).get("contractaddress"))

    def token_transfers(self, addr, contract=None, **kw):
        return self._rows(addr, contract)

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return []


def _dests(client):
    def dest(chain):
        if chain != bc.ARBITRUM_ONE or client is None:
            return None
        return {"client": client, "labels": {}, "currency": "ETH", "is_contract": None}

    return dest


def _token_run(name, rows=None, l2=None, table=None, amount=None, **origin):
    fx = _fx(name)
    rows = fx["l1_rows"] if rows is None else rows
    user = rows[0]["from"]
    token = rows[0]["contractAddress"]
    decimals = int(rows[0]["tokenDecimal"])
    amount = amount or sum(int(r["value"]) for r in rows) / 10**decimals
    result = trace_funds(
        Origin(fx, rows, **origin),
        user,
        amount,
        start_block=int(fx["tx"]["blockNumber"], 16) - 5,
        token=token,
        chain_id=1,
        destinations=_dests(Arbitrum(fx) if l2 is None else l2),
        bridge_table=table,
    )
    return fx, result


# --- Arbitrum token gateway -------------------------------------------------------------


def test_outbound_transfer_is_followed_on_arbitrum_in_the_l2_token():
    fx, result = _token_run("arbitrum_gateway_outbound.json")
    user = "0x05cb44f8ac18cb2ded4cabd906fec6876e941d27"
    bridge = result["edges"][0]
    assert bridge["kind"] == "bridge" and bridge["to"] == STD_GATEWAY
    info = bridge["bridge"]
    assert info["protocol"] == "arbitrum" and info["dest_chain_id"] == 42161
    assert info["state"] == bc.CREDITED and info["message_kind"] == "token_deposit"
    assert info["message_num"] == 2567922 and info["function"] == "outboundTransfer"
    assert info["l2_recipient"] == user
    assert info["l1_token"] == fx["l1_token"] and info["l2_token"] == fx["l2_token"]
    assert info["fill_tx"] == fx["l2_rows"][0]["hash"]
    (end,) = result["terminals"]
    assert end["chain_id"] == 42161 and end["address"] == user
    assert end["asset"] == "MOON"  # the L2 token, named from its transfer
    assert abs(end["amount"] - 36825.474728810314) < 1e-6
    assert end["reason"] == "not moved on (held or untraced)"


def test_custom_refund_takes_the_credit_of_the_same_amount():
    fx, result = _token_run("arbitrum_gateway_custom_refund.json")
    info = result["edges"][0]["bridge"]
    assert info["function"] == "outboundTransferCustomRefund"
    assert info["fill_tx"] == "0x659644b1b0b6b90b9e451e80cbc4db687ace36e75025bd0a027380452e36faa2"
    assert info["refund_recipient"] == "0xd411d428a63cf4c7029bc53f0e0f56c4933fdbb7"
    on_l2 = [t for t in result["terminals"] if t.get("chain_id") == 42161]
    assert on_l2 and abs(sum(t["amount"] for t in on_l2) - 0.90010523) < 1e-6
    assert {t["asset"] for t in on_l2} == {"WBTC"}


def test_token_credit_not_found_ends_with_the_recipient_and_tokens():
    fx = _fx("arbitrum_gateway_outbound.json")
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", l2=Arbitrum(fx, []))
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum, l2 credit not found"
    assert end["address"] == STD_GATEWAY and end["chain_id"] == 1
    info = end["bridge"]
    assert info["state"] == bc.L2_CREDIT_NOT_FOUND
    assert info["l2_recipient"] == "0x05cb44f8ac18cb2ded4cabd906fec6876e941d27"
    assert info["l2_token"] == fx["l2_token"]


def test_l2_token_lookup_failure_is_lookup_failed_and_recorded():
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", l2_token=ApiError("node down"))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["bridge"]["protocol"] == "arbitrum"
    assert end["bridge"]["state"] == "lookup_failed"
    assert result["error_count"] >= 1


def test_l2_credit_lookup_failure_is_lookup_failed():
    fx = _fx("arbitrum_gateway_outbound.json")
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", l2=Arbitrum(fx, fail=True))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed"
    assert end["bridge"]["l2_recipient"] == "0x05cb44f8ac18cb2ded4cabd906fec6876e941d27"


def test_token_deposit_of_another_amount_or_sender_is_not_this_transfer():
    fx = _fx("arbitrum_gateway_outbound.json")
    row = fx["l1_rows"][0]
    less = dict(row, value=str(int(row["value"]) - 1))
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", rows=[less])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"
    other = "0x" + "5" * 40
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", rows=[dict(row, **{"from": other})])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"


def test_each_gateway_deposit_is_taken_by_one_transfer_only():
    fx = _fx("arbitrum_gateway_outbound.json")
    first = fx["l1_rows"][0]
    second = dict(first, transactionIndex=str(int(first["transactionIndex"]) + 1))
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", rows=[first, second])
    states = sorted(e["bridge"]["state"] for e in result["edges"] if e["kind"] == "bridge")
    assert states == [bc.CREDITED, "not_followed"]


def test_an_unlisted_gateway_is_followed_when_its_inbox_message_confirms_it():
    # a token with its own gateway (not in the table): the Inbox message sent by that
    # gateway, carrying the same token, sender, recipient and amount, ties it
    table = {k: v for k, v in TABLE.items() if k != (1, STD_GATEWAY)}
    fx, result = _token_run("arbitrum_gateway_outbound.json", table=table)
    info = result["edges"][0]["bridge"]
    assert info["state"] == bc.CREDITED and info["gateway"] == STD_GATEWAY
    assert any(t.get("chain_id") == 42161 for t in result["terminals"])


def test_a_forged_unlisted_gateway_is_not_followed():
    # an unlisted contract can emit DepositInitiated and send the matching retryable
    # itself; it counts only when the router names it as the token's gateway
    table = {k: v for k, v in TABLE.items() if k != (1, STD_GATEWAY)}
    other = "0x" + "de" * 20
    fx, result = _token_run("arbitrum_gateway_outbound.json", table=table, gateway=other)
    assert all(e["kind"] != "bridge" for e in result["edges"])
    (end,) = result["terminals"]
    assert end["reason"] == "contract without a payment back"
    assert end["address"] == STD_GATEWAY and "bridge" not in end
    assert not any(t.get("chain_id") == 42161 for t in result["terminals"])


def test_router_lookup_failure_for_an_unlisted_gateway_is_lookup_failed():
    table = {k: v for k, v in TABLE.items() if k != (1, STD_GATEWAY)}
    _fx_, result = _token_run(
        "arbitrum_gateway_outbound.json", table=table, gateway=ApiError("node down")
    )
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["bridge"]["protocol"] == "arbitrum"
    assert result["error_count"] >= 1


def test_a_listed_gateway_needs_no_router_check():
    fx = _fx("arbitrum_gateway_outbound.json")
    origin = Origin(fx, fx["l1_rows"], gateway=ApiError("not asked"))
    result = trace_funds(
        origin,
        fx["l1_rows"][0]["from"],
        36825.474728810314,
        start_block=int(fx["tx"]["blockNumber"], 16) - 5,
        token=fx["l1_token"],
        chain_id=1,
        destinations=_dests(Arbitrum(fx)),
    )
    assert result["edges"][0]["bridge"]["state"] == bc.CREDITED
    assert not any(d.startswith(bc.SELECTOR_GET_GATEWAY) for _to, d in origin.calls)


def test_without_destinations_the_gateway_is_named_not_followed():
    fx = _fx("arbitrum_gateway_outbound.json")
    rows = fx["l1_rows"]
    result = trace_funds(
        Origin(fx, rows),
        rows[0]["from"],
        int(rows[0]["value"]) / WEI,
        start_block=int(fx["tx"]["blockNumber"], 16) - 5,
        token=rows[0]["contractAddress"],
        chain_id=1,
    )
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"


# --- ZKsync -----------------------------------------------------------------------------


def test_zksync_erc20_deposit_ends_with_recipient_and_token():
    fx, result = _token_run("zksync_two_bridges.json")
    receiver = "0x9452ed6da616783786c6d8aaf73047ecad126c57"
    (end,) = result["terminals"]
    assert end["reason"] == f"bridge zksync (recipient {receiver}, chain not supported)"
    assert end["address"] == NTV
    info = end["bridge"]
    assert info["protocol"] == "zksync" and info["dest_chain_id"] == 324
    assert info["state"] == "destination_chain_not_supported"
    assert info["l1_token"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert info["amount"] == 74977800000
    assert info["function"] == "requestL2TransactionTwoBridges"
    assert "token 0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in bridge_text(info)


def test_zksync_erc20_deposit_of_another_amount_or_sender_is_not_tied():
    fx = _fx("zksync_two_bridges.json")
    row = fx["l1_rows"][0]
    _fx_, result = _token_run("zksync_two_bridges.json", rows=[dict(row, value="1")])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge zksync (not followed)"
    tx = dict(fx["tx"], **{"from": "0x" + "6" * 40})
    _fx_, result = _token_run("zksync_two_bridges.json", tx=tx)
    (end,) = result["terminals"]
    assert end["reason"] == "bridge zksync (not followed)"


def test_zksync_erc20_transaction_lookup_failure_is_lookup_failed():
    _fx_, result = _token_run("zksync_two_bridges.json", tx=ApiError("node down"))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["bridge"]["protocol"] == "zksync"
    assert result["error_count"] >= 1


def _native_row(fx, value=None):
    tx = fx["tx"]
    return {
        "from": tx["from"],
        "to": tx["to"],
        "value": str(int(tx["value"], 16) if value is None else value),
        "blockNumber": str(int(tx["blockNumber"], 16)),
        "timeStamp": str(fx["ts"]),
        "hash": fx["tx_hash"],
        "input": tx["input"],
        "isError": "0",
        "transactionIndex": str(int(tx["transactionIndex"], 16)),
    }


def _native_run(fx, row):
    return trace_funds(
        Origin(fx, [row]),
        row["from"],
        int(row["value"]) / WEI,
        start_block=int(row["blockNumber"]) - 5,
        chain_id=1,
        destinations=_dests(None),
    )


def test_legacy_mailbox_request_ends_with_the_l2_recipient():
    fx = _fx("zksync_legacy_request.json")
    result = _native_run(fx, _native_row(fx))
    user = "0xcdb91c04b6a730f52fd23887d002d9d6bee4c066"
    (end,) = result["terminals"]
    assert end["reason"] == f"bridge zksync (recipient {user}, chain not supported)"
    assert end["address"] == DIAMOND
    info = end["bridge"]
    assert info["dest_chain_id"] == 324 and info["function"] == "requestL2Transaction"
    assert info["l2_recipient"] == user and info["l2_value"] == 1500000000000000
    assert result["edges"][0]["kind"] == "bridge"


def test_legacy_request_carrying_less_than_its_l2_value_is_not_tied():
    fx = _fx("zksync_legacy_request.json")
    result = _native_run(fx, _native_row(fx, value=10**15))
    (end,) = result["terminals"]
    assert end["reason"] == "bridge zksync (not followed)"


# --- reports ----------------------------------------------------------------------------


def test_bridge_text_names_both_tokens_of_a_gateway_hop():
    info = {
        "protocol": "arbitrum",
        "dest_chain_id": 42161,
        "state": bc.CREDITED,
        "message_num": 7,
        "l1_token": "0x" + "1" * 40,
        "l2_token": "0x" + "2" * 40,
        "l2_recipient": "0x" + "c" * 40,
        "fill_tx": "0x" + "d" * 64,
    }
    text = bridge_text(info, {42161: "arbitrum"}.get)
    assert f"token {'0x' + '1' * 40} (L2 {'0x' + '2' * 40})" in text
    assert text.endswith(f"L2 tx {'0x' + 'd' * 64}")


def test_bridge_states_note_explains_token_deposits():
    for text in ("token gateway", "L2 token", "requestL2Transaction", "ERC-20"):
        assert text in BRIDGE_STATES_NOTE


class _Registry:
    def network(self, chain_id):
        return {42161: get_network("arbitrum"), 1: get_network("ethereum")}.get(chain_id)


def test_html_report_shows_the_tokens_and_links_the_l2_recipient():
    fx, result = _token_run("arbitrum_gateway_outbound.json")
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    assert fx["l1_token"] in html and fx["l2_token"] in html
    assert "https://arbiscan.io/address/0x05cb44f8ac18cb2ded4cabd906fec6876e941d27" in html
    assert "https://arbiscan.io/tx/" + fx["l2_rows"][0]["hash"] in html


class TwoTx(Origin):
    """Two gateway deposits of the same amount in two transactions (receipts by hash)."""

    def __init__(self, fx, rows, receipts):
        super().__init__(fx, rows)
        self.receipts = receipts

    def tx_receipt(self, tx_hash):
        return self.receipts.get(tx_hash)

    def tx_info(self, tx_hash):
        return dict(self.fx["tx"], hash=tx_hash) if tx_hash in self.receipts else None


def _renumbered(receipt, num):
    """The fixture receipt with every message number (and sequence number) set to ``num``."""
    rc = json.loads(json.dumps(receipt))
    topic = f"0x{num:064x}"
    for lg in rc["logs"]:
        if lg["topics"][0] in (bc.TOPIC_BRIDGE_MESSAGE, bc.TOPIC_INBOX_MESSAGE):
            lg["topics"][1] = topic
        elif lg["topics"][0] == bc.TOPIC_DEPOSIT_INITIATED:
            lg["topics"][3] = topic
    return rc


def test_one_l2_token_credit_is_not_claimed_by_two_deposits():
    fx = _fx("arbitrum_gateway_outbound.json")
    first = fx["l1_rows"][0]
    later = "0x" + "ab" * 32
    second = dict(
        first,
        hash=later,
        blockNumber=str(int(first["blockNumber"]) + 1),
        timeStamp=str(int(first["timeStamp"]) + 12),
    )
    receipts = {first["hash"]: fx["receipt"], later: _renumbered(fx["receipt"], 2567923)}
    decimals = int(first["tokenDecimal"])
    result = trace_funds(
        TwoTx(fx, [first, second], receipts),
        first["from"],
        2 * int(first["value"]) / 10**decimals,
        start_block=int(first["blockNumber"]) - 5,
        token=first["contractAddress"],
        chain_id=1,
        destinations=_dests(Arbitrum(fx)),
    )
    states = sorted(
        (e["bridge"]["message_num"], e["bridge"]["state"], e["bridge"].get("fill_tx") or "")
        for e in result["edges"]
        if e["kind"] == "bridge"
    )
    assert states == [
        (2567922, bc.CREDITED, fx["l2_rows"][0]["hash"]),
        (2567923, bc.L2_CREDIT_NOT_FOUND, ""),
    ]


def test_page1_diagram_shortens_a_token_known_only_by_its_address():
    from tornado_demix.report_page1 import asset_label

    token = "0xb2490e357980ce57bf5745e181e537a64eb367b1"
    assert asset_label(token) == "0xb249…67b1"
    assert asset_label("MOON") == "MOON" and asset_label(None) == ""
    fx = _fx("arbitrum_gateway_outbound.json")
    rows = [dict(fx["l1_rows"][0], tokenSymbol="")]  # no symbol: named by its address
    _fx_, result = _token_run("arbitrum_gateway_outbound.json", rows=rows, l2=Arbitrum(fx, []))
    assert result["asset"] == token
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    assert "36825.5 0xb249…67b1" in html  # the page-1 flow
    assert "bridge arbitrum, 36825.5 0xb249…67b1" in html  # the trace graph


def test_a_token_trace_is_named_by_the_symbol_of_its_transfers():
    _fx_, result = _token_run("arbitrum_gateway_outbound.json")
    assert result["asset"] == "MOON" and result["edges"][0]["asset"] == "MOON"


def test_each_zksync_erc20_call_is_taken_by_one_transfer_only():
    fx = _fx("zksync_two_bridges.json")
    first = fx["l1_rows"][0]
    second = dict(first, transactionIndex=str(int(first["transactionIndex"]) + 1))
    _fx_, result = _token_run("zksync_two_bridges.json", rows=[first, second])
    states = sorted(e["bridge"]["state"] for e in result["edges"] if e["kind"] == "bridge")
    assert states == ["destination_chain_not_supported", "not_followed"]


# --- token identity ---------------------------------------------------------------------


def test_edges_terminals_and_result_keep_the_token_address():
    fx, result = _token_run("arbitrum_gateway_outbound.json")
    assert result["asset"] == "MOON" and result["asset_address"] == fx["l1_token"]
    edge = result["edges"][0]
    assert edge["asset_address"] == fx["l1_token"] and edge["chain_id"] == 1
    (end,) = result["terminals"]
    assert end["asset"] == "MOON" and end["asset_address"] == fx["l2_token"]
    assert end["chain_id"] == 42161
    data = json.loads(json.dumps(result))
    assert data["terminals"][0]["asset_address"] == fx["l2_token"]
    assert data["edges"][0]["asset_address"] == fx["l1_token"]


def test_native_assets_carry_no_token_address():
    fx = _fx("zksync_legacy_request.json")
    result = _native_run(fx, _native_row(fx))
    assert result["asset_address"] is None
    assert result["edges"][0]["asset_address"] is None
    assert result["terminals"][0]["asset_address"] is None


def test_totals_keep_a_same_symbol_impostor_apart():
    from tornado_demix.report_html import trace_totals

    real, fake = "0x" + "1" * 40, "0x" + "2" * 40
    result = {
        "amount": 3.0,
        "asset": "USDC",
        "asset_address": real,
        "terminals": [
            {
                "asset": "USDC",
                "asset_address": real,
                "chain_id": 1,
                "amount": 2.0,
                "reason": "labelled address",
            },
            {
                "asset": "USDC",
                "asset_address": fake,
                "chain_id": 1,
                "amount": 1.0,
                "reason": "labelled address",
            },
        ],
    }
    rows = trace_totals(result)
    assert len(rows) == 2
    by_address = {r["asset_address"]: r for r in rows}
    assert by_address[real]["total"] == 2.0 and by_address[fake]["total"] == 1.0
    # the two rows read differently: the address tells them apart
    assert len({r["asset"] for r in rows}) == 2
    assert all(r["asset"].startswith("USDC") for r in rows)


def test_totals_of_an_older_result_without_addresses_group_by_name():
    from tornado_demix.report_html import trace_totals

    result = {
        "amount": 3.0,
        "asset": "ETH",
        "terminals": [
            {"asset": "ETH", "amount": 2.0, "reason": "labelled address"},
            {"asset": "ETH", "amount": 1.0, "reason": "labelled address"},
        ],
    }
    (row,) = trace_totals(result)
    assert row["asset"] == "ETH" and row["total"] == 3.0
