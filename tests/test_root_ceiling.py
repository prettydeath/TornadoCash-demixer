import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
from root_ceiling import ceiling_share, fetch_deposit_index, leaves_before  # noqa: E402

INDEX = [(100, 0, "0xa"), (100, 1, "0xb"), (105, 2, "0xc"), (110, 3, "0xd"), (200, 4, "0xe")]


def test_leaves_before_counts_insertions_up_to_block_end():
    assert leaves_before(INDEX, 99) == 0
    assert leaves_before(INDEX, 100) == 2
    assert leaves_before(INDEX, 150) == 4


def test_leaves_before_offsets_by_first_leaf_of_a_partial_index():
    assert leaves_before(INDEX[2:], 99) == 2
    assert leaves_before(INDEX[2:], 105) == 3


def test_ceiling_share_counts_withdrawals_within_history():
    assert ceiling_share([1], [110], INDEX, history=5) == 1.0
    assert ceiling_share([1], [110], INDEX, history=2) == 0.0
    assert ceiling_share([1], [110, 200], INDEX, history=5) == 1.0


def test_ceiling_share_empty_inputs():
    assert ceiling_share([], [110], INDEX, history=5) == 0.0
    assert ceiling_share([1], [], INDEX, history=5) == 0.0


class _Client:
    def get_logs(self, address, topic0, start, end):
        def log(block, leaf, tx):
            return {
                "topics": ["0x0", "0xc"],
                "data": "0x" + format(leaf, "064x") + format(1, "064x"),
                "transactionHash": tx,
                "blockNumber": hex(block),
                "timeStamp": "0x1",
            }

        return [log(200, 4, "0xe"), log(100, 0, "0xa")]


def test_fetch_deposit_index_sorted_by_leaf():
    assert fetch_deposit_index(_Client(), "0xp", 0, 300) == [(100, 0, "0xa"), (200, 4, "0xe")]


# --- ROOT_HISTORY_SIZE per pool, read from the contract (no hard-coded 100/30) ---------


class _Pool:
    def __init__(self, key, address):
        self.key, self.address = key, address


class _HistoryClient:
    chain_id = 1

    def __init__(self, sizes):
        self.sizes = sizes
        self.calls = []

    def eth_call(self, to, data):
        self.calls.append(to)
        size = self.sizes.get(to)
        if size is None:
            from tornado_demix.errors import ApiError

            raise ApiError("not answered")
        return "0x" + format(size, "064x")


def test_pool_histories_reads_each_pool_and_caches(tmp_path):
    from root_ceiling import pool_histories

    pools = [_Pool("0.1 ETH", "0x" + "01" * 20), _Pool("100 ETH", "0x" + "02" * 20)]
    client = _HistoryClient({pools[0].address: 100, pools[1].address: 30})
    got = pool_histories(client, pools, str(tmp_path))
    assert got == {"0.1 ETH": 100, "100 ETH": 30}
    again = _HistoryClient({})
    assert pool_histories(again, pools, str(tmp_path)) == got and again.calls == []


def test_pool_histories_unreadable_pool_is_none(tmp_path):
    from root_ceiling import pool_histories

    pools = [_Pool("1 ETH", "0x" + "03" * 20)]
    assert pool_histories(_HistoryClient({}), pools, str(tmp_path)) == {"1 ETH": None}


def test_pool_rows_use_each_pool_history_and_skip_unread_pools():
    from root_ceiling import pool_rows

    per_pool = {
        "a": {"windows": [("w", 0, 300)], "deposits": {"w": [(100, "0xb")]}},
        "b": {"windows": [("w", 0, 300)], "deposits": {"w": [(100, "0xb")]}},
        "c": {"windows": [("w", 0, 300)], "deposits": {"w": [(100, "0xb")]}},
    }
    loaded = {k: (INDEX, [110]) for k in per_pool}
    # leaf 1, withdrawal at block 110 (4 leaves): history 5 can exclude, 2 cannot
    rows, missing, unchecked = pool_rows(["a", "b", "c"], loaded, per_pool, {"a": 5, "b": 2})
    assert rows["a"]["excludable"] == 1 and rows["a"]["history"] == 5
    assert rows["b"]["excludable"] == 0 and rows["b"]["history"] == 2
    assert "c" not in rows and unchecked == ["c"] and missing == 0


def test_ceiling_tool_has_no_global_history_argument():
    import root_ceiling

    src = open(root_ceiling.__file__, encoding="utf-8").read()
    assert "--history" not in src and "= 100;" not in src
