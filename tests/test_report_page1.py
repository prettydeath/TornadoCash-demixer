"""The first page of every HTML report: header, verdict, key figures, flow diagram,
address table, checks and the list of what follows (report_page1.py)."""

import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix.heuristics import (
    apply_heuristics,
    candidate_kind,
    conclusion,
    ranked_candidates,
)
from tornado_demix.report_html import (
    build_characterize_report,
    build_classify_report,
    build_html_report,
    build_multi_report,
    build_trace_report,
)
from tornado_demix.report_page1 import TOP_N, candidate_status

ROOT = Path(__file__).resolve().parents[1]
DEP = "0x" + "d" * 40
SENDER = "0x" + "c" * 40


def _page1(html):
    start = html.index('<section class="p1"')
    return html[start : html.index("</section>", html.index('class="p1-toc"', start)) + 10]


def _count(html, key):
    m = re.search(r'data-count="%s" data-value="([^"]*)"' % key, html)
    assert m, key
    return m.group(1)


def _lead_data():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    return data


def _context_data():
    """No lead: BOB's withdrawal was sent by a counterparty of the depositor."""
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEP]})
    data["heuristics"]["linked_senders"] = [("1 ETH", fx.BOB, SENDER, "0x" + "e" * 64)]
    sig = data["denoms"]["1 ETH"]["signals"].setdefault(fx.BOB, [])
    if "linked_sender" not in sig:
        sig.append("linked_sender")
    return data


def test_page1_blocks_come_in_order_before_the_sections():
    html = build_html_report(_lead_data(), fx.network(), json_sha256="ab" * 32)
    p1 = _page1(html)
    order = [
        'class="p1-head"',
        'class="verdict"',
        'class="p1-figs"',
        "<svg",
        'class="p1-tab"',
        'class="p1-checks"',
        'class="p1-toc"',
    ]
    pos = [p1.index(x) for x in order]
    assert pos == sorted(pos)
    assert html.index('class="p1"') < html.index('class="toc"')
    # header: full depositor address, network, pools, generated time, version, JSON note
    head = p1[: p1.index('class="verdict"')]
    assert fx.WALLET in head and "Ethereum" in head and "1 ETH" in head
    assert (
        "Generated" in head and "tornado-demix" in head and "JSON SHA-256 on the last page" in head
    )


def test_page1_verdict_word_and_counts_follow_the_bands():
    data = _lead_data()
    cands = ranked_candidates(data)
    html = build_html_report(data, fx.network())
    p1 = _page1(html)
    assert "<b>Lead</b>" in p1
    statuses = [candidate_status(c) for c in cands]
    assert _count(p1, "strong") == str(statuses.count("strong"))
    assert _count(p1, "moderate") == str(statuses.count("lead"))
    assert _count(p1, "context") == str(statuses.count("context"))
    assert _count(p1, "chance") == str(statuses.count("chance"))
    # the figure counts distinct addresses; the bands count (address, pool) candidates
    assert _count(p1, "candidates") == str(len({c["address"] for c in cands}))
    assert _count(p1, "leads") == "1"
    assert _count(p1, "searched") == str(
        sum(r["total_qualifying_withdrawals"] for r in data["denoms"].values())
    )


def test_page1_reason_names_the_real_signal_of_the_top_lead():
    p1 = _page1(build_html_report(_lead_data(), fx.network()))
    answer = re.search(r'<p class="answer">([^<]*)</p>', p1).group(1)
    assert fx.BOB in answer and "not proof" in answer
    assert "direct counterparty of the depositor" in answer and "(D2)" in answer


def test_page1_reason_of_a_context_case_names_the_sender_and_why_it_is_context():
    p1 = _page1(build_html_report(_context_data(), fx.network()))
    assert "<b>No lead</b>" in p1
    answer = re.search(r'<p class="answer">([^<]*)</p>', p1).group(1)
    assert SENDER in answer and "sent by" in answer
    assert DEP in answer  # the shared deposit address, in full
    assert "did not beat chance" in answer and "never raises the band" in answer


def test_page1_flow_and_table_use_full_addresses_and_band_shapes():
    data = _lead_data()
    cands = ranked_candidates(data)
    p1 = _page1(build_html_report(data, fx.network()))
    table = p1[p1.index('class="p1-tab"') :]
    for c in cands:
        assert f'<td class="mono">{c["address"]}</td>' in table
    svg = p1[p1.index("<svg") : p1.index("</svg>")]
    assert 'class="mk lead"' in svg  # filled marker for a lead
    assert "direct counterparty" in svg  # the dashed link of an early direct link
    assert "line width = number of withdrawals" in p1


def test_page1_shows_top_n_and_says_how_many_more():
    data = _lead_data()
    base = ranked_candidates(data)[-1]
    many = [dict(base, address="0x%040x" % (i + 1)) for i in range(TOP_N + 4)]
    from tornado_demix.honest import provenance
    from tornado_demix.report_page1 import demix_page1

    p1 = demix_page1(data, fx.network(), many, provenance(fx.network(), "x"), None, "r")
    assert p1.count('<td class="mono">') == TOP_N
    assert "+4 more addresses below" in p1
    svg = p1[p1.index("<svg") : p1.index("</svg>")]
    assert svg.count('class="mk ') - svg.count("mk ring") == TOP_N


def _alice_in_two_pools():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB}, shared_deposits={fx.ALICE: [DEP]})
    return data


def test_one_address_in_two_pools_is_one_address_with_two_table_rows():
    data = _alice_in_two_pools()
    cands = ranked_candidates(data)
    alice = [c for c in cands if c["address"] == fx.ALICE]
    assert len(alice) == 2  # the fixture's ALICE withdrew from both pools
    p1 = _page1(build_html_report(data, fx.network()))
    assert _count(p1, "candidates") == "2"
    assert "<b>2</b>addresses, 4 withdrawals" in p1
    assert f"({len(cands)} address-pool candidates; band counts are per candidate)" in p1
    table = p1[p1.index('class="p1-tab"') :]
    assert table.count(f'<td class="mono">{fx.ALICE}</td>') == 2
    rows = re.findall(r'<tr><td class="num">(\d+)</td><td class="mono">(0x[0-9a-f]+)</td>', table)
    assert {n for n, a in rows if a == fx.ALICE} == {"2"}  # one number per address
    assert table.count(f'<td class="num">″</td><td class="mono">{fx.ALICE}</td>') == 1  # ditto
    svg = p1[p1.index("<svg") : p1.index("</svg>")]
    assert svg.count(f"<title>{fx.ALICE}</title>") == 1  # one marker per address


def test_figure_three_names_addresses_withdrawals_and_value():
    p1 = _page1(build_html_report(_alice_in_two_pools(), fx.network()))
    figs = p1[p1.index('class="p1-figs"') : p1.index("</div></div>", p1.index('class="p1-figs"'))]
    assert "<b>2.1 ETH</b>deposited, 3 notes" in figs
    assert "addresses, 4 withdrawals, 3.059 ETH" in figs
    assert "addresses, 4 withdrawals, " in figs and "Strongest band" not in figs


def test_network_display_names_on_the_first_page():
    from tests.test_report import _bsc_network, _linked_bsc_data

    p1 = _page1(build_html_report(_linked_bsc_data(), _bsc_network()))
    head = p1[: p1.index('class="verdict"')]
    assert "BSC" in head and "Bsc" not in head


def test_fresh_addresses_are_counted_in_the_checks():
    data = _lead_data()
    data["fresh_addresses"] = {fx.BOB: {"fresh": True, "history_hours": 2}}
    p1 = _page1(build_html_report(data, fx.network()))
    checks = p1[p1.index('class="p1-checks"') :]
    assert "Fresh addresses: 1 of the 2 addresses" in checks and "not scored" in checks


def test_shared_deposit_reason_is_network_neutral_without_nested_parentheses():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [DEP]})
    answer = re.search(
        r'<p class="answer">([^<]*)</p>', _page1(build_html_report(data, fx.network()))
    ).group(1)
    assert "shared exchange deposit address has not been shown to beat chance" in answer
    assert "Ethereum 14/2" not in answer and "Polygon" not in answer
    assert not re.search(r"\([^)]*\(", answer)  # no parenthesis inside a parenthesis


def test_page1_checks_cover_placebo_data_and_method():
    data = _lead_data()
    data["errors"] = [{"kind": "lookup_failed", "what": "txlist", "address": fx.BOB}]
    data["truncated"] = [{"what": "tokentx", "address": fx.BOB, "reason": "row_cap"}]
    p1 = _page1(build_html_report(data, fx.network()))
    checks = p1[p1.index('class="p1-checks"') :]
    assert "Placebo: not run" in checks
    assert "1 lookup failed and 1 list cut off by the explorer" in checks
    assert 'class="mk1 warn"' in checks and _count(checks, "failed") == "1"
    assert "no PROTOCOL.lock" in checks or "PROTOCOL.lock" in checks


def test_page1_has_no_external_resource():
    html = build_html_report(_lead_data(), fx.network())
    p1 = _page1(html)
    assert 'href="http' not in p1 and "src=" not in p1 and "<script" not in p1
    assert "url(" not in p1


def test_every_report_type_has_the_same_first_page():
    from tests.test_report import _char_info, _multi_corr
    from tests.test_report_layout import START, _trace

    info = _char_info()
    pages = {
        "multi": build_multi_report(_multi_corr(), fx.network()),
        "trace": build_trace_report(_trace(), fx.network()),
        "characterize": build_characterize_report(info, fx.network()),
        "classify": build_classify_report(
            {
                "address": fx.BOB,
                "kind": "exchange_deposit",
                "senders": [fx.ALICE],
                "sweep_targets": [START],
                "evidence": True,
                "errors": [],
                "truncated": [],
            },
            fx.network(),
        ),
    }
    for name, html in pages.items():
        p1 = _page1(html)
        for block in (
            'class="p1-head"',
            'class="verdict"',
            'class="p1-figs"',
            'class="p1-checks"',
            'class="p1-toc"',
            "<svg",
        ):
            assert block in p1, (name, block)
        assert p1.count('class="p1-figs"') == 1
        assert len(re.findall(r'<div class="[a-z]*" data-count=', p1)) == 4, name
    assert "<b>Partly traced</b>" in _page1(pages["trace"])
    assert "<b>Exchange deposit</b>" in _page1(pages["classify"])
    assert ">Multi-pool consolidator</b>" in _page1(pages["characterize"])


def test_the_old_verdict_card_is_gone():
    html = build_html_report(_lead_data(), fx.network())
    assert 'class="vcounts"' not in html and "Case overview" not in html
    from tests.test_report import _avax_cluster_traces
    from tornado_demix.report_html import build_cluster_report

    cluster = build_cluster_report(_avax_cluster_traces(), fx.network())
    p1 = _page1(cluster)
    assert ">Reconvergence</b>" in p1 and "<svg" in p1 and 'class="vcounts"' not in cluster
    assert f'<td class="mono">{fx.BOB}</td>' in p1  # the reconvergence point, in full


# --- layout in a real browser ---------------------------------------------------------


def test_reports_have_no_overflow_or_overlapping_text(tmp_path):
    """Run tools/report_layout_check.py (Playwright + Chromium) on one report per type;
    skipped when Playwright or its browser is not installed."""
    pytest.importorskip("playwright.sync_api")
    from tests.test_report import _char_info, _multi_corr
    from tests.test_report_layout import START, _trace

    data = fx.two_pool_result()
    data["deposit_addresses"] = [
        {
            "address": DEP,
            "kind": "forwarder",
            "operator": "0x" + "1" * 40,
            "senders": [SENDER] * 216,
            "evidence": False,
        },
    ]
    apply_heuristics(data, counterparties={fx.BOB})
    reports = {
        "demix": build_html_report(data, fx.network()),
        "context": build_html_report(_context_data(), fx.network()),
        "multi": build_multi_report(_multi_corr(), fx.network()),
        "trace": build_trace_report(_trace(), fx.network()),
        "characterize": build_characterize_report(_char_info(), fx.network()),
        "classify": build_classify_report(
            {
                "address": DEP,
                "kind": "forwarder",
                "operator": "0x" + "1" * 40,
                "senders": [fx.ALICE],
                "sweep_targets": [START],
                "errors": [],
                "truncated": [],
            },
            fx.network(),
        ),
    }
    paths = []
    for name, html in reports.items():
        path = tmp_path / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        paths.append(str(path))
    run = subprocess.run(
        [
            sys.executable or shutil.which("python"),
            str(ROOT / "tools" / "report_layout_check.py"),
            *paths,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    if run.returncode == 77:
        pytest.skip(run.stderr.strip() or "playwright browser not available")
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]


# --- one classification and an open section that holds rows (A/B findings U-1, U-2) ---


def _context_only():
    """Every candidate is weak with a context signal: no lead, nothing chance-level."""
    data = _context_data()
    for res in data["denoms"].values():
        for addr, sig in res.get("signals", {}).items():
            if addr != fx.BOB and "shared_deposit" not in sig:
                sig.append("linked_late")
    return data


def test_a_report_without_leads_opens_the_first_section_with_rows():
    data = _context_data()
    html = build_html_report(data, fx.network())
    opened = re.findall(r'data-sec="([a-z]+)" open>', html)
    kinds = [candidate_kind(c) for c in ranked_candidates(data)]
    assert kinds.count("lead") == 0 and opened == ["context"]
    # the first page lists the candidates although there is no lead
    assert _page1(html).count('<td class="mono">') == len(kinds)
    frag = build_html_report(data, fx.network(), fragment=True, uid="w1")
    assert 'id="w1-context" data-sec="context" open>' in frag


def test_a_report_with_a_lead_still_opens_leads():
    html = build_html_report(_lead_data(), fx.network())
    assert re.findall(r'data-sec="([a-z]+)" open>', html) == ["leads"]


def test_the_cli_summary_uses_the_report_classification():
    data = _context_data()
    kinds = Counter(candidate_kind(c) for c in ranked_candidates(data))
    text = conclusion(data)
    assert f"({kinds['context']} context only, {kinds['chance']} chance-level)" in text
    html = build_html_report(data, fx.network())
    assert _count(_page1(html), "context") == str(kinds["context"])
    assert _count(_page1(html), "chance") == str(kinds["chance"])


def test_context_only_candidates_are_not_called_chance_level_in_the_cli():
    data = _context_only()
    assert {candidate_kind(c) for c in ranked_candidates(data)} == {"context"}
    text = conclusion(data)
    assert "Highest-scoring context-only candidates" in text
    assert "chance-level candidates" not in text


def test_the_web_ui_opens_the_same_section(monkeypatch):
    pytest.importorskip("flask")
    import copy

    from tests.test_webui_layout import CSRF, webapp

    data = _context_data()
    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(webapp, "load_attribution", lambda *a, **kw: {})
    monkeypatch.setattr(webapp, "make_contract_check", lambda url: lambda a: None)
    monkeypatch.setattr(webapp, "run_demix", lambda *a, **kw: copy.deepcopy(data))
    monkeypatch.setattr(webapp, "verify_run", lambda client, d: ([], [], []))
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = CSRF
    body = client.post(
        "/", data={"csrf": CSRF, "analysis": "demix", "network": "ethereum", "wallets": fx.WALLET}
    ).get_data(as_text=True)
    result = body[body.index('<section class="result"') :]
    assert re.findall(r'data-sec="([a-z]+)" open>', result) == ["context"]
    assert "<b>No lead</b>" in result


# --- multi and classify first pages --------------------------------------------------


def _multi():
    from tests.test_report import _multi_corr

    return _multi_corr()


def _section_count(html, key):
    m = re.search(
        r'data-sec="%s"[^>]*><summary><h2>[^<]*</h2><span class="cnt">(\d+)</span>' % key, html
    )
    return m.group(1) if m else "0"


def test_multi_first_page_counts_equal_the_section_counts():
    corr = _multi()
    html = build_multi_report(corr, fx.network())
    p1 = _page1(html)
    assert _count(p1, "context") == _section_count(html, "context")
    assert _count(p1, "chance") == _section_count(html, "chance")
    # the fixture's single-pool profile match is listed under Chance-level, not Context
    chance = html[html.index('data-sec="chance"') :]
    assert "Single-pool profile matches" in chance[: chance.index("</details>")]


def test_multi_cluster_without_a_lead_is_not_called_a_lead():
    corr = _multi()
    corr["operator_clusters"] = [{"wallets": [fx.WALLET, fx.ALICE], "edges": []}]
    corr["cross_profile"] = {}
    p1 = _page1(build_multi_report(corr, fx.network()))
    assert "<b>Operator cluster</b>" in p1 and "<b>Lead</b>" not in p1


def test_multi_overlap_check_links_to_its_callout():
    from tests.test_report import POOL_KEY_1_ETH

    corr = _multi()
    fp = {POOL_KEY_1_ETH: 2}
    corr["fingerprints"] = {fx.WALLET: dict(fp), fx.ALICE: dict(fp), fx.BOB: dict(fp)}
    html = build_multi_report(corr, fx.network())
    checks = _page1(html)[_page1(html).index('class="p1-checks"') :]
    assert 'href="#r-overlap"' in checks
    assert '<div class="callout" id="r-overlap">' in html


def test_classify_first_page_does_not_repeat_the_kind_and_starts_upper_case():
    from tests.test_report_layout import START

    p1 = _page1(
        build_classify_report(
            {
                "address": fx.BOB,
                "kind": "exchange_deposit",
                "senders": [fx.ALICE],
                "sweep_targets": [START],
                "evidence": True,
                "errors": [],
                "truncated": [],
            },
            fx.network(),
        )
    )
    figs = p1[p1.index('class="p1-figs"') : p1.index("<h2>")]
    assert "<b>Exchange deposit</b>" in p1 and "xchange deposit" not in figs
    answer = re.search(r'<p class="answer">([^<]*)</p>', p1).group(1)
    assert answer[0].isupper()
