"""Reports write addresses in full in tables and running text (diagram labels may be short):
the exit-group anchor, the trace totals' token tag and the page-1 end-state reason."""

import re

from tests.fixtures import demix_result as fx
from tornado_demix.report_html import _groups_html, trace_totals
from tornado_demix.report_page1 import compact_reason

ANCHOR = "0x0562ddf7ea5ab56728852eea2eacab61c4b78a1a"
MEMBER = "0x" + "4e" * 20


def test_exit_group_anchor_is_full_and_linked():
    group = {
        "anchored": True,
        "pool_key": "100 ETH",
        "members": [ANCHOR, MEMBER],
        "notes": 7,
        "first_ts": 1656610261,
        "last_ts": 1656673649,
        "anchors": [{"address": ANCHOR, "why": "known exit"}],
    }
    html = _groups_html([group], fx.network())
    why = html[html.index('class="why"') : html.index("</div>", html.index('class="why"'))]
    assert ANCHOR in why and "…" not in why
    assert 'class="ad"' in why  # a link like the member list, so it gets the copy button
    assert "(known exit)" in why


def test_trace_totals_tag_a_same_symbol_token_with_its_full_address():
    real, fake = "0x" + "1" * 40, "0x" + "2" * 40
    ends = [
        {
            "asset": "USDC",
            "asset_address": a,
            "chain_id": 1,
            "amount": 1.0,
            "reason": "labelled address",
        }
        for a in (real, fake)
    ]
    rows = trace_totals({"amount": 2.0, "asset": "USDC", "terminals": ends})
    assert {r["asset"] for r in rows} == {f"USDC ({real})", f"USDC ({fake})"}


def test_page1_reason_never_shows_part_of_an_identifier():
    addr = "0x" + "7" * 40
    b58 = "Hx7aBcDeFgHjKmNpQrStUvWxYz123456789abcdefghJ"
    early = "bridge zksync (to " + addr + ", chain not supported by this tool)"
    for reason in (
        f"bridge zksync (recipient {addr}, chain not supported)",
        f"bridge mayan (to solana, recipient {b58})",
        "swap cow (order 0x" + "ab" * 56 + ", payout not found)",
        early,
    ):
        text = compact_reason(reason, limit=60)
        for ident in re.findall(r"0x[0-9a-f]+|Hx7\w+", reason):
            assert ident in text or not any(ident[:k] in text for k in (8, 12))
    # an identifier that ends before the cut stays whole
    assert addr in compact_reason(early, limit=70) and len(early) > 70
