"""Command-line interface for the tornado_demix package.

Subcommands:
  demix         - analyse a single wallet
  multi         - analyse several wallets and correlate them
  cluster       - trace split-exit reconvergence for one or more wallets
  characterize  - describe one exit-candidate address
  classify-address - deposit-address check of one address
  trace         - follow withdrawn funds forward over several hops
  labels        - download or inspect the address-label (attribution) set

The API key is read from api.csv (see tornado_demix.config), never from argv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import config
from .attribution import (
    attribution_status,
    fetch_attribution,
    format_label,
    load_attribution,
)
from .blockscout_labels import (
    fetch_and_cache,
    fetch_tags,
    to_labels,
    with_blockscout,
    write_cache,
)
from .blockscout_labels import (
    load_cache as load_blockscout_cache,
)
from .bridges import Destinations
from .characterize import characterize_address, run_classify
from .cluster import MIN_FORWARD_FRACTION, trace_wallet
from .demix import run_demix
from .deposit_addresses import check_text, kind_label, target_label
from .errors import ConfigError, TornadoDemixError
from .etherscan import (
    CACHE_SAFE_DEPTH,
    DEFAULT_MAX_WAIT,
    EtherscanClient,
)
from .heuristics import CONTEXT_SIGNALS, NOTE_SIGNALS, conclusion, ranked_candidates
from .honest import (
    EXACT_K_CAVEAT,
    EXACT_K_TITLE,
    GROUP_VIEW_TITLE,
    SHARED_CANDIDATES_CAVEAT,
    SHARED_CANDIDATES_TITLE,
    exploratory_banner,
    label_sets_for,
    limitations_text,
    provenance,
)
from .incomplete import lines as incomplete_lines
from .incomplete import summary as incomplete_summary
from .multi import correlate, exact_k_skips, exact_k_tail_text
from .networks import get_network
from .placebo import BANDS, run_placebo
from .relayer import (
    analyze_relayers,
    collect_withdrawals,
    fee_key,
    fee_label,
    frequency_text,
    verify_run,
)
from .report import (
    evidence_families,
    format_fingerprint,
    format_span,
    format_totals,
    terminal_bridge_text,
    withdrawal_hashes,
    write_characterize_report,
    write_classify_report,
    write_cluster_csv,
    write_cluster_report,
    write_demix_csv,
    write_demix_json_hashed,
    write_html_report,
    write_multi_csv,
    write_multi_json,
    write_multi_report,
    write_relayer_csv,
    write_trace_report,
)
from .rpc import make_contract_check
from .trace import (
    END_STATES_NOTE,
    MAX_HOPS,
    arrival_text,
    bridge_text,
    share_status,
    share_text,
    trace_funds,
)

# --progress-every: one stderr line per this many explorer calls.
DEFAULT_PROGRESS_EVERY = 50


def _positive_hours(value):
    """argparse type: a strictly positive number of hours (for --exit-window)."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number of hours") from None
    if hours <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0 hours")
    return hours


def _positive_int(value):
    """argparse type: a strictly positive whole number (for --window-days)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _positive_float(value):
    """argparse type: a finite number greater than 0 (for --amount)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number") from None
    if not number > 0 or number == float("inf"):
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return number


def _non_negative_float(value):
    """argparse type: a finite number of seconds, 0 or more (for --pause, --max-wait)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not number >= 0 or number == float("inf"):
        raise argparse.ArgumentTypeError("must be a finite number of seconds, 0 or more")
    return number


def _non_negative_int(value):
    """argparse type: a whole number, 0 or more (for --progress-every)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _lenient_stream(stream):
    """Make a text stream replace unencodable characters instead of raising.

    A Windows console on a legacy code page cannot print a Cyrillic or emoji
    label; the report must not die on it.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


def _prepare_outputs(args: argparse.Namespace) -> None:
    """Create or validate every output location before any API work starts.

    A typo in --report found after an hour of explorer calls costs the whole run.
    """
    out_dir = getattr(args, "out_dir", "")
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            raise ConfigError("--out-dir {!r} cannot be used: {}".format(out_dir, exc)) from None
    for flag in ("report", "json"):
        target = getattr(args, flag, "")
        if not target:
            continue
        if os.path.isdir(target):
            raise ConfigError("--{} {!r} is a directory; give a file path".format(flag, target))
        parent = os.path.dirname(os.path.abspath(target))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                "--{} {!r}: its directory cannot be used: {}".format(flag, target, exc)
            ) from None


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
        type=_positive_int,
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
        help="network name from config/networks.csv, or its chain id (default ethereum)",
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
    _add_network_options(parser)


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
    Tornado deposits found"; a mixed-case address must pass its EIP-55 checksum.
    """
    return config.normalize_address(value)


def _valid_tx_hash(value):
    """Validate a transaction hash, returning it lowercased."""
    tx = (value or "").strip().lower()
    if not config.is_tx_hash(tx):
        raise ConfigError(
            "{!r} is not a transaction hash: expected 0x followed by 64 hex characters.".format(
                value
            )
        )
    return tx


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
    if pb.get("direct_link_verdict"):
        print(f"  [!] Direct links: {pb['direct_link_verdict']}")
    for caveat in pb["caveats"]:
        print(f"  [!] {caveat}")
    print(f"  {pb['note']}")


def _load_labels(args: argparse.Namespace, net) -> dict:
    """Attribution labels, plus the cached Blockscout ones when ``--labels-blockscout``."""
    labels = load_attribution(net.name, getattr(args, "attribution_dir", None))
    if getattr(args, "labels_blockscout", False):
        directory = os.path.join(getattr(args, "cache_dir", ".cache"), "labels", "blockscout")
        if not load_blockscout_cache(net.chain_id, directory):
            print(
                f"[!] --labels-blockscout: no cached Blockscout tags for {net.name} in "
                f"{directory}; fill the cache with 'tornado-demix labels blockscout "
                f"--network {net.name} --addresses-file F' (no Blockscout labels used)",
                file=sys.stderr,
            )
        labels = with_blockscout(labels, net.chain_id, directory)
    return labels


MAX_PRINTED_CANDIDATES = 40  # ranked candidates listed on the console; the CSV lists all
MAX_PRINTED_HASHES = 12  # withdrawal hashes listed per candidate on the console


def _print_ranked(data: dict, labels: dict | None = None) -> None:
    """Print every ranked candidate with band, score, evidence families and the full
    hashes of the withdrawals behind it."""
    ranked = ranked_candidates(data, attribution=labels)
    if not ranked:
        return
    print("\n[candidates] ranked by band, then score (full withdrawal tx hashes):")
    for r in ranked[:MAX_PRINTED_CANDIDATES]:
        tag = f"  [{r['attribution']}]" if r.get("attribution") else ""
        print(f"  {r['band']:<8}{r['confidence']:.2f}  {r['address']} ({r['pool_key']}){tag}")
        families = ", ".join(evidence_families(r["signals"])) or "none"
        context = sorted(set(r["signals"]) & (CONTEXT_SIGNALS | NOTE_SIGNALS))
        extra = f"; context only: {', '.join(context)}" if context else ""
        print(f"     families: {families}{extra}")
        hashes = withdrawal_hashes(data, r["pool_key"], r["address"])
        for h in hashes[:MAX_PRINTED_HASHES]:
            print(f"     tx {h}")
        if len(hashes) > MAX_PRINTED_HASHES:
            print(f"     ... and {len(hashes) - MAX_PRINTED_HASHES} more (all in the CSV and JSON)")
    if len(ranked) > MAX_PRINTED_CANDIDATES:
        print(f"  ... and {len(ranked) - MAX_PRINTED_CANDIDATES} more (all in the CSV and JSON)")


def _provenance(args: argparse.Namespace, net) -> dict:
    """What a case file needs to know about this run (see tornado_demix.honest)."""
    cache = os.path.join(getattr(args, "cache_dir", ".cache"), "labels", "blockscout")
    sets = label_sets_for(
        net,
        getattr(args, "attribution_dir", None),
        cache if getattr(args, "labels_blockscout", False) else None,
    )
    command = getattr(args, "command_line", None) or " ".join(sys.argv)
    return provenance(net, command=command, label_sets=sets)


def _print_footer() -> None:
    """The footer of every analysis: the exploratory banner (no PROTOCOL.lock) and the
    limitations, from the one list in tornado_demix.honest."""
    banner = exploratory_banner()
    if banner:
        print(f"\n[!] {banner}")
    print(f"\n{limitations_text()}")


MAX_INCOMPLETE_LINES = 40  # failed lookups listed on the console; the reports list all


def _print_incomplete(result: dict) -> None:
    """Print "[!] N lookups failed - results may be incomplete" and the list, if any."""
    text = incomplete_summary(result, dash="-")
    if not text:
        return
    print(f"\n[!] {text}:")
    found = incomplete_lines(result)
    for line in found[:MAX_INCOMPLETE_LINES]:
        print(f"     {line}")
    if len(found) > MAX_INCOMPLETE_LINES:
        print(f"     ... and {len(found) - MAX_INCOMPLETE_LINES} more (all in the JSON and HTML)")


def cmd_demix(args: argparse.Namespace) -> None:
    wallet = _valid_address(args.wallet)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = _make_client(args, key, net)
    labels = _load_labels(args, net)
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
        root_check=getattr(args, "root_check", False),
        root_cache_dir=os.path.join(getattr(args, "cache_dir", ".cache"), "merkle"),
    )
    data["provenance"] = _provenance(args, net)

    print("\n=== DEMIX SUMMARY ===")
    print(f"Wallet: {data['wallet']}")
    _print_incomplete(data)
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

    _print_ranked(data, labels)

    fresh = [a for a, f in sorted(data.get("fresh_addresses", {}).items()) if f["fresh"]]
    if fresh:
        print(f"\n[fresh] {len(fresh)} top candidate(s) had at most a day of history before")
        print("  their first withdrawal (disposable exit; context, not scored):")
        for addr in fresh:
            print(f"     {addr}")

    deposits = data.get("deposit_addresses") or []
    if deposits:
        kinds = {d.get("kind", "exchange_deposit") for d in deposits}
        noun = "exchange deposit" if kinds == {"exchange_deposit"} else "deposit"
        print(f"\n[deposit] {len(deposits)} {noun} address(es) the depositor sent to:")
        for d in deposits:
            if d.get("kind", "exchange_deposit") == "exchange_deposit":
                where = f"swept to {d.get('exchange') or 'a hot wallet'}"
            else:
                where = target_label(d)
            print(f"     {d['address']}  {where}; {len(d['senders'])} sender(s) [{kind_label(d)}]")
        shared = {}
        for pool_key, addr, addrs in data.get("heuristics", {}).get("shared_deposits", []):
            shared.setdefault(addr, (pool_key, addrs))
        if shared:
            print("  candidates that sent to one of them too (context only, not a lead):")
            for addr, (pool_key, addrs) in sorted(shared.items()):
                print(f"     [{pool_key}] {addr} via {', '.join(addrs)}")

    rc = data.get("root_check")
    if rc:
        print(
            f"\n[root] Merkle-root check: {rc['withdrawals_checked']} withdrawal(s) read, "
            f"roots found {rc['roots_found']}, not found {rc['roots_not_found']} "
            f"(outside the pool's root history {rc.get('roots_outside_history', 0)}, "
            f"ambiguous {rc.get('roots_ambiguous', 0)}) (exploratory)"
        )
        for pool_key, size in sorted((rc.get("history_sizes") or {}).items()):
            print(f"  [{pool_key}] ROOT_HISTORY_SIZE {size}")
        for c in rc["excluded_candidates"]:
            print(f"  [{c['pool_key']}] excluded by Merkle root: {c['address']}")
            for w in c["withdrawals"]:
                print(f"     {w['tx_hash']} (root holds {w['n_root']} leaves)")
        for c in rc.get("dropped_after_recount", []):
            print(f"  [{c['pool_key']}] dropped after recount (count match lost): {c['address']}")
            for w in c["withdrawals"]:
                print(f"     {w['tx_hash']} (root holds {w['n_root']} leaves)")
        if rc["incomplete"]:
            print("  [!] root_check_incomplete: some candidates were not checked")

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
            root_check=getattr(args, "root_check", False),
            root_cache_dir=os.path.join(getattr(args, "cache_dir", ".cache"), "merkle"),
        )
        _print_placebo(data["placebo"])

    # Relayer intelligence is only available in event mode.
    relayer_stats, leads = None, []
    if args.mode == "events":
        withdrawals = collect_withdrawals(data)
        relayer_stats = analyze_relayers(withdrawals)
        leads, _context, fingerprints = verify_run(client, data)
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
                fee = fee_label(fee_key(lead.get("fee_profile")))
                print(
                    f"     {lead['to']}  {lead['pool_key']}  {lead['tx_hash']}  [{note}; fee {fee}]"
                )
            shared = [g for g in fingerprints if g["shared"]]
            if shared:
                print("  SHARED FEE FINGERPRINTS (context, not scored):")
                for g in shared:
                    print(
                        f"     {g['label']}: {g['withdrawals']} withdrawal(s) to "
                        f"{len(g['recipients'])} recipients; {frequency_text(g)}"
                    )
                    for r in g["recipients"]:
                        print(f"        {r}")

    if data["vouchers"]:
        print("\n[summary]")
        for line in conclusion(data).splitlines():
            print(f"  {line}")

    if args.out_dir:
        files = write_demix_csv(data, args.out_dir, net)
        if relayer_stats is not None:
            files += write_relayer_csv(relayer_stats, leads, args.out_dir, net, data)
        print(f"\n[*] CSV report ({len(files)} files) in: {args.out_dir}", file=sys.stderr)

    json_sha = None
    if getattr(args, "json", ""):
        _path, json_sha = write_demix_json_hashed(data, args.json, attribution=labels)
        print(f"[*] JSON result: {args.json}", file=sys.stderr)

    if args.report:
        write_html_report(data, args.report, net, attribution=labels, json_sha256=json_sha)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)
    _print_footer()


def _print_group(group: dict) -> None:
    """Print the group view of ``multi --group``: caveat first, then the merged notes,
    the ranked candidates and the exploratory exact-k rows."""
    print(f"\n=== {GROUP_VIEW_TITLE} ===")
    print(f"[!] {group.get('caveat', '')}")
    print(f"Wallets merged: {', '.join(group.get('wallets', []))}")
    profile = format_fingerprint(group.get("profile", {})) or "no deposits"
    print(f"Merged notes: {profile}")
    for note in group.get("notices", []):
        print(f"[!] {note}")
    for pool_key, res in sorted(group.get("denoms", {}).items()):
        for w in res.get("windows", []):
            print(
                f"  [{pool_key}] one window, blocks {w.get('start_block')}..{w.get('end_block')}: "
                f"{res.get('total_qualifying_withdrawals', 0)} withdrawals, "
                f"{res.get('unique_recipients', 0)} recipients"
            )
    for u in group.get("unresolved", []):
        print(f"  [{u['pool_key']}] not searched: {u['reason']}")
    if ranked_candidates(group):
        _print_ranked(group)
    else:
        print("  No candidate on the merged notes.")
    rows = group.get("exact_k", [])
    if rows:
        print(f"\n{EXACT_K_TITLE}:")
        print(f"  {EXACT_K_CAVEAT}")
        for r in rows:
            if r["base_windows"]:
                base = (
                    f"base mean {r['base_mean']:.2f} over {r['base_windows']} earlier window(s) "
                    f"{r['base_counts']}; P(>= observed) {exact_k_tail_text(r)}"
                )
            else:
                base = "base mean n/a: no usable earlier window"
            skips = exact_k_skips(r)
            print(
                f"  [{r['pool_key']}] exactly {r['k']} notes within {r['window_hours']:.0f} h: "
                f"observed {r['observed']}; {base}" + (f"; {skips}" if skips else "")
            )


def cmd_multi(args: argparse.Namespace) -> None:
    if getattr(args, "exact_k", False) and not getattr(args, "group", False):
        raise ConfigError("--exact-k needs --group (it runs on the merged notes)")
    wallets = _wallets_from_args(args)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = _make_client(args, key, net)
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
        labels=_load_labels(args, net),
        deposit_addresses=not getattr(args, "no_deposit_addresses", False),
        group=getattr(args, "group", False),
        exact_k=getattr(args, "exact_k", False),
        exact_k_windows=getattr(args, "exact_k_windows", None),
    )

    print("\n=== CROSS-WALLET CORRELATION ===")
    print(f"Wallets analysed: {len(wallets)}")
    _print_incomplete(corr)
    print(f"{SHARED_CANDIDATES_TITLE}: {len(corr['strong'])}")
    print(f"  {SHARED_CANDIDATES_CAVEAT}")
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

    if corr.get("group") is not None:
        _print_group(corr["group"])

    if getattr(args, "json", ""):
        write_multi_json(corr, args.json)
        print(f"[*] JSON result: {args.json}", file=sys.stderr)

    if args.out_dir:
        files = write_multi_csv(corr, args.out_dir, net)
        print(f"\n[*] CSV report ({len(files)} files) in: {args.out_dir}", file=sys.stderr)

    if args.report:
        write_multi_report(corr, args.report, net)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)
    _print_footer()


def cmd_cluster(args: argparse.Namespace) -> None:
    wallets = _wallets_from_args(args)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = _make_client(args, key, net)

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
        _print_incomplete(trace)
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
    _print_footer()


def cmd_characterize(args: argparse.Namespace) -> None:
    address = _valid_address(args.address)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = _make_client(args, key, net)
    labels = _load_labels(args, net)
    info = characterize_address(
        client,
        address,
        net,
        labels=labels,
        deposit_check=not getattr(args, "no_deposit_check", False),
        is_contract=make_contract_check(net.rpc_url),
    )

    print("\n=== CANDIDATE CHARACTERISATION ===")
    print(f"Address: {info['address']}")
    _print_incomplete(info)
    if info.get("label"):
        print(
            f"Attribution: {format_label(info['label'])}"
            + (f" - {info['label']['entity']}" if info["label"].get("entity") else "")
        )
    print(f"Classification: {info['classification'].upper()} - {info['classification_reason']}")
    if info.get("deposit_check"):
        print(f"Deposit-address check: {check_text(info['deposit_check'])}")
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
            if h.get("deposit_check"):
                print(f"         deposit-address check: {check_text(h['deposit_check'])}")

    print(
        "\nAll of this is a lead, not proof. Corroborate deposit->inflow timing "
        "and the next hop before drawing a conclusion."
    )

    if getattr(args, "report", ""):
        write_characterize_report(info, args.report, net)
        print(f"\n[*] HTML report: {args.report}", file=sys.stderr)
    _print_footer()


def cmd_classify(args: argparse.Namespace) -> None:
    """``classify-address``: the deposit-address check of one given address."""
    address = _valid_address(args.address)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = _make_client(args, key, net)
    labels = _load_labels(args, net)
    info = run_classify(client, address, net, labels, make_contract_check(net.rpc_url))

    print("\n=== DEPOSIT-ADDRESS CHECK ===")
    print(f"Address: {info['address']} on {net.name}")
    _print_incomplete(info)
    print(f"Result: {check_text(info)}")
    for t in info.get("sweep_targets") or []:
        tag = format_label(labels.get(t)) if labels else ""
        print(f"  swept to {t}" + (f"  [{tag}]" if tag else ""))
    senders = info.get("senders") or []
    for s in senders[:20]:
        print(f"  sender {s}")
    if len(senders) > 20:
        print(f"  ... and {len(senders) - 20} more (all in the JSON and HTML)")
    print(
        "\nBehaviour, not identity. Two addresses that sent to the same deposit address "
        "are context only, never a lead."
    )
    if getattr(args, "json", ""):
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=2, sort_keys=True)
        print(f"\n[*] JSON: {args.json}", file=sys.stderr)
    if getattr(args, "report", ""):
        write_classify_report(info, args.report, net)
        print(f"\n[*] HTML report: {args.report}", file=sys.stderr)
    _print_footer()


def _chain_name(destinations, chain_id):
    """Registry name of a chain id, or the bare id."""
    net = destinations.network(chain_id) if chain_id is not None else None
    return net.name if net else ("" if chain_id is None else f"chain {chain_id}")


def cmd_trace(args: argparse.Namespace) -> None:
    address = _valid_address(args.address)
    key = config.load_api_key(args.api_csv)
    net = _network(args)
    client = _make_client(args, key, net)
    labels = _load_labels(args, net)
    token = _valid_address(args.token) if args.token else None
    arrival_tx = _valid_tx_hash(args.arrival_tx) if getattr(args, "arrival_tx", "") else None
    destinations = Destinations(key, args.networks_csv, getattr(args, "attribution_dir", None))
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
        chain_id=net.chain_id,
        destinations=destinations,
        arrival_tx=arrival_tx,
    )

    def chain_name(chain_id):
        return _chain_name(destinations, chain_id)

    print("\n=== MULTI-HOP TRACE (FIFO) ===")
    print(f"Start: {result['start']}  {args.amount} {result['asset']}")
    if arrival_text(result):
        print(f"Arrival: {arrival_text(result)}")
    _print_incomplete(result)
    for e in result["edges"]:
        swap = f" -> swapped to {e['swapped_to']}" if e["kind"] == "swap" else ""
        if e["kind"] == "bridge":
            swap = " -> " + bridge_text(e["bridge"], chain_name, arrow="->")
        chain = _chain_name(destinations, e.get("chain_id"))
        where = f" [{chain}]" if chain and e.get("chain_id") != net.chain_id else ""
        print(
            f"{'  ' * e['hop']}hop {e['hop']}{where}: {e['from']} -> {e['to']}  "
            f"{share_text(e, '-')} of {e['value']} {e['asset']}"
            f"{'  ' + share_status(e) if share_status(e) else ''}{swap}  {e['tx_hash']}"
        )
    print("\nWhere the traced funds stop:")
    for t in result["terminals"]:
        tag = format_label(t["label"]) if t["label"] else ""
        chain = _chain_name(destinations, t.get("chain_id"))
        where = f"  on {chain}" if chain and t.get("chain_id") != net.chain_id else ""
        # a bridge end reason that does not already name the protocol gets the bridge line
        bridged = terminal_bridge_text(t, destinations, arrow="->")
        print(
            f"  {t['address']}  {t['amount']} {t['asset']}  hop {t['hop']}  [{t['reason']}]"
            + (f"  {bridged}" if bridged else "")
            + where
            + (f"  {tag}" if tag else "")
        )
    print(
        "\nFIFO attribution is a convention: funds in one account are fungible, "
        "so each edge is a lead to corroborate. [lower - upper] is the share the "
        "traced funds can have under any ordering; ? marks an incomplete history, "
        "where nothing is certain."
    )
    print(END_STATES_NOTE)
    if getattr(args, "json", ""):
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1, sort_keys=True)
        print(f"[*] JSON trace: {args.json}", file=sys.stderr)
    if getattr(args, "report", ""):
        write_trace_report(result, args.report, net, destinations)
        print(f"[*] HTML report: {args.report}", file=sys.stderr)
    _print_footer()


LABELS_NOTE = (
    "Labels are third-party data and are not bundled: each upstream source keeps its "
    "own licence (see the README of prettydeath/wallet-attribution)."
)


def _count(n: int) -> str:
    return "{:,}".format(n)


def cmd_labels(args: argparse.Namespace) -> None:
    """``labels fetch`` / ``labels status``: manage the attribution set."""
    print(LABELS_NOTE)
    if args.labels_command == "blockscout":
        _cmd_labels_blockscout(args)
        return
    if args.labels_command == "fetch":
        results = fetch_attribution(args.network or None, args.dir)
        print("{:<10} {:>9} {:>9}  {}".format("network", "rows", "exchange", "result"))
        for name, r in results.items():
            print(
                "{:<10} {:>9} {:>9}  {}".format(
                    name,
                    _count(r["rows"]),
                    _count(r["exchange"]),
                    r["path"] if r["status"] == "ok" else r["status"],
                )
            )
        if any(r["status"] != "ok" for r in results.values()):
            raise SystemExit(1)
        return
    print("{:<10} {:>9} {:>9}  {}".format("network", "rows", "exchange", "file"))
    for name, r in attribution_status(args.network or None, args.dir).items():
        print(
            "{:<10} {:>9} {:>9}  {}".format(
                name, _count(r["rows"]), _count(r["exchange"]), r["path"] or "not loaded"
            )
        )


def _add_network_options(parser) -> None:
    """Flags of the data-provider layer, shared by every command that reads the chain."""
    parser.add_argument(
        "--max-wait",
        type=_non_negative_float,
        default=DEFAULT_MAX_WAIT,
        metavar="SECONDS",
        help="longest one call backs off on a transient error (rate limit, HTTP 5xx, "
        "timeout) before giving up (default {:.0f}). Permanent errors (invalid key, "
        "chain not on the plan, bad address) fail at once".format(DEFAULT_MAX_WAIT),
    )
    parser.add_argument(
        "--pause",
        type=_non_negative_float,
        default=0.0,
        metavar="SECONDS",
        help="wait this long before every explorer call; for a free key shared with "
        "other programs (default 0)",
    )
    parser.add_argument(
        "--http-cache",
        default=None,
        metavar="DIR",
        help="keep explorer answers in DIR and reuse them. Only queries that end at "
        "least {} blocks below the chain head are stored; errors and the API key "
        "never are".format(CACHE_SAFE_DEPTH),
    )
    parser.add_argument(
        "--progress-every",
        type=_non_negative_int,
        default=DEFAULT_PROGRESS_EVERY,
        metavar="N",
        help="print a progress line to stderr every N explorer calls "
        "(default {}; 0 = silent)".format(DEFAULT_PROGRESS_EVERY),
    )


def _make_client(args, key, net):
    """The explorer client for ``net`` with the network-layer flags applied."""
    return EtherscanClient(
        key,
        pause=getattr(args, "pause", 0.0),
        max_wait=getattr(args, "max_wait", DEFAULT_MAX_WAIT),
        cache_dir=getattr(args, "http_cache", None),
        progress_every=getattr(args, "progress_every", 0),
        **net.client_kwargs(),
    )


def _add_cache_dir(parser) -> None:
    parser.add_argument(
        "--cache-dir",
        default=".cache",
        help="cache root; --labels-blockscout reads <dir>/labels/blockscout/ (default .cache)",
    )


def _add_blockscout_flag(parser) -> None:
    parser.add_argument(
        "--labels-blockscout",
        action="store_true",
        help="also use cached Blockscout metadata tags (fill the cache with "
        "'labels blockscout'); a source of its own lineage, opt-in",
    )


def _cmd_labels_blockscout(args: argparse.Namespace) -> None:
    net = get_network(args.network, args.networks_csv)
    addresses = [_valid_address(a) for a in args.address]
    if args.addresses_file:
        with open(args.addresses_file, encoding="utf-8") as fh:
            addresses += [_valid_address(ln.strip()) for ln in fh if ln.strip()]
    if not addresses:
        raise ConfigError("give --address and/or --addresses-file")
    if args.out:
        fresh = to_labels(fetch_tags(net.chain_id, addresses))
        path = write_cache(net.chain_id, fresh, path=args.out)
        found = len(fresh)
    else:
        directory = os.path.join(args.cache_dir, "labels", "blockscout")
        cache = fetch_and_cache(net.chain_id, addresses, directory=directory)
        found = sum(1 for a in addresses if a.lower() in cache)
        path = os.path.join(directory, "{}.csv".format(net.chain_id))
    print(
        "{}: {} of {} addresses tagged ({}); Blockscout tags are often copied from "
        "Etherscan, so they are not corroboration of an Etherscan-derived label.".format(
            net.name, found, len(addresses), path
        )
    )


EPILOG = """Examples:
  tornado-demix demix 0x0000000000000000000000000000000000000001 --rapid --report demix.html
  tornado-demix characterize 0x0000000000000000000000000000000000000002 --network 137
  tornado-demix trace 0x0000000000000000000000000000000000000002 --amount 10 --json trace.json

Exit codes:
  0  finished (an empty result is still 0: read the report for what was and was not found)
  1  the run stopped on a package error: bad input, missing key or file, failed lookup
  2  usage error: an option or value argparse rejected
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tornado-demix",
        description=(
            "Tornado Cash forensics on 8 EVM chains: evidence-banded exit leads, exit groups, operator clusters, tracing."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--root-check",
        action="store_true",
        help="exploratory: read the Merkle root of each candidate withdrawal and drop those "
        "whose root predates every note of the depositor in that pool (a candidate with all "
        "withdrawals excluded leaves the ranking). One transaction lookup per withdrawal (max "
        "200); the first run builds a root index per pool, about 30 min for a large one, "
        "cached under --cache-dir/merkle",
    )
    p_demix.add_argument(
        "--cache-dir", default=".cache", help="directory for the Merkle root index cache"
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
    _add_blockscout_flag(p_demix)
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
    p_multi.add_argument(
        "--json", default="", help="write the correlation (and the group view) as JSON to this path"
    )
    p_multi.add_argument(
        "--group",
        action="store_true",
        help="also treat the wallets as ONE depositor (merged deposits, one window per pool, "
        "count match and early profile on the merged notes); conditional on your assertion "
        "that they are one actor. Per-wallet results do not change",
    )
    p_multi.add_argument(
        "--exact-k",
        action="store_true",
        help="with --group: exploratory context, recipients with exactly the group's k notes "
        "of a pool against earlier windows of the same pool (extra log reads, see "
        "--exact-k-windows); never scored",
    )
    p_multi.add_argument(
        "--exact-k-windows",
        type=_positive_int,
        default=None,
        metavar="N",
        help="earlier base windows per pool for --exact-k (default 6, at most 20); each costs "
        "one log read of the pool over the group window's length; together at most 365 days",
    )
    _add_common(p_multi)
    _add_cache_dir(p_multi)
    _add_blockscout_flag(p_multi)
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
        help="network name from config/networks.csv, or its chain id (default ethereum)",
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
    p_char.add_argument(
        "--no-deposit-check",
        action="store_true",
        help="skip the deposit-address check of the address and its next hops (saves two "
        "explorer calls per address)",
    )
    _add_network_options(p_char)
    _add_cache_dir(p_char)
    _add_blockscout_flag(p_char)
    p_char.set_defaults(func=cmd_characterize)

    p_trace = sub.add_parser(
        "trace", help="follow withdrawn funds forward over several hops (FIFO, swaps)"
    )
    p_trace.add_argument("address", help="exit address the funds were withdrawn to (0x...)")
    p_trace.add_argument("--amount", type=_positive_float, required=True, help="amount to follow")
    p_trace.add_argument(
        "--token", default="", help="ERC-20 contract of the amount (default: native currency)"
    )
    p_trace.add_argument(
        "--start-block", type=int, default=0, help="block the funds arrived in (default 0)"
    )
    p_trace.add_argument(
        "--arrival-tx",
        default="",
        help="transaction that brought the amount to the address (default: matched by amount)",
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
        help="network name from config/networks.csv, or its chain id (default ethereum)",
    )
    p_trace.add_argument("--networks-csv", default=None, help="CSV defining pools per network")
    p_trace.add_argument(
        "--attribution-dir",
        default=None,
        help="directory holding <network>.csv attribution tables; a labelled "
        "address ends the trace",
    )
    _add_network_options(p_trace)
    _add_cache_dir(p_trace)
    _add_blockscout_flag(p_trace)
    p_trace.set_defaults(func=cmd_trace)

    p_class = sub.add_parser(
        "classify-address",
        help="deposit-address check of one address (exchange / service / deposit-like "
        "deposit address, forwarder, smart account, or none)",
    )
    p_class.add_argument("address", help="address to classify (0x...)")
    p_class.add_argument(
        "--api-csv", default=None, help="CSV with an api_key for the explorer (see demix)"
    )
    p_class.add_argument(
        "--network",
        default="ethereum",
        help="network name from config/networks.csv, or its chain id (default ethereum)",
    )
    p_class.add_argument("--networks-csv", default=None, help="CSV defining pools per network")
    p_class.add_argument(
        "--attribution-dir",
        default=None,
        help="directory holding <network>.csv attribution tables (labels of sweep targets)",
    )
    p_class.add_argument("--json", default="", help="write the result as JSON to this path")
    p_class.add_argument("--report", default="", help="write an HTML report to this path")
    _add_network_options(p_class)
    _add_cache_dir(p_class)
    _add_blockscout_flag(p_class)
    p_class.set_defaults(func=cmd_classify)

    p_labels = sub.add_parser(
        "labels", help="download or inspect the address-label (attribution) set"
    )
    labels_sub = p_labels.add_subparsers(dest="labels_command", required=True)
    for name, text in (
        ("fetch", "download the public label set (prettydeath/wallet-attribution)"),
        ("status", "show which networks have labels loaded"),
    ):
        p_sub = labels_sub.add_parser(name, help=text)
        p_sub.add_argument(
            "--network",
            action="extend",
            nargs="+",
            default=[],
            metavar="N",
            help="network(s) to {} (default: all networks of the tool)".format(name),
        )
        p_sub.add_argument(
            "--dir",
            default=None,
            help="directory holding <network>.csv; default $TORNADO_DEMIX_ATTRIBUTION "
            "or config/attribution/",
        )
    p_bs = labels_sub.add_parser(
        "blockscout",
        help="fetch Blockscout metadata tags for addresses (needs the labels extra for "
        "EIP-55 checksums); cached for --labels-blockscout",
    )
    p_bs.add_argument("--network", default="ethereum", help="network name (default ethereum)")
    p_bs.add_argument("--networks-csv", default=None, help="CSV defining pools per network")
    p_bs.add_argument(
        "--address", action="extend", nargs="+", default=[], metavar="A", help="address(es)"
    )
    p_bs.add_argument("--addresses-file", default=None, help="file with one address per line")
    p_bs.add_argument(
        "--out",
        default=None,
        help="write only the fetched labels to this CSV instead of adding them to the cache",
    )
    p_bs.add_argument(
        "--cache-dir", default=".cache", help="cache root; tags go to <dir>/labels/blockscout/"
    )
    p_labels.set_defaults(func=cmd_labels)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Run a subcommand, turning package errors into a clean non-zero exit.

    The only place a ``TornadoDemixError`` becomes a ``SystemExit``.
    """
    _lenient_stream(sys.stdout)
    _lenient_stream(sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "rapid", False) and getattr(args, "exit_window", None) is None:
        args.exit_window = RAPID_EXIT_WINDOW_HOURS
    args.command_line = " ".join(["tornado-demix"] + list(sys.argv[1:] if argv is None else argv))
    try:
        _prepare_outputs(args)
        args.func(args)
    except TornadoDemixError as exc:
        raise SystemExit("{}: {}".format(type(exc).__name__, exc)) from None


if __name__ == "__main__":
    main()
