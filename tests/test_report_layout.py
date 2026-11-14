"""The shared report layout: verdict card, sections in a fixed order, the candidate
table with its evidence timelines, offline-only resources and the trace totals."""

import re

from tests.fixtures import demix_result as fx
from tornado_demix.heuristics import apply_heuristics, ranked_candidates
from tornado_demix.report_html import (
    REPORT_CSS,
    REPORT_JS,
    SECTION_ORDER,
    build_characterize_report,
    build_classify_report,
    build_html_report,
    build_multi_report,
    build_trace_report,
    candidate_kind,
    end_state,
    trace_totals,
)

START = "0x5555000000000000000000000000000000000005"
HOP1 = "0x6666000000000000000000000000000000000006"
HOP2 = "0x7777000000000000000000000000000000000007"
DUST = "0x8888000000000000000000000000000000000008"
TX1 = "0x" + "1" * 64
TX2 = "0x" + "2" * 64
TX3 = "0x" + "3" * 64


def _demix():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    return data


def _count(html, key):
    m = re.search(r'data-count="%s" data-value="([^"]*)"' % key, html)
    assert m, key
    return m.group(1)


def _section_keys(html):
    return re.findall(r'<details class="sec" id="[^"]+" data-sec="([a-z]+)"', html)


def _trace():
    return {
        "start": START,
        "amount": 10.0,
        "asset": "ETH",
        "nodes_expanded": 3,
        "arrival_matched": "exact",
        "arrival_tx": TX1,
        "edges": [
            {
                "hop": 1,
                "from": START,
                "to": HOP1,
                "tx_hash": TX2,
                "ts": 1_700_000_000,
                "asset": "ETH",
                "value": 9.95,
                "attributed": 9.95,
                "kind": "transfer",
                "lower": 9.95,
                "upper": 9.95,
                "history_incomplete": False,
                "bounds_available": True,
                "sources": [{"tx_hash": TX1, "from": START, "amount": 9.95}],
            },
            {
                "hop": 2,
                "from": HOP1,
                "to": HOP2,
                "tx_hash": TX3,
                "ts": 1_700_003_600,
                "asset": "ETH",
                "value": 6.0,
                "attributed": 6.0,
                "kind": "bridge",
                "lower": 0.0,
                "upper": 6.0,
                "history_incomplete": True,
                "bounds_available": True,
                "bridge": {"protocol": "across", "dest_chain_id": None, "state": "not_followed"},
            },
        ],
        "terminals": [
            {
                "address": HOP2,
                "asset": "ETH",
                "amount": 6.0,
                "hop": 2,
                "reason": "bridge across (not followed)",
                "label": None,
            },
            {
                "address": DUST,
                "asset": "ETH",
                "amount": 0.05,
                "hop": 1,
                "reason": "below_threshold",
                "label": None,
            },
            {
                "address": HOP1,
                "asset": "ETH",
                "amount": 3.95,
                "hop": 1,
                "reason": "lookup_failed",
                "label": None,
            },
        ],
        "errors": [{"kind": "lookup_failed", "what": "history", "address": HOP1, "error": "x"}],
        "error_count": 1,
        "truncated": [],
    }


# --- demix ---------------------------------------------------------------------------


def test_verdict_card_comes_first_with_the_counts_of_the_fixture():
    html = build_html_report(_demix(), fx.network())
    verdict = html.index('class="verdict"')
    assert verdict < html.index('class="toc"') < html.index('data-sec="leads"')
    # BOB is a moderate lead (early direct link), ALICE a chance-level count match.
    assert _count(html, "leads") == "1"
    assert _count(html, "context") == "0"
    assert _count(html, "chance") == "1"
    assert _count(html, "failed") == "0"
    assert _count(html, "band") == "moderate"
    answer = re.search(r'<p class="answer">([^<]*)</p>', html).group(1)
    assert fx.BOB in answer and "not proof" in answer
    assert "no PROTOCOL.lock" in html[verdict : html.index('class="toc"')]


def test_verdict_counts_follow_the_candidate_kinds():
    data = _demix()
    kinds = [candidate_kind(c) for c in ranked_candidates(data)]
    html = build_html_report(data, fx.network())
    assert _count(html, "leads") == str(kinds.count("lead"))
    assert _count(html, "chance") == str(kinds.count("chance"))


def test_failed_lookups_reach_the_verdict():
    data = _demix()
    data["errors"] = [{"kind": "lookup_failed", "what": "history", "error": "boom"}]
    data["truncated"] = [{"reason": "rpc_fallback_no_gas_price", "what": "logs"}]
    html = build_html_report(data, fx.network())
    assert _count(html, "failed") == "1"  # the RPC notice is not a failure
    assert "results may be incomplete" in html


def test_sections_are_in_the_fixed_order_and_only_leads_is_open():
    html = build_html_report(_demix(), fx.network())
    keys = _section_keys(html)
    assert keys == [k for k in SECTION_ORDER if k in keys]
    assert keys[:3] == ["leads", "context", "chance"] and keys[-3:] == [
        "method",
        "provenance",
        "raw",
    ]
    opened = re.findall(r'data-sec="([a-z]+)" open>', html)
    assert opened == ["leads"]
    # the table of contents lists every section with its count
    toc = html[html.index('class="toc"') : html.index("</nav>")]
    assert toc.count("<a ") == len(keys)
    assert '<a href="#r-leads">Leads<span class="n">1</span></a>' in toc


def test_every_candidate_row_is_in_the_static_html_with_its_full_address():
    data = _demix()
    html = build_html_report(data, fx.network())
    cands = ranked_candidates(data)
    assert html.count('<tbody class="cand"') == len(cands)
    for c in cands:
        assert f">{c['address']}</a>" in html
        assert f'data-band="{c["band"]}"' in html
    # the filter bars are hidden until the script fills them; rows never depend on JS
    assert '<div class="fbar noprint" data-scope="r-leads" hidden>' in html
    # the "#" column ranks over the whole ranking, whichever section a row is in
    for rank, c in enumerate(cands, 1):
        row = html[html.index(f">{c['address']}</a>") - 600 : html.index(f">{c['address']}</a>")]
        assert f'<td class="num">{rank}</td>' in row


def _cand_body(html, address):
    body = html[html.rindex('<tbody class="cand"', 0, html.index(f">{address}</a>")) :]
    return body[: body.index("</tbody>")]


def test_each_candidate_opens_an_evidence_timeline_with_full_hashes():
    html = build_html_report(_demix(), fx.network())
    alice = _cand_body(html, fx.ALICE)
    assert "Evidence timeline" in alice
    for tx in ("0xa1", "0xa2"):  # its own withdrawals, oldest first
        assert f">{tx}</a>" in alice
    assert alice.index(">0xa1</a>") < alice.index(">0xa2</a>")
    # deposits are not repeated per candidate: the count match refers to them by number
    assert ">0xd1</a>" not in alice and ">0xd2</a>" not in alice
    assert "D1–D2" in alice
    assert "feeds: amount+timing (count match)" in alice
    assert "gas price equals deposit D1" in alice
    assert "What was checked" in alice and "context, never raises the band" in alice
    assert "Band: weak" in alice


def test_deposits_are_listed_once_near_the_top_with_numbers():
    html = build_html_report(_demix(), fx.network())
    top = html[: html.index('<details class="sec"')]
    assert 'class="deps"' in top and "Tornado.Cash deposits" in top
    for n, tx in ((1, "0xd1"), (2, "0xd2"), (3, "0xd3")):
        assert f'<td class="num">D{n}</td>' in top and f">{tx}</a>" in top
        assert html.count(f">{tx}</a>") == 1  # listed once in the whole report


def test_feeds_tags_only_name_signals_that_fired_for_the_candidate():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB}, withdrawal_senders={"0xb1": fx.BOB})
    html = build_html_report(data, fx.network())
    bob = _cand_body(html, fx.BOB)
    assert "amount+timing" not in bob.split("What was checked")[0]
    assert "feeds: linked address (lead" in bob
    assert "linked withdrawal sender (context, not scored)" in bob


def test_why_line_is_full_width_and_names_the_evidence():
    from tornado_demix.heuristics import candidate_reason

    data = fx.two_pool_result()
    sender = "0x82e6000000000000000000000000000000000082"
    apply_heuristics(data, counterparties={sender}, withdrawal_senders={"0xb1": sender})
    html = build_html_report(data, fx.network())
    bob = _cand_body(html, fx.BOB)
    row = next(r for r in ranked_candidates(data) if r["address"] == fx.BOB)
    import html as htmllib

    why = bob[bob.index('<div class="why">') :]
    why = why[: why.index("</div>")]
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", why))
    assert text == "Why: " + candidate_reason(row)
    assert f">{sender}</a>" in why  # full address, linked
    assert '<div class="why">' in bob.split('<details class="tl">')[0]
    assert "qualifying" not in html


def test_families_column_shows_context_signals_marked_as_context():
    data = fx.two_pool_result()
    sender = "0x82e6000000000000000000000000000000000082"
    apply_heuristics(data, counterparties={sender}, withdrawal_senders={"0xb1": sender})
    html = build_html_report(data, fx.network())
    bob = _cand_body(html, fx.BOB)
    first_row = bob[: bob.index("</tr>")]
    assert '<span class="pill ctx">linked_sender (context)</span>' in first_row
    assert "<td>—</td>" not in first_row


def _print_css():
    css = REPORT_CSS
    return css[css.index("@media print") :]


def test_print_css_never_forces_a_page_break_per_row():
    # the one forced break ends the first page (the summary page), never a row
    forced_rules = re.findall(r"([^{}]+)\{[^{}]*break-after:page", REPORT_CSS)
    assert [r.strip() for r in forced_rules] == [".p1"]
    css = re.sub(r"\.p1\{[^}]*break-after:page[^}]*\}", "", REPORT_CSS)
    for forced in (
        "break-before:page",
        "break-after:page",
        "page-break-before",
        "page-break-after",
    ):
        assert forced not in css, forced
    printed = _print_css()
    # the expanded timeline row may flow over pages; only small blocks avoid a break
    assert "tr.det{break-inside:auto" in printed
    avoid = re.findall(r"([^{}]+)\{[^}]*break-inside:avoid", printed)
    for selectors in avoid:
        for sel in selectors.split(","):
            assert sel.strip() not in ("tr", "tbody", "table", "tbody.cand"), sel


def test_print_css_lets_hash_cells_wrap_and_tables_fit_the_page():
    printed = _print_css()
    assert ".ad" in printed and "overflow-wrap:anywhere" in printed
    assert "min-width:0" in printed  # the narrow-screen min-width does not apply on paper
    assert ".sec-body{overflow:visible" in printed


def test_ties_are_labelled():
    data = _demix()
    html = build_html_report(data, fx.network())
    scores = [f"{c['confidence']:.2f}" for c in ranked_candidates(data)]
    assert ("tie at" in html) == (len(scores) != len(set(scores)))


def test_report_uses_no_external_resource_except_explorer_links():
    net = fx.network()
    for html in (
        build_html_report(_demix(), net),
        build_multi_report(_multi(), net),
        build_trace_report(_trace(), net),
    ):
        assert "<link" not in html and "@import" not in html and "url(" not in html
        assert not re.search(r"<script[^>]+src=", html)
        for url in re.findall(r'(?:href|src)="([^"]*)"', html):
            assert url.startswith("#") or url.startswith("https://etherscan.io/"), url


def test_print_css_expands_sections_hides_controls_and_numbers_pages():
    css = REPORT_CSS
    assert "@media print" in css and "counter(page)" in css
    printed = css[css.index("@media print") :]
    assert "details::details-content" in printed
    assert ".fbar" in printed and "button.cp" in printed and ".toc" in printed
    assert "tbody.fh{display:table-row-group" in printed
    assert "beforeprint" in REPORT_JS  # opens closed sections for printing


def test_dark_and_light_themes_come_from_the_system_setting():
    assert "@media (prefers-color-scheme:dark)" in REPORT_CSS
    assert ':root[data-theme="dark"]' in REPORT_CSS


def test_script_only_adds_controls():
    # copy buttons and filters are built by the script; the HTML has no dead buttons
    html = build_html_report(_demix(), fx.network())
    body = html[: html.index("<script>")]
    assert 'class="cp"' not in body
    assert "navigator.clipboard" in REPORT_JS and "'copy'" in REPORT_JS


def test_fragment_and_uid_for_the_web_ui():
    frag = build_html_report(_demix(), fx.network(), fragment=True, uid="w1")
    assert "<!DOCTYPE" not in frag and "<script" not in frag and "<style" not in frag
    assert 'id="w1-leads"' in frag and 'data-scope="w1-leads"' in frag


def test_report_without_deposits_still_has_a_verdict():
    data = {"wallet": fx.WALLET, "deposits": [], "vouchers": [], "denoms": {}, "params": {}}
    html = build_html_report(data, fx.network())
    assert "No Tornado.Cash deposits found" in html
    assert 'class="verdict"' in html and _section_keys(html) == ["method", "provenance"]


# --- other report types --------------------------------------------------------------


def _multi():
    from tests.test_report import _multi_corr

    return _multi_corr()


def test_multi_report_has_the_verdict_and_ordered_sections():
    html = build_multi_report(_multi(), fx.network())
    assert html.index('class="verdict"') < html.index('class="toc"')
    keys = _section_keys(html)
    assert keys == [k for k in SECTION_ORDER if k in keys]
    for key in ("leads", "context", "chance", "failed", "clusters", "wallets"):
        _count(html, key)


def test_characterize_and_classify_reports_have_a_verdict():
    from tests.test_report import _char_info

    html = build_characterize_report(_char_info(), fx.network())
    assert 'class="verdict"' in html and _count(html, "inflows")
    assert _section_keys(html)[0] == "context"
    info = {
        "address": fx.BOB,
        "kind": "deposit_like",
        "senders": [fx.ALICE],
        "sweep_targets": [START],
        "errors": [],
        "truncated": [],
    }
    html = build_classify_report(info, fx.network())
    assert _count(html, "senders") == "1"
    assert "context, not scored" in html
    assert f">{START}</a>" in html


def test_trace_totals_add_up_to_the_start_amount():
    result = _trace()
    rows = trace_totals(result)
    assert rows == [
        {"asset": "ETH", "endpoints": 6.0, "below": 0.05, "failed": 3.95, "total": 10.0}
    ]
    html = build_trace_report(result, fx.network())
    assert "End points 6 + below threshold 0.05 + failed lookups 3.95 = <b>10 ETH</b>" in html
    assert "(start amount 10 ETH)" in html
    assert _count(html, "stops") == "3" and _count(html, "failed") == "1"
    assert "1 end point, 1 below threshold, 1 at a failed lookup" in html
    assert 'class="vbar"' not in html  # stops of different kinds are not one proportion


def test_trace_end_states_are_badges_and_hops_carry_utc_times():
    html = build_trace_report(_trace(), fx.network())
    assert '<span class="badge bridge">bridge</span>' in html
    assert '<span class="badge chance">below threshold</span>' in html
    assert '<span class="badge fail">lookup failed</span>' in html
    assert "2023-11-14 22:13" in html  # first edge, UTC
    assert "possible (history incomplete)" in html
    assert "FIFO 6.0 [0.0 – 6.0] ?" in html
    for tx in (TX2, TX3):
        assert f">{tx}</a>" in html


def test_trace_diagram_labels_end_states_and_bridges():
    html = build_trace_report(_trace(), fx.network())
    svg = html[
        html.index('<svg class="flow"') : html.index("</svg>", html.index('<svg class="flow"'))
    ]
    assert "below threshold" in svg and "lookup failed" in svg
    assert 'class="e-bridge"' in svg and "bridge across" in svg
    assert f"<title>{HOP2} — bridge across (not followed)</title>" in svg


def test_end_state_tones():
    assert end_state("lookup_failed")[0] == "fail"
    assert end_state("below_threshold")[0] == "chance"
    assert end_state("tornado_redeposit")[0] == "warn"
    assert end_state("bridge arbitrum, l2 credit not found")[0] == "bridge"
    assert end_state("fill_not_found")[0] == "bridge"
    assert end_state("labelled address")[0] == "ok"


# --- fix round 1 -------------------------------------------------------------------


def test_each_table_has_its_own_filter_bar():
    for html in (
        build_html_report(_demix(), fx.network()),
        build_multi_report(
            dict(_multi(), cross_profile={fx.BOB: [fx.WALLET, fx.ALICE]}), fx.network()
        ),
        build_trace_report(_trace(), fx.network()),
    ):
        bars = re.findall(r'<div class="fbar noprint" data-scope="([^"]+)" hidden>', html)
        tables = re.findall(r'<table class="(?:cands )?ft" data-scope="([^"]+)"', html)
        assert sorted(bars) == sorted(tables)
        assert bars
        assert len(set(bars)) == len(bars)
        for scope in bars:  # the bar sits right before its own table
            bar = html.index(f'<div class="fbar noprint" data-scope="{scope}"')
            assert html.index(f'data-scope="{scope}"', bar + 30) > bar


def test_filter_status_counts_one_table_and_search_ignores_buttons():
    js = REPORT_JS
    assert "rows in this table shown" in js and "No row of this table matches" in js
    # the search text is read before the copy buttons are added
    assert js.index("tdxText=it.textContent") < js.index("b.className='cp'")
    assert "it.tdxText" in js and "it.textContent.toLowerCase().indexOf" not in js
    # a polite live region announces the copy result
    assert "aria-live','polite'" in js and "live.textContent=ok?'Copied '" in js


def test_badge_tones_all_have_a_style():
    for tone in ("strong", "moderate", "weak", "chance", "ctx", "fail", "warn", "ok", "bridge"):
        assert f".badge.{tone}" in REPORT_CSS, tone


def test_demix_verdict_counts_context_candidates():
    data = fx.two_pool_result()
    dep = "0x" + "d" * 40
    data["deposit_addresses"] = [
        {"address": dep, "kind": "exchange_deposit", "senders": [], "evidence": True}
    ]
    apply_heuristics(data, counterparties=set(), shared_deposits={fx.BOB: [dep]})
    html = build_html_report(data, fx.network())
    assert _count(html, "leads") == "0"
    assert _count(html, "context") == "1"
    assert _count(html, "chance") == "1"
    ctx = html[html.index('id="r-context"') : html.index('id="r-chance"')]
    assert f">{fx.BOB}</a>" in ctx and "shared exchange deposit address" in ctx
    assert "No lead: all 2 candidates are weak (1 with context only, 1 chance-level)" in html


def test_multi_counts_single_pool_profile_matches_as_chance_level():
    html = build_multi_report(_multi(), fx.network())
    # the fixture's one profile match is on a single-pool fingerprint (a count match)
    assert _count(html, "context") == "0"
    assert _count(html, "chance") == "2"
    assert "1 single-pool profile match:" in html


def test_fee_section_counts_shared_fingerprints_and_self_relayed_leads():
    html = build_html_report(_demix(), fx.network())
    assert '<a href="#r-fees">Fee fingerprints<span class="n">0 shared, ' in html


HOSTILE = '<img src=x onerror=alert(1)><script>alert(2)</script>"'


def _no_raw_markup(html):
    assert "<img src=x" not in html and "<script>alert" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_hostile_label_text_is_escaped_in_every_new_place():
    from tornado_demix.report_html import _sections_html

    data = _demix()
    html = build_html_report(data, fx.network(), attribution={fx.BOB: {"label": HOSTILE}})
    _no_raw_markup(html)
    toc = _sections_html("r", {"leads": (HOSTILE, "x", HOSTILE)})
    _no_raw_markup(toc)
    result = _trace()
    result["terminals"][0]["reason"] = HOSTILE
    result["terminals"][1]["label"] = {"label": HOSTILE}
    html = build_trace_report(result, fx.network())
    _no_raw_markup(html)
    svg = html[
        html.index('<svg class="flow"') : html.index("</svg>", html.index('<svg class="flow"'))
    ]
    assert "&lt;img src=x" in svg  # the end reason in a node title


def _with_incomplete(data, failed, cut):
    data["errors"] = [
        {"kind": "lookup_failed", "what": "txlist", "address": "0x" + "1" * 40}
    ] * failed
    data["error_count"] = failed
    data["truncated"] = [
        {"what": "tokentx", "address": "0x" + "2" * 40, "reason": "row_cap", "limit": 1000}
    ] * cut
    return data


def test_verdict_counts_failed_lookups_and_cut_off_lists_apart():
    html = build_html_report(_with_incomplete(fx.two_pool_result(), 3, 1), fx.network())
    assert "3 lookups failed and 1 list cut off by the explorer" in html
    assert _count(html, "failed") == "3"
    assert "1 list cut off" in html


def test_verdict_with_only_cut_off_lists_does_not_say_lookups_failed():
    html = build_html_report(_with_incomplete(fx.two_pool_result(), 0, 2), fx.network())
    assert "2 lists cut off by the explorer, so results may be incomplete" in html
    assert _count(html, "failed") == "0"
    assert "lookup(s)" not in html and "lookups failed" not in html.split("Verdict")[1][:1500]


def test_singular_and_plural_in_the_verdict_sentence():
    html = build_html_report(_with_incomplete(fx.two_pool_result(), 1, 0), fx.network())
    assert "1 lookup failed" in html
    assert "lookup(s)" not in html and "candidate(s)" not in html and "lead(s)" not in html


def test_weak_candidates_are_never_called_leads_in_the_placebo_line():
    from tornado_demix.placebo import _verdict

    text = _verdict("weak", 6, 2, 1.5, 0.5)
    assert "6 weak candidates" in text and "weak lead" not in text
    assert _verdict("weak", 1, 0, 1.0, 0.0).startswith("1 weak candidate in")
    assert _verdict("moderate", 1, 1, 1.0, 1.0).startswith("1 moderate lead in")
    assert _verdict("weak", 0, 0, 1.0, 1.0) == "No weak candidates in either window"


def test_deposited_tile_shows_amount_then_asset_with_thousands_separator():
    from tornado_demix.report_html import format_totals_human

    assert format_totals_human({"ETH": 2400.0}) == "2,400 ETH"
    assert format_totals_human({"ETH": 0.5, "DAI": 990.0}) == "990 DAI; 0.5 ETH"


def test_raw_data_is_screen_only_with_a_print_note():
    from tornado_demix.report_html import REPORT_CSS

    html = build_html_report(fx.two_pool_result(), fx.network())
    assert "details.sec[data-sec=raw]{display:none!important}" in REPORT_CSS
    assert 'class="printonly"' in html and "in the JSON export" in html
    assert ".printonly{display:none}" in REPORT_CSS


def test_provenance_and_hashes_print_and_only_the_raw_dump_is_hidden():
    from tornado_demix.report_html import REPORT_CSS

    html = build_html_report(fx.two_pool_result(), fx.network())
    keys = _section_keys(html)
    assert "provenance" in keys and "raw" in keys
    prov = html[html.index('data-sec="provenance"') : html.index('data-sec="raw"')]
    assert "Provenance" in prov and "Transaction hashes" in prov and "SHA-256" in prov
    raw = html[html.index('data-sec="raw"') :]
    assert "Transaction hashes" not in raw.split("</details>")[0]
    # print CSS hides only the raw section
    assert "data-sec=raw]{display:none" in REPORT_CSS
    assert "data-sec=provenance" not in REPORT_CSS and "data-sec=inflows" not in REPORT_CSS


def test_no_optional_plural_forms_remain_in_the_report_sources():
    import pathlib

    import tornado_demix.report_html as mod

    text = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    import re

    # "(s)" survives only inside the JS/CSS of the page (e.g. selectors), not in prose
    prose = [
        m
        for m in re.finditer(
            r"(?:wallet|pool|hop|stop|swap|bridge|point|voucher|deposit|withdrawal|lookup|candidate|lead|cluster|consolidator|inflow)\(s\)",
            text,
        )
    ]
    assert not prose, [m.group(0) for m in prose]


def test_characterize_pool_inflows_are_not_in_the_screen_only_section():
    from tests.test_report import _char_info

    html = build_characterize_report(_char_info(), fx.network())
    keys = _section_keys(html)
    assert "inflows" in keys and "raw" not in keys
