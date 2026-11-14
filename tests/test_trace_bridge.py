"""A trace that meets an Across deposit continues on the destination chain."""

import json
import os

from tornado_demix.trace import trace_funds

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "bridges")
with open(os.path.join(FIXTURES, "across_new_arbitrum.json"), encoding="utf-8") as fh:
    FX = json.load(fh)

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ARB_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
USER = "0xb9f84f474e178aee1434c854337a2d9b2d8a48fa"  # depositor and recipient in the fixture
SPOKE = "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5"
EXCHANGE = "0x" + "8" * 40
BLOCK = int(FX["receipt"]["blockNumber"], 16)


def _token_row(frm, to, units, block, h, token, ts, symbol):
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
        "tokenDecimal": "6",
        "tokenSymbol": symbol,
    }


class Source:
    def token_transfers(self, addr, contract=None, **kw):
        row = _token_row(USER, SPOKE, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"], "USDC")
        return [row] if addr in (USER, SPOKE) and contract in (None, USDC) else []

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return []

    def tx_receipt(self, tx_hash):
        return FX["receipt"] if tx_hash == FX["tx_hash"] else None


class Arbitrum:
    def __init__(self, fills=True):
        self.fills = fills

    def block_by_time(self, ts, closest="before"):
        return FX["dest_window"][0 if closest == "before" else 1]

    def get_logs(self, address, topic0, start, end, topics=None):
        if not self.fills:
            return []
        return [r for r in FX["fills"] if r["topics"][0] == topic0 and r["address"] == address]

    def token_transfers(self, addr, contract=None, **kw):
        row = _token_row(
            USER, EXCHANGE, 3261266981, 410030000, "0xdd", ARB_USDC, 1765584500, "USDC"
        )
        return [row] if addr == USER and contract in (None, ARB_USDC) else []

    def outgoing_txs(self, addr):
        return []

    def internal_txs(self, addr):
        return []


def _dest(chain, fills=True):
    if chain != 42161:
        return None
    return {
        "client": Arbitrum(fills),
        "labels": {EXCHANGE: {"entity": "Some exchange"}},
        "currency": "ETH",
        "is_contract": None,
    }


def _run(destinations, **kw):
    return trace_funds(
        Source(),
        USER,
        3261.597488,
        start_block=BLOCK - 5,
        token=USDC,
        chain_id=1,
        destinations=destinations,
        **kw,
    )


def test_trace_follows_an_across_deposit_to_the_destination_chain():
    result = _run(_dest)
    bridge, after = result["edges"]
    assert bridge["kind"] == "bridge" and bridge["chain_id"] == 1 and bridge["hop"] == 1
    assert bridge["bridge"]["protocol"] == "across"
    assert bridge["bridge"]["dest_chain_id"] == 42161
    assert bridge["bridge"]["deposit_id"] == 3497136
    assert bridge["bridge"]["fill_tx"] == FX["fills"][0]["transactionHash"]
    # the walk goes on from the fill recipient, on Arbitrum, with the hop count continuing
    assert after["chain_id"] == 42161 and after["hop"] == 2
    assert after["from"] == USER and after["to"] == EXCHANGE
    (end,) = result["terminals"]
    assert end["chain_id"] == 42161 and end["reason"] == "labelled address"
    assert end["label"]["entity"] == "Some exchange"
    # the amount that arrived is the fill's output, not the deposit's input
    assert abs(after["attributed"] - 3261.266981) < 1e-6


def test_missing_fill_stops_with_the_bridge_reason():
    # the fixture's fill deadline (2025) has passed: the deposit may have been refunded
    result = _run(lambda chain: _dest(chain, fills=False))
    (edge,) = result["edges"]
    assert edge["kind"] == "bridge" and "fill_tx" not in edge["bridge"]
    (end,) = result["terminals"]
    assert end["reason"] == "deposit_expired_possible_refund"
    assert end["address"] == SPOKE


def test_unsupported_destination_chain_is_named():
    result = _run(lambda chain: None)
    (end,) = result["terminals"]
    assert end["reason"] == "destination_chain_not_supported"
    assert end["bridge"]["dest_chain_id"] == 42161


def test_without_destinations_the_old_contract_rule_applies():
    result = trace_funds(Source(), USER, 3261.597488, start_block=BLOCK - 5, token=USDC)
    (end,) = result["terminals"]
    assert end["reason"] == "contract without a payment back"
    assert "chain_id" not in result["edges"][0]


class _Registry:
    """Stand-in for bridges.Destinations: chain id -> registry entry."""

    def __init__(self):
        from tornado_demix.networks import get_network

        self.nets = {42161: get_network("arbitrum"), 1: get_network("ethereum")}

    def network(self, chain_id):
        return self.nets.get(chain_id)


def test_report_shows_the_bridge_hop_and_the_destination_chain():
    from tornado_demix.networks import get_network
    from tornado_demix.report_html import build_trace_report

    result = _run(_dest)
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    assert "bridge across" in html and "deposit id 3497136" in html
    assert "arbitrum" in html
    assert "arbiscan.io/tx/" + FX["fills"][0]["transactionHash"] in html
    # an edge on the destination chain links to that chain's explorer
    assert "arbiscan.io/address/" + USER in html


def test_report_without_registry_still_renders_a_bridge_edge():
    from tornado_demix.networks import get_network
    from tornado_demix.report_html import build_trace_report

    html = build_trace_report(_run(_dest), get_network("ethereum"))
    assert "chain 42161" in html


def test_webui_trace_view_names_the_chain_and_links_the_fill(monkeypatch):
    import sys

    pytest = __import__("pytest")
    pytest.importorskip("flask")
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
    )
    import app as webapp

    registry = _Registry()

    class Fake(_Registry):
        def __init__(self, *_a, **_k):
            super().__init__()

        def __call__(self, chain):
            return _dest(chain)

    monkeypatch.setattr(webapp, "Destinations", Fake)
    monkeypatch.setattr(webapp, "load_attribution", lambda name: {})
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: lambda a: None)
    src = Source()
    src.api_key = "k"
    params = {"amount": 3261.597488, "start_block": BLOCK - 5, "max_hops": 4, "token": USDC}
    view, (header, rows), reports = webapp._run_trace(src, USER, registry.nets[1], params)
    kinds = [e["kind"] for e in view["edges"]]
    assert kinds == ["bridge", "transfer"]
    assert view["edges"][0]["bridge_to"] == "arbitrum"
    assert "arbiscan.io/tx/" in view["edges"][0]["bridge_fill_url"]
    assert view["edges"][1]["chain"] == "arbitrum"
    assert view["terminals"][0]["chain"] == "arbitrum"
    assert rows[1][header.index("chain")] == "arbitrum"


def test_destination_lookup_error_ends_only_that_branch():
    from tornado_demix.errors import BlockLookupError

    class Failing(Arbitrum):
        def block_by_time(self, ts, closest="before"):
            raise BlockLookupError("could not resolve timestamp 1 to a block")

    result = _run(lambda chain: dict(_dest(chain), client=Failing()) if chain == 42161 else None)
    (end,) = result["terminals"]
    assert end["reason"] == "lookup_failed"
    assert end["bridge"]["state"] == "lookup_failed"
    assert "could not resolve" in result["errors"][0]["error"]
    assert result["edges"][0]["kind"] == "bridge"


def test_a_rejected_api_key_still_aborts_the_trace():
    import pytest

    from tornado_demix.errors import ApiKeyError

    class Rejected(Arbitrum):
        def block_by_time(self, ts, closest="before"):
            raise ApiKeyError("Invalid API Key")

    with pytest.raises(ApiKeyError):
        _run(lambda chain: dict(_dest(chain), client=Rejected()) if chain == 42161 else None)


def test_a_call_to_a_pool_with_no_decodable_deposit_ends_the_branch_by_name():
    class NoEvents(Source):
        def tx_receipt(self, tx_hash):
            return dict(FX["receipt"], logs=[])

    result = trace_funds(
        NoEvents(),
        USER,
        3261.597488,
        start_block=BLOCK - 5,
        token=USDC,
        chain_id=1,
        destinations=_dest,
    )
    (end,) = result["terminals"]
    assert end["reason"] == "bridge across, deposit event not decoded"


def test_v2_pool_is_named_as_not_decoded():
    from tornado_demix.bridges import load_bridges

    v2 = "0x4d9079bb4165aeb4084c526a32695dcfd2f77381"
    assert load_bridges()[(1, v2)]["role"].lower().count("v2")

    class ToV2(Source):
        def token_transfers(self, addr, contract=None, **kw):
            row = _token_row(USER, v2, 3261597488, BLOCK, FX["tx_hash"], USDC, FX["ts"], "USDC")
            return [row] if addr == USER else []

        def tx_receipt(self, tx_hash):
            return dict(FX["receipt"], logs=[])

    result = trace_funds(
        ToV2(), USER, 3261.597488, start_block=BLOCK - 5, token=USDC, chain_id=1, destinations=_dest
    )
    assert (
        result["terminals"][0]["reason"]
        == "bridge across, deposit event not decoded (v2 not decoded)"
    )


def test_a_fill_stamped_before_its_deposit_is_still_followed():
    class Skewed(Arbitrum):
        def get_logs(self, address, topic0, start, end, topics=None):
            logs = super().get_logs(address, topic0, start, end, topics)
            return [dict(log, timeStamp=hex(FX["ts"] - 100)) for log in logs]

    result = _run(lambda chain: dict(_dest(chain), client=Skewed()) if chain == 42161 else None)
    bridge, after = result["edges"]
    assert bridge["kind"] == "bridge" and after["chain_id"] == 42161
    assert abs(after["attributed"] - 3261.266981) < 1e-6
    assert after["lower"] <= after["attributed"] <= after["upper"]
