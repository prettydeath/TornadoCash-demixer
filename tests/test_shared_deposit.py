"""The shared_deposit signal: wiring in the heuristics, run_demix, CLI, report and web UI."""

import argparse
import json
import os
import sys

import pytest

from tests.fixtures import demix_result as fx
from tests.test_demix import RELAYER, _FakeClient, _recipient, _tx, _wlog
from tornado_demix import cli
from tornado_demix.demix import run_demix
from tornado_demix.heuristics import (
    LINKED_SIGNALS,
    METHOD_FAMILY,
    SIGNAL_LABEL,
    SIGNAL_WEIGHTS,
    apply_heuristics,
    candidate_reason,
    confidence_band,
    method_breakdown,
    ranked_candidates,
)
from tornado_demix.report_html import build_html_report

DEPOSIT = "0x" + "c" * 40
HOT = "0x" + "d" * 40
DEPOSIT_ROW = {
    "address": DEPOSIT,
    "sweep_target": HOT,
    "sweep_targets": [HOT],
    "exchange": "binance",
    "senders": [fx.WALLET, fx.BOB],
}


def _row(data, address):
    return next(r for r in ranked_candidates(data) if r["address"] == address)


# --- signal wiring -----------------------------------------------------------


def test_the_signal_is_a_linked_address_signal_with_the_linked_weight():
    assert METHOD_FAMILY["shared_deposit"] == "linked address"
    assert "shared_deposit" in LINKED_SIGNALS
    assert SIGNAL_WEIGHTS["shared_deposit"] == SIGNAL_WEIGHTS["linked"] == 0.40
    assert SIGNAL_LABEL["shared_deposit"] == "Shared exchange deposit address"


def test_a_shared_deposit_alone_is_a_moderate_band_and_admits_the_recipient():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    row = _row(data, fx.BOB)
    assert row["signals"] == ["shared_deposit"]
    assert row["band"] == "moderate"
    assert confidence_band({"shared_deposit"}) == "moderate"


def test_a_shared_deposit_with_a_count_match_is_strong():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.ALICE: [DEPOSIT]})
    row = _row(data, fx.ALICE)
    assert {"count_match", "shared_deposit"} <= set(row["signals"])
    assert row["band"] == "strong"


def test_the_signal_is_recorded_per_pool_where_the_recipient_appears():
    data = fx.two_pool_result()  # ALICE receives in both pools
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.ALICE: [DEPOSIT]})
    assert sorted(data["heuristics"]["shared_deposits"]) == [
        ("0.1 ETH", fx.ALICE, [DEPOSIT]),
        ("1 ETH", fx.ALICE, [DEPOSIT]),
    ]


def test_the_depositor_itself_never_earns_a_shared_deposit():
    data = fx.two_pool_result()
    data["wallet"] = fx.ALICE
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.ALICE: [DEPOSIT]})
    assert data["heuristics"]["shared_deposits"] == []


def test_without_hits_no_signal_and_no_claim_in_the_evidence():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    row = _row(data, fx.BOB)
    assert "shared_deposit" not in row["signals"]
    line = next(e for e in row["evidence"] if e["signal"] == "shared_deposit")
    assert line["holds"] is False
    assert "same exchange deposit address" not in line["detail"]
    assert "same exchange deposit address" not in candidate_reason(row)
    assert data["heuristics"]["shared_deposits"] == []


def test_the_evidence_text_names_the_address_and_where_it_is_swept():
    data = fx.two_pool_result()
    data["deposit_addresses"] = [DEPOSIT_ROW]
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    row = _row(data, fx.BOB)
    line = next(e for e in row["evidence"] if e["signal"] == "shared_deposit")
    assert line["holds"] is True and line["family"] == "linked address"
    assert "sent funds to the same exchange deposit address as the depositor" in line["detail"]
    assert DEPOSIT[:10] in line["detail"] and "swept to binance" in line["detail"]
    assert "same exchange deposit address as the depositor" in candidate_reason(row)


def test_an_unlabelled_sweep_target_reads_as_a_hot_wallet():
    data = fx.two_pool_result()
    data["deposit_addresses"] = [dict(DEPOSIT_ROW, exchange="")]
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    line = next(e for e in _row(data, fx.BOB)["evidence"] if e["signal"] == "shared_deposit")
    assert "swept to a hot wallet" in line["detail"]


def test_the_method_breakdown_lists_the_recipient_under_the_signal():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    group = next(m for m in method_breakdown(data) if m["method"] == "shared_deposit")
    assert group["label"] == "Shared exchange deposit address"
    assert {r["address"] for r in group["rows"]} == {fx.BOB}


# --- run_demix ----------------------------------------------------------------


SHARER = _recipient(3)


class _DepositClient(_FakeClient):
    """A depositor that also sent funds to an exchange deposit address."""

    def __init__(self, txs, logs):
        super().__init__(txs, logs)
        self.calls = []
        self.lists = {
            ("txlist", DEPOSIT): [
                {"from": fx.WALLET, "to": DEPOSIT, "hash": "0x1"},
                {"from": SHARER, "to": DEPOSIT, "hash": "0x2"},
                {"from": DEPOSIT, "to": HOT, "hash": "0x3"},
                {"from": DEPOSIT, "to": HOT, "hash": "0x4"},
            ]
        }

    def call(self, params):
        self.calls.append((params["action"], params["address"]))
        return self.lists.get((params["action"], params["address"]), [])

    def has_at_least_txs(self, address, n):
        return address == HOT


def _setup():
    deposits = [
        _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xd0"),
        _tx(fx.POOL_1_ETH, 1.0, 1010, 11, "0xd1"),
        _tx(DEPOSIT, 0.5, 900, 9, "0xto-exchange"),
    ]
    logs = [_wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15) for i in range(2, 22)]
    return deposits, logs


def test_run_demix_passes_the_hits_and_stores_the_deposit_addresses():
    client = _DepositClient(*_setup())
    labels = {HOT: {"category": "exchange", "entity": "binance"}}
    data = run_demix(client, fx.WALLET, network=fx.network(), labels=labels)
    assert [d["address"] for d in data["deposit_addresses"]] == [DEPOSIT]
    json.dumps(data["deposit_addresses"])  # serialisable
    row = _row(data, SHARER)
    assert "shared_deposit" in row["signals"] and row["band"] == "moderate"
    assert data["heuristics"]["shared_deposits"] == [("1 ETH", SHARER, [DEPOSIT])]
    line = next(e for e in row["evidence"] if e["signal"] == "shared_deposit")
    assert "swept to binance" in line["detail"]


def test_run_demix_can_turn_the_lookup_off():
    client = _DepositClient(*_setup())
    data = run_demix(client, fx.WALLET, network=fx.network(), labels={}, deposit_addresses=False)
    assert data["deposit_addresses"] == []
    assert client.calls == []
    assert all(
        "shared_deposit" not in s for r in data["denoms"].values() for s in r["signals"].values()
    )


def test_the_early_return_without_deposits_does_not_look_up_addresses():
    client = _DepositClient([_tx(DEPOSIT, 0.5, 900, 9, "0xto-exchange")], [])
    data = run_demix(client, fx.WALLET, network=fx.network())
    assert data["deposits"] == [] and client.calls == []


# --- CLI ------------------------------------------------------------------------


def test_the_no_deposit_addresses_flag_is_parsed_for_demix_and_multi():
    parser = cli.build_parser()
    assert parser.parse_args(["demix", fx.WALLET]).no_deposit_addresses is False
    assert parser.parse_args(["demix", fx.WALLET, "--no-deposit-addresses"]).no_deposit_addresses
    assert parser.parse_args(["multi", fx.WALLET, "--no-deposit-addresses"]).no_deposit_addresses


def _demix_args(**over):
    base = dict(
        api_csv=None,
        network="ethereum",
        networks_csv=None,
        wallet=fx.WALLET,
        out_dir="",
        report="",
        json="",
        window_days=30,
        exit_window=None,
        fee_lo=0.9,
        fee_hi=0.995,
        gap_hours=24,
        mode="events",
    )
    base.update(over)
    return argparse.Namespace(**base)


def _stub_cli(monkeypatch, data, seen):
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "_network", lambda args: fx.network())

    def fake(*a, **kw):
        seen.update(kw)
        return data

    monkeypatch.setattr(cli, "run_demix", fake)


def test_cmd_demix_passes_the_flag_and_prints_the_deposit_block(monkeypatch, capsys):
    data = fx.two_pool_result()
    data["deposit_addresses"] = [DEPOSIT_ROW]
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    seen = {}
    _stub_cli(monkeypatch, data, seen)
    cli.cmd_demix(_demix_args())
    out = capsys.readouterr().out
    assert seen["deposit_addresses"] is True
    assert "[deposit] 1 exchange deposit address(es)" in out
    assert f"{DEPOSIT}  swept to binance; 2 sender(s)" in out
    assert f"{fx.BOB} via {DEPOSIT}" in out

    cli.cmd_demix(_demix_args(no_deposit_addresses=True))
    assert seen["deposit_addresses"] is False


def test_cmd_demix_prints_no_deposit_block_when_none_were_found(monkeypatch, capsys):
    data = fx.two_pool_result()
    data["deposit_addresses"] = []
    apply_heuristics(data, counterparties=set())
    _stub_cli(monkeypatch, data, {})
    cli.cmd_demix(_demix_args())
    assert "[deposit]" not in capsys.readouterr().out


# --- HTML report -----------------------------------------------------------------


def test_the_html_report_lists_deposit_addresses_only_when_found():
    data = fx.two_pool_result()
    data["deposit_addresses"] = [DEPOSIT_ROW]
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    html = build_html_report(data, fx.network())
    assert "Exchange deposit addresses" in html
    assert DEPOSIT in html and "binance" in html
    assert "same exchange deposit address as the depositor" in html
    assert "Shared exchange deposit address" in html or "shared exchange deposit address" in html

    empty = fx.two_pool_result()
    empty["deposit_addresses"] = []
    apply_heuristics(empty, counterparties={fx.BOB})
    assert "Exchange deposit addresses" not in build_html_report(empty, fx.network())


# --- web UI ----------------------------------------------------------------------

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")
)
import app as webapp  # noqa: E402


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: object())
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = "tok"
    return client


def test_the_form_has_the_deposit_address_checkbox_on_by_default(web):
    html = web.get("/").get_data(as_text=True)
    assert 'name="deposit_addresses"' in html
    box = html[html.index('id="deposit_addresses"') :].split(">", 1)[0]
    assert "checked" in box


@pytest.mark.parametrize("form,expected", [({"deposit_addresses": "1"}, True), ({}, False)])
def test_the_checkbox_reaches_run_demix(web, monkeypatch, form, expected):
    seen = {}

    def fake(*a, **kw):
        seen.update(kw)
        return fx.two_pool_result()

    monkeypatch.setattr(webapp, "run_demix", fake)
    web.post("/", data={"wallets": fx.WALLET, "analysis": "demix", "csrf": "tok", **form})
    assert seen["deposit_addresses"] is expected


def test_the_result_page_shows_the_deposit_addresses(web, monkeypatch):
    data = fx.two_pool_result()
    data["deposit_addresses"] = [DEPOSIT_ROW]
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEPOSIT]})
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **kw: data)
    body = web.post("/", data={"wallets": fx.WALLET, "analysis": "demix", "csrf": "tok"}).get_data(
        as_text=True
    )
    assert "Exchange deposit addresses</h4>" in body
    assert DEPOSIT in body and "binance" in body and fx.BOB in body
