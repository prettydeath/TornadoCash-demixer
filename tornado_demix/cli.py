"""Command-line interface for the tornado_demix package.

Subcommands:
  demix         - analyse a single wallet
  multi         - analyse several wallets and correlate them
  cluster       - trace split-exit reconvergence for one or more wallets
  characterize  - describe one exit-candidate address
  trace         - follow withdrawn funds forward over several hops

The API key is read from api.csv (see tornado_demix.config), never from argv.
"""

from __future__ import annotations

import argparse
import json
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
from .placebo import BANDS, run_placebo
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
    write_trace_report,
)
from .rpc import make_contract_check
from .trace import MAX_HOPS, trace_funds


def _positive_hours(value):
    """argparse type: a strictly positive number of hours (for --exit-window)."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number of hours") from None
    if hours <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0 hours")
    return hours


# --rapid: MixLaunder (27 public laundering cases) found 95.8 % of the intervals
# between adjacent deposits and withdrawals within 7 days.
RAPID_EXIT_WINDOW_HOURS = 168.0


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
        "--max-voucher-span",
        type=_positive_hours,
        default=None,
        metavar="HOURS",
        help="start a new voucher when a deposit comes more than HOURS after the "
        "voucher's first one (default: no limit)",
    )
    parser.add_argument(
        "--rapid",
        action="store_true",
        help="shortcut for --exit-window 168: in public laundering cases "
        "95.8%% of deposit-to-withdrawal intervals were within 7 days",
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


def _print_placebo(pb: dict) -> None:
    print(f"\n[placebo] decoy window {pb['offset_days']:g} days before the real one")
    print(
        f"  withdrawals searched: {pb['target_withdrawals']} real, {pb['decoy_withdrawals']} decoy"
    )
    for band in BANDS:
        print(
            f"  {band:9} real {pb['target'][band]} | decoy {pb['decoy'][band]}"
            f"  - {pb['verdicts'][band]}"
        )
    for caveat in pb["caveats"]:
        print(f"  [!] {caveat}")
    print(f"  {pb['note']}")


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
        max_voucher_span_hours=getattr(args, "max_voucher_span", None),
        known_exits=[_valid_address(a) for a in getattr(args, "known_exit", None) or []],
        labels=labels,
        deposit_addresses=not getattr(args, "no_deposit_addresses", False),
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

    fresh = [a for a, f in sorted(data.get("fresh_addresses", {}).items()) if f["fresh"]]
    if fresh:
        print(f"\n[fresh] {len(fresh)} top candidate(s) had at most a day of history before")
        print("  their first withdrawal (disposable exit; context, not scored):")
        for addr in fresh:
            print(f"     {addr}")

    deposits = data.get("deposit_addresses") or []
    if deposits:
        print(f"\n[deposit] {len(deposits)} exchange deposit address(es) the depositor sent to:")
        for d in deposits:
            print(
                f"     {d['address']}  swept to {d.get('exchange') or 'a hot wallet'}; "
                f"{len(d['senders'])} sender(s)"
            )
        shared = {}
        for pool_key, addr, addrs in data.get("heuristics", {}).get("shared_deposits", []):
            shared.setdefault(addr, (pool_key, addrs))
        if shared:
            print("  candidates that sent to one of them too (linked address):")
            for addr, (pool_key, addrs) in sorted(shared.items()):
                print(f"     [{pool_key}] {addr} via {', '.join(addrs)}")

    anchored = [g for g in data.get("exit_groups", []) if g["anchored"]]
    if anchored:
        print("\n[groups] exits withdrawn in joint bursts with an anchor (context, not scored):")
        for g in anchored:
            why = ", ".join(f"{a['address']} ({a['why']})" for a in g["anchors"])
            print(
                f"  [{g['pool_key']}] {len(g['members'])} addresses, {g['notes']} notes; anchor {why}"
            )
            for addr in g["members"]:
                print(f"     {addr}")

    # A pool whose block lookup failed was never searched; say so per pool.
    for unresolved in data.get("unresolved", []):
        print(
            f"  [!] {unresolved['pool_key']} pool NOT searched "
            f"(block lookup failed): {unresolved['reason']}"
        )

    if getattr(args, "placebo", False) and data["deposits"]:
        data["placebo"] = run_placebo(
            client,
            wallet,
            data,
            args.window_days,
            network=net,
            labels=labels,
            exit_window_hours=args.exit_window,
            fee_lo=args.fee_lo,
            fee_hi=args.fee_hi,
            gap_hours=args.gap_hours,
            mode=args.mode,
            max_voucher_span_hours=getattr(args, "max_voucher_span", None),
        )
        _print_placebo(data["placebo"])

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
        max_voucher_span_hours=getattr(args, "max_voucher_span", None),
        labels=load_attribution(net.name, getattr(args, "attribution_dir", None)),
        deposit_addresses=not getattr(args, "no_deposit_addresses", False),
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

    funders = corr.get("shared_funders", {})
    if funders:
        print("\nShared immediate funders (not labelled, not busy):")
        for funder, ws in funders.items():
            print(f"  {funder} -> {', '.join(ws)}")

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
    if info.get("disposable"):
        print(
            "Disposable: first activity at most a day before the first pool inflow "
            "(typical of a laundering exit; not proof)"
        )
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


def cmd_trace(args: argparse.Namespace) -> None:
    address = _valid_address(args.address)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = EtherscanClient(key, **net.client_kwargs())
    labels = load_attribution(net.name, getattr(args, "attribution_dir", None))
    token = _valid_address(args.token) if args.token else None
    result = trace_funds(
        client,
        address,
        args.amount,
        start_block=args.start_block,
        token=token,
        currency=net.currency,
        max_hops=args.max_hops,
        labels=labels,
        is_contract=make_contract_check(net.rpc_url),
    )

    print("\n=== MULTI-HOP TRACE (FIFO) ===")
    print(f"Start: {result['start']}  {args.amount} {result['asset']}")
    for e in result["edges"]:
        swap = f" -> swapped to {e['swapped_to']}" if e["kind"] == "swap" else ""
        print(
            f"{'  ' * e['hop']}hop {e['hop']}: {e['from']} -> {e['to']}  "
            f"{e['attributed']} of {e['value']} {e['asset']}{swap}  {e['tx_hash']}"
        )
    print("\nWhere the traced funds stop:")
    for t in result["terminals"]:
        tag = format_label(t["label"]) if t["label"] else ""
        print(
            f"  {t['address']}  {t['amount']} {t['asset']}  hop {t['hop']}  [{t['reason']}]"
            + (f"  {tag}" if tag else "")
        )
    print(
        "\nFIFO attribution is a convention: funds in one account are fungible, "
        "so each edge is a lead to corroborate."
    )
    if getattr(args, "json", ""):
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1, sort_keys=True)
        print(f"[*] JSON trace: {args.json}", file=sys.stderr)
    if getattr(args, "report", ""):
        write_trace_report(result, args.report, net)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tornado-demix",
        description=(
            "Tornado Cash forensics on 8 EVM chains: evidence-banded exit leads, exit groups, operator clusters, tracing."
        ),
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
        "--known-exit",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="an exit already known from the investigation (repeatable); its exit "
        "group, the recipients withdrawn with it in joint bursts, is listed",
    )
    p_demix.add_argument(
        "--placebo",
        action="store_true",
        help="also run a decoy window that ends before the first deposit and compare the "
        "leads found there with the real ones (doubles explorer calls)",
    )
    p_demix.add_argument(
        "--no-deposit-addresses",
        action="store_true",
        help="skip the exchange-deposit-address lookup; saves up to ~50 explorer calls "
        "(more with many unlabelled sweep targets)",
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
        "--no-deposit-addresses",
        action="store_true",
        help="skip the exchange-deposit-address lookup of each wallet; saves explorer calls",
    )
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

    p_trace = sub.add_parser(
        "trace", help="follow withdrawn funds forward over several hops (FIFO, swaps)"
    )
    p_trace.add_argument("address", help="exit address the funds were withdrawn to (0x...)")
    p_trace.add_argument("--amount", type=float, required=True, help="amount to follow")
    p_trace.add_argument(
        "--token", default="", help="ERC-20 contract of the amount (default: native currency)"
    )
    p_trace.add_argument(
        "--start-block", type=int, default=0, help="block the funds arrived in (default 0)"
    )
    p_trace.add_argument(
        "--max-hops", type=int, default=MAX_HOPS, help=f"hops to follow (default {MAX_HOPS})"
    )
    p_trace.add_argument("--json", default="", help="write the trace as JSON to this path")
    p_trace.add_argument("--report", default="", help="write an HTML trace report to this path")
    p_trace.add_argument(
        "--api-csv", default=None, help="CSV with an api_key for the explorer (see demix)"
    )
    p_trace.add_argument(
        "--network",
        default="ethereum",
        help="network name from config/networks.csv (default ethereum)",
    )
    p_trace.add_argument("--networks-csv", default=None, help="CSV defining pools per network")
    p_trace.add_argument(
        "--attribution-dir",
        default=None,
        help="directory holding <network>.csv attribution tables; a labelled "
        "address ends the trace",
    )
    p_trace.set_defaults(func=cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Run a subcommand, turning package errors into a clean non-zero exit.

    The only place a ``TornadoDemixError`` becomes a ``SystemExit``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "rapid", False) and getattr(args, "exit_window", None) is None:
        args.exit_window = RAPID_EXIT_WINDOW_HOURS
    try:
        args.func(args)
    except TornadoDemixError as exc:
        raise SystemExit("{}: {}".format(type(exc).__name__, exc)) from None


if __name__ == "__main__":
    main()
