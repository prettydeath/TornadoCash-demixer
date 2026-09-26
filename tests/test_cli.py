"""CLI summary printing, driven with a stubbed client and tracer.

``cmd_cluster`` prints straight from the dicts ``cluster.trace_wallet`` returns,
so a rename there breaks the command with no test to catch it. These tests pin
the printed summary to the pool-keyed shape.
"""

import argparse

import pytest

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

    with open("pyproject.toml", "rb") as fh:
        assert tomllib.load(fh)["project"]["version"] == __version__
