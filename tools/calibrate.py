#!/usr/bin/env python3
"""Precision/recall calibration for the confidence-scoring heuristics.

The signal weights in ``tornado_demix/heuristics.py`` (``SIGNAL_WEIGHTS``,
``MIN_COUNT_DISCRIMINATION``) are judgement calls, not a fitted model. This
tool is how a future change to those numbers gets justified by evidence: it
runs the real pipeline against a set of *confirmed* depositor -> exit pairs
and reports how well :func:`ranked_candidates` recovers the known answer, at
a range of confidence thresholds.

For each case it runs :func:`run_demix` on the depositor, on the case's own
network, and checks whether the confirmed exit address appears among
:func:`ranked_candidates` at or above each threshold in :data:`THRESHOLDS`.

* Precision at a threshold = confirmed-exit rows / all candidate rows
  produced at or above that threshold, summed across every case. It answers
  "of the leads the tool hands over at this confidence, how many are right".
* Recall at a threshold = cases whose confirmed exit was found at or
  above that threshold / total cases. It answers "of the answers we know,
  how many does the tool surface".

A case whose run errors (bad network, an address the depositor never used,
an API failure) counts as zero candidates and a recall miss - it is not
dropped from the denominator, because a live API hiccup should not be able
to inflate the reported precision by shrinking the sample.

This is a live tool: it needs a real API key and makes real API calls,
so it is run by hand, never imported by the test suite for its side effects.
Importing this module runs nothing; all network activity happens inside
:func:`main`.

Usage
-----
    cp calibration/cases.csv.example calibration/cases.csv   # fill in real pairs
    python tools/calibrate.py
    python tools/calibrate.py --cases-csv calibration/cases.csv --window-days 45
    python tools/calibrate.py --baseline calibration/baseline.json

Exit status: 0 normally; 1 if precision at :data:`DEFAULT_THRESHOLD` regressed
against the value recorded in ``calibration/baseline.json``. If no baseline
file exists yet, the current numbers are written as the new baseline and the
run still exits 0 - there is nothing to regress against on the first run.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix import config  # noqa: E402
from tornado_demix.demix import run_demix  # noqa: E402
from tornado_demix.etherscan import EtherscanClient  # noqa: E402
from tornado_demix.heuristics import ranked_candidates  # noqa: E402
from tornado_demix.networks import get_network  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
DEFAULT_CASES_CSV = os.path.join(_REPO_ROOT, "calibration", "cases.csv")
DEFAULT_BASELINE_JSON = os.path.join(_REPO_ROOT, "calibration", "baseline.json")

# Confidence thresholds the table is computed at. 0.0 is the shipped default -
# report.py and the web UI both call ranked_candidates() with no
# min_confidence override, so that is the threshold the regression gate
# checks against unless overridden.
THRESHOLDS = [0.0, 0.2, 0.4, 0.6, 0.8]
DEFAULT_THRESHOLD = 0.0


def load_cases(path):
    """Return confirmed depositor->exit pairs from a calibration cases CSV.

    ``path``: path to a CSV with columns network, depositor, confirmed_exit,
    source, notes (header required, extra columns ignored - the same
    convention as :func:`tornado_demix.config.load_wallets`). Blank rows and
    comment rows (network starting with '#') are skipped.

    Returns a list of dicts: network, depositor, confirmed_exit (both
    addresses lowercased), source, notes.

    Raises SystemExit if the file does not exist. There is deliberately no
    fallback to ``cases.csv.example``: its one row is a placeholder with a
    zero-address exit, and silently substituting it would produce a
    calibration report that looks real but proves nothing, exactly the
    hazard ``config.py`` already guards against for wallets.csv/api.csv.
    """
    if not path or not os.path.exists(path):
        raise SystemExit(
            "Calibration cases file not found: {}. Copy the placeholder and "
            "fill in real confirmed pairs:\n"
            "    cp calibration/cases.csv.example calibration/cases.csv".format(path)
        )
    cases = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            network = (row.get("network") or "").strip().lower()
            if not network or network.startswith("#"):
                continue
            depositor = (row.get("depositor") or "").strip().lower()
            confirmed_exit = (row.get("confirmed_exit") or "").strip().lower()
            if not depositor or not confirmed_exit:
                continue
            cases.append(
                {
                    "network": network,
                    "depositor": depositor,
                    "confirmed_exit": confirmed_exit,
                    "source": (row.get("source") or "").strip(),
                    "notes": (row.get("notes") or "").strip(),
                }
            )
    return cases


def run_case(case, api_key, window_days, gap_hours):
    """Run the real pipeline for one case and return its ranked candidates.

    Returns a list of rows from :func:`ranked_candidates` (unfiltered -
    ``min_confidence=0.0``, so every threshold can be applied afterward
    without re-running the analysis). Raises on failure; callers decide how
    a failed case counts toward the statistics.
    """
    net = get_network(case["network"])
    client = EtherscanClient(api_key, **net.client_kwargs())
    data = run_demix(
        client, case["depositor"], window_days=window_days, gap_hours=gap_hours, network=net
    )
    return ranked_candidates(data)


def score_case(case, candidates, thresholds):
    """Return per-threshold stats for one case's candidate rows.

    ``candidates`` is the unfiltered :func:`ranked_candidates` output (or an
    empty list for a case that errored). Returns
    ``{threshold: {"candidates": int, "true_positives": int, "hit": bool}}``.
    ``true_positives`` counts candidate rows whose address is the confirmed
    exit - normally 0 or 1, but a confirmed address that qualifies in more
    than one pool for the same wallet would count more than once, which is
    correct: each such row is a candidate a reader of the report would see.
    """
    out = {}
    for threshold in thresholds:
        rows = [c for c in candidates if c["confidence"] >= threshold]
        tp = sum(1 for c in rows if c["address"] == case["confirmed_exit"])
        out[threshold] = {
            "candidates": len(rows),
            "true_positives": tp,
            "hit": tp > 0,
        }
    return out


def aggregate(per_case, thresholds):
    """Combine per-case threshold stats into precision/recall per threshold.

    ``per_case`` is a list of the dicts :func:`score_case` returns, one per
    case (including cases that errored, scored as all-zero). Returns
    ``{threshold: {"precision": float|None, "recall": float|None,
    "candidates": int, "true_positives": int, "hits": int, "cases": int}}``.
    ``precision``/``recall`` are None when their denominator is zero (no
    candidates surfaced at all / no cases), which the caller must render as
    "n/a" rather than divide by zero.
    """
    n_cases = len(per_case)
    out = {}
    for threshold in thresholds:
        candidates = sum(c[threshold]["candidates"] for c in per_case)
        tp = sum(c[threshold]["true_positives"] for c in per_case)
        hits = sum(1 for c in per_case if c[threshold]["hit"])
        out[threshold] = {
            "candidates": candidates,
            "true_positives": tp,
            "hits": hits,
            "cases": n_cases,
            "precision": (tp / candidates) if candidates else None,
            "recall": (hits / n_cases) if n_cases else None,
        }
    return out


def _fmt_pct(value):
    return "n/a" if value is None else "{:.0%}".format(value)


def print_table(stats, thresholds):
    print(
        "\n{:>10}  {:>9}  {:>9}  {:>11}  {:>6}  {:>5}".format(
            "threshold", "precision", "recall", "candidates", "TP", "hits"
        )
    )
    for threshold in thresholds:
        s = stats[threshold]
        print(
            "{:>10.2f}  {:>9}  {:>9}  {:>11}  {:>6}  {:>4}/{}".format(
                threshold,
                _fmt_pct(s["precision"]),
                _fmt_pct(s["recall"]),
                s["candidates"],
                s["true_positives"],
                s["hits"],
                s["cases"],
            )
        )


def load_baseline(path):
    """Return the baseline dict from ``path``, or None if it does not exist."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_baseline(path, stats, thresholds, default_threshold):
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "default_threshold": default_threshold,
        "thresholds": {
            "{:.2f}".format(t): {
                "precision": stats[t]["precision"],
                "recall": stats[t]["recall"],
                "candidates": stats[t]["candidates"],
                "true_positives": stats[t]["true_positives"],
                "hits": stats[t]["hits"],
                "cases": stats[t]["cases"],
            }
            for t in thresholds
        },
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return payload


def check_regression(baseline, stats, default_threshold):
    """Compare current precision at ``default_threshold`` against baseline.

    Returns (regressed, message). ``regressed`` is always False when either
    number is unavailable (nothing to compare, or nothing was found this
    run) - there is no evidence of a regression, only an inconclusive run,
    and the caller should say so rather than fail the gate on it.
    """
    key = "{:.2f}".format(default_threshold)
    baseline_entry = (baseline.get("thresholds") or {}).get(key)
    if baseline_entry is None:
        return False, "Baseline has no entry for threshold {:.2f}; skipping check.".format(
            default_threshold
        )
    baseline_precision = baseline_entry.get("precision")
    current_precision = stats[default_threshold]["precision"]
    if baseline_precision is None or current_precision is None:
        return False, "Precision undefined this run or in baseline; skipping check."
    if current_precision < baseline_precision - 1e-9:
        return True, (
            "REGRESSION: precision at threshold {:.2f} dropped from {:.0%} "
            "(baseline) to {:.0%} (this run).".format(
                default_threshold, baseline_precision, current_precision
            )
        )
    return False, "Precision at threshold {:.2f} held: {:.0%} (baseline {:.0%}).".format(
        default_threshold, current_precision, baseline_precision
    )


def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="Report precision/recall of ranked_candidates() against "
        "confirmed depositor->exit pairs, across confidence "
        "thresholds."
    )
    ap.add_argument(
        "--cases-csv",
        default=DEFAULT_CASES_CSV,
        help="calibration cases CSV (default: calibration/cases.csv)",
    )
    ap.add_argument(
        "--api-csv", default=None, help="CSV holding the Etherscan API key (see config.py)"
    )
    ap.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE_JSON,
        help="baseline JSON to compare against / write (default: calibration/baseline.json)",
    )
    ap.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="withdrawal search window after a deposit (default 30)",
    )
    ap.add_argument(
        "--gap-hours",
        type=float,
        default=24,
        help="max gap to cluster deposits into one voucher (default 24)",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="confidence threshold the regression gate checks "
        "(default {:.2f}, must be one of {})".format(DEFAULT_THRESHOLD, THRESHOLDS),
    )
    ap.add_argument(
        "--update-baseline",
        action="store_true",
        help="overwrite the baseline with this run's numbers even if one already exists",
    )
    return ap


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.threshold not in THRESHOLDS:
        raise SystemExit("--threshold must be one of {}, got {}".format(THRESHOLDS, args.threshold))

    cases = load_cases(args.cases_csv)
    if not cases:
        print("No cases in {}. Nothing to calibrate.".format(args.cases_csv))
        return 0
    print("[*] {} case(s) loaded from {}".format(len(cases), args.cases_csv))

    api_key = config.load_api_key(args.api_csv)

    per_case = []
    for case in cases:
        label = "{} on {}".format(case["depositor"], case["network"])
        try:
            candidates = run_case(case, api_key, args.window_days, args.gap_hours)
        except SystemExit:
            raise
        except Exception as exc:  # one bad case must not abort the whole run
            print("[!] {}: {}: {}".format(label, type(exc).__name__, exc), file=sys.stderr)
            candidates = []
        per_case.append(score_case(case, candidates, THRESHOLDS))

    stats = aggregate(per_case, THRESHOLDS)
    print_table(stats, THRESHOLDS)

    baseline = load_baseline(args.baseline)
    if baseline is None or args.update_baseline:
        write_baseline(args.baseline, stats, THRESHOLDS, args.threshold)
        verb = "Updated" if (baseline is not None and args.update_baseline) else "Wrote new"
        print("\n[*] {} baseline: {}".format(verb, args.baseline))
        return 0

    regressed, message = check_regression(baseline, stats, args.threshold)
    print("\n[*] " + message)
    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
