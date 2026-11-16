import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
import bridge_coverage as bc  # noqa: E402

from tornado_demix.networks import get_network  # noqa: E402


def _bridge(protocol, state):
    return {"protocol": protocol, "state": state}


def _edge(kind="transfer", bridge=None, swap=None, hop=1):
    e = {"kind": kind, "hop": hop, "to": "0x" + "1" * 40}
    if bridge is not None:
        e["bridge"] = bridge
    if swap is not None:
        e["swap"] = swap
    return e


def _end(reason, bridge=None, label=None):
    t = {"address": "0x" + "2" * 40, "reason": reason, "amount": 1.0, "hop": 1, "label": label}
    if bridge is not None:
        t["bridge"] = bridge
    return t


def _result(edges=(), terminals=()):
    return {"edges": list(edges), "terminals": list(terminals)}


def test_wilson_matches_known_values():
    lo, hi = bc.wilson(0, 50)
    assert lo == 0.0 and 0.07 < hi < 0.075
    lo, hi = bc.wilson(25, 50)
    assert 0.365 < lo < 0.368 and 0.632 < hi < 0.636
    assert bc.wilson(0, 0) is None


def test_followed_bridge_state_is_followed():
    r = bc.classify_trace(
        _result([_edge("bridge", _bridge("across", "filled"))], [_end("hop limit")]), {}
    )
    assert r["category"] == "followed"
    assert r["protocols"] == {"across": "followed"}


@pytest.mark.parametrize("state", ["l2_credited", "minted", "same_chain_payout"])
def test_other_followed_states(state):
    r = bc.classify_trace(_result([_edge("bridge", _bridge("x", state))]), {})
    assert r["category"] == "followed"


def test_named_bridge_end_is_named_not_followed():
    r = bc.classify_trace(
        _result(
            [_edge("bridge", _bridge("relay", "destination_not_on_chain"))],
            [_end("bridge relay (request 0x1, destination not on chain)", _bridge("relay", "x"))],
        ),
        {},
    )
    assert r["category"] == "named"
    assert r["protocols"] == {"relay": "named"}


def test_followed_beats_named_beats_swap():
    r = bc.classify_trace(
        _result(
            [
                _edge("swap"),
                _edge("bridge", _bridge("layerswap", "off_chain_matching")),
                _edge("bridge", _bridge("arbitrum", "l2_credited")),
            ]
        ),
        {},
    )
    assert r["category"] == "followed"
    assert r["protocols"] == {"layerswap": "named", "arbitrum": "followed"}


def test_protocol_followed_wins_over_named_for_the_same_protocol():
    r = bc.classify_trace(
        _result(
            [
                _edge("bridge", _bridge("across", "fill_not_found")),
                _edge("bridge", _bridge("across", "filled")),
            ]
        ),
        {},
    )
    assert r["protocols"] == {"across": "followed"}


def test_swap_edge_and_cow_swap():
    assert bc.classify_trace(_result([_edge("swap")]), {})["category"] == "swap"
    r = bc.classify_trace(
        _result([_edge("transfer", swap={"protocol": "cow", "state": "trade_not_tied"})]), {}
    )
    assert r["category"] == "swap"
    assert r["swap_protocols"] == ["cow"]


def test_exchange_label_and_other_label():
    ex = bc.classify_trace(
        _result(terminals=[_end("labelled address", label={"category": "exchange"})]), {}
    )
    assert ex["category"] == "exchange"
    other = bc.classify_trace(
        _result(terminals=[_end("labelled address", label={"category": "mixer"})]), {}
    )
    assert other["category"] == "other_labelled"
    assert other["label_categories"] == ["mixer"]
    rows = [{"category": "other_labelled", "protocols": {}, "label_categories": ["defi"]}]
    summ = bc.summarise(rows)
    assert summ["other_label_categories"] == {"defi": 1}
    meta = {"start_block": 1, "end_block": 2, "seed": 1, "date": "d"}
    assert "defi 1" in bc.render(summ, meta)


def test_unknown_contract_redeposit_unread_and_eoa():
    unknown = _result(terminals=[_end("contract without a payment back")])
    assert bc.classify_trace(unknown, {})["category"] == "unknown_contract"
    redeposit = _result(terminals=[_end("tornado_redeposit")])
    assert bc.classify_trace(redeposit, {})["category"] == "tornado_redeposit"
    unread = _result(terminals=[_end("lookup_failed")])
    assert bc.classify_trace(unread, {})["category"] == "unread"
    for reason in ("hop limit", "not moved on (held or untraced)", "below_threshold"):
        assert bc.classify_trace(_result(terminals=[_end(reason)]), {})["category"] == "eoa"


def test_priority_between_the_plain_end_states():
    r = bc.classify_trace(
        _result(terminals=[_end("hop limit"), _end("lookup_failed"), _end("tornado_redeposit")]),
        {},
    )
    assert r["category"] == "tornado_redeposit"
    r = bc.classify_trace(_result(terminals=[_end("hop limit"), _end("lookup_failed")]), {})
    assert r["category"] == "unread"


def test_aggregator_unknown_is_named():
    end = _end(
        "aggregator lifi (bridge or swap unknown)", _bridge("lifi", "bridge_or_swap_unknown")
    )
    r = bc.classify_trace(_result(terminals=[end]), {})
    assert r["category"] == "named"


def test_summarise_counts_shares_and_intervals():
    rows = [
        {"category": "followed", "protocols": {"across": "followed"}},
        {"category": "named", "protocols": {"relay": "named", "across": "named"}},
        {"category": "eoa", "protocols": {}},
        {"category": "eoa", "protocols": {}},
    ]
    s = bc.summarise(rows)
    assert s["n"] == 4
    assert s["by_category"]["eoa"]["count"] == 2
    assert s["by_category"]["eoa"]["share"] == 0.5
    assert s["by_category"]["eoa"]["ci"] == bc.wilson(2, 4)
    assert s["any_bridge"]["count"] == 2
    assert s["by_protocol"]["across"] == {
        "followed": 1,
        "named": 1,
        "any": 2,
        "ci": bc.wilson(2, 4),
    }
    assert s["by_protocol"]["relay"]["named"] == 1


def _withdrawal(i, fee_wei=0):
    return {"to": f"0x{i:040x}", "tx_hash": f"0x{i:064x}", "block": 100 + i, "fee_wei": fee_wei}


def test_sample_is_reproducible_and_changes_with_seed(monkeypatch):
    net = get_network("ethereum")
    by_pool = {}
    for i in range(60):
        by_pool.setdefault(net.pools[i % len(net.pools)].address, []).append(_withdrawal(i))

    def fake_fetch(client, pool_address, start, end):
        return [w for w in by_pool.get(pool_address, []) if start <= w["block"] <= end]

    monkeypatch.setattr(bc, "fetch_withdrawals", fake_fetch)
    a = bc.sample_recipients(None, net, 100, 200, 10, 1)
    b = bc.sample_recipients(None, net, 100, 200, 10, 1)
    c = bc.sample_recipients(None, net, 100, 200, 10, 2)
    assert a == b and a != c
    assert len(a) == 10 and len({x["address"] for x in a}) == 10
    assert all({"address", "block", "tx_hash", "amount", "pool"} <= set(x) for x in a)


def test_sample_keeps_the_first_withdrawal_of_a_recipient(monkeypatch):
    net = get_network("ethereum")
    pool = net.pools[0]
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
    rows = [
        {"to": a, "tx_hash": "0x1", "block": 5, "fee_wei": 0},
        {"to": a, "tx_hash": "0x2", "block": 9, "fee_wei": 0},
        {"to": b, "tx_hash": "0x3", "block": 7, "fee_wei": 10**15},
    ]
    monkeypatch.setattr(
        bc, "fetch_withdrawals", lambda c, p, s, e: rows if p == pool.address else []
    )
    out = {x["address"]: x for x in bc.sample_recipients(None, net, 0, 100, 10, 1)}
    assert set(out) == {a, b}
    assert out[a]["tx_hash"] == "0x1"
    assert out[b]["amount"] == pytest.approx(pool.denom - 0.001)


def test_render_states_parameters_and_limits():
    rows = [{"category": "followed", "protocols": {"across": "followed"}, "address": "0x1"}]
    text = bc.render(
        bc.summarise(rows),
        {"n": 1, "seed": 7, "start_block": 10, "end_block": 20, "date": "2026-11-14"},
    )
    assert "seed 7" in text and "2026-11-14" in text
    assert "10" in text and "20" in text
    assert "exploratory" in text.lower()
    assert "across" in text


def test_the_default_sample_is_150_recipients(monkeypatch):
    import argparse

    seen = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        seen["ns"] = original(self, [], namespace)
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(SystemExit):
        bc.main([])
    assert seen["ns"].n == 150
