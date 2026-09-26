#!/usr/bin/env python3
"""Optional Flask web UI for tornado-demix.

Run from the repository root::

    pip install ".[web]"
    python webui/app.py
    # then open http://localhost:5000

A front-end over the same functions the CLI calls: demix, multi, cluster and
characterize, with a CSV download of the results table and the HTML reports.
"""

import csv
import io
import os
import secrets
import sys
from collections import OrderedDict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, abort, render_template, request, session  # noqa: E402

from tornado_demix import config  # noqa: E402
from tornado_demix.attribution import format_label, load_attribution  # noqa: E402
from tornado_demix.characterize import characterize_address  # noqa: E402
from tornado_demix.cluster import trace_wallet  # noqa: E402
from tornado_demix.demix import run_demix  # noqa: E402
from tornado_demix.errors import TornadoDemixError  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import (  # noqa: E402
    candidate_reason,
    conclusion,
    cross_method,
    method_breakdown,
    ranked_candidates,
)
from tornado_demix.multi import correlate  # noqa: E402
from tornado_demix.networks import DEFAULT_NETWORKS_CSV, load_networks  # noqa: E402
from tornado_demix.relayer import (  # noqa: E402
    analyze_relayers,
    collect_withdrawals,
    self_relayed_candidates,
    verify_broadcaster,
)
from tornado_demix.report import (  # noqa: E402
    analysis_assumptions,
    build_characterize_report,
    build_cluster_report,
    build_html_report,
    build_multi_report,
    demix_json,
    deposited_by_asset,
    format_fingerprint,
    format_totals,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(16))

MAX_WALLETS = 25

# Validated, not defaulted: the dispatch below ends in a bare else.
ANALYSES = ("demix", "multi", "cluster", "characterize")

# Above a year the run becomes a whole-chain scan that exhausts an API key.
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 365


def _window_days(raw):
    """Parse the window field. Returns ``(days, error_or_None)``."""
    if raw is None or str(raw).strip() == "":
        return 30, None
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        return 30, "Window must be a whole number of days."
    if not MIN_WINDOW_DAYS <= days <= MAX_WINDOW_DAYS:
        return 30, "Window must be between {} and {} days.".format(MIN_WINDOW_DAYS, MAX_WINDOW_DAYS)
    return days, None


def _exit_window(raw):
    """Parse the optional exit-window field (hours). Returns ``(hours_or_None, error)``.

    Empty means the full window. When set, the withdrawal search is narrowed to
    that many hours after each voucher's last deposit (see demix.voucher_windows).
    """
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        hours = float(str(raw).strip())
    except (TypeError, ValueError):
        return None, "Exit window must be a number of hours."
    if not 0 < hours <= MAX_WINDOW_DAYS * 24:
        return None, "Exit window must be between 0 and {} hours.".format(MAX_WINDOW_DAYS * 24)
    return hours, None


# Server-side store for the last runs' reports and CSV tables, which are too big
# for the signed-cookie session. The session holds only the token.
_STORE = OrderedDict()
_STORE_MAX = 20


def _store_put(payload):
    token = secrets.token_hex(8)
    _STORE[token] = payload
    while len(_STORE) > _STORE_MAX:
        _STORE.popitem(last=False)
    return token


def _store_get():
    return _STORE.get(session.get("token"))


def _fmt_ts(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_wallets(raw):
    """Split a textarea blob into validated, de-duplicated addresses."""
    seen, wallets, bad = set(), [], []
    for chunk in (raw or "").replace(",", "\n").replace(";", "\n").split("\n"):
        value = chunk.strip().lower()
        if not value:
            continue
        if config.is_address(value):
            if value not in seen:
                seen.add(value)
                wallets.append(value)
        else:
            bad.append(chunk.strip())
    return wallets, bad


def _base_context():
    networks = load_networks(DEFAULT_NETWORKS_CSV)
    return {
        "wallets_raw": "",
        "window_days": 30,
        "exit_window": "",
        "mode": "events",
        "analysis": "demix",
        "network": "ethereum",
        "networks": sorted(networks),
        "network_assets": {name: net.assets for name, net in networks.items()},
        "asset": "",
        "result": None,
        "error": None,
        "max_wallets": MAX_WALLETS,
    }


def _run_demix(client, wallets, net, window_days, mode, exit_window_hours=None):
    """Per-wallet demix. Returns (blocks, csv_rows, reports)."""
    blocks, rows, reports = [], [], {}
    labels = load_attribution(net.name)
    for wallet in wallets:
        data = run_demix(
            client,
            wallet,
            window_days=window_days,
            mode=mode,
            network=net,
            exit_window_hours=exit_window_hours,
        )
        reports[wallet] = build_html_report(data, net, attribution=labels)
        reports[wallet + ".json"] = demix_json(data, attribution=labels)
        denoms = []
        for pool_key, res in data["denoms"].items():
            candidates = []
            for n in res.get("target_counts", []):
                for addr in res["candidates_by_count"].get(n, []):
                    records = res["detail"][addr]
                    n_self = sum(1 for r in records if r.get("self_relayed"))
                    conf = res.get("confidence", {}).get(addr, 0.0)
                    sigs = res.get("signals", {}).get(addr, [])
                    candidates.append(
                        {
                            "address": addr,
                            "hits": n,
                            "url": net.addr_url(addr),
                            "self_relayed": n_self,
                            "confidence": round(conf, 3),
                            "signals": sigs,
                        }
                    )
                    rows.append(
                        [
                            wallet,
                            net.name,
                            pool_key,
                            addr,
                            n,
                            n_self,
                            round(conf, 3),
                            "|".join(sigs),
                        ]
                    )
            denoms.append(
                {
                    "pool_key": pool_key,
                    "pool": res["pool"],
                    "total": res["total_qualifying_withdrawals"],
                    "unique": res["unique_recipients"],
                    "candidates": candidates,
                }
            )
        relayers, leads = None, []
        if mode == "events":
            stats = analyze_relayers(collect_withdrawals(data))
            relayers = {
                "unique": stats["unique_relayers"],
                "self_relayed": len(stats["self_relayed"]),
                "top": list(stats["relayers"].items())[:5],
            }
            found = self_relayed_candidates(data)
            verify_broadcaster(client, found)
            leads = [
                {
                    "address": lead["to"],
                    "pool_key": lead["pool_key"],
                    "url": net.addr_url(lead["to"]),
                    "tx": lead["tx_hash"],
                    "tx_url": net.tx_url(lead["tx_hash"]),
                    "broadcaster": lead.get("broadcaster") or "",
                    "broadcaster_status": lead.get("broadcaster_status", "unverified"),
                    "time": _fmt_ts(lead["ts"]),
                }
                for lead in found
            ]
        ranked = [
            {
                "address": r["address"],
                "pool_key": r["pool_key"],
                "url": net.addr_url(r["address"]),
                "score": "{:.2f}".format(r["confidence"]),
                "bar": int(r["confidence"] * 100),
                "band": r["band"],
                "attribution": r["attribution"],
                "reason": candidate_reason(r),
                "disc": "{:.2f}".format(r["discrimination"]),
                "field": r["field_size"],
                "evidence": r["evidence"],
            }
            for r in ranked_candidates(data, attribution=labels)
        ]
        show_attr = any(r["attribution"] for r in ranked)
        methods = []
        for m in method_breakdown(data):
            if not m["rows"]:
                continue
            for r in m["rows"]:
                r["url"] = net.addr_url(r["address"])
            methods.append({"label": m["label"], "rows": m["rows"]})
        xmatch = [
            {
                "address": r["address"],
                "pool_key": r["pool_key"],
                "url": net.addr_url(r["address"]),
                "n_methods": r["n_methods"],
                "methods": r["methods"],
                "reasons": r["reasons"],
            }
            for r in cross_method(data)
        ]
        top_band = ranked[0]["band"] if ranked else "none"
        blocks.append(
            {
                "wallet": wallet,
                "vouchers": [
                    {
                        "pool_key": v["pool_key"],
                        "count": v["count"],
                        "first": _fmt_ts(v["first_ts"]),
                    }
                    for v in data["vouchers"]
                ],
                "denoms": denoms,
                "relayers": relayers,
                "leads": leads,
                "ranked": ranked,
                "show_attr": show_attr,
                "methods": methods,
                "xmatch": xmatch,
                "conclusion": conclusion(data),
                "no_deposits": not data["vouchers"],
                "deposited": format_totals(deposited_by_asset(data["vouchers"])),
                # Never searched, which is not the same as "no candidates".
                "unresolved": [
                    {"pool_key": u["pool_key"], "reason": u["reason"]}
                    for u in data.get("unresolved", [])
                ],
                "n_notes": len(data["deposits"]),
                "n_candidates": len(ranked),
                "top_band": top_band,
                "assumptions": analysis_assumptions(data),
            }
        )
    header = [
        "wallet",
        "network",
        "pool",
        "candidate_address",
        "hit_count",
        "self_relayed_count",
        "confidence",
        "signals",
    ]
    return blocks, (header, rows), reports


def _run_multi(client, wallets, net, window_days, mode, exit_window_hours=None):
    corr = correlate(
        client, wallets, window_days, mode=mode, network=net, exit_window_hours=exit_window_hours
    )
    report = build_multi_report(corr, net)
    strong = [
        {"pool_key": k, "address": a, "url": net.addr_url(a), "wallets": ws}
        for k, a, ws in corr["strong"][:200]
    ]
    profiles = []
    rows = []
    for wallet, matches in corr["profile_matches"].items():
        fp = corr["fingerprints"][wallet]
        printable = format_fingerprint(fp)
        for addr, detail, exact in matches:
            desc = ", ".join(f"{d}:{r}/{n}" for d, (r, n) in sorted(detail.items()))
            profiles.append(
                {
                    "wallet": wallet,
                    "fingerprint": printable,
                    "address": addr,
                    "url": net.addr_url(addr),
                    "exact": exact,
                    "detail": desc,
                }
            )
            rows.append([wallet, printable, addr, "exact" if exact else "gte", desc])
    grades = corr.get("consolidator_grades", {})
    band_rank = {"strong": 0, "moderate": 1, "weak": 2}
    cross = sorted(
        (
            {
                "address": a,
                "url": net.addr_url(a),
                "wallets": ws,
                "band": grades.get(a, {}).get("band", "weak"),
                "artefact": grades.get(a, {}).get("artefact_kind"),
            }
            for a, ws in corr["cross_profile"].items()
        ),
        key=lambda c: (band_rank.get(c["band"], 2), -len(c["wallets"]), c["address"]),
    )
    header = ["wallet", "fingerprint", "consolidator_address", "match_type", "received_vs_needed"]
    return (
        {"strong": strong, "profiles": profiles, "cross": cross, "n_strong": len(corr["strong"])},
        (header, rows),
        {"multi": report},
    )


def _run_cluster(client, wallets, net, window_days, cache_dir=".cache"):
    traces, rows, raw_traces = [], [], []
    for wallet in wallets:
        trace = trace_wallet(client, wallet, cache_dir, window_days, network=net)
        raw_traces.append(trace)
        printable = format_fingerprint(trace["fingerprint"])
        clusters = []
        for cluster in trace["clusters"][:20]:
            totals = format_totals(cluster["totals"])
            clusters.append(
                {
                    "z": cluster["z"],
                    "url": net.addr_url(cluster["z"]),
                    "layers": cluster["n_layers"],
                    "pools": ", ".join(str(k) for k in cluster["pools_covered"]),
                    "totals": totals,
                }
            )
            rows.append([wallet, printable, cluster["z"], cluster["n_layers"], totals])
        traces.append({"wallet": wallet, "fingerprint": printable, "clusters": clusters})
    report = build_cluster_report(raw_traces, net)
    header = ["wallet", "fingerprint", "cluster_point_z", "num_pool_layers", "value_funneled"]
    return traces, (header, rows), {"cluster": report}


def _run_characterize(client, address, net):
    """Characterise one exit-candidate address. Returns (view, table, reports)."""
    labels = load_attribution(net.name)
    info = characterize_address(client, address, net, labels=labels)
    totals = ", ".join(f"{v} {a}" for a, v in sorted(info["inflow_totals"].items()))
    inflows = [
        {
            "when": datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "value": r["value"],
            "asset": r["asset"],
            "pool": r["pool_key"],
        }
        for r in info["pool_inflows"]
    ]
    next_hops = [
        {
            "count": h["count"],
            "total": h["total_value"],
            "call": h.get("kind") == "call",
            "address": h["address"],
            "url": net.addr_url(h["address"]),
            "label": format_label(h.get("label")),
        }
        for h in info["top_next_hops"]
    ]
    view = {
        "address": info["address"],
        "label": format_label(info.get("label")),
        "classification": info["classification"],
        "reason": info["classification_reason"],
        "normal_txs": info["normal_txs"],
        "internal_txs": info["internal_txs"],
        "n_inflows": len(info["pool_inflows"]),
        "n_pools": len(info["distinct_pools"]),
        "totals": totals,
        "inflows": inflows,
        "next_hops": next_hops,
    }
    header = ["when_utc", "value", "asset", "pool"]
    rows = [[r["when"], r["value"], r["asset"], r["pool"]] for r in inflows]
    return view, (header, rows), {"characterize": build_characterize_report(info, net)}


@app.route("/", methods=["GET", "POST"])
def index():
    ctx = _base_context()
    if request.method != "POST":
        return render_template("index.html", **ctx)

    raw = request.form.get("wallets") or ""
    mode = "events"
    analysis = request.form.get("analysis") or "demix"
    network_name = request.form.get("network") or "ethereum"
    ctx.update(wallets_raw=raw, mode=mode, analysis=analysis, network=network_name)

    # Parse the numeric fields before the try block so bad input becomes a message,
    # not a 500, and echo them back so one error does not blank the other field.
    ctx["exit_window"] = request.form.get("exit_window") or ""
    window_days, error = _window_days(request.form.get("window_days"))
    ctx["window_days"] = window_days
    if error:
        ctx["error"] = error
        return render_template("index.html", **ctx)

    exit_window, error = _exit_window(request.form.get("exit_window"))
    if error:
        ctx["error"] = error
        return render_template("index.html", **ctx)

    if analysis not in ANALYSES:
        ctx["error"] = "Unknown analysis '{}'. Choose one of: {}.".format(
            analysis, ", ".join(sorted(ANALYSES))
        )
        ctx["analysis"] = "demix"
        return render_template("index.html", **ctx)

    wallets, bad = _parse_wallets(raw)
    if not wallets:
        ctx["error"] = "Enter at least one 0x-prefixed address (one per line)."
        return render_template("index.html", **ctx)
    if len(wallets) > MAX_WALLETS:
        ctx["error"] = f"Too many addresses ({len(wallets)}). Limit is {MAX_WALLETS}."
        return render_template("index.html", **ctx)
    if analysis == "multi" and len(wallets) < 2:
        ctx["error"] = "Cross-wallet correlation needs at least two addresses."
        return render_template("index.html", **ctx)
    if analysis == "characterize" and len(wallets) != 1:
        ctx["error"] = "Characterise takes exactly one candidate address."
        return render_template("index.html", **ctx)

    networks = load_networks(DEFAULT_NETWORKS_CSV)
    if network_name not in networks:
        ctx["error"] = f"Unknown network '{network_name}'."
        return render_template("index.html", **ctx)
    net = networks[network_name]

    try:
        api_key = config.load_api_key()
    except TornadoDemixError as exc:
        ctx["error"] = str(exc)
        return render_template("index.html", **ctx)

    reports = {}
    try:
        client = EtherscanClient(api_key, **net.client_kwargs())
        if analysis == "demix":
            blocks, table, reports = _run_demix(
                client, wallets, net, window_days, mode, exit_window
            )
            payload = {"kind": "demix", "blocks": blocks}
        elif analysis == "multi":
            data, table, reports = _run_multi(client, wallets, net, window_days, mode, exit_window)
            payload = {"kind": "multi", **data}
        elif analysis == "characterize":
            view, table, reports = _run_characterize(client, wallets[0], net)
            payload = {"kind": "characterize", **view}
        else:
            traces, table, reports = _run_cluster(client, wallets, net, window_days)
            payload = {"kind": "cluster", "traces": traces}
    except Exception as exc:  # surface in the UI rather than a 500
        ctx["error"] = f"Analysis failed: {exc}"
        return render_template("index.html", **ctx)

    header, rows = table
    session["token"] = _store_put(
        {
            "csv": {"header": header, "rows": rows},
            "reports": reports,
        }
    )
    # multi/cluster/characterize produce one report, stored under the analysis name.
    report_key = None
    if analysis in ("multi", "cluster", "characterize") and reports:
        report_key = analysis
    payload.update(
        skipped=bad,
        network=net.name,
        currency=net.currency,
        assets=net.assets,
        n_wallets=len(wallets),
        has_csv=bool(rows),
        report_wallets=[k for k in reports if not k.endswith(".json")],
        report_key=report_key,
    )
    ctx["result"] = payload
    return render_template("index.html", **ctx)


@app.route("/download.csv")
def download_csv():
    """Stream the last result table as CSV."""
    store = _store_get()
    data = store and store.get("csv")
    if not data:
        return Response("No results to download yet.\n", mimetype="text/plain", status=404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(data["header"])
    writer.writerows(data["rows"])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tornado_demix_results.csv"},
    )


@app.route("/report/<wallet>.html")
def download_report(wallet):
    """Serve a standalone HTML report from the last run.

    The key is a wallet address for a demix run, or the fixed name 'multi' /
    'cluster' for the one combined report those analyses produce.
    """
    store = _store_get()
    reports = (store or {}).get("reports") or {}
    doc = reports.get(wallet.lower())
    if not doc:
        abort(404)
    return Response(
        doc,
        mimetype="text/html",
        headers={"Content-Disposition": f"attachment; filename=tornado_report_{wallet[:16]}.html"},
    )


@app.route("/result/<wallet>.json")
def download_json(wallet):
    """Serve the last demix run for ``wallet`` as JSON."""
    store = _store_get()
    doc = ((store or {}).get("reports") or {}).get(wallet.lower() + ".json")
    if not doc:
        abort(404)
    return Response(
        doc,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=tornado_result_{wallet[:16]}.json"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
