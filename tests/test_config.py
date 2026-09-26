"""Config path resolution must not depend on the process working directory."""

import os

import pytest

from tornado_demix import config
from tornado_demix.errors import ConfigError


@pytest.fixture
def cfg_tree(tmp_path):
    """A directory holding config/api.csv, returned as (root, config_dir)."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "api.csv").write_text("service,api_key\netherscan,KEYFROMDIR\n")
    return tmp_path, cfg


@pytest.fixture
def fake_package_config(tmp_path, monkeypatch):
    """Point config._PACKAGE_CONFIG at a controlled directory.

    Without this the resolution tests read the developer's own repository
    tree, so they pass on a fresh checkout and fail the moment anyone follows
    the README quickstart and creates config/api.csv, config/wallets.csv and
    config/networks.csv. A green branch would then show a red suite to every
    new contributor, which teaches them to ignore the suite. The directory is
    named 'config' because resolution is asserted on the tail of the path.
    """
    pkg = tmp_path / "package" / "config"
    pkg.mkdir(parents=True)
    monkeypatch.setattr(config, "_PACKAGE_CONFIG", str(pkg))
    return pkg


def test_resolve_prefers_env_var(cfg_tree, monkeypatch, tmp_path):
    _root, cfg = cfg_tree
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg))
    monkeypatch.chdir(tmp_path)
    assert config.resolve("api.csv") == os.path.join(str(cfg), "api.csv")


def test_resolve_finds_cwd_config(cfg_tree, monkeypatch):
    root, cfg = cfg_tree
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.chdir(root)
    assert config.resolve("api.csv") == os.path.join("config", "api.csv")


def test_resolve_falls_back_to_package_config(fake_package_config, monkeypatch, tmp_path):
    """From an unrelated directory, the repository's own config is still found.

    This is the reported bug: the web UI launched from webui/ saw no
    networks.csv and fell back to the ethereum-only builtin registry.
    """
    (fake_package_config / "networks.csv.example").write_text(
        "network,chain_id,currency,denomination,pool_address\n"
    )
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    found = config.resolve("networks.csv", allow_example=True)
    assert found is not None
    assert found.endswith(os.path.join("config", "networks.csv.example"))


def test_resolve_returns_none_for_unknown_name(fake_package_config, monkeypatch, tmp_path):
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.resolve("no-such-file.csv") is None


def test_resolve_ignores_the_example_sibling_by_default(fake_package_config, monkeypatch, tmp_path):
    """api.csv.example is git-tracked, but its key is a placeholder."""
    (fake_package_config / "api.csv.example").write_text(
        "service,api_key\netherscan,YOUR_ETHERSCAN_KEY\n"
    )
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.resolve("api.csv") is None
    assert config.resolve("api.csv", allow_example=True) is not None


def test_load_wallets_does_not_fall_back_to_the_example_addresses(
    fake_package_config, monkeypatch, tmp_path
):
    """A forgotten wallets.csv must fail loudly, not analyse example wallets."""
    (fake_package_config / "wallets.csv.example").write_text(
        "address,label\n0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2,example-1\n"
    )
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError):
        config.load_wallets()


def test_the_env_var_scopes_the_search_instead_of_prepending_to_it(
    fake_package_config, monkeypatch, tmp_path
):
    """TORNADO_DEMIX_CONFIG must not fall through to another case's config.

    An analyst who points the variable at /cases/case-42 and forgets to put
    wallets.csv there must get an error, not a silent fall-through to the
    repository's own wallets.csv - which would analyse a different subject.
    """
    (fake_package_config / "wallets.csv").write_text(
        "address,label\n0x450cc23c0bc13d46288deb36913fdaaee49fcd99,other-case\n"
    )
    (fake_package_config / "api.csv").write_text("service,api_key\netherscan,OTHERCASEKEY\n")
    case = tmp_path / "case-42"
    case.mkdir()
    monkeypatch.setenv(config.CONFIG_ENV, str(case))
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    assert config.config_dirs() == [str(case)]
    assert config.resolve("wallets.csv") is None
    with pytest.raises(ConfigError):
        config.load_wallets()
    with pytest.raises(ConfigError):
        config.load_api_key()


def test_the_env_var_scope_also_excludes_the_cwd_config(fake_package_config, monkeypatch, tmp_path):
    """./config is skipped too - the whole point is a single named directory."""
    cwd_cfg = tmp_path / "config"
    cwd_cfg.mkdir()
    (cwd_cfg / "wallets.csv").write_text(
        "address,label\n0x450cc23c0bc13d46288deb36913fdaaee49fcd99,other-case\n"
    )
    case = tmp_path / "case-42"
    case.mkdir()
    monkeypatch.setenv(config.CONFIG_ENV, str(case))
    monkeypatch.chdir(tmp_path)
    assert config.resolve("wallets.csv") is None


def test_load_api_key_uses_resolved_path(cfg_tree, monkeypatch, tmp_path):
    _root, cfg = cfg_tree
    monkeypatch.setenv(config.CONFIG_ENV, str(cfg))
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.load_api_key() == "KEYFROMDIR"


def test_load_api_key_falls_back_to_env(fake_package_config, monkeypatch, tmp_path):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path))
    monkeypatch.setenv("ETHERSCAN_API_KEY", "KEYFROMENV")
    monkeypatch.chdir(tmp_path)
    assert config.load_api_key() == "KEYFROMENV"


def test_load_api_key_raises_when_absent(fake_package_config, monkeypatch, tmp_path):
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path))
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError):
        config.load_api_key()


def test_load_wallets_reads_address_column(tmp_path, monkeypatch):
    path = tmp_path / "wallets.csv"
    path.write_text(
        "address,label\n"
        "0x019B5BB2051797E33F726D0E7A8CB9B9C2003AC2,suspect-1\n"
        "\n"
        "#0xdeadbeef,comment\n"
        "not-an-address,junk\n"
        "0x450cc23c0bc13d46288deb36913fdaaee49fcd99,suspect-2\n"
    )
    monkeypatch.chdir(tmp_path)
    assert config.load_wallets(str(path)) == [
        "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2",
        "0x450cc23c0bc13d46288deb36913fdaaee49fcd99",
    ]


@pytest.mark.parametrize(
    "value, ok",
    [
        ("0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2", True),
        ("0x019B5BB2051797E33F726D0E7A8CB9B9C2003AC2", True),
        ("0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac", False),
        ("019b5bb2051797e33f726d0e7a8cb9b9c2003ac2aa", False),
        ("0x019b5bb2051797e33f726d0e7a8cb9b9c2003ag2", False),
        ("0x_19b5bb2051797e33f726d0e7a8cb9b9c2003ac2", False),
        ("", False),
    ],
)
def test_is_address_accepts_only_0x_and_40_hex_digits(value, ok):
    assert config.is_address(value) is ok


def test_load_wallets_skips_rows_that_are_not_hex(tmp_path):
    path = tmp_path / "wallets.csv"
    path.write_text(
        "address\n0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2\n0x" + "z" * 40 + "\n",
        encoding="utf-8",
    )
    assert config.load_wallets(str(path)) == ["0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"]
