"""Wording that must not claim more than the method shows."""

import re
from pathlib import Path

from tornado_demix import honest, placebo
from tornado_demix.heuristics import band_rationale
from tornado_demix.report_page1 import LEGEND_TEXT

ROOT = Path(__file__).resolve().parent.parent
FAMILIES = "two different lead-signal families"
NOT_ESTABLISHED = "independence is not established"


def test_the_strong_band_names_families_and_denies_independence():
    text = band_rationale("strong", {"linked", "early_profile"})
    assert text.startswith(FAMILIES) and NOT_ESTABLISHED in text
    assert "independent lead" not in text


def test_how_to_read_and_band_note_do_not_claim_independent_sources():
    for text in (" ".join(honest.how_to_read()), honest.band_note()):
        assert FAMILIES in text and NOT_ESTABLISHED in text
        assert "two independent" not in text


def test_the_page_one_legend_does_not_claim_independent_sources():
    assert "independent" not in LEGEND_TEXT["strong"]
    assert "families" in LEGEND_TEXT["strong"]


def test_limitations_cover_an_unsolicited_receipt_and_the_floor():
    text = " ".join(honest.limitations())
    assert "unsolicited" in text
    assert "not that its owner controls it or took part" in text
    assert "raises the cost" in text and "does not stop" in text


def test_the_placebo_wording_is_not_a_false_discovery_rate():
    assert "false-discovery rate" in placebo.NOTE
    assert "window before the deposits" in placebo.NOTE
    verdict = placebo._verdict("moderate", 2, 2, 2.0, 4.0)
    assert "target rate not above the decoy rate (no equivalence test)" in verdict
    assert "indistinguishable" not in verdict


def test_the_holdout_report_names_the_ratio_not_a_false_lead_share():
    head = (ROOT / "tools" / "holdout_report.py").read_text(encoding="utf-8")[:1500]
    assert "decoy-to-target rate ratio (not a false-discovery rate)" in head
    assert "false-lead share" not in head


def test_the_methodology_states_what_the_hash_chain_proves():
    text = " ".join((ROOT / "docs" / "METHODOLOGY.md").read_text(encoding="utf-8").split())
    assert "only relative to an external witness taken earlier" in text
    assert "not proof of pre-registration" in text
    assert "public timestamped witness" in text


def test_no_document_calls_the_two_lead_sources_independent():
    pattern = re.compile(
        r"(two|2)\s+independent\s+(lead[- ]?(signals?|sources?)|sources?|families)|"
        r"independent\s+lead\s+(signals?|sources?)",
        re.I,
    )
    files = [ROOT / "README.md", ROOT / "docs" / "EVALUATION.md", ROOT / "docs" / "METHODOLOGY.md"]
    files += [ROOT / "docs" / "llms.txt"]
    files += list((ROOT / "tornado_demix").glob("*.py")) + list((ROOT / "webui").rglob("*.html"))
    # the frozen PREREG_* files keep their registered text; EVALUATION.md Errata (2.23) reads it
    files = [f for f in files if not f.name.startswith("PREREG")]
    bad = [f.name for f in files if pattern.search(f.read_text(encoding="utf-8"))]
    assert not bad, bad


def test_the_unsolicited_receipt_limitation_reaches_the_json_fields():
    fields = honest.json_fields()
    assert any("unsolicited" in line for line in fields["limitations"])


def test_the_errata_reads_the_frozen_independence_wording():
    text = " ".join((ROOT / "docs" / "EVALUATION.md").read_text(encoding="utf-8").split())
    assert "PREREG_HOLDOUT.md and PREREG_HOLDOUT2.md" in text
    assert "independence is not established" in text
