"""Circle CCTP (v1 and v2): deposits decoded, tied to the traced USDC transfer and followed
to the mint on an EVM destination; a non-EVM destination is named with its recipient.

Fixtures (tests/fixtures/bridges/cctp_*.json): real transactions picked by a fixed rule,
the first successful matching ``DepositForBurn`` at or after Ethereum block 26,300,000
whose transaction the depositor sent straight to the TokenMessenger, skipping any whose
addresses (sender, log and calldata words, message bodies, the bytes32 recipient also in
base58) appear in the project's private data other than protocol or token contracts:

* cctp_v1_deposit: first v1 ``depositForBurn`` to a registry EVM domain (Arbitrum), with
  its mint on Arbitrum (MessageReceived + MintAndWithdraw, nonce 508310);
* cctp_v2_deposit: first v2 ``depositForBurn`` to a registry EVM domain (Arbitrum), with
  the recipient's MintAndWithdraw logs in the search window (two: only one matches);
* cctp_v2_solana: first v2 ``depositForBurn`` to Solana (domain 5);
* cctp_v2_fast: first v2 fast transfer (minFinalityThreshold 1000) to Arbitrum or Polygon
  (the destinations whose explorer answered; Base's did not) at or after 26,300,000: a
  maxFee of 15,040,114, a fee executed of 15,038,609, and a mint of the burned amount
  less that fee (MintAndWithdraw's amount is net, its feeCollected is the fee).
"""

import json
import os

import pytest

from tornado_demix import bridges_cctp as cc
from tornado_demix.bridges import load_bridges
from tornado_demix.errors import ApiError
from tornado_demix.keccak import keccak256
from tornado_demix.networks import get_network, load_networks
from tornado_demix.report_html import build_trace_report, end_state
from tornado_demix.trace import BRIDGE_STATES_NOTE, bridge_text, bridge_tx_label, trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ARB_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
V1_USER = "0x8252a1a673796110ca091344fdae1729e7aea94b"
V2_USER = "0xb8ce694deaefd85a431c73cb6050b90a74f73cd3"
SOLANA_RECIPIENT = "13EFLnJcDD1fuoNe4ZgdTsZ87bqptnHtkph8dptkfHjn"


def _fx(name):
    with open(os.path.join(FIXTURES, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _topic(sig):
    return "0x" + keccak256(sig.encode()).hex()


# --- topics, selectors, table rows ------------------------------------------------------


def test_topics_and_selectors_are_keccak_of_the_circle_signatures():
    assert cc.TOPIC_DEPOSIT_V1 == _topic(
        "DepositForBurn(uint64,address,uint256,address,bytes32,uint32,bytes32,bytes32)"
    )
    assert cc.TOPIC_DEPOSIT_V2 == _topic(
        "DepositForBurn(address,uint256,address,bytes32,uint32,bytes32,bytes32,uint256,uint32,bytes)"
    )
    assert cc.TOPIC_MINT_V1 == _topic("MintAndWithdraw(address,uint256,address)")
    assert cc.TOPIC_MINT_V2 == _topic("MintAndWithdraw(address,uint256,address,uint256)")
    assert cc.TOPIC_RECEIVED_V1 == _topic("MessageReceived(address,uint32,uint64,bytes32,bytes)")
    assert cc.TOPIC_RECEIVED_V2 == _topic(
        "MessageReceived(address,uint32,bytes32,bytes32,uint32,bytes)"
    )
    sigs = {
        "depositForBurn(uint256,uint32,bytes32,address)": ("depositForBurn", 1),
        "depositForBurnWithCaller(uint256,uint32,bytes32,address,bytes32)": (
            "depositForBurnWithCaller",
            1,
        ),
        "depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)": (
            "depositForBurn",
            2,
        ),
        "depositForBurnWithHook(uint256,uint32,bytes32,address,bytes32,uint256,uint32,bytes)": (
            "depositForBurnWithHook",
            2,
        ),
    }
    assert {_topic(s)[:10]: v for s, v in sigs.items()} == cc.CALLS


def test_domains_come_from_the_single_cctp_table():
    from tornado_demix import bridges_named as bn

    assert cc.CCTP_DOMAINS is bn.CCTP_DOMAINS
    assert cc.domain_chain(3) == ("Arbitrum", 42161)
    assert cc.domain_chain(5) == ("Solana", None)
    assert cc.domain_chain(4) == ("Noble", None) and cc.domain_chain(25) == ("Starknet", None)
    assert cc.domain_chain(999) == ("domain 999", None)
    assert cc.origin_domain(1) == 0 and cc.origin_domain(8453) == 6
    assert cc.origin_domain(100) is None


def test_contracts_listed_on_every_registry_chain_with_a_domain():
    registry = {n.chain_id for n in load_networks().values()}
    # BNB Chain (domain 17) is not on Circle's EVM contracts pages: nothing listed there
    chains = {c for _n, c in cc.CCTP_DOMAINS.values() if c in registry} - {56}
    assert chains == {1, 10, 137, 8453, 42161, 43114}
    for chain in chains:
        rows = [r for (c, _a), r in TABLE.items() if c == chain and r["protocol"] == "cctp"]
        kinds = {cc.contract_role(r) for r in rows}
        for version in (1, 2):
            for kind in ("messenger", "transmitter", "minter"):
                assert (kind, version) in kinds, (chain, kind, version)
        for r in rows:
            assert r["source_url"].startswith(
                ("https://developers.circle.com/", "https://etherscan.io/")
            )
    v2 = "0x28b5a0e9c621a5badaa536219b3a228c8168cf5d"
    assert all(TABLE[(c, v2)]["protocol"] == "cctp" for c in chains)


# --- decoders on the real fixtures ------------------------------------------------------


def test_decode_v1_call_and_deposit():
    fx = _fx("cctp_v1_deposit")
    call = cc.decode_call(fx["tx"]["input"])
    assert call["function"] == "depositForBurn" and call["version"] == 1
    assert call["amount"] == 230167652918 and call["dest_domain"] == 3
    assert call["burn_token"] == USDC and call["recipient"] == V1_USER
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    assert d["version"] == 1 and d["nonce"] == 508310
    assert d["depositor"] == V1_USER and d["burn_token"] == USDC
    assert d["amount"] == 230167652918 and d["dest_domain"] == 3
    assert d["dest_chain"] == "Arbitrum" and d["dest_chain_id"] == 42161
    assert d["recipient"] == V1_USER
    assert d["dest_messenger"] == "0x19330d10d9cc8751218eaf51e8885d058642e08a"


def test_decode_v2_deposit_carries_fee_and_finality_and_no_nonce():
    fx = _fx("cctp_v2_deposit")
    call = cc.decode_call(fx["tx"]["input"])
    assert call["function"] == "depositForBurn" and call["version"] == 2
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    assert d["version"] == 2 and d["nonce"] is None
    assert d["depositor"] == V2_USER and d["amount"] == 519604474
    assert d["max_fee"] == 0 and d["min_finality"] == 2000
    assert d["dest_chain_id"] == 42161 and d["recipient"] == V2_USER


def test_decode_v2_deposit_to_solana_gives_the_base58_recipient():
    fx = _fx("cctp_v2_solana")
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    assert d["dest_domain"] == 5 and d["dest_chain"] == "Solana"
    assert d["dest_chain_id"] is None and d["recipient"] == SOLANA_RECIPIENT
    assert d["dest_messenger"] is None  # not an EVM address


def test_deposit_from_an_unlisted_emitter_is_ignored():
    fx = _fx("cctp_v1_deposit")
    receipt = json.loads(json.dumps(fx["receipt"]))
    for log in receipt["logs"]:
        log["address"] = "0x" + "11" * 20
    assert cc.decode_deposits(receipt, 1, TABLE) == []
    assert cc.decode_deposits(dict(receipt, status="0x0"), 1, TABLE) == []


def test_decode_received_and_mint_on_the_destination():
    fx = _fx("cctp_v1_deposit")
    (m,) = cc.decode_received(fx["dest"]["receipt"], 42161, TABLE)
    assert m["version"] == 1 and m["source_domain"] == 0 and m["nonce"] == 508310
    assert m["burn_token"] == USDC and m["recipient_word"] == int(V1_USER, 16)
    assert m["amount"] == 230167652918 and m["message_sender"] == V1_USER
    (mint,) = cc.decode_mints(fx["dest"]["receipt"], 42161, TABLE)
    assert mint == {
        "version": 1,
        "recipient": V1_USER,
        "amount": 230167652918,
        "token": ARB_USDC,
        "fee": 0,
        "log_index": 13,
    }
    fx2 = _fx("cctp_v2_deposit")
    (m2,) = cc.decode_received(fx2["dest"]["receipt"], 42161, TABLE)
    assert m2["version"] == 2 and m2["fee_executed"] == 0 and m2["message_sender"] == V2_USER


# --- the destination -------------------------------------------------------------------


class Dest:
    """The destination chain: MintAndWithdraw logs, receipts and the recipient's rows."""

    def __init__(self, fx, logs=None, receipt=None, fail=None):
        self.fx = fx
        self.logs = fx["dest"]["mint_logs"] if logs is None else logs
        self.receipt = fx["dest"]["receipt"] if receipt is None else receipt
        self.fail = fail
        self.calls = []

    def block_by_time(self, ts, closest="before"):
        return self.fx["dest"]["window"][0 if closest == "before" else 1]

    def current_block(self):
        return self.fx["dest"]["window"][1] + 10

    def get_logs(self, address, topic0, start, end, topics=None):
        self.calls.append((address, topic0, topics))
        if self.fail:
            raise self.fail
        return [
            log
            for log in self.logs
            if log["address"] == address
            and log["topics"][0] == topic0
            and all(log["topics"][i] == t for i, t in (topics or {}).items())
        ]

    def tx_receipt(self, tx_hash):
        return self.receipt if tx_hash == self.receipt.get("transactionHash") else None

    def token_transfers(self, addr, contract=None, **kw):
        return [
            r
            for r in self.fx["dest"].get("rows", [])
            if addr in (r["to"], r["from"]) and contract in (None, r["contractAddress"])
        ]

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return []


def test_find_mint_takes_the_log_whose_message_matches_the_deposit():
    fx = _fx("cctp_v2_deposit")
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    dest = Dest(fx)
    mint = cc.find_mint(dest, d, 0, fx["ts"], TABLE, now=fx["ts"] + 10**6)
    assert mint["tx_hash"] == fx["dest"]["receipt"]["transactionHash"]
    assert mint["amount"] == 519604474 and mint["token"] == ARB_USDC and mint["fee"] == 0
    assert mint["block"] == 0x1EF5ECFE and mint["ts"] == 0x6AE38C6A
    # one query, by the recipient, on the destination messenger the deposit names
    ((addr, topic0, topics),) = dest.calls
    assert addr == d["dest_messenger"] and topic0 == cc.TOPIC_MINT_V2
    assert topics == {1: "0x" + "0" * 24 + V2_USER[2:]}
    claimed = {(42161, mint["tx_hash"], mint["log_index"])}
    assert cc.find_mint(dest, d, 0, fx["ts"], TABLE, now=fx["ts"] + 10**6, claimed=claimed) is None


def test_find_mint_rejects_a_message_from_another_domain_or_sender():
    fx = _fx("cctp_v1_deposit")
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    now = fx["ts"] + 10**6
    assert cc.find_mint(Dest(fx), d, 0, fx["ts"], TABLE, now=now) is not None
    assert cc.find_mint(Dest(fx), d, 6, fx["ts"], TABLE, now=now) is None  # from Base
    other = dict(d, depositor="0x" + "22" * 20)
    assert cc.find_mint(Dest(fx), other, 0, fx["ts"], TABLE, now=now) is None
    other = dict(d, nonce=d["nonce"] + 1)
    assert cc.find_mint(Dest(fx), other, 0, fx["ts"], TABLE, now=now) is None


# --- in a trace -------------------------------------------------------------------------


class Client:
    def __init__(self, fx, user, rows, receipt=None):
        self.fx, self.user, self.rows = fx, user, rows
        self.receipt = fx["receipt"] if receipt is None else receipt

    def token_transfers(self, addr, contract=None, **kw):
        if addr != self.user:
            return []
        return [r for r in self.rows if contract in (None, r["contractAddress"])]

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt if tx_hash == self.fx["tx_hash"] else None

    def tx_info(self, tx_hash):
        return self.fx["tx"] if tx_hash == self.fx["tx_hash"] else None


def _run(name, rows=None, dest=None, receipt=None, follow=True, amount=None):
    fx = _fx(name)
    rows = fx["l1_rows"] if rows is None else rows
    user = rows[0]["from"]
    dest_client = Dest(fx) if dest is None and "dest" in fx else dest

    def destinations(chain):
        if chain == 42161 and dest_client:
            return {"client": dest_client, "labels": {}, "currency": "ETH"}
        return None

    result = trace_funds(
        Client(fx, user, rows, receipt),
        user,
        amount or int(rows[0]["value"]) / 10**6,
        start_block=int(fx["tx"]["blockNumber"], 16) - 5,
        token=USDC,
        chain_id=1,
        destinations=destinations if follow else None,
    )
    return fx, result


@pytest.mark.parametrize("name", ["cctp_v1_deposit", "cctp_v2_deposit"])
def test_deposit_is_followed_to_the_mint_on_the_destination(name):
    fx, result = _run(name)
    (edge,) = [e for e in result["edges"] if e["chain_id"] == 1]
    assert edge["kind"] == "bridge"
    info = edge["bridge"]
    assert info["protocol"] == "cctp" and info["state"] == cc.MINTED
    assert (
        info["dest_chain_id"] == 42161
        and info["fill_tx"] == fx["dest"]["receipt"]["transactionHash"]
    )
    assert info["mint_token"] == ARB_USDC and info["dest_recipient"] == fx["l1_rows"][0]["from"]
    assert bridge_tx_label(info) == "mint tx"
    (end,) = result["terminals"]
    assert end["chain_id"] == 42161 and end["asset_address"] == ARB_USDC
    assert end["asset"] == "USDC"
    assert end["address"] == fx["l1_rows"][0]["from"]
    assert end["reason"] == "not moved on (held or untraced)"
    assert abs(end["amount"] - int(fx["l1_rows"][0]["value"]) / 10**6) < 1e-6


def test_v1_info_carries_the_nonce_and_the_function():
    _fx_, result = _run("cctp_v1_deposit")
    info = result["edges"][0]["bridge"]
    assert info["nonce"] == 508310 and info["version"] == 1
    assert info["function"] == "depositForBurn"
    text = bridge_text(info)
    assert text.startswith("bridge cctp → chain 42161") and "nonce 508310" in text
    assert "mint tx 0x8ce4dd6e" in text


def test_solana_destination_is_named_with_its_recipient():
    _fx_, result = _run("cctp_v2_solana")
    (end,) = result["terminals"]
    assert end["reason"] == f"bridge cctp (to Solana, recipient {SOLANA_RECIPIENT})"
    assert end["bridge"]["state"] == cc.MINTED_OFF_EVM
    assert end["bridge"]["dest_recipient"] == SOLANA_RECIPIENT
    assert end_state(end["reason"])[0] == "bridge"


def test_destination_chain_not_readable_is_not_supported():
    fx = _fx("cctp_v1_deposit")
    _fx_, result = _run("cctp_v1_deposit", dest=False)
    (end,) = result["terminals"]
    assert end["reason"] == f"bridge cctp (to Arbitrum, recipient {V1_USER}, chain not supported)"
    assert end["bridge"]["state"] == "destination_chain_not_supported"
    assert abs(end["amount"] - int(fx["l1_rows"][0]["value"]) / 10**6) < 1e-6


def test_mint_not_found_ends_the_branch_with_the_recipient():
    fx = _fx("cctp_v2_deposit")
    _fx_, result = _run("cctp_v2_deposit", dest=Dest(fx, logs=[]))
    (end,) = result["terminals"]
    assert end["reason"] == f"bridge cctp (to Arbitrum, recipient {V2_USER}, mint not found)"
    assert end["bridge"]["state"] == cc.MINT_NOT_FOUND


def test_destination_lookup_failure_is_lookup_failed():
    fx = _fx("cctp_v2_deposit")
    _fx_, result = _run("cctp_v2_deposit", dest=Dest(fx, fail=ApiError("down")))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["bridge"]["protocol"] == "cctp"
    assert result["error_count"] >= 1


def test_receipt_failure_is_lookup_failed():
    _fx_, result = _run("cctp_v1_deposit", receipt=ApiError("down"))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["bridge"]["protocol"] == "cctp"


def test_amount_not_tied_is_named_only():
    fx = _fx("cctp_v1_deposit")
    row = dict(fx["l1_rows"][0], value=str(int(fx["l1_rows"][0]["value"]) - 1))
    _fx_, result = _run("cctp_v1_deposit", rows=[row])
    assert result["terminals"][0]["reason"] == "bridge cctp (not followed)"


def test_deposit_by_another_address_is_named_only():
    fx = _fx("cctp_v1_deposit")
    row = dict(fx["l1_rows"][0], **{"from": "0x" + "33" * 20})
    _fx_, result = _run("cctp_v1_deposit", rows=[row])
    assert result["terminals"][0]["reason"] == "bridge cctp (not followed)"


def test_each_deposit_is_taken_by_one_transfer():
    fx = _fx("cctp_v1_deposit")
    row = fx["l1_rows"][0]
    amount = 2 * int(row["value"]) / 10**6
    _fx_, result = _run("cctp_v1_deposit", rows=[row, dict(row)], amount=amount)
    reasons = sorted(t["reason"] for t in result["terminals"])
    assert reasons == ["bridge cctp (not followed)", "not moved on (held or untraced)"]


def test_without_destinations_the_deposit_is_named_only():
    _fx_, result = _run("cctp_v1_deposit", follow=False)
    assert result["terminals"][0]["reason"] == "bridge cctp (not followed)"


def test_states_note_and_report():
    for word in ("CCTP", "mint not found", "Solana"):
        assert word in BRIDGE_STATES_NOTE
    _fx_, result = _run("cctp_v1_deposit")
    html = build_trace_report(result, get_network("ethereum"))
    assert "mint tx" in html and "cctp" in html
    _fx_, result = _run("cctp_v2_solana")
    assert SOLANA_RECIPIENT in build_trace_report(result, get_network("ethereum"))


# --- v2 fee (fast transfers) ------------------------------------------------------------

FAST_USER = "0x6a2abff960b663462cbc46a2cfcf85063fe8ae14"
FAST_BURN, FAST_FEE = 150386095031, 15038609


def test_real_fast_transfer_mint_is_net_of_the_fee():
    fx = _fx("cctp_v2_fast")
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    assert d["amount"] == FAST_BURN and d["max_fee"] == 15040114 and d["min_finality"] == 1000
    (mint,) = cc.decode_mints(fx["dest"]["receipt"], 42161, TABLE)
    assert mint["fee"] == FAST_FEE and mint["amount"] == FAST_BURN - FAST_FEE
    (msg,) = cc.decode_received(fx["dest"]["receipt"], 42161, TABLE)
    assert msg["fee_executed"] == FAST_FEE and msg["amount"] == FAST_BURN


def test_fast_transfer_is_followed_with_the_burn_less_the_fee():
    fx, result = _run("cctp_v2_fast")
    (edge,) = [e for e in result["edges"] if e["chain_id"] == 1]
    info = edge["bridge"]
    assert info["state"] == cc.MINTED and info["fee"] == FAST_FEE
    assert info["minted"] == FAST_BURN - FAST_FEE
    (end,) = result["terminals"]
    assert end["chain_id"] == 42161 and end["address"] == FAST_USER
    assert abs(end["amount"] - (FAST_BURN - FAST_FEE) / 10**6) < 1e-6


def _patched(fx, mint_amount=None, fee_collected=None, fee_executed=None):
    """The fast fixture's destination with the mint log (and its receipt copy) or the
    message's feeExecuted changed."""
    dest = json.loads(json.dumps(fx["dest"]))
    logs = dest["mint_logs"] + [
        x for x in dest["receipt"]["logs"] if x["topics"][0] == cc.TOPIC_MINT_V2
    ]
    for log in logs:
        amount, fee = int(log["data"][2:66], 16), int(log["data"][66:130], 16)
        amount = amount if mint_amount is None else mint_amount
        fee = fee if fee_collected is None else fee_collected
        log["data"] = "0x{:064x}{:064x}".format(amount, fee)
    if fee_executed is not None:
        for log in dest["receipt"]["logs"]:
            if log["topics"][0] == cc.TOPIC_RECEIVED_V2:
                body = 2 + 64 * 4  # the body after sourceDomain, sender, offset, length
                at = body + 164 * 2
                data = log["data"]
                log["data"] = data[:at] + "{:064x}".format(fee_executed) + data[at + 64 :]
    return dict(fx, dest=dest)


def _find(fx, deposit=None):
    (d,) = cc.decode_deposits(fx["receipt"], 1, TABLE)
    d = dict(d, **(deposit or {}))
    return cc.find_mint(Dest(fx), d, 0, fx["ts"], TABLE, now=fx["ts"] + 10**6)


def test_fee_rejections():
    fx = _fx("cctp_v2_fast")
    assert _find(fx)["fee"] == FAST_FEE
    # a fee above the deposit's maxFee
    assert _find(fx, {"max_fee": FAST_FEE - 1}) is None
    # feeCollected (mint) differs from feeExecuted (message), the sum still the burn
    odd = _patched(fx, mint_amount=FAST_BURN - FAST_FEE + 1, fee_collected=FAST_FEE - 1)
    assert _find(odd) is None
    assert _find(_patched(fx, fee_executed=FAST_FEE - 1)) is None
    # minted plus fee is not the burned amount
    assert _find(_patched(fx, mint_amount=FAST_BURN - FAST_FEE - 1)) is None
    assert _find(_patched(fx, mint_amount=FAST_BURN)) is None


def test_noble_recipient_is_shown_as_bytes32_by_name():
    fx = _fx("cctp_v2_solana")
    receipt = json.loads(json.dumps(fx["receipt"]))
    for log in receipt["logs"]:
        if log["topics"][0] == cc.TOPIC_DEPOSIT_V2:
            data = log["data"]
            log["data"] = data[: 2 + 128] + "{:064x}".format(4) + data[2 + 192 :]
    (d,) = cc.decode_deposits(receipt, 1, TABLE)
    assert d["dest_chain"] == "Noble" and d["dest_chain_id"] is None
    assert d["recipient"].startswith("0x") and len(d["recipient"]) == 66
    _fx_, result = _run("cctp_v2_solana", receipt=receipt)
    assert result["terminals"][0]["reason"] == f"bridge cctp (to Noble, recipient {d['recipient']})"
