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
