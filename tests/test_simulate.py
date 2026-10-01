"""The synthetic benchmark runs end to end and its measures behave."""

import importlib.util
import os
import random

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "simulate.py")
_spec = importlib.util.spec_from_file_location("simulate", _PATH)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


def test_a_linked_exit_is_found_and_ranked_first():
    rng = random.Random(3)
    case = sim.Case(notes={sim.POOL_1: 3}, linked=True, gas_reuse=True)
    data, truth = sim.single_trial(rng, case, sim.Field(size=20))
    rows = sim.ranking(data, sim.FULL, rng)
    assert rows[0][0] in truth
    # one lead source (the link); gas reuse is context only
    assert rows[0][2] == "moderate"


def test_an_exit_outside_the_window_is_not_a_positive():
    rng = random.Random(4)
    case = sim.Case(notes={sim.POOL_1: 3}, delayed=3)
    _data, truth = sim.single_trial(rng, case, sim.Field(size=20))
    assert truth == set()


def test_ablation_metrics_are_rates():
    result = sim.experiment_ablation(trials=4, seed=1, sizes=[20])[20]
    assert set(result) == {"A", "B", "C", "D", "E", "F"}
    for metrics in result.values():
        for name, value in metrics.items():
            assert 0.0 <= value <= 1.0, name


def test_the_suppressed_graph_never_merges_more_than_the_naive_rule():
    for kind, m in sim.experiment_operators(trials=4, seed=1, size=20).items():
        assert m["merged_suppressed"] <= m["merged_naive"], kind


@pytest.mark.parametrize(
    "pairs, expected",
    [
        ([(2.0, True), (1.0, False)], 1.0),
        ([(2.0, False), (1.0, True)], 0.5),
        ([(1.0, True), (1.0, False)], 0.5),
        ([(None, True), (1.0, False)], 0.0),
    ],
)
def test_average_precision(pairs, expected):
    assert sim.average_precision(pairs) == pytest.approx(expected)
