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
    assert "| of which no withdraw call decoded | - | - |" in text.replace("|   of", "| of")


# --- the shipped validated read (decoded withdraw call + root history at the block) ---

from root_exclusion_eval import validated_n_root  # noqa: E402


def _stats():
    return {
        "roots_ambiguous": 0,
        "roots_outside_history": 0,
        "roots_undecoded": 0,
        "roots_unverified": 0,
    }


def test_eval_imports_no_unvalidated_read():
    import root_exclusion_eval

    src = open(root_exclusion_eval.__file__, encoding="utf-8").read()
    assert "root_from_input" not in src and "candidate_roots" not in src


def test_validated_n_root_uses_the_root_history_at_the_withdrawal_block():
    index = {"0x" + format(501, "064x"): 5, "0x" + format(999, "064x"): 40}
    blocks = [1 + i for i in range(41)]  # one insertion per block, 41 in all
    entry = {"root": "0x" + format(501, "064x"), "source": "decoded"}
    stats = _stats()
    assert validated_n_root(entry, index, blocks, 41, 100, stats) == 5
    assert validated_n_root(entry, index, blocks, 41, 30, stats) is None
    assert stats["roots_outside_history"] == 1


def test_validated_n_root_unknown_without_a_decoded_root():
    stats = _stats()
    assert validated_n_root({"root": "", "source": "adjacency"}, {}, [1], 1, 100, stats) is None
    assert validated_n_root({"root": "", "source": "ambiguous"}, {}, [1], 1, 100, stats) is None
    assert validated_n_root(None, {}, [1], 1, 100, stats) is None
    assert validated_n_root({"root": "", "source": "unverified"}, {}, [1], 1, 100, stats) is None
    assert stats == {
        "roots_ambiguous": 1,
        "roots_outside_history": 0,
        "roots_undecoded": 2,
        "roots_unverified": 1,
    }


def test_failed_transaction_lookup_is_not_cached():
    from root_exclusion_eval import _read_entry

    assert _read_entry(None, "0x" + "22" * 32, "0x" + "a1" * 20, set()) is None
    entry = _read_entry(
        {"input": "0x", "to": "0x" + "a1" * 20}, "0x" + "22" * 32, "0x" + "a1" * 20, set()
    )
    assert entry == {"root": "", "source": "none"}
