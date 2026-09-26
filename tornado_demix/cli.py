"""Command-line interface for the tornado_demix package.

Subcommands:
  demix         - analyse a single wallet
  multi         - analyse several wallets and correlate them
  cluster       - trace split-exit reconvergence for one or more wallets
  characterize  - describe one exit-candidate address

The API key is read from api.csv (see tornado_demix.config), never from argv.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import config
from .attribution import format_label, load_attribution
from .characterize import characterize_address
from .cluster import MIN_FORWARD_FRACTION, trace_wallet
from .demix import run_demix
from .errors import ConfigError, TornadoDemixError
from .etherscan import EtherscanClient
from .multi import correlate
from .networks import get_network
from .relayer import (
    analyze_relayers,
    collect_withdrawals,
    self_relayed_candidates,
    verify_broadcaster,
)
from .report import (
    format_fingerprint,
    format_span,
    format_totals,
    write_characterize_report,
    write_cluster_csv,
    write_cluster_report,
    write_demix_csv,
    write_demix_json,
    write_html_report,
    write_multi_csv,
    write_multi_report,
    write_relayer_csv,
)


def _positive_hours(value):
    """argparse type: a strictly positive number of hours (for --exit-window)."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number of hours") from None
    if hours <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0 hours")
    return hours


def _add_common(parser):
    parser.add_argument(
        "--api-csv",
        default=None,
        help="CSV file holding the Etherscan API key "
        "(columns: service,api_key). Default: api.csv "
        "found in $TORNADO_DEMIX_CONFIG, ./config/, or "
        "the repository's config/ directory",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="withdrawal search window after a deposit (default 30)",
    )
    parser.add_argument(
        "--exit-window",
        type=_positive_hours,
        default=None,
        metavar="HOURS",
        help="end the withdrawal search HOURS after each voucher's "
        "last deposit instead of --window-days. A tight window "
        "cuts the recipient field so a count match actually "
        "discriminates, surfacing fast exits; it misses "
        "withdrawals delayed longer than HOURS",
    )
    parser.add_argument(
        "--fee-lo",
        type=float,
        default=0.90,
        help="lower multiplier of denomination (default 0.90 = -10%%)",
    )
    parser.add_argument(
        "--fee-hi",
        type=float,
        default=0.995,
        help="upper multiplier of denomination (default 0.995)",
    )
    parser.add_argument(
        "--gap-hours", type=float, default=24, help="max gap to cluster deposits into one voucher"
    )
    parser.add_argument(
        "--network",
        default="ethereum",
        help="network name from config/networks.csv (default ethereum)",
    )
    parser.add_argument(
        "--networks-csv",
        default=None,
        help="CSV defining pools per network. Default: "
        "networks.csv found on the config search path",
    )
    parser.add_argument(
        "--mode",
        choices=["events", "transfers"],
        default="events",
        help="'events' reads Withdrawal logs for the exact "
        "recipient/relayer/fee (default); 'transfers' uses "
        "the older internal-transfer fee-window heuristic",
    )


def _version():
    """Read __version__ lazily so importing cli does not re-enter the package."""
    from . import __version__

    return __version__


def _network(args):
    """Resolve the Network object selected by --network / --networks-csv."""
    return get_network(args.network, args.networks_csv)


def _valid_address(value):
    """Validate one wallet address, returning it lowercased.

    A typo sent to the provider comes back empty and would read as "no
    Tornado deposits found".
    """
    addr = (value or "").strip().lower()
    if not config.is_address(addr):
        raise ConfigError(
            "{!r} is not a wallet address: expected 0x followed by 40 hex characters.".format(value)
        )
    return addr


def _wallets_from_args(args):
    """Resolve the wallet list from positional args or a CSV file."""
    if getattr(args, "wallets_csv", None):
        return config.load_wallets(args.wallets_csv)
    if getattr(args, "wallets", None):
        return [_valid_address(w) for w in args.wallets]
    raise ConfigError("Provide wallet address(es) or --wallets-csv")


def cmd_demix(args: argparse.Namespace) -> None:
    wallet = _valid_address(args.wallet)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = EtherscanClient(key, **net.client_kwargs())
    labels = load_attribution(net.name, getattr(args, "attribution_dir", None))
    data = run_demix(
        client,
        wallet,
        args.window_days,
        args.fee_lo,
        args.fee_hi,
        args.gap_hours,
        mode=args.mode,
        network=net,
        exit_window_hours=args.exit_window,
    )

    print("\n=== DEMIX SUMMARY ===")
    print(f"Wallet: {data['wallet']}")
    for voucher in data["vouchers"]:
        started = datetime.fromtimestamp(voucher["first_ts"], tz=timezone.utc)
        print(
            f"Voucher: {voucher['count']} x {voucher['pool_key']} starting "
            f"{started:%Y-%m-%d %H:%M} UTC"
        )
    for pool_key, res in data["denoms"].items():
        print(f"\n[{pool_key}] pool {res['pool']}")
        print(
            f"  qualifying withdrawals: {res['total_qualifying_withdrawals']} | "
            f"unique recipients: {res['unique_recipients']}"
        )
        for n, addrs in res["candidates_by_count"].items():
            print(f"  recipients hit exactly {n}x -> {len(addrs)} candidate(s)")
            for addr in addrs:
                tag = format_label(labels.get(addr)) if labels else ""
                print(f"     {addr}" + (f"  [{tag}]" if tag else ""))

    # A pool whose block lookup failed was never searched; say so per pool.
    for unresolved in data.get("unresolved", []):
        print(
            f"  [!] {unresolved['pool_key']} pool NOT searched "
            f"(block lookup failed): {unresolved['reason']}"
        )

    # Relayer intelligence is only available in event mode.
    relayer_stats, leads = None, []
    if args.mode == "events":
        withdrawals = collect_withdrawals(data)
        relayer_stats = analyze_relayers(withdrawals)
        leads = self_relayed_candidates(data)
        verify_broadcaster(client, leads)
        print(
            f"\n[relayers] {relayer_stats['unique_relayers']} relayer(s) | "
            f"{len(relayer_stats['self_relayed'])} self-relayed withdrawal(s)"
        )
        if leads:
            print(f"  SELF-RELAYED CANDIDATES: {len(leads)}")
            for lead in leads[:10]:
                status = lead.get("broadcaster_status", "unverified")
                note = {
                    "self": "broadcast by the recipient",
                    "other": "broadcast by {}".format(lead.get("broadcaster")),
                    "unverified": "broadcaster NOT verified",
                }[status]
                print(f"     {lead['to']}  {lead['pool_key']}  {lead['tx_hash']}  [{note}]")

    if args.out_dir:
        files = write_demix_csv(data, args.out_dir, net)
        if relayer_stats is not None:
            files += write_relayer_csv(relayer_stats, leads, args.out_dir, net)
        print(f"\n[*] CSV report ({len(files)} files) in: {args.out_dir}", file=sys.stderr)

    if args.report:
        write_html_report(data, args.report, net, attribution=labels)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)

    if getattr(args, "json", ""):
        write_demix_json(data, args.json, attribution=labels)
        print(f"[*] JSON result: {args.json}", file=sys.stderr)


def cmd_multi(args: argparse.Namespace) -> None:
    wallets = _wallets_from_args(args)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = EtherscanClient(key, **net.client_kwargs())
    corr = correlate(
        client,
        wallets,
        args.window_days,
        args.fee_lo,
        args.fee_hi,
        args.gap_hours,
        mode=args.mode,
        network=net,
        exit_window_hours=args.exit_window,
    )

    print("\n=== CROSS-WALLET CORRELATION ===")
    print(f"Wallets analysed: {len(wallets)}")
    print(f"Strong links (count-matched candidate shared by 2+): {len(corr['strong'])}")
    for pool_key, addr, ws in corr["strong"]:
        print(f"  [{pool_key}] {addr} <- {', '.join(ws)}")
    print("\nProfile matches (full fingerprint on one address):")
    for wallet, matches in corr["profile_matches"].items():
        fingerprint = corr["fingerprints"][wallet]
        printable = format_fingerprint(fingerprint)
        exact = [m for m in matches if m[2]]
        print(f"  {wallet} ({printable}): {len(exact)} exact, {len(matches)} total match(es)")
        for addr, _detail, is_exact in exact[:5]:
            print(f"     {'EXACT' if is_exact else '>='} {addr}")
    sync_groups = corr.get("sync_groups", [])
    if sync_groups:
        print("\nDeposit synchronicity (wallets that deposited as one timed batch):")
        for g in sync_groups:
            print(
                f"  {len(g['wallets'])} wallets within {format_span(g['span_seconds'])}: "
                f"{', '.join(g['wallets'])}"
            )

    if corr["cross_profile"]:
        grades = corr.get("consolidator_grades", {})
        print("\nCross-wallet consolidators (band = how much it discriminates):")
        ordered = sorted(
            corr["cross_profile"].items(),
            key=lambda kv: (
                {"strong": 0, "moderate": 1, "weak": 2}.get(
                    grades.get(kv[0], {}).get("band", "weak"), 2
                ),
                -len(kv[1]),
            ),
        )
        for addr, ws in ordered:
            band = grades.get(addr, {}).get("band", "weak")
            note = " (window-overlap artefact)" if grades.get(addr, {}).get("artefact") else ""
            print(f"  [{band}]{note} {addr} <- {', '.join(ws)}")

    if args.out_dir:
        files = write_multi_csv(corr, args.out_dir, net)
        print(f"\n[*] CSV report ({len(files)} files) in: {args.out_dir}", file=sys.stderr)

    if args.report:
        write_multi_report(corr, args.report, net)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)


def cmd_cluster(args: argparse.Namespace) -> None:
    wallets = _wallets_from_args(args)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = EtherscanClient(key, **net.client_kwargs())

    traces = []
    for wallet in wallets:
        trace = trace_wallet(
            client,
            wallet,
            args.cache_dir,
            args.window_days,
            args.fee_lo,
            args.fee_hi,
            args.gap_hours,
            args.layer_cap,
            network=net,
            min_forward_frac=args.min_forward_frac,
        )
        traces.append(trace)

    print("\n=== CLUSTER CONSOLIDATION POINTS ===")
    for trace in traces:
        printable = format_fingerprint(trace["fingerprint"])
        print(f"\n{trace['wallet']} ({printable})")
        if not trace["clusters"]:
            print("   no downstream address is fed by 2+ pool layers")
            continue
        for cluster in trace["clusters"][:8]:
            covered = ", ".join(str(k) for k in cluster["pools_covered"])
            totals = format_totals(cluster["totals"])
            print(f"   Z {cluster['z']} | layers={cluster['n_layers']} [{covered}] | {totals}")

    if args.out_dir:
        files = write_cluster_csv(traces, args.out_dir, net)
        print(f"\n[*] CSV report ({len(files)} files) in: {args.out_dir}", file=sys.stderr)

    if args.report:
        write_cluster_report(traces, args.report, net)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)


def cmd_characterize(args: argparse.Namespace) -> None:
    address = _valid_address(args.address)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = EtherscanClient(key, **net.client_kwargs())
    labels = load_attribution(net.name, getattr(args, "attribution_dir", None))
    info = characterize_address(client, address, net, labels=labels)

    print("\n=== CANDIDATE CHARACTERISATION ===")
    print(f"Address: {info['address']}")
    if info.get("label"):
        print(
            f"Attribution: {format_label(info['label'])}"
            + (f" - {info['label']['entity']}" if info["label"].get("entity") else "")
        )
    print(f"Classification: {info['classification'].upper()} - {info['classification_reason']}")
    print(f"Activity: {info['normal_txs']} normal tx, {info['internal_txs']} internal tx")
    if info["first_activity_ts"]:
        first = datetime.fromtimestamp(info["first_activity_ts"], tz=timezone.utc)
        last = datetime.fromtimestamp(info["last_activity_ts"], tz=timezone.utc)
        print(f"Active: {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC")

    inflows = info["pool_inflows"]
    totals = ", ".join(f"{v} {a}" for a, v in sorted(info["inflow_totals"].items()))
    print(
        f"\nTornado-pool inflows: {len(inflows)} across "
        f"{len(info['distinct_pools'])} pool(s)" + (f" (total {totals})" if totals else "")
    )
    for r in inflows[:40]:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc)
        print(f"  {ts:%Y-%m-%d %H:%M}  {r['value']:>10} {r['asset']}  from {r['pool_key']} pool")
    if len(inflows) > 40:
        print(f"  ... and {len(inflows) - 40} more")

    if info["top_next_hops"]:
        print("\nDominant next hops (transactions sent; no value = contract call):")
        for h in info["top_next_hops"]:
            tag = format_label(h.get("label"))
            amount = (
                "contract call" if h.get("kind") == "call" else f"{h['total_value']} {net.currency}"
            )
            print(
                f"  {h['count']:>3}x -> {h['address']}  ({amount})" + (f"  [{tag}]" if tag else "")
            )

    print(
        "\nAll of this is a lead, not proof. Corroborate deposit->inflow timing "
        "and the next hop before drawing a conclusion."
    )

    if getattr(args, "report", ""):
        write_characterize_report(info, args.report, net)
        print(f"\n[*] HTML report: {args.report}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tornado-demix",
        description=("Tornado.Cash demixing across 8 EVM chains via amount + timing correlation."),
    )
    parser.add_argument(
        "--version", action="version", version="tornado-demix {}".format(_version())
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demix = sub.add_parser("demix", help="analyse a single wallet")
    p_demix.add_argument("wallet", help="depositor wallet address (0x...)")
    p_demix.add_argument("--out-dir", default="", help="directory to write CSV report files")
    p_demix.add_argument(
        "--report", default="", help="write a downloadable HTML demix report to this path"
    )
    p_demix.add_argument(
        "--json",
        default="",
        help="write the full result (parameters, candidates, raw data) as JSON to this path",
    )
    p_demix.add_argument(
        "--attribution-dir",
        default=None,
        help="directory holding <network>.csv attribution tables "
        "(exchanges, mixers, sanctioned...). When set, a "
        "labelled candidate is named inline. Else read from "
        "$TORNADO_DEMIX_ATTRIBUTION or config/attribution/",
    )
    _add_common(p_demix)
    p_demix.set_defaults(func=cmd_demix)

    p_multi = sub.add_parser("multi", help="analyse & correlate several wallets")
    p_multi.add_argument("wallets", nargs="*", help="wallet addresses")
    p_multi.add_argument("--wallets-csv", default="", help="CSV file with an 'address' column")
    p_multi.add_argument("--out-dir", default="", help="directory to write CSV report files")
    p_multi.add_argument(
        "--report", default="", help="write a downloadable HTML correlation report to this path"
    )
    _add_common(p_multi)
    p_multi.set_defaults(func=cmd_multi)

    p_cluster = sub.add_parser("cluster", help="trace split-exit reconvergence")
    p_cluster.add_argument("wallets", nargs="*", help="wallet addresses")
    p_cluster.add_argument("--wallets-csv", default="", help="CSV file with an 'address' column")
    p_cluster.add_argument("--out-dir", default="", help="directory to write CSV report files")
    p_cluster.add_argument(
        "--report", default="", help="write a downloadable HTML cluster report to this path"
    )
    p_cluster.add_argument(
        "--cache-dir", default=".cache", help="directory for resumable forward/layer caches"
    )
    p_cluster.add_argument(
        "--layer-cap", type=int, default=35, help="skip a pool layer with more candidates than this"
    )
    p_cluster.add_argument(
        "--min-forward-frac",
        type=float,
        default=MIN_FORWARD_FRACTION,
        help="ignore forward hops below this fraction of the denomination (default 0.01 = 1%%)",
    )
    _add_common(p_cluster)
    p_cluster.set_defaults(func=cmd_cluster)

    p_char = sub.add_parser(
        "characterize", help="characterise an exit-candidate address (pool inflows, type, next hop)"
    )
    p_char.add_argument("address", help="candidate/recipient address (0x...)")
    p_char.add_argument(
        "--api-csv", default=None, help="CSV with an api_key for the explorer (see demix)"
    )
    p_char.add_argument(
        "--network",
        default="ethereum",
        help="network name from config/networks.csv (default ethereum)",
    )
    p_char.add_argument("--networks-csv", default=None, help="CSV defining pools per network")
    p_char.add_argument(
        "--attribution-dir",
        default=None,
        help="directory holding <network>.csv attribution tables "
        "(exchanges, mixers, sanctioned...). Scopes the "
        "lookup: the file must be here. Else read from "
        "$TORNADO_DEMIX_ATTRIBUTION or config/attribution/",
    )
    p_char.add_argument(
        "--report", default="", help="write a downloadable HTML characterisation report here"
    )
    p_char.set_defaults(func=cmd_characterize)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Run a subcommand, turning package errors into a clean non-zero exit.

    The only place a ``TornadoDemixError`` becomes a ``SystemExit``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except TornadoDemixError as exc:
        raise SystemExit("{}: {}".format(type(exc).__name__, exc)) from None


if __name__ == "__main__":
    main()
