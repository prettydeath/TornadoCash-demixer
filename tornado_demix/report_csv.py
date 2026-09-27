"""CSV writers and the formatting helpers the reports share."""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone

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
