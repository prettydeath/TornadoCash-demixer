"""CLI parity and validation (v2.23): operator clusters on the console, --json on
characterize and cluster, rejected nonsense parameters, limitations in every JSON."""

import json

import pytest

from tests.fixtures import demix_result as fx
from tests.test_cli import _stub, args, networks_csv  # noqa: F401  (fixtures)
from tests.test_report import _char_info, _multi_corr
from tornado_demix import cli
from tornado_demix.honest import exploratory_banner, limitations
from tornado_demix.report_json import demix_json, multi_json

WALLET = "0x1234567890abcdef1234567890abcdef12345678"


def _parse(*argv):
    return cli.build_parser().parse_args(list(argv))


# --- multi prints the operator clusters -----------------------------------------


def _corr_with_cluster():
    corr = _multi_corr()
    corr["operator_clusters"] = [
        {
            "wallets": [fx.WALLET, fx.ALICE],
            "edges": [(fx.WALLET, fx.ALICE, "shared gas-price exit", fx.BOB)],
        }
    ]
    return corr


def test_cmd_multi_prints_operator_clusters(monkeypatch, args, capsys):  # noqa: F811
    _stub(monkeypatch, correlate=lambda *a, **kw: _corr_with_cluster(), _load_labels=lambda *a: {})
    cli.cmd_multi(args(wallets=[fx.WALLET, fx.ALICE]))
    out = capsys.readouterr().out
    assert "Operator clusters" in out
    assert "Operator cluster 1 of 1: 2 wallets" in out
    assert fx.WALLET in out and fx.ALICE in out
    assert "shared gas-price exit" in out and fx.BOB in out


def test_cmd_multi_says_when_there_is_no_operator_cluster(monkeypatch, args, capsys):  # noqa: F811
    _stub(monkeypatch, correlate=lambda *a, **kw: _multi_corr(), _load_labels=lambda *a: {})
    cli.cmd_multi(args(wallets=[fx.WALLET, fx.ALICE]))
    out = capsys.readouterr().out
    assert "Operator clusters" in out and "none" in out.split("Operator clusters", 1)[1][:80]


# --- characterize and cluster --json --------------------------------------


def test_characterize_and_cluster_accept_json():
    assert _parse("characterize", fx.BOB, "--json", "x.json").json == "x.json"
    assert _parse("cluster", WALLET, "--json", "x.json").json == "x.json"


def test_cmd_characterize_writes_json_with_limitations(monkeypatch, args, tmp_path):  # noqa: F811
    _stub(
        monkeypatch,
        characterize_address=lambda *a, **kw: _char_info(),
        _load_labels=lambda *a: {},
        make_contract_check=lambda *a: None,
    )
    path = str(tmp_path / "c.json")
    cli.cmd_characterize(args(address=fx.BOB, json=path, no_deposit_check=True))
    doc = json.load(open(path, encoding="utf-8"))
    assert doc["limitations"] == limitations()
    assert doc["exploratory"] == bool(exploratory_banner())
    assert doc["address"] == _char_info()["address"]


# --- nonsense parameters ----------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["demix", WALLET, "--gap-hours", "-5"],
        ["demix", WALLET, "--gap-hours", "0"],
        ["demix", WALLET, "--gap-hours", "nan"],
        ["demix", WALLET, "--fee-lo", "-0.1"],
        ["demix", WALLET, "--fee-lo", "1.5"],
        ["demix", WALLET, "--fee-hi", "0"],
        ["demix", WALLET, "--fee-hi", "1.01"],
        ["multi", WALLET, "--gap-hours", "-1"],
        ["cluster", WALLET, "--layer-cap", "0"],
        ["cluster", WALLET, "--min-forward-frac", "-1"],
        ["trace", WALLET, "--amount", "1", "--max-hops", "-2"],
        ["trace", WALLET, "--amount", "1", "--max-hops", "0"],
        ["trace", WALLET, "--amount", "1", "--start-block", "-100"],
    ],
)
def test_nonsense_parameters_are_rejected(argv, monkeypatch):
    monkeypatch.setattr(cli.config, "load_api_key", lambda p: pytest.fail("ran the analysis"))
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_fee_lo_must_be_below_fee_hi(monkeypatch):
    monkeypatch.setattr(cli.config, "load_api_key", lambda p: pytest.fail("ran the analysis"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["demix", WALLET, "--fee-lo", "0.99", "--fee-hi", "0.9"])
    assert exc.value.code == 2


def test_sane_parameters_still_parse():
    ns = _parse("trace", WALLET, "--amount", "1", "--max-hops", "1", "--start-block", "0")
    assert ns.max_hops == 1 and ns.start_block == 0
    ns = _parse("demix", WALLET, "--gap-hours", "0.5", "--fee-lo", "0", "--fee-hi", "1")
    assert ns.gap_hours == 0.5 and ns.fee_lo == 0.0 and ns.fee_hi == 1.0


# --- JSON carries the limitations and the exploratory flag ------------------------


def test_demix_and_multi_json_carry_limitations_and_banner_flag():
    for text in (demix_json(fx.two_pool_result()), multi_json(_multi_corr())):
        doc = json.loads(text)
        assert doc["limitations"] == limitations()
        assert doc["exploratory"] is bool(exploratory_banner())
        assert doc["exploratory_banner"] == exploratory_banner()


# --- unanchored exit groups are marked --------------------------------------------


def test_demix_json_counts_anchored_and_unanchored_exit_groups():
    data = fx.two_pool_result()
    data["exit_groups"] = [
        {"pool_key": "1 ETH", "members": [fx.BOB], "anchors": [{}], "anchored": True},
        {"pool_key": "1 ETH", "members": [fx.ALICE], "anchors": [], "anchored": False},
    ]
    doc = json.loads(demix_json(data))
    summary = doc["exit_group_summary"]
    assert summary["anchored"] == 1 and summary["unanchored"] == 1
    assert "not evidence" in summary["note"]


# --- limitations on the other CLI exports -------------------------------------------------------


def test_cluster_trace_and_classify_json_carry_limitations(monkeypatch, args, tmp_path):  # noqa: F811
    from tests.test_webui import _TRACE

    honest = {"limitations": limitations(), "exploratory": bool(exploratory_banner())}

    def check(path):
        doc = json.load(open(path, encoding="utf-8"))
        assert {k: doc[k] for k in honest} == honest
        return doc

    trace = {"wallet": fx.WALLET, "fingerprint": {}, "clusters": [], "incomplete": None}
    _stub(monkeypatch, trace_wallet=lambda *a, **kw: trace)
    path = str(tmp_path / "cluster.json")
    cli.cmd_cluster(args(json=path))
    assert check(path)["traces"][0]["wallet"] == fx.WALLET

    _stub(monkeypatch, trace_funds=lambda *a, **kw: _TRACE, load_attribution=lambda *a, **kw: {})
    path = str(tmp_path / "trace.json")
    cli.cmd_trace(
        args(address=fx.BOB, amount=2.0, token="", start_block=0, max_hops=4, json=path, report="")
    )
    assert check(path)["nodes_expanded"] == 2

    info = {
        "address": fx.BOB,
        "kind": "deposit_like",
        "senders": [fx.ALICE],
        "sweep_targets": [],
        "errors": [],
        "truncated": [],
    }
    _stub(
        monkeypatch,
        run_classify=lambda *a, **kw: info,
        _load_labels=lambda *a: {},
        make_contract_check=lambda *a: None,
    )
    path = str(tmp_path / "classify.json")
    cli.cmd_classify(args(address=fx.BOB, json=path, report=""))
    assert check(path)["kind"] == "deposit_like"


def test_fee_hi_error_names_the_default_fee_lo(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load_api_key", lambda p: pytest.fail("ran the analysis"))
    with pytest.raises(SystemExit):
        cli.main(["demix", WALLET, "--fee-hi", "0.8"])
    assert "default 0.9" in capsys.readouterr().err
