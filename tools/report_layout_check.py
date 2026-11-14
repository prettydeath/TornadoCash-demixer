"""Layout check of HTML reports in a real browser (Playwright + Chromium).

For each report it opens every section and looks, on screen (1280 and 1440 px wide)
and in print (A4 content width, print media), for:

* the page scrolling sideways, or an element reaching past the right edge of the page;
* an element whose content is cut off (overflow hidden/clip with a wider content);
* a chip, link or text in a table cell sticking out of its cell;
* text of two cells of one table row overlapping.

It also checks every inline SVG: each text inside the viewBox and inside its own box,
no two texts overlapping, no edge label on a thick stroke; and the blocks of the first
page (header cells, verdict, figures, checks) for content sticking out.

In print it measures the first page (``.p1``) against the A4 content height. That is the
height of the element laid out with print media at the A4 content width, not a count of
PDF pages; ``--pdf`` writes the paginated PDFs for a look.

    python tools/report_layout_check.py [--shots DIR] [--pdf DIR] report.html ...

Prints a JSON list of issues and exits 0 when there are none, 1 when there are, and
77 when Playwright or its Chromium is not installed (a test treats that as a skip).
Pages are opened from ``file://`` URLs; a report loads nothing else.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

SCREEN_WIDTHS = (1280, 1440)
# A4 at 96 dpi minus the report's @page margins (12 mm top, 11 mm sides, 14 mm bottom).
PRINT_WIDTH = round((210 - 22) / 25.4 * 96)
PRINT_HEIGHT = round((297 - 26) / 25.4 * 96)

CHECK_JS = r"""
() => {
  const issues = [];
  const doc = document.documentElement;
  const vw = doc.clientWidth;
  const label = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\s+/).join('.');
    return s;
  };
  const text = el => (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const shown = el => {
    for (let e = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      if (e.classList && (e.classList.contains('sr') || e.classList.contains('cpx'))) return false;
    }
    return true;
  };
  // inside a container that scrolls on purpose (overflow-x auto/scroll)
  const inScroller = el => {
    for (let e = el.parentElement; e && e !== doc; e = e.parentElement) {
      const ox = getComputedStyle(e).overflowX;
      if (ox === 'auto' || ox === 'scroll') return true;
    }
    return false;
  };
  if (doc.scrollWidth > vw + 1)
    issues.push({kind: 'page-scrolls-sideways', scrollWidth: doc.scrollWidth, width: vw});
  const all = Array.from(document.body.querySelectorAll('*')).filter(shown);
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (r.right > vw + 1 && !inScroller(el))
      issues.push({kind: 'past-right-edge', el: label(el), right: Math.round(r.right), width: vw, text: text(el)});
    const cs = getComputedStyle(el);
    const clips = ['hidden', 'clip'].includes(cs.overflowX);
    if (clips && el.scrollWidth > el.clientWidth + 1 && !['svg'].includes(el.tagName.toLowerCase()))
      issues.push({kind: 'clipped', el: label(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, text: text(el)});
  }
  const CELLS = 'td,th,.p1-figs>div,.p1-head .l,.p1-head .r>div,.p1-word,.p1 .answer,' +
    '.p1-checks li,.p1-toc a';
  for (const cell of document.querySelectorAll(CELLS)) {
    if (!shown(cell)) continue;
    const c = cell.getBoundingClientRect();
    if (!c.width) continue;
    if (cell.scrollWidth > cell.clientWidth + 1)
      issues.push({kind: 'cell-overflow', el: label(cell), scrollWidth: cell.scrollWidth, clientWidth: cell.clientWidth, text: text(cell)});
    for (const ch of cell.querySelectorAll('*')) {
      if (!shown(ch)) continue;
      const r = ch.getBoundingClientRect();
      if (!r.width) continue;
      if (r.right > c.right + 1 || r.left < c.left - 1)
        issues.push({kind: 'sticks-out-of-cell', el: label(ch), cell: label(cell), text: text(ch)});
    }
  }
  // text of different cells of one row must not overlap
  const rects = node => {
    const out = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      if (!t.textContent.trim() || !shown(t.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(t);
      for (const r of range.getClientRects()) if (r.width && r.height) out.push(r);
    }
    return out;
  };
  for (const row of document.querySelectorAll('tr')) {
    if (!shown(row)) continue;
    const cells = Array.from(row.children).filter(shown).map(rects);
    for (let i = 0; i < cells.length; i++)
      for (let j = i + 1; j < cells.length; j++)
        for (const a of cells[i]) for (const b of cells[j])
          if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) {
            issues.push({kind: 'text-overlap', row: text(row).slice(0, 60), cells: [i, j]});
            i = cells.length; j = cells.length; break;
          }
  }
  // SVG: every text inside the viewBox and inside its own box, no two texts overlapping,
  // no edge label sitting on a thick stroke (a halo cannot make that legible)
  const inside = (b, r) => b.x >= r.x - 0.5 && b.y >= r.y - 0.5 &&
    b.x + b.width <= r.x + r.width + 0.5 && b.y + b.height <= r.y + r.height + 0.5;
  for (const svg of document.querySelectorAll('svg')) {
    if (!shown(svg)) continue;
    const vb = svg.viewBox && svg.viewBox.baseVal;
    if (!vb || vb.width < 50) continue;  // legend swatches
    const texts = Array.from(svg.querySelectorAll('text')).filter(t => t.textContent.trim());
    const boxes = texts.map(t => t.getBBox());
    texts.forEach((t, i) => {
      const b = boxes[i], what = t.textContent.trim().slice(0, 60);
      if (!inside(b, vb)) issues.push({kind: 'svg-text-outside-viewbox', text: what});
      const g = t.parentElement;
      const rect = g && g.tagName.toLowerCase() === 'g' ? g.querySelector(':scope > rect.box, :scope > rect.hub, :scope > rect.cp') : null;
      if (rect && !inside(b, rect.getBBox())) issues.push({kind: 'svg-text-outside-box', text: what});
    });
    // glyph boxes include ascent and descent: trim 2 units off top and bottom
    const core = b => ({l: b.x, r: b.x + b.width, t: b.y + 2, btm: b.y + b.height - 2});
    for (let i = 0; i < texts.length; i++)
      for (let j = i + 1; j < texts.length; j++) {
        const a = core(boxes[i]), c = core(boxes[j]);
        if (a.l < c.r - 0.5 && c.l < a.r - 0.5 && a.t < c.btm && c.t < a.btm)
          issues.push({kind: 'svg-text-overlap', text: [texts[i].textContent.trim(), texts[j].textContent.trim()].join(' | ').slice(0, 90)});
      }
    const thick = Array.from(svg.querySelectorAll('path')).filter(
      p => parseFloat(p.getAttribute('stroke-width') || '0') >= 4);
    texts.forEach((t, i) => {
      if (!t.classList.contains('lbl')) return;
      const b = boxes[i];
      const pts = [[0.5, 0.5], [0.1, 0.5], [0.9, 0.5], [0.3, 0.3], [0.7, 0.7]].map(
        ([fx, fy]) => new DOMPoint(b.x + fx * b.width, b.y + fy * b.height));
      if (thick.some(p => pts.some(pt => p.isPointInStroke(pt))))
        issues.push({kind: 'svg-label-on-thick-stroke', text: t.textContent.trim()});
    });
  }
  return issues;
}
"""

P1_JS = r"""
() => Array.from(document.querySelectorAll('.p1')).map(p => Math.round(p.getBoundingClientRect().height))
"""


def _open_all(page) -> None:
    page.evaluate("() => document.querySelectorAll('details').forEach(d => { d.open = true; })")


def check_file(browser, path: pathlib.Path, shots: pathlib.Path | None, pdf: pathlib.Path | None):
    url = path.resolve().as_uri()
    issues = []
    for width in SCREEN_WIDTHS:
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.goto(url)
        if shots:
            page.screenshot(path=str(shots / f"{path.stem}-{width}.png"), full_page=True)
        _open_all(page)
        if shots:
            page.screenshot(path=str(shots / f"{path.stem}-{width}-open.png"), full_page=True)
        for issue in page.evaluate(CHECK_JS):
            issues.append({"file": path.name, "view": f"screen {width}px", **issue})
        page.close()
    page = browser.new_page(viewport={"width": PRINT_WIDTH, "height": PRINT_HEIGHT})
    page.goto(url)
    page.emulate_media(media="print")
    _open_all(page)
    if shots:
        page.screenshot(path=str(shots / f"{path.stem}-print.png"), full_page=True)
    for issue in page.evaluate(CHECK_JS):
        issues.append({"file": path.name, "view": "print A4", **issue})
    for height in page.evaluate(P1_JS):
        if height > PRINT_HEIGHT:
            issues.append(
                {
                    "file": path.name,
                    "view": "print A4",
                    "kind": "first-page-too-tall",
                    "height": height,
                    "limit": PRINT_HEIGHT,
                }
            )
    if pdf:
        page.pdf(path=str(pdf / f"{path.stem}.pdf"), format="A4", print_background=True)
    page.close()
    return issues


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports", nargs="+", type=pathlib.Path)
    ap.add_argument("--shots", type=pathlib.Path, help="directory for full-page screenshots")
    ap.add_argument("--pdf", type=pathlib.Path, help="directory for A4 PDFs")
    args = ap.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed", file=sys.stderr)
        return 77
    for d in (args.shots, args.pdf):
        if d:
            d.mkdir(parents=True, exist_ok=True)
    issues = []
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # browser binary missing
            print(f"chromium is not available: {exc}", file=sys.stderr)
            return 77
        for path in args.reports:
            try:
                issues.extend(check_file(browser, path, args.shots, args.pdf))
            except Exception as exc:  # a crash or timeout is a finding, not a traceback
                issues.append({"file": path.name, "kind": "tool-error", "error": str(exc)[:500]})
        browser.close()
    print(json.dumps(issues, indent=1, ensure_ascii=False))
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
