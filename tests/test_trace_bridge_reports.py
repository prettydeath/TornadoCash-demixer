"""Bridge protocol, destination chain and state in the CLI, HTML report, web UI and JSON."""

import json
import os
import sys

import pytest

from tests.fixtures import demix_result as fx
from tests.test_cli import _stub, args, networks_csv  # noqa: F401  (pytest fixtures)
from tests.test_trace_bridge_states import LIFI, _op_run
from tornado_demix import cli
from tornado_demix.networks import get_network
from tornado_demix.report_html import build_trace_report
from tornado_demix.trace import BRIDGE_STATES_NOTE, END_STATES_NOTE, bridge_text

STARGATE = "0xc026395860db2d07ee33e05fe50ed7bd583189c7"
SPOKE = "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5"
WAITING = {"protocol": "across", "dest_chain_id": 10, "deposit_id": 7, "state": "fill_not_found"}
NAMED = {"protocol": "stargate", "dest_chain_id": None, "state": "not_followed", "role": "x"}


def _edge(to, info):
    return {
        "hop": 1,
        "from": fx.BOB,
        "to": to,
        "tx_hash": "0x" + "ab" * 32,
        "ts": 0,
        "asset": "ETH",
        "value": 1.0,
        "attributed": 1.0,
        "lower": 1.0,
        "upper": 1.0,
        "kind": "bridge",
        "bridge": dict(info),
        "chain_id": 1,
    }


def _end(to, info, reason):
    return {
        "address": to,
        "asset": "ETH",
        "amount": 1.0,
        "hop": 1,
        "reason": reason,
        "label": None,
        "chain_id": 1,
        "bridge": dict(info),
    }


ENDED = {
    "start": fx.BOB,
    "amount": 2.0,
    "asset": "ETH",
    "edges": [_edge(SPOKE, WAITING), _edge(STARGATE, NAMED)],
    "terminals": [
        _end(SPOKE, WAITING, "fill_not_found"),
        _end(STARGATE, NAMED, "bridge stargate (not followed)"),
    ],
    "nodes_expanded": 1,
}


class _Registry:
    def network(self, chain_id):
        return {10: get_network("optimism"), 1: get_network("ethereum")}.get(chain_id)


def test_bridge_text_names_protocol_chain_and_state():
    name = {10: "optimism"}.get
    assert bridge_text(WAITING, name) == "bridge across → optimism, deposit id 7, fill_not_found"
    assert bridge_text(NAMED) == "bridge stargate, not followed"
    filled = dict(WAITING, state="filled", fill_tx="0xf", via=LIFI)
    assert bridge_text(filled, name, arrow="->") == (
        f"bridge across -> optimism, deposit id 7, via {LIFI}, fill 0xf"
    )
    assert bridge_text(filled, name, fill=False).endswith(f"via {LIFI}")
    assert bridge_text(WAITING, name, state=False) == "bridge across → optimism, deposit id 7"


def test_end_states_note_explains_the_bridge_states():
    for state in (
        "fill_not_found",
        "deposit_expired_possible_refund",
        "destination_chain_not_supported",
        "destination_pool_not_listed",
        "(not followed)",
    ):
        assert state in BRIDGE_STATES_NOTE
    assert BRIDGE_STATES_NOTE in END_STATES_NOTE


def test_html_report_shows_protocol_chain_and_state_for_edges_and_end_points():
    html = build_trace_report(ENDED, get_network("ethereum"), _Registry())
    assert "bridge across → optimism, deposit id 7, fill_not_found" in html
    assert "bridge stargate, not followed" in html
    assert "bridge stargate (not followed)" in html  # the end reason
    assert "fill not found" not in html  # the old catch-all text is gone


def test_html_report_of_a_filled_deposit_links_the_fill_and_names_the_way_in():
    fx_op, result = _op_run()
    html = build_trace_report(result, get_network("ethereum"), _Registry())
    assert "bridge across → optimism, deposit id 4601060, via " + LIFI in html
    assert "optimistic.etherscan.io/tx/" + fx_op["fills"][0]["transactionHash"] in html


def test_cli_prints_protocol_chain_and_state(monkeypatch, args, capsys):  # noqa: F811
    _stub(monkeypatch, trace_funds=lambda *a, **kw: ENDED, load_attribution=lambda *a, **kw: {})
    cli.cmd_trace(args(address=fx.BOB, amount=2.0, token="", start_block=0, max_hops=4, json=""))
    out = capsys.readouterr().out
    # the test registry has no Optimism row: the chain is named by its id
    assert "-> bridge across -> chain 10, deposit id 7, fill_not_found  0x" in out
    assert "[fill_not_found]  bridge across -> chain 10, deposit id 7\n" in out
    assert "[bridge stargate (not followed)]" in out
    assert "fill not found" not in out


def test_json_export_carries_the_bridge_state_of_edges_and_end_points():
    _fx, result = _op_run()
    data = json.loads(json.dumps(result))
    assert data["edges"][0]["bridge"]["state"] == "filled"
    assert data["edges"][0]["bridge"]["dest_chain_id"] == 10


def test_webui_shows_protocol_chain_and_state(monkeypatch):
    pytest.importorskip("flask")
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
    )
    import app as webapp

    class Fake(_Registry):
        def __init__(self, *_a, **_k):
            pass

        def __call__(self, chain):
            return None

    monkeypatch.setattr(webapp, "Destinations", Fake)
    monkeypatch.setattr(webapp, "trace_funds", lambda *a, **kw: ENDED)
    monkeypatch.setattr(webapp, "load_attribution", lambda name: {})
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: lambda a: None)

    class Client:
        api_key = "k"

    params = {"amount": 2.0, "start_block": 0, "max_hops": 4, "token": None}
    view, (header, rows), reports = webapp._run_trace(
        Client(), fx.BOB, get_network("ethereum"), params
    )
    assert view["edges"][0]["bridge_text"] == (
        "bridge across → optimism, deposit id 7, fill_not_found"
    )
    assert view["edges"][1]["bridge_text"] == "bridge stargate, not followed"
    assert view["terminals"][0]["bridge_text"] == "bridge across → optimism, deposit id 7"
    assert view["terminals"][1]["bridge_text"] == ""  # the reason already names it
    assert rows[0][header.index("bridge")] == view["edges"][0]["bridge_text"]
    assert view["end_states_note"] == END_STATES_NOTE
    # the page shows the report body: protocol, chain, deposit id and state
    body = view["report_body"]
    assert "bridge across → optimism, deposit id 7, fill_not_found" in body
    assert "bridge stargate, not followed" in body
    assert "fill not found" not in body


def test_an_end_point_of_several_deposits_lists_each_of_them():
    from tornado_demix.report_html import terminal_bridge_text

    other = dict(WAITING, deposit_id=8)
    end = dict(_end(SPOKE, WAITING, "fill_not_found"), bridges=[WAITING, other])
    text = terminal_bridge_text(end, _Registry())
    assert text == "bridge across → optimism, deposit id 7; bridge across → optimism, deposit id 8"
    html = build_trace_report(dict(ENDED, terminals=[end]), get_network("ethereum"), _Registry())
    assert "deposit id 7; bridge across → optimism, deposit id 8" in html
