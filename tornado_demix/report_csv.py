"""CSV writers and the formatting helpers the reports share."""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone

from .heuristics import METHOD_FAMILY, ranked_candidates
from .incomplete import is_notice
from .networks import Network


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
    _warn_overwrite(path)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_sanitize_row(header))
        writer.writerows(_sanitize_row(r) for r in rows)
    return path


def _warn_overwrite(path):
    """A saved report can be case material; say so when a run replaces one."""
    if os.path.exists(path):
        print("[!] overwriting {}".format(path), file=sys.stderr)


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


# Columns every candidate listing carries: the band and score of the candidate, the
# evidence families that hold (context is no family), every signal that fired, and
# the full hashes of the withdrawals behind it.
CANDIDATE_COLUMNS = ["band", "score", "families", "signals", "evidence_tx_hashes"]


def evidence_families(signals) -> list[str]:
    """Evidence families of a signal set; context-only and note signals are no family."""
    return sorted({METHOD_FAMILY.get(s, s) for s in signals} - {"context"})


def ranked_index(data: dict) -> dict:
    """``{(pool_key, address): ranked row}`` of a demix result (empty for a result that
    carries no per-recipient detail)."""
    if not all("detail" in res for res in data.get("denoms", {}).values()):
        return {}
    return {(r["pool_key"], r["address"]): r for r in ranked_candidates(data)}


def withdrawal_hashes(data: dict, pool_key: str, address: str) -> list[str]:
    """Full hashes of the withdrawals an address received in one pool, oldest first."""
    recs = data.get("denoms", {}).get(pool_key, {}).get("detail", {}).get(address, [])
    return [x["hash"] for x in sorted(recs, key=lambda x: x.get("ts", 0)) if x.get("hash")]


def candidate_cells(data: dict, address: str, pool_keys=None, ranked=None) -> list:
    """The :data:`CANDIDATE_COLUMNS` cells of ``address`` in ``data``.

    ``pool_keys`` limits the pools (default: every pool the address appears in). The
    band and score are those of the best ranked row; an address the ranking does not
    list has empty band and score and still carries its withdrawal hashes.
    """
    ranked = ranked_index(data) if ranked is None else ranked
    pools = (
        list(pool_keys)
        if pool_keys is not None
        else [k for k, res in data.get("denoms", {}).items() if address in res.get("detail", {})]
    )
    rows = [ranked[(k, address)] for k in pools if (k, address) in ranked]
    hashes = [h for k in pools for h in withdrawal_hashes(data, k, address)]
    if not rows:
        return ["", "", "", "", ";".join(hashes)]
    best = max(
        rows, key=lambda r: ({"strong": 3, "moderate": 2, "weak": 1}[r["band"]], r["confidence"])
    )
    signals = sorted({s for r in rows for s in r["signals"]})
    return [
        best["band"],
        f"{best['confidence']:.2f}",
        ";".join(evidence_families(signals)),
        ";".join(signals),
        ";".join(hashes),
    ]


def multi_candidate_cells(corr: dict, wallets, address: str, pool_key=None) -> list:
    """:func:`candidate_cells` over several wallets' results: the best band and score,
    the union of families and signals, every withdrawal hash."""
    band_rank = {"": 0, "weak": 1, "moderate": 2, "strong": 3}
    best = ["", "", set(), set(), []]
    for w in wallets:
        data = corr["results"].get(w) or {}
        cells = candidate_cells(data, address, [pool_key] if pool_key else None)
        if band_rank[cells[0]] > band_rank[best[0]] or (
            cells[0] and cells[0] == best[0] and cells[1] > best[1]
        ):
            best[0], best[1] = cells[0], cells[1]
        best[2] |= set(filter(None, cells[2].split(";")))
        best[3] |= set(filter(None, cells[3].split(";")))
        best[4] += [h for h in cells[4].split(";") if h and h not in best[4]]
    return [
        best[0],
        best[1],
        ";".join(sorted(best[2])),
        ";".join(sorted(best[3])),
        ";".join(best[4]),
    ]


def _ensure_dir(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _with_network(network, header, rows):
    """Prefix a CSV header and its rows with the network that produced them.

    An output file that cannot say which chain it came from cannot be used as
    evidence, and the per-pool filenames collide across chains without it.
    """
    return ["network"] + header, [[network.name] + row for row in rows]


def _lookup_errors_csv(results, out_dir: str) -> list[str]:
    """``lookup_errors.csv`` with every failed lookup and cut-off list of ``results``
    (run results, see tornado_demix.incomplete); no file when nothing failed."""
    header = ["wallet", "kind", "what", "address", "tx", "error"]
    rows = []
    for res in results:
        wallet = res.get("wallet", "")
        for e in res.get("errors") or []:
            rows.append(
                [
                    e.get("wallet") or wallet,
                    e.get("kind", "lookup_failed"),
                    e.get("what", ""),
                    e.get("address", ""),
                    e.get("tx", ""),
                    e.get("error", ""),
                ]
            )
        for t in res.get("truncated") or []:
            if is_notice(t):
                rows.append(
                    [
                        wallet,
                        "notice",
                        t.get("action", ""),
                        t.get("address", ""),
                        "",
                        "read from the RPC node: no gas price on these rows",
                    ]
                )
                continue
            limit = f"cut off at {t['limit']} rows" if t.get("limit") else "cut off"
            rows.append([wallet, "truncated", t.get("action", ""), t.get("address", ""), "", limit])
    if not rows:
        return []
    return [_write(os.path.join(out_dir, "lookup_errors.csv"), header, rows)]


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
    ranked = ranked_index(data)
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
                    + candidate_cells(data, addr, [pool_key], ranked)
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
        ]
        + CANDIDATE_COLUMNS,
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
            sample = ";".join(x["hash"] for x in records[:4])
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
                    sample,
                ]
                + candidate_cells(data, addr, [pool_key], ranked)
            )
        rows.sort(key=lambda r: (r[2] != "yes", -r[1], r[0]))
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
            ]
            + CANDIDATE_COLUMNS,
            rows,
        )
        files.append(
            _write(
                os.path.join(out_dir, "{}_withdrawals_{}.csv".format(network.name, safe)),
                header,
                out_rows,
            )
        )

    files += _lookup_errors_csv([data], out_dir)
    return files


def write_relayer_csv(
    relayer_stats: dict,
    self_relayed_leads: list[dict],
    out_dir: str,
    network: Network,
    data: dict | None = None,
) -> list[str]:
    """Write relayer statistics and self-relayed leads (event mode only).

    ``data`` is the demix result the leads come from; with it, each lead row carries
    the :data:`CANDIDATE_COLUMNS` of its recipient (without it they are empty).
    """
    _ensure_dir(out_dir)
    files = []
    ranked = ranked_index(data) if data is not None else {}

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
        ]
        + CANDIDATE_COLUMNS,
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
            + (
                candidate_cells(data, lead["to"], [lead.get("pool_key")], ranked)
                if data is not None
                else ["", "", "", "", lead["tx_hash"]]
            )
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
            rows.append(
                [pool_key, addr, len(wallets), detail]
                + multi_candidate_cells(corr, wallets, addr, pool_key)
            )
        header, out_rows = _with_network(
            network,
            ["pool", "address", "num_wallets", "wallets_and_hitcounts"] + CANDIDATE_COLUMNS,
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
            prof_rows.append(
                [wallet, printable, addr, "exact" if exact else "gte", recv, multi]
                + multi_candidate_cells(corr, [wallet], addr)
            )
    header, rows = _with_network(
        network,
        [
            "wallet",
            "fingerprint",
            "consolidator_address",
            "match_type",
            "received_vs_needed",
            "multi_denomination",
        ]
        + CANDIDATE_COLUMNS,
        prof_rows,
    )
    files.append(_write(os.path.join(out_dir, "profile_matches.csv"), header, rows))

    # cross_consolidators.csv
    grades = corr.get("consolidator_grades", {})
    cons_rows = []
    for addr, ws in corr["cross_profile"].items():
        cells = multi_candidate_cells(corr, ws, addr)
        if addr in grades:  # the consolidator's own grade (window artefacts stay weak)
            cells[0] = grades[addr]["band"]
            cells[2] = ";".join(
                sorted({METHOD_FAMILY.get(s, s) for s in grades[addr].get("independent", [])})
            )
        cons_rows.append([addr, len(ws), ";".join(ws)] + cells)
    header, rows = _with_network(
        network,
        ["consolidator_address", "num_wallets", "wallets"] + CANDIDATE_COLUMNS,
        cons_rows,
    )
    files.append(_write(os.path.join(out_dir, "cross_consolidators.csv"), header, rows))

    files += _lookup_errors_csv([corr], out_dir)
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
    files += _lookup_errors_csv(traces, out_dir)
    return files
