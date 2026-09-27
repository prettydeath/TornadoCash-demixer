"""CLI summary printing, driven with a stubbed client and tracer.

``cmd_cluster`` prints straight from the dicts ``cluster.trace_wallet`` returns,
so a rename there breaks the command with no test to catch it. These tests pin
the printed summary to the pool-keyed shape.
"""

import argparse
import os

import pytest

from tests.fixtures import demix_result as fx
from tests.test_report import _char_info, _multi_corr
from tornado_demix import cli

WALLET = "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"
Z_ADDR = "0xdddd000000000000000000000000000000000009"


def test_positive_hours_accepts_a_positive_value():
    assert cli._positive_hours("6") == 6.0
    assert cli._positive_hours("0.5") == 0.5


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_positive_hours_rejects_non_positive_or_nonnumeric(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        cli._positive_hours(bad)


# A self-contained registry. Resolving --network against the repository's own
# config/ instead would make these tests depend on the developer's working
# tree: anyone who follows the README quickstart and writes their own
# config/networks.csv without an avalanche row would see them fail.
NETWORKS_CSV = (
    "network,chain_id,currency,asset,denomination,pool_address,decimals,"
    "token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx,"
    "router_address\n"
    "avalanche,43114,AVAX,AVAX,10,0x330bdFADE01eE9bF63C209Ee33102DD334618e0a,"
    "18,,,,,,,\n"
    "avalanche,43114,AVAX,AVAX,10,0xe1376deF383d1656f5A40B6ba31f8c035bfc26AA,"
    "18,,,,,,,\n"
)


@pytest.fixture
def networks_csv(tmp_path):
    path = tmp_path / "networks.csv"
    path.write_text(NETWORKS_CSV)
    return str(path)


@pytest.fixture
def args(networks_csv):
    """Build a CLI Namespace pinned to the fixture registry."""

    def _make(**over):
        base = dict(
            api_csv=None,
            network="ethereum",
            networks_csv=networks_csv,
            wallets=[WALLET],
            wallets_csv="",
            out_dir="",
            report="",
            cache_dir=".cache",
            window_days=30,
            exit_window=None,
            fee_lo=0.90,
            fee_hi=0.995,
            gap_hours=24,
            layer_cap=35,
            min_forward_frac=0.01,
            mode="events",
        )
        base.update(over)
        return argparse.Namespace(**base)

    return _make


def _trace(clusters):
    return {
        "wallet": WALLET,
        "fingerprint": {"1 ETH": 2, "0.1 ETH": 1},
        "layers": {},
        "clusters": clusters,
    }


@pytest.fixture
def stub_cluster(monkeypatch):
    """Neutralise the API key, the client and the tracer; return a setter."""
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())

    def _install(trace):
        monkeypatch.setattr(cli, "trace_wallet", lambda *a, **kw: trace)

    return _install


def test_cmd_demix_warns_per_unresolved_pool(monkeypatch, args, capsys):
    """A pool skipped by a failed block lookup is printed as NOT searched,
    so it is not read as "searched and found nothing" (Finding 2)."""
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    data = {
        "wallet": WALLET,
        "vouchers": [{"count": 1, "pool_key": "1 ETH", "first_ts": 1000}],
        "denoms": {},
        "unresolved": [{"pool_key": "1 ETH", "reason": "provider returned None"}],
    }
    monkeypatch.setattr(cli, "run_demix", lambda *a, **kw: data)
    cli.cmd_demix(args(wallet=WALLET, out_dir="", report=""))

    out = capsys.readouterr().out
    assert "1 ETH pool NOT searched" in out
    assert "provider returned None" in out


def test_cmd_cluster_prints_pool_keys_not_denominations(stub_cluster, args, capsys):
    stub_cluster(
        _trace(
            [
                {
                    "z": Z_ADDR,
                    "pools_covered": ["0.1 ETH", "1 ETH"],
                    "n_layers": 2,
                    "totals": {"ETH": 1.8},
                    "by_pool": {},
                }
            ]
        )
    )
    cli.cmd_cluster(args())

    out = capsys.readouterr().out
    assert "[0.1 ETH, 1 ETH]" in out
    assert "layers=2" in out
    assert "ETH:1.8" in out
    assert "1x0.1 ETH + 2x1 ETH" in out
    # The pool key already names the asset; it must not be suffixed again.
    assert "ETHETH" not in out


def test_cmd_cluster_labels_the_total_with_the_network_currency(stub_cluster, args, capsys):
    """On Avalanche the funnelled total is AVAX, and both 10 AVAX pools show."""
    stub_cluster(
        _trace(
            [
                {
                    "z": Z_ADDR,
                    "pools_covered": ["10 AVAX", "10 AVAX#2"],
                    "n_layers": 2,
                    "totals": {"AVAX": 20.0},
                    "by_pool": {},
                }
            ]
        )
    )
    cli.cmd_cluster(args(network="avalanche"))

    out = capsys.readouterr().out
    assert "[10 AVAX, 10 AVAX#2]" in out
    assert "AVAX:20" in out
    assert "AVAXETH" not in out


def test_cmd_cluster_never_sums_a_mixed_asset_cluster(stub_cluster, args, capsys):
    """A Z fed by a 1 ETH layer and a 1000 DAI layer prints one value per
    asset, never a summed 991.8 of nothing."""
    stub_cluster(
        _trace(
            [
                {
                    "z": Z_ADDR,
                    "pools_covered": ["1 ETH", "1000 DAI"],
                    "n_layers": 2,
                    "totals": {"ETH": 1.8, "DAI": 990.0},
                    "by_pool": {},
                }
            ]
        )
    )
    cli.cmd_cluster(args())

    out = capsys.readouterr().out
    assert "ETH:1.8" in out
    assert "DAI:990" in out
    assert "991.8" not in out


def test_cmd_cluster_reports_no_reconvergence_in_pool_terms(stub_cluster, args, capsys):
    stub_cluster(_trace([]))
    cli.cmd_cluster(args())

    out = capsys.readouterr().out
    assert "no downstream address is fed by 2+ pool layers" in out


# Wallet-address validation
# The CSV loader and the web UI have always validated addresses; the command
# line did not. A typo went to the provider unchanged and came back empty, so
# the run printed "no Tornado deposits found" - which for this tool is the most
# dangerous sentence it can produce.
import pytest  # noqa: E402

from tornado_demix.cli import _valid_address  # noqa: E402
from tornado_demix.errors import ConfigError  # noqa: E402

GOOD = "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"


@pytest.mark.parametrize(
    "bad",
    [
        "hello-world",
        "0xdeadbeef",  # too short
        "019b5bb2051797e33f726d0e7a8cb9b9c2003ac2",  # no 0x
        "0x" + "z" * 40,  # not hex
        "0xZZZZ5bb2051797e33f726d0e7a8cb9b9c2003ac2",
        "0x" + "a" * 41,  # too long
        "",
        None,
    ],
)
def test_a_bad_wallet_address_is_refused(bad):
    with pytest.raises(ConfigError):
        _valid_address(bad)


def test_a_good_address_is_accepted_and_lowercased():
    assert _valid_address(GOOD.upper()) == GOOD
    assert _valid_address("  " + GOOD + "  ") == GOOD


def test_the_error_names_the_value_the_operator_typed():
    with pytest.raises(ConfigError) as exc:
        _valid_address("0xdeadbeef")
    assert "0xdeadbeef" in str(exc.value)


def test_demix_refuses_a_bad_address_before_touching_the_api(monkeypatch):
    """No key, no network lookup: the typo fails on the spot."""
    from tornado_demix import cli

    def explode(*_a, **_k):  # pragma: no cover
        raise AssertionError("the API key was loaded despite a bad address")

    monkeypatch.setattr(cli.config, "load_api_key", explode)
    with pytest.raises(SystemExit):
        cli.main(["demix", "0xdeadbeef"])


def test_multi_refuses_a_bad_address_before_touching_the_api(monkeypatch):
    from tornado_demix import cli

    def explode(*_a, **_k):  # pragma: no cover
        raise AssertionError("the API key was loaded despite a bad address")

    monkeypatch.setattr(cli.config, "load_api_key", explode)
    with pytest.raises(SystemExit):
        cli.main(["multi", GOOD, "not-an-address"])


def test_the_cli_reports_its_version(capsys):
    from tornado_demix import __version__, cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_the_declared_version_matches_pyproject():
    """Two copies of a version number drift; this is the one that catches it."""
    tomllib = pytest.importorskip("tomllib", reason="stdlib from Python 3.11")
    from tornado_demix import __version__

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
        assert tomllib.load(fh)["project"]["version"] == __version__


def test_rapid_sets_a_seven_day_exit_window(monkeypatch):
    seen = {}
    real = cli.build_parser

    def parser_with_probe():
        parser = real()
        for action in parser._subparsers._group_actions:
            for sub in action.choices.values():
                sub.set_defaults(func=lambda args: seen.setdefault("hours", args.exit_window))
        return parser

    monkeypatch.setattr(cli, "build_parser", parser_with_probe)
    cli.main(["demix", "0x" + "1" * 40, "--rapid"])
    assert seen["hours"] == 168.0


def test_an_explicit_exit_window_wins_over_rapid(monkeypatch):
    seen = {}
    real = cli.build_parser

    def parser_with_probe():
        parser = real()
        for action in parser._subparsers._group_actions:
            for sub in action.choices.values():
                sub.set_defaults(func=lambda args: seen.setdefault("hours", args.exit_window))
        return parser

    monkeypatch.setattr(cli, "build_parser", parser_with_probe)
    cli.main(["demix", "0x" + "1" * 40, "--rapid", "--exit-window", "24"])
    assert seen["hours"] == 24.0


# cmd_multi and cmd_characterize, stubbed end to end.
OTHER = "0x" + "b" * 40


def _stub(monkeypatch, **fns):
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    for name, fn in fns.items():
        monkeypatch.setattr(cli, name, fn)


def test_cmd_multi_prints_links_profiles_sync_and_writes_files(monkeypatch, args, capsys, tmp_path):
    corr = _multi_corr()
    corr["sync_groups"] = [
        {"wallets": [fx.WALLET, fx.ALICE], "span_seconds": 600, "first_ts": 1000}
    ]
    corr["cross_profile"] = {fx.BOB: [fx.WALLET, fx.ALICE]}
    corr["consolidator_grades"] = {
        fx.BOB: {
            "wallets": [fx.WALLET, fx.ALICE],
            "band": "moderate",
            "independent": [],
            "self_relayed": False,
            "distinct_fingerprints": 1,
            "artefact": True,
            "edge_reason": None,
        }
    }
    seen = {}

    def correlate(*a, **kw):
        seen.update(kw)
        return corr

    _stub(monkeypatch, correlate=correlate)
    out_dir, report = str(tmp_path / "csv"), str(tmp_path / "multi.html")
    cli.cmd_multi(args(wallets=[WALLET, OTHER], out_dir=out_dir, report=report, max_voucher_span=6))

    out = capsys.readouterr().out
    assert "Wallets analysed: 2" in out
    assert "Strong links (count-matched candidate shared by 2+): 1" in out
    assert "1 exact, 1 total match(es)" in out
    assert "2 wallets within" in out
    assert "[moderate] (window-overlap artefact) " + fx.BOB in out
    assert seen["max_voucher_span_hours"] == 6
    assert os.listdir(out_dir) and os.path.getsize(report) > 0


def test_cmd_characterize_prints_the_summary_and_writes_the_report(
    monkeypatch, args, capsys, tmp_path
):
    info = _char_info()
    info["label"] = {"label": "Hot wallet", "category": "exchange", "entity": "ExampleEx"}
    info["pool_inflows"] = info["pool_inflows"] * 41
    info["top_next_hops"].append({"address": OTHER, "count": 2, "kind": "call", "label": None})
    _stub(
        monkeypatch,
        characterize_address=lambda *a, **kw: info,
        load_attribution=lambda *a, **kw: {},
    )
    report = str(tmp_path / "c.html")
    cli.cmd_characterize(args(address=fx.BOB, report=report))

    out = capsys.readouterr().out
    assert "Classification: AGGREGATOR" in out
    assert "ExampleEx" in out
    assert "... and 1 more" in out
    assert "contract call" in out
    assert "lead, not proof" in out
    assert os.path.getsize(report) > 0


def test_cmd_trace_prints_edges_and_terminals_and_writes_json(monkeypatch, args, capsys, tmp_path):
    result = {
        "start": fx.BOB,
        "amount": 1.0,
        "asset": "ETH",
        "edges": [
            {
                "hop": 1,
                "from": fx.BOB,
                "to": OTHER,
                "tx_hash": "0xa",
                "ts": 0,
                "asset": "ETH",
                "value": 2.0,
                "attributed": 1.0,
                "kind": "swap",
                "swapped_to": "DAI",
            }
        ],
        "terminals": [
            {
                "address": OTHER,
                "asset": "DAI",
                "amount": 3000.0,
                "hop": 2,
                "reason": "labelled address",
                "label": {"label": "Hot wallet", "category": "exchange"},
            }
        ],
        "nodes_expanded": 1,
    }
    seen = {}

    def trace_funds(*a, **kw):
        seen.update(kw)
        return result

    _stub(monkeypatch, trace_funds=trace_funds, load_attribution=lambda *a, **kw: {})
    path = str(tmp_path / "t.json")
    cli.cmd_trace(args(address=fx.BOB, amount=1.0, token="", start_block=5, max_hops=3, json=path))
    out = capsys.readouterr().out
    assert "swapped to DAI" in out and "[labelled address]" in out
    assert seen["start_block"] == 5 and seen["max_hops"] == 3 and seen["token"] is None
    assert '"nodes_expanded": 1' in open(path, encoding="utf-8").read()


def test_the_trace_subcommand_parses():
    ns = cli.build_parser().parse_args(["trace", fx.BOB, "--amount", "2"])
    assert ns.func is cli.cmd_trace and ns.max_hops == cli.MAX_HOPS
