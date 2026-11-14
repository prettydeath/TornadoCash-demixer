"""The pure-Python keccak-256 and EIP-55 on an install without pycryptodome."""

import builtins
import hashlib

import pytest

from tornado_demix import config
from tornado_demix.config import ConfigError, normalize_address
from tornado_demix.keccak import keccak256

# EIP-55 specification test vectors.
EIP55 = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


def test_known_vectors():
    assert (
        keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert (
        keccak256(b"abc").hex()
        == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_is_not_sha3_and_handles_block_boundaries():
    assert keccak256(b"abc") != hashlib.sha3_256(b"abc").digest()
    for n in (135, 136, 137, 272):
        assert len(keccak256(b"a" * n)) == 32
    assert keccak256(b"a" * 135) != keccak256(b"a" * 136)


def test_matches_pycryptodome_when_available():
    keccak = pytest.importorskip("Crypto.Hash.keccak")
    for data in (b"", b"x" * 40, b"y" * 136, b"z" * 300):
        assert keccak256(data) == keccak.new(digest_bits=256, data=data).digest()


@pytest.fixture
def no_pycryptodome(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "Crypto" or name.startswith("Crypto."):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


@pytest.mark.parametrize("addr", EIP55)
def test_eip55_vectors_accepted_without_pycryptodome(no_pycryptodome, addr):
    assert normalize_address(addr) == addr.lower()


def test_bad_checksum_refused_without_pycryptodome(no_pycryptodome):
    bad = EIP55[0][:-1] + "E"
    with pytest.raises(ConfigError, match="EIP-55"):
        normalize_address(bad)


def test_config_keccak_is_never_none():
    assert config._keccak256() is not None
