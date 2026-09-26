"""The registry has to survive `pip install`.

Without the package-data entry a wheel carries only the four built-in Ethereum
pools, and every ``--network`` but ethereum fails. ``pip install -e .`` cannot
catch that, because an editable install resolves into the source tree.
"""

import os

import pytest

from tornado_demix.config import PACKAGE_DATA, resolve
from tornado_demix.networks import load_networks

# tomllib is stdlib from 3.11; the package supports 3.9+. The two metadata
# tests that need it skip on older interpreters - CI runs the matrix, so the
# assertion is still made on every release build.
tomllib = pytest.importorskip("tomllib", reason="stdlib from Python 3.11")

EXPECTED_POOLS = 55
EXPECTED_NETWORKS = 8


def test_the_registry_lives_inside_the_package():
    assert os.path.isfile(os.path.join(PACKAGE_DATA, "networks.csv"))


def test_pyproject_declares_the_registry_as_package_data():
    with open("pyproject.toml", "rb") as fh:
        config = tomllib.load(fh)
    patterns = config["tool"]["setuptools"]["package-data"]["tornado_demix"]
    assert any(p.endswith(".csv") for p in patterns), patterns


def test_every_packaged_data_file_is_covered_by_those_patterns():
    """A new data file that no pattern matches is silently left out of the wheel."""
    import fnmatch

    with open("pyproject.toml", "rb") as fh:
        patterns = tomllib.load(fh)["tool"]["setuptools"]["package-data"]["tornado_demix"]
    for root, _dirs, files in os.walk(PACKAGE_DATA):
        rel_root = os.path.relpath(root, os.path.dirname(PACKAGE_DATA))
        for name in files:
            rel = os.path.join(rel_root, name).replace(os.sep, "/")
            assert any(fnmatch.fnmatch(rel, p) for p in patterns), rel


def test_the_registry_resolves_without_any_config_directory(monkeypatch, tmp_path):
    """The case that broke: no ./config, no repo config/, nothing copied."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TORNADO_DEMIX_CONFIG", raising=False)
    assert resolve("networks.csv", allow_example=True, allow_packaged=True)
    nets = load_networks()
    assert len(nets) == EXPECTED_NETWORKS
    assert sum(len(n.pools) for n in nets.values()) == EXPECTED_POOLS


def test_the_registry_resolves_even_when_the_env_var_scopes_a_case_dir(monkeypatch, tmp_path):
    """A case directory scopes case *inputs*, not the verified registry."""
    monkeypatch.setenv("TORNADO_DEMIX_CONFIG", str(tmp_path))
    nets = load_networks()
    assert sum(len(n.pools) for n in nets.values()) == EXPECTED_POOLS


def test_a_local_registry_still_overrides_the_packaged_one(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "networks.csv").write_text(
        "network,chain_id,currency,asset,denomination,pool_address,decimals,token_address\n"
        "testnet,1,ETH,ETH,1,0x%040x,18,\n" % 1,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TORNADO_DEMIX_CONFIG", raising=False)
    nets = load_networks()
    assert "testnet" in nets
