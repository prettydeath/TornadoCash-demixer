import pytest

from tornado_demix.withdraw_root import root_from_input, withdrawal_root

SEL = "21a0adb6"  # any 4-byte selector
ROOT = "11" * 32
NULL = "22" * 32


def word(h):
    return h.rjust(64, "0")


def test_root_is_the_word_before_the_nullifier_direct_pool():
    data = "0x" + SEL + word("e0") + ROOT + NULL + word("ab" * 20) + word("0") * 3
    assert root_from_input(data, "0x" + NULL) == int(ROOT, 16)


def test_root_found_behind_a_proxy_address_argument():
    data = "0x" + SEL + word("cd" * 20) + word("e0") + ROOT + NULL + word("0") * 4
    assert root_from_input(data, "0x" + NULL) == int(ROOT, 16)


def test_missing_or_unaligned_nullifier_gives_none():
    assert root_from_input("0x" + SEL + word("0") * 4, "0x" + NULL) is None
    # odd (half-byte) offset never matches
    assert root_from_input("0x" + SEL + "0" + ROOT + NULL, "0x" + NULL) is None


def test_root_found_in_nested_inner_call_shifted_by_4_bytes():
    inner = "b438689f" + word("e0") + ROOT + NULL + word("0") * 3
    data = "0x" + "174dea71" + word("40") + word("20") + word(format(len(inner) // 2, "x")) + inner
    assert root_from_input(data, "0x" + NULL) == int(ROOT, 16)


def test_unaligned_bytes_offset_finds_root():
    assert root_from_input("0x" + SEL + "00" + ROOT + NULL, "0x" + NULL) == int(ROOT, 16)


def test_fewer_than_64_chars_before_nullifier_gives_none():
    assert root_from_input("0x" + SEL + "11" * 10 + NULL, "0x" + NULL) is None


def test_nullifier_as_first_word_has_no_root():
    assert root_from_input("0x" + SEL + NULL, "0x" + NULL) is None


def test_empty_input_gives_none():
    assert root_from_input("", "0x" + NULL) is None
    assert root_from_input("0x", "0x" + NULL) is None


class _Client:
    def __init__(self, info):
        self.info = info

    def tx_info(self, tx_hash):
        return self.info


def test_withdrawal_root_uses_tx_input():
    data = "0x" + SEL + word("e0") + ROOT + NULL
    w = {"tx_hash": "0x" + "aa" * 32, "nullifier": "0x" + NULL}
    assert withdrawal_root(_Client({"input": data}), w) == int(ROOT, 16)
    assert withdrawal_root(_Client(None), w) is None


# (kind, tx hash, nullifier, expected n_leaves of the root in the 1 ETH pool's index)
_REAL_1ETH = [
    (
        "direct call to the pool, relayer named",
        "0x6c1504ace6e2d1d1ceed6669f44a2d27eaa130f59bb9e570d7096420e7c98bc4",
        "0x2bd61551bd64881d218a2900dde57b1983beae630287ce69fa781a943c680e21",
        96556,
    ),
    (
        "Tornado Router, no relayer",
        "0xfdb5d81ba9af066ae895c99142ea7a333e15595ef551dffe967792134951d6f1",
        "0x2eba4576bbfcf356eaa62517b7a3898107fa3aa894566dc21e25effb463177aa",
        96614,
    ),
    (
        "Tornado Router, relayer named",
        "0xa8e64a1edfc3351ffd727639add4fec274ac3ea1d47397b84cbe0e88cfcc021c",
        "0x1776d368304f60e129d3bcba951258a9a1de25080f10895b809b2640f37d6a7a",
        96620,
    ),
    (
        "Multicall3 wrapper, inner call inside bytes",
        "0xf1b2b28b94826808db9adcb2565fccd14d9871b323a4519a954a083267680950",
        "0x17cc1c6116bd75b8be08cf09ca33a2b6cabb02ade56ec8d06e4e29d833837b5c",
        97568,
    ),
]


@pytest.mark.live
def test_real_1_eth_withdrawals_root_is_in_the_root_index():
    """Opt-in: python -m pytest -m live tests/test_withdraw_root.py (needs .cache/merkle)."""
    import json
    import os
    import sys

    root = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, os.path.join(root, "tools"))
    from ens_labels import api_keys

    from tornado_demix.etherscan import EtherscanClient
    from tornado_demix.networks import get_network

    net = get_network("ethereum")
    pool = net.by_key["1 ETH"]
    path = os.path.join(root, ".cache", "merkle", "1_{}.json".format(pool.address))
    with open(path, encoding="utf-8") as fh:
        roots = json.load(fh)["roots"]
    client = EtherscanClient(api_keys()[0], pause=0.36, **net.client_kwargs())
    for kind, tx_hash, nullifier, n_leaves in _REAL_1ETH:
        got = withdrawal_root(client, {"tx_hash": tx_hash, "nullifier": nullifier})
        assert got is not None, kind
        assert roots.get("0x{:064x}".format(got)) == n_leaves, kind
