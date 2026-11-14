"""The first page of every HTML report: what was analysed, the verdict, four key
figures, how the money moved, the addresses, the checks and what follows.

One layout for every report type (demix, multi, trace, characterize, classify-address).
Each ``*_page1`` function turns a result into the parts of the page; :func:`page1_html`
renders them. The flow diagram is an inline SVG built here (no script, no external
resource); its colours come from the report stylesheet (:data:`PAGE1_CSS`), and every
status also has its own marker shape and fill, so the diagram reads in greyscale.

In print the page ends with a page break and fits one A4 sheet for typical cases: the
diagram and the address table show the first :data:`TOP_N` addresses and say how many
more the sections below list.
"""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timezone

from .networks import display_name

TOP_N = 8  # addresses in the diagram and the table of the first page

# status -> chip text. "strong" and "lead" are the strong and moderate bands, "context"
# a weak candidate with a context-only signal, "chance" a weak one on amount+timing or
# gas price alone. The trace and address reports use the neutral and end-state ones.
STATUS_LABEL = {
    "strong": "strong lead",
    "lead": "lead",
    "context": "context",
    "chance": "chance-level",
    "fail": "lookup failed",
    "bridge": "bridge",
    "below": "below threshold",
    "end": "end point",
    "labelled": "labelled",
    "warn": "Tornado re-deposit",
    "node": "address",
}

LEGEND_TEXT = {
    "strong": "strong lead: two independent lead sources",
    "lead": "lead: one lead signal",
    "context": "context: a link that did not beat chance",
    "chance": "chance-level: amount+timing or gas price alone",
    "fail": "a lookup failed",
    "bridge": "bridge",
    "below": "below the threshold, not followed",
    "end": "end point",
    "labelled": "labelled address",
    "warn": "sent into Tornado again",
    "node": "address",
}

PAGE1_CSS = """
 .p1{--p1-lead:#1f4e8c;--p1-ctx:#a87a1c;--p1-chance:#68717d;--p1-fail:#a3222d;
   --p1-ok:#2e6b3f;--p1-paper:var(--panel);--p1-frame:var(--ink);
   background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
   padding:1rem 1.1rem .9rem;margin:.6rem 0 1rem;overflow-wrap:anywhere}
 @media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .p1{--p1-lead:#8fb8e6;
   --p1-ctx:#e0b45a;--p1-chance:#a3adb9;--p1-fail:#ff9a9f;--p1-ok:#7fd39a}}
 :root[data-theme="dark"] .p1{--p1-lead:#8fb8e6;--p1-ctx:#e0b45a;--p1-chance:#a3adb9;
   --p1-fail:#ff9a9f;--p1-ok:#7fd39a}
 .p1 .mono{font-size:.86em}
 .p1-head{display:grid;grid-template-columns:minmax(0,1fr) minmax(11rem,15rem);
   border:1.5px solid var(--p1-frame)}
 .p1-head .l{padding:.6rem .8rem;min-width:0}
 .p1-head .l small{display:block;color:var(--muted);font-size:.78rem}
 .p1-head h1{font-size:1.2rem;margin:.15rem 0 .25rem;overflow-wrap:anywhere}
 .p1-head h1 .mono{font-size:.92em;word-break:break-all}
 .p1-head .r{border-left:1.5px solid var(--p1-frame);display:flex;flex-direction:column;
   min-width:0}
 .p1-head .r div{padding:.3rem .6rem;font-size:.78rem;border-bottom:1px solid var(--line)}
 .p1-head .r div:last-child{border-bottom:0}
 .p1 .verdict{display:grid;grid-template-columns:minmax(10rem,15rem) minmax(0,1fr);
   border:1.5px solid var(--p1-frame);border-top:0;margin:0;padding:0;border-radius:0;
   background:transparent}
 .p1-word{background:var(--ink);color:var(--panel);padding:.8rem .9rem;display:flex;
   flex-direction:column;justify-content:center;-webkit-print-color-adjust:exact;
   print-color-adjust:exact}
 .p1-word b{font-size:1.75rem;line-height:1.05;overflow-wrap:break-word}
 .p1-word b.long{font-size:1.3rem}
 .p1-word .cnt{font-size:.8rem;opacity:.9;margin-top:.35rem}
 .p1-word .cnt span{white-space:nowrap}
 .p1 .verdict .answer{margin:0;padding:.7rem .9rem;font-size:.95rem;font-weight:400;
   line-height:1.5;max-width:none;overflow-wrap:anywhere}
 .p1-figs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
   border:1.5px solid var(--p1-frame);border-top:0}
 .p1-figs>div{padding:.5rem .7rem;border-right:1px solid var(--line);font-size:.78rem;
   color:var(--muted);min-width:0}
 .p1-figs>div:last-child{border-right:0}
 .p1-figs b{display:block;font-size:1.3rem;line-height:1.15;color:var(--ink);
   overflow-wrap:anywhere}
 .p1-figs .lead b{color:var(--p1-lead)}.p1-figs .ctx b{color:var(--p1-ctx)}
 .p1-figs .fail b{color:var(--p1-fail)}
 .p1 h2{font-size:.95rem;margin:1rem 0 .3rem}
 .p1-flow{margin:.2rem 0 0}
 .p1-flow svg{display:block;width:100%;max-width:880px;height:auto}
 .p1-flow text{font-family:ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
 .p1-flow .t1{fill:var(--ink)}.p1-flow .m1{fill:var(--muted)}
 .p1-flow .mono1{font-family:ui-monospace,Consolas,monospace}
 .p1-flow .box{fill:var(--panel);stroke:var(--ink);stroke-width:1.5}
 .p1-flow .hub{fill:var(--head);stroke:var(--ink);stroke-width:1.5}
 .p1-flow .cp{fill:var(--panel);stroke:var(--p1-ctx);stroke-width:1.5;stroke-dasharray:4 3}
 .p1-flow .e-dep{stroke:var(--ink);opacity:.85}
 .p1-flow .e-lead,.p1-flow .e-strong{stroke:var(--p1-lead)}
 .p1-flow .e-context{stroke:var(--p1-ctx)}
 .p1-flow .e-chance,.p1-flow .e-below,.p1-flow .e-node,.p1-flow .e-end,
 .p1-flow .e-labelled{stroke:var(--p1-chance)}
 .p1-flow .e-fail{stroke:var(--p1-fail);stroke-dasharray:6 3}
 .p1-flow .e-bridge{stroke:var(--p1-lead);stroke-dasharray:6 3}
 .p1-flow .e-warn{stroke:var(--p1-ctx)}
 .p1-flow .e-cp{stroke:var(--p1-ctx);stroke-dasharray:4 3;fill:none}
 .p1-flow .e-dep,.p1-flow [class^="e-"]{fill:none}
 .p1-flow .lbl-cp{fill:var(--p1-ctx)}
 .p1-flow .lbl{paint-order:stroke;stroke:var(--panel);stroke-width:3px;stroke-linejoin:round}
 .mk{stroke-width:2}
 .mk.strong,.mk.lead{fill:var(--p1-lead);stroke:var(--p1-lead)}
 .mk.ring{fill:none;stroke:var(--p1-lead);stroke-width:1.5}
 .mk.context{fill:var(--panel);stroke:var(--p1-ctx)}
 .mk.chance,.mk.below{fill:var(--panel);stroke:var(--p1-chance);stroke-dasharray:3 2}
 .mk.fail{fill:var(--panel);stroke:var(--p1-fail)}
 .mk.bridge{fill:var(--panel);stroke:var(--p1-lead)}
 .mk.warn{fill:var(--panel);stroke:var(--p1-ctx)}
 .mk.end,.mk.node{fill:var(--panel);stroke:var(--ink)}
 .mk.labelled{fill:var(--ink);stroke:var(--ink)}
 .mkn{font-weight:700;font-size:10px}
 .mkn.strong,.mkn.lead,.mkn.labelled{fill:var(--panel)}
 .mkn.context,.mkn.warn{fill:var(--p1-ctx)}.mkn.fail{fill:var(--p1-fail)}
 .mkn.bridge{fill:var(--p1-lead)}
 .mkn.chance,.mkn.below,.mkn.end,.mkn.node{fill:var(--ink)}
 .p1-legend{display:flex;flex-wrap:wrap;gap:.25rem 1.1rem;font-size:.76rem;color:var(--muted);
   margin:.2rem 0 .2rem}
 .p1-legend span{display:inline-flex;align-items:center;gap:.3rem}
 .p1-legend svg{width:14px;height:14px;flex:none}
 .p1 table.p1-tab{margin:.2rem 0 .2rem}
 .p1-tab td,.p1-tab th{overflow-wrap:anywhere}
 .p1-tab td.mono{word-break:break-all}
 .chip1{display:inline-block;max-width:100%;border:1.5px solid var(--line);border-radius:9px;
   padding:0 .45rem;font-size:.74rem;font-weight:650;line-height:1.35;white-space:normal;
   overflow-wrap:anywhere;-webkit-print-color-adjust:exact;print-color-adjust:exact}
 .chip1.strong{background:var(--p1-lead);border-color:var(--p1-lead);color:var(--panel);
   box-shadow:0 0 0 1.5px var(--panel),0 0 0 3px var(--p1-lead)}
 .chip1.lead{background:var(--p1-lead);border-color:var(--p1-lead);color:var(--panel)}
 .chip1.context,.chip1.warn{border-color:var(--p1-ctx);color:var(--p1-ctx)}
 .chip1.chance,.chip1.below{border-style:dashed;border-color:var(--p1-chance);
   color:var(--soft)}
 .chip1.fail{border-color:var(--p1-fail);color:var(--p1-fail)}
 .chip1.bridge{border-style:dashed;border-color:var(--p1-lead);color:var(--p1-lead)}
 .chip1.labelled{background:var(--ink);border-color:var(--ink);color:var(--panel)}
 .chip1.end,.chip1.node{border-color:var(--ink);color:var(--ink)}
 .p1-more{font-size:.8rem;color:var(--muted);margin:.1rem 0 .2rem}
 .p1-checks{list-style:none;padding:0;margin:0;display:grid;gap:.3rem}
 .p1-checks li{display:grid;grid-template-columns:1.5rem minmax(0,1fr);gap:.5rem;
   font-size:.84rem;align-items:start}
 .p1-checks .mk1{font-weight:700;text-align:center;border-radius:3px;color:#fff;
   line-height:1.45;-webkit-print-color-adjust:exact;print-color-adjust:exact}
 .mk1.ok{background:#2e6b3f}.mk1.warn{background:#9a3b12}.mk1.info{background:#5b6674}
 .p1-toc{margin-top:1rem;display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));
   gap:.6rem;font-size:.78rem;color:var(--muted)}
 .p1-toc a{display:block;border-top:2px solid var(--ink);padding-top:.3rem;color:var(--muted)}
 .p1-toc a b{display:block;color:var(--ink)}
 @media screen and (max-width:760px){.p1-head,.p1 .verdict{grid-template-columns:1fr}
   .p1-head .r{border-left:0;border-top:1.5px solid var(--p1-frame)}
   .p1-figs{grid-template-columns:repeat(2,minmax(0,1fr))}}
 @media print{
   .p1{border:0;border-radius:0;padding:0;margin:0;break-after:page;
     --p1-lead:#1f4e8c;--p1-ctx:#a87a1c;--p1-chance:#5f6873;--p1-fail:#a3222d}
   .p1-head h1{font-size:12pt;margin:.5mm 0 1mm}
   .p1-head .l{padding:2.5mm 3.5mm}.p1-head .l small{font-size:7.8pt}
   .p1-head .r div{font-size:7.8pt;padding:1.2mm 2.5mm}
   .p1 .verdict{grid-template-columns:48mm minmax(0,1fr)}
   .p1-word{padding:3mm 3.5mm;background:#17202b;color:#fff}
   .p1-word b{font-size:18pt}.p1-word b.long{font-size:13pt}.p1-word .cnt{font-size:8pt}
   .p1 .verdict .answer{font-size:9pt;padding:2.5mm 3.5mm;line-height:1.38}
   .p1-figs>div{padding:2mm 2.5mm;font-size:7.6pt}.p1-figs b{font-size:13pt}
   .p1 h2{font-size:9.5pt;margin:3mm 0 1mm}
   .p1-flow svg{max-width:none;max-height:80mm}
   .p1-legend{font-size:7.4pt;margin:.5mm 0 1mm}
   .p1-tab th{font-size:7pt}.p1-tab td{font-size:7.6pt}
   .chip1{font-size:6.8pt;padding:0 1.2mm}
   .p1-more{font-size:7.4pt}
   .p1-checks{gap:1mm}.p1-checks li{font-size:8pt;grid-template-columns:5mm minmax(0,1fr)}
   .p1-toc{margin-top:3mm;font-size:7.6pt;gap:2.5mm}
   .p1-toc a{color:#444}
   .p1 table,.p1-checks li,.p1-flow,.p1 .verdict,.p1-figs,.p1-head{break-inside:avoid}}
"""


def _e(x) -> str:
    return html.escape(str(x))


def _n(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "")


def _fit(text: str, limit: int) -> str:
    """Descriptive SVG text cut to ``limit`` characters (never used for a full address)."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_amount(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return (f"{v:,.6f}".rstrip("0").rstrip(".")) or "0"


def utc_range(first_ts, last_ts) -> str:
    """'2020-10-23 08:10–10:19' (one day) or '2020-10-23 08:10 – 2020-10-24 02:47' UTC."""
    if first_ts is None:
        return ""
    a = datetime.fromtimestamp(int(first_ts), tz=timezone.utc)
    b = datetime.fromtimestamp(int(last_ts if last_ts is not None else first_ts), tz=timezone.utc)
    if a.strftime("%Y-%m-%d %H:%M") == b.strftime("%Y-%m-%d %H:%M"):
        return a.strftime("%Y-%m-%d %H:%M")
    if a.date() == b.date():
        return f"{a:%Y-%m-%d %H:%M}–{b:%H:%M}"
    return f"{a:%Y-%m-%d %H:%M} – {b:%Y-%m-%d %H:%M}"


def chip(status: str, text: str | None = None) -> str:
    return f'<span class="chip1 {_e(status)}">{_e(text or STATUS_LABEL.get(status, status))}</span>'


# --- the flow diagram ----------------------------------------------------------------

_W = 760
_LX, _LW = 0, 150  # left column boxes
_MX, _MW = 262, 140  # middle column boxes
_CX, _R = 572, 10  # right column markers (centre x, radius)
_TX = _CX + _R + 8  # right column labels


def _marker(status: str, x: float, y: float, num) -> str:
    """The marker of one right-column node: shape and fill carry the status, so the
    diagram reads without colour (filled = lead, ring = strong, hollow = context,
    dashed = chance-level, square = failed lookup, diamond = bridge)."""
    s = _e(status)
    if status == "fail":
        shape = (
            f'<rect class="mk fail" x="{x - _R:.1f}" y="{y - _R:.1f}" width="{2 * _R}" '
            f'height="{2 * _R}" rx="1"/>'
        )
    elif status == "bridge":
        shape = (
            f'<path class="mk bridge" d="M{x:.1f} {y - _R - 2:.1f} L{x + _R + 2:.1f} {y:.1f} '
            f'L{x:.1f} {y + _R + 2:.1f} L{x - _R - 2:.1f} {y:.1f} Z"/>'
        )
    else:
        shape = f'<circle class="mk {s}" cx="{x:.1f}" cy="{y:.1f}" r="{_R}"/>'
        if status == "strong":
            shape += f'<circle class="mk ring" cx="{x:.1f}" cy="{y:.1f}" r="{_R + 4}"/>'
    label = (
        f'<text class="mkn {s}" x="{x:.1f}" y="{y + 3.5:.1f}" text-anchor="middle">{_e(num)}</text>'
        if num is not None
        else ""
    )
    return shape + label


def _legend_swatch(status: str) -> str:
    inner = (
        _marker(status, 7, 7, None)
        .replace(f'r="{_R}"', 'r="5"')
        .replace(f'r="{_R + 4}"', 'r="6.5"')
    )
    if status in ("fail", "bridge"):
        inner = {
            "fail": '<rect class="mk fail" x="2" y="2" width="10" height="10" rx="1"/>',
            "bridge": '<path class="mk bridge" d="M7 1 L13 7 L7 13 L1 7 Z"/>',
        }[status]
    return f'<svg viewBox="0 0 14 14" aria-hidden="true">{inner}</svg>'


def legend_html(statuses, extra: list[str] | None = None) -> str:
    """Legend of the statuses present, plus the line-width note."""
    items = []
    for s in dict.fromkeys(statuses):
        items.append(f"<span>{_legend_swatch(s)}{_e(LEGEND_TEXT.get(s, s))}</span>")
    for text in extra or []:
        items.append(f"<span>{text}</span>")
    return f'<div class="p1-legend">{"".join(items)}</div>'


def flow_svg(left: list[dict], mid: list[dict], right: list[dict], edges: list[dict], aria: str):
    """A three-column flow: boxes on the left and in the middle, numbered status markers
    on the right, edges between them with a width proportional to their ``w``.

    A node is ``{"id", "lines": [(text, cls)], "title", "style"}`` (``style`` box, hub
    or cp for a dashed counterparty); a right node is ``{"id", "num", "status",
    "label", "sub", "title"}``. An edge is ``{"a", "b", "w", "cls", "label"}``; ``w``
    None draws a thin line."""
    n_rows = max(len(right), 1)
    row_h = 38 if n_rows > 4 else 46
    inner = max(n_rows * row_h, len(left) * 46, len(mid) * 58, 120)
    top = 12
    height = inner + 2 * top
    pos = {}  # id -> (x_left, x_right, y_centre)

    def place(nodes, x, w, min_h):
        k = max(len(nodes), 1)
        slot = inner / k
        for i, node in enumerate(nodes):
            lines = node.get("lines") or []
            want = 12 + 14 * len(lines)
            h = max(min(want, slot - 8, 92), min_h)
            yc = top + slot * (i + 0.5)
            node["_box"] = (x, yc - h / 2, w, h)
            node["_n_lines"] = max(1, min(len(lines), int((h - 6) // 14)))
            pos[node["id"]] = (x, x + w, yc)

    place(left, _LX + 1, _LW, 26)
    place(mid, _MX, _MW, 26)
    slot = inner / n_rows
    for i, node in enumerate(right):
        yc = top + slot * (i + 0.5)
        pos[node["id"]] = (_CX - _R, _CX + _R, yc)
        node["_y"] = yc

    weights = [e["w"] for e in edges if e.get("w")]
    w_max = max(weights) if weights else 1

    def width(e):
        if not e.get("w"):
            return 1.4
        return round(1.5 + 8.5 * e["w"] / w_max, 1)

    def skips_middle(e):
        return pos[e["a"]][1] < _MX and pos[e["b"]][0] > _MX + _MW

    drawn = [
        e for e in edges if e["a"] in pos and e["b"] in pos and pos[e["b"]][0] > pos[e["a"]][1]
    ]
    # An edge from the left column straight to the right one runs in a lane under the
    # middle column, so it never crosses a middle box or its text.
    lane = top + inner + 10
    if any(skips_middle(e) for e in drawn):
        height += 22
    parts = [
        f'<svg viewBox="0 0 {_W} {height:.0f}" role="img" aria-label="{_e(aria)}" '
        'xmlns="http://www.w3.org/2000/svg" font-size="11">'
    ]
    labels = []
    right_ids = {n["id"] for n in right}
    mid_ids = {n["id"] for n in mid}
    for e in sorted(drawn, key=lambda e: (e.get("cls") != "cp", -(e.get("w") or 0))):
        _xa, x1, y1 = pos[e["a"]]
        x2, _xb, y2 = pos[e["b"]]
        end = x2 - (3 if e["b"] in right_ids else 1)
        mx = (x1 + end) / 2
        cls = "e-cp" if e.get("cls") == "cp" else f"e-{_e(e.get('cls') or 'node')}"
        if skips_middle(e):
            d = (
                f"M{x1:.1f} {y1:.1f} C{x1 + 45:.1f} {y1:.1f} {x1 + 45:.1f} {lane:.1f} "
                f"{x1 + 90:.1f} {lane:.1f} L{end - 90:.1f} {lane:.1f} C{end - 45:.1f} {lane:.1f} "
                f"{end - 45:.1f} {y2:.1f} {end:.1f} {y2:.1f}"
            )
        else:
            d = f"M{x1:.1f} {y1:.1f} C{mx:.1f} {y1:.1f} {mx:.1f} {y2:.1f} {end:.1f} {y2:.1f}"
        parts.append(f'<path class="{cls}" d="{d}" stroke-width="{width(e)}"/>')
        if e.get("label"):
            # Labels go on thin edges only, clear of boxes: on the lane under the middle
            # column, under the start of a fan out of a middle node, or below the middle
            # of an edge into the middle column. A halo keeps them legible.
            if skips_middle(e):
                lx, ly = _MX + _MW / 2, lane + 12
            elif e["a"] in mid_ids:
                lx, ly = x1 + 58, y1 + 15
            else:
                lx, ly = mx, max(y1, y2) + 12
            lcls = "lbl lbl-cp" if e.get("cls") == "cp" else "lbl m1"
            labels.append(
                f'<text class="{lcls}" x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                f'font-size="9.5">{_e(_fit(e["label"], 22))}</text>'
            )
    for node in left + mid:
        x, y, w, h = node["_box"]
        style = node.get("style", "box")
        rx = h / 2 if style == "hub" else 4
        parts.append(
            f"<g><title>{_e(node.get('title', ''))}</title>"
            f'<rect class="{_e(style)}" x="{x:.1f}" y="{y:.1f}" width="{w}" height="{h:.1f}" '
            f'rx="{min(rx, 28):.1f}"/>'
        )
        lines = (node.get("lines") or [])[: node["_n_lines"]]
        y0 = y + h / 2 - (len(lines) - 1) * 7 + 4
        cx = x + w / 2
        for j, (text, cls) in enumerate(lines):
            mono = " mono1" if "mono" in cls else ""
            weight = ' font-weight="600"' if "b" in cls.split() else ""
            size = 10 if "mono" in cls or "m" in cls.split() else 11.5
            colour = "m1" if "m" in cls.split() else "t1"
            parts.append(
                f'<text class="{colour}{mono}" x="{cx:.1f}" y="{y0 + 14 * j:.1f}" '
                f'text-anchor="middle" font-size="{size}"{weight}>{_e(_fit(text, 24))}</text>'
            )
        parts.append("</g>")
    for node in right:
        y = node["_y"]
        parts.append(f"<g><title>{_e(node.get('title', ''))}</title>")
        parts.append(_marker(node["status"], _CX, y, node.get("num")))
        if node.get("sub"):
            parts.append(
                f'<text class="t1" x="{_TX}" y="{y - 1.5:.1f}" font-size="10.5">'
                f"{_e(_fit(node['label'], 27))}</text>"
                f'<text class="m1" x="{_TX}" y="{y + 10.5:.1f}" font-size="9">'
                f"{_e(_fit(node['sub'], 32))}</text>"
            )
        else:
            parts.append(
                f'<text class="t1" x="{_TX}" y="{y + 3.5:.1f}" font-size="10.5">'
                f"{_e(_fit(node['label'], 27))}</text>"
            )
        parts.append("</g>")
    parts.extend(labels)
    parts.append("</svg>")
    return "".join(parts)


# --- the page ------------------------------------------------------------------------


def check(mark: str, text_html: str, key: str | None = None, value=None) -> tuple:
    """One line of the checks list: ``mark`` ok (✓), warn (!) or info (i)."""
    return (mark, text_html, key, value)


def page1_html(uid: str, p: dict) -> str:
    """Render the first page from its parts (see the ``*_page1`` builders)."""
    right = "".join(f"<div>{cell}</div>" for cell in p.get("right") or [])
    head = (
        '<div class="p1-head"><div class="l">'
        f"<small>{_e(p['kicker'])}</small><h1>{p['title_html']}</h1>"
        f"<small>{p.get('sub_html', '')}</small></div>"
        f'<div class="r">{right}</div></div>'
    )
    counts = " · ".join(
        f'<span data-count="{_e(k)}" data-value="{_e(v)}">{_e(v)} '
        f"{_e(label if isinstance(label, str) else label[v != 1])}</span>"
        for k, v, label in p.get("counts") or []
    )
    word_attr = (
        f' data-count="{_e(p["word_key"])}" data-value="{_e(p["word_value"])}"'
        if p.get("word_key")
        else ""
    )
    # a single word longer than the block is set smaller instead of breaking mid-word
    long_word = ' class="long"' if max(len(w) for w in p["word"].split()) > 11 else ""
    verdict = (
        f'<section class="verdict" id="{_e(uid)}-verdict" aria-label="Verdict">'
        f'<div class="p1-word"{word_attr}><b{long_word}>{_e(p["word"])}</b>'
        f'<div class="cnt">{counts}</div></div>'
        f'<p class="answer">{_e(p["reason"])}</p></section>'
    )
    figs = "".join(
        f'<div class="{_e(tone)}" data-count="{_e(key)}" data-value="{_e(raw)}">'
        f"<b>{_e(value)}</b>{_e(label)}</div>"
        for key, value, label, tone, raw in p.get("figures") or []
    )
    out = [
        f'<section class="p1" id="{_e(uid)}-p1" aria-label="Summary">',
        head,
        verdict,
        f'<div class="p1-figs">{figs}</div>',
    ]
    if p.get("flow"):
        out.append(f"<h2>{_e(p.get('flow_title', 'How the money moved'))}</h2>")
        out.append(f'<div class="p1-flow">{p["flow"]}</div>')
        out.append(p.get("legend", ""))
    if p.get("table"):
        out.append(f"<h2>{_e(p.get('table_title', 'Addresses'))}</h2>")
        out.append(p["table"])
    if p.get("more"):
        out.append(f'<div class="p1-more">{p["more"]}</div>')
    items = []
    for mark, text, key, value in p.get("checks") or []:
        sym = {"ok": "&#10003;", "warn": "!", "info": "i"}[mark]
        attr = f' data-count="{_e(key)}" data-value="{_e(value)}"' if key else ""
        items.append(
            f'<li{attr}><span class="mk1 {mark}" aria-label="{_e(mark)}">{sym}</span>'
            f"<span>{text}</span></li>"
        )
    out.append(f'<h2>Checks</h2><ul class="p1-checks">{"".join(items)}</ul>')
    toc = "".join(
        f'<a href="#{_e(uid)}-{_e(key)}"><b>{_e(title)}</b>{_e(desc)}</a>'
        for key, title, desc in p.get("toc") or []
    )
    if toc:
        out.append(
            f'<div class="p1-toc" role="navigation" aria-label="Following pages">{toc}</div>'
        )
    out.append("</section>")
    return "".join(out)


def table_html(head: list[tuple[str, str]], rows: list[list[str]]) -> str:
    """A compact table; ``head`` is ``[(title, width)]``, cells are HTML."""
    cols = "".join(f'<col style="width:{w}">' if w else "<col>" for _t, w in head)
    ths = "".join(f"<th>{_e(t)}</th>" for t, _w in head)
    body = "".join("<tr>" + "".join(f"<td{c[0]}>{c[1]}</td>" for c in r) + "</tr>" for r in rows)
    return (
        f'<table class="p1-tab"><colgroup>{cols}</colgroup><thead><tr>{ths}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _cell(html_text: str, cls: str = "") -> tuple[str, str]:
    return (f' class="{cls}"' if cls else "", html_text)


def mono_cell(addr: str) -> tuple[str, str]:
    """A full address in a table cell (plain text, wraps anywhere)."""
    return _cell(_e(addr), "mono")


def num_cell(text) -> tuple[str, str]:
    return _cell(_e(text), "num")


def text_cell(html_text: str) -> tuple[str, str]:
    return _cell(html_text)


# --- shared checks -------------------------------------------------------------------


def data_check(result: dict, unresolved: int = 0) -> tuple:
    """✓ or ! for data completeness: failed lookups and lists the explorer cut off are
    counted apart (a cut-off list is not a failed lookup)."""
    from .report_html import failed_parts, incomplete_phrase

    failed, cut = failed_parts(result)
    if failed or cut or unresolved:
        parts = []
        phrase = incomplete_phrase(result)
        if phrase:
            parts.append(phrase)
        if unresolved:
            parts.append(f"{_n(unresolved, 'pool')} could not be searched (a block lookup failed)")
        text = "Data: " + "; ".join(parts) + ", so results may be incomplete."
        if cut:
            text += (
                " A cut-off list is a list the explorer returned only in part, not a failed lookup."
            )
        return check("warn", _e(text), "failed", failed)
    return check(
        "ok",
        "Data: every lookup answered and no list was cut off by the explorer.",
        "failed",
        0,
    )


def method_checks() -> list[tuple]:
    from .honest import exploratory_banner, limitations

    banner = exploratory_banner()
    out = []
    if banner:
        out.append(check("info", "Method: " + _e(banner)))
    else:
        out.append(
            check("ok", "Method: the rules are locked in docs/PROTOCOL.lock (confirmatory).")
        )
    out.append(
        check(
            "info",
            f"Leads, not proof: {len(limitations())} limitations apply to every result "
            "(Method and limitations).",
        )
    )
    return out


def provenance_cells(prov: dict, json_note: str | None) -> list[str]:
    cells = [
        f"Generated {_e(prov.get('generated_utc', ''))}",
        f"tornado-demix {_e(prov.get('version', '?'))}",
    ]
    if json_note:
        cells.append(_e(json_note))
    return cells


def figure(key: str, value, label: str, tone: str = "", raw=None) -> tuple:
    """One of the four key figures; ``raw`` (default ``value``) is its data-value."""
    return (key, value, label, tone, value if raw is None else raw)


def _addrs(count: int) -> str:
    return f"{count} {'address' if count == 1 else 'addresses'}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- demix ---------------------------------------------------------------------------

# Why each context signal never raises the band, in a short network-neutral phrase for
# the first page (the method section carries the measured figures).
_CONTEXT_PHRASE = {
    "linked_sender": "a withdrawal sent by the depositor's side did not beat chance on "
    "real depositors",
    "linked_late": "a direct link withdrawn late did not beat chance on real depositors",
    "shared_deposit": "a shared exchange deposit address has not been shown to beat chance",
}


def candidate_status(row: dict) -> str:
    """strong / lead / context / chance for a ranked candidate."""
    from .heuristics import candidate_kind

    if row["band"] == "strong":
        return "strong"
    if row["band"] == "moderate":
        return "lead"
    return candidate_kind(row)


def _context_story(data: dict, ctx: list[dict]) -> list[str]:
    """Sentences on what the context-only candidates rest on, from their real signals:
    who sent their withdrawals, which deposit address they share, how late they came."""
    from .heuristics import EARLY_EXIT_HOURS

    wallet = (data.get("wallet") or "").lower()
    keys = {(c["pool_key"], c["address"]) for c in ctx}
    h = data.get("heuristics", {})
    by_sender: dict[str, set] = {}
    for pk, addr, sender, _tx in h.get("linked_senders", []):
        if (pk, addr) in keys:
            by_sender.setdefault(sender, set()).add(addr)
    out = []
    for sender, addrs in sorted(by_sender.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:2]:
        who = (
            "the depositor itself"
            if sender == wallet
            else f"{sender}, an address the depositor transacted with"
        )
        n = len(addrs)
        out.append(
            f"{'One address' if n == 1 else _cap(_addrs(n))} received withdrawals sent by {who}."
        )
    if len(by_sender) > 2:
        out.append(f"{len(by_sender) - 2} more senders of that kind are listed under Context.")
    late = sorted({c["address"] for c in ctx if "linked_late" in c["signals"]})
    if late:
        out.append(
            f"{_cap(_addrs(len(late)))} transacted with the depositor directly but first "
            f"withdrew more than {EARLY_EXIT_HOURS} h after the last deposit."
        )
    shared = set()
    for pk, addr, deps in h.get("shared_deposits", []):
        if (pk, addr) in keys:
            shared.update(deps)
    if shared:
        n = sum(1 for c in ctx if "shared_deposit" in c["signals"])
        deps = sorted(shared)
        named = deps[0] + (f" and {len(deps) - 1} more" if len(deps) > 1 else "")
        out.append(
            f"{_cap(_addrs(n))} sent funds to an exchange deposit address the depositor "
            f"also used ({named})."
        )
    fired = [
        s
        for s in ("linked_sender", "linked_late", "shared_deposit")
        if any(s in c["signals"] for c in ctx)
    ]
    if fired:
        why = "; ".join(_CONTEXT_PHRASE[s] for s in fired)
        out.append(
            f"That is context: {why}. It never raises the band and does not show that "
            "these withdrawals spent this depositor's notes."
        )
    return out


def demix_reason(data: dict, cands: list[dict], kinds: Counter, incomplete: str, net: str) -> str:
    """The verdict paragraph: the one-sentence answer, then what it rests on, built
    from the signals that fired (never generic wording)."""
    from .heuristics import candidate_reason_short
    from .report_html import _demix_answer

    text = _demix_answer(cands, kinds, incomplete, net)
    if not cands:
        single = [v for v in data.get("vouchers", []) if v["count"] == 1]
        if single:
            text += (
                f" {_cap(_n(len(single), 'voucher'))} of a single note: a count of one does "
                "not narrow the pool; trace the deposit funding source instead."
            )
        return text
    if kinds["lead"]:
        text += f" Why: {candidate_reason_short(cands[0])}."
        more = kinds["lead"] - 1
        if more:
            text += f" {_cap(_n(more, 'more lead'))} below, each with its evidence."
        return text
    ctx = [c for c in cands if candidate_status(c) == "context"]
    story = _context_story(data, ctx)
    if story:
        text += " " + " ".join(story)
    n = kinds["chance"]
    if n:
        lead_in = f"{n} more" if ctx else ("The" if n == 1 else f"All {n}")
        text += (
            f" {lead_in} {'candidate rests' if n == 1 else 'candidates rest'} on amount+timing "
            "or gas price alone, which real depositors and decoy windows show equally often."
        )
    return text


def _placebo_check(pb: dict | None) -> tuple:
    if not pb:
        return check(
            "info",
            "Placebo: not run for this report (--placebo repeats the search in a decoy "
            "window that ends before the first deposit).",
        )
    t, d = pb.get("target", {}), pb.get("decoy", {})
    tl = t.get("strong", 0) + t.get("moderate", 0)
    dl = d.get("strong", 0) + d.get("moderate", 0)
    band = "strong" if t.get("strong") else ("moderate" if t.get("moderate") else "weak")
    verdict = (pb.get("verdicts") or {}).get(band, "")
    text = (
        f"Placebo: real window {_n(tl, 'lead')} and {t.get('weak', 0)} weak; decoy window "
        f"(ends before the first deposit) {_n(dl, 'lead')} and {d.get('weak', 0)} weak."
    )
    if verdict:
        text += f" {_cap(verdict)}."
    if tl and tl > dl:
        mark = "ok"
    elif tl or dl:
        mark = "warn"
    elif t.get("weak", 0) > d.get("weak", 0):
        mark = "ok"
    else:
        mark = "info"
    if pb.get("direct_link_verdict"):
        text += f" Direct links: {pb['direct_link_verdict']}"
    return check(mark, _e(text))


_RANK = {"strong": 0, "lead": 1, "context": 2, "chance": 3}


def by_address(cands: list[dict], statuses: list[str]) -> list[dict]:
    """The ranked (address, pool) rows grouped per address, in rank order: one entry per
    distinct address with its rows, its strongest status and its total withdrawals."""
    out: dict[str, dict] = {}
    for c, st in zip(cands, statuses):
        a = out.setdefault(c["address"], {"address": c["address"], "rows": [], "status": st})
        a["rows"].append((c, st))
        if _RANK[st] < _RANK[a["status"]]:
            a["status"] = st
    for a in out.values():
        a["hits"] = sum(c["hits"] for c, _st in a["rows"])
        a["fresh"] = any((c.get("fresh") or {}).get("fresh") for c, _st in a["rows"])
    return list(out.values())


def _demix_flow(data: dict, shown: list[dict]) -> tuple[str, bool]:
    """The demix diagram (one marker per address, one edge per pool it withdrew from)
    and whether it draws a dashed link (for the legend)."""
    wallet = (data.get("wallet") or "").lower()
    notes = Counter()
    for v in data.get("vouchers") or []:
        notes[v["pool_key"]] += v["count"]
    pools = sorted(set(notes) | {c["pool_key"] for a in shown for c, _st in a["rows"]})
    dates = [v["first_ts"] for v in data.get("vouchers") or []]
    lines = [("Depositor", "t b"), (short_addr(wallet), "mono")]
    lines.append((_n(sum(notes.values()), "note"), "m"))
    if dates:
        lines.append(
            (datetime.fromtimestamp(min(dates), tz=timezone.utc).strftime("%Y-%m-%d"), "m")
        )
    left = [{"id": "dep", "style": "box", "title": data.get("wallet", ""), "lines": lines}]
    mid = []
    for k in pools:
        searched = data.get("denoms", {}).get(k, {}).get("total_qualifying_withdrawals")
        # the note count goes inside the hub: a label on a thick edge is illegible
        pl = [(f"{k} pool", "t b"), (f"{_n(notes[k], 'note')} in", "m")]
        if searched is not None:
            pl.append((f"{searched:,} searched", "m"))
        mid.append({"id": f"p:{k}", "style": "hub", "title": f"{k} pool", "lines": pl})
    edges = [{"a": "dep", "b": f"p:{k}", "w": notes[k], "cls": "dep"} for k in pools if notes[k]]
    num = {a["address"]: i for i, a in enumerate(shown, 1)}
    by_sender: dict[str, list] = {}
    for _pk, addr, sender, _tx in data.get("heuristics", {}).get("linked_senders", []):
        if addr in num and addr not in by_sender.get(sender, []):
            by_sender.setdefault(sender, []).append(addr)
    cps = sorted(by_sender.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:2]
    right = []
    for i, a in enumerate(shown, 1):
        rows = a["rows"]
        label = (
            f"{rows[0][0]['hits']} × {rows[0][0]['pool_key']}"
            if len(rows) == 1
            else f"{a['hits']} withdrawals, {len(rows)} pools"
        )
        right.append(
            {
                "id": f"c:{i}",
                "num": i,
                "status": a["status"],
                "label": label,
                "sub": STATUS_LABEL[a["status"]] + (" · fresh" if a["fresh"] else ""),
                "title": a["address"],
            }
        )
        for c, st in rows:
            edges.append({"a": f"p:{c['pool_key']}", "b": f"c:{i}", "w": c["hits"], "cls": st})
    dashed = False
    for sender, addrs in cps:
        dashed = True
        src = "dep"
        if sender != wallet:
            src = f"cp:{sender}"
            mid.append(
                {
                    "id": src,
                    "style": "cp",
                    "title": sender,
                    "lines": [("Counterparty", "t b"), (short_addr(sender), "mono")],
                }
            )
            edges.append({"a": "dep", "b": src, "w": None, "cls": "cp", "label": "transacted with"})
        for j, addr in enumerate(addrs):
            label = "sent the withdrawals" if j == 0 else ""
            edges.append({"a": src, "b": f"c:{num[addr]}", "w": None, "cls": "cp", "label": label})
    linked = False
    for a in shown:
        if any({"linked", "linked_late"} & set(c["signals"]) for c, _st in a["rows"]):
            edges.append(
                {
                    "a": "dep",
                    "b": f"c:{num[a['address']]}",
                    "w": None,
                    "cls": "cp",
                    "label": "" if linked else "direct counterparty",
                }
            )
            linked = dashed = True
    svg = flow_svg(
        left, mid, right, edges, "Money flow from the depositor through the pools to the addresses"
    )
    return svg, dashed


_KIND_SECTION = {
    "lead": ("leads", "Leads"),
    "context": ("context", "Context"),
    "chance": ("chance", "Chance-level"),
}


def _more_note(uid: str, total: int, rest_kinds: Counter, noun: str = "candidates") -> str:
    if total <= TOP_N:
        return ""
    where = ", ".join(
        f'<a href="#{_e(uid)}-{_KIND_SECTION[k][0]}">{rest_kinds[k]} in {_KIND_SECTION[k][1]}</a>'
        for k in ("lead", "context", "chance")
        if rest_kinds[k]
    )
    return f"+{total - TOP_N} more {noun} below" + (f": {where}." if where else ".")


def demix_page1(
    data: dict, network, cands: list[dict], prov: dict, json_sha256: str | None, uid: str
) -> str:
    """The first page of a single-wallet demix report."""
    from .report_csv import _fmt_hours, deposited_by_asset
    from .report_html import format_totals_human, incomplete_phrase

    params = data.get("params", {})
    net_name = params.get("network", network.name)
    wallet = data["wallet"]
    vouchers = data.get("vouchers") or []
    pools = sorted({v["pool_key"] for v in vouchers})
    exit_h = params.get("exit_window_hours")
    window = (
        f"exit window {_fmt_hours(exit_h)} h after each voucher's last deposit"
        if exit_h
        else f"{params.get('window_days', 30)}-day window after each voucher's deposits"
    )
    mode = params.get("mode", "events")
    sub = [_e(display_name(net_name))]
    if pools:
        sub.append(_e(", ".join(pools)) + (" pool" if len(pools) == 1 else " pools"))
    sub.append(_e(window))
    if mode != "events":
        sub.append(f"mode {_e(mode)}")
    json_note = (
        "JSON SHA-256 on the last page (Provenance)"
        if json_sha256
        else "JSON export not written in this run"
    )
    p = {
        "kicker": "Tornado.Cash exit analysis, research use",
        "title_html": f'Depositor <span class="mono">{_e(wallet)}</span>',
        "sub_html": " · ".join(sub),
        "right": provenance_cells(prov, json_note),
    }
    unresolved = len(data.get("unresolved") or [])
    incomplete = incomplete_phrase(data)
    if not vouchers:
        p.update(
            word="No deposits",
            word_key="band",
            word_value="none",
            counts=[],
            reason=(
                f"No Tornado.Cash deposits found for this address on {display_name(net_name)}: it "
                "made no "
                "deposits into the configured pools of this network, so there is nothing to "
                "demix."
                + (f" Note: {incomplete}, so results may be incomplete." if incomplete else "")
            ),
            figures=[
                figure("deposited", "0", "deposits into the configured pools", "", 0),
                figure("searched", "0", "withdrawals searched", "", 0),
                figure("candidates", "0", "candidate addresses", "", 0),
                figure("leads", "0", "leads above the threshold", "", 0),
            ],
            checks=[data_check(data, unresolved), *method_checks()],
            toc=[
                ("method", "Method and limitations", "how to read a result, the limits"),
                ("provenance", "Provenance", "tool, sources, JSON SHA-256"),
            ],
        )
        return page1_html(uid, p)

    statuses = [candidate_status(c) for c in cands]
    kind_of = ["lead" if s in ("strong", "lead") else s for s in statuses]
    kinds = Counter({"lead": 0, "context": 0, "chance": 0})
    kinds.update(kind_of)
    per_band = Counter(c["band"] for c in cands)
    n_notes = sum(v["count"] for v in vouchers)
    first = min(v["first_ts"] for v in vouchers)
    last = max(v.get("last_ts", v["first_ts"]) for v in vouchers)
    days = datetime.fromtimestamp(first, tz=timezone.utc).strftime("%Y-%m-%d")
    if last - first >= 86400:
        days += " – " + datetime.fromtimestamp(last, tz=timezone.utc).strftime("%Y-%m-%d")
    searched = sum(
        res.get("total_qualifying_withdrawals") or 0 for res in data.get("denoms", {}).values()
    )
    hits = sum(c["hits"] for c in cands)
    value = Counter()
    for c in cands:
        value[c.get("asset") or network.currency] += c.get("total_value") or 0
    word = "Strong lead" if per_band["strong"] else ("Lead" if per_band["moderate"] else "No lead")
    addrs = by_address(cands, statuses)
    shown = addrs[:TOP_N]
    rows = [
        [
            # the number is shown once; the next pool of the same address repeats it as a ditto
            num_cell(i if k == 0 else "″"),
            mono_cell(a["address"]),
            num_cell(c["pool_key"]),
            text_cell(_e(f"{c['hits']} · {utc_range(c['first_ts'], c['last_ts'])}")),
            text_cell(chip(st)),
        ]
        for i, a in enumerate(shown, 1)
        for k, (c, st) in enumerate(a["rows"])
    ]
    flow, dashed = _demix_flow(data, shown) if shown else ("", False)
    extra = ["line width = number of withdrawals"]
    if dashed:
        extra.insert(0, "dashed = how the depositor is linked to the address")
    n_addr = len(addrs)
    cand_label = (
        f"{'address' if n_addr == 1 else 'addresses'}, {_n(hits, 'withdrawal')}, "
        f"{format_totals_human(dict(value))}"
    )
    if n_addr != len(cands):
        cand_label += f" ({len(cands)} address-pool candidates; band counts are per candidate)"
    rest = Counter()
    for a in addrs[TOP_N:]:
        rest["lead" if a["status"] in ("strong", "lead") else a["status"]] += 1
    n_fresh = sum(1 for a in addrs if a["fresh"])
    fresh_check = (
        [
            check(
                "info",
                _e(
                    f"Fresh addresses: {n_fresh} of the {_addrs(n_addr)} had their first "
                    "activity shortly before their first withdrawal, as a disposable exit "
                    "does (context, not scored)."
                ),
            )
        ]
        if n_fresh
        else []
    )
    p.update(
        word=word,
        word_key="band",
        word_value=cands[0]["band"] if cands else "none",
        counts=[
            ("strong", per_band["strong"], "strong"),
            ("moderate", per_band["moderate"], "lead"),
            ("context", kinds["context"], "context"),
            ("chance", kinds["chance"], "chance-level"),
        ],
        reason=demix_reason(data, cands, kinds, incomplete, display_name(net_name)),
        figures=[
            figure(
                "deposited",
                format_totals_human(deposited_by_asset(vouchers)),
                f"deposited, {_n(n_notes, 'note')}, {days}",
                "",
                n_notes,
            ),
            figure("searched", f"{searched:,}", "withdrawals searched in the window", "", searched),
            figure(
                "candidates",
                n_addr,
                cand_label,
                "lead" if kinds["lead"] else ("ctx" if kinds["context"] else ""),
            ),
            figure(
                "leads",
                kinds["lead"],
                f"{'lead' if kinds['lead'] == 1 else 'leads'} above the threshold "
                "(strong or moderate band)",
                "lead" if kinds["lead"] else "",
            ),
        ],
        flow=flow,
        legend=legend_html(
            [
                s
                for s in ("strong", "lead", "context", "chance")
                if s in {a["status"] for a in shown}
            ],
            extra,
        ),
        table=table_html(
            [
                ("#", "5%"),
                ("Address", "45%"),
                ("Pool", "11%"),
                ("Withdrawals (UTC)", "25%"),
                ("Status", "14%"),
            ],
            rows,
        )
        if rows
        else "",
        more=_more_note(uid, n_addr, rest, "addresses")
        if cands
        else "No candidate: no count match narrows its field enough to count.",
        checks=[
            _placebo_check(data.get("placebo")),
            data_check(data, unresolved),
            *fresh_check,
            *method_checks(),
        ],
        toc=[
            ("deps", "Tornado.Cash deposits", "the depositor's deposits, D1, D2, …"),
            ("leads", "Candidates", "each address with its evidence and transactions"),
            ("method", "Method and limitations", "how the signals count, placebo, limits"),
            ("provenance", "Provenance and hashes", "tool, sources, all hashes, JSON SHA-256"),
        ],
    )
    return page1_html(uid, p)


def _plain_cells(prov: dict) -> list[str]:
    return provenance_cells(prov, "research use; leads, not proof")


def _ellipsis_rows(items: list, noun: str) -> str:
    return f"+{len(items) - TOP_N} more {noun} below." if len(items) > TOP_N else ""


# --- multi ---------------------------------------------------------------------------


def multi_page1(
    corr: dict,
    network,
    uid: str,
    *,
    answer: str,
    lead_rows: list,
    weak_rows: list,
    n_multi_profiles: int,
    n_single_profiles: int,
    warnings: list[str],
    n_funders: int = 0,
    group_note: str = "",
) -> str:
    """The first page of a multi-wallet correlation report: the wallets, the pools they
    used and the addresses they share."""
    from .honest import provenance as _provenance
    from .report_csv import _fmt_hours

    results = corr.get("results", {})
    wallets = list(results)
    clusters = corr.get("operator_clusters", [])
    strong = corr.get("strong", [])
    addr_detail = corr.get("addr_detail", {}) or {}
    n_strong = sum(1 for t in lead_rows if t[2]["band"] == "strong")
    net_name, window = network.name, ""
    for data in results.values():
        params = data.get("params", {})
        net_name = params.get("network", network.name)
        exit_h = params.get("exit_window_hours")
        window = (
            f"exit window {_fmt_hours(exit_h)} h"
            if exit_h
            else f"{params.get('window_days', 30)}-day window"
        )
        break
    pools = sorted({v["pool_key"] for d in results.values() for v in d.get("vouchers") or []})

    shared: list[tuple[str, list, str]] = []  # (address, wallets, status)
    seen = set()
    for addr, ws, grade in lead_rows:
        shared.append((addr, list(ws), "strong" if grade["band"] == "strong" else "lead"))
        seen.add(addr)
    for addr, ws, _grade in weak_rows:
        if addr not in seen:
            shared.append((addr, list(ws), "context"))
            seen.add(addr)
    for _pk, addr, ws in strong:
        if addr not in seen:
            shared.append((addr, list(ws), "chance"))
            seen.add(addr)
    pools_of: dict[str, dict] = {}
    for (pk, addr), by_wallet in addr_detail.items():
        pools_of.setdefault(addr, {})[pk] = sum(by_wallet.values())
    for pk, addr, _ws in strong:
        pools_of.setdefault(addr, {}).setdefault(pk, 0)

    shown_w = wallets[:TOP_N]
    left, mid, right, edges = [], [], [], []
    notes_by_pool = Counter()
    for i, w in enumerate(shown_w, 1):
        notes = Counter()
        for v in results[w].get("vouchers") or []:
            notes[v["pool_key"]] += v["count"]
        left.append(
            {
                "id": f"w:{w}",
                "style": "box",
                "title": w,
                "lines": [
                    (f"Wallet {i}", "t b"),
                    (short_addr(w), "mono"),
                    (_n(sum(notes.values()), "note"), "m"),
                ],
            }
        )
        for k, n in notes.items():
            notes_by_pool[k] += n
            edges.append({"a": f"w:{w}", "b": f"p:{k}", "w": n, "cls": "dep"})
    for k in pools:
        mid.append(
            {
                "id": f"p:{k}",
                "style": "hub",
                "title": f"{k} pool",
                "lines": [(f"{k} pool", "t b"), (_n(notes_by_pool[k], "note"), "m")],
            }
        )
    for i, (addr, ws, st) in enumerate(shared[:TOP_N], 1):
        right.append(
            {
                "id": f"s:{i}",
                "num": i,
                "status": st,
                "label": f"links {len(ws)} wallets",
                "sub": STATUS_LABEL[st],
                "title": addr,
            }
        )
        by_pool = pools_of.get(addr) or {}
        if by_pool:
            for pk, hits in by_pool.items():
                edges.append({"a": f"p:{pk}", "b": f"s:{i}", "w": hits or None, "cls": st})
        else:
            for w in ws:
                edges.append({"a": f"w:{w}", "b": f"s:{i}", "w": None, "cls": st})
    flow = ""
    if shown_w and pools:
        flow = flow_svg(
            left, mid, right, edges, "Flow from the wallets through the pools to shared addresses"
        )
    rows = []
    for i, (addr, ws, st) in enumerate(shared[:TOP_N], 1):
        rows.append(
            [
                num_cell(i),
                mono_cell(addr),
                num_cell(len(ws)),
                text_cell(_e(", ".join(sorted(pools_of.get(addr) or {})) or "-")),
                text_cell(chip(st)),
            ]
        )
    more = []
    if len(wallets) > TOP_N:
        more.append(f"The diagram shows the first {TOP_N} of {len(wallets)} wallets.")
    if len(shared) > TOP_N:
        more.append(f"+{len(shared) - TOP_N} more shared addresses in the sections below.")
    # An operator cluster is not a band lead: it gets its own word, never "Lead".
    word = (
        "Strong lead"
        if n_strong
        else ("Lead" if lead_rows else ("Operator cluster" if clusters else "No lead"))
    )
    reason = answer
    if lead_rows:
        addr, ws, grade = lead_rows[0]
        extra = (
            f" (independent: {', '.join(grade['independent'])})" if grade.get("independent") else ""
        )
        reason += (
            f" The first, {addr}, received the full deposit fingerprint of {len(ws)} wallets "
            f"and is graded {grade['band']}{extra}."
        )
    elif weak_rows:
        reason += (
            f" {_cap(_n(len(weak_rows), 'consolidator'))} matched only inside a shared window "
            "(window-prone): context, not a lead."
        )
    if n_single_profiles:
        reason += (
            f" {n_single_profiles} single-pool profile "
            f"{'match' if n_single_profiles == 1 else 'matches'}: a count match in one pool, "
            "chance-level."
        )
    if strong:
        reason += (
            f" {_cap(_n(len(strong), 'shared count-matched candidate'))}: chance-level, a "
            "count match that real depositors and decoy windows show equally often."
        )
    if n_funders:
        reason += (
            f" {_cap(_n(n_funders, 'shared funder'))} of two or more depositors: context, "
            "not scored."
        )
    checks = [check("warn", w) for w in warnings]
    if group_note:
        checks.append(check("info", group_note))
    checks += [data_check(corr), *method_checks()]
    p = {
        "kicker": "Tornado.Cash multi-wallet correlation, research use",
        "title_html": f"{_e(len(wallets))} wallets",
        "sub_html": " · ".join(
            x for x in (_e(display_name(net_name)), _e(", ".join(pools)), _e(window)) if x
        ),
        "right": _plain_cells(_provenance(network, command="not recorded")),
        "word": word,
        "word_key": "band",
        "word_value": "strong"
        if n_strong
        else ("moderate" if lead_rows else ("cluster" if clusters else "none")),
        "counts": [
            ("strong", n_strong, "strong"),
            ("moderate", len(lead_rows) - n_strong, "lead"),
            ("context", len(weak_rows) + n_multi_profiles + n_funders, "context"),
            ("chance", len(strong) + n_single_profiles, "chance-level"),
        ],
        "reason": reason,
        "figures": [
            figure("wallets", len(wallets), "wallets analysed"),
            figure("clusters", len(clusters), "operator clusters on discriminating evidence"),
            figure("shared", len(shared), "addresses shared by two or more wallets"),
            figure(
                "leads",
                len(lead_rows),
                "consolidator leads (strong or moderate)",
                "lead" if lead_rows else "",
            ),
        ],
        "flow": flow,
        "legend": legend_html(
            [
                s
                for s in ("strong", "lead", "context", "chance")
                if s in {x[2] for x in shared[:TOP_N]}
            ],
            ["line width = number of notes or withdrawals"],
        )
        if flow
        else "",
        "table_title": "Shared addresses",
        "table": table_html(
            [
                ("#", "5%"),
                ("Address", "47%"),
                ("Wallets", "10%"),
                ("Pools", "22%"),
                ("Status", "16%"),
            ],
            rows,
        )
        if rows
        else "",
        "more": " ".join(more) or ("" if rows else "No address is shared by two or more wallets."),
        "checks": checks,
        "toc": [
            ("leads", "Leads", "cross-wallet consolidators with their grades"),
            ("context", "Context", "window-prone links, deposit synchronicity"),
            ("groups", "Groups and clusters", "operator clusters, the group view"),
            ("method", "Method and limitations", "how the links are graded, the limits"),
        ],
    }
    return page1_html(uid, p)


# --- trace ---------------------------------------------------------------------------

_TONE_STATUS = {
    "fail": "fail",
    "warn": "warn",
    "bridge": "bridge",
    "ok": "labelled",
    "ctx": "end",
}


def _trace_status(reason: str) -> str:
    from .report_html import end_state
    from .trace import BELOW_THRESHOLD

    tone, _label = end_state(reason)
    if tone == "chance":
        return "below" if reason == BELOW_THRESHOLD else "chance"
    return _TONE_STATUS.get(tone, "end")


def trace_page1(result: dict, network, uid: str, destinations, answer: str) -> str:
    """The first page of a multi-hop trace: start, hops, end states."""
    from .honest import provenance as _provenance
    from .report_html import end_state, failed_parts, trace_totals
    from .trace import BELOW_THRESHOLD, LOOKUP_FAILED, arrival_text

    edges, terminals = result["edges"], result.get("terminals") or []
    statuses = [_trace_status(t["reason"]) for t in terminals]
    n_below = sum(1 for t in terminals if t["reason"] == BELOW_THRESHOLD)
    n_fail = statuses.count("fail")
    n_end = len(terminals) - n_below - n_fail
    max_hop = max((e["hop"] for e in edges), default=0)
    failed, cut = failed_parts(result)
    order = sorted(range(len(terminals)), key=lambda i: -terminals[i]["amount"])
    shown = order[:TOP_N]
    left = [
        {
            "id": "start",
            "style": "box",
            "title": result["start"],
            "lines": [
                ("Start", "t b"),
                (short_addr(result["start"]), "mono"),
                (f"{fmt_amount(result['amount'])} {result['asset']}", "m"),
            ],
        }
    ]
    hops = sorted({terminals[i]["hop"] for i in shown})
    mid = []
    for h in hops:
        at = [i for i in shown if terminals[i]["hop"] == h]
        mid.append(
            {
                "id": f"h:{h}",
                "style": "hub",
                "title": f"hop {h}",
                "lines": [(f"Hop {h}", "t b"), (_n(len(at), "stop"), "m")],
            }
        )
    right, fedges = [], []
    by_hop = Counter()
    for num, i in enumerate(shown, 1):
        t = terminals[i]
        by_hop[t["hop"]] += t["amount"]
        _tone, short = end_state(t["reason"])
        right.append(
            {
                "id": f"t:{num}",
                "num": num,
                "status": statuses[i],
                "label": f"{fmt_amount(t['amount'])} {t['asset']}",
                "sub": short,
                "title": f"{t['address']} — {t['reason']}",
            }
        )
        fedges.append({"a": f"h:{t['hop']}", "b": f"t:{num}", "w": t["amount"], "cls": statuses[i]})
    for h in hops:
        fedges.append({"a": "start", "b": f"h:{h}", "w": by_hop[h], "cls": "dep"})
    flow = (
        flow_svg(left, mid, right, fedges, "Flow from the start address over the hops to the stops")
        if shown
        else ""
    )
    rows = []
    for num, i in enumerate(shown, 1):
        t = terminals[i]
        _tone, short = end_state(t["reason"])
        reason = (
            ""
            if t["reason"] in (short, LOOKUP_FAILED, BELOW_THRESHOLD)
            else f'<div class="meta">{_e(t["reason"])}</div>'
        )
        rows.append(
            [
                num_cell(num),
                mono_cell(t["address"]),
                num_cell(f"{fmt_amount(t['amount'])} {t['asset']}"),
                num_cell(t["hop"]),
                text_cell(chip(statuses[i], short) + reason),
            ]
        )
    totals = trace_totals(result)
    reason = answer
    if totals:
        reason += (
            " "
            + "; ".join(
                f"End points {r['endpoints']:g} + below threshold {r['below']:g} + failed lookups "
                f"{r['failed']:g} = {r['total']:g} {r['asset']}"
                for r in totals
            )
            + "."
        )
    checks = [data_check(result)]
    bridged = sum(1 for e in edges if e["kind"] == "bridge")
    swaps = sum(1 for e in edges if e["kind"] == "swap")
    if bridged or swaps:
        checks.append(
            check(
                "info",
                _e(
                    f"Asset changes: {_n(bridged, 'bridge edge')} and {_n(swaps, 'swap')}; "
                    "totals are per asset."
                ),
            )
        )
    if arrival_text(result):
        checks.append(check("info", _e("Arrival: " + arrival_text(result) + ".")))
    checks.append(
        check(
            "info",
            "FIFO: each edge says where the traced amount would go under first-in-first-out; "
            "[lower – upper] in Hops gives what any ordering allows.",
        )
    )
    checks += method_checks()
    word = "Partly traced" if (n_fail or failed or cut) else "Traced"
    p = {
        "kicker": "Multi-hop trace of withdrawn funds, FIFO attribution, research use",
        "title_html": f'Start <span class="mono">{_e(result["start"])}</span>',
        "sub_html": f"{_e(fmt_amount(result['amount']))} {_e(result['asset'])} · "
        f"{_e(display_name(network.name))}",
        "right": _plain_cells(_provenance(network, command="not recorded")),
        "word": word,
        "counts": [
            ("endpoints", n_end, ("end point", "end points")),
            ("below_count", n_below, "below threshold"),
            ("failed_ends", n_fail, "at a failed lookup"),
        ],
        "reason": reason,
        "figures": [
            figure(
                "amount",
                f"{fmt_amount(result['amount'])} {result['asset']}",
                "traced from the start",
                "",
                result["amount"],
            ),
            figure(
                "stops",
                len(terminals),
                f"stops within {_n(max_hop, 'hop')}: {_n(n_end, 'end point')}, {n_below} below "
                f"threshold, {n_fail} at a failed lookup",
            ),
            figure("edges", len(edges), f"edges: {_n(swaps, 'swap')}, {_n(bridged, 'bridge')}"),
            figure(
                "expanded",
                result.get("nodes_expanded", 0),
                "addresses expanded (bounds the API calls)",
            ),
        ],
        "flow": flow,
        "flow_title": "Where the money went",
        "legend": legend_html(
            [
                s
                for s in ("end", "labelled", "bridge", "warn", "below", "chance", "fail")
                if s in {statuses[i] for i in shown}
            ],
            ["line width = amount"],
        )
        if flow
        else "",
        "table_title": "Stops",
        "table": table_html(
            [
                ("#", "5%"),
                ("Address", "45%"),
                ("Amount", "17%"),
                ("Hop", "7%"),
                ("End state", "26%"),
            ],
            rows,
        )
        if rows
        else "",
        "more": f"+{len(terminals) - TOP_N} more stops in Where the traced funds stop."
        if len(terminals) > TOP_N
        else ("" if rows else "The traced amount was fully attributed along the edges."),
        "checks": checks,
        "toc": [
            ("leads", "Where the funds stop", "every stop with its end state"),
            ("context", "Hops", "every edge with its FIFO bounds and transaction"),
            ("groups", "Flow diagram", "all hops as a diagram"),
            ("method", "Method and limitations", "FIFO, end states, limits"),
        ],
    }
    return page1_html(uid, p)


# --- characterize --------------------------------------------------------------------


def characterize_page1(info: dict, network, uid: str, answer: str) -> str:
    """The first page of an exit-candidate characterisation: pools → address → next hops."""
    from .attribution import format_label
    from .deposit_addresses import check_text
    from .honest import provenance as _provenance

    addr = info["address"]
    inflows = info.get("pool_inflows") or []
    by_pool = Counter(r["pool_key"] for r in inflows)
    hops = info.get("top_next_hops") or []
    totals = ", ".join(f"{fmt_amount(v)} {a}" for a, v in sorted(info["inflow_totals"].items()))
    left = [
        {
            "id": f"p:{k}",
            "style": "hub",
            "title": f"{k} pool",
            "lines": [(f"{k} pool", "t b"), (_n(n, "inflow"), "m")],
        }
        for k, n in by_pool.most_common(TOP_N)
    ]
    mid = [
        {
            "id": "a",
            "style": "box",
            "title": addr,
            "lines": [
                ("Address", "t b"),
                (short_addr(addr), "mono"),
                (info["classification"], "m"),
            ],
        }
    ]
    edges = [{"a": f"p:{k}", "b": "a", "w": n, "cls": "dep"} for k, n in by_pool.most_common(TOP_N)]
    right, rows = [], []
    statuses = []
    for i, h in enumerate(hops[:TOP_N], 1):
        tag = format_label(h.get("label")) if h.get("label") else ""
        st = "labelled" if tag else "node"
        statuses.append(st)
        total = (
            f"contract call, no {network.currency}"
            if h.get("kind") == "call"
            else f"{fmt_amount(h.get('total_value', 0))} {network.currency}"
        )
        right.append(
            {
                "id": f"h:{i}",
                "num": i,
                "status": st,
                "label": f"{_n(h['count'], 'transaction')}",
                "sub": tag or total,
                "title": h["address"],
            }
        )
        edges.append({"a": "a", "b": f"h:{i}", "w": h["count"], "cls": st})
        rows.append(
            [
                num_cell(i),
                mono_cell(h["address"]),
                num_cell(h["count"]),
                num_cell(total),
                text_cell(chip("labelled", tag) if tag else "-"),
            ]
        )
    flow = ""
    if left or right:
        if not left:  # no pool inflow: the address alone on the left
            mid[0]["id"] = "a"
        flow = flow_svg(
            left, mid, right, edges, "Flow from the pools through the address to its next hops"
        )
    checks = [data_check(info)]
    if info.get("label"):
        checks.append(check("info", _e(f"Known address: {format_label(info['label'])}.")))
    if info.get("disposable"):
        checks.append(
            check(
                "info",
                "Disposable: first activity at most a day before the first pool inflow (context, not proof).",
            )
        )
    if info.get("deposit_check"):
        checks.append(
            check("info", _e(f"Deposit-address check: {check_text(info['deposit_check'])}."))
        )
    checks += method_checks()
    p = {
        "kicker": "Tornado.Cash exit-candidate characterisation, recipient side, research use",
        "title_html": f'Address <span class="mono">{_e(addr)}</span>',
        "sub_html": _e(display_name(network.name)),
        "right": _plain_cells(_provenance(network, command="not recorded")),
        "word": _cap(info["classification"]),
        "counts": [
            ("pool_inflows", len(inflows), ("pool inflow", "pool inflows")),
            ("pools", len(info.get("distinct_pools") or []), ("pool", "pools")),
            ("hops", len(hops), ("next hop", "next hops")),
        ],
        "reason": f"{answer} {_cap(info.get('classification_reason', ''))}.".replace("..", "."),
        "figures": [
            figure(
                "inflows",
                len(inflows),
                f"pool inflows from {_n(len(info.get('distinct_pools') or []), 'pool')}",
            ),
            figure("total_in", totals or "0", "received from known pools", "", totals or 0),
            figure(
                "activity",
                info.get("normal_txs", 0),
                f"normal transactions, {info.get('internal_txs', 0)} internal",
            ),
            figure("next_hops", len(hops), "destinations listed"),
        ],
        "flow": flow,
        "legend": legend_html(
            [s for s in ("labelled", "node") if s in statuses], ["line width = number of transfers"]
        )
        if flow
        else "",
        "table_title": "Next hops",
        "table": table_html(
            [
                ("#", "5%"),
                ("Destination", "47%"),
                ("Transfers", "11%"),
                ("Total", "17%"),
                ("Attribution", "20%"),
            ],
            rows,
        )
        if rows
        else "",
        "more": f"+{len(hops) - TOP_N} more next hops in Dominant next hops."
        if len(hops) > TOP_N
        else ("" if rows else "No outgoing transfer found."),
        "checks": checks,
        "toc": [
            ("context", "Classification", "why this class, label, deposit-address check"),
            ("deposits", "Dominant next hops", "where the address sends funds"),
            ("method", "Method and limitations", "the limits of every result"),
            ("inflows", "Tornado-pool inflows", "every pool withdrawal received"),
        ],
    }
    return page1_html(uid, p)


# --- classify-address ----------------------------------------------------------------

KIND_WORD = {
    "exchange_deposit": "Exchange deposit",
    "service_deposit": "Service deposit",
    "deposit_like": "Deposit-like",
    "forwarder": "Forwarder",
    "smart_account": "Smart account",
    "unknown": "Unknown",
    "not_deposit": "Not a deposit address",
}


def classify_page1(info: dict, network, uid: str, answer: str, targets: list, labels) -> str:
    """The first page of a deposit-address check: senders → address → sweep targets."""
    from .attribution import format_label
    from .deposit_addresses import NOT_DEPOSIT, UNKNOWN_KIND
    from .honest import provenance as _provenance

    addr = info["address"]
    kind = info.get("kind") or "exchange_deposit"
    senders = info.get("senders") or []
    is_deposit = kind not in (NOT_DEPOSIT, UNKNOWN_KIND)
    shown_s = senders[:TOP_N] if is_deposit else []
    word = KIND_WORD.get(kind, _cap(kind.replace("_", " ")))
    role = "operator" if info.get("operator") else "swept to"
    left = [
        {
            "id": f"s:{i}",
            "style": "box",
            "title": s,
            "lines": [(f"Sender S{i}", "t b"), (short_addr(s), "mono")],
        }
        for i, s in enumerate(shown_s, 1)
    ]
    if info.get("gas_funder") and is_deposit:
        left.append(
            {
                "id": "g",
                "style": "cp",
                "title": info["gas_funder"],
                "lines": [("Gas funder", "t b"), (short_addr(info["gas_funder"]), "mono")],
            }
        )
    mid = [
        {
            "id": "a",
            "style": "hub",
            "title": addr,
            "lines": [(word, "t b"), (short_addr(addr), "mono")],
        }
    ]
    edges = [{"a": f"s:{i}", "b": "a", "w": None, "cls": "dep"} for i in range(1, len(shown_s) + 1)]
    if info.get("gas_funder") and is_deposit:
        edges.append({"a": "g", "b": "a", "w": None, "cls": "cp", "label": "paid the gas"})
    right, rows, statuses = [], [], []
    for i, t in enumerate(targets[:TOP_N], 1):
        tag = format_label(labels.get(t)) if labels and labels.get(t) else ""
        st = "labelled" if tag else "node"
        statuses.append(st)
        right.append(
            {
                "id": f"t:{i}",
                "num": i,
                "status": st,
                "label": role,
                "sub": tag or "unlabelled",
                "title": t,
            }
        )
        edges.append({"a": "a", "b": f"t:{i}", "w": None, "cls": st})
        rows.append(
            [
                num_cell(i),
                mono_cell(t),
                text_cell(_e(role)),
                text_cell(chip("labelled", tag) if tag else "unlabelled"),
            ]
        )
    for i, s in enumerate(shown_s, 1):
        rows.append([num_cell(f"S{i}"), mono_cell(s), text_cell("sender"), text_cell("-")])
    if info.get("gas_funder") and is_deposit:
        rows.append(
            [num_cell("G"), mono_cell(info["gas_funder"]), text_cell("gas funder"), text_cell("-")]
        )
    flow = ""
    if is_deposit and (left or right):
        flow = flow_svg(
            left, mid, right, edges, "Senders, the checked address and where it sweeps to"
        )
    p = {
        "kicker": "Tornado.Cash deposit-address check, address behaviour, research use",
        "title_html": f'Address <span class="mono">{_e(addr)}</span>',
        "sub_html": _e(display_name(network.name)),
        "right": _plain_cells(_provenance(network, command="not recorded")),
        "word": word,
        "counts": [
            ("sender_count", len(senders), ("sender", "senders")),
            ("target_count", len(targets), role),
        ],
        "reason": _cap(answer),
        "figures": [
            figure(
                "senders",
                len(senders),
                f"{'sender' if len(senders) == 1 else 'senders'} whose asset it sweeps",
            ),
            figure("targets", len(targets), f"{role} address{'es' if len(targets) != 1 else ''}"),
            figure(
                "gas_funder",
                "yes" if info.get("gas_funder") else "none",
                "gas funder before the first sweep",
                "",
                info.get("gas_funder") or "",
            ),
            figure("status", "context", "not scored: behaviour, not identity"),
        ],
        "flow": flow,
        "flow_title": "How funds pass through",
        "legend": legend_html([s for s in ("labelled", "node") if s in statuses]) if flow else "",
        "table": table_html(
            [("#", "6%"), ("Address", "50%"), ("Role", "16%"), ("Label", "28%")], rows
        )
        if rows
        else "",
        "more": f"+{len(senders) - TOP_N} more senders in Sweeps and senders."
        if is_deposit and len(senders) > TOP_N
        else "",
        "checks": [
            data_check(info),
            check(
                "info",
                "Context, not scored: two addresses that sent to the same deposit address are "
                "context only, never a lead (rule 2.21).",
            ),
            *method_checks(),
        ],
        "toc": [
            ("context", "Result", "the classification and what it means"),
            ("deposits", "Sweeps and senders", "every sender and sweep target"),
            ("method", "Method and limitations", "the limits of every result"),
        ],
    }
    return page1_html(uid, p)


# --- cluster (split-exit reconvergence) ----------------------------------------------


def cluster_page1(traces: list[dict], network, uid: str, answer: str, merged: dict) -> str:
    """The first page of a split-exit cluster report: wallets → pool layers →
    reconvergence points Z (one hop)."""
    from .honest import provenance as _provenance
    from .report_csv import format_totals

    all_z = [(t["wallet"], c) for t in traces for c in t.get("clusters", [])]
    all_z.sort(key=lambda wc: (-wc[1]["n_layers"], -len(wc[1]["totals"]), wc[1]["z"]))
    shown = all_z[:TOP_N]
    wallets = [t["wallet"] for t in traces]
    left, mid, right, edges = [], [], [], []
    pools = []
    for t in traces[:TOP_N]:
        fp = t.get("fingerprint") or {}
        left.append(
            {
                "id": f"w:{t['wallet']}",
                "style": "box",
                "title": t["wallet"],
                "lines": [
                    ("Wallet", "t b"),
                    (short_addr(t["wallet"]), "mono"),
                    (_n(sum(fp.values()), "note"), "m"),
                ],
            }
        )
        for k, n in fp.items():
            if k not in pools:
                pools.append(k)
            edges.append({"a": f"w:{t['wallet']}", "b": f"p:{k}", "w": n, "cls": "dep"})
    for _wallet, c in shown:
        for k in c["pools_covered"]:
            if str(k) not in pools:
                pools.append(str(k))
    mid = [
        {"id": f"p:{k}", "style": "hub", "title": f"{k} pool", "lines": [(f"{k} pool", "t b")]}
        for k in pools
    ]
    for i, (_wallet, c) in enumerate(shown, 1):
        right.append(
            {
                "id": f"z:{i}",
                "num": i,
                "status": "node",
                "label": f"fed by {c['n_layers']} pool layers",
                "sub": format_totals(c["totals"]),
                "title": c["z"],
            }
        )
        for k in c["pools_covered"]:
            edges.append({"a": f"p:{k}", "b": f"z:{i}", "w": None, "cls": "node"})
    rows = [
        [
            num_cell(i),
            mono_cell(c["z"]),
            mono_cell(wallet),
            num_cell(c["n_layers"]),
            text_cell(_e(format_totals(c["totals"]))),
        ]
        for i, (wallet, c) in enumerate(shown, 1)
    ]
    flow = (
        flow_svg(
            left, mid, right, edges, "Wallets, the pool layers and where split exits reconverge"
        )
        if right and mid
        else ""
    )
    more = []
    if len(traces) > TOP_N:
        more.append(f"The diagram shows the first {TOP_N} of {len(wallets)} wallets.")
    if len(all_z) > TOP_N:
        more.append(f"+{len(all_z) - TOP_N} more reconvergence points below.")
    p = {
        "kicker": "Tornado.Cash split-exit clusters, one-hop reconvergence, research use",
        "title_html": f"{_e(len(traces))} {'wallet' if len(traces) == 1 else 'wallets'}",
        "sub_html": _e(display_name(network.name)),
        "right": _plain_cells(_provenance(network, command="not recorded")),
        "word": "Reconvergence" if all_z else "No reconvergence",
        "counts": [
            ("z_points", len(all_z), ("reconvergence point", "reconvergence points")),
            ("wallet_count", len(traces), ("wallet", "wallets")),
        ],
        "reason": answer
        + (
            " A point fed by two or more pool layers is where a wallet's split exits may "
            "reconverge; a busy service (an exchange hot wallet, a DEX) can also collect "
            "unrelated exits, so it is a structural lead to corroborate."
            if all_z
            else " The exits may reconverge beyond one hop, which this tracer does not follow."
        ),
        "figures": [
            figure("wallets", len(traces), "wallets traced for split exits"),
            figure(
                "leads",
                len(all_z),
                "reconvergence points fed by 2+ pool layers",
                "lead" if all_z else "",
            ),
            figure("pools", len(pools), "pools in the wallets' fingerprints"),
            figure(
                "layers",
                max((c["n_layers"] for _w, c in all_z), default=0),
                "most pool layers into one point",
            ),
        ],
        "flow": flow,
        "legend": legend_html(["node"], ["line width = number of notes"]) if flow else "",
        "table_title": "Reconvergence points",
        "table": table_html(
            [
                ("#", "5%"),
                ("Point Z", "36%"),
                ("Wallet", "36%"),
                ("Layers", "8%"),
                ("Value", "15%"),
            ],
            rows,
        )
        if rows
        else "",
        "more": " ".join(more)
        or ("" if rows else "No downstream address is fed by two or more pool layers."),
        "checks": [
            data_check(merged),
            check(
                "info",
                "One hop: a wallet whose exits reconverge only after several hops shows no "
                "point here; totals are per asset, never summed across assets.",
            ),
            *method_checks(),
        ],
        "toc": [
            ("leads", "Reconvergence points", "each point with its pool layers and value"),
            ("groups", "Per-wallet clusters", "every wallet's points"),
            ("method", "Method and limitations", "how to read a one-hop trace, the limits"),
        ],
    }
    return page1_html(uid, p)
