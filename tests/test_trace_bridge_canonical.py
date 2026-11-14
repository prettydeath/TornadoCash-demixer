"""Arbitrum Inbox and ZKsync Bridgehub hops in a trace, and how reports show them.

Fixtures and their selection rule: see tests/test_bridges_canonical.py. The Arbitrum
fixtures also keep the L2 recipient's normal transactions in the hour after the L1
block (the credit and, where there is one, the next transfer out).
"""

import json
import os
import sys

import pytest

from tests.test_bridges_canonical import _inbox_receipt
from tests.test_cli import _stub, args, networks_csv  # noqa: F401  (pytest fixtures)
from tornado_demix import bridges_canonical as bc
from tornado_demix import cli
from tornado_demix.bridges import load_bridges
from tornado_demix.errors import ApiError
from tornado_demix.networks import get_network
from tornado_demix.report_html import build_trace_report, terminal_bridge_text
from tornado_demix.trace import BRIDGE_STATES_NOTE, bridge_text, trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
TABLE = load_bridges()
INBOX = bc.ARBITRUM_INBOX
HUB = "0x303a465b659cbb0ab36ee643ea362c509eeb5213"
WEI = 10**18


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _row(fx, frm=None, value=None):
    tx = fx["tx"]
    return {
        "from": frm or tx["from"],
        "to": tx["to"],
        "value": str(int(tx["value"], 16) if value is None else value),
        "blockNumber": str(int(tx["blockNumber"], 16)),
        "timeStamp": str(fx["ts"]),
        "hash": fx["tx_hash"],
        "input": tx["input"],
        "isError": "0",
        "transactionIndex": str(int(tx["transactionIndex"], 16)),
    }


class Origin:
    """Ethereum: the outgoing rows of ``user`` and the fixture's receipt."""

    def __init__(self, fx, user, rows, receipt=None):
        self.fx, self.user, self.rows = fx, user, rows
        self.receipt = fx["receipt"] if receipt is None else receipt

    def token_transfers(self, addr, contract=None, **kw):
        return []

    def outgoing_txs(self, addr):
        return list(self.rows) if addr == self.user else []

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt if tx_hash == self.fx["tx_hash"] else None


class Arbitrum:
    """Arbitrum One over the fixture's L2 rows (credit lookup and histories)."""

    def __init__(self, fx, rows=None, fail=False):
        self.fx, self.fail = fx, fail
        self.rows = fx.get("l2_rows", []) if rows is None else rows

    def block_by_time(self, ts, closest="before"):
        return self.fx.get("dest_window", [0, 1])[0 if closest == "before" else 1]

    def current_block(self):
        return self.fx.get("dest_window", [0, 1])[1]

    def fetch_all(self, action, address, start_block=0, end_block=0, extra=None):
        if self.fail:
            raise ApiError("arbiscan down")
        return [r for r in self.rows if address in (r["from"], r["to"])]

    def outgoing_txs(self, addr):
        return [r for r in self.rows if addr in (r["from"], r["to"])]

    def token_transfers(self, addr, contract=None, **kw):
        return []

    def internal_txs(self, addr):
        return []


def _dests(client):
    def dest(chain):
        if chain != bc.ARBITRUM_ONE or client is None:
            return None
        return {"client": client, "labels": {}, "currency": "ETH", "is_contract": None}

    return dest


def _run(name, user=None, rows=None, receipt=None, l2=None, destinations=True, amount=None, **kw):
    fx = _fx(name)
    user = user or fx["tx"]["from"]
    rows = rows if rows is not None else [_row(fx, frm=user)]
    amount = amount or int(rows[0]["value"]) / WEI
    block = int(fx["tx"]["blockNumber"], 16)
    if destinations:
        dests = _dests(None if l2 is False else Arbitrum(fx) if l2 is None else l2)
    else:
        dests = None
    result = trace_funds(
        Origin(fx, user, rows, receipt),
        user,
        amount,
        start_block=block - 5,
        chain_id=1,
        destinations=dests,
        **kw,
    )
    return fx, result


def _ends(result):
    return {(t["reason"], t["address"]): t for t in result["terminals"]}


# --- Arbitrum --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, recipient, l2_tx",
    [
        (
            "arbitrum_retryable.json",
            "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9",
            "0x13a653a59b5d3ff7a8f33c9add6fbd3d115d9b7bfda39716a968ccd7bbcd5d72",
        ),
        (
            "arbitrum_unsafe_retryable.json",
            "0x973a62b07f7561003b93e8baeb34cf6aa981495c",
            "0xd2fcbd1a5ce55b7e4d6ef28fbf9894c980c98f53f669e167497b35716c47e74e",
        ),
        (
            "arbitrum_deposit_eth.json",
            "0xc4af0bebf5365082bebd6cc743c5b656b5532c7e",
            "0x3d0f11af7df34a3cee5759db6be72b18c4d40adc37219eae71528244c7c5e5d5",
        ),
    ],
)
def test_inbox_deposit_is_followed_on_arbitrum_from_the_l2_recipient(name, recipient, l2_tx):
    fx, result = _run(name)
    bridge = result["edges"][0]
    assert bridge["kind"] == "bridge" and bridge["to"] == INBOX
    info = bridge["bridge"]
    assert info["protocol"] == "arbitrum" and info["dest_chain_id"] == 42161
    assert info["state"] == bc.CREDITED
    assert info["l2_recipient"] == recipient
    assert info["fill_tx"] == l2_tx
    assert info["function"] == bc.decode_inbox_call(fx["tx"]["input"])["function"]
    (msg,) = bc.decode_inbox_messages(fx["receipt"], fx["tx_hash"], 1, TABLE)
    assert info["message_num"] == msg["message_num"]
    on_l2 = [t for t in result["terminals"] if t.get("chain_id") == 42161]
    assert on_l2, result["terminals"]
    # the L2 fees are not carried over: what reaches L2 is l2CallValue
    assert abs(sum(t["amount"] for t in on_l2) - msg["l2_call_value"] / WEI) < 1e-6
    later = [e for e in result["edges"] if e.get("chain_id") == 42161]
    if fx["l2_rows"][1:]:
        assert later and later[0]["from"] == recipient and later[0]["hop"] == 2


def test_l2_credit_not_found_ends_the_branch_but_shows_the_recipient():
    fx, result = _run("arbitrum_retryable.json", l2=Arbitrum(_fx("arbitrum_retryable.json"), []))
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum, l2 credit not found"
    assert end["address"] == INBOX and end["chain_id"] == 1
    assert end["bridge"]["state"] == bc.L2_CREDIT_NOT_FOUND
    assert end["bridge"]["l2_recipient"] == "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    assert result["edges"][0]["kind"] == "bridge"


def test_l2_lookup_failure_is_lookup_failed_and_recorded():
    fx = _fx("arbitrum_retryable.json")
    _fx_, result = _run("arbitrum_retryable.json", l2=Arbitrum(fx, fail=True))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed"
    assert end["bridge"]["state"] == "lookup_failed"
    assert end["bridge"]["l2_recipient"] == "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    assert result["error_count"] >= 1


def test_receipt_failure_is_lookup_failed():
    _fx_, result = _run("arbitrum_retryable.json", receipt=ApiError("node down"))
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed" and end["bridge"]["protocol"] == "arbitrum"


def test_arbitrum_not_readable_ends_with_the_recipient_and_chain_not_supported():
    _fx_, result = _run("arbitrum_deposit_eth.json", l2=False)
    (end,) = result["terminals"]
    recipient = "0xc4af0bebf5365082bebd6cc743c5b656b5532c7e"
    assert end["reason"] == f"bridge arbitrum (recipient {recipient}, chain not supported)"
    assert end["bridge"]["state"] == "destination_chain_not_supported"


def test_message_of_another_amount_is_not_this_transfer():
    fx = _fx("arbitrum_retryable.json")
    value = int(fx["tx"]["value"], 16) - 1
    _fx_, result = _run("arbitrum_retryable.json", rows=[_row(fx, value=value)])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"


def test_message_sent_by_someone_else_is_not_this_transfer():
    other = "0x" + "5" * 40
    _fx_, result = _run("arbitrum_retryable.json", user=other)
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"


def test_each_message_is_taken_by_one_transfer_only():
    fx = _fx("arbitrum_deposit_eth.json")
    first = _row(fx)
    second = dict(first, transactionIndex=str(int(first["transactionIndex"]) + 1))
    _fx_, result = _run(
        "arbitrum_deposit_eth.json",
        rows=[first, second],
        l2=Arbitrum(fx, fx["l2_rows"][:1]),
        amount=2 * int(first["value"]) / WEI,
    )
    states = sorted(e["bridge"]["state"] for e in result["edges"] if e["kind"] == "bridge")
    assert states == [bc.CREDITED, "not_followed"]


def test_without_destinations_the_inbox_is_named_not_followed():
    _fx_, result = _run("arbitrum_retryable.json", destinations=False)
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"


# --- ZKsync ----------------------------------------------------------------------------


def test_bridgehub_direct_shows_the_l2_contract_and_ends_unsupported():
    fx, result = _run("zksync_direct.json")
    (end,) = result["terminals"]
    recipient = "0x68f8fdb4ccc3552726a5429410edf0db9038c146"
    assert end["reason"] == f"bridge zksync (recipient {recipient}, chain not supported)"
    assert end["address"] == HUB
    info = end["bridge"]
    assert info["protocol"] == "zksync" and info["dest_chain_id"] == 1942323
    assert info["state"] == "destination_chain_not_supported"
    assert info["l2_recipient"] == recipient and info["refund_recipient"] == recipient
    assert info["function"] == "requestL2TransactionDirect"
    assert result["edges"][0]["kind"] == "bridge"


def test_bridgehub_two_bridges_shows_the_l2_receiver():
    fx, result = _run("zksync_two_bridges.json")
    (end,) = result["terminals"]
    receiver = "0x9452ed6da616783786c6d8aaf73047ecad126c57"
    assert end["reason"] == f"bridge zksync (recipient {receiver}, chain not supported)"
    assert end["bridge"]["dest_chain_id"] == 324
    assert end["bridge"]["refund_recipient"] == "0x" + "0" * 40


def test_bridgehub_call_of_another_value_is_not_this_transfer():
    fx = _fx("zksync_direct.json")
    rows = [_row(fx, value=int(fx["tx"]["value"], 16) + 1)]
    _fx_, result = _run("zksync_direct.json", rows=rows)
    (end,) = result["terminals"]
    assert end["reason"] == "bridge zksync (not followed)"


# --- reports ---------------------------------------------------------------------------

CREDITED_INFO = {
    "protocol": "arbitrum",
    "dest_chain_id": 42161,
    "state": bc.CREDITED,
    "message_num": 7,
    "l2_recipient": "0x" + "c" * 40,
    "fill_tx": "0x" + "d" * 64,
}
ZK_INFO = {
    "protocol": "zksync",
    "dest_chain_id": 2741,
    "state": "destination_chain_not_supported",
    "l2_recipient": "0x" + "e" * 40,
    "refund_recipient": "0x" + "0" * 40,
}


class _Registry:
    def network(self, chain_id):
        return {42161: get_network("arbitrum"), 1: get_network("ethereum")}.get(chain_id)


def test_bridge_text_names_the_l2_recipient_and_the_l2_transaction():
    name = {42161: "arbitrum"}.get
    assert bridge_text(CREDITED_INFO, name) == (
        f"bridge arbitrum → arbitrum, message 7, L2 recipient {'0x' + 'c' * 40}, "
        f"L2 tx {'0x' + 'd' * 64}"
    )
    assert bridge_text(CREDITED_INFO, name, fill=False).endswith("0x" + "c" * 40)
    assert bridge_text(ZK_INFO) == (
        f"bridge zksync → chain 2741, L2 recipient {'0x' + 'e' * 40}, "
        "refund recipient: the sender (zero in the call), destination_chain_not_supported"
    )
    named = dict(ZK_INFO, refund_recipient="0x" + "f" * 40)
    assert f"refund recipient {'0x' + 'f' * 40} (unused fees only)" in bridge_text(named)


def test_bridge_states_note_explains_the_canonical_bridge_ends():
    for text in ("l2 credit not found", "chain not supported", "aliased", "refund"):
        assert text in BRIDGE_STATES_NOTE


def test_html_report_links_the_l2_recipient_and_the_l2_transaction():
    fx, result = _run("arbitrum_retryable.json")
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    recipient = "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    assert "https://arbiscan.io/address/" + recipient in html
    assert "https://arbiscan.io/tx/" + result["edges"][0]["bridge"]["fill_tx"] in html
    assert ">L2 tx</a>" in html


def test_html_report_shows_the_recipient_of_an_unconfirmed_credit():
    fx = _fx("arbitrum_retryable.json")
    _fx_, result = _run("arbitrum_retryable.json", l2=Arbitrum(fx, []))
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    assert "bridge arbitrum, l2 credit not found" in html
    assert html.count("https://arbiscan.io/address/0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9") >= 2


def test_html_report_shows_a_zksync_recipient_in_full():
    _fx_, result = _run("zksync_direct.json")
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    assert "L2 recipient 0x68f8fdb4ccc3552726a5429410edf0db9038c146" in html


def test_terminal_line_of_a_named_end_lists_the_recipients():
    end = {"reason": "bridge arbitrum, l2 credit not found", "bridge": CREDITED_INFO}
    assert terminal_bridge_text(end, _Registry()) == f"L2 recipient {'0x' + 'c' * 40}"
    stargate = {"protocol": "stargate", "dest_chain_id": None, "state": "not_followed"}
    assert (
        terminal_bridge_text({"reason": "bridge stargate (not followed)", "bridge": stargate}) == ""
    )


def test_cli_prints_the_l2_recipient(monkeypatch, args, capsys):  # noqa: F811
    fx = _fx("arbitrum_retryable.json")
    _fx_, result = _run("arbitrum_retryable.json", l2=Arbitrum(fx, []))
    _stub(monkeypatch, trace_funds=lambda *a, **kw: result, load_attribution=lambda *a, **kw: {})
    cli.cmd_trace(
        args(address=fx["tx"]["from"], amount=0.1, token="", start_block=0, max_hops=4, json="")
    )
    out = capsys.readouterr().out
    assert "[bridge arbitrum, l2 credit not found]  L2 recipient 0x29e1506c" in out
    assert "L2 recipient 0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9" in out


def test_json_export_carries_the_l2_recipient():
    _fx_, result = _run("zksync_two_bridges.json")
    data = json.loads(json.dumps(result))
    info = data["terminals"][0]["bridge"]
    assert info["l2_recipient"] == "0x9452ed6da616783786c6d8aaf73047ecad126c57"
    assert info["refund_recipient"] == "0x" + "0" * 40


def test_webui_links_the_l2_recipient(monkeypatch):
    pytest.importorskip("flask")
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
    )
    import app as webapp

    fx = _fx("arbitrum_retryable.json")
    _fx_, result = _run("arbitrum_retryable.json", l2=Arbitrum(fx, []))

    class Fake(_Registry):
        def __init__(self, *_a, **_k):
            pass

        def __call__(self, chain):
            return None

    monkeypatch.setattr(webapp, "Destinations", Fake)
    monkeypatch.setattr(webapp, "trace_funds", lambda *a, **kw: result)
    monkeypatch.setattr(webapp, "load_attribution", lambda name: {})
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: lambda a: None)

    class Client:
        api_key = "k"

    params = {"amount": 0.1, "start_block": 0, "max_hops": 4, "token": None}
    view, _csv, _reports = webapp._run_trace(
        Client(), fx["tx"]["from"], get_network("ethereum"), params
    )
    recipient = "0x29e1506ce9a71663b2280e2f6e77cd87815e4ce9"
    link = {
        "label": "L2 recipient",
        "address": recipient,
        "url": "https://arbiscan.io/address/" + recipient,
    }
    assert link in view["edges"][0]["bridge_recipients"]
    assert link in view["terminals"][0]["bridge_recipients"]
    # the page shows the same report body as the HTML report, recipient linked
    assert f'href="{link["url"]}"' in view["report_body"]
    assert f">{recipient}</a>" in view["report_body"]


# --- synthetic receipts: ties and claimed credits ---------------------------------------

GATEWAY_ROUTER = "0x72ce9c846789fdb6fc1f34ac4ad25dd9ef7031ef"
L2_GATEWAY = "0x" + "9a" * 20
EOA = "0x" + "e1" * 20
L1_TS = 1_800_000_000


def _native(frm, to, value, h, block, index=0, ts=L1_TS):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "blockNumber": str(block),
        "timeStamp": str(ts),
        "hash": h,
        "input": "0x",
        "isError": "0",
        "transactionIndex": str(index),
    }


class Synth:
    """Origin chain over synthetic rows and receipts."""

    def __init__(self, user, rows, receipts):
        self.user, self.rows, self.receipts = user, rows, receipts

    def token_transfers(self, addr, contract=None, **kw):
        return []

    def outgoing_txs(self, addr):
        return list(self.rows) if addr == self.user else []

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        return self.receipts.get(tx_hash)


def _l2(rows):
    return Arbitrum({"dest_window": [1, 2], "l2_rows": rows})


def _retryable(to, call_value, deposit, refund=EOA):
    words = [int(to, 16), call_value, deposit, 1, int(refund, 16), int(refund, 16), 1, 1, 0]
    return "".join(f"{w:064x}" for w in words)


def _synth_run(user, rows, receipts, amount, l2_rows):
    return trace_funds(
        Synth(user, rows, receipts),
        user,
        amount,
        start_block=90,
        chain_id=1,
        destinations=_dests(_l2(l2_rows)),
    )


def test_gateway_fee_retryable_is_not_an_l2_recipient():
    # outboundTransfer: the router forwards the fee into a retryable to the L2 gateway
    # with l2CallValue 0; the real recipient is inside the gateway calldata
    fee = 3 * 10**15
    payload = _retryable(L2_GATEWAY, 0, fee)
    rc = _inbox_receipt(EOA, GATEWAY_ROUTER, 5, bc.KIND_RETRYABLE, payload)
    rows = [_native(EOA, GATEWAY_ROUTER, fee, "0xa1", 100)]
    result = _synth_run(EOA, rows, {"0xa1": rc}, fee / WEI, [])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"
    assert "l2_recipient" not in end["bridge"]


def test_message_of_another_l1_sender_is_not_tied_by_the_transaction_sender():
    value = 10**18
    router = "0x" + "77" * 20
    rc = _inbox_receipt(
        EOA, router, 6, bc.KIND_RETRYABLE, _retryable("0x" + "c2" * 20, value, value)
    )
    rows = [_native(EOA, bc.ARBITRUM_INBOX, value, "0xa2", 100)]
    result = _synth_run(EOA, rows, {"0xa2": rc}, 1.0, [])
    (end,) = result["terminals"]
    assert end["reason"] == "bridge arbitrum (not followed)"


def _two_deposits(credits):
    value = 10**18
    rows, receipts = [], {}
    for i, h in enumerate(("0xb1", "0xb2")):
        rows.append(_native(EOA, bc.ARBITRUM_INBOX, value, h, 100 + i, ts=L1_TS + 60 * i))
        payload = EOA[2:] + f"{value:064x}"
        receipts[h] = _inbox_receipt(EOA, EOA, 40 + i, bc.KIND_ETH_DEPOSIT, payload)
    l2_rows = [
        _native(bc.l1_to_l2_alias(EOA), EOA, value, h, 5000 + i, ts=L1_TS + 600 + 60 * i)
        for i, h in enumerate(credits)
    ]
    return _synth_run(EOA, rows, receipts, 2.0, l2_rows)


def test_one_l2_credit_is_not_claimed_by_two_messages():
    result = _two_deposits(["0xc1"])
    states = sorted(
        (e["bridge"].get("message_num"), e["bridge"]["state"], e["bridge"].get("fill_tx") or "")
        for e in result["edges"]
        if e["kind"] == "bridge"
    )
    assert states == [(40, bc.CREDITED, "0xc1"), (41, bc.L2_CREDIT_NOT_FOUND, "")]


def test_two_identical_messages_take_two_distinct_credits():
    result = _two_deposits(["0xc1", "0xc2"])
    fills = [
        (e["bridge"]["message_num"], e["bridge"]["fill_tx"])
        for e in result["edges"]
        if e["kind"] == "bridge"
    ]
    assert sorted(fills) == [(40, "0xc1"), (41, "0xc2")]


def test_a_contract_that_called_deposit_eth_is_followed_at_its_aliased_address():
    safe, owner, value = "0x" + "5a" * 20, "0x" + "0e" * 20, 10**18
    dest = bc.l1_to_l2_alias(safe)
    rc = _inbox_receipt(owner, safe, 50, bc.KIND_ETH_DEPOSIT, dest[2:] + f"{value:064x}")
    rows = [_native(safe, bc.ARBITRUM_INBOX, value, "0xd1", 100)]
    credit = _native(dest, dest, value, "0xd2", 5000, ts=L1_TS + 600)
    result = _synth_run(safe, rows, {"0xd1": rc}, 1.0, [credit])
    info = result["edges"][0]["bridge"]
    assert info["state"] == bc.CREDITED and info["l2_recipient"] == dest
    assert info["fill_tx"] == "0xd2"
