"""Honest wording of the reports: how to read, conclusion, cross-method, limitations."""

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix import honest
from tornado_demix.heuristics import (
    CONTEXT_SIGNALS,
    LEAD_SOURCE,
    apply_heuristics,
    conclusion,
    cross_method,
)
from tornado_demix.report import build_html_report

pytest.importorskip("flask", reason="web UI is optional (pip install .[web])")

from tests.test_webui import _post, client, webapp  # noqa: E402, F401


def _data():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())
    return data


# --- how to read (B11) -------------------------------------------------------


def test_context_phrases_cover_exactly_the_context_signals():
    assert set(honest.CONTEXT_PHRASE) == set(CONTEXT_SIGNALS)


def test_how_to_read_is_generated_from_the_signal_constants():
    text = " ".join(honest.how_to_read())
    for source in LEAD_SOURCE.values():
        assert source in text
    lead_sentence = next(p for p in honest.how_to_read() if p.startswith("Lead signals"))
    context_sentence = next(p for p in honest.how_to_read() if p.startswith("Context only"))
    for phrase in honest.CONTEXT_PHRASE.values():
        assert phrase in context_sentence
        assert phrase not in lead_sentence


def test_how_to_read_follows_a_changed_constant(monkeypatch):
    monkeypatch.setitem(LEAD_SOURCE, "linked_sender", "sender link")
    assert "sender link" in " ".join(honest.how_to_read())


def test_the_html_report_does_not_list_a_shared_deposit_as_a_lead():
    html = build_html_report(_data(), fx.network())
    start = html.index("How to read this")
    block = html[start : start + 4000]
    lead_part = block.split("Context only")[0]
    assert "shared exchange deposit address" not in lead_part
    assert "shared exchange deposit address" in block


# --- cross-method (B12) ------------------------------------------------------


def test_cross_method_ignores_context_and_note_families():
    data = _data()
    # ALICE: amount+timing + context signals only; BOB: context and a note only.
    res = data["denoms"]["1 ETH"]
    res["signals"][fx.BOB] = ["count_match", "linked_sender", "shared_deposit", "root_partial"]
    res["detail"][fx.BOB]  # present
    res["counts"][fx.BOB] = 2
    res["target_counts"] = [2]
    res["signals"][fx.ALICE] = ["count_match", "linked_sender", "shared_deposit"]
    found = {r["address"]: r for r in cross_method(data)}
    assert fx.ALICE not in found
    assert fx.BOB not in found


def test_cross_method_counts_independent_families_only():
    out = cross_method(_data())
    assert out and out[0]["families"] == ["amount+timing", "gas price"]
    assert all("context" not in r["families"] for r in out)


# --- weak-only conclusion (B13) ---------------------------------------------


def test_weak_only_conclusion_says_no_lead_and_how_ties_are_ordered():
    text = conclusion(_data())
    assert "No lead." in text
    assert "chance-level candidates" in text
    assert "tied at score" in text
    assert "Top candidate" not in text
    assert "independent)" not in text
    assert "Order within a tie" in text


def test_weak_only_conclusion_counts_the_tied_candidates():
    data = _data()
    res = data["denoms"]["1 ETH"]
    res["signals"][fx.BOB] = ["count_match", "gas_price", "self_relayed"]
    res["counts"][fx.BOB] = 2
    res["detail"][fx.BOB] = res["detail"][fx.ALICE]
    res["confidence"][fx.BOB] = res["confidence"][fx.ALICE]
    res["candidates_by_count"] = {2: [fx.ALICE, fx.BOB]}
    text = conclusion(data)
    assert "2 tied at score 0.47" in text
    # deterministic order: the lower address first
    assert text.index(fx.ALICE) < text.index(fx.BOB)


def test_a_lead_keeps_the_top_candidate_wording():
    data = _data()
    data["denoms"]["1 ETH"]["signals"][fx.ALICE].append("linked")
    data.setdefault("vouchers", [])
    text = conclusion(data)
    assert "Top candidate" in text


# --- verified broadcaster (B14) and fee fingerprint frequency ---------------

from tornado_demix import relayer  # noqa: E402
from tornado_demix.report import demix_json  # noqa: E402

TXA1, TXA2 = "0x" + "a1" * 32, "0x" + "a2" * 32


def _data_with_self_relayed():
    data = _data()
    res = data["denoms"]["1 ETH"]
    res["withdrawals"] = [
        {"to": fx.ALICE, "tx_hash": TXA1, "self_relayed": True, "ts": 1100, "fee": 0.0},
        {"to": fx.ALICE, "tx_hash": TXA2, "self_relayed": True, "ts": 1200, "fee": 0.0},
        {"to": fx.BOB, "tx_hash": "0x" + "b1" * 32, "self_relayed": False, "ts": 1300, "fee": 0.1},
        {"to": "0xr", "tx_hash": "0x" + "c1" * 32, "self_relayed": False, "ts": 1400, "fee": 0.1},
    ]
    return data


class _Client:
    """tx_info answers: the recipient broadcast TXA1, somebody else TXA2."""

    def tx_info(self, tx_hash):
        sender = fx.ALICE if tx_hash == TXA1 else "0x" + "9" * 40
        return {
            "from": sender,
            "type": "0x2",
            "maxPriorityFeePerGas": hex(3 * 10**9),
            "maxFeePerGas": hex(30 * 10**9),
            "gas": hex(428_000),
        }


def test_verified_leads_are_stored_for_every_report():
    data = _data_with_self_relayed()
    leads, _context, _groups = relayer.verify_run(_Client(), data)
    by_tx = {x["tx_hash"]: x for x in data["self_relayed_leads"]}
    assert by_tx[TXA1]["broadcaster_status"] == "self"
    assert by_tx[TXA2]["broadcaster_status"] == "other"
    assert leads and data["self_relayed_leads"] == leads


def test_html_and_json_use_the_verified_list():
    data = _data_with_self_relayed()
    relayer.verify_run(_Client(), data)
    html = build_html_report(data, fx.network())
    assert "the recipient" in html
    assert "9" * 40 in html  # the other broadcaster is named
    doc = __import__("json").loads(demix_json(data))
    statuses = {x["broadcaster_status"] for x in doc["result"]["self_relayed_leads"]}
    assert statuses == {"self", "other"}


def test_an_unverified_run_still_says_unverified():
    html = build_html_report(_data_with_self_relayed(), fx.network())
    assert "unverified" in html


def test_fee_fingerprint_frequency_counts_among_self_relayed_withdrawals():
    data = _data_with_self_relayed()
    relayer.verify_run(_Client(), data)
    groups = data["fee_fingerprints"]
    assert groups
    freq = groups[0]["frequency"]
    # every self-relayed withdrawal of the pool had its fee settings read
    assert freq["measured"] is True
    assert freq["total"] == 2 and freq["count"] == 2
    assert relayer.frequency_text(groups[0]) == "2 of 2 self-relayed withdrawals in these pools"


def test_fee_fingerprint_frequency_not_measured_when_fees_were_not_read():
    data = _data_with_self_relayed()
    relayer.verify_run(_Client(), data)
    # a self-relayed withdrawal whose fee settings were never read
    data["denoms"]["1 ETH"]["withdrawals"].append(
        {"to": fx.BOB, "tx_hash": "0x" + "d1" * 32, "self_relayed": True, "ts": 1500, "fee": 0.0}
    )
    relayer.record_fee_profiles(data, data["self_relayed_leads"])
    text = relayer.frequency_text(data["fee_fingerprints"][0])
    assert text.startswith("frequency not measured")


def test_the_fingerprint_heading_no_longer_guesses_one_operator():
    data = _data_with_self_relayed()
    relayer.verify_run(_Client(), data)
    data["fee_fingerprints"][0]["shared"] = True
    html = build_html_report(data, fx.network())
    assert "one tool or operator?" not in html
    assert "hints at one tool or operator" not in html
    assert "How common" in html and "of 2 self-relayed withdrawals" in html


# --- multi and characterize wording (B16, B17) -------------------------------


def test_the_multi_report_calls_shared_candidates_a_window_overlap_not_evidence():
    from tests.test_report import _bsc_network, _multi_corr
    from tornado_demix.report import build_multi_report

    html = build_multi_report(_multi_corr(), _bsc_network())
    assert "Shared count-matched candidates (window overlap, not evidence)" in html
    assert "Strong links" not in html
    assert honest.SHARED_CANDIDATES_CAVEAT in html.replace("&amp;", "&")


def test_the_characterize_text_is_neutral_about_a_consolidator():
    from tornado_demix.characterize import _classify

    label, reason = _classify(n_normal=5, n_inflows=21, n_pools=4)
    assert label == "multi-pool consolidator"
    assert "could be a private exit or a service" in reason
    assert "not one depositor's private" not in reason
    assert "aggregator" not in reason.lower()


def test_the_characterize_report_has_no_aggregator_wording():
    from tests.test_report import _bsc_network, _char_info
    from tornado_demix.report import build_characterize_report

    info = _char_info()
    info["classification"] = "multi-pool consolidator"
    html = build_characterize_report(info, _bsc_network())
    assert "aggregator" not in html.lower()
    assert "multi-pool consolidator" in html


# --- band, score, families and full evidence hashes in CSV and CLI (B18) ----

import csv  # noqa: E402
import os  # noqa: E402

from tornado_demix.report import write_demix_csv, write_multi_csv, write_relayer_csv  # noqa: E402


def _rows(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_candidates_csv_has_band_score_families_and_full_hashes(tmp_path):
    data = _data()
    write_demix_csv(data, str(tmp_path), fx.network())
    row = next(r for r in _rows(tmp_path / "candidates.csv") if r["candidate_address"] == fx.ALICE)
    assert row["band"] == "weak"
    assert row["score"] == "0.47"
    assert row["families"] == "amount+timing;gas price"
    assert row["evidence_tx_hashes"] == "0xa1;0xa2"


def test_withdrawals_csv_has_the_columns_and_empty_band_for_a_non_candidate(tmp_path):
    write_demix_csv(_data(), str(tmp_path), fx.network())
    rows = _rows(tmp_path / "ethereum_withdrawals_1_ETH.csv")
    by = {r["recipient_address"]: r for r in rows}
    assert by[fx.ALICE]["band"] == "weak"
    assert by[fx.BOB]["band"] == "" and by[fx.BOB]["evidence_tx_hashes"] == "0xb1"


def test_self_relayed_leads_csv_has_the_columns(tmp_path):
    data = _data_with_self_relayed()
    leads, *_ = relayer.verify_run(_Client(), data)
    stats = relayer.analyze_relayers(relayer.collect_withdrawals(data))
    write_relayer_csv(stats, leads, str(tmp_path), fx.network(), data)
    rows = _rows(tmp_path / "self_relayed_leads.csv")
    assert rows and {"band", "score", "families", "evidence_tx_hashes"} <= set(rows[0])
    assert rows[0]["band"] == "weak"


def test_multi_csvs_have_the_columns(tmp_path):
    from tests.test_report import _bsc_network, _multi_corr

    write_multi_csv(_multi_corr(), str(tmp_path), _bsc_network())
    for name in ("strong_links", "soft_overlaps", "profile_matches", "cross_consolidators"):
        with open(os.path.join(tmp_path, f"{name}.csv"), encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        assert {"band", "score", "families", "evidence_tx_hashes"} <= set(header), name


def test_cli_demix_prints_band_score_families_and_full_hashes(monkeypatch, capsys):
    from tests.test_cli import WALLET, _stub
    from tornado_demix import cli

    data = _data()
    data["wallet"] = WALLET
    _stub(monkeypatch, load_attribution=lambda *a, **kw: {}, run_demix=lambda *a, **kw: data)
    import argparse

    ns = argparse.Namespace(
        api_csv=None,
        network="ethereum",
        networks_csv="",
        wallet=WALLET,
        out_dir="",
        report="",
        json="",
        cache_dir=".cache",
        window_days=30,
        exit_window=None,
        fee_lo=0.9,
        fee_hi=0.995,
        gap_hours=24,
        mode="transfers",
    )
    monkeypatch.setattr(cli, "_network", lambda a: fx.network())
    cli.cmd_demix(ns)
    out = capsys.readouterr().out
    assert f"weak    0.47  {fx.ALICE} (1 ETH)" in out
    assert "families: amount+timing, gas price" in out
    assert "0xa1" in out and "0xa2" in out


def test_web_demix_csv_has_band_families_and_hashes(monkeypatch):
    pytest = __import__("pytest")
    pytest.importorskip("flask")
    from tests.test_webui import _QuietClient, webapp

    data = _data()
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: data)
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    _blocks, (header, rows), _reports = webapp._run_demix(
        _QuietClient(), [fx.WALLET], fx.network(), window_days=30, mode="transfers"
    )
    assert header[-3:] == ["band", "families", "evidence_tx_hashes"]
    assert rows[0][-3:] == ["weak", "amount+timing;gas price", "0xa1;0xa2"]


# --- limitations and the exploratory banner everywhere (B23) ----------------


def _reports():
    from tests.test_report import _bsc_network, _char_info, _multi_corr
    from tornado_demix.report import (
        build_characterize_report,
        build_classify_report,
        build_cluster_report,
        build_multi_report,
    )

    net = fx.network()
    out = {
        "demix": build_html_report(_data(), net),
        "multi": build_multi_report(_multi_corr(), _bsc_network()),
        "characterize": build_characterize_report(_char_info(), _bsc_network()),
    }
    from tests.test_report import _avax_cluster_traces

    out["cluster"] = build_cluster_report(_avax_cluster_traces(), _bsc_network())
    out["classify"] = build_classify_report(
        {"address": fx.BOB, "kind": "unknown", "errors": [], "truncated": []}, net
    )
    return out


def test_every_html_report_has_the_limitations_from_the_one_list():
    import html as htmllib

    for name, page in _reports().items():
        text = htmllib.unescape(page)
        assert "Limitations" in text, name
        for line in honest.limitations():
            assert line in text, (name, line[:40])


def test_the_limitations_name_the_agreed_points():
    text = " ".join(honest.limitations())
    for needle in (
        "proxy hot wallet",
        "low-traffic",
        "BSC",
        "free Etherscan tier",
        "Across",
        "Arbitrum",
        "named only",
        "rule 2.21",
        "post hoc",
        "exploratory",
        "incomplete",
        "Fee fingerprints",
    ):
        assert needle in text, needle


def test_changing_the_list_changes_every_report(monkeypatch):
    monkeypatch.setattr(honest, "LIMITATIONS", ("A synthetic limitation.",))
    for name, page in _reports().items():
        assert "A synthetic limitation." in page, name


def test_banner_shows_without_a_lock_and_not_with_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "no PROTOCOL.lock" in build_html_report(_data(), fx.network())
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PROTOCOL.lock").write_text("# lock\n", encoding="utf-8")
    assert "no PROTOCOL.lock" not in build_html_report(_data(), fx.network())
    assert honest.exploratory_banner() == ""


def test_cli_prints_the_limitations_and_the_banner(monkeypatch, capsys):
    from tornado_demix import cli

    cli._print_footer()
    out = capsys.readouterr().out
    assert "Limitations:" in out and "no PROTOCOL.lock" in out
    for line in honest.limitations():
        assert line in out


def test_the_web_ui_panel_shows_the_limitations_and_how_to_read(client, monkeypatch):  # noqa: F811
    import html as htmllib

    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: _data())
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    _response, body = _post(client, analysis="demix", mode="transfers")
    text = htmllib.unescape(body)
    assert ">Limitations<" in text and "no PROTOCOL.lock" in text
    for line in honest.limitations():
        assert line in text
    assert honest.how_to_read()[1] in text
    assert "Shared count-matched" not in text  # multi only


def test_the_web_ui_multi_names_the_shared_candidates(client, monkeypatch):  # noqa: F811
    from tests.test_report import _multi_corr

    monkeypatch.setattr(webapp, "correlate", lambda *a, **k: _multi_corr())
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **k: {})
    _response, body = _post(client, analysis="multi", wallets=chr(10).join([fx.WALLET, fx.ALICE]))
    assert honest.SHARED_CANDIDATES_TITLE in body
    assert "Strong links" not in body


# --- provenance and full hashes in the HTML report --------------------------


def test_provenance_collects_version_commit_command_sources_and_time():
    from tornado_demix import __version__

    prov = honest.provenance(fx.network(), command="tornado-demix demix 0x1", label_sets=[])
    assert prov["version"] == __version__
    assert prov["command"] == "tornado-demix demix 0x1"
    assert prov["generated_utc"].endswith("UTC")
    assert prov["data_sources"] and prov["commit"]
    assert prov["protocol_lock"] in (True, False)


def test_data_sources_never_record_a_node_address_or_key():
    class Net:
        api_base = "https://api.example.org/api?apikey=SECRETKEY"
        rpc_url = "https://rpc.example.org/v2/SECRETKEY"

    text = " ".join(honest.data_sources(Net()))
    assert "SECRETKEY" not in text and "api.example.org" in text


def test_label_set_info_hashes_the_file(tmp_path):
    path = tmp_path / "ethereum.csv"
    path.write_text("address,entity\n0x1,X\n", encoding="utf-8")
    info = honest.label_set_info(str(path))
    import hashlib

    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info["file"] == "ethereum.csv"
    assert honest.label_set_info(str(tmp_path / "none.csv"))["sha256"] is None


def test_html_report_has_a_provenance_section():
    data = _data()
    data["provenance"] = {
        "version": "9.9.9",
        "commit": "abc123def456",
        "command": "tornado-demix demix 0xW --window-days 30",
        "generated_utc": "2026-01-02 03:04 UTC",
        "data_sources": ["Etherscan V2 explorer API"],
        "label_sets": [{"name": "attribution labels", "file": "ethereum.csv", "sha256": "f" * 64}],
        "protocol_lock": False,
    }
    html = build_html_report(data, fx.network(), json_sha256="e" * 64)
    assert "Provenance" in html
    for needle in (
        "9.9.9",
        "abc123def456",
        "tornado-demix demix 0xW --window-days 30",
        "Etherscan V2 explorer API",
        "f" * 64,
        "e" * 64,
        "2026-01-02 03:04 UTC",
    ):
        assert needle in html, needle


def test_html_report_without_a_json_export_says_so():
    assert "not written in this run" in build_html_report(_data(), fx.network())


def test_print_css_numbers_the_pages():
    html = build_html_report(_data(), fx.network())
    assert "@page" in html and "counter(page)" in html


def test_html_lists_deposit_and_count_match_withdrawal_hashes_in_full():
    data = _data()
    data["deposits"][0]["hash"] = "0x" + "d1" * 32
    res = data["denoms"]["1 ETH"]
    res["detail"][fx.ALICE][0]["hash"] = "0x" + "a1" * 32
    html = build_html_report(data, fx.network())
    assert "0x" + "d1" * 32 in html
    assert "0x" + "a1" * 32 in html
    assert "d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1" in html


def test_cli_report_carries_the_sha256_of_the_json_export(monkeypatch, tmp_path):
    import argparse
    import hashlib

    from tests.test_cli import WALLET, _stub
    from tornado_demix import cli

    data = _data()
    data["wallet"] = WALLET
    _stub(monkeypatch, load_attribution=lambda *a, **kw: {}, run_demix=lambda *a, **kw: data)
    monkeypatch.setattr(cli, "_network", lambda a: fx.network())
    json_path, report = tmp_path / "r.json", tmp_path / "r.html"
    ns = argparse.Namespace(
        api_csv=None,
        network="ethereum",
        wallet=WALLET,
        out_dir="",
        report=str(report),
        json=str(json_path),
        cache_dir=".cache",
        window_days=30,
        exit_window=None,
        fee_lo=0.9,
        fee_hi=0.995,
        gap_hours=24,
        mode="transfers",
        command_line="tornado-demix demix X --json r.json",
    )
    cli.cmd_demix(ns)
    digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
    html = report.read_text(encoding="utf-8")
    assert digest in html
    assert "tornado-demix demix X --json r.json" in html
    assert fx.ALICE in html and "0xa1" in html
    assert "0xa1" in json_path.read_text(encoding="utf-8")


def test_web_report_carries_the_sha256_of_its_json(monkeypatch):
    import hashlib

    from tests.test_webui import _QuietClient

    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: _data())
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    _blocks, _table, reports = webapp._run_demix(
        _QuietClient(), [fx.WALLET], fx.network(), window_days=30, mode="transfers"
    )
    digest = hashlib.sha256(reports[fx.WALLET + ".json"].encode("utf-8")).hexdigest()
    assert digest in reports[fx.WALLET]
    assert "web UI run, equivalent to: tornado-demix demix" in reports[fx.WALLET]


# --- fix round 1 -------------------------------------------------------------


def test_tied_candidates_are_listed_by_withdrawals_then_address():
    data = _data()
    res = data["denoms"]["1 ETH"]
    third = "0x" + "c" * 40
    for addr in (fx.BOB, third):
        res["signals"][addr] = ["count_match", "gas_price", "self_relayed"]
        res["counts"][addr] = 2
        res["detail"][addr] = res["detail"][fx.ALICE]
        # unrounded scores differ in the third decimal but round to the same 0.47
        res["confidence"][addr] = 0.4749 if addr == fx.BOB else 0.4725
    res["confidence"][fx.ALICE] = 0.4701
    res["candidates_by_count"] = {2: [fx.ALICE, fx.BOB, third]}
    text = conclusion(data)
    assert "3 tied at score 0.47" in text
    assert text.index(fx.ALICE) < text.index(fx.BOB) < text.index(third)


def test_band_note_comes_from_the_constants(monkeypatch):
    monkeypatch.setitem(LEAD_SOURCE, "linked_sender", "sender link")
    assert "sender link" in honest.band_note()
    for phrase in honest.CONTEXT_PHRASE.values():
        assert phrase in honest.band_note()


def test_web_page_band_note_is_rendered_from_honest(client, monkeypatch):  # noqa: F811
    import html as htmllib

    monkeypatch.setattr(webapp, "run_demix", lambda *a, **k: _data())
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    _r, body = _post(client, analysis="demix", mode="transfers")
    assert honest.band_note() in htmllib.unescape(body)
    assert "358 h" not in body


def test_evidence_hashes_say_how_many_candidates_are_left_out():
    from tornado_demix.report_html import evidence_hashes_html

    data = _data()
    cands = [
        dict(c, address=fx.ALICE)
        for c in __import__("tornado_demix.heuristics", fromlist=["x"]).ranked_candidates(data)
    ] * 61
    html = evidence_hashes_html(data, fx.network(), cands)
    assert "1 more candidates" in html and "CSV and JSON" in html


def test_the_provenance_command_never_carries_a_key():
    cmd = honest.scrub_command(
        [
            "tornado-demix",
            "demix",
            "0x1",
            "--api-key",
            "SECRET1",
            "--etherscan-token=SECRET2",
            "--api-csv",
            "config/api.csv",
            "--window-days",
            "30",
        ]
    )
    assert "SECRET1" not in cmd and "SECRET2" not in cmd
    assert "--api-key <hidden>" in cmd and "--etherscan-token=<hidden>" in cmd
    assert "--api-csv config/api.csv" in cmd and "--window-days 30" in cmd
    prov = honest.provenance(command="x --api-key SECRET3 y")
    assert "SECRET3" not in prov["command"]


def test_every_context_signal_has_its_own_verdict():
    from tornado_demix.heuristics import CONTEXT_VERDICT

    assert set(CONTEXT_VERDICT) == set(CONTEXT_SIGNALS)
    assert all(CONTEXT_VERDICT[s].strip() for s in CONTEXT_SIGNALS)


def test_shared_deposit_is_not_said_to_have_failed_against_chance():
    from tornado_demix.heuristics import CONTEXT_VERDICT, band_rationale

    verdict = CONTEXT_VERDICT["shared_deposit"]
    assert (
        "post-hoc" in verdict and "untested" in verdict and "Ethereum 14/2, Polygon 23/0" in verdict
    )
    for text in (
        " ".join(honest.how_to_read()),
        honest.band_note(),
        band_rationale("weak", {"shared_deposit"}),
    ):
        assert verdict in text or "post-hoc" in text
    # the old blanket claim is gone: shared deposit never "did not beat chance"
    assert "did not beat chance" not in band_rationale("weak", {"shared_deposit"})
    assert "did not beat chance" not in verdict


def test_context_sentence_gives_each_signal_its_own_wording():
    text = " ".join(honest.how_to_read())
    from tornado_demix.heuristics import CONTEXT_VERDICT

    for s in CONTEXT_SIGNALS:
        assert CONTEXT_VERDICT[s] in text


def test_limitations_name_the_multi_profile_match_and_the_unlisted_bridges():
    text = " ".join(honest.LIMITATIONS)
    assert "listed bridges are named only" in text and "not listed end as contracts" in text
    assert '">="' in text and "6 h" in text and "exploratory" in text
