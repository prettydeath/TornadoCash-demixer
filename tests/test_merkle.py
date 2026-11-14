"""MiMCSponge and the Tornado.Cash tree zero values.

Zero values were read from the deployed Ethereum 0.1 ETH pool
0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc (verified source, solc 0.5.11):
``ZERO_VALUE`` is the constant ``keccak256("tornado") % FIELD_SIZE`` and the
constructor fills the public array ``zeros`` with
``zeros[i] = hashLeftRight(zeros[i - 1], zeros[i - 1])``. ``zeros(i)`` below was
read with ``eth_call`` to that pool's ``zeros(uint256)`` on 2026-11-12. The
Polygon pools (solc 0.7.6, ROOT_HISTORY_SIZE 30) have the same ZERO_VALUE and
the same ``hashLeftRight`` (two MiMCSponge calls with k = 0), so the values
apply to both.
"""

from __future__ import annotations

import os
import time

import pytest

from tornado_demix.merkle import FIELD, LEVELS, ZERO_VALUE, hash_left_right, mimc_sponge, zeros

# eth_call zeros(i) on 0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc, 2026-11-12.
CHAIN_ZEROS = {
    0: "0x2fe54c60d3acabf3343a35b6eba15db4821b340f76e741e2249685ed4899af6c",
    1: "0x256a6135777eee2fd26f54b8b7037a25439d5235caee224154186d2b8a52e31d",
    2: "0x1151949895e82ab19924de92c40a3d6f7bcb60d92b00504b8199613683f0c200",
    19: "0x198622acbd783d1b0d9064105b1fc8e4d8889de95c4c519b3f635809fe6afc05",
}


def test_zero_value_is_keccak_tornado_mod_field():
    # tornado-core: uint256(keccak256("tornado")) % FIELD_SIZE
    assert (
        ZERO_VALUE == 21663839004416932945382355908790599225266501822907911457504978515578255421292
    )


def test_first_levels_match_the_contract():
    assert zeros(0) == ZERO_VALUE
    assert hex(zeros(1)) == CHAIN_ZEROS[1]
    assert hex(zeros(2)) == CHAIN_ZEROS[2]


def test_top_level_matches_the_contract():
    assert LEVELS == 20
    assert zeros(19) == int(CHAIN_ZEROS[19], 16)


def test_zero_value_hex_matches_the_chain():
    assert int(CHAIN_ZEROS[0], 16) == ZERO_VALUE


def test_constants_file_is_well_formed():
    from tornado_demix.merkle import _constants

    cts = _constants()
    assert len(cts) == 220
    assert cts[0] == 0 and cts[-1] == 0
    assert all(0 <= c < FIELD for c in cts)
    # circomlib's first non-zero round constant
    assert cts[1] == 7120861356467848435263064379192047478074060781135320967663101236819528304084


def test_hash_left_right_rejects_values_outside_the_field():
    with pytest.raises(ValueError):
        hash_left_right(FIELD, 0)
    with pytest.raises(ValueError):
        hash_left_right(0, FIELD)
    with pytest.raises(ValueError):
        hash_left_right(-1, 0)


def test_hash_left_right_is_not_symmetric():
    assert hash_left_right(1, 2) != hash_left_right(2, 1)


def test_mimc_sponge_returns_field_elements():
    xl, xr = mimc_sponge(1, 2)
    assert 0 <= xl < FIELD and 0 <= xr < FIELD


def test_hash_left_right_is_deterministic_and_in_field():
    hypothesis = pytest.importorskip("hypothesis")
    st = hypothesis.strategies

    @hypothesis.settings(deadline=None, max_examples=25)
    @hypothesis.given(st.integers(0, FIELD - 1), st.integers(0, FIELD - 1))
    def check(left, right):
        a = hash_left_right(left, right)
        assert a == hash_left_right(left, right)
        assert 0 <= a < FIELD

    check()


def test_one_hash_takes_well_under_a_second():
    hash_left_right(1, 2)  # warm the constants cache
    start = time.perf_counter()
    hash_left_right(3, 4)
    assert time.perf_counter() - start < 1.0


@pytest.mark.live
def test_zeros_match_every_deployed_ethereum_pool():
    """Opt-in: python -m pytest -m live tests/test_merkle.py -v"""
    from tornado_demix.networks import get_network
    from tornado_demix.rpc import RpcClient

    net = get_network("ethereum")
    client = RpcClient(net.rpc_url)
    selector = "0xe8295588"  # zeros(uint256)
    for pool in net.pools:
        for level in range(LEVELS):
            raw = client.eth_call(pool.address, selector + format(level, "064x"))
            assert raw is not None, "{} zeros({}) unanswered".format(pool.key, level)
            assert int(raw, 16) == zeros(level), "{} zeros({})".format(pool.key, level)


# --- incremental tree, root index, disk cache --------------------------------

import json  # noqa: E402

from tornado_demix.errors import ApiError  # noqa: E402
from tornado_demix.merkle import (  # noqa: E402
    IncrementalTree,
    cached_root_index,
    leaves_at_block,
    root_history_size,
    root_index,
    root_known_at,
)
from tornado_demix.pools import Pool  # noqa: E402


def test_empty_tree_root_is_top_zero():
    assert IncrementalTree().root == zeros(LEVELS)
    assert IncrementalTree().size == 0


def test_one_leaf_root_matches_manual_fold():
    leaf = 12345
    node = leaf
    for lvl in range(LEVELS):
        node = hash_left_right(node, zeros(lvl))
    t = IncrementalTree()
    assert t.insert(leaf) == node
    assert t.root == node
    assert t.size == 1


def test_two_leaves_root_matches_manual_fold():
    a, b = 11, 22
    node = hash_left_right(a, b)
    for lvl in range(1, LEVELS):
        node = hash_left_right(node, zeros(lvl))
    t = IncrementalTree()
    t.insert(a)
    assert t.insert(b) == node


def test_state_roundtrip_resumes_identically():
    t = IncrementalTree()
    for leaf in (5, 6, 7):
        t.insert(leaf)
    state = json.loads(json.dumps(t.state()))
    assert state["size"] == 3
    assert state["root"] == hex(t.root) or state["root"] == "0x{:064x}".format(t.root)
    resumed = IncrementalTree.from_state(state)
    assert resumed.root == t.root and resumed.size == 3
    assert resumed.insert(8) == t.insert(8)


def test_from_state_rejects_inconsistent_state():
    t = IncrementalTree()
    t.insert(1)
    state = t.state()
    state["size"] = 3
    with pytest.raises(ValueError):
        IncrementalTree.from_state(state)


def test_root_index_maps_each_root_to_leaf_count_and_resumes():
    idx, state = root_index([1, 2, 3])
    assert sorted(idx.values()) == [0, 1, 2, 3]
    idx2, _ = root_index([4], start_state=state)
    full, _ = root_index([1, 2, 3, 4])
    assert full[max(full, key=full.get)] == 4
    assert set(idx2) <= set(full)
    assert idx2 == {r: n for r, n in full.items() if n == 4}


def _log(leaf_index, block, commitment):
    return {
        "topics": ["0xdeposit", "0x{:064x}".format(commitment)],
        "data": "0x{:064x}{:064x}".format(leaf_index, 1_700_000_000 + leaf_index),
        "transactionHash": "0x{:064x}".format(1000 + leaf_index),
        "blockNumber": hex(block),
        "timeStamp": hex(1_700_000_000 + block),
        "logIndex": "0x0",
    }


class FakeClient:
    chain_id = 1

    def __init__(self, logs):
        self.logs = logs
        self.calls = []

    def get_logs(self, address, topic0, start_block, end_block):
        self.calls.append((address, start_block, end_block))
        return [lg for lg in self.logs if start_block <= int(lg["blockNumber"], 16) <= end_block]


POOL = Pool("0x" + "ab" * 20, 1, "ETH")
COMMITS = [101, 102, 103, 104]


def _all_logs():
    return [_log(i, 100 + 10 * i, c) for i, c in enumerate(COMMITS)]


def test_cached_root_index_second_call_fetches_only_new_blocks(tmp_path):
    client = FakeClient(_all_logs())
    first = cached_root_index(client, POOL, 120, str(tmp_path))
    assert first["state"]["size"] == 3 and first["end_block"] == 120
    assert sorted(first["roots"].values()) == [0, 1, 2, 3]
    second = cached_root_index(client, POOL, 200, str(tmp_path))
    assert client.calls[-1][1:] == (121, 200)
    assert second["state"]["size"] == 4
    expected, _ = root_index(COMMITS)
    assert {int(k, 16): v for k, v in second["roots"].items()} == expected
    path = tmp_path / "{}_{}.json".format(1, POOL.address)
    saved = json.loads(path.read_text())
    assert saved["pool"] == POOL.address and saved["chain"] == 1 and saved["end_block"] == 200
    # an already-covered range is not fetched again
    n_calls = len(client.calls)
    cached_root_index(client, POOL, 150, str(tmp_path))
    assert len(client.calls) == n_calls


def test_cached_root_index_gap_raises_and_writes_nothing(tmp_path):
    logs = [lg for lg in _all_logs() if lg["data"][2:66] != "{:064x}".format(1)]
    with pytest.raises(ApiError):
        cached_root_index(FakeClient(logs), POOL, 200, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_cached_root_index_non_sequential_after_cache_raises(tmp_path):
    cached_root_index(FakeClient(_all_logs()), POOL, 120, str(tmp_path))
    bad = [_log(5, 150, 999)]  # next leaf must be 3
    with pytest.raises(ApiError, match=r"delete .*\.json") as exc:
        cached_root_index(FakeClient(bad), POOL, 200, str(tmp_path))
    assert str(tmp_path) in str(exc.value)


def test_cached_root_index_duplicated_leaf_index_raises(tmp_path):
    logs = _all_logs()
    logs.append(_log(2, 121, COMMITS[2]))  # leaf 2 returned again across the block boundary
    with pytest.raises(ApiError):
        cached_root_index(FakeClient(logs), POOL, 200, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.live
def test_root_over_all_deposits_matches_get_last_root_ethereum_1_eth():
    """Opt-in (slow: ~20 hashes per deposit): python -m pytest -m live -k get_last_root"""
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
    from ens_labels import api_keys

    from tornado_demix.etherscan import EtherscanClient
    from tornado_demix.networks import get_network
    from tornado_demix.rpc import RpcClient

    net = get_network("ethereum")
    pool = net.by_key["1 ETH"]
    client = EtherscanClient(api_keys()[0], pause=0.36, **net.client_kwargs())
    rpc = RpcClient(net.rpc_url)
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", "merkle")
    for _attempt in range(3):
        head = client.current_block()
        cache = cached_root_index(client, pool, head, cache_dir)
        last = rpc.eth_call(pool.address, "0xba70f757")  # getLastRoot()
        assert last is not None
        if int(last, 16) == int(cache["state"]["root"], 16):
            break
    else:
        pytest.fail("root mismatch: ours {} chain {}".format(cache["state"]["root"], last))


def test_cached_root_index_records_the_block_of_every_leaf(tmp_path):
    client = FakeClient(_all_logs())
    first = cached_root_index(client, POOL, 120, str(tmp_path))
    assert first["blocks"] == [100, 110, 120]
    second = cached_root_index(client, POOL, 200, str(tmp_path))
    assert second["blocks"] == [100, 110, 120, 130]


def test_cache_written_without_blocks_is_completed_from_the_logs(tmp_path):
    client = FakeClient(_all_logs())
    cached_root_index(client, POOL, 120, str(tmp_path))
    path = tmp_path / "{}_{}.json".format(1, POOL.address)
    old = json.loads(path.read_text())
    del old["blocks"]
    path.write_text(json.dumps(old))
    again = cached_root_index(client, POOL, 120, str(tmp_path))
    assert again["blocks"] == [100, 110, 120]
    assert again["roots"] == old["roots"] and again["state"] == old["state"]
    assert json.loads(path.read_text())["blocks"] == [100, 110, 120]


def test_cache_without_blocks_that_disagrees_with_the_logs_raises(tmp_path):
    client = FakeClient(_all_logs())
    cached_root_index(client, POOL, 120, str(tmp_path))
    path = tmp_path / "{}_{}.json".format(1, POOL.address)
    old = json.loads(path.read_text())
    del old["blocks"]
    path.write_text(json.dumps(old))
    with pytest.raises(ApiError):
        cached_root_index(FakeClient(_all_logs()[:2]), POOL, 120, str(tmp_path))


def test_leaves_at_block_counts_before_and_up_to_the_block():
    blocks = [100, 110, 110, 130]
    assert leaves_at_block(blocks, 99) == (0, 0)
    assert leaves_at_block(blocks, 110) == (1, 3)
    assert leaves_at_block(blocks, 120) == (3, 3)
    assert leaves_at_block(blocks, 500) == (4, 4)


def test_root_known_at_follows_the_history_window():
    blocks = list(range(1, 201))  # leaf i inserted in block i + 1; 200 leaves
    # at block 201 the pool holds 200 leaves: roots after 101..200 insertions are accepted (100)
    assert root_known_at(200, blocks, 201, 100)
    assert root_known_at(101, blocks, 201, 100)
    assert not root_known_at(100, blocks, 201, 100)
    # a history of 30 accepts only 171..200
    assert root_known_at(171, blocks, 201, 30)
    assert not root_known_at(170, blocks, 201, 30)
    # a root that does not exist yet is not accepted
    assert not root_known_at(150, blocks, 100, 100)


def test_root_known_at_same_block_uncertainty_favours_acceptance():
    blocks = [10, 10, 10, 10]
    assert root_known_at(4, blocks, 10, 100)  # a later deposit of the block may be before the tx
    assert root_known_at(0, blocks, 10, 1)  # ... or after it, with nothing inserted yet


class _CallClient:
    chain_id = 1

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def eth_call(self, to, data):
        self.calls.append((to, data))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_root_history_size_is_read_from_the_contract_once_and_cached(tmp_path):
    client = _CallClient("0x" + format(30, "064x"))
    assert root_history_size(client, POOL, str(tmp_path)) == 30
    assert client.calls == [(POOL.address, "0xcd87a3b4")]
    assert root_history_size(_CallClient(ApiError("down")), POOL, str(tmp_path)) == 30
    assert (tmp_path / "{}_{}.history.json".format(1, POOL.address)).exists()


def test_root_history_size_cache_is_written_atomically(tmp_path, monkeypatch):
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    assert root_history_size(_CallClient("0x" + format(30, "064x")), POOL, str(tmp_path)) == 30
    assert len(replaced) == 1 and replaced[0][0].endswith(".tmp")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "{}_{}.history.json".format(1, POOL.address)
    ]


@pytest.mark.parametrize("answer", [ApiError("down"), "0x", "0x" + "0" * 64, None, "zz"])
def test_root_history_size_unreadable_raises_and_caches_nothing(tmp_path, answer):
    with pytest.raises(ApiError):
        root_history_size(_CallClient(answer), POOL, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_root_history_size_needs_a_client_that_can_call(tmp_path):
    with pytest.raises(ApiError):
        root_history_size(FakeClient([]), POOL, str(tmp_path))
