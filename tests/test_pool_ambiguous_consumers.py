"""Deposits whose twin pool is uncertain (``pool_ambiguous``): profile, root check, reports."""

import csv

from tests.fixtures import demix_result as fx
from tests.test_early_profile import HIT, PROFILE, make
from tornado_demix import incomplete
from tornado_demix.heuristics import apply_heuristics, wallet_profile
from tornado_demix.report_csv import write_demix_csv
from tornado_demix.report_html import build_html_report, deposits_table_html
from tornado_demix.root_exclusion import check_candidates

KEYS = ["10 AVAX", "10 AVAX#2"]


def _ambiguous_deposit(ts=5_000_000, h="0xamb"):
    return {
        "pool_key": "1 ETH",
        "denom": 1.0,
        "asset": "ETH",
        "ts": ts,
        "block": 99,
        "hash": h,
        "to": "0x" + "9" * 40,
        "via": "router",
        "gas_price": 1,
        "pool_ambiguous": ["1 ETH", "1 ETH#2"],
    }


def _all_ambiguous():
    return {
        "wallet": fx.WALLET,
        "params": {"window_days": 30, "mode": "events", "network": "ethereum", "currency": "ETH"},
        "deposits": [_ambiguous_deposit(), _ambiguous_deposit(5_000_100, "0xamb2")],
        "vouchers": [],
        "denoms": {},
        "unresolved": [],
        "errors": [incomplete.pool_ambiguous("0xamb", ["1 ETH", "1 ETH#2"], "no receipt read")],
        "error_count": 1,
        "truncated": [],
    }


# A: profile --------------------------------------------------------------------------


def test_wallet_profile_skips_ambiguous_deposits():
    data = make()
    base = wallet_profile(data)
    data["deposits"].append(_ambiguous_deposit())
    assert wallet_profile(data) == base == PROFILE


def test_early_profile_is_not_applied_while_a_deposits_pool_is_uncertain():
    """The profile without the uncertain note is only part of the fingerprint: a recipient
    matching that part is no early_profile lead, and the reason is recorded and shown."""
    data = make()
    apply_heuristics(data, counterparties=set())
    assert "early_profile" in data["denoms"]["1 ETH"]["signals"][HIT]
    assert "early_profile_note" not in data["heuristics"]
    data = make()
    data["deposits"].append(_ambiguous_deposit(ts=9_000_000))
    apply_heuristics(data, counterparties=set())
    assert "early_profile" not in data["denoms"]["1 ETH"]["signals"].get(HIT, [])
    assert data["heuristics"]["early_profile_note"] == "early profile not applied: pool uncertain"
    assert data["heuristics"]["early_profiles"] == []
    from tornado_demix.heuristics import candidate_evidence

    ev = candidate_evidence(data, "1 ETH", HIT)
    detail = next(e["detail"] for e in ev if e["signal"] == "early_profile")
    assert "early profile not applied: pool uncertain" in detail


# B: root check ----------------------------------------------------------------------------


def test_root_check_does_not_feed_an_ambiguous_deposit_to_wallet_notes(monkeypatch):
    from tornado_demix import root_exclusion as rx

    seen = []

    def fake_notes(client, pool, deposits, shifted):
        seen.append([d["hash"] for d in deposits])
        return None

    monkeypatch.setattr(rx, "wallet_notes", fake_notes)
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    data["deposits"].append(_ambiguous_deposit())

    class NoLogs:
        def get_logs(self, *args):
            return []

    out = check_candidates(NoLogs(), data, fx.network(), None)
    assert all("0xamb" not in hashes for hashes in seen)
    # its leaf is in no tree: the pools it may belong to stay unchecked
    assert "1 ETH" in out["stats"]["unchecked_reasons"]


# C: all deposits ambiguous --------------------------------------------------------------


def test_all_ambiguous_report_does_not_claim_no_deposits():
    html = build_html_report(_all_ambiguous(), fx.network())
    assert "made no deposits" not in html and "No Tornado.Cash deposits found" not in html
    assert "2 deposits found" in html or "2 Tornado.Cash deposits found" in html
    assert "pool could not be settled" in html and "none searched" in html
    assert 'data-count="deposited" data-value="2"' in html


def test_no_deposits_at_all_keeps_the_old_wording():
    data = _all_ambiguous()
    data.update(deposits=[], errors=[], error_count=0)
    html = build_html_report(data, fx.network())
    assert "No Tornado.Cash deposits found" in html


# D: table and CSV -----------------------------------------------------------------------


def test_deposits_table_shows_uncertain_pool_and_counts_them():
    data = fx.two_pool_result()
    data["deposits"].append(_ambiguous_deposit())
    html = deposits_table_html(data, fx.network())
    assert "uncertain: 1 ETH / 1 ETH#2" in html
    assert "1 deposit with an uncertain pool" in html


def test_deposits_csv_has_a_pool_ambiguous_column(tmp_path):
    data = fx.two_pool_result()
    data["deposits"].append(_ambiguous_deposit())
    files = write_demix_csv(data, str(tmp_path), fx.network())
    path = next(f for f in files if f.endswith("deposits.csv"))
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    amb = rows[-1]
    assert amb["pool_ambiguous"] == "1 ETH / 1 ETH#2" and amb["pool"] == "uncertain"
    assert all(r["pool_ambiguous"] == "" for r in rows[:-1])


# B2: an uncertain-twin deposit never makes the root check remove a real exit ------------

TWIN = "0x" + "fe" * 20  # a second 0.1 ETH contract: key "0.1 ETH#2"


def _twin_network():
    from tornado_demix.networks import Network
    from tornado_demix.pools import Pool

    return Network("ethereum", 1, "ETH", [*fx.network().pools, Pool(TWIN, 0.1, "ETH", 18, None)])


def _rooted(monkeypatch, amb_hash, shifted=False):
    """The root-check fixture (wallet notes at leaves 10-12, every WINNER withdrawal proved
    against a 5-leaf root) plus one uncertain-twin deposit made at block 4."""
    from tests import test_root_exclusion as tr
    from tornado_demix.constants import TOPIC_DEPOSIT
    from tornado_demix.demix import run_demix

    class TwinClient(tr.RootClient):
        def get_logs(self, address, topic0, start_block, end_block):
            if topic0 == TOPIC_DEPOSIT and address.lower() == TWIN:
                return []  # the twin's tree holds none of these notes
            return super().get_logs(address, topic0, start_block, end_block)

    tr._index(monkeypatch, {501: 5, 999: 40})
    client = TwinClient(
        {0: 501, 1: 501, 2: 501}, extra_txs=[tr._tx(tr.WINNER, 0.5, 1050, 15, "0xfund")]
    )
    net = _twin_network()
    data = run_demix(client, fx.WALLET, network=net)
    data["deposits"].append(
        {
            "pool_key": "0.1 ETH",
            "denom": 0.1,
            "asset": "ETH",
            "ts": 905,
            "block": 4,
            "hash": amb_hash,
            "to": "0x" + "9" * 40,
            "via": "router",
            "pool_ambiguous": ["0.1 ETH", "0.1 ETH#2"],
        }
    )
    if shifted:
        data["params"]["deposit_shift_seconds"] = 3600
    errors = []
    return check_candidates(client, data, net, None, errors=errors), errors, tr.WINNER


def test_an_uncertain_deposit_placed_by_its_leaf_keeps_the_exit(monkeypatch):
    # 0xold3 is leaf 3 of the 0.1 ETH tree: the 5-leaf root holds it, nothing is excluded
    out, _, winner = _rooted(monkeypatch, "0xold3")
    assert not out["removed"]
    assert out["stats"]["wallet_leaves"]["0.1 ETH"] == [3, 10, 11, 12]
    assert [e[1] for e in out["partial"]] == [winner] * 3
    assert "0.1 ETH" not in out["stats"]["unchecked_pools"]


def test_an_uncertain_deposit_that_cannot_be_placed_skips_the_pool(monkeypatch):
    out, errors, _ = _rooted(monkeypatch, "0xnowhere")
    assert not out["removed"] and not out["excluded"]
    assert "0.1 ETH" in out["stats"]["unchecked_pools"]
    assert "found in no twin pool" in out["stats"]["unchecked_reasons"]["0.1 ETH"]
    assert out["stats"]["incomplete"] is True
    assert any("twin pool is uncertain" in str(e) for e in errors)


def test_a_decoy_run_skips_pools_with_an_uncertain_deposit(monkeypatch):
    out, _, _ = _rooted(monkeypatch, "0xold3", shifted=True)
    assert not out["removed"] and not out["excluded"]
    assert "decoy run" in out["stats"]["unchecked_reasons"]["0.1 ETH"]


def test_the_reason_a_pool_was_not_checked_is_shown_in_cli_and_html(monkeypatch, capsys):
    from tornado_demix import cli
    from tornado_demix.report_html import build_html_report

    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())
    why = "a deposit whose twin pool is uncertain: its leaf was found in no twin pool"
    data["root_check"] = {
        "withdrawals_checked": 0,
        "roots_found": 0,
        "roots_not_found": 0,
        "excluded_candidates": [],
        "incomplete": True,
        "unchecked_pools": ["1 ETH"],
        "unchecked_reasons": {"1 ETH": why},
        "roots_outside_history": 0,
        "roots_ambiguous": 0,
        "roots_undecoded": 0,
        "roots_unverified": 0,
        "tx_info_calls": 0,
        "history_sizes": {},
    }
    html = build_html_report(data, fx.network())
    assert "1 ETH not checked: " + why in html
    from webui import app as webapp

    assert webapp._root_check_view(data, fx.network())["unchecked_reasons"] == {"1 ETH": why}
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "run_demix", lambda *a, **kw: data)
    cli.cmd_demix(cli.build_parser().parse_args(["demix", fx.WALLET, "--root-check"]))
    assert "[1 ETH] not checked: " + why in capsys.readouterr().out
