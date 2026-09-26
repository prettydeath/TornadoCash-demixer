"""CSV and self-contained HTML reports for every analysis.

CSV files carry a ``network`` column and UTC timestamps. HTML reports are
single files with inline CSS, so they can be attached to a case as they are.
"""

from __future__ import annotations

import csv
import html
import json
import os
from collections import Counter
from datetime import datetime, timezone

from .attribution import format_label
from .demix import NATIVE_DENOM_TOLERANCE
from .heuristics import (
    BAND_ORDER,
    MAX_GAS_PRICE_SHARE,
    MIN_COUNT_DISCRIMINATION,
    MIN_FIELD_SIZE,
    band_rationale,
    candidate_reason,
    conclusion,
    cross_method,
    method_breakdown,
    ranked_candidates,
)
from .multi import SYNC_MAX_SPAN_HOURS
from .networks import Network
from .relayer import analyze_relayers, collect_withdrawals, self_relayed_candidates


def _ts(ts):
    """UNIX seconds -> 'YYYY-MM-DD HH:MM:SSZ' (UTC)."""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _fmt_hours(hours):
    """Drop a redundant '.0' so an integer hour count reads '6 h', not '6.0 h'."""
    try:
        f = float(hours)
    except (TypeError, ValueError):
        return str(hours)
    return str(int(f)) if f.is_integer() else str(f)


def format_span(seconds: float) -> str:
    """Human-readable duration: seconds -> '45 seconds' / '10 minutes' / '2.7 hours' / '3.1 days'."""
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} second{'s' if s != 1 else ''}"
    if s < 90 * 60:
        return f"{round(s / 60)} minutes"
    if s < 48 * 3600:
        return f"{s / 3600:.1f} hours"
    return f"{s / 86400:.1f} days"


def _write(path, header, rows):
    """Write a single CSV file and return its path.

    The encoding is explicit so the output does not depend on the Windows
    codepage. No BOM: the content is ASCII in practice (addresses, hashes, pool
    keys), and the package's own readers accept one anyway.
    """
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_sanitize_row(header))
        writer.writerows(_sanitize_row(r) for r in rows)
    return path


# Leading characters Excel and LibreOffice treat as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value):
    """Neutralise a cell that a spreadsheet would evaluate as a formula.

    Defence in depth: fields come from chain data or the registry, but the
    registry ``asset`` column is free text and these files get opened by
    double-click. A leading apostrophe is the conventional escape.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _sanitize_row(row):
    return [_sanitize_cell(v) for v in row]


def _ensure_dir(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _with_network(network, header, rows):
    """Prefix a CSV header and its rows with the network that produced them.

    An output file that cannot say which chain it came from cannot be used as
    evidence, and the per-pool filenames collide across chains without it.
    """
    return ["network"] + header, [[network.name] + row for row in rows]


def write_demix_csv(data: dict, out_dir: str, network: Network) -> list[str]:
    """Write single-wallet demix results as CSV files. Returns the file list."""
    _ensure_dir(out_dir)
    files = []

    # vouchers.csv
    header, rows = _with_network(
        network,
        ["pool", "denomination", "asset", "num_deposits", "first_deposit_utc", "last_deposit_utc"],
        [
            [
                v["pool_key"],
                v["denom"],
                v["asset"],
                v["count"],
                _ts(v["first_ts"]),
                _ts(v["last_ts"]),
            ]
            for v in data["vouchers"]
        ],
    )
    files.append(_write(os.path.join(out_dir, "vouchers.csv"), header, rows))

    # deposits.csv
    header, rows = _with_network(
        network,
        [
            "idx",
            "pool",
            "denomination",
            "asset",
            "timestamp_utc",
            "block",
            "to_address",
            "via",
            "tx_hash",
        ],
        [
            [
                i,
                d["pool_key"],
                d["denom"],
                d["asset"],
                _ts(d["ts"]),
                d["block"],
                d["to"],
                d["via"],
                d["hash"],
            ]
            for i, d in enumerate(data["deposits"], 1)
        ],
    )
    files.append(_write(os.path.join(out_dir, "deposits.csv"), header, rows))

    # candidates.csv (count-matched recipients across all pools)
    cand_rows = []
    for pool_key, res in data["denoms"].items():
        for n in res.get("target_counts", []):
            for addr in res["candidates_by_count"].get(n, []):
                records = res["detail"][addr]
                total = round(sum(x["value"] for x in records), 6)
                cand_rows.append(
                    [
                        pool_key,
                        res["denom"],
                        res["asset"],
                        n,
                        addr,
                        total,
                        _ts(min(x["ts"] for x in records)),
                        _ts(max(x["ts"] for x in records)),
                    ]
                )
    header, rows = _with_network(
        network,
        [
            "pool",
            "denomination",
            "asset",
            "match_count",
            "candidate_address",
            "total_received",
            "first_seen_utc",
            "last_seen_utc",
        ],
        cand_rows,
    )
    files.append(_write(os.path.join(out_dir, "candidates.csv"), header, rows))

    # withdrawals_<pool>.csv: every qualifying recipient for the pool
    for pool_key, res in data["denoms"].items():
        targets = set(res.get("target_counts", []))
        rows = []
        for addr, hits in res["counts"].items():
            records = res["detail"][addr]
            total = round(sum(x["value"] for x in records), 6)
            timestamps = sorted(x["ts"] for x in records)
            hashes = ";".join(x["hash"] for x in records[:4])
            # Event mode carries exact relayer/fee data; transfer mode does not.
            n_self = sum(1 for x in records if x.get("self_relayed"))
            fees = [x.get("fee") for x in records if x.get("fee") is not None]
            avg_fee = round(sum(fees) / len(fees), 8) if fees else ""
            rows.append(
                [
                    addr,
                    hits,
                    "yes" if hits in targets else "",
                    total,
                    _ts(timestamps[0]),
                    _ts(timestamps[-1]),
                    n_self,
                    avg_fee,
                    hashes,
                ]
            )
        rows.sort(key=lambda r: (r[2] != "yes", -r[1]))
        safe = pool_key.replace(".", "_").replace(" ", "_").replace("#", "_")
        header, out_rows = _with_network(
            network,
            [
                "recipient_address",
                "hit_count",
                "is_candidate",
                "total_received",
                "first_seen_utc",
                "last_seen_utc",
                "self_relayed_count",
                "avg_relayer_fee",
                "sample_tx_hashes",
            ],
            rows,
        )
        files.append(
            _write(
                os.path.join(out_dir, "{}_withdrawals_{}.csv".format(network.name, safe)),
                header,
                out_rows,
            )
        )

    return files


def write_relayer_csv(
    relayer_stats: dict, self_relayed_leads: list[dict], out_dir: str, network: Network
) -> list[str]:
    """Write relayer statistics and self-relayed leads (event mode only)."""
    _ensure_dir(out_dir)
    files = []

    header, rows = _with_network(
        network,
        ["relayer_address", "withdrawals", "avg_fee", "total_fee", "asset", "unique_recipients"],
        [
            [
                addr,
                s["withdrawals"],
                s["avg_fee"],
                s["total_fee"],
                s["asset"],
                s["unique_recipients"],
            ]
            for addr, s in relayer_stats.get("relayers", {}).items()
        ],
    )
    files.append(_write(os.path.join(out_dir, "relayers.csv"), header, rows))

    # broadcaster / broadcaster_status separate the implied claim ("the recipient
    # paid their own gas") from what was actually checked. "unverified" means
    # nobody read the transaction; it must not be read as confirmation.
    header, rows = _with_network(
        network,
        [
            "pool",
            "denomination",
            "asset",
            "recipient_address",
            "withdrawal_tx",
            "timestamp_utc",
            "fee",
            "broadcaster_address",
            "broadcaster_status",
        ],
        [
            [
                lead.get("pool_key", ""),
                lead.get("denom", ""),
                lead.get("asset", ""),
                lead["to"],
                lead["tx_hash"],
                _ts(lead["ts"]),
                lead["fee"],
                lead.get("broadcaster") or "",
                lead.get("broadcaster_status", "unverified"),
            ]
            for lead in self_relayed_leads
        ],
    )
    files.append(_write(os.path.join(out_dir, "self_relayed_leads.csv"), header, rows))
    return files


def write_multi_csv(corr: dict, out_dir: str, network: Network) -> list[str]:
    """Write multi-wallet correlation results as CSV files."""
    _ensure_dir(out_dir)
    results = corr["results"]
    addr_detail = corr["addr_detail"]
    files = []

    # wallets_overview.csv
    overview = []
    for wallet, data in results.items():
        if not data["vouchers"]:
            overview.append([wallet, "", "", "", "", "", ""])
            continue
        for v in data["vouchers"]:
            res = data["denoms"][v["pool_key"]]
            n_cand = sum(
                len(res["candidates_by_count"].get(n, [])) for n in res.get("target_counts", [])
            )
            overview.append(
                [
                    wallet,
                    v["pool_key"],
                    v["count"],
                    _ts(v["first_ts"]),
                    res["total_qualifying_withdrawals"],
                    res["unique_recipients"],
                    n_cand,
                ]
            )
    header, rows = _with_network(
        network,
        [
            "wallet",
            "pool",
            "num_deposits",
            "first_deposit_utc",
            "qualifying_withdrawals",
            "unique_recipients",
            "num_candidates",
        ],
        overview,
    )
    files.append(_write(os.path.join(out_dir, "wallets_overview.csv"), header, rows))

    # strong_links.csv and soft_overlaps.csv
    for name, entries in (("strong_links", corr["strong"]), ("soft_overlaps", corr["soft"])):
        rows = []
        for pool_key, addr, wallets in entries:
            detail = ";".join(f"{w}({addr_detail[(pool_key, addr)][w]}x)" for w in wallets)
            rows.append([pool_key, addr, len(wallets), detail])
        header, out_rows = _with_network(
            network,
            ["pool", "address", "num_wallets", "wallets_and_hitcounts"],
            rows,
        )
        files.append(_write(os.path.join(out_dir, f"{name}.csv"), header, out_rows))

    # profile_matches.csv
    prof_rows = []
    for wallet, fingerprint in corr["fingerprints"].items():
        printable = format_fingerprint(fingerprint)
        multi = "yes" if len(fingerprint) >= 2 else "no"
        for addr, detail, exact in corr["profile_matches"].get(wallet, []):
            recv = ";".join(f"{d}:{r}/{n}" for d, (r, n) in sorted(detail.items()))
            prof_rows.append([wallet, printable, addr, "exact" if exact else "gte", recv, multi])
    header, rows = _with_network(
        network,
        [
            "wallet",
            "fingerprint",
            "consolidator_address",
            "match_type",
            "received_vs_needed",
            "multi_denomination",
        ],
        prof_rows,
    )
    files.append(_write(os.path.join(out_dir, "profile_matches.csv"), header, rows))

    # cross_consolidators.csv
    header, rows = _with_network(
        network,
        ["consolidator_address", "num_wallets", "wallets"],
        [[addr, len(ws), ";".join(ws)] for addr, ws in corr["cross_profile"].items()],
    )
    files.append(_write(os.path.join(out_dir, "cross_consolidators.csv"), header, rows))

    return files


def format_fingerprint(fingerprint: dict[str, int]) -> str:
    """Render {'1 ETH': 2, '0.1 ETH': 1} as '1x0.1 ETH + 2x1 ETH'.

    Pool keys already name the asset, so no currency is appended.
    """
    return " + ".join(f"{n}x{key}" for key, n in sorted(fingerprint.items()))


def deposited_by_asset(vouchers: list[dict]) -> dict[str, float]:
    """{asset: total units deposited}, never summed across assets."""
    totals = {}
    for v in vouchers:
        totals[v["asset"]] = totals.get(v["asset"], 0) + v["count"] * v["denom"]
    return {asset: round(value, 8) for asset, value in totals.items()}


def format_totals(totals: dict[str, float]) -> str:
    """Render {'ETH': 1.8, 'DAI': 990.0} as 'DAI:990; ETH:1.8'.

    Never sums across assets: a cluster fed by a 1 ETH layer and a 1000 DAI
    layer at once carries one number per asset, not one meaningless total.
    Fixed-point, so 4 500 000 cDAI does not read '4.5e+06'.
    """
    return "; ".join(f"{a}:{_fixed(v)}" for a, v in sorted(totals.items()))


def _fixed(value: float) -> str:
    return "{:.8f}".format(value).rstrip("0").rstrip(".") or "0"


def write_cluster_csv(traces: list[dict], out_dir: str, network: Network) -> list[str]:
    """Write cluster-trace results as CSV files."""
    _ensure_dir(out_dir)
    files = []

    cluster_rows, detail_rows = [], []
    for trace in traces:
        printable = format_fingerprint(trace["fingerprint"])
        if not trace["clusters"]:
            cluster_rows.append([trace["wallet"], printable, "", 0, "", "", ""])
            continue
        for cluster in trace["clusters"]:
            sources = []
            for pool_key, lst in cluster["by_pool"].items():
                for src, _v, _t, _h, _a in lst[:2]:
                    sources.append(f"{pool_key}:{src}")
            cluster_rows.append(
                [
                    trace["wallet"],
                    printable,
                    cluster["z"],
                    cluster["n_layers"],
                    ",".join(str(k) for k in cluster["pools_covered"]),
                    format_totals(cluster["totals"]),
                    ";".join(sources[:6]),
                ]
            )
            for pool_key, lst in cluster["by_pool"].items():
                for src, value, ts, tx_hash, asset in lst:
                    detail_rows.append(
                        [
                            trace["wallet"],
                            cluster["z"],
                            pool_key,
                            asset,
                            src,
                            round(value, 6),
                            _ts(ts),
                            tx_hash,
                        ]
                    )

    header, rows = _with_network(
        network,
        [
            "wallet",
            "fingerprint",
            "cluster_point_z",
            "num_pool_layers",
            "pools_covered",
            "value_funneled",
            "sample_source_candidates",
        ],
        cluster_rows,
    )
    files.append(_write(os.path.join(out_dir, "clusters.csv"), header, rows))

    header, rows = _with_network(
        network,
        [
            "wallet",
            "cluster_point_z",
            "pool",
            "asset",
            "source_candidate",
            "forward_value",
            "forward_time_utc",
            "tx_hash",
        ],
        detail_rows,
    )
    files.append(_write(os.path.join(out_dir, "cluster_detail.csv"), header, rows))
    return files


def _e(x):
    return html.escape(str(x))


def _addr_link(network, addr):
    return '<a class="wrap" href="{}" target="_blank" rel="noopener">{}</a>'.format(
        _e(network.addr_url(addr)), _e(addr)
    )


def _tx_link(network, tx_hash):
    return '<a class="wrap" href="{}" target="_blank" rel="noopener">{}</a>'.format(
        _e(network.tx_url(tx_hash)), _e(tx_hash)
    )


def _band_badge(band):
    """Coloured strong/moderate/weak badge, legible in @media print."""
    return '<span class="badge {0}">{0}</span>'.format(_e(band))


_REPORT_CSS = """
 :root{--bg:#f7f6f3;--panel:#fefdfb;--ink:#1c1b19;--soft:#57534e;--muted:#8a857d;
   --line:#e6e3dd;--head:#f2f0eb;--accent:#0f766e;--accent-soft:#e6f1ef;
   --flag:#b45309;--radius:12px}
 *{box-sizing:border-box}
 body{font:15px/1.62 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
   color:var(--ink);margin:0;background:var(--bg);
   -webkit-font-smoothing:antialiased}
 .sheet{max-width:920px;margin:0 auto;padding:0 1.4rem 4rem}
 .band{border-top:3px solid var(--accent);background:var(--panel);
   border-bottom:1px solid var(--line);margin-bottom:1.6rem}
 .band .sheet{padding-top:1.6rem;padding-bottom:1.4rem}
 .eyebrow{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
   color:var(--accent);font-weight:600}
 h1{font-size:1.7rem;font-weight:600;letter-spacing:-.02em;margin:.35rem 0 .5rem}
 h2{font-size:1.08rem;font-weight:600;letter-spacing:-.01em;margin:2.2rem 0 .3rem}
 h2 .rule{display:block;height:1px;background:var(--line);margin:.55rem 0 0}
 h3{font-size:.9rem;font-weight:600;margin:1.3rem 0 .4rem;color:var(--soft)}
 .meta{color:var(--muted);font-size:.82rem}
 .meta b{color:var(--soft);font-weight:600}
 .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.8rem}
 .num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
 .wrap{overflow-wrap:anywhere;word-break:break-word}
 .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
   gap:1px;background:var(--line);border:1px solid var(--line);border-radius:var(--radius);
   overflow:hidden;margin-top:1.2rem}
 .metric{background:var(--panel);padding:.9rem 1rem}
 .metric .k{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
 .metric .v{font-family:ui-monospace,monospace;font-size:1.35rem;font-weight:600;
   margin-top:.2rem;font-variant-numeric:tabular-nums}
 .metric .s{font-size:.74rem;color:var(--muted)}
 table{width:100%;border-collapse:collapse;margin:.7rem 0 .4rem;table-layout:fixed}
 th{text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;
   color:var(--muted);font-weight:600;padding:.55rem .6rem;border-bottom:1px solid var(--line)}
 td{padding:.55rem .6rem;font-size:.85rem;border-bottom:1px solid var(--line);
   vertical-align:top;overflow-wrap:anywhere}
 tbody tr{transition:background .12s ease}
 @media (prefers-reduced-motion:no-preference){tbody tr:hover{background:var(--head)}}
 tr.lead td{background:var(--accent-soft)}
 .pill{display:inline-block;background:var(--head);border:1px solid var(--line);
   border-radius:999px;padding:.1rem .55rem;font-size:.7rem;color:var(--soft);margin:1px 3px 1px 0}
 .flag{color:var(--accent);font-weight:600}
 .conf b{font-family:ui-monospace,monospace;font-size:.82rem;display:block;margin-bottom:.2rem}
 .cbar{display:block;height:3px;width:100%;max-width:96px;border-radius:2px;
   background:linear-gradient(90deg,var(--accent) var(--w),var(--head) var(--w))}
 .callout{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--flag);
   border-radius:0 var(--radius) var(--radius) 0;padding:.95rem 1.1rem;color:var(--soft);
   font-size:.85rem;margin-top:.8rem}
 .badge{display:inline-block;border-radius:999px;padding:.1rem .55rem;font-size:.66rem;
   font-weight:700;letter-spacing:.06em;text-transform:uppercase;border:1px solid var(--line);
   vertical-align:middle;-webkit-print-color-adjust:exact;print-color-adjust:exact}
 .badge.strong{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
 .badge.moderate{background:#fbecd6;color:var(--flag);border-color:var(--flag)}
 .badge.weak{background:var(--head);color:var(--muted);border-color:var(--line)}
 .lead-card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
   padding:.85rem 1.05rem;margin:.7rem 0}
 .lead-card .top{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
 .lead-card .addr{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem}
 .lead-card .why{color:var(--soft);font-size:.85rem;margin-top:.4rem}
 .lead-card .rat{color:var(--muted);font-size:.76rem;margin-top:.2rem}
 .conf .pct{color:var(--muted);font-weight:400;font-size:.72rem}
 .evidence{list-style:none;margin:.45rem 0 .1rem;padding:0;font-size:.8rem}
 .evidence li{padding:.1rem 0}
 .evidence .ev-yes b{color:var(--accent)}
 .evidence .ev-no{color:var(--muted)}
 .foot{margin-top:2.6rem;padding-top:1rem;border-top:1px solid var(--line);
   color:var(--muted);font-size:.74rem}
 a{color:var(--accent);text-decoration:none}
 @media (prefers-reduced-motion:no-preference){a{transition:color .12s ease}}
 a:hover{text-decoration:underline}
 @media print{body{background:#fff}.band{border-bottom-color:#ccc}tbody tr:hover{background:none}
   a{color:var(--ink)}}
"""


def analysis_assumptions(data: dict) -> dict:
    """The parameters and search ranges behind a demix result.

    Returns {"params": [(label, value)], "windows": [{pool_key, blocks, dates,
    vouchers, withdrawals, recipients}]}, so a report states the assumptions its
    candidates depend on and the exact block ranges that were read.
    """
    from . import __version__

    params = data.get("params", {})
    if params.get("mode", "events") == "events":
        source = "Withdrawal events of each pool (recipient, relayer and fee exact)"
    else:
        lo, hi = params.get("fee_window", [None, None])
        source = f"internal transfers from the pool, {lo}-{hi} of the denomination"
    exit_h = params.get("exit_window_hours")
    window = (
        f"{_fmt_hours(exit_h)} h after each voucher's last deposit"
        if exit_h
        else f"{params.get('window_days', 30)} days after each voucher's last deposit"
    )
    rows = [
        ("Tool", f"tornado-demix {__version__}"),
        ("Network", params.get("network", "")),
        ("Withdrawal data", source),
        (
            "Voucher gap",
            f"{_fmt_hours(params.get('gap_hours', 24))} h between deposits into one pool",
        ),
        ("Search window", window),
        ("Deposit amount", f"native within {NATIVE_DENOM_TOLERANCE:.1%}; ERC-20 exact"),
        (
            "Count match",
            f"counts only at disc >= {MIN_COUNT_DISCRIMINATION:.2f} "
            f"in a field of at least {MIN_FIELD_SIZE} recipients",
        ),
        (
            "Gas-price reuse",
            f"price shared by at most {MAX_GAS_PRICE_SHARE} withdrawals, blocks without "
            "an EIP-1559 base fee only",
        ),
    ]
    windows = []
    for pool_key, res in data.get("denoms", {}).items():
        for w in res.get("windows", []):
            windows.append(
                {
                    "pool_key": pool_key,
                    "blocks": f"{w.get('start_block')}..{w.get('end_block')}",
                    "dates": f"{_ts(w['first_ts'])} .. {_ts(w['end_ts'])}",
                    "vouchers": w.get("voucher_count", 1),
                    "withdrawals": res.get("total_qualifying_withdrawals", 0),
                    "recipients": res.get("unique_recipients", 0),
                }
            )
    return {"params": rows, "windows": windows}


def demix_json(data: dict, attribution: dict[str, dict] | None = None) -> str:
    """A demix result as JSON: parameters, ranked candidates and the raw result.

    Meant for archiving with a case and for re-analysis (tools/sensitivity.py)
    without repeating the API calls.
    """
    from . import __version__

    doc = {
        "tool": f"tornado-demix {__version__}",
        "generated_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assumptions": analysis_assumptions(data),
        "candidates": ranked_candidates(data, attribution=attribution),
        "result": data,
    }
    return json.dumps(doc, indent=1, default=_json_default)


def write_demix_json(data: dict, path: str, attribution: dict[str, dict] | None = None) -> str:
    """Write :func:`demix_json` to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(demix_json(data, attribution=attribution))
    return path


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


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


def build_html_report(
    data: dict, network: Network, attribution: dict[str, dict] | None = None
) -> str:
    """Render a single-wallet demix result as a self-contained HTML string.

    ``network`` supplies the block-explorer URLs and the native currency, so
    a BSC report links to bscscan and a Polygon report to polygonscan.

    ``attribution`` is an optional ``{address_lower: label_dict}`` map (see
    :func:`tornado_demix.attribution.load_attribution`). When it names any ranked
    candidate, the ranked table gains an "Attribution" column and the lead cards
    show the label; with no set, or no match, the report is unchanged.
    """
    attribution = attribution or {}
    wallet = data["wallet"]
    params = data.get("params", {})
    net_name = params.get("network", network.name)
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    p = [
        f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tornado demix report {_e(wallet[:10])}</title>
<style>{_REPORT_CSS}</style></head><body>"""
    ]

    # Header band
    p.append('<div class="band"><div class="sheet">')
    p.append('<div class="eyebrow">Forensic demix report · research use</div>')
    p.append("<h1>Tornado.Cash exit analysis</h1>")
    p.append(f'<div class="meta">Depositor <b class="mono">{_e(wallet)}</b></div>')
    exit_h = params.get("exit_window_hours")
    window_txt = (
        f"exit window <b>{_e(_fmt_hours(exit_h))} h</b> after each voucher's last deposit"
        if exit_h
        else f"window <b>{_e(params.get('window_days', 30))} days</b>"
    )
    p.append(
        f'<div class="meta">Network <b>{_e(net_name)}</b> · mode <b>{_e(params.get("mode", "events"))}</b>'
        f" · {window_txt} · generated <b>{generated}</b></div>"
    )
    if exit_h:
        p.append(
            '<div class="meta">Narrow exit window: only withdrawals within '
            f"{_e(_fmt_hours(exit_h))} h of a deposit were searched. Fast exits are "
            "surfaced with far less shared-window noise; withdrawals delayed "
            "longer than this are <b>not</b> in scope.</div>"
        )
    p.append("</div></div>")

    p.append('<div class="sheet">')

    if not data.get("vouchers"):
        p.append('<h2>No Tornado.Cash deposits found<span class="rule"></span></h2>')
        p.append(
            '<div class="meta">This address made no deposits into the configured '
            "Tornado pools on this network.</div>"
        )
        p.append("</div></body></html>")
        return "".join(p)

    cands = ranked_candidates(data, attribution=attribution)
    show_attr = any(c.get("attribution") for c in cands)
    leads = self_relayed_candidates(data) if params.get("mode", "events") == "events" else []
    # One total per asset: 1 ETH + 100000 DAI must not read '100001 ETH'.
    deposited = format_totals(deposited_by_asset(data["vouchers"]))
    top_band = cands[0]["band"] if cands else "none"

    # Metric strip
    p.append('<div class="metrics">')
    p.append(
        f'<div class="metric"><div class="k">Deposited</div>'
        f'<div class="v">{_e(deposited)}</div><div class="s">{len(data["deposits"])} notes</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Vouchers</div>'
        f'<div class="v">{len(data["vouchers"])}</div><div class="s">denomination groups</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Candidates</div>'
        f'<div class="v">{len(cands)}</div><div class="s">ranked exits</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Strongest band</div>'
        f'<div class="v">{_e(top_band)}</div><div class="s">{len(leads)} self-relayed leads</div></div>'
    )
    p.append("</div>")
    p.append(_assumptions_html(analysis_assumptions(data)))

    # Strong first, then moderate; a bare count match is never shown here.
    p.append('<h2>Most likely candidates<span class="rule"></span></h2>')
    likely = sorted(
        (c for c in cands if c["band"] in ("strong", "moderate")),
        key=lambda c: (-BAND_ORDER[c["band"]], -c["confidence"]),
    )
    p.append(
        '<div class="meta">These are <b>probabilistic leads for '
        "corroboration, not proof</b>. The band reflects how many "
        "independent families of evidence agree on an address; each card lists "
        "what was checked. The score only orders leads within a band.</div>"
    )
    if likely:
        for r in likely[:8]:
            attr = (
                f'<span class="pill flag">{_e(r["attribution"])}</span>'
                if r.get("attribution")
                else ""
            )
            p.append(
                '<div class="lead-card">'
                f'<div class="top">{_band_badge(r["band"])}'
                f'<span class="addr wrap">{_addr_link(network, r["address"])}</span>'
                f'<span class="pill">{_e(r["pool_key"])}</span>{attr}'
                f'<span class="conf pct">score {r["confidence"]:.2f}</span></div>'
                f'<div class="why">{_e(candidate_reason(r))}</div>'
                f"{_evidence_html(r['evidence'])}"
                f'<div class="rat">Band: {_e(r["band"])} — '
                f"{_e(band_rationale(r['band'], set(r['signals'])))}.</div>"
                "</div>"
            )
    else:
        p.append(
            '<div class="callout">No corroborated candidates — every lead '
            "rests on the amount+timing match alone; treat them as starting "
            "points and trace the funding source.</div>"
        )

    # A pool whose block lookup failed was never searched; that must not read
    # as "no exits found".
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

    # Vouchers
    p.append('<h2>Deposits<span class="rule"></span></h2>')
    p.append(
        '<table><colgroup><col style="width:22%"><col style="width:16%"><col></colgroup>'
        "<tr><th>Pool</th><th>Notes</th><th>First deposit (UTC)</th></tr>"
    )
    for v in data["vouchers"]:
        p.append(
            '<tr><td class="num">{}</td><td class="num">{}</td><td class="num">{}</td></tr>'.format(
                _e(v["pool_key"]), _e(v["count"]), _ts(v["first_ts"])
            )
        )
    p.append("</table>")

    # Ranked candidates (with a short reason per address)
    p.append('<h2>Ranked exit candidates<span class="rule"></span></h2>')
    if cands:
        # The Attribution column appears only when at least one row has a label.
        if show_attr:
            p.append(
                '<table><colgroup><col style="width:5%"><col style="width:10%">'
                '<col style="width:24%"><col style="width:7%"><col style="width:8%">'
                '<col style="width:14%"><col></colgroup>'
                "<tr><th>#</th><th>Band</th><th>Recipient</th><th>Pool</th>"
                "<th>Score</th><th>Attribution</th><th>Why this address</th></tr>"
            )
        else:
            p.append(
                '<table><colgroup><col style="width:5%"><col style="width:11%">'
                '<col style="width:28%"><col style="width:7%"><col style="width:9%"><col></colgroup>'
                "<tr><th>#</th><th>Band</th><th>Recipient</th><th>Pool</th><th>Score</th>"
                "<th>Why this address</th></tr>"
            )
        for i, r in enumerate(cands[:60], 1):
            pct = int(r["confidence"] * 100)
            attr_cell = f'<td class="flag">{_e(r["attribution"])}</td>' if show_attr else ""
            p.append(
                f'<tr><td class="num">{i}</td><td>{_band_badge(r["band"])}</td>'
                f'<td class="mono wrap">{_addr_link(network, r["address"])}</td>'
                f'<td class="num">{_e(r["pool_key"])}</td>'
                f'<td class="conf"><span class="pct">{r["confidence"]:.2f}</span>'
                f'<span class="cbar" style="--w:{pct}%"></span></td>'
                f"{attr_cell}"
                f"<td>{_e(candidate_reason(r))}"
                f'<div class="meta">disc {r["discrimination"]:.2f} in a field of '
                f"{r['field_size']} recipients</div></td></tr>"
            )
        p.append("</table>")
        p.append(
            '<div class="meta">The <b>band</b> is the headline: it reflects how many '
            "independent families of evidence corroborate the address. The score is a "
            "noisy-OR over expert signal weights that orders leads within a band; it is "
            "uncalibrated and is not a probability.</div>"
        )
    else:
        p.append(
            '<div class="meta">No discriminating candidates. This usually means single-note '
            "vouchers (count of 1), where the method cannot narrow the pool.</div>"
        )

    # Cross-method matches
    xm = cross_method(data)
    p.append('<h2>Cross-method matches<span class="rule"></span></h2>')
    if xm:
        p.append(
            '<div class="meta">Addresses selected by <b>two or more independent '
            "families of evidence</b> at once — amount+timing, gas price, and a direct "
            "counterparty relationship. Independence is what makes agreement meaningful, "
            "so a self-relayed withdrawal does not count separately from the count match "
            "it was found on: both read the same withdrawals. Still leads, not proof.</div>"
        )
        p.append(
            '<table><colgroup><col style="width:34%"><col style="width:8%">'
            '<col style="width:9%"><col></colgroup>'
            "<tr><th>Recipient</th><th>Pool</th><th>Methods</th><th>Why (per method)</th></tr>"
        )
        for r in xm[:40]:
            methods = "".join(f'<span class="pill">{_e(m)}</span>' for m in r["methods"])
            reasons = "; ".join(_e(x) for x in r["reasons"])
            p.append(
                f'<tr class="lead"><td class="mono flag wrap">{_addr_link(network, r["address"])}</td>'
                f'<td class="num">{_e(r["pool_key"])}</td>'
                f'<td class="num">{r["n_methods"]}x</td>'
                f'<td>{methods}<div class="meta" style="margin-top:.2rem">{reasons}</div></td></tr>'
            )
        p.append("</table>")
    else:
        p.append(
            '<div class="meta">No address was selected by more than one method. '
            "Every candidate rests on a single method, so treat them as weaker.</div>"
        )

    # Results by method
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
                f'<tr><td class="mono wrap">{_addr_link(network, r["address"])}</td>'
                f'<td class="num">{_e(r["pool_key"])}</td><td>{_e(r["reason"])}</td></tr>'
            )
        p.append("</table>")
    if not any_method:
        p.append(
            '<div class="meta">No method produced a discriminating selection for this wallet.</div>'
        )

    # Heuristic hits
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
                    f'<td class="mono wrap">{_addr_link(network, addr)}</td>'
                    f'<td class="num">{gp / 1e9:.3f}</td>'
                    f'<td class="mono wrap">{_tx_link(network, txh)}</td></tr>'
                )
            p.append("</table>")
        if h.get("linked_addresses"):
            p.append(
                "<h3>Linked addresses (direct counterparties of the depositor)</h3>"
                '<table><colgroup><col style="width:12%"><col></colgroup>'
                "<tr><th>Pool</th><th>Recipient</th></tr>"
            )
            for pool_key, addr in h["linked_addresses"][:40]:
                p.append(
                    f'<tr><td class="num">{_e(pool_key)}</td>'
                    f'<td class="mono wrap">{_addr_link(network, addr)}</td></tr>'
                )
            p.append("</table>")

    # Relayers + self-relayed leads
    if params.get("mode", "events") == "events":
        stats = analyze_relayers(collect_withdrawals(data))
        p.append('<h2>Relayers<span class="rule"></span></h2>')
        p.append(
            f'<div class="meta">{stats["unique_relayers"]} relayer(s), '
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
                '<table><colgroup><col style="width:28%"><col style="width:8%">'
                '<col style="width:14%"><col style="width:24%"><col></colgroup>'
                "<tr><th>Recipient</th><th>Pool</th><th>Time (UTC)</th>"
                "<th>Broadcast by</th><th>Withdrawal tx</th></tr>"
            )
            for lead in leads[:40]:
                status = lead.get("broadcaster_status", "unverified")
                if status == "self":
                    who = '<span class="flag">the recipient</span>'
                elif status == "other":
                    who = _addr_link(network, lead.get("broadcaster"))
                else:
                    who = '<span class="meta">unverified</span>'
                p.append(
                    f'<tr class="lead"><td class="mono flag wrap">{_addr_link(network, lead["to"])}</td>'
                    f'<td class="num">{_e(lead["pool_key"])}</td><td class="num">{_ts(lead["ts"])}</td>'
                    f'<td class="mono wrap">{who}</td>'
                    f'<td class="mono wrap">{_tx_link(network, lead["tx_hash"])}</td></tr>'
                )
            p.append("</table>")

    p.append('<h2>Summary<span class="rule"></span></h2>')
    p.append(
        '<div class="callout" style="border-left-color:var(--accent);white-space:pre-line">'
        f"{_e(conclusion(data))}</div>"
    )

    p.append(
        '<h2>How to read this<span class="rule"></span></h2><div class="callout">'
        "This report lists <b>probabilistic leads, not proof</b>. The strongest signals are "
        "<b>linked</b> and <b>gas-price</b> matches; a <b>self-relayed</b> exit strengthens a "
        "count match but belongs to the same evidence family. A bare count "
        "match on a busy pool is weak, and single-note (count of 1) vouchers cannot be demixed "
        "by this method. Corroborate any candidate with independent evidence (funding source, "
        "exchange KYC, further hops) before drawing a conclusion.</div>"
    )

    p.append(
        f'<div class="foot">Generated by tornado-demix · analysis of public on-chain data · '
        f"{generated}. Not legal advice.</div>"
    )
    p.append("</div></body></html>")
    return "".join(p)


def write_html_report(
    data: dict, path: str, network: Network, attribution: dict[str, dict] | None = None
) -> str:
    """Write the single-wallet HTML report to ``path``.

    ``attribution`` is threaded to :func:`build_html_report` so the ranked table
    can carry attribution labels when a set is loaded (see that function).

    Written as UTF-8 explicitly: the document declares that charset, and the
    platform default on Windows is a legacy codepage.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_html_report(data, network, attribution=attribution))
    return path


def _report_head(title, eyebrow):
    """Return the shared <head> + opening header band for an HTML report."""
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{_REPORT_CSS}</style></head><body>"
    )
    band = (
        '<div class="band"><div class="sheet">'
        f'<div class="eyebrow">{_e(eyebrow)}</div>'
        f"<h1>{_e(title)}</h1>"
    )
    return [head, band], generated


def build_multi_report(corr: dict, network: Network) -> str:
    """Render a multi-wallet correlation result as a self-contained HTML string.

    Mirrors :func:`build_html_report`: same header band, sheet layout and print
    CSS. Surfaces the operator clusters, the profile matches (one address received
    a wallet's full fingerprint) and the cross-wallet consolidators. A
    consolidator linking two or more wallets is a multi-wallet structural lead and
    gets the strong band's emphasis, but it is still a lead for corroboration, not
    proof.
    """
    results = corr.get("results", {})
    wallets = list(results)
    strong = corr.get("strong", [])
    clusters = corr.get("operator_clusters", [])
    cross = corr.get("cross_profile", {})
    grades = corr.get("consolidator_grades", {})
    profile_matches = corr.get("profile_matches", {})
    n_profiles = sum(len(v) for v in profile_matches.values())

    # Split the consolidator count so artefacts are not hidden behind one number.
    n_strong = sum(1 for g in grades.values() if g.get("band") == "strong")
    n_moderate = sum(1 for g in grades.values() if g.get("band") == "moderate")
    n_artefact = sum(1 for g in grades.values() if g.get("artefact"))

    # Two shapes inflate the headline counts: several wallets with one identical
    # fingerprint, or a tight synchronised batch. The banner names whichever
    # applies. A single-linkage batch can chain across days, so only a tight one
    # counts.
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
    exit_h = None
    for data in results.values():
        net_name = data.get("params", {}).get("network", network.name)
        exit_h = data.get("params", {}).get("exit_window_hours")
        break

    p, generated = _report_head(
        "Tornado.Cash multi-wallet correlation", "Forensic correlation report · research use"
    )
    reach = f" · exit window <b>{_e(_fmt_hours(exit_h))} h</b>" if exit_h else ""
    p.append(
        f'<div class="meta">Wallets analysed <b>{len(wallets)}</b> · '
        f"network <b>{_e(net_name)}</b>{reach} · generated <b>{generated}</b></div>"
    )
    p.append("</div></div>")

    p.append('<div class="sheet">')

    # Metric strip
    p.append('<div class="metrics">')
    p.append(
        f'<div class="metric"><div class="k">Wallets</div>'
        f'<div class="v">{len(wallets)}</div><div class="s">analysed together</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Operator clusters</div>'
        f'<div class="v">{len(clusters)}</div><div class="s">on discriminating evidence</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Consolidators</div>'
        f'<div class="v">{len(cross)}</div>'
        f'<div class="s">{n_strong} strong · {n_moderate} moderate · '
        f"{n_artefact} window-prone</div></div>"
    )
    p.append(
        f'<div class="metric"><div class="k">Profile matches</div>'
        f'<div class="v">{n_profiles}</div><div class="s">full fingerprint</div></div>'
    )
    p.append("</div>")

    # Window-overlap banner, only when it applies.
    if dominant_n >= 3:
        pretty = format_fingerprint(dict(dominant_fp)) or "an identical fingerprint"
        p.append(
            '<div class="callout"><b>Window-overlap warning.</b> '
            f"{dominant_n} of the analysed wallets deposited with the <b>same "
            f"fingerprint</b> ({_e(pretty)}). When wallets deposit synchronously "
            "with equal denominations their candidate sets and profile matches "
            "overlap <b>by construction</b>, so the <b>Consolidators</b> and "
            "<b>Profile matches</b> counts above are inflated by the shared "
            "window, not the number of real exits. Only consolidators graded "
            "<b>strong</b> (an independent gas-price / linked signal), or "
            "<b>moderate</b> over a different set of wallets that did not deposit "
            "together, rise above that artefact — see below.</div>"
        )
    elif biggest_batch and len(biggest_batch["wallets"]) >= 3:
        p.append(
            '<div class="callout"><b>Window-overlap warning.</b> '
            f"{len(biggest_batch['wallets'])} of the analysed wallets deposited "
            f"as one <b>synchronised batch</b> (within {format_span(biggest_batch['span_seconds'])}, "
            f"{_ts(biggest_batch['first_ts'])}). Their search windows overlap almost "
            "entirely, so the <b>Profile matches</b> count and any distinct-fingerprint "
            "consolidator across these wallets are inflated by the shared window — a "
            "consolidator matched wholly inside one batch is graded <b>window-prone</b>. "
            "The synchronicity itself is the real coordination lead here, not the exit "
            "addresses — see <b>Deposit synchronicity</b> below.</div>"
        )

    # Most likely: cross-wallet consolidators (graded by real discrimination)
    p.append('<h2>Most likely: cross-wallet consolidators<span class="rule"></span></h2>')
    p.append(
        '<div class="meta">These are <b>probabilistic leads for corroboration, '
        "not proof</b>. A consolidator is graded by how much it actually "
        "discriminates: <b>strong</b> carries an independent gas-price or linked "
        "signal beyond the fingerprint overlap; <b>moderate</b> spans distinct "
        "fingerprints of wallets that did <b>not</b> deposit as one synchronised "
        "batch; <b>weak</b> is a window-overlap artefact — the matched wallets "
        "share one identical fingerprint, or deposited together as a synchronised "
        "batch, so a shared window explains the match.</div>"
    )

    # An ungraded entry (direct caller) defaults to the conservative weak shape.
    def _grade_of(addr):
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

    shown = [
        (addr, ws)
        for addr, ws in sorted(
            cross.items(),
            key=lambda kv: (-BAND_ORDER.get(_grade_of(kv[0])["band"], 1), -len(kv[1]), kv[0]),
        )
        if len(ws) >= 2
    ][:8]
    if shown:
        for addr, ws in shown:
            g = _grade_of(addr)
            band = g["band"]
            if band == "strong":
                rat = (
                    "Band: strong — corroborated by an independent signal ("
                    + _e(", ".join(g["independent"]))
                    + ") beyond the "
                    "fingerprint overlap, so the link is not a shared-window "
                    "artefact."
                )
            elif band == "moderate":
                rat = (
                    "Band: moderate — matches the <b>distinct</b> fingerprints of "
                    f"{g['distinct_fingerprints']} different note-patterns, which a "
                    "shared search window does not produce by chance. No independent "
                    "gas-price / linked signal, so corroborate before concluding."
                )
            elif g.get("artefact_kind") == "synchronized":
                rat = (
                    "Band: weak — <b>window-overlap artefact (synchronised batch)</b>. "
                    "The fingerprints differ, but every matched wallet deposited "
                    "inside one synchronised batch, and their pools overlap, so a "
                    "single busy-pool recipient satisfies these distinct patterns by "
                    "shared window rather than by a real link. Corroborate with a "
                    "per-wallet demix signal (gas-price / linked) before treating it "
                    "as a real consolidator."
                )
            else:
                rat = (
                    "Band: weak — <b>window-overlap artefact</b>. The matched "
                    "wallets share one identical fingerprint and this address "
                    "carries no independent signal, so a synchronous batch of "
                    "equal-denomination deposits lets many unrelated recipients "
                    "match all of them at once. Corroborate with a per-wallet "
                    "demix signal (self-relayed / gas-price / linked) before "
                    "treating it as a real consolidator."
                )
            p.append(
                '<div class="lead-card">'
                f'<div class="top">{_band_badge(band)}'
                f'<span class="addr wrap">{_addr_link(network, addr)}</span>'
                f'<span class="pill">{len(ws)} wallets</span></div>'
                f'<div class="why">Received the full deposit fingerprint of '
                f"{len(ws)} wallets: {_e(', '.join(ws))}.</div>"
                f'<div class="rat">{rat}</div>'
                "</div>"
            )
    else:
        p.append(
            '<div class="callout">No cross-wallet consolidator — no single address '
            "received the full fingerprint of two or more wallets. Treat any "
            "per-wallet profile match below as a single-wallet lead only.</div>"
        )

    # Deposit synchronicity (a positive coordination lead in its own right)
    p.append('<h2>Deposit synchronicity<span class="rule"></span></h2>')
    if sync_groups:
        p.append(
            '<div class="meta">Wallets whose deposits chain together in time '
            "(single-linkage within 6h). A tight batch is <b>genuine coordination "
            "evidence</b> — independent of any exit address — and is also why a "
            "consolidator matched wholly inside one batch is graded window-prone: "
            "the shared window, not a real link, explains its fingerprint match.</div>"
        )
        p.append(
            '<table><colgroup><col style="width:12%"><col style="width:20%">'
            '<col style="width:20%"><col></colgroup>'
            "<tr><th>Wallets</th><th>Span</th><th>First deposit (UTC)</th>"
            "<th>Members</th></tr>"
        )
        for g in sync_groups:
            members = ", ".join(g["wallets"])
            p.append(
                f'<tr><td class="num">{len(g["wallets"])}</td>'
                f"<td>{_e(format_span(g['span_seconds']))}</td>"
                f'<td class="mono">{_e(_ts(g["first_ts"]))}</td>'
                f'<td class="mono wrap">{_e(members)}</td></tr>'
            )
        p.append("</table>")
    else:
        p.append(
            '<div class="meta">No two wallets deposited within one synchronised '
            "batch. Deposit timing does not, on its own, tie any of these wallets "
            "together.</div>"
        )

    # Operator clusters
    p.append('<h2>Operator clusters<span class="rule"></span></h2>')
    if clusters:
        p.append(
            '<div class="meta">Wallets linked into a component only by '
            "<b>discriminating</b> evidence (a shared gas-price/linked exit, or a "
            "shared full-fingerprint consolidator) — never by the window-overlap "
            "artefact that raw count intersections carry.</div>"
        )
        for i, cluster in enumerate(clusters, 1):
            ws = ", ".join(cluster["wallets"])
            p.append(
                f'<h3>Cluster {i} <span class="pill">{len(cluster["wallets"])} wallets</span></h3>'
            )
            p.append(f'<div class="meta mono wrap">{_e(ws)}</div>')
            if cluster["edges"]:
                p.append(
                    '<table><colgroup><col style="width:30%"><col style="width:30%">'
                    '<col style="width:22%"><col></colgroup>'
                    "<tr><th>Wallet A</th><th>Wallet B</th><th>Via address</th><th>Why</th></tr>"
                )
                for a, b, reason, addr in cluster["edges"][:40]:
                    p.append(
                        f'<tr><td class="mono wrap">{_addr_link(network, a)}</td>'
                        f'<td class="mono wrap">{_addr_link(network, b)}</td>'
                        f'<td class="mono wrap">{_addr_link(network, addr)}</td>'
                        f"<td>{_e(reason)}</td></tr>"
                    )
                p.append("</table>")
    else:
        p.append(
            '<div class="meta">No two wallets were linked by discriminating '
            "evidence. Any overlaps below rest on shared search windows, not on a "
            "proven common operator.</div>"
        )

    # Profile matches (per wallet)
    p.append('<h2>Profile matches (full fingerprint on one address)<span class="rule"></span></h2>')
    prof_rows = []
    for wallet, matches in profile_matches.items():
        fingerprint = corr.get("fingerprints", {}).get(wallet, {})
        printable = format_fingerprint(fingerprint)
        for addr, detail, exact in matches:
            recv = "; ".join(f"{d}:{r}/{n}" for d, (r, n) in sorted(detail.items()))
            prof_rows.append((wallet, printable, addr, exact, recv))
    if prof_rows:
        p.append(
            '<div class="meta">One address that received at least a wallet\'s whole '
            "deposit fingerprint. A multi-pool fingerprint is far more discriminating "
            'than a single count; a single-pool "fingerprint" is just the count match.</div>'
        )
        p.append(
            '<table><colgroup><col style="width:28%"><col style="width:16%">'
            '<col style="width:28%"><col style="width:9%"><col></colgroup>'
            "<tr><th>Wallet</th><th>Fingerprint</th><th>Consolidator</th>"
            "<th>Match</th><th>Received / needed</th></tr>"
        )
        for wallet, printable, addr, exact, recv in prof_rows[:60]:
            match = '<span class="flag">exact</span>' if exact else "&ge;"
            p.append(
                f'<tr><td class="mono wrap">{_addr_link(network, wallet)}</td>'
                f'<td class="num">{_e(printable)}</td>'
                f'<td class="mono wrap">{_addr_link(network, addr)}</td>'
                f'<td>{match}</td><td class="mono wrap">{_e(recv)}</td></tr>'
            )
        p.append("</table>")
    else:
        p.append('<div class="meta">No address received any wallet\'s full fingerprint.</div>')

    # Strong links (with the window-artefact caveat)
    p.append('<h2>Strong links<span class="rule"></span></h2>')
    p.append(
        '<div class="callout">Caveat: wallets that deposited at similar times with '
        "equal voucher sizes share candidate sets <b>by construction</b>, so treat a "
        "raw overlap as a window artefact, not evidence of a link. The discriminating "
        "signals are the operator clusters and the cross-wallet consolidators above.</div>"
    )
    if strong:
        p.append(
            '<table><colgroup><col style="width:14%"><col style="width:56%">'
            "<col></colgroup>"
            "<tr><th>Pool</th><th>Shared candidate</th><th>Wallets</th></tr>"
        )
        for pool_key, addr, ws in strong[:60]:
            p.append(
                f'<tr><td class="num">{_e(pool_key)}</td>'
                f'<td class="mono wrap">{_addr_link(network, addr)}</td>'
                f'<td class="num">{len(ws)}</td></tr>'
            )
        p.append("</table>")
    else:
        p.append(
            '<div class="meta">No count-matched candidate was shared by two or more wallets.</div>'
        )

    p.append(
        '<h2>How to read this<span class="rule"></span></h2><div class="callout">'
        "This report lists <b>probabilistic leads, not proof</b>. A cross-wallet "
        "consolidator or an operator cluster is a multi-wallet structural lead; a bare "
        "strong-link overlap is often just a shared search window. Corroborate any "
        "link with independent evidence (funding source, exchange KYC, further hops) "
        "before drawing a conclusion.</div>"
    )
    p.append(
        f'<div class="foot">Generated by tornado-demix · analysis of public on-chain '
        f"data · {generated}. Not legal advice.</div>"
    )
    p.append("</div></body></html>")
    return "".join(p)


def write_multi_report(corr: dict, path: str, network: Network) -> str:
    """Write the multi-wallet correlation HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_multi_report(corr, network))
    return path


def build_characterize_report(info: dict, network: Network) -> str:
    """Render an exit-candidate characterisation (see characterize.py) as HTML."""
    addr = info["address"]
    p, generated = _report_head(
        "Tornado.Cash exit-candidate characterisation", "Recipient-side analysis · research use"
    )
    p.append(
        f'<div class="meta">Address <b class="mono">{_e(addr)}</b> · '
        f"network <b>{_e(network.name)}</b> · generated <b>{generated}</b></div>"
    )
    p.append("</div></div>")
    p.append('<div class="sheet">')

    totals = ", ".join(f"{v} {a}" for a, v in sorted(info["inflow_totals"].items()))
    p.append('<div class="metrics">')
    p.append(
        f'<div class="metric"><div class="k">Classification</div>'
        f'<div class="v" style="font-size:1.1rem">{_e(info["classification"])}</div>'
        f'<div class="s">from activity + pool diversity</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Activity</div>'
        f'<div class="v">{info["normal_txs"]}</div>'
        f'<div class="s">normal tx · {info["internal_txs"]} internal</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Pool inflows</div>'
        f'<div class="v">{len(info["pool_inflows"])}</div>'
        f'<div class="s">{len(info["distinct_pools"])} distinct pool(s)</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Total in</div>'
        f'<div class="v" style="font-size:1.1rem">{_e(totals) or "—"}</div>'
        f'<div class="s">from known pools</div></div>'
    )
    p.append("</div>")

    label = info.get("label")
    if label:
        entity = f" — entity <b>{_e(label['entity'])}</b>" if label.get("entity") else ""
        conf = f" · confidence {_e(label['confidence'])}" if label.get("confidence") else ""
        p.append(
            '<div class="callout"><b>Attribution: {}.</b> This address is a '
            "KNOWN labelled address{}{}. Its identity, not its behaviour, is the "
            "authoritative fact here.</div>".format(_e(format_label(label)), entity, conf)
        )

    p.append(
        '<div class="callout"><b>{}.</b> {}</div>'.format(
            _e(info["classification"].capitalize()), _e(info["classification_reason"])
        )
    )

    p.append('<h2>Tornado-pool inflows<span class="rule"></span></h2>')
    if info["pool_inflows"]:
        p.append(
            '<div class="meta">Every withdrawal this address received from a '
            "known pool of this network, oldest first. A spread across pools the "
            "subject never used marks a shared aggregator. Native pools only; "
            "ERC-20 pool withdrawals are out of scope.</div>"
        )
        p.append(
            '<table><colgroup><col style="width:26%"><col style="width:20%">'
            "<col></colgroup>"
            "<tr><th>When (UTC)</th><th>Amount</th><th>Pool</th></tr>"
        )
        for r in info["pool_inflows"][:100]:
            p.append(
                f'<tr><td class="mono">{_e(_ts(r["ts"]))}</td>'
                f'<td class="num">{_e(r["value"])} {_e(r["asset"])}</td>'
                f'<td class="num">{_e(r["pool_key"])}</td></tr>'
            )
        p.append("</table>")
    else:
        p.append(
            '<div class="meta">No withdrawals from a known pool were found — '
            "not a Tornado exit on the pools analysed.</div>"
        )

    p.append('<h2>Dominant next hops<span class="rule"></span></h2>')
    if info["top_next_hops"]:
        p.append(
            '<div class="meta">Where this address sends transactions, ranked by count. '
            "A row with no native value is a contract call (for example a token "
            "transfer or approval), not native funds sent onward; token amounts are "
            "not traced here.</div>"
        )
        p.append(
            '<table><colgroup><col style="width:10%"><col style="width:16%">'
            '<col style="width:40%"><col></colgroup>'
            "<tr><th>Count</th><th>Total</th><th>Destination</th>"
            "<th>Attribution</th></tr>"
        )
        for h in info["top_next_hops"]:
            tag = format_label(h.get("label"))
            tag_cell = f'<span class="flag">{_e(tag)}</span>' if tag else "—"
            p.append(
                f'<tr><td class="num">{h["count"]}</td>'
                f'<td class="num">{_hop_total(h, network)}</td>'
                f'<td class="mono wrap">{_addr_link(network, h["address"])}</td>'
                f"<td>{tag_cell}</td></tr>"
            )
        p.append("</table>")
    else:
        p.append('<div class="meta">No outgoing transfers found.</div>')

    p.append(
        '<div class="callout">All of this is a <b>lead, not proof</b>. '
        "Corroborate the deposit→inflow timing and the next hop before drawing "
        "a conclusion.</div>"
    )
    p.append("</div></body></html>")
    return "".join(p)


def write_characterize_report(info: dict, path: str, network: Network) -> str:
    """Write the candidate-characterisation HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_characterize_report(info, network))
    return path


def build_cluster_report(traces: list[dict], network: Network) -> str:
    """Render cluster split-exit traces as a self-contained HTML string.

    Mirrors :func:`build_html_report`. Per wallet it lists the reconvergence
    points Z, the pool layers that feed each Z, and the per-asset totals (via
    :func:`format_totals`, so nothing is summed across assets). A Z fed by two or
    more pool layers is where a wallet's split exits reconverge - a structural
    lead. Tracing is one hop, so deeper reconvergence may be missed.
    """
    n_clusters = sum(len(t.get("clusters", [])) for t in traces)

    p, generated = _report_head(
        "Tornado.Cash split-exit clusters", "Forensic reconvergence report · research use"
    )
    p.append(
        f'<div class="meta">Wallets traced <b>{len(traces)}</b> · '
        f"network <b>{_e(network.name)}</b> · generated <b>{generated}</b></div>"
    )
    p.append("</div></div>")

    p.append('<div class="sheet">')

    # Metric strip
    p.append('<div class="metrics">')
    p.append(
        f'<div class="metric"><div class="k">Wallets</div>'
        f'<div class="v">{len(traces)}</div><div class="s">traced for split exits</div></div>'
    )
    p.append(
        f'<div class="metric"><div class="k">Reconvergence points</div>'
        f'<div class="v">{n_clusters}</div><div class="s">fed by 2+ pool layers</div></div>'
    )
    p.append("</div>")

    # Most likely: reconvergence points across all wallets
    all_z = []
    for trace in traces:
        for cluster in trace.get("clusters", []):
            all_z.append((trace["wallet"], cluster))
    all_z.sort(key=lambda wc: (-wc[1]["n_layers"], -len(wc[1]["totals"]), wc[1]["z"]))

    p.append('<h2>Most likely reconvergence points<span class="rule"></span></h2>')
    p.append(
        '<div class="meta">These are <b>probabilistic leads for corroboration, not '
        "proof</b>, from <b>1-hop</b> tracing that may miss deeper reconvergence. A "
        "downstream address fed by <b>two or more</b> pool layers is where a wallet's "
        "split exits reconverge — a structural lead.</div>"
    )
    if all_z:
        for wallet, cluster in all_z[:8]:
            p.append(
                '<div class="lead-card">'
                f'<div class="top">'
                f'<span class="addr wrap">{_addr_link(network, cluster["z"])}</span>'
                f'<span class="pill">{cluster["n_layers"]} pool layers</span></div>'
                f'<div class="why">Fed by {cluster["n_layers"]} pool layers '
                f"({_e(', '.join(str(k) for k in cluster['pools_covered']))}); "
                f"reconverged value {_e(format_totals(cluster['totals']))}.</div>"
                f'<div class="rat">Wallet {_e(wallet)} — a Z fed by 2+ pool layers is '
                "where split exits may reconverge. One hop is not an evidence band: a busy "
                "service (an exchange hot wallet, a DEX) can collect unrelated exits.</div>"
                "</div>"
            )
    else:
        p.append(
            '<div class="callout">No reconvergence point — no downstream address is '
            "fed by two or more pool layers within the traced window. The exits may "
            "reconverge beyond one hop, which this 1-hop tracer does not follow.</div>"
        )

    # Per-wallet detail
    p.append('<h2>Per-wallet clusters<span class="rule"></span></h2>')
    for trace in traces:
        fingerprint = trace.get("fingerprint", {})
        printable = format_fingerprint(fingerprint)
        p.append(
            f"<h3>{_addr_link(network, trace['wallet'])} "
            f'<span class="pill">{_e(printable)}</span></h3>'
        )
        clusters = trace.get("clusters", [])
        if not clusters:
            p.append('<div class="meta">No downstream address is fed by 2+ pool layers.</div>')
            continue
        p.append(
            '<table><colgroup><col style="width:34%"><col style="width:8%">'
            '<col style="width:22%"><col></colgroup>'
            "<tr><th>Reconvergence point Z</th><th>Layers</th>"
            "<th>Pools</th><th>Value funneled (per asset)</th></tr>"
        )
        for cluster in clusters[:40]:
            pools = ", ".join(str(k) for k in cluster["pools_covered"])
            p.append(
                f'<tr class="lead"><td class="mono flag wrap">{_addr_link(network, cluster["z"])}</td>'
                f'<td class="num">{cluster["n_layers"]}</td>'
                f'<td class="num">{_e(pools)}</td>'
                f'<td class="num">{_e(format_totals(cluster["totals"]))}</td></tr>'
            )
        p.append("</table>")

    p.append(
        '<h2>How to read this<span class="rule"></span></h2><div class="callout">'
        "This report lists <b>probabilistic leads, not proof</b>. Tracing is "
        "<b>1-hop</b>: a wallet whose exits reconverge only after several hops shows "
        "no cluster here. Per-asset totals are never summed across assets. Corroborate "
        "any reconvergence point with independent evidence before drawing a conclusion."
        "</div>"
    )
    p.append(
        f'<div class="foot">Generated by tornado-demix · analysis of public on-chain '
        f"data · {generated}. Not legal advice.</div>"
    )
    p.append("</div></body></html>")
    return "".join(p)


def write_cluster_report(traces: list[dict], path: str, network: Network) -> str:
    """Write the cluster-trace HTML report to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_cluster_report(traces, network))
    return path
