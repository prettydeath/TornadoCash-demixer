"""The early-profile window rule through the real analysis paths, the narrowing tool and notices."""

import os
import random
import sys
import time

from tests import test_semisynthetic as ts
from tests.fixtures import demix_result as fx
from tests.test_demix import RELAYER, _FakeClient, _tx, _wlog
from tests.test_early_profile import HIT, make
from tests.test_multi import ALICE, BOB, EXIT_A, POOL_1, POOL_10, T0, StubClient, network
from tornado_demix.demix import run_demix
from tornado_demix.heuristics import apply_heuristics
from tornado_demix.incomplete import notice_only, summary
from tornado_demix.multi import correlate
from tornado_demix.report_html import build_html_report

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))

HISTORY = "early profile not applied: withdrawal history incomplete"
WINDOW = "early profile not applied: window shorter than 72 h"
DAY = 86400


class _PoolClient(_FakeClient):
    """Logs per pool address; records a cut-off list on ``cut`` when asked to read it."""

    def __init__(self, txs, logs_by_pool, now=None, cut=None):
        super().__init__(txs, [])
        self.logs_by_pool = logs_by_pool
        self.truncated = []
        self.now = now
        self.cut = cut

    def get_logs(self, address, topic0, start_block, end_block):
        if self.cut and address.lower() == self.cut:
            self.truncated.append(
                {
                    "action": "getLogs",
                    "address": address.lower(),
                    "block": 1,
                    "limit": 1000,
                    "reason": "truncated",
                }
            )
        return self.logs_by_pool.get(address.lower(), [])

    def current_block(self):
        return int(self.now or time.time()) + 10**6


def _profile_run(base_ts, cut=None):
    """0.1 ETH x 6 and 1 ETH x 4 deposits from ``base_ts``; HIT takes 6 and 4 within 72 h."""
    txs, logs = [], {fx.POOL_01_ETH.lower(): [], fx.POOL_1_ETH.lower(): []}
    for pool, value, n in ((fx.POOL_01_ETH, 0.1, 6), (fx.POOL_1_ETH, 1.0, 4)):
        for i in range(n):
            txs.append(_tx(pool, value, base_ts + i * 60, 10 + i, f"0xdep{value}{i}"))
            logs[pool.lower()].append(
                _wlog(
                    HIT,
                    format(int(value * 100) * 100 + i, "064x"),
                    RELAYER,
                    10**15,
                    ts=base_ts + 3600 + i,
                    block=base_ts + 3600 + i,
                )
            )
    client = _PoolClient(txs, logs, cut=cut)
    return run_demix(client, fx.WALLET, network=fx.network(), window_days=30)


def _applied(data):
    return any("early_profile" in s for r in data["denoms"].values() for s in r["signals"].values())


def test_run_demix_applies_the_profile_when_everything_was_read():
    data = _profile_run(1_700_000_000)
    assert _applied(data) and "early_profile_note" not in data["heuristics"]


def test_run_demix_cut_off_pool_list_means_no_profile_and_a_note():
    data = _profile_run(1_700_000_000, cut=fx.POOL_1_ETH.lower())
    assert data["truncated"] and not _applied(data)
    assert data["heuristics"]["early_profile_note"] == HISTORY


def test_run_demix_deposit_younger_than_72_h_means_no_profile_and_a_note():
    data = _profile_run(int(time.time()) - 3600)
    assert not _applied(data)
    assert data["heuristics"]["early_profile_note"] == WINDOW
    assert data["params"]["read_ts"] > 0


def test_group_view_cut_off_pool_list_means_no_profile_and_a_note():
    class Cutting(StubClient):
        truncated: list = []

        def __init__(self, *a):
            super().__init__(*a)
            self.truncated = []

        def get_logs(self, address, topic0, start_block, end_block):
            if address.lower() == POOL_10:
                self.truncated.append(
                    {
                        "action": "getLogs",
                        "address": POOL_10,
                        "block": 1,
                        "limit": 9,
                        "reason": "truncated",
                    }
                )
            return super().get_logs(address, topic0, start_block, end_block)

    deposits = {
        ALICE: [(POOL_1, T0 + 60 * i) for i in range(6)],
        BOB: [(POOL_10, T0 + 600 + 60 * i) for i in range(4)],
    }
    withdrawals = {
        POOL_1: [(EXIT_A, T0 + 3600 * (i + 1)) for i in range(6)],
        POOL_10: [(EXIT_A, T0 + 3600 * (i + 7)) for i in range(4)],
    }
    corr = correlate(
        Cutting(deposits, withdrawals), [ALICE, BOB], window_days=30, network=network(), group=True
    )
    group = corr["group"]
    assert group["heuristics"]["early_profile_note"] == HISTORY
    assert not _applied(group)


def test_narrowing_never_widens_the_cached_window():
    import placebo_windows as pw

    data = make()
    data["params"]["exit_window_hours"] = 24
    assert pw.narrow(data, 720)["params"]["exit_window_hours"] == 24
    assert pw.narrow(data, 12)["params"]["exit_window_hours"] == 12
    assert "early_profile_note" in pw.narrow(data, 720)["heuristics"]


def test_semisynthetic_runs_keep_the_early_profile_by_construction(monkeypatch):
    monkeypatch.setitem(ts.sem.DELAYS, "now", (1.0, 0.01))
    beh = ts.sem.Behaviour("now", "single", **ts.NO_ERRORS)
    run = ts.background(pools=(ts.POOL, ts.POOL2))
    data, positives, _info = ts.sem.plant(run, {ts.POOL: 5, ts.POOL2: 5}, beh, random.Random(1))
    (exit_addr,) = {a for _pk, a in positives}
    assert any("early_profile" in r["signals"][exit_addr] for r in data["denoms"].values())
    assert "early_profile_note" not in data["heuristics"]
    short, _p, _i = ts.sem.plant(
        ts.background(pools=(ts.POOL, ts.POOL2)),
        {ts.POOL: 5, ts.POOL2: 5},
        beh,
        random.Random(1),
        window_hours=24,
    )
    assert short["heuristics"]["early_profile_note"] == WINDOW


def test_hole_between_vouchers_goes_through_apply_heuristics():
    import copy

    data = make()
    far = copy.deepcopy(data["vouchers"][0])
    far["first_ts"] = far["last_ts"] = data["vouchers"][0]["first_ts"] + 200 * 3600
    data["vouchers"].append(far)
    data["deposits"].append(dict(data["deposits"][0], ts=far["first_ts"], hash="0xlate"))
    data["params"]["exit_window_hours"] = 72
    for res in data["denoms"].values():
        res["pool"] = "0x" + "aa" * 20
    apply_heuristics(data, set(), is_contract=lambda a: False)
    assert data["heuristics"]["early_profile_note"] == HISTORY
    assert data["heuristics"]["early_profiles"] == []


def test_the_note_is_a_run_level_notice_even_without_candidates():
    data = make()
    data["params"]["exit_window_hours"] = 1
    apply_heuristics(data, set(), is_contract=lambda a: False)
    assert WINDOW in summary(data)
    assert summary(data).endswith("notice") and notice_only(data)
    assert WINDOW in build_html_report(data, fx.network())


def test_no_note_no_notice():
    data = make()
    apply_heuristics(data, set(), is_contract=lambda a: False)
    assert summary(data) == "" and not notice_only(data)
