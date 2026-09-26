"""tools/sensitivity.py: re-scoring saved results under perturbed weights."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import sensitivity  # noqa: E402

from tornado_demix.heuristics import SIGNAL_WEIGHTS  # noqa: E402


def _data(candidates):
    """candidates: {address: (signals, discrimination)} in one pool."""
    detail = {a: [{"value": 1.0, "ts": 1, "hash": "0x1"}] * 2 for a in candidates}
    return {
        "vouchers": [{"pool_key": "1 ETH", "count": 2}],
        "denoms": {
            "1 ETH": {
                "denom": 1.0,
                "asset": "ETH",
                "counts": {a: 2 for a in candidates},
                "detail": detail,
                "target_counts": [2],
                "unique_recipients": 40,
                "signals": {a: s for a, (s, _d) in candidates.items()},
                "discrimination": {a: d for a, (_s, d) in candidates.items()},
                "confidence": {a: 0.0 for a in candidates},
            }
        },
        "heuristics": {"gas_price_matches": []},
    }


def test_weights_are_restored_after_each_sample():
    before = dict(SIGNAL_WEIGHTS)
    sensitivity.analyse(_data({"0xa": (["count_match", "linked"], 1.0)}), samples=20)
    assert SIGNAL_WEIGHTS == before


def test_a_strong_lead_keeps_its_place_above_weaker_bands():
    data = _data(
        {
            "0xa": (["count_match", "linked"], 0.6),  # strong
            "0xb": (["count_match", "self_relayed"], 1.0),  # moderate
            "0xc": (["count_match"], 1.0),  # weak
        }
    )
    s = sensitivity.analyse(data, samples=300, spread=0.9)
    assert s["reference"] == "0xa" and s["band"] == "strong"
    assert s["keeps_rank"] == 1.0 and s["worst_rank"] == 1


def test_order_within_a_band_can_move():
    data = _data(
        {
            "0xa": (["count_match", "gas_price"], 0.7),
            "0xb": (["count_match", "linked"], 0.7),
        }
    )
    s = sensitivity.analyse(data, samples=300, spread=0.9)
    assert 0.0 < s["same_top"] < 1.0
    assert s["mean_tau"] < 1.0


def test_kendall_tau_of_identical_and_reversed_orders():
    assert sensitivity.kendall_tau([1, 2, 3], [1, 2, 3]) == 1.0
    assert sensitivity.kendall_tau([1, 2, 3], [3, 2, 1]) == -1.0
