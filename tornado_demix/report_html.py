"""Self-contained HTML reports with inline CSS."""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import datetime, timezone

from .attribution import format_label
from .bridges_canonical import recipient_links
from .deposit_addresses import NOT_DEPOSIT, UNKNOWN_KIND, check_text, kind_label, target_label
from .heuristics import (
    BAND_ORDER,
    CONTEXT_SIGNALS,
    LEAD_SIGNALS,
    NOTE_SIGNALS,
    _last_deposit_before,
    _numbers_text,
    band_rationale,
    candidate_kind,
    candidate_reason,
    conclusion,
    cross_method,
    method_breakdown,
    numbered_deposits,
    profile_match_kind,
    ranked_candidates,
    voucher_deposit_numbers,
)
from .honest import (
    EXACT_K_CAVEAT,
    EXACT_K_TITLE,
    GROUP_VIEW_TITLE,
    SHARED_CANDIDATES_CAVEAT,
    SHARED_CANDIDATES_TITLE,
    band_note,
    context_verdicts,
    exploratory_banner,
    how_to_read,
    limitations,
)
from .honest import provenance as _provenance
from .incomplete import NOTICE_REASONS
from .incomplete import lines as _incomplete_lines
from .incomplete import notice_only as _notice_only
from .incomplete import summary as _incomplete_summary
from .multi import SYNC_MAX_SPAN_HOURS, exact_k_skips, exact_k_tail_text
from .networks import Network
from .placebo import BANDS
from .relayer import (
    analyze_relayers,
    collect_withdrawals,
    fee_key,
    fee_label,
    frequency_text,
    report_leads,
)
from .report_csv import (
    _fmt_hours,
    _ts,
    evidence_families,
    format_fingerprint,
    format_span,
    format_totals,
)
from .report_json import (
    analysis_assumptions,
)
from .report_page1 import (
    PAGE1_CSS,
    characterize_page1,
    classify_page1,
    cluster_page1,
    demix_page1,
    multi_page1,
    trace_page1,
)
from .trace import (
    BELOW_THRESHOLD,
    END_STATES_NOTE,
    LOOKUP_FAILED,
    TORNADO_REDEPOSIT,
    bridge_text,
    bridge_tx_label,
    recipient_text,
    share_status,
    share_text,
)


def _e(x):
    return html.escape(str(x))


def _addr_link(network, addr):
    return '<a class="ad" href="{}" target="_blank" rel="noopener">{}</a>'.format(
        _e(network.addr_url(addr)), _e(addr)
    )


def _tx_link(network, tx_hash):
    return '<a class="ad" href="{}" target="_blank" rel="noopener">{}</a>'.format(
        _e(network.tx_url(tx_hash)), _e(tx_hash)
    )


def how_to_read_html():
    """The "How to read this" section, built from the signal constants (honest.how_to_read)."""
    body = "".join(f"<p>{_e(par)}</p>" for par in how_to_read())
    return f'<h2>How to read this<span class="rule"></span></h2><div class="callout">{body}</div>'


def banner_html():
    """The "exploratory: no PROTOCOL.lock" banner, or "" when the lock exists."""
    text = exploratory_banner()
    return f'<div class="callout">{_e(text)}</div>' if text else ""


def limitations_html():
    """The "Limitations" section, one list item per entry of honest.LIMITATIONS."""
    items = "".join(f"<li>{_e(line)}</li>" for line in limitations())
    return (
        '<h2>Limitations<span class="rule"></span></h2>'
        f'<div class="callout"><ul style="margin:0;padding-left:1.1rem">{items}</ul></div>'
    )


def provenance_html(prov: dict | None, params: dict | None = None, json_sha256: str | None = None):
    """The "Provenance" section: tool version and commit, command line, parameters, data
    sources, label sets, the SHA-256 of the JSON export and the UTC generation time."""
    prov = prov or {}
    rows = [
        (
            "Tool",
            f"tornado-demix {prov.get('version', '?')} (commit {prov.get('commit', 'unknown')})",
        ),
        ("Generated", prov.get("generated_utc", "")),
        ("Command", prov.get("command") or "not recorded"),
    ]
    if params:
        rows.append(("Parameters", ", ".join(f"{k}={v}" for k, v in sorted(params.items()))))
    rows.append(("Data sources", "; ".join(prov.get("data_sources") or []) or "not recorded"))
    sets = []
    for ls in prov.get("label_sets") or []:
        if ls.get("sha256"):
            sets.append(
                f"{ls['name']}: {ls['file']}, SHA-256 {ls['sha256']}"
                + (f", modified {ls['modified_utc']}" if ls.get("modified_utc") else "")
            )
        else:
            sets.append(f"{ls['name']}: none loaded")
    rows.append(("Label sets", "; ".join(sets) or "none loaded"))
    rows.append(
        (
            "JSON export SHA-256",
            json_sha256 or "the JSON export was not written in this run",
        )
    )
    rows.append(
        (
            "PROTOCOL.lock",
            "present" if prov.get("protocol_lock") else "absent (exploratory)",
        )
    )
    body = "".join(
        f'<tr><th style="width:22%">{_e(k)}</th><td class="mono wrap">{_e(v)}</td></tr>'
        for k, v in rows
    )
    return f'<h2>Provenance<span class="rule"></span></h2><table>{body}</table>'


def evidence_hashes_html(data: dict, network: Network, cands: list[dict] | None = None):
    """Every deposit and every count-match withdrawal transaction hash, in full."""
    out = ['<h2>Transaction hashes<span class="rule"></span></h2>']
    nums = [n for n, d in numbered_deposits(data) if d.get("hash")]
    if nums:
        # Listed in full once, in the deposits table at the top (always printed).
        out.append(
            f'<h3>Deposits</h3><div class="meta">{_n(len(nums), "deposit transaction")} '
            f"({_e(_numbers_text(nums))}): full hashes in the Tornado.Cash deposits table at "
            "the top of this report.</div>"
        )
    cands = ranked_candidates(data) if cands is None else cands
    blocks = []
    for r in cands[:60]:
        recs = data["denoms"][r["pool_key"]].get("detail", {}).get(r["address"], [])
        hashes = "<br>".join(
            _tx_link(network, x["hash"])
            for x in sorted(recs, key=lambda x: x.get("ts", 0))
            if x.get("hash")
        )
        blocks.append(
            f'<tr class="long"><td class="mono wrap">{_addr_link(network, r["address"])}</td>'
            f'<td class="num">{_e(r["pool_key"])}</td><td class="mono wrap">{hashes}</td></tr>'
        )
    if blocks:
        out.append(
            "<h3>Withdrawals received by the candidates</h3>"
            '<table><colgroup><col style="width:32%"><col style="width:9%"><col></colgroup>'
            "<tr><th>Recipient</th><th>Pool</th><th>Withdrawal transactions</th></tr>"
            + "".join(blocks)
            + "</table>"
        )
        if len(cands) > 60:
            out.append(
                f'<div class="meta">{len(cands) - 60} more candidates: their withdrawal '
                "hashes are in the CSV and JSON.</div>"
            )
    return "".join(out)


def _band_badge(band):
    """Coloured strong/moderate/weak badge, legible in @media print."""
    return '<span class="badge {0}">{0}</span>'.format(_e(band))


# One stylesheet for the HTML reports and the web UI (webui/app.py inlines it), so the
# two share one layout. Light and dark through prefers-color-scheme; a data-theme
# attribute on <html> (the web UI's toggle) overrides it. Colours meet WCAG AA on
# their backgrounds. In print every section is expanded, filters and buttons are
# hidden and the pages are numbered.
REPORT_CSS = (
    """
 :root{--bg:#f4f6f8;--panel:#ffffff;--ink:#17202b;--soft:#3b4654;--muted:#566070;
   --line:#d9dee6;--head:#eef1f5;--accent:#0b6b63;--accent-soft:#e0f1ee;
   --mod:#8a4500;--mod-soft:#fbecd8;--weak:#4f5966;--weak-soft:#eceff3;
   --ctx:#2f5d8a;--ctx-soft:#e4edf7;--fail:#a3222d;--fail-soft:#fbe6e8;
   --radius:8px;color-scheme:light}
 @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
   --bg:#0f141a;--panel:#161d26;--ink:#e4e9ef;--soft:#c3ccd7;--muted:#9aa6b4;
   --line:#2b3644;--head:#1d2631;--accent:#5fd4c4;--accent-soft:#123430;
   --mod:#f2b46c;--mod-soft:#3a2810;--weak:#b3bdc9;--weak-soft:#222b36;
   --ctx:#8fb8e6;--ctx-soft:#16273a;--fail:#ff9a9f;--fail-soft:#3a1519;color-scheme:dark}}
 :root[data-theme="dark"]{
   --bg:#0f141a;--panel:#161d26;--ink:#e4e9ef;--soft:#c3ccd7;--muted:#9aa6b4;
   --line:#2b3644;--head:#1d2631;--accent:#5fd4c4;--accent-soft:#123430;
   --mod:#f2b46c;--mod-soft:#3a2810;--weak:#b3bdc9;--weak-soft:#222b36;
   --ctx:#8fb8e6;--ctx-soft:#16273a;--fail:#ff9a9f;--fail-soft:#3a1519;color-scheme:dark}
 *{box-sizing:border-box}
 [hidden]{display:none!important}
 body{font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
   color:var(--ink);margin:0;background:var(--bg);-webkit-font-smoothing:antialiased}
 .sheet{max-width:1180px;margin:0 auto;padding:0 1.25rem 3rem}
 .rhead{background:var(--panel);border-bottom:1px solid var(--line);margin-bottom:1rem}
 .rhead .sheet{padding-top:1.1rem;padding-bottom:.9rem}
 .kicker{font-size:.8rem;color:var(--muted)}
 h1{font-size:1.45rem;font-weight:650;letter-spacing:-.015em;margin:.15rem 0 .35rem}
 h2{font-size:1.02rem;font-weight:650;margin:1.6rem 0 .3rem}
 h2 .rule{display:none}
 h3{font-size:.9rem;font-weight:650;margin:1.1rem 0 .35rem;color:var(--soft)}
 p{margin:.35rem 0}
 .meta{color:var(--muted);font-size:.82rem}
 .meta b{color:var(--soft);font-weight:600}
 .mono,.ad{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.8rem}
 .num{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
   font-variant-numeric:tabular-nums}
 .wrap,.ad{overflow-wrap:anywhere;word-break:break-all}
 a{color:var(--accent);text-decoration:none}
 a:hover{text-decoration:underline}
 a:focus-visible,button:focus-visible,summary:focus-visible,select:focus-visible,
 input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
 table{width:100%;border-collapse:collapse;margin:.5rem 0 .4rem;table-layout:fixed}
 th{text-align:left;font-size:.74rem;color:var(--muted);font-weight:600;
   padding:.4rem .5rem;border-bottom:1px solid var(--line);background:var(--head)}
 td{padding:.42rem .5rem;font-size:.84rem;border-bottom:1px solid var(--line);
   vertical-align:top;overflow-wrap:anywhere}
 tr.lead td{background:var(--accent-soft)}
 .pill{display:inline-block;background:var(--head);border:1px solid var(--line);
   border-radius:999px;padding:0 .5rem;font-size:.74rem;color:var(--soft);margin:1px 3px 1px 0}
 .flag{color:var(--accent);font-weight:600}
 .callout{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--mod);
   border-radius:0 var(--radius) var(--radius) 0;padding:.7rem .9rem;color:var(--soft);
   font-size:.85rem;margin-top:.6rem}
 .callout.fail{border-left-color:var(--fail);background:var(--fail-soft);color:var(--ink)}
 .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
   background:var(--line);border:1px solid var(--line);border-radius:var(--radius);
   overflow:hidden;margin-top:.8rem}
 .metric{background:var(--panel);padding:.7rem .9rem}
 .metric .k{font-size:.76rem;color:var(--muted)}
 .metric .v{font-family:ui-monospace,monospace;font-size:1.2rem;font-weight:600;margin-top:.1rem}
 .metric .s{font-size:.74rem;color:var(--muted)}
 .badge{display:inline-block;white-space:nowrap;border-radius:4px;padding:0 .4rem;
   font-size:.74rem;font-weight:650;border:1px solid var(--line);vertical-align:middle;
   -webkit-print-color-adjust:exact;print-color-adjust:exact}
 .tag{display:inline-block;border-radius:4px;padding:0 .4rem;font-size:.74rem;
   font-weight:650;border:1px solid var(--line);color:var(--soft);background:var(--head)}
 .tag.warn{background:var(--mod-soft);color:var(--mod);border-color:var(--mod)}
 .tag.fail{background:var(--fail-soft);color:var(--fail);border-color:var(--fail)}
 .badge.strong{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
 .badge.moderate{background:var(--mod-soft);color:var(--mod);border-color:var(--mod)}
 .badge.weak,.badge.chance{background:var(--weak-soft);color:var(--weak)}
 .badge.ctx{background:var(--ctx-soft);color:var(--ctx);border-color:var(--ctx)}
 .badge.fail{background:var(--fail-soft);color:var(--fail);border-color:var(--fail)}
 .badge.warn{background:var(--mod-soft);color:var(--mod);border-color:var(--mod)}
 .badge.ok{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
 .badge.bridge{background:var(--ctx-soft);color:var(--ctx);border-color:var(--ctx)}
 .cbar{display:block;height:3px;width:100%;max-width:80px;border-radius:2px;margin-top:3px;
   background:linear-gradient(90deg,var(--accent) var(--w),var(--line) var(--w))}
 .conf .pct{color:var(--muted);font-size:.76rem}
 .lead-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
   padding:.7rem .9rem;margin:.5rem 0}
 .lead-card .top{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
 .lead-card .why{color:var(--soft);font-size:.85rem;margin-top:.3rem}
 .rat{color:var(--muted);font-size:.78rem;margin-top:.2rem}
 .evidence{list-style:none;margin:.35rem 0 .1rem;padding:0;font-size:.8rem}
 .evidence li{padding:.08rem 0}
 .evidence .ev-yes b{color:var(--accent)}
 .evidence .ev-no{color:var(--muted)}
 .flow-wrap{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
   padding:.5rem;margin:.6rem 0;overflow-x:auto}
 .flow{display:block;width:100%;min-width:560px;height:auto;color:var(--ink)}
 .flow .n{fill:var(--panel);stroke:var(--line)}
 .flow .t{fill:var(--ink)}
 .flow .m{fill:var(--muted)}
 .flow .e-pool{stroke:var(--line)}
 .flow .e-strong{stroke:var(--accent)}.flow .e-moderate{stroke:var(--mod)}
 .flow .e-weak{stroke:var(--weak)}.flow .dep{stroke:var(--accent);fill:var(--accent-soft)}
 .flow .b-strong{fill:var(--accent)}.flow .b-moderate{fill:var(--mod)}.flow .b-weak{fill:var(--weak)}
 .flow .e-bridge{stroke:var(--ctx);stroke-dasharray:5 4}.flow .b-bridge{fill:var(--ctx)}
 .flow .e-fail{stroke:var(--fail)}.flow .b-fail{fill:var(--fail)}
 .flow .b-ctx{fill:var(--ctx)}.flow .b-warn{fill:var(--mod)}.flow .b-ok{fill:var(--accent)}
 .toc{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
   display:flex;flex-wrap:wrap;gap:.25rem .9rem;padding:.45rem 0;margin:0 0 .6rem;
   font-size:.82rem}
 .toc a{color:var(--soft)}
 .toc a .n{font-family:ui-monospace,monospace;color:var(--muted);margin-left:.2rem}
 .toc .tbtn{margin-left:auto}
 details.sec{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
   margin:.5rem 0;scroll-margin-top:3rem}
 details.sec>summary{cursor:pointer;list-style:none;display:flex;align-items:baseline;gap:.6rem;
   padding:.6rem .9rem}
 details.sec>summary::-webkit-details-marker{display:none}
 details.sec>summary::before{content:"\\25B8";color:var(--muted);font-size:.8rem}
 details.sec[open]>summary::before{content:"\\25BE"}
 details.sec>summary h2{margin:0;font-size:1rem}
 details.sec>summary .cnt{font-family:ui-monospace,monospace;font-size:.8rem;color:var(--muted)}
 details.sec>summary .sub{font-size:.8rem;color:var(--muted)}
 .sec-body{padding:0 .9rem .8rem;border-top:1px solid var(--line)}
 .sec-body h2{font-size:.92rem;margin:1rem 0 .3rem;color:var(--soft)}
 tbody.cand>tr:first-child>td{border-bottom:0}
 tr.det td{padding-top:0}
 tr.det .why{font-size:.82rem;color:var(--soft);margin:0 0 .25rem;overflow-wrap:anywhere}
 tr.det .why b{color:var(--ink);font-weight:600}
 .pill.ctx{background:var(--ctx-soft);color:var(--ctx);border-color:var(--ctx)}
 .deps{margin:.4rem 0 .6rem}
 .deps h2{margin:.6rem 0 .2rem}
 details.tl>summary{cursor:pointer;color:var(--muted);font-size:.78rem}
 .tl ol{margin:.35rem 0 .3rem;padding-left:1.2rem;font-size:.8rem}
 .tl li{margin:.18rem 0}
 .tl .fam{font-size:.72rem;color:var(--muted)}
 .tl .lead-s{color:var(--accent);font-weight:600}.tl .ctx-s{color:var(--ctx)}
 .fbar{display:flex;flex-wrap:wrap;gap:.5rem .8rem;align-items:center;font-size:.8rem;
   margin:.2rem 0 .4rem;color:var(--muted)}
 .fbar select,.fbar input{font:inherit;color:var(--ink);background:var(--panel);
   border:1px solid var(--line);border-radius:6px;padding:.2rem .4rem}
 .fbar input{min-width:16rem}
 .fh{display:none}
 .cpx{position:fixed;left:-9999px;opacity:0}
 .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
 button.cp,.tbtn{font:inherit;font-size:.68rem;color:var(--muted);background:transparent;
   border:1px solid var(--line);border-radius:4px;padding:0 .3rem;margin-left:.3rem;
   cursor:pointer;vertical-align:middle;line-height:1.4}
 button.cp:hover,.tbtn:hover{color:var(--accent);border-color:var(--accent)}
 .totals{font-size:.86rem;margin:.5rem 0;padding:.5rem .7rem;background:var(--head);
   border-radius:6px}
 .pill,.badge,.tag{overflow-wrap:normal;word-break:normal}
 .pill,.tag{max-width:100%;overflow-wrap:anywhere}
 .pill.long{border-radius:6px;word-break:break-all}
 @media (max-width:900px){.sec-body{overflow-x:auto}table.cands,table.ft{min-width:860px}}
 .foot{margin-top:2rem;padding-top:.8rem;border-top:1px solid var(--line);
   color:var(--muted);font-size:.76rem}
 @media (prefers-reduced-motion:no-preference){a,button{transition:color .12s ease}}
 @page{size:A4;margin:12mm 11mm 14mm;
   @bottom-center{content:"Page " counter(page) " of " counter(pages);font-size:8pt;color:#57534e}}
 .printonly{display:none}
 @media print{
   :root{--bg:#fff;--panel:#fff;--ink:#000;--soft:#222;--muted:#444;--line:#bbb;--head:#f2f2f2;
     color-scheme:light}
   body{background:#fff;font-size:8.5pt;line-height:1.35}
   .sheet{max-width:none;padding:0}
   .rhead{border:0;margin-bottom:.3rem}
   .rhead .sheet{padding:0 0 .3rem}
   h1{font-size:13pt;margin:.1rem 0 .2rem}
   h2{font-size:10pt;margin:.6rem 0 .2rem}
   h3{font-size:9pt;margin:.4rem 0 .15rem}
   p{margin:.2rem 0}
   .toc,.fbar,button.cp,.tbtn,.noprint{display:none!important}
   details.sec[data-sec=raw]{display:none!important}
   .printonly{display:block}
   .fh{display:revert!important}
   tbody.fh{display:table-row-group!important}
   details.sec{border:0;border-radius:0;margin:.3rem 0 0;break-inside:auto}
   details.sec>summary{padding:.2rem 0;border-bottom:1.5px solid var(--ink)}
   details.sec>summary h2{font-size:10.5pt}
   .sec-body{overflow:visible;padding:.1rem 0 .2rem;border-top:0}
   .sec-body h2{font-size:9.5pt;margin:.5rem 0 .15rem}
   details::details-content{content-visibility:visible;display:block}
   details>summary::before{content:""!important}
   details.tl>summary{font-size:7.5pt;margin:.1rem 0}
   a{color:var(--ink)}
   table{margin:.2rem 0;min-width:0!important}
   table.cands,table.ft{min-width:0!important}
   th{font-size:7pt;padding:.15rem .25rem}
   td{font-size:7.8pt;padding:.15rem .25rem}
   .mono,.ad{font-size:7.2pt}
   .ad,.wrap{overflow-wrap:anywhere;word-break:break-all}
   td,.why,.tl li{overflow-wrap:anywhere}
   table.dtab .ad{font-size:6.8pt}
   .pill,.badge,.tag{font-size:6.8pt;padding:0 .25rem;word-break:normal}
   .meta,.rat,.evidence{font-size:7.5pt}
   .tl ol{margin:.15rem 0;padding-left:1rem;font-size:7.5pt}
   .tl li{margin:.05rem 0}
   .tl .fam{font-size:6.8pt}
   tr.det .why{font-size:7.8pt;margin:0 0 .1rem}
   .evidence li{padding:0}
   .callout{padding:.35rem .5rem;margin-top:.3rem;font-size:7.8pt}
   .metric{padding:.3rem .45rem}.metric .v{font-size:10pt}
   .flow-wrap{padding:.2rem;margin:.3rem 0;overflow:visible}
   .flow{min-width:0!important;width:78%;margin:0 auto}
   .foot{margin-top:.6rem;padding-top:.3rem}
   tr:not(.det):not(.long),.lead-card,.flow-wrap,h2,h3{break-inside:avoid}
   tr.det{break-inside:auto}
   tr.long{break-inside:auto}
   tbody.cand{break-inside:auto}
   tbody.cand>tr:first-child,h2,h3,details.sec>summary{break-after:avoid}}
"""
    + PAGE1_CSS
)

# Filters, search, copy buttons, the table of contents and print expansion. Plain
# DOM code that only adds controls: with JS off every row, section and address is
# already in the HTML and the page works as a static document.
REPORT_JS = r"""
(function(){
var d=document;
function all(sel,root){return Array.prototype.slice.call((root||d).querySelectorAll(sel));}
// search text of each filterable row, read before any button is added
all('tbody[data-f]').forEach(function(it){it.tdxText=it.textContent.toLowerCase();});
var live=d.createElement('span'); live.className='sr'; live.setAttribute('role','status');
live.setAttribute('aria-live','polite'); d.body.appendChild(live);
function fallbackCopy(t){var a=d.createElement('textarea');a.value=t;a.setAttribute('readonly','');
  a.className='cpx';d.body.appendChild(a);a.select();var ok=false;try{ok=d.execCommand('copy');}catch(e){}
  d.body.removeChild(a);return ok;}
function copy(t,b){function done(ok){b.textContent=ok?'copied':'select it';
  live.textContent=ok?'Copied '+t:'Copy failed: select the text and copy it';
  setTimeout(function(){b.textContent='copy';},1400);}
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(
    function(){done(true);},function(){done(fallbackCopy(t));});}else{done(fallbackCopy(t));}}
all('.ad').forEach(function(el){
  if(el.getAttribute('data-cp'))return; el.setAttribute('data-cp','1');
  var v=el.getAttribute('data-v')||el.textContent;
  var b=d.createElement('button'); b.type='button'; b.className='cp'; b.textContent='copy';
  b.setAttribute('aria-label','Copy '+v);
  b.addEventListener('click',function(ev){ev.preventDefault();copy(v,b);});
  el.parentNode.insertBefore(b,el.nextSibling);});
function openTarget(id){var t=id&&d.getElementById(id); if(t&&t.tagName==='DETAILS')t.open=true;}
all('.toc a[href^="#"]').forEach(function(a){a.addEventListener('click',function(){
  openTarget(a.getAttribute('href').slice(1));});});
if(location.hash)openTarget(location.hash.slice(1));
window.addEventListener('hashchange',function(){openTarget(location.hash.slice(1));});
all('.toc').forEach(function(nav){
  var b=d.createElement('button'); b.type='button'; b.className='tbtn'; b.textContent='Expand all';
  var secs=all('details.sec',nav.parentNode);
  b.addEventListener('click',function(){var open=b.textContent==='Expand all';
    secs.forEach(function(s){s.open=open;}); b.textContent=open?'Collapse all':'Expand all';});
  nav.appendChild(b);});
var BAND=['strong','moderate','weak'];
all('.fbar').forEach(function(bar){
  var scope=bar.getAttribute('data-scope');
  var items=all('table[data-scope="'+scope+'"] tbody[data-f]');
  if(!items.length)return;
  var fields=[['band','Band'],['state','End state'],['fam','Family'],['net','Network'],
    ['pool','Pool']], sels=[];
  fields.forEach(function(f){
    var seen={},vals=[];
    items.forEach(function(it){(it.getAttribute('data-'+f[0])||'').split('|').forEach(function(v){
      if(v&&!seen[v]){seen[v]=1;vals.push(v);}});});
    if(vals.length<2)return;
    vals.sort(function(a,b){var i=BAND.indexOf(a),j=BAND.indexOf(b);
      return (i<0?9:i)-(j<0?9:j)||(a<b?-1:a>b?1:0);});
    var lab=d.createElement('label'), s=d.createElement('select');
    lab.textContent=f[1]+' '; s.setAttribute('data-f',f[0]);
    var o=d.createElement('option'); o.value=''; o.textContent='all'; s.appendChild(o);
    vals.forEach(function(v){var o=d.createElement('option'); o.value=v; o.textContent=v; s.appendChild(o);});
    lab.appendChild(s); bar.appendChild(lab); sels.push(s); s.addEventListener('change',apply);});
  var q=d.createElement('input'); q.type='search'; q.placeholder='Search address or hash';
  q.setAttribute('aria-label','Search address or hash'); bar.appendChild(q);
  q.addEventListener('input',apply);
  var out=d.createElement('span'); out.setAttribute('role','status'); bar.appendChild(out);
  function apply(){
    var text=q.value.trim().toLowerCase(), shown=0;
    items.forEach(function(it){
      var ok=sels.every(function(s){return !s.value||
        ('|'+(it.getAttribute('data-'+s.getAttribute('data-f'))||'')+'|').indexOf('|'+s.value+'|')>=0;});
      if(ok&&text)ok=(it.tdxText||'').indexOf(text)>=0;
      it.classList.toggle('fh',!ok); if(ok)shown++;});
    out.textContent=shown?shown+' of '+items.length+' rows in this table shown':
      'No row of this table matches (0 of '+items.length+')';}
  bar.hidden=false; apply();});
var reopened=[];
window.addEventListener('beforeprint',function(){all('details:not([open])').forEach(function(x){
  reopened.push(x); x.open=true;});});
window.addEventListener('afterprint',function(){reopened.forEach(function(x){x.open=false;});
  reopened=[];});
})();
"""

# Report sections, always in this order (missing ones are left out). Leads is open; the
# rest are collapsed (a report without Leads opens its first section).
SECTION_ORDER = (
    "leads",
    "context",
    "chance",
    "deposits",
    "groups",
    "fees",
    "inflows",
    "method",
    "provenance",
    "raw",
)
SECTION_TITLES = {
    "leads": "Leads",
    "context": "Context",
    "chance": "Chance-level candidates",
    "deposits": "Deposit addresses",
    "groups": "Groups and clusters",
    "fees": "Fee fingerprints",
    "method": "Method and limitations",
    "inflows": "Tornado-pool inflows",
    "provenance": "Provenance and hashes",
    "raw": "Raw data",
}


def _sections_html(uid: str, sections: dict) -> str:
    """Sticky table of contents plus the sections as <details>, in SECTION_ORDER; the
    first section with a non-zero count is open (Leads, or the first one, when none is).

    ``sections`` maps a key of SECTION_ORDER to ``(count, body)`` or
    ``(count, body, title)``; ``count`` None shows no number.
    """
    keys = [k for k in SECTION_ORDER if k in sections]
    # Open the first section that holds rows: an empty Leads section opened on its own
    # reads as "no candidates" when the candidates are under Context or Chance-level.
    # A section without a count (a classification, a diagram) has content; the method,
    # provenance and raw-data sections never open by themselves.
    filled = [
        k
        for k in keys
        if k not in ("method", "provenance", "raw")
        and (sections[k][0] is None or (isinstance(sections[k][0], int) and sections[k][0] > 0))
    ]
    if filled:
        first = filled[0]
    elif "leads" in keys:
        first = "leads"
    else:
        first = keys[0] if keys else None
    toc, out = [], []
    for key in keys:
        entry = sections[key]
        count, body = entry[0], entry[1]
        title = entry[2] if len(entry) > 2 else SECTION_TITLES[key]
        sid = f"{uid}-{key}"
        num = "" if count is None else f'<span class="n">{_e(count)}</span>'
        toc.append(f'<a href="#{sid}">{_e(title)}{num}</a>')
        cnt = "" if count is None else f'<span class="cnt">{_e(count)}</span>'
        out.append(
            f'<details class="sec" id="{sid}" data-sec="{key}"{" open" if key == first else ""}>'
            f"<summary><h2>{_e(title)}</h2>{cnt}</summary>"
            f'<div class="sec-body">{body}</div></details>'
        )
        if key == "raw":
            out.append(
                '<p class="printonly">Raw data (the deposit list) is in the JSON export; '
                "provenance and transaction hashes are above.</p>"
            )
    nav = f'<nav class="toc" aria-label="Sections">{"".join(toc)}</nav>' if toc else ""
    return nav + "".join(out)


def _filter_bar(uid: str) -> str:
    """Filter controls for the candidate tables of one report; filled in by REPORT_JS
    (hidden without JS, where every row stays visible)."""
    return f'<div class="fbar noprint" data-scope="{_e(uid)}" hidden>Filter</div>'


def failed_count(result: dict) -> int:
    """Lookups that failed plus lists the explorer cut off (notices not counted)."""
    errors = result.get("errors") or []
    failed = sum(1 for e in errors if e.get("kind", "lookup_failed") == "lookup_failed")
    cut = [t for t in result.get("truncated") or [] if t.get("reason") not in NOTICE_REASONS]
    return failed + len(cut)


def _n(count: int, noun: str) -> str:
    """ "1 lead", "2 leads"."""
    return f"{count} {noun if count == 1 else noun + 's'}"


def failed_parts(result: dict) -> tuple[int, int]:
    """``(failed lookups, lists cut off by the explorer)``; notices are not counted."""
    errors = result.get("errors") or []
    failed = sum(1 for e in errors if e.get("kind", "lookup_failed") == "lookup_failed")
    cut = [t for t in result.get("truncated") or [] if t.get("reason") not in NOTICE_REASONS]
    return failed, len(cut)


def incomplete_phrase(result: dict) -> str:
    """E.g. "3 lookups failed and 1 list cut off by the explorer" (proper plurals), or ""."""
    failed, cut = failed_parts(result)
    parts = []
    if failed:
        parts.append(f"{failed} {'lookup' if failed == 1 else 'lookups'} failed")
    if cut:
        parts.append(f"{cut} {'list' if cut == 1 else 'lists'} cut off by the explorer")
    return " and ".join(parts)


def format_totals_human(totals: dict) -> str:
    """{'ETH': 2400.0, 'DAI': 990.5} as '990.5 DAI; 2,400 ETH' (display only)."""
    out = []
    for asset, v in sorted(totals.items()):
        text = f"{v:,.8f}".rstrip("0").rstrip(".") or "0"
        out.append(f"{text} {asset}")
    return "; ".join(out)


def _page(title: str, body: str) -> str:
    """A complete self-contained document: inline CSS, the body, the inline script."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{REPORT_CSS}</style></head><body>"
        f"{body}<script>{REPORT_JS}</script></body></html>"
    )


_REPORT_CSS = REPORT_CSS  # older name


def _hop_total(hop: dict, network: Network) -> str:
    if hop.get("kind") == "call":
        return "contract call, no " + _e(network.currency)
    return "{} {}".format(_e(hop["total_value"]), _e(network.currency))


def _assumptions_html(assumptions: dict) -> str:
    rows = "".join(
        f"<tr><td>{_e(label)}</td><td>{_e(value)}</td></tr>"
        for label, value in assumptions["params"]
    )
    parts = [
        '<h2>Analysis parameters<span class="rule"></span></h2>',
        '<div class="meta">Every candidate below depends on these assumptions. A re-run '
        "with other values can give other candidates.</div>",
        f'<table><colgroup><col style="width:22%"><col></colgroup>{rows}</table>',
    ]
    if assumptions["windows"]:
        wrows = "".join(
            f'<tr><td class="num">{_e(w["pool_key"])}</td><td class="num">{_e(w["blocks"])}</td>'
            f'<td class="num">{_e(w["dates"])}</td><td class="num">{w["vouchers"]}</td>'
            f'<td class="num">{w["withdrawals"]} / {w["recipients"]}</td></tr>'
            for w in assumptions["windows"]
        )
        parts.append(
            '<table><colgroup><col style="width:14%"><col style="width:24%"><col>'
            '<col style="width:10%"><col style="width:18%"></colgroup>'
            "<tr><th>Pool</th><th>Blocks read</th><th>Window (UTC)</th><th>Vouchers</th>"
            f"<th>Withdrawals / recipients</th></tr>{wrows}</table>"
        )
    return "".join(parts)


def _evidence_html(evidence: list[dict]) -> str:
    items = "".join(
        f'<li class="{"ev-yes" if e["holds"] else "ev-no"}">'
        f"<b>{'&#10003;' if e['holds'] else '&#8211;'} {_e(e['label'])}</b> "
        f"{_e(e['detail'])}</li>"
        for e in evidence
    )
    return f'<ul class="evidence">{items}</ul>'


def _early_html(early: bool | None) -> str:
    """A linked exit withdrawn soon after the deposit; context, not a signal."""
    if not early:
        return ""
    return (
        '<div class="rat">Early exit: the linked address withdrew within 72 h of the deposit; '
        "in the placebo test such leads almost never appeared in decoy windows (not scored).</div>"
    )


def _fresh_html(fresh: dict | None) -> str:
    """The history an exit had before its first withdrawal; context, not a signal."""
    if not fresh:
        return ""
    if fresh["fresh"]:
        text = (
            f"Fresh address: first activity {fresh['history_hours']:.0f} h before its first "
            "withdrawal, typical of a disposable laundering exit (not scored)"
        )
    else:
        text = (
            f"Address active {fresh['history_hours'] / 24:.0f} day(s) before its first "
            "withdrawal (not scored)"
        )
    return f'<div class="rat">{_e(text)}.</div>'


def _incomplete_html(result: dict) -> str:
    """The "N lookups failed — results may be incomplete" box, near the top ("" if none)."""
    text = _incomplete_summary(result)
    if not text:
        return ""
    items = "".join(f'<li class="mono">{_e(line)}</li>' for line in _incomplete_lines(result))
    if _notice_only(result):
        return (
            f'<div class="callout"><b>{_e(text)}</b>. Nothing is missing: the lists are '
            "complete, but a node log has no gas price, so the unique-gas-price signal "
            f"is off for them.<ul>{items}</ul></div>"
        )
    return (
        f'<div class="callout"><b>{_e(text)}</b>. A lookup that failed is not '
        '"nothing found": what it would have shown is missing from this report. '
        f"Re-run to retry.<ul>{items}</ul></div>"
    )


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def _groups_html(groups: list[dict], network: Network) -> str:
    """Exit groups (see groups.py): anchored ones in full, the rest as a count."""
    if not groups:
        return ""
    anchored = [g for g in groups if g["anchored"]]
    parts = [
        '<h2>Exit groups<span class="rule"></span></h2>',
        '<div class="meta">Recipients withdrawn together in repeated bursts (two or more '
        "withdrawals within ten minutes of each other), as an operator paying out pooled "
        "notes does. A group is tied to this depositor only through an anchor: a "
        "corroborated candidate or a known exit. Context for the investigation, not "
        f"scored. {len(groups) - len(anchored)} group(s) without an anchor are not listed.</div>",
    ]
    for g in anchored:
        anchors = ", ".join(f"{_short(a['address'])} ({a['why']})" for a in g["anchors"])
        members = "".join(f'<li class="mono">{_addr_link(network, a)}</li>' for a in g["members"])
        parts.append(
            '<div class="lead-card">'
            f'<div class="top"><span class="pill">{_e(g["pool_key"])}</span>'
            f"<b>{len(g['members'])} addresses · {g['notes']} notes</b>"
            f'<span class="conf pct">{_e(_ts(g["first_ts"]))} – {_e(_ts(g["last_ts"]))}</span></div>'
            f'<div class="why">Anchor: {_e(anchors)}</div>'
            f'<ul class="evidence">{members}</ul></div>'
        )
    return "".join(parts)


def _kind_cell(d: dict) -> str:
    """The kind of a deposit address as a chip that wraps inside its cell when it
    carries a long value (an operator address), instead of running into the next one."""
    label = kind_label(d)
    cls = "pill long" if len(label) > 28 else "pill"
    return f'<span class="{cls}">{_e(label)}</span>'


def _deposit_addresses_html(data: dict, network: Network) -> str:
    """The depositor's exchange deposit addresses (see deposit_addresses.py); only when found."""
    rows = data.get("deposit_addresses") or []
    if not rows:
        return ""
    hits = {}
    for pool_key, addr, addrs in data.get("heuristics", {}).get("shared_deposits", []):
        for dep in addrs:
            hits.setdefault(dep, []).append((pool_key, addr))
    parts = [
        '<h2>Deposit addresses<span class="rule"></span></h2>',
        '<div class="meta">Addresses the depositor sent funds to that behave like a per-customer '
        "exchange deposit address: few senders, quiet, and what arrives is swept to a hot "
        "wallet. A recipient of the searched withdrawals that sent funds to the same address "
        "shares a deposit address with the depositor (the address-reuse idea of Tutela, through "
        "an intermediary). Context only since 2.21: in the second hold-out this did not beat "
        "chance, so it never raises the band. An exchange address can be shared by mistake or "
        "by a service. A <i>service deposit</i> sweeps to a labelled non-exchange wallet and a "
        "<i>forwarder</i> moves tokens through an operator; neither is ever linked to a "
        "candidate.</div>",
        '<table><colgroup><col style="width:30%"><col style="width:18%"><col style="width:22%">'
        '<col style="width:8%"><col></colgroup>'
        "<tr><th>Deposit address</th><th>Kind</th><th>Swept to</th><th>Senders</th>"
        "<th>Also used by a candidate</th></tr>",
    ]
    for d in rows:
        who = "<br>".join(
            f'<span class="mono wrap">{_addr_link(network, addr)}</span> '
            f'<span class="pill">{_e(pool_key)}</span>'
            for pool_key, addr in hits.get(d["address"], [])
        )
        if not who and not d.get("evidence", True):
            who = "<br>".join(
                f'<span class="mono wrap">{_addr_link(network, addr)}</span> (context)'
                for addr, addrs in data.get("deposit_context_hits", {}).items()
                if d["address"] in addrs
            )
        parts.append(
            f'<tr><td class="mono wrap">{_addr_link(network, d["address"])}</td>'
            f"<td>{_kind_cell(d)}</td>"
            f"<td>{_e(target_label(d))}</td>"
            f'<td class="num">{len(d.get("senders", []))}</td>'
            f"<td>{who or '-'}</td></tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def _root_check_html(data: dict, network: Network) -> str:
    """Candidates removed by the Merkle-root check (root_exclusion.py); only when it ran."""
    rc = data.get("root_check")
    if not rc:
        return ""
    parts = [
        '<h2>Excluded by Merkle root<span class="rule"></span></h2>',
        '<div class="meta">Exploratory. A withdrawal proof names the Merkle root it was built '
        "against; a root that held none of the depositor's notes in the pool cannot belong to "
        "this depositor. Only the withdrawals of ranked candidates were read. A candidate whose "
        "every withdrawal in a pool is excluded is removed from the ranking and listed here; a "
        "partly excluded one stays with a lower count and a context note, or, if the recount "
        "costs it its count match, is listed as dropped after recount. This is not a lead "
        f"and is not scored. Read: {rc['withdrawals_checked']} withdrawal(s), "
        f"roots found {rc['roots_found']}, not found {rc['roots_not_found']} "
        f"(outside the pool's root history {rc.get('roots_outside_history', 0)}, "
        f"ambiguous {rc.get('roots_ambiguous', 0)}), "
        f"{rc['tx_info_calls']} transaction lookup(s). A root counts only when the pool "
        "still accepted it at the withdrawal block (within its ROOT_HISTORY_SIZE); a "
        "calldata with several such candidate roots leaves the root unknown.</div>",
    ]
    sizes = rc.get("history_sizes") or {}
    if sizes:
        parts.append(
            '<div class="meta">ROOT_HISTORY_SIZE: '
            + _e(", ".join(f"{k} {v}" for k, v in sorted(sizes.items())))
            + ".</div>"
        )
    if rc.get("incomplete"):
        parts.append(
            '<div class="meta flag">The check is incomplete (call limit reached or a pool could '
            "not be checked"
            + (f": {_e(', '.join(rc['unchecked_pools']))}" if rc.get("unchecked_pools") else "")
            + "): some candidates were not checked.</div>"
        )
    dropped = rc.get("dropped_after_recount") or []
    if not rc["excluded_candidates"]:
        parts.append('<div class="meta">No candidate was excluded in full.</div>')
    else:
        parts.append(_root_table(rc["excluded_candidates"], network))
    if dropped:
        parts.append(
            '<h3>Dropped after recount</h3><div class="meta">Partly excluded candidates that '
            "no longer qualify once the excluded withdrawals are not counted (the count match "
            "was lost).</div>" + _root_table(dropped, network)
        )
    return "".join(parts)


def _root_table(rows: list[dict], network: Network) -> str:
    parts = [
        '<table><colgroup><col style="width:10%"><col style="width:38%"><col></colgroup>'
        "<tr><th>Pool</th><th>Candidate</th><th>Withdrawal (root leaves)</th></tr>"
    ]
    for c in rows:
        txs = "<br>".join(
            f'<span class="mono wrap">{_tx_link(network, w["tx_hash"])}</span> '
            f"(root holds {w['n_root']} leaves)"
            for w in c["withdrawals"]
        )
        parts.append(
            f'<tr><td class="num">{_e(c["pool_key"])}</td>'
            f'<td class="mono wrap">{_addr_link(network, c["address"])}</td><td>{txs}</td></tr>'
        )
    parts.append("</table>")
    return "".join(parts)


def _placebo_html(pb: dict | None, network: Network) -> str:
    """Placebo check (see placebo.py): leads per band in the real and the decoy window."""
    if not pb:
        return ""

    def rate(v):
        return "n/a" if v is None else f"{v:g}"

    window = pb.get("decoy_window")
    span = f" ({_e(_ts(window[0]))} – {_e(_ts(window[1]))})" if window else ""
    rows = "".join(
        "<tr><td>{}</td>".format(_band_badge(b))
        + f'<td class="num">{pb["target"][b]}</td><td class="num">{pb["decoy"][b]}</td>'
        + f'<td class="num">{rate(pb["target_per_1000"][b])} / {rate(pb["decoy_per_1000"][b])}</td>'
        + f"<td>{_e(pb['verdicts'][b])}</td></tr>"
        for b in BANDS
    )
    parts = [
        '<h2>Placebo check<span class="rule"></span></h2>',
        '<div class="meta">A decoy window is a search window that ends a day before the '
        "first real deposit, so it cannot contain this wallet's notes: any lead found there "
        "is chance. The same pipeline ran on it, "
        f"{pb['offset_days']:g} days earlier{span}. "
        f"Withdrawals searched: {pb['target_withdrawals']} real, {pb['decoy_withdrawals']} decoy.</div>",
        '<table><colgroup><col style="width:12%"><col style="width:11%"><col style="width:11%">'
        '<col style="width:18%"><col></colgroup>'
        "<tr><th>Band</th><th>Real window</th><th>Decoy window</th>"
        "<th>Per 1000 withdrawals (real / decoy)</th><th>Verdict</th></tr>" + rows + "</table>",
        f'<div class="callout">{_e(pb["note"])}</div>',
    ]
    if pb.get("direct_link_verdict"):
        parts.append(
            f'<div class="callout"><b>Direct links:</b> {_e(pb["direct_link_verdict"])}</div>'
        )
    for caveat in pb.get("caveats", []):
        parts.append(f'<div class="callout">{_e(caveat)}</div>')
    for lead in pb.get("decoy_leads", []):
        parts.append(
            '<div class="lead-card">'
            f'<div class="top">{_band_badge(lead["band"])}'
            f'<span class="addr wrap">{_addr_link(network, lead["address"])}</span>'
            f'<span class="pill">{_e(lead["pool_key"])}</span></div>'
            f'<div class="why">Decoy lead (window before the deposit; can contain earlier activity of the depositor). Signals: '
            f"{_e(', '.join(lead['signals']))}</div></div>"
        )
    return "".join(parts)


CANDIDATE_ROW_CAP = 200  # candidate rows per table in the HTML; the CSV and JSON list all


def _plain_addr(addr) -> str:
    """A full address shown without a link (copyable through REPORT_JS)."""
    return f'<span class="ad">{_e(addr)}</span>'


def _signal_status(signal: str) -> str:
    return "lead" if signal in LEAD_SIGNALS else "context"


def _checks_html(evidence: list[dict]) -> str:
    """Every evidence check of a candidate with its family and whether it is a lead. A
    check that holds points to the "Why" line above, which carries its evidence."""
    items = []
    for e in evidence:
        status = _signal_status(e["signal"])
        tag = "lead signal" if status == "lead" else "context, never raises the band"
        detail = "fired: the evidence is in the Why line above" if e["holds"] else e["detail"]
        items.append(
            f'<li class="{"ev-yes" if e["holds"] else "ev-no"}">'
            f"<b>{'&#10003;' if e['holds'] else '&#8211;'} {_e(e['label'])}</b> "
            f"{_e(detail)} "
            f'<span class="fam">({_e(e["family"])}; {tag})</span></li>'
        )
    return f'<ul class="evidence">{"".join(items)}</ul>'


_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}|0x[0-9a-fA-F]{40}")


def _linked_text(text: str, network: Network) -> str:
    """Escaped ``text`` with every full transaction hash and address as an explorer link
    (the value shown in full, never shortened)."""
    out, pos = [], 0
    for m in _HASH_RE.finditer(text):
        out.append(_e(text[pos : m.start()]))
        value = m.group(0)
        out.append(_tx_link(network, value) if len(value) == 66 else _addr_link(network, value))
        pos = m.end()
    out.append(_e(text[pos:]))
    return "".join(out)


def _gwei(wei) -> str:
    return f"{wei / 1e9:.3f}" if wei else ""


def deposits_table_html(data: dict, network: Network, uid: str = "r") -> str:
    """The depositor's Tornado deposits, listed once near the top of the report and
    numbered D1, D2, ... (:func:`numbered_deposits`); the evidence of every candidate
    refers to these numbers instead of repeating the deposits."""
    nums = numbered_deposits(data)
    if not nums:
        return ""
    vno = {}
    for i, v in enumerate(data.get("vouchers") or [], 1):
        for n in voucher_deposit_numbers(data, v):
            vno[n] = f"V{i} ({_n(v['count'], 'note')})"
    rows = "".join(
        f'<tr><td class="num">D{n}</td>'
        f'<td class="num">{_e(_ts(d["ts"])) if d.get("ts") is not None else "time unknown"}</td>'
        f'<td class="num">{_e(d.get("pool_key", ""))}</td><td class="num">{_e(vno.get(n, ""))}</td>'
        f'<td class="num">{_gwei(d.get("gas_price"))}</td>'
        f"<td>{_tx_link(network, d['hash']) if d.get('hash') else 'hash not recorded'}</td></tr>"
        for n, d in nums
    )
    return (
        f'<section class="deps" id="{_e(uid)}-deps" aria-label="Deposits">'
        f'<h2>Tornado.Cash deposits<span class="rule"></span></h2>'
        f'<div class="meta">{_n(len(nums), "deposit")} by the depositor, oldest first. The '
        "evidence of each candidate refers to them by number (D1, D2, ...).</div>"
        '<table class="dtab"><colgroup><col style="width:5%"><col style="width:19%">'
        '<col style="width:8%"><col style="width:12%"><col style="width:7%"><col></colgroup>'
        "<thead><tr><th>#</th><th>Time (UTC)</th><th>Pool</th><th>Voucher</th>"
        f"<th>Gas (gwei)</th><th>Transaction</th></tr></thead><tbody>{rows}</tbody></table>"
        "</section>"
    )


def _timeline_html(data: dict, r: dict, network: Network) -> str:
    """Evidence timeline of one candidate: its own withdrawals and the dated facts behind
    the signals that fired for it (deposits by number), each withdrawal tagged with the
    fired signals it feeds; then every check and why it is a lead or context."""
    pool, addr = r["pool_key"], r["address"]
    h = data.get("heuristics", {})
    signals = set(r["signals"])
    wallet = (data.get("wallet") or "").lower()
    gas = {tx: gp for pk, a, gp, tx in h.get("gas_price_matches", []) if pk == pool and a == addr}
    senders = {
        tx: sender for pk, a, sender, tx in h.get("linked_senders", []) if pk == pool and a == addr
    }
    dep_by_gas = {}
    for n, d in numbered_deposits(data):
        if d.get("gas_price"):
            dep_by_gas.setdefault(d["gas_price"], n)
    res = data["denoms"][pool]
    asset = res.get("asset") or network.currency
    recs = sorted(res.get("detail", {}).get(addr, []), key=lambda x: x.get("ts") or 0)
    first_hash = recs[0].get("hash") if recs else None
    # Depositor-side senders are written out once (S1 = full address) and then by label.
    label = {}
    for sender in senders.values():
        if sender != wallet and sender not in label:
            label[sender] = f"S{len(label) + 1}"
    timed = []
    wrows = []  # (ts, [value, relaying], [other facts], tx, feeds) per withdrawal
    for rec in recs:
        head = [f"{_e(rec.get('value', ''))} {_e(asset)}"]
        if rec.get("self_relayed"):
            head.append("self-relayed (no relayer)")
        elif (rec.get("relayer") or "").lower() == addr:
            head.append(f"relayer field is this address, fee {_e(rec.get('fee', 0))}")
        else:
            head.append("through a relayer")
        bits = []
        if rec.get("gas_price"):
            bits.append(f"gas {rec['gas_price'] / 1e9:.3f} gwei")
        feeds = []
        if "count_match" in signals:
            feeds.append("amount+timing (count match)")
        if "self_relayed" in signals and rec.get("self_relayed"):
            feeds.append("self-relay (same family as amount+timing)")
        if rec.get("hash") in gas:
            num = dep_by_gas.get(gas[rec["hash"]])
            feeds.append(
                f"gas price equals deposit D{num}'s" if num else "gas price equals a deposit's"
            )
        if rec.get("hash") == first_hash:
            if "linked" in signals:
                feeds.append("linked address (lead: first withdrawal within 72 h)")
            elif "linked_late" in signals:
                feeds.append("late direct link (context: first withdrawal after 72 h)")
        sender = senders.get(rec.get("hash"))
        if sender:
            # its feed (linked withdrawal sender) is said once, above the list
            bits.append(f"sent by {'the depositor' if sender == wallet else label[sender]}")
        tx = _tx_link(network, rec["hash"]) if rec.get("hash") else "hash not recorded"
        wrows.append((rec.get("ts") or 0, head, bits, tx, "; ".join(feeds)))
    # Facts shared by every withdrawal are said once above the list, not on each line.
    shared_head = len(wrows) > 1 and all(w[1] == wrows[0][1] for w in wrows)
    # (a single withdrawal still gets the note on sent-by marks below)
    shared_feeds = len(wrows) > 1 and all(w[4] == wrows[0][4] for w in wrows)
    common = []
    if shared_head:
        common.append(f"each {', '.join(wrows[0][1])}")
    if shared_feeds and wrows[0][4]:
        common.append(f"each feeds: {_e(wrows[0][4])}")
    if senders:
        common.append(
            "each one marked “sent by” feeds: linked withdrawal sender (context, not scored)"
        )
    for ts, head, bits, tx, feeds in wrows:
        facts = ([] if shared_head else head) + bits
        timed.append(
            (
                ts,
                1,
                f"Withdrawal{', ' if facts else ''}{', '.join(facts)}, tx {tx}",
                "" if shared_feeds else feeds,
            )
        )
    # The deposits a fired signal is measured against, by number (listed once at the top).
    if "count_match" in signals:
        for v in data.get("vouchers", []):
            if v["pool_key"] == pool and v["count"] == r["hits"]:
                nums = voucher_deposit_numbers(data, v)
                where = f" (deposits {_numbers_text(nums)})" if nums else ""
                timed.append(
                    (
                        v.get("last_ts", v["first_ts"]),
                        0,
                        f"Last deposit of the {_e(v['count'])}-note voucher{_e(where)}: the "
                        "withdrawals are counted against this voucher",
                        "amount+timing (count match)",
                    )
                )
    if signals & {"linked", "linked_late"} and recs and recs[0].get("ts"):
        last, num = _last_deposit_before(data, pool, recs[0]["ts"])
        if last is not None and "count_match" not in signals:
            timed.append(
                (
                    last,
                    0,
                    f"Last deposit before the first withdrawal{f' (D{num})' if num else ''}: "
                    "the delay of the direct link is measured from here",
                    "linked address" if "linked" in signals else "late direct link (context)",
                )
            )
    links = data.get("counterparty_links") or {}
    cited = []
    if signals & {"linked", "linked_late"} and addr != wallet:
        cited.append(
            (
                addr,
                "this address",
                "linked address" if "linked" in signals else "late direct link (context)",
            )
        )
    for sender, lab in label.items():
        cited.append(
            (
                sender,
                f"{lab} = {_plain_addr(sender)}",
                "linked withdrawal sender (context, not scored)",
            )
        )
    for who_addr, who, feeds in cited:
        link = links.get(who_addr)
        if link and link.get("tx"):
            timed.append(
                (
                    link.get("ts") or 0,
                    0,
                    f"{who[0].upper() + who[1:]}, a direct counterparty of the depositor: first "
                    f"transaction between the two, tx {_tx_link(network, link['tx'])}",
                    feeds,
                )
            )
        else:
            timed.append(
                (
                    0,
                    0,
                    f"{who[0].upper() + who[1:]} is a direct counterparty of the depositor; the "
                    "linking transaction was not recorded in this run",
                    feeds,
                )
            )
    timed.sort(key=lambda x: (x[0], x[1]))
    items = [
        f'<li><span class="num">{_e(_ts(ts)) if ts else "time unknown"}</span> {text}'
        + (f' <span class="fam">feeds: {_e(feeds)}</span>' if feeds else "")
        + "</li>"
        for ts, _o, text, feeds in timed
    ]
    for pk, a, addrs in h.get("shared_deposits", []):
        if pk == pool and a == addr:
            for dep in addrs:
                items.append(
                    f"<li>Sent funds to deposit address {_addr_link(network, dep)}, which the "
                    'depositor also used <span class="fam ctx-s">feeds: shared exchange '
                    "deposit address (context, not scored)</span></li>"
                )
    if "early_profile" in signals:
        items.append(
            "<li>Received the wallet's full multi-pool profile early "
            '<span class="fam lead-s">feeds: early multi-pool profile (lead)</span></li>'
        )
    intro = (
        f'<div class="meta">{_n(len(wrows), "withdrawal")} to this address: '
        f"{'; '.join(common)}.</div>"
        if common
        else ""
    )
    return (
        f'{intro}<ol class="timeline">{"".join(items)}</ol>'
        f"<h3>What was checked</h3>{_checks_html(r['evidence'])}"
        f"{_fresh_html(r.get('fresh'))}{_early_html(r.get('early_exit'))}"
        f'<div class="rat">Band: {_e(r["band"])} — '
        f"{_e(band_rationale(r['band'], set(r['signals'])))}. Discrimination "
        f"{r['discrimination']:.2f} in a field of {r['field_size']} recipients.</div>"
    )


def _ties(cands: list[dict]) -> Counter:
    return Counter((c["band"], f"{c['confidence']:.2f}") for c in cands)


def _families_html(signals) -> str:
    """Evidence families as pills, then every context or note signal that fired, marked
    as context (it is no family and never scored)."""
    pills = [f'<span class="pill">{_e(f)}</span>' for f in evidence_families(signals)]
    pills += [
        f'<span class="pill ctx">{_e(s)} (context)</span>'
        for s in sorted(signals)
        if s in CONTEXT_SIGNALS | NOTE_SIGNALS
    ]
    return "".join(pills) or "—"


def _cand_table(
    data: dict,
    rows: list[dict],
    network: Network,
    scope: str,
    ties: Counter,
    net_name: str,
    ranks: dict | None = None,
) -> str:
    """The candidate table with its own filter bar: one <tbody> per candidate (summary
    row, then a full-width row with the "Why" line and the evidence timeline), carrying
    data-* attributes for the filters.

    ``scope`` ties the filter bar to this table only; ``ranks`` maps (pool, address) to
    the candidate's rank over the whole ranking (the "#" column)."""
    if not rows:
        return ""
    ranks = ranks or {}
    show_attr = any(r.get("attribution") for r in rows)
    head = (
        _filter_bar(scope)
        + f'<table class="cands ft" data-scope="{_e(scope)}"><colgroup><col style="width:4%">'
        '<col style="width:8%"><col style="width:6%">'
        + (
            '<col style="width:22%"><col style="width:10%">'
            if show_attr
            else '<col style="width:28%">'
        )
        + '<col style="width:7%"><col style="width:15%"><col style="width:13%"><col>'
        '<col style="width:7%"></colgroup>'
        "<thead><tr><th>#</th><th>Band</th><th>Score</th><th>Address</th>"
        + ("<th>Attribution</th>" if show_attr else "")
        + "<th>Pool</th><th>Families</th>"
        "<th>First / last withdrawal (UTC)</th><th>Notes</th><th>Evidence</th></tr></thead>"
    )
    ncols = 10 if show_attr else 9
    out = [head]
    for r in rows[:CANDIDATE_ROW_CAP]:
        fams = evidence_families(r["signals"])
        ctx = [s for s in r["signals"] if s in CONTEXT_SIGNALS | NOTE_SIGNALS]
        holds = sum(1 for e in r["evidence"] if e["holds"])
        score = f"{r['confidence']:.2f}"
        notes = []
        if ties[(r["band"], score)] > 1:
            notes.append(
                f'<span class="pill" title="Order within a tie: more withdrawals first, then '
                f'address; the order carries no meaning">tie at {score}</span>'
            )
        if (r.get("fresh") or {}).get("fresh"):
            notes.append('<span class="pill">fresh address</span>')
        if r.get("early_exit"):
            notes.append('<span class="pill">early exit</span>')
        pct = int(r["confidence"] * 100)
        lead_cls = ' class="lead"' if r["band"] in ("strong", "moderate") else ""
        n_w = _n(r["hits"], "withdrawal")
        out.append(
            f'<tbody class="cand" data-f="1" data-band="{_e(r["band"])}" '
            f'data-fam="{_e("|".join(fams + ["context"] if ctx else fams) or "none")}" '
            f'data-pool="{_e(r["pool_key"])}" data-net="{_e(net_name)}">'
            f'<tr{lead_cls}><td class="num">{ranks.get((r["pool_key"], r["address"]), "")}</td>'
            f"<td>{_band_badge(r['band'])}</td>"
            f'<td class="conf"><span class="pct num">{score}</span>'
            f'<span class="cbar" style="--w:{pct}%"></span></td>'
            f"<td>{_addr_link(network, r['address'])}</td>"
            + (f'<td class="flag">{_e(r.get("attribution") or "")}</td>' if show_attr else "")
            + f'<td class="num">{_e(r["pool_key"])}</td>'
            f"<td>{_families_html(r['signals'])}</td>"
            f'<td class="num">{_e(_ts(r["first_ts"]))}<br>{_e(_ts(r["last_ts"]))}</td>'
            f'<td>{"".join(notes) or "—"}<div class="meta">{n_w}</div></td>'
            f'<td class="num">{holds} of {len(r["evidence"])}</td></tr>'
            f'<tr class="det"><td colspan="{ncols}">'
            f'<div class="why"><b>Why:</b> {_linked_text(candidate_reason(r), network)}</div>'
            f'<details class="tl"><summary>Evidence timeline and checks ({n_w}, '
            f"{holds} of {len(r['evidence'])} checks hold)</summary>"
            f"{_timeline_html(data, r, network)}</details></td></tr></tbody>"
        )
    out.append("</table>")
    if len(rows) > CANDIDATE_ROW_CAP:
        out.append(
            f'<div class="meta">{len(rows) - CANDIDATE_ROW_CAP} more in this group: every '
            "candidate is in the CSV and the JSON.</div>"
        )
    return "".join(out)


def _fees_html(data: dict, network: Network, leads: list[dict]) -> str:
    """Relayers, self-relayed leads and shared fee fingerprints (events mode only)."""
    p = []
    stats = analyze_relayers(collect_withdrawals(data))
    p.append(
        f'<div class="meta">Relayers: {stats["unique_relayers"]} relayer(s), '
        f"{len(stats['self_relayed'])} self-relayed withdrawal(s) in window.</div>"
    )
    if leads:
        p.append(
            "<h3>Self-relayed leads (no relayer named in the proof)</h3>"
            '<div class="meta">Nobody was paid to broadcast these, so the sender '
            "paid gas from an address they already controlled. Who that sender is "
            "is read from the transaction where the data provider allows; "
            "<b>unverified</b> means it was not checked, not that it was "
            "confirmed.</div>"
            '<table><colgroup><col style="width:24%"><col style="width:7%">'
            '<col style="width:12%"><col style="width:17%"><col style="width:14%"><col></colgroup>'
            "<tr><th>Recipient</th><th>Pool</th><th>Time (UTC)</th>"
            "<th>Broadcast by</th><th>Fee settings</th><th>Withdrawal tx</th></tr>"
        )
        profiles = data.get("fee_profiles", {})
        for lead in leads[:40]:
            status = lead.get("broadcaster_status", "unverified")
            if status == "self":
                who = '<span class="flag">the recipient</span>'
            elif status == "other":
                who = _addr_link(network, lead.get("broadcaster"))
            else:
                who = '<span class="meta">unverified</span>'
            p.append(
                f'<tr class="lead"><td>{_addr_link(network, lead["to"])}</td>'
                f'<td class="num">{_e(lead["pool_key"])}</td><td class="num">{_ts(lead["ts"])}</td>'
                f'<td class="mono wrap">{who}</td>'
                f'<td class="mono wrap">{_e(fee_label(fee_key(profiles.get(lead["tx_hash"]))))}</td>'
                f"<td>{_tx_link(network, lead['tx_hash'])}</td></tr>"
            )
        p.append("</table>")
    shared = [g for g in data.get("fee_fingerprints", []) if g.get("shared")]
    if shared:
        p.append(
            "<h3>Shared fee fingerprints</h3>"
            '<div class="meta">Self-relayed withdrawals to different recipients sent '
            "with identical fee settings (priority fee and gas-limit range). This "
            "is <b>context, not scored</b>: many users share wallet defaults. The last "
            "column says how common the fingerprint is among the self-relayed "
            "withdrawals of the same pools in the window.</div>"
            "<table><tr><th>Fee settings</th><th>Withdrawals</th><th>Recipients</th>"
            "<th>How common</th></tr>"
        )
        for g in shared:
            recips = "<br>".join(_addr_link(network, r) for r in g["recipients"])
            p.append(
                f'<tr><td class="mono">{_e(g["label"])}</td>'
                f'<td class="num">{g["withdrawals"]}</td>'
                f"<td>{recips}</td>"
                f"<td>{_e(frequency_text(g))}</td></tr>"
            )
        p.append("</table>")
    elif not leads:
        p.append('<div class="meta">No self-relayed lead and no shared fee fingerprint.</div>')
    return "".join(p)


def _method_demix_html(data: dict, network: Network) -> str:
    """Parameters, unresolved pools, per-method results, heuristic hits, Merkle-root
    check, placebo check, how to read and limitations."""
    p = [_assumptions_html(analysis_assumptions(data))]
    unresolved = data.get("unresolved") or []
    if unresolved:
        rows = "".join(
            '<tr><td class="num">{}</td><td>{}</td></tr>'.format(
                _e(u["pool_key"]), _e(u.get("reason", ""))
            )
            for u in unresolved
        )
        p.append('<h2>Unresolved pools (not searched)<span class="rule"></span></h2>')
        p.append(
            '<div class="callout">{} pool(s) below had a detected deposit but '
            "could not be searched: a block lookup failed, so no withdrawal "
            'window could be resolved. These pools are <b>not</b> "no exits '
            'found" &mdash; they were not searched at all. Re-run to retry.'
            "</div>".format(len(unresolved))
        )
        p.append(
            '<table><colgroup><col style="width:22%"><col></colgroup>'
            "<tr><th>Pool</th><th>Reason</th></tr>{}</table>".format(rows)
        )
    xm = cross_method(data)
    p.append('<h2>Cross-method matches<span class="rule"></span></h2>')
    if xm:
        p.append(
            '<div class="meta">Addresses selected by <b>two or more independent '
            "families of evidence</b> at once — amount+timing, gas price, and a direct "
            "counterparty relationship. Independence is what makes agreement meaningful, "
            "so a self-relayed withdrawal does not count separately from the count match "
            "it was found on: both read the same withdrawals. Still leads, not proof.</div>"
            '<table><colgroup><col style="width:34%"><col style="width:8%">'
            '<col style="width:9%"><col></colgroup>'
            "<tr><th>Recipient</th><th>Pool</th><th>Methods</th><th>Why (per method)</th></tr>"
        )
        for r in xm[:40]:
            methods = "".join(f'<span class="pill">{_e(m)}</span>' for m in r["methods"])
            reasons = "; ".join(_e(x) for x in r["reasons"])
            p.append(
                f"<tr><td>{_addr_link(network, r['address'])}</td>"
                f'<td class="num">{_e(r["pool_key"])}</td>'
                f'<td class="num">{r["n_methods"]}x</td>'
                f'<td>{methods}<div class="meta">{reasons}</div></td></tr>'
            )
        p.append("</table>")
    else:
        p.append(
            '<div class="meta">No address was selected by more than one method. '
            "Every candidate rests on a single method, so treat them as weaker.</div>"
        )
    p.append('<h2>Results by method<span class="rule"></span></h2>')
    p.append(
        '<div class="meta">Every method runs on every wallet. Each block below lists the '
        "addresses that method selected and why.</div>"
    )
    any_method = False
    for m in method_breakdown(data):
        if not m["rows"]:
            continue
        any_method = True
        p.append(f'<h3>{_e(m["label"])} <span class="pill">{len(m["rows"])}</span></h3>')
        p.append(
            '<table><colgroup><col style="width:38%"><col style="width:9%"><col></colgroup>'
            "<tr><th>Address</th><th>Pool</th><th>Why</th></tr>"
        )
        for r in m["rows"][:40]:
            p.append(
                f"<tr><td>{_addr_link(network, r['address'])}</td>"
                f'<td class="num">{_e(r["pool_key"])}</td><td>{_e(r["reason"])}</td></tr>'
            )
        p.append("</table>")
    if not any_method:
        p.append(
            '<div class="meta">No method produced a discriminating selection for this wallet.</div>'
        )
    h = data.get("heuristics", {})
    if h.get("gas_price_matches") or h.get("linked_addresses"):
        p.append('<h2>Heuristic hits<span class="rule"></span></h2>')
        if h.get("gas_price_matches"):
            p.append(
                "<h3>Unique gas-price matches</h3>"
                '<table><colgroup><col style="width:8%"><col style="width:34%">'
                '<col style="width:16%"><col></colgroup>'
                "<tr><th>Pool</th><th>Recipient</th><th>Gas price (gwei)</th><th>Tx</th></tr>"
            )
            for pool_key, addr, gp, txh in h["gas_price_matches"][:40]:
                p.append(
                    f'<tr><td class="num">{_e(pool_key)}</td>'
                    f"<td>{_addr_link(network, addr)}</td>"
                    f'<td class="num">{gp / 1e9:.3f}</td>'
                    f"<td>{_tx_link(network, txh)}</td></tr>"
                )
            p.append("</table>")
        if h.get("linked_addresses"):
            late = {(pk, addr): hours for pk, addr, hours in h.get("linked_late", [])}
            p.append(
                "<h3>Linked addresses (direct counterparties of the depositor)</h3>"
                '<div class="meta">A direct link is a lead only when its first withdrawal came '
                "within 72 h of the last deposit; a later one is chance-level on real "
                "depositors and is shown as context.</div>"
                '<table><colgroup><col style="width:12%"><col style="width:44%"><col></colgroup>'
                "<tr><th>Pool</th><th>Recipient</th><th>Timing</th></tr>"
            )
            for pool_key, addr in h["linked_addresses"][:40]:
                if (pool_key, addr) not in late:
                    timing = "within 72 h: lead"
                elif late[(pool_key, addr)] is None:
                    timing = "not within 72 h of a deposit: context"
                else:
                    timing = f"{late[(pool_key, addr)]:.0f} h after the last deposit: context"
                p.append(
                    f'<tr><td class="num">{_e(pool_key)}</td>'
                    f"<td>{_addr_link(network, addr)}</td>"
                    f"<td>{_e(timing)}</td></tr>"
                )
            p.append("</table>")
    p.append(_root_check_html(data, network))
    p.append(_placebo_html(data.get("placebo"), network))
    p.append(how_to_read_html())
    p.append(limitations_html())
    return "".join(p)


def _placebo_note(pb: dict | None) -> str:
    if not pb:
        return ""
    verdicts = "; ".join(f"{b}: {pb['verdicts'][b]}" for b in BANDS)
    return f'<span class="tag">placebo</span> {_e(verdicts)}'


def _demix_answer(cands: list[dict], kinds: Counter, incomplete: str, net_name: str) -> str:
    if kinds["lead"]:
        top = cands[0]
        text = (
            f"{_n(kinds['lead'], 'lead')} to corroborate; the first is {top['address']} "
            f"({top['pool_key']}, {top['band']}), not proof."
        )
    elif cands:
        text = (
            f"No lead: {'the' if len(cands) == 1 else 'all'} {_n(len(cands), 'candidate')} "
            f"{'is' if len(cands) == 1 else 'are'} weak "
            f"({kinds['context']} with context only, {kinds['chance']} chance-level)."
        )
    else:
        text = "No lead: no count match narrows its field enough to count."
    if incomplete:
        text = text[:-1] + f", and {incomplete}, so results may be incomplete."
    return text


def build_html_report(
    data: dict,
    network: Network,
    attribution: dict[str, dict] | None = None,
    json_sha256: str | None = None,
    fragment: bool = False,
    uid: str = "r",
) -> str:
    """Render a single-wallet demix result as a self-contained HTML string.

    ``network`` supplies the block-explorer URLs and the native currency, so
    a BSC report links to bscscan and a Polygon report to polygonscan.

    ``attribution`` is an optional ``{address_lower: label_dict}`` map (see
    :func:`tornado_demix.attribution.load_attribution`). When it names any ranked
    candidate, the candidate rows carry the label; with no set, or no match, the
    report is unchanged.

    ``json_sha256`` is the SHA-256 of the JSON export written next to this report; the
    provenance section prints it so the two files can be tied together.

    Layout: verdict card, then Leads, Context, Chance-level candidates, Deposit
    addresses, Groups, Fee fingerprints, Method and limitations, Raw data. ``fragment``
    True returns the body only (the web UI embeds it); ``uid`` prefixes the element ids
    so several reports can share one page.
    """
    attribution = attribution or {}
    wallet = data["wallet"]
    params = data.get("params", {})
    prov = data.get("provenance") or _provenance(network, command="not recorded")
    net_name = params.get("network", network.name)
    generated = prov["generated_utc"]

    exit_h = params.get("exit_window_hours")
    cands = ranked_candidates(data, attribution=attribution) if data.get("vouchers") else []
    p = ['<main class="sheet">', demix_page1(data, network, cands, prov, json_sha256, uid)]
    if exit_h:
        p.append(
            '<div class="callout">Narrow exit window: only withdrawals within '
            f"{_e(_fmt_hours(exit_h))} h of a deposit were searched. Fast exits are "
            "surfaced with far less shared-window noise; withdrawals delayed "
            "longer than this are <b>not</b> in scope.</div>"
        )

    if not data.get("vouchers"):
        p.append(_incomplete_html(data))
        p.append(
            '<h2>No Tornado.Cash deposits found<span class="rule"></span></h2>'
            '<div class="meta">This address made no deposits into the configured '
            "Tornado pools on this network.</div>"
        )
        p.append(
            _sections_html(
                uid,
                {
                    "method": (None, how_to_read_html() + limitations_html()),
                    "provenance": (None, provenance_html(prov, params, json_sha256)),
                },
            )
        )
        p.append("</main>")
        body = "".join(p)
        return body if fragment else _page(f"Tornado demix report {wallet[:10]}", body)

    leads = report_leads(data) if params.get("mode", "events") == "events" else []
    groups = {"lead": [], "context": [], "chance": []}
    for c in cands:
        groups[candidate_kind(c)].append(c)
    kinds = Counter({k: len(v) for k, v in groups.items()})
    ties = _ties(cands)

    p.append(_incomplete_html(data))
    p.append(deposits_table_html(data, network, uid))

    ranks = {(c["pool_key"], c["address"]): i for i, c in enumerate(cands, 1)}
    lead_body = [
        '<div class="meta">These are <b>probabilistic leads for '
        "corroboration, not proof</b>. A lead needs a linked address (a direct link only "
        "when its first withdrawal came within 72 h of the last deposit) or an early "
        "multi-pool profile match; the band rises to strong only when two independent lead "
        "sources agree. Amount+timing and gas price never raise the band and alone are "
        "chance-level on real depositors and stay weak. Open a row's evidence timeline to "
        "see what was checked. The score only orders leads within a band.</div>",
    ]
    if groups["lead"]:
        lead_body.append(
            _cand_table(data, groups["lead"], network, f"{uid}-leads", ties, net_name, ranks)
        )
    else:
        lead_body.append(
            '<div class="callout">No corroborated candidates — every candidate '
            "rests on amount+timing, gas price or a context signal alone; treat them as "
            "starting points and trace the funding source.</div>"
        )
    lead_body.append(f'<div class="meta">{_e(band_note())}</div>')

    ctx_body = (
        '<div class="meta">Weak candidates with a context-only signal (a late direct link, a '
        "withdrawal sent by the depositor's side, a shared exchange deposit address). Shown "
        "because they concern this wallet; they never raise the band. Per signal: "
        + _e(context_verdicts())
        + ".</div>"
        + (
            _cand_table(data, groups["context"], network, f"{uid}-context", ties, net_name, ranks)
            or '<div class="meta">None.</div>'
        )
    )
    chance_body = (
        '<div class="meta">Weak candidates on amount+timing (a count match) or gas price '
        "alone. Real depositors and decoy windows show these equally often: treat them as "
        "chance-level, not as leads.</div>"
        + (
            _cand_table(data, groups["chance"], network, f"{uid}-chance", ties, net_name, ranks)
            or '<div class="meta">None.</div>'
        )
    )
    if not cands:
        chance_body += (
            '<div class="meta">No discriminating candidates. This usually means single-note '
            "vouchers (count of 1), where the method cannot narrow the pool.</div>"
        )

    dep_rows = data.get("deposit_addresses") or []
    exit_groups = data.get("exit_groups", [])
    anchored = [g for g in exit_groups if g["anchored"]]
    shared_fp = [g for g in data.get("fee_fingerprints", []) if g.get("shared")]
    sections = {
        "leads": (kinds["lead"], "".join(lead_body)),
        "context": (kinds["context"], ctx_body),
        "chance": (kinds["chance"], chance_body),
        "deposits": (
            len(dep_rows),
            _deposit_addresses_html(data, network)
            or '<div class="meta">No deposit address found (or the search was off).</div>',
        ),
        "groups": (
            len(anchored),
            _groups_html(exit_groups, network)
            or '<div class="meta">No exit group was found.</div>',
            "Exit groups",
        ),
    }
    if params.get("mode", "events") == "events":
        sections["fees"] = (
            f"{len(shared_fp)} shared, {len(leads)} self-relayed",
            _fees_html(data, network, leads),
        )
    sections["method"] = (
        None,
        '<h2>Summary<span class="rule"></span></h2>'
        f'<div class="meta" style="white-space:pre-line">{_e(conclusion(data))}</div>'
        + _method_demix_html(data, network),
    )
    deposits_tbl = [
        '<h2>Deposits<span class="rule"></span></h2>'
        '<table><colgroup><col style="width:22%"><col style="width:16%"><col></colgroup>'
        "<tr><th>Pool</th><th>Notes</th><th>First deposit (UTC)</th></tr>"
    ]
    for v in data["vouchers"]:
        deposits_tbl.append(
            '<tr><td class="num">{}</td><td class="num">{}</td><td class="num">{}</td></tr>'.format(
                _e(v["pool_key"]), _e(v["count"]), _ts(v["first_ts"])
            )
        )
    deposits_tbl.append("</table>")
    # Provenance and the full hashes always print; only the plain deposit dump is screen-only.
    sections["provenance"] = (
        None,
        evidence_hashes_html(data, network, cands) + provenance_html(prov, params, json_sha256),
    )
    sections["raw"] = (None, "".join(deposits_tbl))
    p.append(_sections_html(uid, sections))
    p.append(
        f'<div class="foot">Generated by tornado-demix · analysis of public on-chain data · '
        f"{generated}. Not legal advice.</div>"
    )
    p.append("</main>")
    body = "".join(p)
    return body if fragment else _page(f"Tornado demix report {wallet[:10]}", body)


def write_html_report(
    data: dict,
    path: str,
    network: Network,
    attribution: dict[str, dict] | None = None,
    json_sha256: str | None = None,
) -> str:
    """Write the single-wallet HTML report to ``path``.

    ``attribution`` is threaded to :func:`build_html_report` so the ranked table
    can carry attribution labels when a set is loaded (see that function).

    Written as UTF-8 explicitly: the document declares that charset, and the
    platform default on Windows is a legacy codepage.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_html_report(data, network, attribution=attribution, json_sha256=json_sha256))
    return path


MAX_GROUP_ROWS = 40  # group-view candidates shown in the HTML report; the JSON lists all


def _group_html(group: dict, network: Network) -> str:
    """The group view of ``multi --group``: the conditional caveat first, then the merged
    notes, the windows read, the ranked candidates on the merged notes and the
    exploratory exact-k rows."""
    p = [f'<h2>{_e(GROUP_VIEW_TITLE)}<span class="rule"></span></h2>']
    p.append(f'<div class="callout"><b>Caveat.</b> {_e(group.get("caveat", ""))}</div>')
    profile = format_fingerprint(group.get("profile", {})) or "no deposits"
    p.append(
        f'<div class="meta">Wallets merged <b>{len(group.get("wallets", []))}</b>: '
        f'<span class="mono wrap">{_e(", ".join(group.get("wallets", [])))}</span> · '
        f"merged notes <b>{_e(profile)}</b></div>"
    )
    for note in group.get("notices", []):
        p.append(f'<div class="callout"><b>Long window.</b> {_e(note)}</div>')
    windows = [
        (pool_key, w, res)
        for pool_key, res in sorted(group.get("denoms", {}).items())
        for w in res.get("windows", [])
    ]
    if windows:
        p.append(
            "<table><tr><th>Pool</th><th>Window (UTC)</th><th>Blocks</th>"
            "<th>Withdrawals</th><th>Recipients</th></tr>"
        )
        for pool_key, w, res in windows:
            p.append(
                f'<tr><td class="num">{_e(pool_key)}</td>'
                f'<td class="mono">{_e(_ts(w["first_ts"]))} .. {_e(_ts(w["end_ts"]))}</td>'
                f'<td class="mono">{_e(w.get("start_block"))}..{_e(w.get("end_block"))}</td>'
                f'<td class="num">{res.get("total_qualifying_withdrawals", 0)}</td>'
                f'<td class="num">{res.get("unique_recipients", 0)}</td></tr>'
            )
        p.append("</table>")
    for u in group.get("unresolved", []):
        p.append(
            f'<div class="meta flag">{_e(u["pool_key"])} not searched: {_e(u["reason"])}</div>'
        )
    ranked = ranked_candidates(group)
    if ranked:
        p.append(
            "<table><tr><th>Band</th><th>Score</th><th>Address</th><th>Pool</th>"
            "<th>Hits</th><th>Signals</th></tr>"
        )
        for r in ranked[:MAX_GROUP_ROWS]:
            p.append(
                f"<tr><td>{_band_badge(r['band'])}</td>"
                f'<td class="num">{r["confidence"]:.2f}</td>'
                f'<td class="mono wrap">{_addr_link(network, r["address"])}</td>'
                f'<td class="num">{_e(r["pool_key"])}</td>'
                f'<td class="num">{r["hits"]}</td>'
                f"<td>{_e(', '.join(r['signals']))}</td></tr>"
            )
        p.append("</table>")
        if len(ranked) > MAX_GROUP_ROWS:
            p.append(
                f'<div class="meta">... and {len(ranked) - MAX_GROUP_ROWS} more (all in the '
                "JSON).</div>"
            )
    else:
        p.append('<div class="meta">No candidate on the merged notes.</div>')
    rows = group.get("exact_k", [])
    if rows:
        p.append(f"<h3>{_e(EXACT_K_TITLE)}</h3>")
        p.append(f'<div class="meta">{_e(EXACT_K_CAVEAT)}</div>')
        p.append(
            "<table><tr><th>Pool</th><th>k</th><th>Window</th><th>Observed</th>"
            "<th>Base mean</th><th>P(&ge; observed)</th><th>Base windows</th></tr>"
        )
        for r in rows:
            mean = "n/a" if r["base_mean"] is None else f"{r['base_mean']:.2f}"
            tail = exact_k_tail_text(r)
            skips = exact_k_skips(r)
            extra = f" ({skips})" if skips else ""
            p.append(
                f'<tr><td class="num">{_e(r["pool_key"])}</td><td class="num">{r["k"]}</td>'
                f'<td class="num">{r["window_hours"]:.0f} h</td>'
                f'<td class="num">{r["observed"]}</td><td class="num">{mean}</td>'
                f'<td class="num">{tail}</td>'
                f'<td class="mono">{r["base_windows"]} {_e(r["base_counts"])}{_e(extra)}</td></tr>'
            )
        p.append("</table>")
    return "".join(p)


def _consolidator_rationale(g: dict) -> str:
    """Why a cross-wallet consolidator has its band (HTML)."""
    band = g["band"]
    if band == "strong":
        return (
            "Band: strong — corroborated by an independent signal ("
            + _e(", ".join(g["independent"]))
            + ") beyond the fingerprint overlap, so the link is not a shared-window artefact."
        )
    if band == "moderate":
        return (
            "Band: moderate — matches the <b>distinct</b> fingerprints of "
            f"{g['distinct_fingerprints']} different note-patterns, which a "
            "shared search window does not produce by chance. No independent "
            "gas-price / linked signal, so corroborate before concluding."
        )
    if g.get("artefact_kind") == "synchronized":
        return (
            "Band: weak — <b>window-overlap artefact (synchronised batch)</b>. "
            "The fingerprints differ, but every matched wallet deposited "
            "inside one synchronised batch, and their pools overlap, so a "
            "single busy-pool recipient satisfies these distinct patterns by "
            "shared window rather than by a real link. Corroborate with a "
            "per-wallet demix signal (gas-price / linked) before treating it "
            "as a real consolidator."
        )
    return (
        "Band: weak — <b>window-overlap artefact</b>. The matched "
        "wallets share one identical fingerprint and this address "
        "carries no independent signal, so a synchronous batch of "
        "equal-denomination deposits lets many unrelated recipients "
        "match all of them at once. Corroborate with a per-wallet "
        "demix signal (self-relayed / gas-price / linked) before "
        "treating it as a real consolidator."
    )


def _consolidator_table(rows, network: Network, scope: str, net_name: str) -> str:
    """Cross-wallet consolidators: band, address, wallets; the rationale on demand."""
    if not rows:
        return ""
    out = [
        _filter_bar(scope)
        + f'<table class="ft" data-scope="{_e(scope)}"><colgroup><col style="width:9%">'
        '<col style="width:34%"><col style="width:9%"><col></colgroup>'
        "<thead><tr><th>Band</th><th>Consolidator</th><th>Wallets</th>"
        "<th>Wallets whose full fingerprint it received</th></tr></thead>"
    ]
    for addr, ws, g in rows:
        lead_cls = ' class="lead"' if g["band"] in ("strong", "moderate") else ""
        out.append(
            f'<tbody data-f="1" data-band="{_e(g["band"])}" data-net="{_e(net_name)}">'
            f"<tr{lead_cls}><td>{_band_badge(g['band'])}</td>"
            f'<td>{_addr_link(network, addr)}</td><td class="num">{len(ws)}</td>'
            f"<td>{'<br>'.join(_plain_addr(w) for w in ws)}</td></tr>"
            f'<tr class="det"><td colspan="4"><details class="tl"><summary>Why this band'
            f'</summary><div class="why">Received the full deposit fingerprint of {len(ws)} '
            f'wallets.</div><div class="rat">{_consolidator_rationale(g)}</div></details></td></tr>'
            "</tbody>"
        )
    out.append("</table>")
    return "".join(out)


def build_multi_report(corr: dict, network: Network, fragment: bool = False, uid: str = "r") -> str:
    """Render a multi-wallet correlation result as a self-contained HTML string.

    Same layout as :func:`build_html_report`: verdict card, then Leads (cross-wallet
    consolidators graded strong or moderate), Context (window-prone consolidators,
    deposit synchronicity, profile matches, shared funders), Chance-level (shared
    count-matched candidates), Groups and clusters (operator clusters, the group view),
    Method and limitations. A consolidator linking two or more wallets is a
    multi-wallet structural lead, but still a lead for corroboration, not proof.
    """
    results = corr.get("results", {})
    wallets = list(results)
    strong = corr.get("strong", [])
    clusters = corr.get("operator_clusters", [])
    cross = corr.get("cross_profile", {})
    grades = corr.get("consolidator_grades", {})
    profile_matches = corr.get("profile_matches", {})
    fingerprints = corr.get("fingerprints", {}) or {}

    def _profile_kind(wallet):
        return profile_match_kind(fingerprints.get(wallet) or {})

    # A single-pool "fingerprint" is just the count match: chance-level, not context
    # (heuristics.profile_match_kind, the same scale as candidate_kind).
    n_multi_profiles = sum(
        len(v) for w, v in profile_matches.items() if _profile_kind(w) == "context"
    )
    n_single_profiles = sum(
        len(v) for w, v in profile_matches.items() if _profile_kind(w) == "chance"
    )
    funders = corr.get("shared_funders", {}) or {}

    fp_counts = Counter(
        tuple(sorted(fp.items())) for fp in corr.get("fingerprints", {}).values() if fp
    )
    dominant_fp, dominant_n = (fp_counts.most_common(1) or [((), 0)])[0]
    sync_groups = corr.get("sync_groups", [])
    tight_batches = [
        g for g in sync_groups if g.get("span_seconds", 0) <= SYNC_MAX_SPAN_HOURS * 3600
    ]
    biggest_batch = tight_batches[0] if tight_batches else None

    net_name = network.name
    for data in results.values():
        net_name = data.get("params", {}).get("network", network.name)
        break
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p = ['<main class="sheet">']

    def _grade_of(addr):
        # An ungraded entry (direct caller) defaults to the conservative weak shape.
        return grades.get(
            addr,
            {
                "band": "weak",
                "artefact": True,
                "independent": [],
                "distinct_fingerprints": 1,
                "artefact_kind": "identical",
            },
        )

    ranked = sorted(
        ((addr, ws, _grade_of(addr)) for addr, ws in cross.items() if len(ws) >= 2),
        key=lambda t: (-BAND_ORDER.get(t[2]["band"], 1), -len(t[1]), t[0]),
    )
    lead_rows = [t for t in ranked if t[2]["band"] in ("strong", "moderate")]
    weak_rows = [t for t in ranked if t[2]["band"] not in ("strong", "moderate")]
    in_clusters = len({w for c in clusters for w in c["wallets"]})
    failed = failed_count(corr)

    if clusters:
        answer = (
            f"{_n(len(clusters), 'operator cluster')} {'links' if len(clusters) == 1 else 'link'} "
            f"{in_clusters} of {len(wallets)} wallets "
            f"on discriminating evidence; {_n(len(lead_rows), 'consolidator lead')}; leads, "
            "not proof."
        )
    elif lead_rows:
        answer = (
            f"{_n(len(lead_rows), 'cross-wallet consolidator lead')}, no operator cluster; "
            "corroborate before concluding."
        )
    else:
        answer = (
            f"No discriminating link between the {len(wallets)} wallets: any overlap rests on "
            "shared search windows."
        )
    if failed:
        answer = answer[:-1] + f", and {incomplete_phrase(corr)}, so results may be incomplete."

    notes = []
    if dominant_n >= 3:
        pretty = format_fingerprint(dict(dominant_fp)) or "an identical fingerprint"
        notes.append(
            '<span class="tag warn">window overlap</span> <b>Window-overlap warning.</b> '
            f"{dominant_n} of the analysed wallets deposited with the <b>same "
            f"fingerprint</b> ({_e(pretty)}). When wallets deposit synchronously "
            "with equal denominations their candidate sets and profile matches "
            "overlap <b>by construction</b>, so the consolidator and profile-match "
            "counts are inflated by the shared window, not the number of real exits. Only "
            "consolidators graded <b>strong</b> (an independent gas-price / linked signal), or "
            "<b>moderate</b> over a different set of wallets that did not deposit together, "
            "rise above that artefact."
        )
    elif biggest_batch and len(biggest_batch["wallets"]) >= 3:
        notes.append(
            '<span class="tag warn">window overlap</span> <b>Window-overlap warning.</b> '
            f"{len(biggest_batch['wallets'])} of the analysed wallets deposited "
            f"as one <b>synchronised batch</b> (within {format_span(biggest_batch['span_seconds'])}, "
            f"{_ts(biggest_batch['first_ts'])}). Their search windows overlap almost "
            "entirely, so the profile-match count and any distinct-fingerprint "
            "consolidator across these wallets are inflated by the shared window — a "
            "consolidator matched wholly inside one batch is graded <b>window-prone</b>. "
            "The synchronicity itself is the real coordination lead here, not the exit "
            "addresses — see <b>Deposit synchronicity</b> under Context."
        )
    if corr.get("group") is not None:
        notes.append(
            f'<span class="tag">group view</span> <b>{_e(GROUP_VIEW_TITLE)} included under '
            f"Groups and clusters.</b> {_e(corr['group'].get('caveat', ''))}"
        )
    warnings = []
    if dominant_n >= 3:
        warnings.append(
            f"Window overlap: {dominant_n} wallets deposited with the same fingerprint, so "
            "consolidator and profile-match counts are inflated by the shared window."
        )
    elif biggest_batch and len(biggest_batch["wallets"]) >= 3:
        warnings.append(
            f"Window overlap: {len(biggest_batch['wallets'])} wallets deposited as one "
            "synchronised batch, so their search windows overlap almost entirely."
        )
    p.append(
        multi_page1(
            corr,
            network,
            uid,
            answer=answer,
            lead_rows=lead_rows,
            weak_rows=weak_rows,
            n_multi_profiles=n_multi_profiles,
            n_funders=len(funders),
            n_single_profiles=n_single_profiles,
            warnings=[f'{_e(w)} <a href="#{_e(uid)}-overlap">Details below.</a>' for w in warnings],
            group_note=(
                f"Group view: {_e(GROUP_VIEW_TITLE)} under Groups and clusters (conditional)."
                if corr.get("group") is not None
                else ""
            ),
        )
    )
    for i, note in enumerate(notes):
        # the first note is the window-overlap warning when there is one (page 1 links it)
        anchor = f' id="{_e(uid)}-overlap"' if warnings and i == 0 else ""
        p.append(f'<div class="callout"{anchor}>{note}</div>')
    p.append(_incomplete_html(corr))

    leads_body = [
        '<h2>Most likely: cross-wallet consolidators<span class="rule"></span></h2>',
        '<div class="meta">These are <b>probabilistic leads for corroboration, '
        "not proof</b>. A consolidator is graded by how much it actually "
        "discriminates: <b>strong</b> carries an independent gas-price or linked "
        "signal beyond the fingerprint overlap; <b>moderate</b> spans distinct "
        "fingerprints of wallets that did <b>not</b> deposit as one synchronised "
        "batch; <b>weak</b> is a window-overlap artefact — the matched wallets "
        "share one identical fingerprint, or deposited together as a synchronised "
        "batch, so a shared window explains the match (listed under Context).</div>",
    ]
    if lead_rows:
        leads_body.append(_consolidator_table(lead_rows, network, f"{uid}-leads", net_name))
    elif not weak_rows:
        leads_body.append(
            '<div class="callout">No cross-wallet consolidator — no single address '
            "received the full fingerprint of two or more wallets. Treat any "
            "per-wallet profile match as a single-wallet lead only.</div>"
        )
    else:
        leads_body.append(
            '<div class="callout">No consolidator rises above the window-overlap artefact; '
            f"{_n(len(weak_rows), 'window-prone consolidator')} "
            f"{'is' if len(weak_rows) == 1 else 'are'} listed under Context.</div>"
        )

    ctx = []
    if weak_rows:
        ctx.append("<h3>Window-prone consolidators</h3>")
        ctx.append(_consolidator_table(weak_rows, network, f"{uid}-context", net_name))
    ctx.append('<h2>Deposit synchronicity<span class="rule"></span></h2>')
    if sync_groups:
        ctx.append(
            '<div class="meta">Wallets whose deposits chain together in time '
            "(single-linkage within 6h). A tight batch is <b>genuine coordination "
            "evidence</b> — independent of any exit address — and is also why a "
            "consolidator matched wholly inside one batch is graded window-prone: "
            "the shared window, not a real link, explains its fingerprint match.</div>"
            '<table><colgroup><col style="width:12%"><col style="width:20%">'
            '<col style="width:20%"><col></colgroup>'
            "<tr><th>Wallets</th><th>Span</th><th>First deposit (UTC)</th>"
            "<th>Members</th></tr>"
        )
        for g in sync_groups:
            ctx.append(
                f'<tr><td class="num">{len(g["wallets"])}</td>'
                f"<td>{_e(format_span(g['span_seconds']))}</td>"
                f'<td class="mono">{_e(_ts(g["first_ts"]))}</td>'
                f"<td>{'<br>'.join(_plain_addr(w) for w in g['wallets'])}</td></tr>"
            )
        ctx.append("</table>")
    else:
        ctx.append(
            '<div class="meta">No two wallets deposited within one synchronised '
            "batch. Deposit timing does not, on its own, tie any of these wallets "
            "together.</div>"
        )
    ctx.append(
        '<h2>Profile matches (full fingerprint on one address)<span class="rule"></span></h2>'
    )
    prof_rows, single_rows = [], []
    for wallet, matches in profile_matches.items():
        printable = format_fingerprint(fingerprints.get(wallet, {}))
        for addr, detail, exact in matches:
            recv = "; ".join(f"{d}:{r}/{n}" for d, (r, n) in sorted(detail.items()))
            row = (wallet, printable, addr, exact, recv)
            (prof_rows if _profile_kind(wallet) == "context" else single_rows).append(row)
    if prof_rows:
        ctx.append(
            '<div class="meta">One address that received at least a wallet\'s whole '
            "deposit fingerprint. A multi-pool fingerprint is far more discriminating "
            'than a single count; a single-pool "fingerprint" is just the count match.</div>'
            '<table><colgroup><col style="width:28%"><col style="width:16%">'
            '<col style="width:28%"><col style="width:9%"><col></colgroup>'
            "<tr><th>Wallet</th><th>Fingerprint</th><th>Consolidator</th>"
            "<th>Match</th><th>Received / needed</th></tr>"
        )
        for wallet, printable, addr, exact, recv in prof_rows[:CANDIDATE_ROW_CAP]:
            match = '<span class="flag">exact</span>' if exact else "&ge;"
            ctx.append(
                f"<tr><td>{_addr_link(network, wallet)}</td>"
                f'<td class="num">{_e(printable)}</td>'
                f"<td>{_addr_link(network, addr)}</td>"
                f'<td>{match}</td><td class="mono wrap">{_e(recv)}</td></tr>'
            )
        ctx.append("</table>")
        if len(prof_rows) > CANDIDATE_ROW_CAP:
            ctx.append(
                f'<div class="meta">{len(prof_rows) - CANDIDATE_ROW_CAP} more in the CSV.</div>'
            )
    else:
        ctx.append('<div class="meta">No address received any wallet\'s full fingerprint.</div>')
    ctx.append('<h2>Shared funders (not labelled, not busy)<span class="rule"></span></h2>')
    if funders:
        ctx.append(
            '<div class="meta">An immediate funder shared by two or more depositors. '
            "Context for the investigation, not scored.</div>"
            '<table><colgroup><col style="width:38%"><col style="width:8%"><col></colgroup>'
            "<tr><th>Funder</th><th>Wallets</th><th>Funded depositors</th></tr>"
        )
        for f, ws in funders.items():
            ctx.append(
                f'<tr><td>{_addr_link(network, f)}</td><td class="num">{len(ws)}</td>'
                f"<td>{'<br>'.join(_plain_addr(w) for w in ws)}</td></tr>"
            )
        ctx.append("</table>")
    else:
        ctx.append('<div class="meta">No immediate funder is shared by two depositors.</div>')

    chance = [
        f'<h2>{_e(SHARED_CANDIDATES_TITLE)}<span class="rule"></span></h2>',
        f'<div class="callout">Caveat: {_e(SHARED_CANDIDATES_CAVEAT)}</div>',
    ]
    if single_rows:
        chance.append(
            '<h3>Single-pool profile matches</h3><div class="meta">An address that received '
            "a single-pool wallet's count of notes: the count match, chance-level.</div>"
            '<table><colgroup><col style="width:28%"><col style="width:16%">'
            '<col style="width:28%"><col style="width:9%"><col></colgroup>'
            "<tr><th>Wallet</th><th>Fingerprint</th><th>Address</th>"
            "<th>Match</th><th>Received / needed</th></tr>"
        )
        for wallet, printable, addr, exact, recv in single_rows[:CANDIDATE_ROW_CAP]:
            match = '<span class="flag">exact</span>' if exact else "&ge;"
            chance.append(
                f"<tr><td>{_addr_link(network, wallet)}</td>"
                f'<td class="num">{_e(printable)}</td>'
                f"<td>{_addr_link(network, addr)}</td>"
                f'<td>{match}</td><td class="mono wrap">{_e(recv)}</td></tr>'
            )
        chance.append("</table>")
    if strong:
        chance.append(
            '<table><colgroup><col style="width:14%"><col style="width:56%">'
            "<col></colgroup>"
            "<tr><th>Pool</th><th>Shared candidate</th><th>Wallets</th></tr>"
        )
        for pool_key, addr, ws in strong[:CANDIDATE_ROW_CAP]:
            chance.append(
                f'<tr><td class="num">{_e(pool_key)}</td>'
                f"<td>{_addr_link(network, addr)}</td>"
                f'<td class="num">{len(ws)}</td></tr>'
            )
        chance.append("</table>")
        if len(strong) > CANDIDATE_ROW_CAP:
            chance.append(
                f'<div class="meta">{len(strong) - CANDIDATE_ROW_CAP} more in the JSON.</div>'
            )
    else:
        chance.append(
            '<div class="meta">No count-matched candidate was shared by two or more wallets.</div>'
        )

    grp = ['<h2>Operator clusters<span class="rule"></span></h2>']
    if clusters:
        grp.append(
            '<div class="meta">Wallets linked into a component only by '
            "<b>discriminating</b> evidence (a shared gas-price/linked exit, or a "
            "shared full-fingerprint consolidator) — never by the window-overlap "
            "artefact that raw count intersections carry.</div>"
        )
        for i, cluster in enumerate(clusters, 1):
            grp.append(
                f'<h3>Cluster {i} <span class="pill">{len(cluster["wallets"])} wallets</span></h3>'
            )
            grp.append(
                f'<div class="meta">{"<br>".join(_plain_addr(w) for w in cluster["wallets"])}</div>'
            )
            if cluster["edges"]:
                grp.append(
                    '<table><colgroup><col style="width:30%"><col style="width:30%">'
                    '<col style="width:22%"><col></colgroup>'
                    "<tr><th>Wallet A</th><th>Wallet B</th><th>Via address</th><th>Why</th></tr>"
                )
                for a, b, reason, addr in cluster["edges"][:40]:
                    grp.append(
                        f"<tr><td>{_addr_link(network, a)}</td>"
                        f"<td>{_addr_link(network, b)}</td>"
                        f"<td>{_addr_link(network, addr)}</td>"
                        f"<td>{_e(reason)}</td></tr>"
                    )
                grp.append("</table>")
    else:
        grp.append(
            '<div class="meta">No two wallets were linked by discriminating '
            "evidence. Any overlaps rest on shared search windows, not on a "
            "proven common operator.</div>"
        )
    if corr.get("group") is not None:
        grp.append(_group_html(corr["group"], network))

    method = (
        '<h2>How to read this<span class="rule"></span></h2><div class="callout">'
        "This report lists <b>probabilistic leads, not proof</b>. A cross-wallet "
        "consolidator or an operator cluster is a multi-wallet structural lead; a bare "
        "overlap of count-matched candidates is often just a shared search window. Corroborate any "
        "link with independent evidence (funding source, exchange KYC, further hops) "
        "before drawing a conclusion.</div>" + limitations_html()
    )
    raw = (
        '<h2>Wallets analysed<span class="rule"></span></h2>'
        "<table><tr><th>Wallet</th><th>Deposit fingerprint</th></tr>"
        + "".join(
            f'<tr><td>{_addr_link(network, w)}</td><td class="num">'
            f"{_e(format_fingerprint(corr.get('fingerprints', {}).get(w, {})) or 'no deposits')}"
            "</td></tr>"
            for w in wallets
        )
        + "</table>"
    )
    p.append(
        _sections_html(
            uid,
            {
                "leads": (len(lead_rows), "".join(leads_body)),
                "context": (len(weak_rows) + n_multi_profiles + len(funders), "".join(ctx)),
                "chance": (
                    len(strong) + n_single_profiles,
                    "".join(chance),
                    "Chance-level overlaps",
                ),
                "groups": (len(clusters), "".join(grp)),
                "method": (None, method),
                "provenance": (None, raw, "Wallets analysed"),
            },
        )
    )
    p.append(
        f'<div class="foot">Generated by tornado-demix · analysis of public on-chain '
        f"data · {generated}. Not legal advice.</div>"
    )
    p.append("</main>")
    body = "".join(p)
    return body if fragment else _page("Tornado.Cash multi-wallet correlation", body)


def write_multi_report(corr: dict, path: str, network: Network) -> str:
    """Write the multi-wallet correlation HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_multi_report(corr, network))
    return path


def build_characterize_report(
    info: dict, network: Network, fragment: bool = False, uid: str = "r"
) -> str:
    """Render an exit-candidate characterisation (see characterize.py) as HTML.

    Same layout as the other reports: verdict card, Context (classification, label,
    deposit-address check), Dominant next hops, Method and limitations, Raw data (the
    pool inflows)."""
    p = ['<main class="sheet">']
    failed = failed_count(info)
    totals = ", ".join(f"{v} {a}" for a, v in sorted(info["inflow_totals"].items()))
    n_in, n_pools = len(info["pool_inflows"]), len(info["distinct_pools"])
    label = info.get("label")
    answer = (
        f"{info['classification'].capitalize()}: {_n(n_in, 'pool inflow')} from "
        f"{_n(n_pools, 'pool')}"
        + (f", total {totals}" if totals else "")
        + (f"; known address: {format_label(label)}" if label else "")
        + "; a lead, not proof."
    )
    if failed:
        answer = answer[:-1] + f", and {incomplete_phrase(info)}, so results may be incomplete."
    p.append(characterize_page1(info, network, uid, answer))
    p.append(_incomplete_html(info))

    ctx = [
        '<div class="metrics">'
        f'<div class="metric"><div class="k">Classification</div>'
        f'<div class="v" style="font-size:1.05rem">{_e(info["classification"])}</div>'
        f'<div class="s">from activity + pool diversity</div></div>'
        f'<div class="metric"><div class="k">Total in</div>'
        f'<div class="v" style="font-size:1.05rem">{_e(totals) or "—"}</div>'
        f'<div class="s">from known pools</div></div></div>'
    ]
    if label:
        entity = f" — entity <b>{_e(label['entity'])}</b>" if label.get("entity") else ""
        conf = f" · confidence {_e(label['confidence'])}" if label.get("confidence") else ""
        ctx.append(
            '<div class="callout"><b>Attribution: {}.</b> This address is a '
            "KNOWN labelled address{}{}. Its identity, not its behaviour, is the "
            "authoritative fact here.</div>".format(_e(format_label(label)), entity, conf)
        )
    ctx.append(
        '<div class="callout"><b>{}.</b> {}</div>'.format(
            _e(info["classification"].capitalize()), _e(info["classification_reason"])
        )
    )
    if info.get("disposable"):
        ctx.append(
            '<div class="callout"><b>Disposable:</b> first activity at most a day before the '
            "first pool inflow (context, not proof).</div>"
        )
    if info.get("deposit_check"):
        ctx.append(
            '<div class="callout"><b>Deposit-address check:</b> {}. Behaviour, not identity; '
            "a deposit address shared with another address is context, never a lead."
            "</div>".format(_e(check_text(info["deposit_check"])))
        )
    ctx.append(
        '<div class="callout">All of this is a <b>lead, not proof</b>. '
        "Corroborate the deposit→inflow timing and the next hop before drawing "
        "a conclusion.</div>"
    )

    hops = ['<h2>Dominant next hops<span class="rule"></span></h2>']
    if info["top_next_hops"]:
        hops.append(
            '<div class="meta">Where this address sends transactions, ranked by count. '
            "A row with no native value is a contract call (for example a token "
            "transfer or approval), not native funds sent onward; token amounts are "
            "not traced here.</div>"
        )
        checked = any("deposit_check" in h for h in info["top_next_hops"])
        hops.append(
            '<table><colgroup><col style="width:8%"><col style="width:14%">'
            '<col style="width:34%"><col style="width:16%"><col></colgroup>'
            "<tr><th>Count</th><th>Total</th><th>Destination</th>"
            "<th>Attribution</th>" + ("<th>Deposit-address check</th>" if checked else "") + "</tr>"
        )
        for h in info["top_next_hops"]:
            tag = format_label(h.get("label"))
            tag_cell = f'<span class="flag">{_e(tag)}</span>' if tag else "—"
            check = h.get("deposit_check")
            check_cell = ""
            if checked:
                check_cell = "<td>{}</td>".format(_e(check_text(check)) if check else "—")
            hops.append(
                f'<tr><td class="num">{h["count"]}</td>'
                f'<td class="num">{_hop_total(h, network)}</td>'
                f"<td>{_addr_link(network, h['address'])}</td>"
                f"<td>{tag_cell}</td>{check_cell}</tr>"
            )
        hops.append("</table>")
    else:
        hops.append('<div class="meta">No outgoing transfers found.</div>')

    raw = ['<h2>Tornado-pool inflows<span class="rule"></span></h2>']
    if info["pool_inflows"]:
        raw.append(
            '<div class="meta">Every withdrawal this address received from a '
            "known pool of this network, oldest first. A spread across pools the "
            "subject never used marks a multi-pool consolidator (it could be a private "
            "exit or a service). Native pools only; "
            "ERC-20 pool withdrawals are out of scope.</div>"
            '<table><colgroup><col style="width:26%"><col style="width:20%">'
            "<col></colgroup>"
            "<tr><th>When (UTC)</th><th>Amount</th><th>Pool</th></tr>"
        )
        for r in info["pool_inflows"][:100]:
            raw.append(
                f'<tr><td class="mono">{_e(_ts(r["ts"]))}</td>'
                f'<td class="num">{_e(r["value"])} {_e(r["asset"])}</td>'
                f'<td class="num">{_e(r["pool_key"])}</td></tr>'
            )
        raw.append("</table>")
        if n_in > 100:
            raw.append(f'<div class="meta">{n_in - 100} more in the CSV and the JSON.</div>')
    else:
        raw.append(
            '<div class="meta">No withdrawals from a known pool were found — '
            "not a Tornado exit on the pools analysed.</div>"
        )
    p.append(
        _sections_html(
            uid,
            {
                "context": (None, "".join(ctx), "Classification"),
                "deposits": (len(info["top_next_hops"]), "".join(hops), "Dominant next hops"),
                "method": (None, limitations_html()),
                "inflows": (n_in, "".join(raw), "Tornado-pool inflows"),
            },
        )
    )
    p.append("</main>")
    body = "".join(p)
    return body if fragment else _page("Tornado.Cash exit-candidate characterisation", body)


def build_classify_report(
    info: dict,
    network: Network,
    fragment: bool = False,
    uid: str = "r",
    labels: dict | None = None,
) -> str:
    """Render one deposit-address check (classify-address) as HTML.

    ``labels`` (optional, ``{address: label}``) names the sweep targets when known."""
    p = ['<main class="sheet">']
    kind = info.get("kind")
    senders = info.get("senders") or []
    failed = failed_count(info)
    # Nothing from the classifier is scored since rule 2.21 (shared_deposit is context).
    scored = "context, not scored" + (
        " · swept to labelled exchange wallets (evidence-grade)" if info.get("evidence") else ""
    )
    targets = []
    if kind not in (NOT_DEPOSIT, UNKNOWN_KIND):
        targets = [info["operator"]] if info.get("operator") else info.get("sweep_targets") or []
    answer = f"{check_text(info)}; behaviour, not identity."
    if failed:
        answer = answer[:-1] + f", and {incomplete_phrase(info)}, so results may be incomplete."
    p.append(classify_page1(info, network, uid, answer, targets, labels))
    p.append(_incomplete_html(info))
    ctx = [
        '<div class="metrics">'
        f'<div class="metric"><div class="k">Kind</div>'
        f'<div class="v" style="font-size:1.05rem">{_e(kind_label(info))}</div>'
        f'<div class="s">{scored}</div></div></div>',
        f'<div class="callout"><b>Result:</b> {_e(check_text(info))}.</div>',
        '<div class="callout">The check describes <b>behaviour, not identity</b>: few senders, '
        "and what arrives is swept to a hot wallet (labelled exchange, labelled service, or an "
        "unlabelled busy address) or moved by an operator. Two addresses that sent to the same "
        "deposit address are context only, never a lead.</div>",
    ]
    dep = []
    if targets:
        dep.append('<h2>Swept to / operator<span class="rule"></span></h2><table>')
        for t in targets:
            tag = format_label(labels.get(t)) if labels and labels.get(t) else ""
            tag_cell = f'<span class="flag">{_e(tag)}</span>' if tag else "—"
            dep.append(f"<tr><td>{_addr_link(network, t)}</td><td>{tag_cell}</td></tr>")
        dep.append("</table>")
    if senders and kind not in (NOT_DEPOSIT, UNKNOWN_KIND):
        dep.append('<h2>Senders<span class="rule"></span></h2><table>')
        for s_addr in senders:
            dep.append(f"<tr><td>{_addr_link(network, s_addr)}</td></tr>")
        dep.append("</table>")
    if info.get("gas_funder") and kind not in (NOT_DEPOSIT, UNKNOWN_KIND):
        dep.append(
            '<div class="meta">Gas funder (a small native transfer before the first token '
            f"sweep): {_addr_link(network, info['gas_funder'])}</div>"
        )
    sections = {"context": (None, "".join(ctx), "Result")}
    if dep:
        sections["deposits"] = (len(senders), "".join(dep), "Sweeps and senders")
    sections["method"] = (None, limitations_html())
    p.append(_sections_html(uid, sections))
    p.append("</main>")
    body = "".join(p)
    return body if fragment else _page("Tornado.Cash deposit-address check", body)


def write_classify_report(info: dict, path: str, network: Network) -> str:
    """Write the deposit-address check HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_classify_report(info, network))
    return path


def write_characterize_report(info: dict, path: str, network: Network) -> str:
    """Write the candidate-characterisation HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_characterize_report(info, network))
    return path


def _recipient_html(info, destinations):
    """Linked L2 and refund recipients of a canonical bridge hop (full addresses)."""
    out = []
    for r in recipient_links(info, destinations):
        addr = _e(r["address"])
        if r["url"]:
            addr = '<a class="wrap" href="{}" target="_blank" rel="noopener">{}</a>'.format(
                _e(r["url"]), addr
            )
        out.append(f"{_e(r['label'])} {addr}")
    return out


def _bridge_note(info, network, destinations):
    """Under a bridge edge: protocol, destination chain, deposit id, recipients, state and
    the fill (or L2 transaction) link."""
    chain = info.get("dest_chain_id")
    dest = destinations.network(chain) if destinations is not None and chain is not None else None

    def name(chain_id):
        return dest.name if dest else f"chain {chain_id}"

    linked = bool(info.get("fill_tx") and dest is not None)
    parts = [_e(bridge_text(info, name, fill=not linked, recipients=False))]
    parts += _recipient_html(info, destinations)
    if linked:
        parts.append(
            '<a class="wrap" href="{}" target="_blank" rel="noopener">{}</a>'.format(
                _e(dest.tx_url(info["fill_tx"])), _e(bridge_tx_label(info))
            )
        )
    return f'<div class="meta">{" · ".join(parts)}</div>'


def _terminal_bridge_note(terminal, destinations):
    """Under a bridge end reason: protocol, destination chain, deposit id, recipients."""
    text = terminal_bridge_text(terminal, destinations, recipients=False)
    parts = [_e(text)] if text else []
    for info in _bridge_infos(terminal):
        parts += _recipient_html(info, destinations)
    return f'<div class="meta">{" · ".join(parts)}</div>' if parts else ""


def _bridge_infos(terminal):
    return terminal.get("bridges") or ([terminal["bridge"]] if terminal.get("bridge") else [])


def terminal_bridge_text(terminal, destinations=None, arrow="→", recipients=True):
    """The bridge line of an end point.

    When the reason already names the bridge only the L2 recipients are listed ("" when
    there are none, or when ``recipients`` is False). An end point where several
    deposits ended the same way lists each of them.
    """
    infos = _bridge_infos(terminal)
    if not infos:
        return ""
    if terminal["reason"].startswith("bridge "):
        if not recipients:
            return ""
        return "; ".join(t for t in (recipient_text(i) for i in infos) if t)

    def name(chain_id):
        net = destinations.network(chain_id) if destinations is not None else None
        return net.name if net else f"chain {chain_id}"

    return "; ".join(
        bridge_text(info, name, arrow=arrow, state=False, recipients=recipients) for info in infos
    )


def _share_note(edge):
    status = share_status(edge)
    if not status:
        return ""
    return f'<div class="meta">{_e(status)}</div>'


def _chain_net(network, destinations, chain_id):
    """Registry entry of the chain an edge is on (the trace's own network by default)."""
    if chain_id is None or chain_id == network.chain_id or destinations is None:
        return network
    return destinations.network(chain_id) or network


def _chain_tag(network, destinations, chain_id):
    """Pill naming the chain, for edges and end points off the starting chain."""
    if chain_id is None or chain_id == network.chain_id:
        return ""
    net = destinations.network(chain_id) if destinations is not None else None
    return ' <span class="pill">{}</span>'.format(_e(net.name if net else f"chain {chain_id}"))


def end_state(reason: str) -> tuple[str, str]:
    """``(tone, short label)`` of a trace end reason, for its badge.

    tone is ``fail`` (a lookup failed), ``chance`` (below threshold or stopped by a
    limit), ``warn`` (sent into Tornado again), ``bridge`` (any bridge end state),
    ``ok`` (a labelled address) or ``ctx`` (any other end)."""
    r = reason or ""
    if r == LOOKUP_FAILED or "lookup_failed" in r or "lookup failed" in r:
        return "fail", "lookup failed"
    if r == BELOW_THRESHOLD:
        return "chance", "below threshold"
    if r == TORNADO_REDEPOSIT:
        return "warn", "Tornado re-deposit"
    if r.startswith("bridge") or r in BRIDGE_END_STATES:
        return "bridge", "bridge"
    if r == "labelled address":
        return "ok", "labelled address"
    if r in ("hop limit", "trace budget"):
        return "chance", "stopped: " + r
    return "ctx", "end point"


# Bridge states that can stand alone as an end reason (see trace.BRIDGE_STATES_NOTE).
BRIDGE_END_STATES = frozenset(
    {
        "fill_not_found",
        "destination_chain_not_supported",
        "destination_pool_not_listed",
        "deposit_expired_possible_refund",
    }
)


def _end_badge(reason: str) -> str:
    tone, label = end_state(reason)
    return f'<span class="badge {tone}">{_e(label)}</span>'


def trace_totals(result: dict) -> list[dict]:
    """Per asset: what reached end points, what stayed below the threshold, what stopped
    at a failed lookup, and their sum (it equals the start amount unless a swap or a
    bridge changed the asset)."""
    out = {}
    for t in result.get("terminals") or []:
        tone, _label = end_state(t["reason"])
        row = out.setdefault(
            t["asset"], {"asset": t["asset"], "endpoints": 0.0, "below": 0.0, "failed": 0.0}
        )
        key = (
            "failed"
            if tone == "fail"
            else ("below" if t["reason"] == BELOW_THRESHOLD else "endpoints")
        )
        row[key] += t["amount"]
    rows = []
    for row in out.values():
        for k in ("endpoints", "below", "failed"):
            row[k] = round(row[k], 6)
        row["total"] = round(row["endpoints"] + row["below"] + row["failed"], 6)
        rows.append(row)
    return rows


def _totals_html(result: dict) -> str:
    rows = trace_totals(result)
    if not rows:
        return ""
    lines = [
        f"End points {r['endpoints']:g} + below threshold {r['below']:g} + failed lookups "
        f"{r['failed']:g} = <b>{r['total']:g} {_e(r['asset'])}</b>"
        for r in rows
    ]
    start = f"{result['amount']:g} {_e(result['asset'])}"
    tail = (
        f" (start amount {start})"
        if len(rows) == 1 and rows[0]["asset"] == result["asset"]
        else f". Start amount {start}; a swap or bridge changed the asset, so the totals are "
        "per asset and do not add up to it."
    )
    return f'<div class="totals">{"<br>".join(lines)}{tail}</div>'


def _trace_svg(result: dict, network: Network, destinations=None, limit: int = 40) -> str:
    """Hops as columns: start, the addresses each edge reached, the end states.

    Addresses are abbreviated (full value in each node's title); bridge edges are
    dashed and labelled; every end point carries its end-state label."""
    edges = result["edges"][:limit]
    start_key = (network.chain_id, result["start"])
    col = {start_key: 0}
    for e in edges:
        frm = (e.get("chain_id", network.chain_id), e["from"])
        col.setdefault(frm, max(e["hop"] - 1, 0))
        key = (e.get("chain_id", network.chain_id), e["to"])
        col[key] = min(col.get(key, e["hop"]), e["hop"])
    ends = {}
    for t in result.get("terminals") or []:
        key = (t.get("chain_id", network.chain_id), t["address"])
        col.setdefault(key, t["hop"])
        ends.setdefault(key, []).append(t)
    if len(col) <= 1 and not ends:
        return ""
    by_col = {}
    for key, c in sorted(col.items(), key=lambda kv: (kv[1], kv[0][1])):
        by_col.setdefault(c, []).append(key)
    n_cols = max(by_col) + 1
    rows = max(len(v) for v in by_col.values())
    col_w, row_h, box_w = 310, 56, 160
    width, height = max(760, n_cols * col_w + 20), rows * row_h + 20
    pos = {}
    for c, keys in by_col.items():
        for i, key in enumerate(keys):
            pos[key] = (10 + c * col_w, 10 + i * row_h)
    parts = [
        f'<svg class="flow" viewBox="0 0 {width} {height}" style="min-width:{width * 0.75:.0f}px" '
        'role="img" aria-label="Hops of the traced funds from the start address to the end points" '
        'xmlns="http://www.w3.org/2000/svg" font-family="inherit" font-size="11">'
    ]
    for e in edges:
        chain = e.get("chain_id", network.chain_id)
        a, b = pos.get((chain, e["from"])), pos.get((chain, e["to"]))
        if not a or not b:
            continue
        x1, y1, x2, y2 = a[0] + box_w, a[1] + 18, b[0], b[1] + 18
        cls = "e-bridge" if e["kind"] == "bridge" else "e-pool"
        mid = (x1 + x2) / 2
        parts.append(
            f'<path class="{cls}" d="M{x1} {y1} C{mid} {y1} {mid} {y2} {x2} {y2}" fill="none" '
            'stroke-width="1.5"/>'
        )
        label = f"{e['attributed']:g} {e['asset']}"
        if e["kind"] == "bridge":
            label = "bridge " + ((e.get("bridge") or {}).get("protocol") or "") + ", " + label
        elif e["kind"] == "swap":
            label = "swap, " + label
        parts.append(
            f'<text class="{"b-bridge" if e["kind"] == "bridge" else "m"}" x="{mid:.0f}" '
            f'y="{(y1 + y2) / 2 - 4:.0f}" text-anchor="middle">{_e(label)}</text>'
        )
    for key, (x, y) in pos.items():
        chain, addr = key
        cls = "n dep" if key == start_key else "n"
        lines = [
            f'<text class="t" x="{x + 8}" y="{y + 15}" font-family="monospace">'
            f"{_e(_short(addr))}</text>"
        ]
        title = addr
        if key in ends:
            reason = ends[key][0]["reason"]
            tone, short = end_state(reason)
            more = f" +{len(ends[key]) - 1}" if len(ends[key]) > 1 else ""
            lines.append(
                f'<text class="b-{tone}" x="{x + 8}" y="{y + 29}">{_e(short)}{more}</text>'
            )
            title += " — " + "; ".join(t["reason"] for t in ends[key])
        elif chain != network.chain_id:
            net = destinations.network(chain) if destinations is not None else None
            lines.append(
                f'<text class="m" x="{x + 8}" y="{y + 29}">'
                f"{_e(net.name if net else f'chain {chain}')}</text>"
            )
        parts.append(
            f"<g><title>{_e(title)}</title>"
            f'<rect class="{cls}" x="{x}" y="{y}" width="{box_w}" height="36" rx="6"/>'
            + "".join(lines)
            + "</g>"
        )
    parts.append("</svg>")
    more = (
        f'<div class="meta">The diagram shows the first {limit} of {len(result["edges"])} '
        "edges; the table lists all.</div>"
        if len(result["edges"]) > limit
        else ""
    )
    return f'<div class="flow-wrap">{"".join(parts)}</div>{more}'


def _sources_note(edge: dict, net: Network) -> str:
    """The branches merged into one edge (``sources``), when there is more than one."""
    sources = edge.get("sources") or []
    if len(sources) < 2:
        return ""
    items = "; ".join(
        f"{_e(s.get('amount'))} from {_tx_link(net, s['tx_hash'])}"
        for s in sources
        if s.get("tx_hash")
    )
    return f'<div class="meta">Reached by {len(sources)} branches: {items}</div>'


def build_trace_report(
    result: dict, network: Network, destinations=None, fragment: bool = False, uid: str = "r"
) -> str:
    """Render a multi-hop FIFO trace (see trace.py) as a self-contained HTML string.

    ``destinations`` (see bridges.Destinations) names the chains a bridge trace
    continued on and supplies their explorer links. Layout: verdict card with the
    totals line, then Where the traced funds stop, Hops, Flow diagram, Method and
    limitations.
    """
    p = ['<main class="sheet">']
    edges, terminals = result["edges"], result["terminals"]
    failed = failed_count(result)
    max_hop = max((e["hop"] for e in edges), default=0)
    biggest = max(terminals, key=lambda t: t["amount"], default=None)
    if biggest is not None:
        answer = (
            f"{result['amount']:g} {result['asset']} from {result['start']} come to rest at "
            f"{_n(len(terminals), 'stop')} within {_n(max_hop, 'hop')}; the largest share, "
            f"{biggest['amount']:g} {biggest['asset']}, ends as {biggest['reason']}."
        )
    else:
        answer = (
            f"{result['amount']:g} {result['asset']} from {result['start']}: the traced "
            "amount was fully attributed along the edges."
        )
    if failed:
        answer = answer[:-1] + f"; {incomplete_phrase(result)}, so results may be incomplete."
    p.append(trace_page1(result, network, uid, destinations, answer))
    p.append(_totals_html(result))
    p.append(_incomplete_html(result))

    stop = [_filter_bar(f"{uid}-leads")]
    if terminals:
        stop.append(
            f'<table class="ft" data-scope="{_e(uid)}-leads"><colgroup><col style="width:34%">'
            '<col style="width:14%"><col style="width:6%"><col style="width:13%"><col>'
            "</colgroup><thead><tr><th>Address</th><th>Amount</th><th>Hop</th><th>End state</th>"
            "<th>Reason</th></tr></thead>"
        )
        for t in terminals:
            label = format_label(t["label"]) if t.get("label") else ""
            tag = f' <span class="pill flag">{_e(label)}</span>' if label else ""
            tag += _chain_tag(network, destinations, t.get("chain_id"))
            net = _chain_net(network, destinations, t.get("chain_id"))
            note = _terminal_bridge_note(t, destinations)
            tone, short = end_state(t["reason"])
            stop.append(
                f'<tbody data-f="1" data-state="{_e(short)}" data-net="{_e(net.name)}">'
                f"<tr><td>{_addr_link(net, t['address'])}</td>"
                f'<td class="num">{_e(t["amount"])} {_e(t["asset"])}</td>'
                f'<td class="num">{t["hop"]}</td><td>{_end_badge(t["reason"])}</td>'
                f"<td>{_e(t['reason'])}{tag}{note}</td></tr></tbody>"
            )
        stop.append("</table>")
    else:
        stop.append(
            '<div class="meta">The traced amount was fully attributed along the edges.</div>'
        )

    hops = []
    if edges:
        hops.append(
            '<table><colgroup><col style="width:6%"><col style="width:11%">'
            '<col style="width:20%"><col style="width:20%"><col style="width:17%"><col>'
            "</colgroup><tr><th>Hop</th><th>Time (UTC)</th><th>From</th><th>To</th>"
            "<th>FIFO amount [lower – upper] / sent</th><th>Transaction</th></tr>"
        )
        for e in edges:
            swap = (
                f'<div class="meta">swap → {_e(e["swapped_to"])}</div>'
                if e.get("swapped_to")
                else ""
            )
            if e["kind"] == "bridge":
                swap += _bridge_note(e["bridge"], network, destinations)
            net = _chain_net(network, destinations, e.get("chain_id"))
            when = _ts(e["ts"]) if e.get("ts") else ""
            hops.append(
                f'<tr><td class="num">{e["hop"]}{_chain_tag(network, destinations, e.get("chain_id"))}</td>'
                f'<td class="num">{_e(when)}</td>'
                f"<td>{_addr_link(net, e['from'])}</td>"
                f"<td>{_addr_link(net, e['to'])}</td>"
                f'<td class="num">{_e(share_text(e))} / {_e(e["value"])} {_e(e["asset"])}'
                f"{_share_note(e)}{swap}</td>"
                f"<td>{_tx_link(net, e['tx_hash'])}{_sources_note(e, net)}</td></tr>"
            )
        hops.append("</table>")
    else:
        hops.append('<div class="meta">No outgoing transfer after the start block.</div>')

    method = (
        '<div class="callout">FIFO attribution is a convention: funds in one account are '
        "fungible, so each edge says where the traced amount would have gone under that "
        "rule. [lower – upper] is how much of the edge the traced funds can be under any "
        "ordering convention, from the sender's own history of the asset: above 0 it is certain "
        "at least that much, 0 it is only possible; with an incomplete history nothing is certain "
        "(possible, history incomplete). A transfer reached by several branches is one edge "
        "with the merged amount. Treat every edge as a lead to corroborate.</div>"
        f'<h3>End states</h3><div class="meta">{_e(END_STATES_NOTE)}</div>' + limitations_html()
    )
    diagram = _trace_svg(result, network, destinations)
    sections = {
        "leads": (len(terminals), "".join(stop), "Where the traced funds stop"),
        "context": (len(edges), "".join(hops), "Hops"),
    }
    if diagram:
        sections["groups"] = (None, diagram, "Flow diagram")
    sections["method"] = (None, method)
    p.append(_sections_html(uid, sections))
    p.append("</main>")
    body = "".join(p)
    return body if fragment else _page("Multi-hop trace of withdrawn funds", body)


def write_trace_report(result: dict, path: str, network: Network, destinations=None) -> str:
    """Write the multi-hop trace HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_trace_report(result, network, destinations))
    return path


def build_cluster_report(
    traces: list[dict], network: Network, fragment: bool = False, uid: str = "r"
) -> str:
    """Render cluster split-exit traces as a self-contained HTML string.

    Same layout as the other reports. Per wallet it lists the reconvergence points Z,
    the pool layers that feed each Z, and the per-asset totals (via
    :func:`format_totals`, so nothing is summed across assets). A Z fed by two or
    more pool layers is where a wallet's split exits reconverge - a structural
    lead. Tracing is one hop, so deeper reconvergence may be missed.
    """
    n_clusters = sum(len(t.get("clusters", [])) for t in traces)
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    p = ['<main class="sheet">']
    merged = {
        "errors": [
            dict(e, wallet=t.get("wallet", "")) for t in traces for e in t.get("errors") or []
        ],
        "truncated": [x for t in traces for x in t.get("truncated") or []],
    }
    failed = failed_count(merged)
    answer = (
        f"{_n(n_clusters, 'reconvergence point')} fed by two or more pool layers across "
        f"{_n(len(traces), 'wallet')}; one-hop leads, not proof."
        if n_clusters
        else f"No reconvergence point within one hop for the {_n(len(traces), 'wallet')}."
    )
    if failed:
        answer = answer[:-1] + f", and {incomplete_phrase(merged)}, so results may be incomplete."
    p.append(cluster_page1(traces, network, uid, answer, merged))
    p.append(_incomplete_html(merged))

    all_z = [(trace["wallet"], c) for trace in traces for c in trace.get("clusters", [])]
    all_z.sort(key=lambda wc: (-wc[1]["n_layers"], -len(wc[1]["totals"]), wc[1]["z"]))
    leads = [
        '<h2>Most likely reconvergence points<span class="rule"></span></h2>',
        '<div class="meta">These are <b>probabilistic leads for corroboration, not '
        "proof</b>, from <b>1-hop</b> tracing that may miss deeper reconvergence. A "
        "downstream address fed by <b>two or more</b> pool layers is where a wallet's "
        "split exits reconverge — a structural lead.</div>",
    ]
    if all_z:
        for wallet, cluster in all_z[:8]:
            leads.append(
                '<div class="lead-card"><div class="top">'
                f"{_addr_link(network, cluster['z'])}"
                f'<span class="pill">{cluster["n_layers"]} pool layers</span></div>'
                f'<div class="why">Fed by {cluster["n_layers"]} pool layers '
                f"({_e(', '.join(str(k) for k in cluster['pools_covered']))}); "
                f"reconverged value {_e(format_totals(cluster['totals']))}.</div>"
                f'<div class="rat">Wallet {_plain_addr(wallet)} — a Z fed by 2+ pool layers is '
                "where split exits may reconverge. One hop is not an evidence band: a busy "
                "service (an exchange hot wallet, a DEX) can collect unrelated exits.</div>"
                "</div>"
            )
    else:
        leads.append(
            '<div class="callout">No reconvergence point — no downstream address is '
            "fed by two or more pool layers within the traced window. The exits may "
            "reconverge beyond one hop, which this 1-hop tracer does not follow.</div>"
        )
    grp = ['<h2>Per-wallet clusters<span class="rule"></span></h2>']
    for trace in traces:
        printable = format_fingerprint(trace.get("fingerprint", {}))
        grp.append(
            f"<h3>{_addr_link(network, trace['wallet'])} "
            f'<span class="pill">{_e(printable)}</span></h3>'
        )
        clusters = trace.get("clusters", [])
        if not clusters:
            grp.append('<div class="meta">No downstream address is fed by 2+ pool layers.</div>')
            continue
        grp.append(
            '<table><colgroup><col style="width:34%"><col style="width:8%">'
            '<col style="width:22%"><col></colgroup>'
            "<tr><th>Reconvergence point Z</th><th>Layers</th>"
            "<th>Pools</th><th>Value funneled (per asset)</th></tr>"
        )
        for cluster in clusters[:40]:
            pools = ", ".join(str(k) for k in cluster["pools_covered"])
            grp.append(
                f'<tr class="lead"><td>{_addr_link(network, cluster["z"])}</td>'
                f'<td class="num">{cluster["n_layers"]}</td>'
                f'<td class="num">{_e(pools)}</td>'
                f'<td class="num">{_e(format_totals(cluster["totals"]))}</td></tr>'
            )
        grp.append("</table>")
    method = (
        '<h2>How to read this<span class="rule"></span></h2><div class="callout">'
        "This report lists <b>probabilistic leads, not proof</b>. Tracing is "
        "<b>1-hop</b>: a wallet whose exits reconverge only after several hops shows "
        "no cluster here. Per-asset totals are never summed across assets. Corroborate "
        "any reconvergence point with independent evidence before drawing a conclusion."
        "</div>" + limitations_html()
    )
    p.append(
        _sections_html(
            uid,
            {
                "leads": (n_clusters, "".join(leads)),
                "groups": (len(traces), "".join(grp), "Per-wallet clusters"),
                "method": (None, method),
            },
        )
    )
    p.append(
        f'<div class="foot">Generated by tornado-demix · analysis of public on-chain '
        f"data · {generated}. Not legal advice.</div>"
    )
    p.append("</main>")
    body = "".join(p)
    return body if fragment else _page("Tornado.Cash split-exit clusters", body)


def write_cluster_report(traces: list[dict], path: str, network: Network) -> str:
    """Write the cluster-trace HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_cluster_report(traces, network))
    return path
