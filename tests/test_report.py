"""HTML report and CSV writers, per-network."""

import csv
import os
from collections import defaultdict

from tests.fixtures import demix_result as fx
from tornado_demix.heuristics import apply_heuristics
from tornado_demix.networks import Network
from tornado_demix.pools import Pool
from tornado_demix.report import (
    build_cluster_report,
    build_html_report,
    build_multi_report,
    write_cluster_csv,
    write_demix_csv,
    write_multi_csv,
)


def _bsc_network():
    return Network(
        "bsc",
        56,
        "BNB",
        [
            Pool(fx.POOL_01_ETH, 0.1, "BNB"),
            Pool(fx.POOL_1_ETH, 1.0, "BNB"),
        ],
    )


def _bsc_data():
    """The two-pool fixture relabelled onto BSC.

    The fixture's ``denoms`` dict is keyed by ETH pool keys ('1 ETH',
    '0.1 ETH'), and each result carries its own ``pool_key``. Simply
    relabelling ``asset`` to 'BNB' leaves those keys pointing at ETH pools,
    which is internally inconsistent (a BNB result under an ETH key) and
    breaks the pool-keyed filename derivation. Rebuild the dict, and every
    row's ``pool_key``, under the BNB pool keys from ``_bsc_network()``.
    """
    data = fx.two_pool_result()
    data["params"]["network"] = "bsc"
    data["params"]["currency"] = "BNB"

    net = _bsc_network()
    key_map = {"1 ETH": net.by_key["1 BNB"].key, "0.1 ETH": net.by_key["0.1 BNB"].key}

    new_denoms = {}
    for old_key, res in data["denoms"].items():
        new_key = key_map[old_key]
        res["asset"] = "BNB"
        res["pool_key"] = new_key
        new_denoms[new_key] = res
    data["denoms"] = new_denoms

    for row in data["deposits"] + data["vouchers"]:
        row["asset"] = "BNB"
        row["pool_key"] = key_map[row["pool_key"]]

    apply_heuristics(data, counterparties=set())
    return data


def test_report_links_to_the_right_explorer():
    html = build_html_report(_bsc_data(), _bsc_network())
    assert "bscscan.com/address/" in html
    assert "etherscan.io" not in html


def test_report_labels_the_asset_not_a_bare_number():
    html = build_html_report(_bsc_data(), _bsc_network())
    assert "BNB" in html
    assert "ETH," not in html


def test_demix_report_notes_a_narrow_exit_window():
    data = _bsc_data()
    data["params"]["exit_window_hours"] = 6
    html = build_html_report(data, _bsc_network())
    assert "exit window" in html
    assert "6 h" in html
    assert "not</b> in scope" in html


def test_demix_report_shows_day_window_when_no_exit_window():
    html = build_html_report(_bsc_data(), _bsc_network())
    assert "window" in html
    assert "days" in html
    assert "exit window" not in html


def test_report_survives_a_wallet_with_no_deposits():
    empty = {
        "wallet": fx.WALLET,
        "deposits": [],
        "vouchers": [],
        "denoms": {},
        "params": {"network": "bsc", "currency": "BNB", "mode": "events"},
    }
    html = build_html_report(empty, _bsc_network())
    assert "No Tornado.Cash deposits found" in html


def test_report_surfaces_unresolved_pools():
    """A pool skipped by a failed block lookup must be shown as not searched,
    not silently absent (Finding 2)."""
    data = _bsc_data()
    data["unresolved"] = [{"pool_key": "1 BNB", "reason": "provider returned None"}]
    html = build_html_report(data, _bsc_network())
    assert "Unresolved pools" in html
    assert "not searched" in html
    assert "provider returned None" in html


def test_report_omits_unresolved_section_when_empty():
    data = _bsc_data()
    data["unresolved"] = []
    assert "Unresolved pools" not in build_html_report(data, _bsc_network())


def _mixed_asset_eth_data():
    """An Ethereum wallet with one 1 ETH voucher and one 100000 DAI voucher.

    The Deposited tile must not sum 1 + 100000 across assets and label it a
    single currency (Finding 2)."""
    return {
        "wallet": fx.WALLET,
        "params": {
            "window_days": 30,
            "mode": "events",
            "network": "ethereum",
            "currency": "ETH",
            "gap_hours": 24,
        },
        "deposits": [
            {
                "pool_key": "1 ETH",
                "denom": 1.0,
                "asset": "ETH",
                "ts": 1000,
                "block": 10,
                "hash": "0xd1",
                "to": "0x" + "1" * 40,
                "via": "pool",
            },
            {
                "pool_key": "100000 DAI",
                "denom": 100000.0,
                "asset": "DAI",
                "ts": 1020,
                "block": 12,
                "hash": "0xd2",
                "to": "0x" + "2" * 40,
                "via": "token",
            },
        ],
        "vouchers": [
            {
                "pool_key": "1 ETH",
                "denom": 1.0,
                "asset": "ETH",
                "count": 1,
                "first_ts": 1000,
                "last_ts": 1000,
                "first_block": 10,
                "deposits": [],
            },
            {
                "pool_key": "100000 DAI",
                "denom": 100000.0,
                "asset": "DAI",
                "count": 1,
                "first_ts": 1020,
                "last_ts": 1020,
                "first_block": 12,
                "deposits": [],
            },
        ],
        "denoms": {},
    }


def _mixed_asset_network():
    return Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool("0x" + "1" * 40, 1.0, "ETH", 18, None),
            Pool("0x" + "2" * 40, 100000, "DAI", 18, "0x" + "3" * 40),
        ],
    )


def test_deposited_tile_renders_per_asset_not_a_cross_asset_sum():
    """1 ETH + 100000 DAI must not render as 'Deposited 100001 ETH'."""
    html = build_html_report(_mixed_asset_eth_data(), _mixed_asset_network())
    # The Deposited figure is rendered per asset, using the same sorted
    # 'asset:value' form the cluster path uses (never a single fused number).
    assert "DAI:100000; ETH:1" in html
    # Both assets appear in the Deposited figure.
    assert "ETH" in html and "DAI" in html
    # It never sums 1 + 100000 across assets into a single meaningless total.
    assert "100001" not in html


def test_demix_csv_carries_asset_and_pool_columns(tmp_path):
    out = str(tmp_path)
    write_demix_csv(_bsc_data(), out, _bsc_network())

    with open(os.path.join(out, "vouchers.csv")) as fh:
        header = next(csv.reader(fh))
    assert "asset" in header
    assert "denomination" in header
    assert "denomination_eth" not in header

    with open(os.path.join(out, "candidates.csv")) as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert all(r["asset"] == "BNB" for r in rows)


def test_withdrawal_csv_filename_uses_the_pool_key(tmp_path):
    out = str(tmp_path)
    files = write_demix_csv(_bsc_data(), out, _bsc_network())
    names = {os.path.basename(f) for f in files}
    assert "bsc_withdrawals_1_BNB.csv" in names
    assert "bsc_withdrawals_0_1_BNB.csv" in names


# write_multi_csv / write_cluster_csv
#
# Neither writer is exercised anywhere else: tests/test_cli.py drives
# cmd_multi/cmd_cluster with out_dir="", so the CSV-writing path itself was
# never invoked, and both functions carried a real KeyError (write_multi_csv)
# or read stale keys (write_cluster_csv) until this task. Both fixtures are
# built by hand in the exact shape multi.correlate()/multi._profile_match()
# and cluster.trace_wallet() actually return -- see those modules for the
# structures being mirrored here.
POOL_KEY_1_ETH = "1 ETH"


def _multi_corr():
    """A minimal two-wallet ``corr`` dict, shaped like multi.correlate()'s
    return value, keyed by pool keys throughout (never a bare float).
    """
    wallet_a, wallet_b = fx.WALLET, fx.ALICE
    consolidator = fx.BOB

    def _data(wallet):
        return {
            "wallet": wallet,
            "vouchers": [
                {
                    "pool_key": POOL_KEY_1_ETH,
                    "denom": 1.0,
                    "asset": "ETH",
                    "count": 2,
                    "first_ts": 1000,
                    "last_ts": 1010,
                }
            ],
            "denoms": {
                POOL_KEY_1_ETH: {
                    "total_qualifying_withdrawals": 3,
                    "unique_recipients": 2,
                    "target_counts": [2],
                    "candidates_by_count": {2: [consolidator]},
                }
            },
        }

    results = {wallet_a: _data(wallet_a), wallet_b: _data(wallet_b)}
    # (pool_key, addr) -> {wallet: hits}, mirroring multi.correlate()'s addr_detail
    addr_detail = {(POOL_KEY_1_ETH, consolidator): {wallet_a: 2, wallet_b: 2}}
    strong = [(POOL_KEY_1_ETH, consolidator, sorted([wallet_a, wallet_b]))]
    soft = []
    fingerprints = {wallet_a: {POOL_KEY_1_ETH: 2}, wallet_b: {POOL_KEY_1_ETH: 2}}
    # profile_matches[wallet] = [(addr, {pool_key: (received, need)}, exact)],
    # per multi._profile_match().
    profile_matches = {
        wallet_a: [(consolidator, {POOL_KEY_1_ETH: (2, 2)}, True)],
        wallet_b: [],
    }
    cross_profile = {}
    return {
        "results": results,
        "strong": strong,
        "soft": soft,
        "addr_detail": addr_detail,
        "fingerprints": fingerprints,
        "profile_matches": profile_matches,
        "cross_profile": cross_profile,
    }


def test_write_multi_csv_writes_every_file_and_uses_pool_keys(tmp_path):
    out = str(tmp_path)
    files = write_multi_csv(_multi_corr(), out, _bsc_network())
    names = {os.path.basename(f) for f in files}
    assert names == {
        "wallets_overview.csv",
        "strong_links.csv",
        "soft_overlaps.csv",
        "profile_matches.csv",
        "cross_consolidators.csv",
    }

    with open(os.path.join(out, "profile_matches.csv")) as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    for row in rows:
        assert "ETHETH" not in row["fingerprint"]
        assert "ETH ETH" not in row["fingerprint"]
        assert "ETHETH" not in row["received_vs_needed"]
        assert "ETH ETH" not in row["received_vs_needed"]
    exact = [r for r in rows if r["match_type"] == "exact"]
    assert exact and exact[0]["received_vs_needed"] == "1 ETH:2/2"

    with open(os.path.join(out, "strong_links.csv")) as fh:
        strong_rows = list(csv.DictReader(fh))
    assert strong_rows
    # The pool column must hold the pool key ('1 ETH'), not a bare float.
    assert strong_rows[0]["pool"] == "1 ETH"


def _avax_cluster_traces():
    """A single reconvergence cluster fed by two AVAX pools of different
    denominations, shaped like cluster.trace_wallet()'s return value. Mixed
    denominations on one asset are the case that catches a header/row
    misalignment or a leftover 'ETH' suffix glued onto a non-ETH pool key.
    """
    wallet = fx.WALLET
    z_addr = fx.BOB
    by_pool = {
        "10 AVAX": [(fx.ALICE, 9.5, 1100, "0xh1", "AVAX")],
        "100 AVAX": [(fx.ALICE, 95.0, 1200, "0xh2", "AVAX")],
    }
    totals = defaultdict(float)
    for lst in by_pool.values():
        for _src, value, _ts, _hash, asset in lst:
            totals[asset] += value
    cluster = {
        "z": z_addr,
        "pools_covered": sorted(by_pool.keys()),
        "n_layers": len(by_pool),
        "totals": {a: round(v, 4) for a, v in totals.items()},
        "by_pool": by_pool,
    }
    trace = {
        "wallet": wallet,
        "fingerprint": {"10 AVAX": 1, "100 AVAX": 1},
        "layers": {},
        "clusters": [cluster],
    }
    return [trace]


def test_write_cluster_csv_covers_mixed_pools_with_aligned_rows(tmp_path):
    out = str(tmp_path)
    files = write_cluster_csv(_avax_cluster_traces(), out, _bsc_network())
    names = {os.path.basename(f) for f in files}
    assert names == {"clusters.csv", "cluster_detail.csv"}

    for fname in ("clusters.csv", "cluster_detail.csv"):
        with open(os.path.join(out, fname), newline="") as fh:
            rows = list(csv.reader(fh))
        header, data_rows = rows[0], rows[1:]
        assert data_rows, f"{fname} produced no data rows"
        for row in data_rows:
            assert len(row) == len(header), (
                f"{fname}: row {row!r} has {len(row)} fields, header {header!r} has {len(header)}"
            )

    with open(os.path.join(out, "clusters.csv")) as fh:
        cluster_rows = list(csv.DictReader(fh))
    assert cluster_rows[0]["pools_covered"] == "10 AVAX,100 AVAX"

    with open(os.path.join(out, "cluster_detail.csv")) as fh:
        detail_rows = list(csv.DictReader(fh))
    detail_pools = {r["pool"] for r in detail_rows}
    assert detail_pools == {"10 AVAX", "100 AVAX"}


def test_every_csv_names_its_network(tmp_path):
    from tornado_demix.report import write_demix_csv

    out = str(tmp_path)
    files = write_demix_csv(_bsc_data(), out, _bsc_network())
    for path in files:
        with open(path) as fh:
            reader = csv.reader(fh)
            header = next(reader)
            rows = list(reader)
        assert header[0] == "network", path
        for row in rows:
            assert row[0] == "bsc", path


def test_per_pool_filenames_are_network_scoped(tmp_path):
    """withdrawals_1_ETH.csv is the same name on four chains."""
    from tornado_demix.report import write_demix_csv

    out = str(tmp_path)
    files = write_demix_csv(_bsc_data(), out, _bsc_network())
    names = {os.path.basename(f) for f in files}
    assert "bsc_withdrawals_1_BNB.csv" in names


# Mixed-asset reconvergence (Task 6): a cluster fed by a 1 ETH layer and a
# 1000 DAI layer at once must carry a total per asset, never a cross-asset sum.
def _mixed_asset_traces():
    return [
        {
            "wallet": fx.WALLET,
            "fingerprint": {"1 ETH": 2, "1000 DAI": 1},
            "layers": {},
            "clusters": [
                {
                    "z": "0x" + "d" * 40,
                    "pools_covered": ["1 ETH", "1000 DAI"],
                    "n_layers": 2,
                    "totals": {"ETH": 1.8, "DAI": 990.0},
                    "by_pool": {
                        "1 ETH": [("0x" + "1" * 40, 1.8, 1000, "0xh1", "ETH")],
                        "1000 DAI": [("0x" + "2" * 40, 990.0, 1001, "0xh2", "DAI")],
                    },
                }
            ],
        }
    ]


def test_cluster_csv_reports_a_total_per_asset(tmp_path):
    out = str(tmp_path)
    write_cluster_csv(_mixed_asset_traces(), out, _bsc_network())
    with open(os.path.join(out, "clusters.csv")) as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["pools_covered"] == "1 ETH,1000 DAI"
    assert "ETH:1.8" in rows[0]["value_funneled"]
    assert "DAI:990" in rows[0]["value_funneled"]


def test_cluster_csv_never_sums_across_assets(tmp_path):
    """1.8 ETH + 990 DAI is not 991.8 of anything."""
    out = str(tmp_path)
    write_cluster_csv(_mixed_asset_traces(), out, _bsc_network())
    with open(os.path.join(out, "clusters.csv")) as fh:
        body = fh.read()
    assert "991.8" not in body


def test_cluster_rows_stay_aligned(tmp_path):
    out = str(tmp_path)
    files = write_cluster_csv(_mixed_asset_traces(), out, _bsc_network())
    for path in files:
        with open(path) as fh:
            rows = list(csv.reader(fh))
        header = rows[0]
        for row in rows[1:]:
            assert len(row) == len(header), path


def test_cluster_detail_csv_carries_the_asset_column(tmp_path):
    """Every forward row must say what was actually moved, not just its pool."""
    out = str(tmp_path)
    write_cluster_csv(_mixed_asset_traces(), out, _bsc_network())
    with open(os.path.join(out, "cluster_detail.csv")) as fh:
        rows = list(csv.DictReader(fh))
    by_pool = {r["pool"]: r["asset"] for r in rows}
    assert by_pool == {"1 ETH": "ETH", "1000 DAI": "DAI"}


# Confidence bands render in every report, with consistent wording, and the
# honest "no corroborated candidates" line shows when nothing is corroborated.
def _linked_bsc_data():
    """The two-pool fixture, with one recipient linked so at least one
    candidate carries a corroborating family (moderate or strong)."""
    data = _bsc_data()
    linked = next(iter(next(iter(data["denoms"].values()))["detail"]))
    apply_heuristics(data, counterparties={linked})
    return data


def test_demix_report_leads_with_most_likely_and_band_badges():
    html = build_html_report(_linked_bsc_data(), _bsc_network())
    assert "Most likely candidates" in html
    # The section leads before Deposits.
    assert html.index("Most likely candidates") < html.index(">Deposits<")
    # A band badge appears, and the ranked table now has a Band column.
    assert 'class="badge' in html
    assert "<th>Band</th>" in html
    # Honest caption survives.
    assert "probabilistic leads for" in html and "corroboration" in html


def test_demix_report_shows_an_attribution_column_when_a_set_names_a_candidate():
    data = _linked_bsc_data()
    linked = next(iter(next(iter(data["denoms"].values()))["detail"]))
    attribution = {linked: {"label": "Binance: Hot Wallet", "category": "exchange"}}
    html = build_html_report(data, _bsc_network(), attribution=attribution)
    assert "<th>Attribution</th>" in html
    assert "Binance: Hot Wallet (exchange)" in html


def test_demix_report_has_no_attribution_column_without_a_set():
    html = build_html_report(_linked_bsc_data(), _bsc_network())
    assert "<th>Attribution</th>" not in html


def test_demix_report_has_no_attribution_column_when_no_candidate_is_labelled():
    data = _linked_bsc_data()
    # A set that names a different address must not add an empty column.
    attribution = {"0x%040x" % 99: {"label": "Elsewhere", "category": "cex"}}
    html = build_html_report(data, _bsc_network(), attribution=attribution)
    assert "<th>Attribution</th>" not in html


def _weak_only_data():
    """One recipient count-matched (count 3 of 40, discriminating) with no other
    signal: it ranks, but as a weak band -> the honest line must show."""
    from collections import Counter, defaultdict

    detail = defaultdict(list)
    for i in range(40):
        addr = "0x%040x" % (i + 1)
        reps = 3 if i == 0 else 1
        detail[addr] = [
            {
                "value": 0.099,
                "ts": 1000 + i,
                "hash": "0x%x" % (i * 10 + j),
                "relayer": "0xrelayer",
                "fee": 0.001,
                "asset": "ETH",
                "self_relayed": False,
                "gas_price": 5,
            }
            for j in range(reps)
        ]
    counts = Counter({a: (3 if a == "0x%040x" % 1 else 1) for a in detail})
    res = {
        "pool_key": "0.1 ETH",
        "denom": 0.1,
        "asset": "ETH",
        "decimals": 18,
        "pool": "0x" + "a" * 40,
        "mode": "events",
        "counts": counts,
        "detail": detail,
        "withdrawals": [],
        "target_counts": [3],
        "candidates_by_count": {3: ["0x%040x" % 1]},
        "total_qualifying_withdrawals": 42,
        "unique_recipients": 40,
    }
    data = {
        "wallet": "0xw",
        "params": {"mode": "events", "network": "ethereum"},
        "deposits": [{"gas_price": 999, "pool_key": "0.1 ETH"}],
        "vouchers": [
            {
                "pool_key": "0.1 ETH",
                "denom": 0.1,
                "asset": "ETH",
                "count": 3,
                "first_ts": 1000,
                "last_ts": 1100,
            }
        ],
        "denoms": {"0.1 ETH": res},
    }
    apply_heuristics(data, counterparties=set())
    return data


def test_demix_report_shows_honest_line_when_only_weak():
    html = build_html_report(_weak_only_data(), _bsc_network())
    assert "No corroborated candidates" in html
    # ...but the weak candidate still appears in the ranked table with a badge.
    assert 'class="badge weak">weak</span>' in html


def test_multi_report_has_consolidator_section_and_honest_line():
    # _multi_corr has no cross_profile, so the honest line must show.
    html = build_multi_report(_multi_corr(), _bsc_network())
    assert "Most likely: cross-wallet consolidators" in html
    assert "No cross-wallet consolidator" in html
    assert "probabilistic leads" in html


def test_multi_report_grades_an_identical_fingerprint_consolidator_weak():
    # Both wallets share one identical fingerprint and the consolidator carries
    # no independent signal: this is the window-overlap artefact, not a strong
    # multi-wallet lead. It must render weak, not strong.
    corr = _multi_corr()
    corr["cross_profile"] = {fx.BOB: sorted([fx.WALLET, fx.ALICE])}
    corr["consolidator_grades"] = {
        fx.BOB: {
            "wallets": sorted([fx.WALLET, fx.ALICE]),
            "band": "weak",
            "independent": [],
            "self_relayed": False,
            "distinct_fingerprints": 1,
            "artefact": True,
            "edge_reason": None,
        }
    }
    html = build_multi_report(corr, _bsc_network())
    assert 'class="badge weak">weak</span>' in html
    assert 'class="badge strong">strong</span>' not in html
    assert "window-overlap artefact" in html
    assert "full deposit fingerprint of 2 wallets" in html


def test_multi_report_flags_a_corroborated_consolidator_as_strong():
    # A consolidator carrying an independent linked signal beyond the fingerprint
    # overlap is genuinely strong and renders the strong badge.
    corr = _multi_corr()
    corr["cross_profile"] = {fx.BOB: sorted([fx.WALLET, fx.ALICE])}
    corr["consolidator_grades"] = {
        fx.BOB: {
            "wallets": sorted([fx.WALLET, fx.ALICE]),
            "band": "strong",
            "independent": ["linked"],
            "self_relayed": False,
            "distinct_fingerprints": 1,
            "artefact": False,
            "edge_reason": "shared full-fingerprint consolidator (+linked)",
        }
    }
    html = build_multi_report(corr, _bsc_network())
    assert 'class="badge strong">strong</span>' in html
    assert "independent signal" in html


def test_multi_report_warns_when_wallets_share_one_fingerprint():
    # Three wallets with an identical fingerprint: the profile-match and
    # consolidator counts are inflated by the shared window, and the report must
    # say so up front.
    corr = _multi_corr()
    fp = {POOL_KEY_1_ETH: 2}
    corr["fingerprints"] = {fx.WALLET: dict(fp), fx.ALICE: dict(fp), fx.BOB: dict(fp)}
    html = build_multi_report(corr, _bsc_network())
    assert "Window-overlap warning" in html
    assert "same fingerprint" in html


def test_multi_report_warns_on_a_synchronised_batch_with_distinct_fps():
    # Distinct fingerprints (so the identical-fingerprint banner does NOT fire),
    # but the wallets deposited as one synchronised batch: the report must still
    # warn, and surface the Deposit synchronicity section.
    corr = _multi_corr()
    corr["fingerprints"] = {
        fx.WALLET: {POOL_KEY_1_ETH: 2},
        fx.ALICE: {POOL_KEY_1_ETH: 3, "10 ETH": 1},
    }
    corr["sync_groups"] = [
        {
            "wallets": sorted([fx.WALLET, fx.ALICE, fx.BOB]),
            "first_ts": 1_700_000_000,
            "last_ts": 1_700_000_600,
            "span_seconds": 600,
        }
    ]
    html = build_multi_report(corr, _bsc_network())
    assert "Window-overlap warning" in html
    assert "synchronised batch" in html
    assert "Deposit synchronicity" in html
    assert "10 minutes" in html


def test_multi_report_synchronicity_section_says_none_when_empty():
    corr = _multi_corr()  # no sync_groups
    html = build_multi_report(corr, _bsc_network())
    assert "Deposit synchronicity" in html
    assert "No two wallets deposited within one synchronised batch" in html


def test_cluster_report_leads_with_reconvergence_without_an_evidence_band():
    """A one-hop reconvergence point is a lead, not a 'strong' candidate."""
    html = build_cluster_report(_avax_cluster_traces(), _bsc_network())
    assert "Most likely reconvergence points" in html
    assert 'class="badge' not in html
    assert "One hop is not an evidence band" in html
    assert "1-hop" in html
    # per-asset total, never a cross-asset sum
    assert "AVAX:104.5" in html


def _char_info():
    return {
        "address": fx.BOB,
        "normal_txs": 117,
        "internal_txs": 21,
        "first_activity_ts": 1_700_000_000,
        "last_activity_ts": 1_700_090_000,
        "pool_inflows": [
            {
                "pool_key": "0.1 ETH",
                "asset": "ETH",
                "ts": 1_700_000_100,
                "value": 0.098,
                "tx_hash": "0xa",
                "pool_address": fx.ALICE,
            }
        ],
        "inflow_totals": {"ETH": 243.2},
        "distinct_pools": ["0.1 ETH", "1 ETH", "10 ETH", "100 ETH"],
        "top_next_hops": [{"address": fx.WALLET, "count": 8, "total_value": 0.8}],
        "classification": "aggregator",
        "classification_reason": "collects 21 pool withdrawals across 4 distinct pools",
    }


def test_characterize_report_shows_class_inflows_and_next_hop():
    from tornado_demix.report import build_characterize_report

    html = build_characterize_report(_char_info(), _bsc_network())
    assert "exit-candidate characterisation" in html
    assert "aggregator" in html
    assert "0.1 ETH" in html
    assert "243.2 ETH" in html
    assert "Dominant next hops" in html
    assert "lead, not proof" in html
    # next-hop address is linked to the right explorer
    assert "bscscan.com/address/" in html


def test_characterize_report_shows_attribution_labels():
    from tornado_demix.report import build_characterize_report

    info = _char_info()
    info["label"] = {
        "entity": "blocked",
        "label": "Tornado.Cash: Router",
        "category": "mixer",
        "confidence": "medium",
    }
    info["top_next_hops"] = [
        {
            "address": fx.WALLET,
            "count": 8,
            "total_value": 0.8,
            "label": {"entity": "Binance", "label": "Binance: Hot", "category": "exchange"},
        }
    ]
    html = build_characterize_report(info, _bsc_network())
    assert "Attribution: Tornado.Cash: Router (mixer)" in html
    assert "Binance: Hot (exchange)" in html


def test_characterize_report_without_labels_shows_no_attribution():
    from tornado_demix.report import build_characterize_report

    info = _char_info()
    info["label"] = None
    info["top_next_hops"] = [{"address": fx.WALLET, "count": 8, "total_value": 0.8, "label": None}]
    html = build_characterize_report(info, _bsc_network())
    assert "Attribution:" not in html


def test_characterize_report_handles_no_inflows():
    from tornado_demix.report import build_characterize_report

    info = _char_info()
    info["pool_inflows"] = []
    info["distinct_pools"] = []
    info["inflow_totals"] = {}
    info["classification"] = "no pool inflows"
    html = build_characterize_report(info, _bsc_network())
    assert "not a Tornado exit on the pools analysed" in html


def test_cluster_report_shows_honest_line_when_empty():
    empty = [{"wallet": fx.WALLET, "fingerprint": {"1 ETH": 1}, "layers": {}, "clusters": []}]
    html = build_cluster_report(empty, _bsc_network())
    assert "No reconvergence point" in html


def test_demix_report_states_its_parameters_and_block_ranges():
    data = _linked_bsc_data()
    data["params"]["gap_hours"] = 6
    res = next(iter(data["denoms"].values()))
    res["windows"] = [
        {"first_ts": 1700000000, "end_ts": 1702592000, "start_block": 111, "end_block": 222}
    ]
    html = build_html_report(data, _bsc_network())
    assert "Analysis parameters" in html
    assert "6 h between deposits into one pool" in html
    assert "disc &gt;= 0.50" in html
    assert "111..222" in html
    assert "tornado-demix " in html


def test_demix_report_leads_with_the_band_not_a_percentage():
    html = build_html_report(_linked_bsc_data(), _bsc_network())
    assert "Strongest band" in html
    assert "Top confidence" not in html
    assert "% confidence" not in html


def test_demix_report_lists_the_evidence_of_each_likely_candidate():
    html = build_html_report(_linked_bsc_data(), _bsc_network())
    assert 'class="evidence"' in html
    assert "no direct transaction with the depositor" in html or "transacts directly" in html


def test_demix_json_round_trips_with_candidates_and_parameters(tmp_path):
    import json

    from tornado_demix.report import write_demix_json

    path = write_demix_json(_linked_bsc_data(), str(tmp_path / "r.json"))
    doc = json.loads(open(path, encoding="utf-8").read())
    assert doc["tool"].startswith("tornado-demix ")
    assert doc["candidates"] and "evidence" in doc["candidates"][0]
    assert any(label == "Voucher gap" for label, _v in doc["assumptions"]["params"])
    assert doc["result"]["denoms"]


def test_report_links_escape_the_url_as_well_as_the_text():
    data = _linked_bsc_data()
    res = next(iter(data["denoms"].values()))
    evil = '0x1" onmouseover="alert(1)'
    first = next(iter(res["detail"]))
    for key in ("counts", "detail", "signals", "confidence", "discrimination"):
        res[key][evil] = res[key].pop(first)
    html = build_html_report(data, _bsc_network())
    assert 'onmouseover="alert(1)"' not in html
    assert "&quot; onmouseover=&quot;" in html


def test_characterize_report_marks_a_zero_value_hop_as_a_contract_call():
    from tornado_demix.report import build_characterize_report

    info = _char_info()
    info["top_next_hops"] = [
        {"address": fx.ALICE, "count": 2, "total_value": 0.0, "kind": "call", "label": None},
        {"address": fx.BOB, "count": 1, "total_value": 2.0, "kind": "transfer", "label": None},
    ]
    html = build_characterize_report(info, _bsc_network())
    assert "contract call, no BNB" in html
    assert "2.0 BNB" in html


def test_totals_never_use_scientific_notation():
    from tornado_demix.report import format_totals

    assert format_totals({"cDAI": 4_500_000.0, "ETH": 1.8}) == "ETH:1.8; cDAI:4500000"
