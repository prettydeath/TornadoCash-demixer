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
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import (  # noqa: E402
    Flask,
    Response,
    abort,
    copy_current_request_context,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from tornado_demix import config, honest  # noqa: E402
from tornado_demix.attribution import (  # noqa: E402
    fetch_attribution,
    format_label,
    load_attribution,
)
from tornado_demix.blockscout_labels import fetch_and_cache, with_blockscout  # noqa: E402
from tornado_demix.bridges import Destinations  # noqa: E402
from tornado_demix.bridges_canonical import recipient_links  # noqa: E402
from tornado_demix.characterize import characterize_address, run_classify  # noqa: E402
from tornado_demix.cluster import trace_wallet  # noqa: E402
from tornado_demix.demix import run_demix  # noqa: E402
from tornado_demix.deposit_addresses import (  # noqa: E402
    check_text,
    kind_label,
    target_label,
)
from tornado_demix.errors import ConfigError, TornadoDemixError  # noqa: E402
from tornado_demix.etherscan import DEFAULT_MAX_WAIT, EtherscanClient  # noqa: E402
from tornado_demix.heuristics import (  # noqa: E402
    candidate_reason,
    conclusion,
    cross_method,
    method_breakdown,
    ranked_candidates,
)
from tornado_demix.incomplete import lines as incomplete_lines  # noqa: E402
from tornado_demix.incomplete import notice_only  # noqa: E402
from tornado_demix.incomplete import summary as incomplete_summary  # noqa: E402
from tornado_demix.multi import correlate, exact_k_skips, exact_k_tail_text  # noqa: E402
from tornado_demix.networks import DEFAULT_NETWORKS_CSV, load_networks  # noqa: E402
from tornado_demix.placebo import BANDS, run_placebo  # noqa: E402
from tornado_demix.relayer import (  # noqa: E402
    analyze_relayers,
    collect_withdrawals,
    fee_key,
    fee_label,
    frequency_text,
    verify_run,
)
from tornado_demix.report import (  # noqa: E402
    _sanitize_row,
    analysis_assumptions,
    build_characterize_report,
    build_classify_report,
    build_cluster_report,
    build_html_report,
    build_multi_report,
    build_trace_report,
    candidate_cells,
    demix_json,
    deposited_by_asset,
    format_fingerprint,
    format_totals,
    json_sha256,
    multi_json,
    ranked_index,
    terminal_bridge_text,
)
from tornado_demix.report_html import REPORT_CSS, REPORT_JS  # noqa: E402
from tornado_demix.rpc import make_contract_check  # noqa: E402
from tornado_demix.trace import (  # noqa: E402
    END_STATES_NOTE,
    MAX_HOPS,
    arrival_text,
    bridge_text,
    bridge_tx_label,
    share_status,
    share_text,
    trace_funds,
)  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(16))

# The server binds to 127.0.0.1 only. A page on another site can still make the
# browser send requests to it under an attacker's DNS name (DNS rebinding), so any
# Host that is not loopback is refused.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})


def csp_nonce():
    """Per-response nonce for inline scripts (``<script nonce="{{ csp_nonce }}">``).

    The Content-Security-Policy header of the same response names it, so an inline
    script runs only if the template carries it. Use this for any inline script;
    never loosen ``script-src`` to ``'unsafe-inline'``.
    """
    if "csp_nonce" not in g:
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


@app.context_processor
def _inject_csp_nonce():
    return {"csp_nonce": csp_nonce()}


def _content_security_policy():
    # style-src keeps 'unsafe-inline': the page uses style="" attributes and a nonce
    # cannot cover an attribute. Scripts, the part that matters, are nonce-only.
    return "; ".join(
        (
            "default-src 'self'",
            "script-src 'self' 'nonce-{}'".format(csp_nonce()),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


def _request_hostname():
    host = (request.host or "").strip().lower()
    if host.startswith("["):  # bracketed IPv6 literal: never in the allowlist
        return host
    return host.rsplit(":", 1)[0] if ":" in host else host


@app.before_request
def _refuse_foreign_hosts():
    if _request_hostname() not in ALLOWED_HOSTS:
        abort(400)


@app.after_request
def _security_headers(response):
    response.headers["Content-Security-Policy"] = _content_security_policy()
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _csrf_ok():
    """True only for a non-empty session token matched by a non-empty form token."""
    expected = session.get("csrf") or ""
    given = request.form.get("csrf") or ""
    return bool(expected) and bool(given) and secrets.compare_digest(given, expected)


app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

MAX_WALLETS = 25

# Form fields echoed back into the page when a background job's result is shown.
_ECHO_FIELDS = (
    "wallets_raw",
    "mode",
    "analysis",
    "network",
    "window_days",
    "exit_window",
    "placebo",
    "deposit_addresses",
    "deposit_check",
    "root_check",
    "group",
    "exact_k",
    "labels_blockscout",
    "trace_amount",
    "trace_start_block",
    "trace_token",
    "trace_max_hops",
    "trace_arrival_tx",
    "known_exits",
    "max_wait",
    "pause",
    "http_cache",
)

# Validated, not defaulted: the dispatch below ends in a bare else.
ANALYSES = ("demix", "multi", "cluster", "characterize", "trace", "classify")

# The modes in which each mode-specific field is shown (and validated).
FIELD_MODES = {
    "window_days": ("demix", "multi", "cluster"),
    "exit_window": ("demix", "multi"),
    "known_exits": ("demix",),
    "trace": ("trace",),
}

# The modes of the form: what each one asks for in the address field. Only the
# fields of the chosen mode are shown (the template and its script read this).
MODES = (
    {
        "key": "demix",
        "title": "Demix",
        "hint": "exit leads per depositor",
        "label": f"Depositor wallets, one address per line (max {MAX_WALLETS})",
        "placeholder": "0x0000000000000000000000000000000000000001",
        "single": False,
    },
    {
        "key": "multi",
        "title": "Multi",
        "hint": "correlate 2+ depositors",
        "label": f"Depositor wallets to correlate, one per line (2 to {MAX_WALLETS})",
        "placeholder": "0x0000000000000000000000000000000000000001 (one per line)",
        "single": False,
    },
    {
        "key": "cluster",
        "title": "Cluster",
        "hint": "split exits reconverging",
        "label": f"Depositor wallets, one address per line (max {MAX_WALLETS})",
        "placeholder": "0x0000000000000000000000000000000000000001",
        "single": False,
    },
    {
        "key": "characterize",
        "title": "Characterize",
        "hint": "one exit address",
        "label": "Exit address to characterise (one)",
        "placeholder": "0x0000000000000000000000000000000000000001",
        "single": True,
    },
    {
        "key": "trace",
        "title": "Trace",
        "hint": "follow funds from one exit",
        "label": "Exit address the funds arrived at (one)",
        "placeholder": "0x0000000000000000000000000000000000000001",
        "single": True,
    },
    {
        "key": "classify",
        "title": "Classify address",
        "hint": "deposit-address check",
        "label": "Address to check (one)",
        "placeholder": "0x0000000000000000000000000000000000000001",
        "single": True,
    },
)
MAX_TRACE_HOPS = 8

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
# for the signed-cookie session. The session holds only the token. The store lives
# in this process: run the UI as one process (python webui/app.py), not under a
# multi-worker server.
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


# Provider settings of the form (same meaning as the CLI flags --max-wait, --pause,
# --http-cache).
MAX_WAIT_LIMIT = 3600
PAUSE_LIMIT = 60


def _network_options(form):
    """Parse the provider settings. Returns ``(options, error_or_None)``.

    ``options`` has ``max_wait`` and ``pause`` (seconds) and ``cache_dir`` (a path or
    None). Empty fields mean the defaults: 120 s, no pause, no cache.
    """
    options = {"max_wait": DEFAULT_MAX_WAIT, "pause": 0.0, "cache_dir": None}
    for field, key, limit, label in (
        ("max_wait", "max_wait", MAX_WAIT_LIMIT, "Longest backoff"),
        ("pause", "pause", PAUSE_LIMIT, "Pause between calls"),
    ):
        raw = (form.get(field) or "").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            return options, "{} must be a number of seconds.".format(label)
        if not 0 <= value <= limit:
            return options, "{} must be between 0 and {} seconds.".format(label, limit)
        options[key] = value
    cache = (form.get("http_cache") or "").strip()
    if cache:
        if len(cache) > 260 or "\0" in cache:
            return options, "The HTTP cache directory is not a usable path."
        options["cache_dir"] = cache
    return options, None


# Background jobs: a long analysis runs in a thread and the page polls ``GET /job/<id>``.
# The table lives in this process (like _STORE); a job belongs to the session that
# started it (matched on the CSRF token) and the endpoint is read-only.
#
#   POST /  with async=1        -> 303 to /?job=<id> (409 + message while a job runs)
#   GET  /job/<id>              -> JSON {state: running|done|error, elapsed, calls,
#                                        cache_hits, waited, rpc_fallbacks, error}
#   GET  /?job=<id>             -> status box while running; then the finished result (or the error) in the normal page
_JOBS = OrderedDict()
_JOBS_MAX = 8
_JOBS_LOCK = threading.Lock()


def _job_start(analyse, echo):
    """Run ``analyse(job)`` in a thread; return the job record, or None while another
    job is still running (runs share one API key, so they go one at a time)."""
    job = {
        "id": secrets.token_urlsafe(12),
        "owner": session.get("csrf"),
        "state": "running",
        "started": time.time(),
        "finished": None,
        "echo": echo,
        "client": None,
        "error": None,
        "payload": None,
        "store": None,
        "token": None,
    }

    @copy_current_request_context
    def work():
        try:
            job["payload"], job["store"] = analyse(job)
            job["state"] = "done"
        except Exception as exc:  # surfaced in the page, like a synchronous run
            job["error"] = "Analysis failed: {}".format(exc)
            job["state"] = "error"
        job["finished"] = time.time()

    job["thread"] = threading.Thread(target=work, daemon=True)
    with _JOBS_LOCK:
        if any(j["state"] == "running" for j in _JOBS.values()):
            return None  # one run at a time: they share the API key's rate limit
        _JOBS[job["id"]] = job
        # evict finished jobs, oldest first; a running job is never dropped
        for old_id in [i for i, j in _JOBS.items() if j["state"] != "running"]:
            if len(_JOBS) <= _JOBS_MAX:
                break
            del _JOBS[old_id]
    job["thread"].start()
    return job


def _running_job():
    """The job that is running now (any session), or None."""
    with _JOBS_LOCK:
        return next((j for j in _JOBS.values() if j["state"] == "running"), None)


def _busy(ctx):
    """The page refused because a background run is still going (409)."""
    running = _running_job()
    link = ""
    if running is not None and _job_get(running["id"]) is not None:
        link = " Its status is at /?job={}.".format(running["id"])
    ctx["error"] = (
        "Another analysis is still running; wait for it to finish, then submit again." + link
    )
    return render_template("index.html", **ctx), 409


def _job_get(job_id):
    """The job of this session, or None (unknown id, other session, evicted)."""
    job = _JOBS.get(job_id or "")
    owner = session.get("csrf")
    if job and owner and secrets.compare_digest(str(job["owner"] or ""), owner):
        return job
    return None


def _job_status(job):
    stats = {}
    stats_fn = getattr(job.get("client"), "stats", None)
    if callable(stats_fn):
        try:
            stats = stats_fn()
        except Exception:  # a status poll must never fail the page
            stats = {}
    elapsed = (job["finished"] or time.time()) - job["started"]
    return {
        "state": job["state"],
        "elapsed": round(elapsed, 1),
        "calls": stats.get("calls", 0),
        "cache_hits": stats.get("cache_hits", 0),
        "waited": stats.get("waited", 0),
        "rpc_fallbacks": stats.get("rpc_fallbacks", 0),
        "error": job["error"],
    }


def _fmt_ts(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_wallets(raw):
    """Split a textarea blob into validated, de-duplicated addresses.

    A mixed-case address must pass its EIP-55 checksum; one that does not (or
    cannot be checked without the ``labels`` extra) is reported in ``bad``, and
    ``_wallet_problem`` says why.
    """
    seen, wallets, bad = set(), [], []
    for chunk in (raw or "").replace(",", "\n").replace(";", "\n").split("\n"):
        if not chunk.strip():
            continue
        try:
            value = config.normalize_address(chunk)
        except ConfigError:
            bad.append(chunk.strip())
            continue
        if value not in seen:
            seen.add(value)
            wallets.append(value)
    return wallets, bad


def _wallet_problem(bad):
    """One user-facing sentence for the first well-formed address that failed its checksum."""
    for chunk in bad:
        if config.is_address(chunk):
            try:
                config.normalize_address(chunk)
            except ConfigError as exc:
                return str(exc)
    return None


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
        "limitations": honest.limitations(),
        "exploratory_banner": honest.exploratory_banner(),
        "how_to_read": honest.how_to_read(),
        "band_note": honest.band_note(),
        "shared_title": honest.SHARED_CANDIDATES_TITLE,
        "shared_caveat": honest.SHARED_CANDIDATES_CAVEAT,
        "max_wallets": MAX_WALLETS,
        "max_trace_hops": MAX_TRACE_HOPS,
        "default_hops": MAX_HOPS,
        "trace_amount": "",
        "trace_start_block": "",
        "trace_token": "",
        "trace_max_hops": "",
        "trace_arrival_tx": "",
        "known_exits": "",
        "placebo": False,
        "deposit_addresses": True,
        "deposit_check": True,
        "root_check": False,
        "group": False,
        "exact_k": False,
        "group_title": honest.GROUP_VIEW_TITLE,
        "exact_k_title": honest.EXACT_K_TITLE,
        "labels_blockscout": False,
        "max_wait": "",
        "max_wait_limit": MAX_WAIT_LIMIT,
        "pause_limit": PAUSE_LIMIT,
        "pause": "",
        "http_cache": "",
        "job": None,
        "blockscout_max": BLOCKSCOUT_MAX_ADDRESSES,
        "modes": MODES,
        "modes_by_key": {m["key"]: m for m in MODES},
        "error_field": None,
        "field_modes": FIELD_MODES,
        "report_css": REPORT_CSS,
        "report_js": REPORT_JS,
    }


def _root_row(c, net):
    return {
        "pool_key": c["pool_key"],
        "address": c["address"],
        "url": net.addr_url(c["address"]),
        "withdrawals": [
            {"tx_hash": w["tx_hash"], "url": net.tx_url(w["tx_hash"]), "n_root": w["n_root"]}
            for w in c["withdrawals"]
        ],
    }


def _root_check_view(data, net):
    """Merkle-root check as the template needs it: counts and the fully excluded candidates."""
    rc = data.get("root_check")
    if not rc:
        return None
    return {
        "checked": rc["withdrawals_checked"],
        "found": rc["roots_found"],
        "not_found": rc["roots_not_found"],
        "outside_history": rc.get("roots_outside_history", 0),
        "ambiguous": rc.get("roots_ambiguous", 0),
        "history": ", ".join(
            "{} {}".format(k, v) for k, v in sorted((rc.get("history_sizes") or {}).items())
        ),
        "calls": rc["tx_info_calls"],
        "incomplete": rc["incomplete"],
        "unchecked_pools": ", ".join(rc["unchecked_pools"]),
        "excluded": [_root_row(c, net) for c in rc["excluded_candidates"]],
        "dropped": [_root_row(c, net) for c in rc.get("dropped_after_recount", [])],
    }


def _placebo_view(pb, net):
    """Placebo check as the template needs it: one row per band, decoy leads with links."""
    if not pb:
        return None
    window = pb["decoy_window"]
    return {
        "offset_days": "{:g}".format(pb["offset_days"]),
        "window": "{} .. {}".format(_fmt_ts(window[0]), _fmt_ts(window[1])) if window else "",
        "target_withdrawals": pb["target_withdrawals"],
        "decoy_withdrawals": pb["decoy_withdrawals"],
        "rows": [
            {
                "band": b,
                "target": pb["target"][b],
                "decoy": pb["decoy"][b],
                "target_rate": _rate(pb["target_per_1000"][b]),
                "decoy_rate": _rate(pb["decoy_per_1000"][b]),
                "verdict": pb["verdicts"][b],
            }
            for b in BANDS
        ],
        "leads": [dict(lead, url=net.addr_url(lead["address"])) for lead in pb["decoy_leads"]],
        "caveats": pb["caveats"],
        "note": pb["note"],
        "direct_link": pb.get("direct_link_verdict"),
    }


def _incomplete_view(result):
    """{summary, lines} for the "results may be incomplete" box, or None."""
    text = incomplete_summary(result)
    if not text:
        return None
    return {
        "summary": text,
        "lines": incomplete_lines(result),
        "notice": notice_only(result),
    }


def _rate(value):
    return "n/a" if value is None else "{:g}".format(value)


def _labels(net):
    """Attribution labels, plus cached Blockscout tags when the form ticked the box."""
    labels = load_attribution(net.name)
    if has_request_context() and request.form.get("labels_blockscout"):
        directory = os.path.join(".cache", "labels", "blockscout")
        labels = with_blockscout(labels, net.chain_id, directory)
    return labels


def _demix_command(
    wallet, net, window_days, exit_window_hours, placebo, deposit_addresses, root_check
):
    """The equivalent CLI command of a web UI demix run, for the report's provenance."""
    parts = ["tornado-demix", "demix", wallet, "--network", net.name]
    if exit_window_hours:
        parts += ["--exit-window", str(exit_window_hours)]
    else:
        parts += ["--window-days", str(window_days)]
    if placebo:
        parts.append("--placebo")
    if not deposit_addresses:
        parts.append("--no-deposit-addresses")
    if root_check:
        parts.append("--root-check")
    return "web UI run, equivalent to: " + " ".join(parts)


def _run_demix(
    client,
    wallets,
    net,
    window_days,
    mode,
    exit_window_hours=None,
    known_exits=(),
    placebo=False,
    deposit_addresses=True,
    root_check=False,
):
    """Per-wallet demix. Returns (blocks, csv_rows, reports)."""
    blocks, rows, reports = [], [], {}
    labels = _labels(net)
    for index, wallet in enumerate(wallets, 1):
        data = run_demix(
            client,
            wallet,
            window_days=window_days,
            mode=mode,
            network=net,
            exit_window_hours=exit_window_hours,
            known_exits=known_exits,
            labels=labels,
            deposit_addresses=deposit_addresses,
            root_check=root_check,
        )
        if placebo and data["deposits"]:
            data["placebo"] = run_placebo(
                client,
                wallet,
                data,
                window_days,
                network=net,
                labels=labels,
                exit_window_hours=exit_window_hours,
                mode=mode,
                root_check=root_check,
            )
        relayers, leads, fingerprints = None, [], []
        if mode == "events":
            stats = analyze_relayers(collect_withdrawals(data))
            relayers = {
                "unique": stats["unique_relayers"],
                "self_relayed": len(stats["self_relayed"]),
                "top": list(stats["relayers"].items())[:5],
            }
            found, _context, groups = verify_run(client, data)
            fingerprints = [
                dict(
                    g,
                    links=[{"address": r, "url": net.addr_url(r)} for r in g["recipients"]],
                    how_common=frequency_text(g),
                )
                for g in groups
                if g["shared"]
            ]
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
                    "fee": fee_label(fee_key(lead.get("fee_profile"))),
                }
                for lead in found
            ]
        data["provenance"] = honest.provenance(
            net,
            command=_demix_command(
                wallet, net, window_days, exit_window_hours, placebo, deposit_addresses, root_check
            ),
            label_sets=honest.label_sets_for(
                net,
                blockscout_dir=os.path.join(".cache", "labels", "blockscout")
                if has_request_context() and request.form.get("labels_blockscout")
                else None,
            ),
        )
        json_text = demix_json(data, attribution=labels)
        reports[wallet + ".json"] = json_text
        digest = json_sha256(json_text)
        reports[wallet] = build_html_report(data, net, attribution=labels, json_sha256=digest)
        report_body = build_html_report(
            data, net, attribution=labels, json_sha256=digest, fragment=True, uid=f"w{index}"
        )
        denoms = []
        ranked_idx = ranked_index(data)
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
                    band, _score, families, _signals, hashes = candidate_cells(
                        data, addr, [pool_key], ranked_idx
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
                            band,
                            families,
                            hashes,
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
                "early": r.get("early_exit", False),
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
                "report_body": report_body,
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
                "fingerprints": fingerprints,
                "ranked": ranked,
                "show_attr": show_attr,
                "methods": methods,
                "xmatch": xmatch,
                "conclusion": conclusion(data),
                "groups": [
                    {
                        "pool_key": g["pool_key"],
                        "notes": g["notes"],
                        "anchors": ", ".join(f"{a['address']} ({a['why']})" for a in g["anchors"]),
                        "members": [{"address": a, "url": net.addr_url(a)} for a in g["members"]],
                    }
                    for g in data.get("exit_groups", [])
                    if g["anchored"]
                ],
                "deposit_addresses": [
                    {
                        "address": d["address"],
                        "url": net.addr_url(d["address"]),
                        "kind": kind_label(d),
                        "target": target_label(d),
                        "senders": len(d.get("senders", [])),
                        "shared_by": [
                            {"address": addr, "pool_key": pool_key, "url": net.addr_url(addr)}
                            for pool_key, addr, addrs in data.get("heuristics", {}).get(
                                "shared_deposits", []
                            )
                            if d["address"] in addrs
                        ],
                    }
                    for d in data.get("deposit_addresses") or []
                ],
                "n_unanchored": sum(1 for g in data.get("exit_groups", []) if not g["anchored"]),
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
                "placebo": _placebo_view(data.get("placebo"), net),
                "root_check": _root_check_view(data, net),
                "incomplete": _incomplete_view(data),
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
        "band",
        "families",
        "evidence_tx_hashes",
    ]
    return blocks, (header, rows), reports


def _group_view(group, net):
    """The group view of a multi run for the page, or None without one."""
    if group is None:
        return None
    candidates = [
        {
            "address": r["address"],
            "url": net.addr_url(r["address"]),
            "pool_key": r["pool_key"],
            "band": r["band"],
            "confidence": r["confidence"],
            "hits": r["hits"],
            "signals": r["signals"],
        }
        for r in ranked_candidates(group)
    ]
    windows = [
        {
            "pool_key": pool_key,
            "blocks": f"{w.get('start_block')}..{w.get('end_block')}",
            "withdrawals": res.get("total_qualifying_withdrawals", 0),
            "recipients": res.get("unique_recipients", 0),
        }
        for pool_key, res in sorted(group.get("denoms", {}).items())
        for w in res.get("windows", [])
    ]
    return {
        "caveat": group.get("caveat", ""),
        "wallets": group.get("wallets", []),
        "profile": format_fingerprint(group.get("profile", {})) or "no deposits",
        "windows": windows,
        "unresolved": group.get("unresolved", []),
        "candidates": candidates[:200],
        "n_candidates": len(candidates),
        "exact_k": [
            dict(r, tail_text=exact_k_tail_text(r), skips=exact_k_skips(r))
            for r in group.get("exact_k", [])
        ],
        "exact_k_caveat": group.get("exact_k_caveat", ""),
        "notices": group.get("notices", []),
    }


def _run_multi(
    client, wallets, net, window_days, mode, exit_window_hours=None, group=False, exact_k=False
):
    corr = correlate(
        client,
        wallets,
        window_days,
        mode=mode,
        network=net,
        exit_window_hours=exit_window_hours,
        labels=_labels(net),
        group=group,
        exact_k=group and exact_k,
    )
    report = build_multi_report(corr, net)
    report_body = build_multi_report(corr, net, fragment=True)
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
        {
            "strong": strong,
            "profiles": profiles,
            "cross": cross,
            "n_strong": len(corr["strong"]),
            "funders": [
                {"address": f, "url": net.addr_url(f), "wallets": ws}
                for f, ws in corr.get("shared_funders", {}).items()
            ],
            "incomplete": _incomplete_view(corr),
            "clusters": [
                {
                    "wallets": c["wallets"],
                    "reasons": sorted({e[2] for e in c["edges"]}),
                }
                for c in corr.get("operator_clusters", [])
            ],
            "group": _group_view(corr.get("group"), net),
            "report_body": report_body,
            "json_key": "multi",
        },
        (header, rows),
        {"multi": report, "multi.json": multi_json(corr)},
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
        traces.append(
            {
                "wallet": wallet,
                "fingerprint": printable,
                "clusters": clusters,
                "incomplete": _incomplete_view(trace),
            }
        )
    report = build_cluster_report(raw_traces, net)
    header = ["wallet", "fingerprint", "cluster_point_z", "num_pool_layers", "value_funneled"]
    reports = {
        "cluster": report,
        "cluster.json": json.dumps(raw_traces, indent=1, sort_keys=True, default=str),
    }
    return traces, (header, rows), reports, build_cluster_report(raw_traces, net, fragment=True)


def _trace_params(form):
    """Parse the trace fields. Returns ``(params, error_or_None)``."""
    try:
        amount = float((form.get("trace_amount") or "").strip())
    except ValueError:
        return None, "Trace needs the amount to follow, as a number."
    if not amount > 0:
        return None, "The amount to follow must be greater than 0."
    raw_block = (form.get("trace_start_block") or "").strip()
    raw_hops = (form.get("trace_max_hops") or "").strip()
    try:
        start_block = int(raw_block) if raw_block else 0
        max_hops = int(raw_hops) if raw_hops else MAX_HOPS
    except ValueError:
        return None, "Start block and hops must be whole numbers."
    if start_block < 0 or not 1 <= max_hops <= MAX_TRACE_HOPS:
        return None, "Start block must be 0 or more and hops between 1 and {}.".format(
            MAX_TRACE_HOPS
        )
    token = (form.get("trace_token") or "").strip().lower()
    if token and not config.is_address(token):
        return None, "The token must be a 0x contract address, or empty for the native currency."
    arrival_tx = (form.get("trace_arrival_tx") or "").strip().lower()
    if arrival_tx and not config.is_tx_hash(arrival_tx):
        return None, "The arrival transaction must be a 0x hash of 64 hex digits, or empty."
    return {
        "arrival_tx": arrival_tx or None,
        "amount": amount,
        "start_block": start_block,
        "max_hops": max_hops,
        "token": token or None,
    }, None


def _run_trace(client, address, net, params):
    """Follow withdrawn funds forward from one address. Returns (view, table, reports)."""
    import json

    labels = _labels(net)
    destinations = Destinations(getattr(client, "api_key", ""))
    result = trace_funds(
        client,
        address,
        params["amount"],
        start_block=params["start_block"],
        token=params["token"],
        currency=net.currency,
        max_hops=params["max_hops"],
        labels=labels,
        is_contract=make_contract_check(net.rpc_url),
        chain_id=net.chain_id,
        destinations=destinations,
        arrival_tx=params.get("arrival_tx"),
    )

    def on(chain_id):
        return destinations.network(chain_id) or net if chain_id != net.chain_id else net

    def chain_note(chain_id):
        return on(chain_id).name if chain_id not in (None, net.chain_id) else ""

    def chain_name(chain_id):
        there = destinations.network(chain_id)
        return there.name if there else f"chain {chain_id}"

    edges = []
    for e in result["edges"]:
        here = on(e.get("chain_id"))
        view_edge = {
            **e,
            "from_url": here.addr_url(e["from"]),
            "to_url": here.addr_url(e["to"]),
            "tx_url": here.tx_url(e["tx_hash"]),
            "chain": chain_note(e.get("chain_id")),
            "share": share_text(e),
            "share_status": share_status(e),
        }
        if e["kind"] == "bridge":
            info = e["bridge"]
            dest = info.get("dest_chain_id")
            there = destinations.network(dest) if dest is not None else None
            view_edge["bridge_to"] = (
                there.name if there else ("" if dest is None else f"chain {dest}")
            )
            view_edge["bridge_fill_url"] = (
                there.tx_url(info["fill_tx"]) if there and info.get("fill_tx") else ""
            )
            view_edge["bridge_fill_label"] = bridge_tx_label(info)
            view_edge["bridge_recipients"] = recipient_links(info, destinations)
            view_edge["bridge_text"] = bridge_text(
                info, chain_name, fill=not view_edge["bridge_fill_url"], recipients=False
            )
        edges.append(view_edge)
    terminals = [
        {
            **t,
            "url": on(t.get("chain_id")).addr_url(t["address"]),
            "label": format_label(t.get("label")),
            "chain": chain_note(t.get("chain_id")),
            "bridge_text": terminal_bridge_text(t, destinations, recipients=False),
            "bridge_recipients": [
                link
                for info in (t.get("bridges") or ([t["bridge"]] if t.get("bridge") else []))
                for link in recipient_links(info, destinations)
            ],
        }
        for t in result["terminals"]
    ]
    view = {
        "start": result["start"],
        "amount": result["amount"],
        "asset": result["asset"],
        "edges": edges,
        "terminals": terminals,
        "nodes_expanded": result["nodes_expanded"],
        "arrival_note": arrival_text(result),
        "end_states_note": END_STATES_NOTE,
        "incomplete": _incomplete_view(result),
        "report_body": build_trace_report(result, net, destinations, fragment=True),
        "json_key": result["start"],
    }
    header = [
        "hop",
        "from",
        "to",
        "asset",
        "attributed",
        "value",
        "kind",
        "tx_hash",
        "chain",
        "lower",
        "upper",
        "bounds_available",
        "history_incomplete",
        "status",
        "bridge",
    ]
    rows = [
        [
            e["hop"],
            e["from"],
            e["to"],
            e["asset"],
            e["attributed"],
            e["value"],
            e["kind"],
            e["tx_hash"],
            on(e.get("chain_id")).name,
            e.get("lower", ""),
            e.get("upper", ""),
            e.get("bounds_available", ""),
            e.get("history_incomplete", ""),
            share_status(e),
            bridge_text(e["bridge"], chain_name) if e["kind"] == "bridge" else "",
        ]
        for e in result["edges"]
    ]
    reports = {
        "trace": build_trace_report(result, net, destinations),
        address + ".json": json.dumps(result, indent=1, sort_keys=True),
    }
    return view, (header, rows), reports


def _run_classify(client, address, net):
    """Deposit-address check of one address. Returns (view, table, reports)."""
    labels = _labels(net)
    info = run_classify(client, address, net, labels, make_contract_check(net.rpc_url))
    view = {
        "address": info["address"],
        "url": net.addr_url(info["address"]),
        "kind_label": kind_label(info),
        "text": check_text(info),
        "evidence": bool(info.get("evidence")),
        "targets": [
            {
                "address": t,
                "url": net.addr_url(t),
                "label": format_label(labels.get(t)) if labels else "",
            }
            for t in (
                [info["operator"]] if info.get("operator") else info.get("sweep_targets") or []
            )
        ],
        "senders": [{"address": s, "url": net.addr_url(s)} for s in info.get("senders") or []],
        "gas_funder": info.get("gas_funder") or "",
        "incomplete": _incomplete_view(info),
        "report_body": build_classify_report(info, net, fragment=True, labels=labels),
        "json_key": info["address"],
    }
    header = ["address", "network", "kind", "result", "senders"]
    rows = [
        [
            info["address"],
            net.name,
            info.get("kind", ""),
            check_text(info),
            len(info.get("senders") or []),
        ]
    ]
    reports = {
        "classify": build_classify_report(info, net, labels=labels),
        info["address"] + ".json": json.dumps(info, indent=2, sort_keys=True),
    }
    return view, (header, rows), reports


def _run_characterize(client, address, net, deposit_check=True):
    """Characterise one exit-candidate address. Returns (view, table, reports)."""
    labels = _labels(net)
    info = characterize_address(
        client,
        address,
        net,
        labels=labels,
        deposit_check=deposit_check,
        is_contract=make_contract_check(net.rpc_url),
    )
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
            "check": check_text(h["deposit_check"]) if h.get("deposit_check") else "",
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
        "disposable": info.get("disposable", False),
        "deposit_check": check_text(info["deposit_check"]) if info.get("deposit_check") else "",
        "incomplete": _incomplete_view(info),
        "report_body": build_characterize_report(info, net, fragment=True),
        "json_key": info["address"],
    }
    header = ["when_utc", "value", "asset", "pool"]
    rows = [[r["when"], r["value"], r["asset"], r["pool"]] for r in inflows]
    reports = {
        "characterize": build_characterize_report(info, net),
        info["address"] + ".json": json.dumps(info, indent=2, sort_keys=True, default=str),
    }
    return view, (header, rows), reports


@app.route("/", methods=["GET", "POST"])
def index():
    ctx = _base_context()
    if not session.get("csrf"):
        session["csrf"] = secrets.token_hex(16)
    ctx["csrf"] = session["csrf"]
    ctx["labels_msg"] = session.pop("labels_msg", None)
    if request.method != "POST":
        job = _job_get(request.args.get("job"))
        if job:
            ctx.update(job["echo"])
            if job["state"] == "running":
                ctx["job"] = job["id"]
            elif job["state"] == "error":
                ctx["error"] = job["error"]
            else:
                if job["token"] not in _STORE:
                    job["token"] = _store_put(job["store"])
                session["token"] = job["token"]
                ctx["result"] = job["payload"]
        return render_template("index.html", **ctx)
    if not _csrf_ok():
        ctx["error"] = "The form expired; reload the page and submit again."
        return render_template("index.html", **ctx), 400

    raw = request.form.get("wallets") or ""
    mode = "events"
    analysis = request.form.get("analysis") or "demix"
    network_name = request.form.get("network") or "ethereum"
    ctx.update(wallets_raw=raw, mode=mode, analysis=analysis, network=network_name)

    # Parse the numeric fields before the try block so bad input becomes a message,
    # not a 500, and echo them back so one error does not blank the other field.
    ctx["exit_window"] = request.form.get("exit_window") or ""
    ctx["placebo"] = bool(request.form.get("placebo"))
    ctx["deposit_addresses"] = bool(request.form.get("deposit_addresses"))
    ctx["deposit_check"] = bool(request.form.get("deposit_check"))
    ctx["root_check"] = bool(request.form.get("root_check"))
    ctx["group"] = bool(request.form.get("group"))
    # the exact-k detector runs on the merged notes, so only with the group view
    ctx["exact_k"] = ctx["group"] and bool(request.form.get("exact_k"))
    ctx["labels_blockscout"] = bool(request.form.get("labels_blockscout"))
    for field in (
        "trace_amount",
        "trace_start_block",
        "trace_token",
        "trace_max_hops",
        "trace_arrival_tx",
        "known_exits",
    ):
        ctx[field] = request.form.get(field) or ""
    # A field is validated only in the modes that show it: an error in a field the
    # chosen mode hides would be invisible (FIELD_MODES; the template shows any error
    # of a hidden field in the box at the top).
    window_days, error = _window_days(
        request.form.get("window_days") if analysis in FIELD_MODES["window_days"] else None
    )
    ctx["window_days"] = window_days
    if error:
        ctx["error"] = error
        ctx["error_field"] = "window_days"
        return render_template("index.html", **ctx)

    exit_window, error = _exit_window(
        request.form.get("exit_window") if analysis in FIELD_MODES["exit_window"] else None
    )
    if error:
        ctx["error"] = error
        ctx["error_field"] = "exit_window"
        return render_template("index.html", **ctx)

    options, error = _network_options(request.form)
    ctx.update(
        max_wait=request.form.get("max_wait") or "",
        pause=request.form.get("pause") or "",
        http_cache=request.form.get("http_cache") or "",
    )
    if error:
        ctx["error"] = error
        ctx["error_field"] = "provider"
        return render_template("index.html", **ctx)

    if analysis not in ANALYSES:
        ctx["error"] = "Unknown analysis '{}'. Choose one of: {}.".format(
            analysis, ", ".join(sorted(ANALYSES))
        )
        ctx["analysis"] = "demix"
        return render_template("index.html", **ctx)

    wallets, bad = _parse_wallets(raw)
    problem = _wallet_problem(bad)
    if problem:
        ctx["error"] = problem
        ctx["error_field"] = "wallets"
        return render_template("index.html", **ctx)
    if not wallets:
        ctx["error"] = "Enter at least one 0x-prefixed address (one per line)."
        ctx["error_field"] = "wallets"
        return render_template("index.html", **ctx)
    if len(wallets) > MAX_WALLETS:
        ctx["error"] = f"Too many addresses ({len(wallets)}). Limit is {MAX_WALLETS}."
        ctx["error_field"] = "wallets"
        return render_template("index.html", **ctx)
    if analysis == "multi" and len(wallets) < 2:
        ctx["error"] = "Cross-wallet correlation needs at least two addresses."
        ctx["error_field"] = "wallets"
        return render_template("index.html", **ctx)
    if analysis == "characterize" and len(wallets) != 1:
        ctx["error"] = "Characterise takes exactly one candidate address."
        ctx["error_field"] = "wallets"
        return render_template("index.html", **ctx)
    if analysis == "classify" and len(wallets) != 1:
        ctx["error"] = "The deposit-address check takes exactly one address."
        ctx["error_field"] = "wallets"
        return render_template("index.html", **ctx)
    trace_params = None
    known_exits, bad_known = _parse_wallets(
        (request.form.get("known_exits") or "") if analysis in FIELD_MODES["known_exits"] else ""
    )
    if bad_known:
        ctx["error"] = "Known exits must be 0x addresses: {}".format(", ".join(bad_known))
        ctx["error_field"] = "known_exits"
        return render_template("index.html", **ctx)
    if analysis == "trace":
        if len(wallets) != 1:
            ctx["error"] = "Trace takes exactly one exit address."
            ctx["error_field"] = "wallets"
            return render_template("index.html", **ctx)
        trace_params, error = _trace_params(request.form)
        if error:
            ctx["error"] = error
            ctx["error_field"] = "trace"
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

    def analyse(job=None):
        """Run the chosen analysis; return ``(payload, store_entry)``. Raises on failure."""
        client = EtherscanClient(
            api_key,
            pause=options["pause"],
            max_wait=options["max_wait"],
            cache_dir=options["cache_dir"],
            **net.client_kwargs(),
        )
        if job is not None:
            job["client"] = client  # the status endpoint reads its counters
        if analysis == "demix":
            blocks, table, reports = _run_demix(
                client,
                wallets,
                net,
                window_days,
                mode,
                exit_window,
                known_exits,
                placebo=bool(request.form.get("placebo")),
                deposit_addresses=ctx["deposit_addresses"],
                root_check=ctx["root_check"],
            )
            payload = {"kind": "demix", "blocks": blocks}
        elif analysis == "multi":
            data, table, reports = _run_multi(
                client,
                wallets,
                net,
                window_days,
                mode,
                exit_window,
                group=ctx["group"],
                exact_k=ctx["exact_k"],
            )
            payload = {"kind": "multi", **data}
        elif analysis == "characterize":
            view, table, reports = _run_characterize(
                client, wallets[0], net, deposit_check=ctx["deposit_check"]
            )
            payload = {"kind": "characterize", **view}
        elif analysis == "classify":
            view, table, reports = _run_classify(client, wallets[0], net)
            payload = {"kind": "classify", **view}
        elif analysis == "trace":
            view, table, reports = _run_trace(client, wallets[0], net, trace_params)
            payload = {"kind": "trace", **view}
        else:
            traces, table, reports, body = _run_cluster(client, wallets, net, window_days)
            payload = {
                "kind": "cluster",
                "traces": traces,
                "report_body": body,
                "json_key": "cluster",
            }

        header, rows = table
        # multi/cluster/characterize/trace/classify produce one report, stored under the
        # analysis name.
        report_key = None
        if analysis in ("multi", "cluster", "characterize", "trace", "classify") and reports:
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
            no_labels=analysis == "demix" and not load_attribution(net.name),
        )
        return payload, {"csv": {"header": header, "rows": rows}, "reports": reports}

    if request.form.get("async"):
        echo = {k: ctx[k] for k in _ECHO_FIELDS if k in ctx}
        job = _job_start(analyse, echo)
        if job is None:
            return _busy(ctx)
        # 303: a reload of the status page must not submit the analysis again
        return redirect("/?job=" + job["id"], code=303)
    if _running_job() is not None:
        return _busy(ctx)
    try:
        payload, store = analyse()
    except Exception as exc:  # surface in the UI rather than a 500
        ctx["error"] = f"Analysis failed: {exc}"
        return render_template("index.html", **ctx)
    session["token"] = _store_put(store)
    ctx["result"] = payload
    return render_template("index.html", **ctx)


@app.route("/job/<job_id>")
def job_status(job_id):
    """Progress of a background job (read-only; 404 for another session's or an unknown id)."""
    job = _job_get(job_id)
    if job is None:
        abort(404)
    response = jsonify(_job_status(job))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/labels/fetch", methods=["POST"])
def labels_fetch():
    """Download the public label set for one network (or all) on request.

    The file lands where ``load_attribution`` looks first, so the next analysis
    picks it up. The outcome is kept in the session and shown on the form page.
    """
    if not _csrf_ok():
        abort(403)
    network = request.form.get("network") or "all"
    try:
        results = fetch_attribution(None if network == "all" else [network])
    except TornadoDemixError as exc:
        session["labels_msg"] = {"ok": False, "lines": [str(exc)]}
        return redirect("/")
    lines = []
    for name, r in results.items():
        if r["status"] == "ok":
            lines.append("{}: {:,} labels, {:,} exchange.".format(name, r["rows"], r["exchange"]))
        else:
            lines.append("{}: {}".format(name, r["status"]))
    ok = all(r["status"] == "ok" for r in results.values())
    if ok:
        lines.append("Run the analysis again to use them.")
    session["labels_msg"] = {"ok": ok, "lines": lines}
    return redirect("/")


BLOCKSCOUT_MAX_ADDRESSES = 1000
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _result_addresses(header, rows):
    """Distinct lowercase addresses found in the cells of a result table, in order."""
    seen = {}
    for row in rows:
        for cell in row:
            for match in _ADDRESS_RE.findall(str(cell)):
                seen.setdefault(match.lower(), None)
    return list(seen)


@app.route("/labels/blockscout", methods=["POST"])
def labels_blockscout():
    """Fill the Blockscout tag cache for a network, on request.

    Same rules as ``/labels/fetch``: POST with the session's CSRF token, the outcome
    in the session, the form page shown again. The addresses are the pasted list or,
    when it is empty, the addresses of the last result table; at most
    ``BLOCKSCOUT_MAX_ADDRESSES``, validated, sent only to the Blockscout metadata
    service, and the cache lands under ``.cache/labels/blockscout``.
    """
    if not _csrf_ok():
        abort(403)
    networks = load_networks(DEFAULT_NETWORKS_CSV)
    net = networks.get(request.form.get("network") or "")
    if net is None:
        session["labels_msg"] = {"ok": False, "lines": ["Unknown network."]}
        return redirect("/")
    pasted = (request.form.get("addresses") or "").strip()
    if pasted:
        addresses, bad = _parse_wallets(pasted)
        if bad:
            session["labels_msg"] = {
                "ok": False,
                "lines": ["Not 0x addresses: {}".format(", ".join(bad[:5]))],
            }
            return redirect("/")
    else:
        store = _store_get()
        csv_part = (store or {}).get("csv") or {}
        addresses = _result_addresses(csv_part.get("header", []), csv_part.get("rows", []))
    if not addresses:
        session["labels_msg"] = {"ok": False, "lines": ["No addresses to look up."]}
        return redirect("/")
    if len(addresses) > BLOCKSCOUT_MAX_ADDRESSES:
        session["labels_msg"] = {
            "ok": False,
            "lines": [
                "{} addresses; at most {} per request.".format(
                    len(addresses), BLOCKSCOUT_MAX_ADDRESSES
                )
            ],
        }
        return redirect("/")
    directory = os.path.join(".cache", "labels", "blockscout")
    try:
        cache = fetch_and_cache(net.chain_id, addresses, directory=directory)
    except TornadoDemixError as exc:
        session["labels_msg"] = {"ok": False, "lines": [str(exc)]}
        return redirect("/")
    found = sum(1 for a in addresses if a in cache)
    session["labels_msg"] = {
        "ok": True,
        "lines": [
            '{}: {} of {} addresses tagged by Blockscout. Tick "Blockscout labels (cached)" '
            "and run the analysis again to use them.".format(net.name, found, len(addresses))
        ],
    }
    return redirect("/")


@app.route("/download.csv")
def download_csv():
    """Stream the last result table as CSV."""
    store = _store_get()
    data = store and store.get("csv")
    if not data:
        return Response("No results to download yet.\n", mimetype="text/plain", status=404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_sanitize_row(data["header"]))
    writer.writerows(_sanitize_row(r) for r in data["rows"])
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
