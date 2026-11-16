import pytest

from tornado_demix.withdraw_root import (
    candidate_roots,
    read_root,
    withdrawal_read,
    withdrawal_root,
)

SEL = "21a0adb6"  # any 4-byte selector
ROOT = "11" * 32
NULL = "22" * 32


def word(h):
    return h.rjust(64, "0")


def test_root_is_the_word_before_the_nullifier_direct_pool():
    data = "0x" + SEL + word("e0") + ROOT + NULL + word("ab" * 20) + word("0") * 3
    assert candidate_roots(data, "0x" + NULL) == [int(ROOT, 16)]


def test_root_found_behind_a_proxy_address_argument():
    data = "0x" + SEL + word("cd" * 20) + word("e0") + ROOT + NULL + word("0") * 4
    assert candidate_roots(data, "0x" + NULL) == [int(ROOT, 16)]


def test_missing_or_unaligned_nullifier_gives_none():
    assert candidate_roots("0x" + SEL + word("0") * 4, "0x" + NULL) == []
    # odd (half-byte) offset never matches
    assert candidate_roots("0x" + SEL + "0" + ROOT + NULL, "0x" + NULL) == []


def test_root_found_in_nested_inner_call_shifted_by_4_bytes():
    inner = "b438689f" + word("e0") + ROOT + NULL + word("0") * 3
    data = "0x" + "174dea71" + word("40") + word("20") + word(format(len(inner) // 2, "x")) + inner
    assert candidate_roots(data, "0x" + NULL) == [int(ROOT, 16)]


def test_unaligned_bytes_offset_finds_root():
    assert candidate_roots("0x" + SEL + "00" + ROOT + NULL, "0x" + NULL) == [int(ROOT, 16)]


def test_fewer_than_64_chars_before_nullifier_gives_none():
    assert candidate_roots("0x" + SEL + "11" * 10 + NULL, "0x" + NULL) == []


def test_nullifier_as_first_word_has_no_root():
    assert candidate_roots("0x" + SEL + NULL, "0x" + NULL) == []


def test_empty_input_gives_none():
    assert candidate_roots("", "0x" + NULL) == []
    assert candidate_roots("0x", "0x" + NULL) == []


class _Client:
    def __init__(self, info):
        self.info = info

    def tx_info(self, tx_hash):
        return self.info


def test_withdrawal_root_uses_tx_input_and_target():
    pool = "0x" + "a1" * 20
    data = "0x" + SEL + word("e0") + ROOT + NULL + word("ab" * 20) + word("0") * 3
    w = {"tx_hash": "0x" + "aa" * 32, "nullifier": "0x" + NULL}
    assert withdrawal_root(_Client({"input": data, "to": pool}), w, pool) == int(ROOT, 16)
    assert withdrawal_root(_Client(None), w, pool) is None


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
        w = {"tx_hash": tx_hash, "nullifier": nullifier}
        got = withdrawal_root(client, w, pool.address, net.routers)
        assert got is not None, kind
        assert roots.get("0x{:064x}".format(got)) == n_leaves, kind


def test_every_occurrence_is_a_candidate_root():
    old = "33" * 32
    data = "0x" + SEL + word("e0") + old + NULL + word("0") + ROOT + NULL + word("0")
    assert candidate_roots(data, "0x" + NULL) == [int(old, 16), int(ROOT, 16)]


def test_several_adjacency_candidates_are_not_a_root():
    old = "33" * 32
    data = "0x" + "deadbeef" + old + NULL + ROOT + NULL
    got = read_root(data, "0x" + NULL, "0x" + "a1" * 20, "0x" + "a1" * 20)
    assert got["root"] is None and got["source"] == "adjacency"


def test_repeated_identical_candidate_counts_once():
    data = "0x" + SEL + ROOT + NULL + ROOT + NULL
    assert candidate_roots(data, "0x" + NULL) == [int(ROOT, 16)]


def test_early_occurrence_does_not_hide_a_later_one():
    data = "0x" + SEL + "11" * 10 + NULL + ROOT + NULL
    assert candidate_roots(data, "0x" + NULL) == [int(ROOT, 16)]


# --- decoded withdraw call (the root comes from the call, not from adjacency) ---------

POOL = "0x" + "a1" * 20
OTHER_POOL = "0x" + "a2" * 20
ROUTER = "0x" + "d9" * 20
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"  # Multicall3, same address on every chain
OLD = "cd" * 32  # an older root a wrapper places before the nullifier


def _w(x):
    if isinstance(x, int):
        return format(x, "064x")
    return x[2:].rjust(64, "0") if x.startswith("0x") else x.rjust(64, "0")


def _bytes(hex_body):
    """ABI tail of a ``bytes`` value: length word + data padded to 32 bytes."""
    n = len(hex_body) // 2
    pad = (-len(hex_body)) % 64
    return _w(n) + hex_body + "0" * pad


def _pool_call(root, nullifier, proof="ee" * 40):
    head = _w(7 * 32) + root + nullifier + _w("ab" * 20) + _w(0) + _w(0) + _w(0)
    return "21a0adb6" + head + _bytes(proof)


def _router_call(pool, root, nullifier, proof="ee" * 40):
    head = _w(pool) + _w(8 * 32) + root + nullifier + _w("ab" * 20) + _w(0) + _w(0) + _w(0)
    return "b438689f" + head + _bytes(proof)


def _array_of_dynamic(elements):
    """ABI encoding of a dynamic array whose elements are dynamic (offsets, then tails)."""
    offsets, tails, pos = [], [], 32 * len(elements)
    for el in elements:
        offsets.append(_w(pos))
        tails.append(el)
        pos += len(el) // 2
    return _w(len(elements)) + "".join(offsets) + "".join(tails)


def _aggregate3(calls):
    """aggregate3((address,bool,bytes)[]), allowFailure false, ``calls`` = [(target, hex)]."""
    tuples = [_w(t) + _w(0) + _w(3 * 32) + _bytes(c) for t, c in calls]
    return "82ad56cb" + _w(32) + _array_of_dynamic(tuples)


def _aggregate(calls):
    tuples = [_w(t) + _w(2 * 32) + _bytes(c) for t, c in calls]
    return "252dba42" + _w(32) + _array_of_dynamic(tuples)


def _try_aggregate(calls):
    tuples = [_w(t) + _w(2 * 32) + _bytes(c) for t, c in calls]
    return "bce38bd7" + _w(1) + _w(64) + _array_of_dynamic(tuples)  # requireSuccess


def _aggregate3_value(calls):
    tuples = [_w(t) + _w(0) + _w(0) + _w(4 * 32) + _bytes(c) for t, c in calls]
    return "174dea71" + _w(32) + _array_of_dynamic(tuples)


def _self_multicall(datas):
    return "ac9650d8" + _w(32) + _array_of_dynamic([_bytes(d) for d in datas])


def _read(data, to, routers=(ROUTER,)):
    return read_root("0x" + data, "0x" + NULL, to, POOL, routers)


def test_direct_pool_call_is_decoded():
    got = _read(_pool_call(ROOT, NULL), POOL)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def test_router_call_is_decoded_when_tornado_argument_is_the_pool():
    got = _read(_router_call(POOL, ROOT, NULL), ROUTER)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def test_router_call_for_another_pool_or_from_an_undeclared_router_is_not_decoded():
    assert _read(_router_call(OTHER_POOL, ROOT, NULL), ROUTER)["root"] is None
    assert _read(_router_call(POOL, ROOT, NULL), "0x" + "99" * 20)["root"] is None


def test_pool_call_to_another_address_is_not_decoded():
    got = _read(_pool_call(ROOT, NULL), OTHER_POOL)
    assert got["root"] is None and got["source"] == "adjacency"


def test_withdraw_call_with_another_nullifier_is_ignored():
    got = _read(_pool_call(ROOT, "33" * 32), POOL)
    assert got["root"] is None and got["source"] == "none"


def test_decoy_multicall_noop_before_the_real_withdrawal():
    # a no-op call carries [old root][nullifier]; the real withdrawal names another root
    noop = "00000000" + OLD + NULL
    data = _aggregate3([(MULTICALL, noop), (POOL, _pool_call(ROOT, NULL))])
    assert candidate_roots("0x" + data, "0x" + NULL) == [int(OLD, 16), int(ROOT, 16)]
    got = _read(data, MULTICALL)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def test_decoy_single_adjacent_old_root_without_a_withdraw_call_is_unknown():
    # a custom wrapper (unknown selector) puts the old root right before the nullifier and
    # builds the real call itself: one adjacency candidate, but no decoded withdraw call
    data = "deadbeef" + _w(POOL) + OLD + NULL + _w(0)
    assert candidate_roots("0x" + data, "0x" + NULL) == [int(OLD, 16)]
    got = _read(data, "0x" + "77" * 20)
    assert got["root"] is None and got["source"] == "adjacency"
    assert got["candidates"] == [int(OLD, 16)]


def test_withdraw_call_inside_a_noop_wrapper_targeting_another_address_does_not_count():
    data = _aggregate3([("0x" + "55" * 20, _pool_call(OLD, NULL)), (POOL, _pool_call(ROOT, NULL))])
    got = _read(data, MULTICALL)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def test_two_decoded_withdraw_calls_with_different_roots_are_ambiguous():
    data = _aggregate3([(POOL, _pool_call(OLD, NULL)), (POOL, _pool_call(ROOT, NULL))])
    got = _read(data, MULTICALL)
    assert got["root"] is None and got["source"] == "ambiguous"
    assert sorted(got["candidates"]) == sorted([int(OLD, 16), int(ROOT, 16)])


@pytest.mark.parametrize("wrap", [_aggregate, _try_aggregate, _aggregate3, _aggregate3_value])
def test_every_multicall_wrapper_is_decoded(wrap):
    data = wrap([(MULTICALL, "00000000" + OLD + NULL), (ROUTER, _router_call(POOL, ROOT, NULL))])
    got = _read(data, MULTICALL)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def test_nested_wrappers_and_self_multicall_are_decoded():
    inner = _aggregate3([(POOL, _pool_call(ROOT, NULL))])
    data = _aggregate([(MULTICALL, inner)])
    assert _read(data, MULTICALL)["root"] == int(ROOT, 16)
    # multicall(bytes[]) calls its own contract: the target is inherited; its failure
    # semantics are those of an unknown contract, so the root is decoded but unverified
    got = _read(_self_multicall([_pool_call(ROOT, NULL)]), POOL)
    assert got["source"] == "unverified" and got["candidates"] == [int(ROOT, 16)]
    assert _read(_self_multicall([_pool_call(ROOT, NULL)]), MULTICALL)["source"] == "adjacency"


def test_malformed_wrapper_does_not_raise():
    for data in (
        "82ad56cb" + _w(2**200),
        "82ad56cb" + _w(32) + _w(5),
        "252dba42" + _w(32) + _w(1) + _w(2**250),
        "21a0adb6" + _w(0),
        "",
    ):
        got = _read(data, POOL)
        assert got["root"] is None


def test_deep_nesting_is_bounded():
    data = _pool_call(ROOT, NULL)
    for _ in range(12):
        data = _aggregate([(MULTICALL, data)])
    got = _read(data, MULTICALL)
    assert got["root"] is None and got["source"] == "adjacency"


def test_withdrawal_root_needs_the_transaction_target():
    w = {"tx_hash": "0x" + "aa" * 32, "nullifier": "0x" + NULL}
    info = {"input": "0x" + _pool_call(ROOT, NULL), "to": POOL.upper().replace("0X", "0x")}
    assert withdrawal_root(_Client(info), w, POOL) == int(ROOT, 16)
    assert withdrawal_root(_Client({"input": info["input"]}), w, POOL) is None
    assert withdrawal_root(_Client(None), w, POOL) is None
    assert withdrawal_read(_Client(None), w, POOL)["source"] == "none"


# --- sub-calls that may fail silently; decoding budget ----------------------------------


def _aggregate3_flags(calls):
    """aggregate3 with an explicit allowFailure per call: [(target, allow, calldata)]."""
    tuples = [_w(t) + _w(int(a)) + _w(3 * 32) + _bytes(c) for t, a, c in calls]
    return "82ad56cb" + _w(32) + _array_of_dynamic(tuples)


def _aggregate3_value_flags(calls):
    tuples = [_w(t) + _w(int(a)) + _w(0) + _w(4 * 32) + _bytes(c) for t, a, c in calls]
    return "174dea71" + _w(32) + _array_of_dynamic(tuples)


def _try_aggregate_flag(require_success, calls, selector="bce38bd7"):
    tuples = [_w(t) + _w(2 * 32) + _bytes(c) for t, c in calls]
    return selector + _w(int(require_success)) + _w(64) + _array_of_dynamic(tuples)


OPAQUE = "0x" + "66" * 20


def test_decoy_withdraw_in_a_call_allowed_to_fail_is_not_a_root():
    # the old-root withdraw may revert silently; the real one runs through an opaque contract
    data = _aggregate3_flags(
        [(POOL, True, _pool_call(OLD, NULL)), (OPAQUE, False, "deadbeef" + _w(0))]
    )
    got = _read(data, MULTICALL)
    assert got["root"] is None and got["source"] == "unverified"
    assert got["candidates"] == [int(OLD, 16)]


@pytest.mark.parametrize(
    "data",
    [
        _aggregate3_value_flags([(POOL, True, _pool_call(OLD, NULL))]),
        _try_aggregate_flag(False, [(POOL, _pool_call(OLD, NULL))]),
        _try_aggregate_flag(False, [(POOL, _pool_call(OLD, NULL))], selector="399542e9"),
        _self_multicall([_pool_call(OLD, NULL)]),
    ],
    ids=["aggregate3Value", "tryAggregate", "tryBlockAndAggregate", "multicall"],
)
def test_every_failure_tolerant_frame_leaves_the_root_unverified(data):
    to = POOL if data.startswith("ac9650d8") else MULTICALL
    got = _read(data, to)
    assert got["root"] is None and got["source"] == "unverified"


def test_strict_frames_still_decode():
    strict = [
        _aggregate3_flags([(POOL, False, _pool_call(ROOT, NULL))]),
        _aggregate3_value_flags([(POOL, False, _pool_call(ROOT, NULL))]),
        _try_aggregate_flag(True, [(POOL, _pool_call(ROOT, NULL))]),
    ]
    for data in strict:
        assert _read(data, MULTICALL)["root"] == int(ROOT, 16)


def test_tolerance_is_inherited_by_nested_frames():
    inner = _aggregate([(POOL, _pool_call(OLD, NULL))])  # strict itself
    data = _aggregate3_flags([(MULTICALL, True, inner)])
    assert _read(data, MULTICALL)["source"] == "unverified"


def test_a_strict_root_wins_over_tolerant_ones():
    # both cannot have succeeded (one nullifier); the strict call did, or the tx would revert
    data = _aggregate3_flags(
        [(POOL, True, _pool_call(OLD, NULL)), (POOL, False, _pool_call(ROOT, NULL))]
    )
    got = _read(data, MULTICALL)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def _shared(inner_hex, n):
    """aggregate with ``n`` entries that all point at one shared element."""
    el = _w(0xCA) + _w(64) + _bytes(inner_hex)
    arr = _w(n) + "".join(_w(32 * n) for _ in range(n)) + el
    return "252dba42" + _w(32) + arr


def test_shared_offset_payload_finishes_fast_and_is_malformed():
    import time

    data = "00" * 4
    for _ in range(3):
        data = _shared(data, 256)
    t = time.perf_counter()
    got = _read(data, MULTICALL)
    assert time.perf_counter() - t < 2.0
    assert got["root"] is None and got["source"] == "malformed"


def test_oversized_input_is_malformed():
    data = _pool_call(ROOT, NULL, proof="ee" * 300_000)
    got = _read(data, POOL)
    assert got["root"] is None and got["source"] == "malformed"


def test_missing_nullifier_is_handled():
    got = read_root("0x" + _pool_call(ROOT, NULL), None, POOL, POOL)
    assert got == {"root": None, "source": "none", "candidates": []}


# --- a batch frame is strict only on a known Multicall deployment -----------------------

MULTICALL2 = "0x5ba1e12693dc8f9c48aad8770482f4739beed696"  # Ethereum only
CUSTOM = "0x" + "c5" * 20


def test_known_multicalls_per_chain_come_from_the_data_file():
    from tornado_demix.withdraw_root import MULTICALL3, known_multicall_rows, known_multicalls

    assert MULTICALL3 in known_multicalls(1) and MULTICALL2 in known_multicalls(1)
    assert known_multicalls(137) == {MULTICALL3}
    assert known_multicalls(None) == {MULTICALL3}
    assert known_multicalls(999999) == {MULTICALL3}  # deterministic deployment address
    rows = known_multicall_rows()
    assert rows and all(r["source_url"].startswith("https://") for r in rows)
    assert all(r["address"] == r["address"].lower() and len(r["address"]) == 42 for r in rows)


def test_custom_contract_with_a_strict_wrapper_selector_is_unverified():
    # a contract of the withdrawer's own with the aggregate selector may ignore failures
    data = _aggregate([(POOL, _pool_call(OLD, NULL)), (OPAQUE, "deadbeef")])
    got = _read(data, CUSTOM)
    assert got["root"] is None and got["source"] == "unverified"
    assert got["candidates"] == [int(OLD, 16)]


def test_multicall3_strict_frame_is_decoded():
    data = _aggregate([(POOL, _pool_call(ROOT, NULL))])
    got = _read(data, MULTICALL)
    assert got["source"] == "decoded" and got["root"] == int(ROOT, 16)


def test_multicall2_is_known_on_ethereum_only():
    from tornado_demix.withdraw_root import known_multicalls

    data = "0x" + _aggregate([(POOL, _pool_call(ROOT, NULL))])
    eth = read_root(data, "0x" + NULL, MULTICALL2, POOL, (ROUTER,), known_multicalls(1))
    assert eth["source"] == "decoded"
    poly = read_root(data, "0x" + NULL, MULTICALL2, POOL, (ROUTER,), known_multicalls(137))
    assert poly["source"] == "unverified"


def test_custom_wrapper_nested_in_multicall3_is_unverified():
    inner = _aggregate([(POOL, _pool_call(ROOT, NULL))])
    data = _aggregate([(CUSTOM, inner)])
    assert _read(data, MULTICALL)["source"] == "unverified"
