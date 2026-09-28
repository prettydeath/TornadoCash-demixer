"""Exit groups: recipients withdrawn together in repeated bursts."""

from tornado_demix.groups import JOINT_WINDOW_S, co_withdrawal_groups, exit_groups


def _addr(i):
    return "0x" + format(i, "040x")


def _res(schedule):
    """schedule: {address: [timestamps]} -> a pool analysis shaped like demix's."""
    return {
        "counts": {a: len(ts) for a, ts in schedule.items()},
        "detail": {a: [{"ts": t} for t in ts] for a, ts in schedule.items()},
    }


# Three operator exits paid in the same three bursts; three unrelated recipients.
OPERATOR = {_addr(1): [0, 5000, 9000], _addr(2): [60, 5060, 9100], _addr(3): [120, 5100, 9200]}
OTHERS = {
    _addr(7): [30, 80_000],  # once in a burst, once far away
    _addr(8): [200_000, 300_000],
    _addr(9): [5030],  # a one-off withdrawal shows no rhythm
}


def test_exits_paid_in_repeated_joint_bursts_form_one_group():
    groups = co_withdrawal_groups(_res({**OPERATOR, **OTHERS}))
    assert groups == [set(OPERATOR)]


def test_one_joint_burst_is_not_enough():
    once = {_addr(1): [0, 50_000], _addr(2): [30, 90_000], _addr(3): [60, 130_000]}
    assert co_withdrawal_groups(_res(once)) == []


def test_the_joint_window_is_inclusive():
    w = JOINT_WINDOW_S
    edge = {_addr(1): [0, 10_000], _addr(2): [w, 10_000 + w], _addr(3): [2 * w, 10_000 + 2 * w]}
    assert co_withdrawal_groups(_res(edge)) == [set(edge)]


def _data(schedule, pool_key="100 ETH"):
    return {"denoms": {pool_key: _res(schedule)}}


def test_a_group_is_anchored_by_a_known_exit_and_lists_its_members():
    (group,) = exit_groups(_data({**OPERATOR, **OTHERS}), ranked=[], known_exits=[_addr(2)])
    assert group["anchored"] and group["anchors"] == [{"address": _addr(2), "why": "known exit"}]
    assert set(group["members"]) == set(OPERATOR) and group["notes"] == 9
    assert (group["first_ts"], group["last_ts"]) == (0, 9200)


def test_only_a_corroborated_candidate_of_the_same_pool_anchors_a_group():
    ranked = [
        {"pool_key": "100 ETH", "address": _addr(1), "band": "weak"},
        {"pool_key": "10 ETH", "address": _addr(2), "band": "strong"},
    ]
    (group,) = exit_groups(_data(OPERATOR), ranked)
    assert not group["anchored"]
    ranked.append({"pool_key": "100 ETH", "address": _addr(3), "band": "moderate"})
    (group,) = exit_groups(_data(OPERATOR), ranked)
    assert group["anchors"] == [{"address": _addr(3), "why": "moderate candidate"}]


def test_anchored_groups_come_first():
    second = {_addr(i + 20): [t + 100_000 for t in ts] for i, ts in enumerate(OPERATOR.values())}
    groups = exit_groups(_data({**OPERATOR, **second}), [], known_exits=[_addr(20)])
    assert [g["anchored"] for g in groups] == [True, False]
    assert _addr(20) in groups[0]["members"]


def test_the_report_lists_anchored_groups_and_counts_the_rest():
    from tests.test_report import _bsc_network
    from tornado_demix.report_html import _groups_html

    second = {_addr(i + 20): [t + 100_000 for t in ts] for i, ts in enumerate(OPERATOR.values())}
    groups = exit_groups(_data({**OPERATOR, **second}), [], known_exits=[_addr(1)])
    html = _groups_html(groups, _bsc_network())
    assert "Exit groups" in html and "1 group(s) without an anchor" in html
    assert "known exit" in html and _addr(3) in html and _addr(21) not in html
    assert _groups_html([], _bsc_network()) == ""
