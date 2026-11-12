import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
from root_exclusion_eval import _render, _shard  # noqa: E402


def _kind(excluded):
    return {
        "candidates": {"moderate": 4},
        "withdrawals_checked": 10,
        "roots_found": 9,
        "roots_not_found": 1,
        "withdrawals_excluded": excluded,
        "withdrawals_note_partial": 2,
        "candidates_fully_excluded": {"moderate": 1} if excluded else {},
        "candidates_partly_excluded": {},
        "real_leaf_sanity": {"withdrawals_excluded": 10, "candidates_fully_excluded": {}},
    }


def test_shard_deals_items_round_robin():
    assert _shard([1, 2, 3, 4, 5], 2) == [[1, 3, 5], [2, 4]]


def test_render_writes_a_table_per_network(tmp_path):
    summary = {
        "dir": "x",
        "depositors": 5,
        "weak_depositors": 2,
        "skipped_candidate_rows": {"decoy/weak": 3},
        "kinds": {"target": _kind(0), "decoy": _kind(5)},
    }
    (tmp_path / "summary_polygon.json").write_text(json.dumps(summary), encoding="utf-8")
    _render(str(tmp_path))
    text = (tmp_path / "RESULTS.md").read_text(encoding="utf-8")
    assert "## polygon (x, 5 depositors" in text
    assert "| withdrawals excluded | 0 | 5 |" in text
    assert "decoy/weak 3" in text
