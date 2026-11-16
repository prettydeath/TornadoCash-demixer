"""classify-address in the CLI, the HTML report and the web UI; characterize shows the
deposit-address check of every address it lists. Synthetic addresses only."""

import json
import os

import pytest

from tornado_demix import cli
from tornado_demix.characterize import characterize_address, run_classify
from tornado_demix.networks import Network
from tornado_demix.pools import Pool
from tornado_demix.report_html import build_characterize_report, build_classify_report

W = "0x" + "a" * 40
X = "0x" + "c" * 40  # deposit address
HOT = "0x" + "d" * 40
USDC = "0x" + "1" * 40
POOL = "0x" + "2" * 40
LAB = {HOT: {"category": "exchange", "entity": "Binance", "source": "attr"}}


def _row(frm, to, value, ts, h, contract=None):
    row = {
        "from": frm,
        "to": to,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 10),
        "hash": h,
    }
    if contract:
        row["contractAddress"] = contract
    return row


def _lists():
    txl = [_row(X, USDC, 0, 1600 + i, f"0xsw{i}") for i in range(3)]
    tkl = []
    for i in range(3):
        tkl.append(_row(W, X, 10**8, 1000 + i, f"0xin{i}", USDC))
        tkl.append(_row(X, HOT, 10**8, 1600 + i, f"0xsw{i}", USDC))
    return {("txlist", X): txl, ("tokentx", X): tkl}


class Stub:
    def __init__(self, lists):
        self.lists, self.calls = lists, []

    def call(self, p):
        self.calls.append((p["action"], p["address"]))
        return self.lists.get((p["action"], p["address"]), [])

    def has_at_least_txs(self, a, n):
        return False

    def outgoing_txs(self, a):
        return self.lists.get(("txlist", a), [])

    def internal_txs(self, a):
        return []


def _net():
    return Network("polygon", 137, "MATIC", [Pool(POOL, 100.0, "MATIC", 18, None)])


def eoa(_a):
    return False


def test_run_classify_returns_the_kind_errors_and_network():
    info = run_classify(Stub(_lists()), X, _net(), labels=LAB, is_contract=eoa)
    assert info["kind"] == "exchange_deposit" and info["exchange"] == "Binance"
    assert info["network"] == "polygon" and info["errors"] == [] and info["error_count"] == 0
    none = run_classify(Stub({}), W, _net(), labels=LAB, is_contract=eoa)
    assert none["kind"] == "not_deposit"


def test_characterize_shows_the_deposit_check_of_every_listed_address():
    client = Stub(_lists())
    info = characterize_address(client, X, _net(), labels=LAB, is_contract=eoa)
    assert info["deposit_check"]["kind"] == "exchange_deposit"
    assert info["top_next_hops"] and all("deposit_check" in h for h in info["top_next_hops"])
    assert info["top_next_hops"][0]["deposit_check"]["kind"] == "not_deposit"
    html = build_characterize_report(info, _net())
    assert "Deposit-address check" in html and "exchange deposit" in html


def test_characterize_can_skip_the_deposit_check():
    client = Stub(_lists())
    info = characterize_address(client, X, _net(), labels=LAB, deposit_check=False)
    assert "deposit_check" not in info and client.calls == []


def test_classify_report_names_the_kind_and_the_caveat():
    info = run_classify(Stub(_lists()), X, _net(), labels=LAB, is_contract=eoa)
    html = build_classify_report(info, _net())
    assert "exchange deposit" in html and X in html and "context" in html


def test_cli_classify_address_prints_and_writes_json_and_report(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: Stub(_lists()))
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **kw: dict(LAB))
    monkeypatch.setattr(cli, "make_contract_check", lambda url: eoa)
    out_json, out_html = str(tmp_path / "c.json"), str(tmp_path / "c.html")
    cli.main(
        ["classify-address", X, "--network", "polygon", "--json", out_json, "--report", out_html]
    )
    out = capsys.readouterr().out
    assert "exchange deposit" in out and "Binance" in out and "1 sender" in out
    with open(out_json, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["kind"] == "exchange_deposit" and data["senders"] == [W]
    assert os.path.getsize(out_html) > 0


def test_cli_characterize_prints_the_deposit_check(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: Stub(_lists()))
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **kw: dict(LAB))
    monkeypatch.setattr(cli, "make_contract_check", lambda url: eoa)
    cli.main(["characterize", X, "--network", "polygon"])
    out = capsys.readouterr().out
    assert "Deposit-address check: exchange deposit" in out
    assert "not a deposit address" in out  # the next hop


def _paid_lists():
    """100 MATIC received; three years later 1 wei paid to a labelled exchange wallet."""
    later = 1000 + 3 * 365 * 24 * 3600
    return {
        ("txlist", X): [_row(W, X, 100 * 10**18, 1000, "0xin"), _row(X, HOT, 1, later, "0xpay")]
    }


def test_paying_a_labelled_exchange_is_shown_as_context_in_every_output(
    monkeypatch, capsys, tmp_path
):
    info = run_classify(Stub(_paid_lists()), X, _net(), labels=LAB, is_contract=eoa)
    assert info["kind"] == "paid_exchange" and info["evidence"] is False
    html = build_classify_report(info, _net(), labels=LAB)
    assert "paid an exchange (Binance)" in html and "Paid, not swept" in html
    assert "Exchange deposit" not in html and HOT in html
    char = characterize_address(Stub(_paid_lists()), X, _net(), labels=LAB, is_contract=eoa)
    assert char["deposit_check"]["kind"] == "paid_exchange"
    assert "paid an exchange (Binance)" in build_characterize_report(char, _net())

    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: Stub(_paid_lists()))
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **kw: dict(LAB))
    monkeypatch.setattr(cli, "make_contract_check", lambda url: eoa)
    out_json = str(tmp_path / "c.json")
    cli.main(["classify-address", X, "--network", "polygon", "--json", out_json])
    out = capsys.readouterr().out
    assert "paid an exchange (Binance), not a deposit address" in out
    assert f"paid {HOT}" in out and f"swept to {HOT}" not in out
    with open(out_json, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["kind"] == "paid_exchange" and data["paid_targets"] == [HOT]


@pytest.mark.parametrize(
    "argv",
    [
        ["characterize", X, "--labels-blockscout", "--cache-dir", "d"],
        ["trace", X, "--amount", "1", "--labels-blockscout", "--cache-dir", "d"],
        ["multi", X, W, "--labels-blockscout", "--cache-dir", "d"],
        ["classify-address", X, "--labels-blockscout", "--cache-dir", "d"],
    ],
)
def test_every_command_with_blockscout_labels_has_a_cache_dir(argv):
    assert cli.build_parser().parse_args(argv).cache_dir == "d"


def test_blockscout_labels_without_a_cache_say_so(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **kw: {})
    args = cli.build_parser().parse_args(
        ["classify-address", X, "--labels-blockscout", "--cache-dir", str(tmp_path)]
    )
    net = type("N", (), {"name": "polygon", "chain_id": 137})()
    assert cli._load_labels(args, net) == {}
    err = capsys.readouterr().err
    assert "Blockscout" in err and "labels blockscout" in err


def test_web_ui_lists_the_paid_exchange_wallet_as_a_target(monkeypatch):
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "webui"))
    import app as webapp

    monkeypatch.setattr(webapp, "_labels", lambda net: dict(LAB))
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: eoa)
    view, (_header, rows), reports = webapp._run_classify(Stub(_paid_lists()), X, _net())
    assert view["kind_label"] == "paid an exchange (Binance), not a deposit address"
    assert [t["address"] for t in view["targets"]] == [HOT] and view["senders"] == []
    assert rows[0][2] == "paid_exchange"
    assert json.loads(reports[X + ".json"])["kind"] == "paid_exchange"
