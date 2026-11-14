"""CLI input validation, output-path checks before API work, EIP-55, console safety."""

import io

import pytest

from tornado_demix import cli, config
from tornado_demix.errors import ConfigError, RegistryError
from tornado_demix.networks import get_network

CHECKSUMMED = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
LOWER = CHECKSUMMED.lower()
SYNTH = "0x" + "0" * 39 + "1"


def _has_keccak():
    try:
        import Crypto.Hash.keccak  # noqa: F401
    except ImportError:
        return False
    return True


needs_keccak = pytest.mark.skipif(not _has_keccak(), reason="needs the labels extra")


# EIP-55
@needs_keccak
def test_a_correct_checksum_is_accepted():
    assert config.normalize_address(CHECKSUMMED) == LOWER


@needs_keccak
def test_a_wrong_checksum_is_an_error_naming_the_checksum():
    wrong = CHECKSUMMED[:-1] + "D"
    with pytest.raises(ConfigError, match="checksum"):
        config.normalize_address(wrong)


@needs_keccak
def test_every_case_flip_of_a_checksummed_address_is_caught():
    for i, ch in enumerate(CHECKSUMMED):
        if i >= 2 and ch.isalpha():
            flipped = CHECKSUMMED[:i] + ch.swapcase() + CHECKSUMMED[i + 1 :]
            if flipped[2:] in (flipped[2:].lower(), flipped[2:].upper()):
                continue  # became uniform case: no checksum claimed
            with pytest.raises(ConfigError):
                config.normalize_address(flipped)


def test_uniform_case_needs_no_checksum():
    assert config.normalize_address(LOWER) == LOWER
    assert config.normalize_address("0x" + LOWER[2:].upper()) == LOWER
    assert config.normalize_address(SYNTH) == SYNTH


def test_a_non_address_is_still_an_error():
    with pytest.raises(ConfigError, match="not a wallet address"):
        config.normalize_address("0x1234")


def test_the_cli_validator_uses_the_checksum_rule():
    with pytest.raises(ConfigError, match="EIP-55"):
        cli._valid_address(CHECKSUMMED[:-1] + ("e" if CHECKSUMMED[-1] != "e" else "F"))


# console
def test_console_streams_never_crash_on_non_ascii():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    cli._lenient_stream(stream)
    print("label: Жук ☃", file=stream)
    stream.flush()
    assert b"label:" in raw.getvalue()


def test_main_makes_stdout_and_stderr_lenient(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_lenient_stream", lambda s: seen.append(s))
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert len(seen) == 2


# numeric options
@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_a_non_positive_amount_is_a_usage_error(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["trace", SYNTH, "--amount", value])
    assert exc.value.code == 2


@pytest.mark.parametrize("value", ["0", "-3", "abc", "1.5"])
def test_a_non_positive_window_is_a_usage_error(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["demix", SYNTH, "--window-days", value])
    assert exc.value.code == 2


def test_positive_values_parse():
    args = cli.build_parser().parse_args(["trace", SYNTH, "--amount", "0.5"])
    assert args.amount == 0.5
    args = cli.build_parser().parse_args(["demix", SYNTH, "--window-days", "7"])
    assert args.window_days == 7


# output paths before API work
def _stop(*_a, **_k):
    raise ConfigError("stop here: API work starts")


def test_the_report_directory_is_created_before_any_api_work(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load_api_key", _stop)
    target = tmp_path / "new" / "sub" / "report.html"
    with pytest.raises(SystemExit) as exc:
        cli.main(["demix", SYNTH, "--report", str(target)])
    assert "stop here" in str(exc.value)
    assert target.parent.is_dir()


def test_an_unusable_report_directory_fails_before_the_key_is_read(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(config, "load_api_key", lambda *a, **k: called.append(1) or "K")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit) as exc:
        cli.main(["demix", SYNTH, "--report", str(blocker / "r.html")])
    assert "report" in str(exc.value).lower()
    assert called == []


def test_out_dir_is_created_up_front(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load_api_key", _stop)
    out = tmp_path / "csvs"
    with pytest.raises(SystemExit):
        cli.main(["demix", SYNTH, "--out-dir", str(out)])
    assert out.is_dir()


def test_a_nonexistent_api_csv_is_an_error_not_a_silent_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    missing = str(tmp_path / "nope.csv")
    with pytest.raises(ConfigError, match="not found"):
        config.load_api_key(missing)
    with pytest.raises(SystemExit) as exc:
        cli.main(["demix", SYNTH, "--api-csv", missing])
    assert "nope.csv" in str(exc.value)


def test_the_env_var_still_serves_when_no_api_csv_is_named(monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    assert config.load_api_key(None)


# --network accepts a chain id
def test_network_accepts_a_chain_id():
    assert get_network("1").name == "ethereum"
    assert get_network(" 1 ").chain_id == 1
    assert get_network("ethereum").chain_id == 1


def test_an_unknown_chain_id_lists_the_options():
    with pytest.raises(RegistryError, match="Unknown network"):
        get_network("999999")


# --help
def test_help_has_three_examples_and_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Examples" in out and "Exit codes" in out
    examples = [ln for ln in out.splitlines() if ln.startswith("  tornado-demix ")]
    assert len(examples) >= 3
    for code in ("0", "1", "2"):
        assert "\n  {}  ".format(code) in out
