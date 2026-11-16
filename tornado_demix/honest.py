"""The texts every report shares: how to read it, its limitations, its status.

One source for each, so the CLI footer, the HTML sections and the web UI panel
cannot drift apart:

* :func:`how_to_read` is generated from ``heuristics.LEAD_SOURCE`` and
  ``heuristics.CONTEXT_SIGNALS``; a signal that stops being a lead stops being
  described as one.
* :data:`LIMITATIONS` is the one list of limitations (:func:`limitations`).
* :func:`exploratory_banner` says so when ``docs/PROTOCOL.lock`` is absent.
* :func:`provenance` collects what a case file needs to know about a run: version,
  commit, command line, data sources, label sets, UTC time.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .heuristics import CONTEXT_SIGNALS, CONTEXT_VERDICT, EARLY_EXIT_HOURS, LEAD_SOURCE
from .registry import PROTOCOL_LOCK_DEFAULT

# What each context-only signal is called in running text. The keys must equal
# ``heuristics.CONTEXT_SIGNALS`` (a test pins that).
CONTEXT_PHRASE = {
    "linked_late": f"a direct link first withdrawn more than {EARLY_EXIT_HOURS} h after the last deposit",
    "linked_sender": "a withdrawal sent by the depositor's side",
    "shared_deposit": "a shared exchange deposit address",
    "linked_posthoc": "a direct link whose real-value transaction came after the first withdrawal",
    "linked_dust": "a direct link by a zero-value or dust transaction",
    "linked_unverified_token": "a direct link by a token off the list of real assets",
    "linked_unknown_contract": "a direct link to an address whose contract check failed",
}

# Evidence read from the same withdrawals as the count match, or from fee settings:
# shown, never a lead and never able to raise the band.
OTHER_CONTEXT = ("amount+timing (count match)", "gas price", "self-relayed withdrawals")


def context_verdicts() -> str:
    """What is known about each context signal: its own wording, from one source."""
    parts = [f"{CONTEXT_PHRASE[s]}: {CONTEXT_VERDICT[s]}" for s in sorted(CONTEXT_SIGNALS)]
    parts.append("amount+timing and gas price: did not beat chance on real depositors")
    return "; ".join(parts)


# The bridge coverage of the trace, in one place: the README, docs/llms.txt and the
# limitations below carry this text word for word (a test compares them). No markdown.
BRIDGES_SUMMARY = (
    "Bridges followed across chains: Across (V3 and current deposits; SpokePools on "
    "Ethereum, Optimism, BNB Chain, Polygon, Arbitrum, Base and Avalanche; a deposit made "
    "directly, through a periphery or router, after a swap, to the "
    "MulticallHandler or from an ERC-4337 smart account), native-ETH deposits through the "
    "Arbitrum Inbox and ERC-20 deposits through the Arbitrum token gateways (followed on "
    "Arbitrum once the L2 credit is found), and Circle CCTP burns (v1 and v2) to an EVM "
    "chain of the registry (followed from the mint). A Mayan same-chain payout and a CoW "
    "Protocol order (tied to the owner's trade and its payout in the settlement receipt) "
    "are followed as swaps. Named, not followed: ZKsync deposits (Bridgehub native and "
    "ERC-20, legacy Era requestL2Transaction) show the L2 recipient; Relay, Mayan, "
    "THORChain, Maya, Layerswap, NEAR Intents, Unit and Hyperliquid deposits show what the "
    "chain gives (request id, memo, destination, recipient); a CCTP burn to Solana or "
    "another non-EVM domain shows its recipient; other listed bridges are named only. "
    "A listed swap-and-bridge router (LI.FI, Socket, Rango, Squid) that only refunds native "
    "coin ends as bridge or swap unknown, and bridges that are not listed end as contracts. A hop "
    "is followed only when the decoded call is tied to the traced transfer; otherwise the "
    "branch ends with a named state."
)

# The one list of limitations. Every report prints all of it.
LIMITATIONS = (
    "Exchange hot wallets are recognised by their labels. A proxy hot wallet (for example "
    "Binance's) or a low-traffic hot wallet that carries no label is missed, so a deposit "
    "address swept to it is not seen as an exchange deposit address.",
    "BSC is not served by the free Etherscan tier, so BSC runs need a paid key or another "
    "provider; without one, results on BSC are missing, not empty.",
    BRIDGES_SUMMARY,
    "Two cases are followed with a limit. An Across deposit made after a swap is followed as "
    "the amount the fill paid (no fee bound across two tokens); a fill to the Across "
    "MulticallHandler is followed only to the handler's payments in the fill transaction "
    "(calls of the message that swap are not followed). A CoW Protocol settlement is a "
    "batch of several users' orders: the payout is the order's output, not a path through "
    "the batch.",
    'The multi-wallet profile match uses ">=" (an address that received at least as many '
    "withdrawals per pool as the wallet's fingerprint) and a fixed 6 h synchronicity gap; "
    "both are exploratory, not measured.",
    "A direct link is a lead only when its transaction succeeded, moved real value (native "
    "coin at or above a per-network dust floor, about 0.001 ETH worth, or a listed real "
    "token such as USDC, USDT, DAI or WETH above its floor) and came before the first "
    "withdrawal to that address, and the address is known not to be a contract. Zero-value, "
    "dust, unlisted-token and post-withdrawal links, and addresses whose contract check "
    "failed, are context. The floor prices a planted link, it does not stop one: an "
    "outgoing link above the floor can be created by whoever controls the depositor, for "
    "about the floor plus gas per address (about 0.001 ETH worth on every network, at "
    "prices of 2026-11-14), so a depositor can seed early direct links to addresses that "
    "later receive withdrawals. The token list covers the main stablecoins and wrapped "
    "coins of each supported network (native and bridged USDC, USDT, DAI, WETH and the "
    "like); a payment in another real token is context, and the floors are judgement "
    "calls, not measured.",
    "A shared exchange deposit address (rule 2.21) is context, not scored. The rule was set "
    "after the second hold-out had been read (post hoc) and has not been tested on new "
    "data.",
    "Deposit-address kinds (exchange_deposit, service_deposit, deposit_like, paid_exchange, "
    "forwarder, smart_account), the sender and gas-funder thresholds and the bridge decoding "
    "are exploratory: reasonable settings that were not measured.",
    "An exchange label on a sweep target names the exchange; it is not evidence of a sweep. "
    "A labelled exchange target counts only when the address sweeps its balance there: one "
    "transfer of at least 90 % of what arrived in that asset since its previous outgoing "
    "transfer, within 30 days of the latest arrival, or one that passes the 38,400 s / "
    "0.01 ETH forwarding test (unlabelled targets keep only that test). An address that only paid a labelled exchange is "
    "paid_exchange (context), not an exchange deposit address. The 90 % and 30-day values "
    "are exploratory, not measured; one such sweep is enough, repetition is not required, "
    "and the classifier's precision and recall on independently labelled addresses were "
    "not measured.",
    "Address histories can be incomplete (a lookup failed, the explorer cut a list off or "
    "answered while part of the range was still being indexed: partial indexing). "
    "When one is, the report carries a notice at the top; a missing row is then not an "
    "absent transaction.",
    "Fee fingerprints are context: many users share wallet defaults, so identical fee "
    "settings do not show one operator.",
    "Transactions are read as they were mined. Fee-bump and replacement transactions, "
    "private relays and MEV bundles are not covered: nothing in the data shows how a "
    "transaction was routed or ordered, so a path that went through one is not "
    "recognised as such.",
    "A depositor can send notes and a direct transfer to an address that never agreed to "
    "receive them (an unsolicited receipt). Since 2.23 a direct link needs real value above a "
    "per-network floor and must come strictly before the first withdrawal; that raises the "
    "cost of planting one but does not stop a depositor who sends real value before "
    "withdrawing. A band says that funds reached the address, not that its owner controls it "
    "or took part.",
    "Leads, not proof. The score is uncalibrated and only orders candidates within a band.",
)


# The count-matched candidates two or more wallets share (``corr["strong"]``).
SHARED_CANDIDATES_TITLE = "Shared count-matched candidates (window overlap, not evidence)"
SHARED_CANDIDATES_CAVEAT = (
    "Wallets that deposited at similar times with equal voucher sizes share candidate sets "
    "by construction, so a raw overlap is a window artefact, not evidence of a link. The "
    "discriminating signals are the operator clusters and the cross-wallet consolidators."
)


# The group view of ``multi --group`` (``corr["group"]``, tornado_demix.multi.group_view).
GROUP_VIEW_TITLE = "Group view (the wallets treated as one depositor)"
GROUP_VIEW_CAVEAT = (
    "Conditional on your assertion that these wallets are one actor. The group view merges "
    "their deposits into one depositor and runs the count match, the voucher-order check and "
    "the early-profile check on the merged notes, so a match or a band here holds only if "
    "that assertion is true; merging unrelated wallets produces matches by chance. The tool "
    "does not test the assertion, and the per-wallet results are unchanged."
)
EXACT_K_TITLE = "Exactly k notes within the group window (exploratory context)"
EXACT_K_CAVEAT = (
    "Exploratory context, never scored and never a lead: how many recipients received "
    "exactly the group's k notes of a pool inside the group window, against the same count "
    "in earlier non-overlapping windows of the same length in the same pool (windows with "
    "no withdrawal at all are skipped). P is smoothed, (x + 1) / (n + 1) over n usable "
    "windows. A base window only partly covered by the pool's life, or cut off by the "
    "explorer's list limit, holds fewer withdrawals and biases P downward; the observed "
    "count may include the group's own lead. Not measured on labelled data."
)


def limitations() -> list[str]:
    """The limitations of every report, in order."""
    return list(LIMITATIONS)


def json_fields(path: str | None = None) -> dict:
    """The honesty fields every JSON export carries: the limitations and whether the
    run is exploratory (no PROTOCOL.lock), with the banner text."""
    banner = exploratory_banner(path)
    return {
        "limitations": limitations(),
        "exploratory": bool(banner),
        "exploratory_banner": banner,
    }


def limitations_text(prefix: str = "  - ") -> str:
    """The limitations as a console block (CLI footer)."""
    return "\n".join(["Limitations:"] + [f"{prefix}{line}" for line in LIMITATIONS])


def how_to_read() -> list[str]:
    """The "how to read this" paragraphs, built from the signal constants."""
    leads = " and ".join(sorted(LEAD_SOURCE.values()))
    context = "; ".join(CONTEXT_PHRASE[s] for s in sorted(CONTEXT_SIGNALS))
    return [
        "This report lists probabilistic leads, not proof.",
        f"Lead signals, the only ones that set the band: {leads}. A direct link is a lead "
        f"only when its first withdrawal came within {EARLY_EXIT_HOURS} h of the last "
        "deposit. The band rises to strong only when two different lead-signal families agree; "
        "their independence is not established.",
        f"Context only (shown, never scored, never a lead): {context}; also "
        + ", ".join(OTHER_CONTEXT)
        + f". Per signal: {context_verdicts()}.",
        "A bare count match on a busy pool is weak, and single-note (count of 1) vouchers "
        "cannot be demixed by this method.",
        "Corroborate any candidate with independent evidence (funding source, exchange KYC, "
        "further hops) before drawing a conclusion.",
    ]


def band_note() -> str:
    """The note under the ranked table: what the bands mean, from the signal constants."""
    leads = " and ".join(sorted(LEAD_SOURCE.values()))
    context = "; ".join(CONTEXT_PHRASE[s] for s in sorted(CONTEXT_SIGNALS))
    return (
        f"The band is the headline. strong: lead signals from two different lead-signal families "
        f"({leads}; their independence is not established); moderate: a lead signal from one source; weak: no lead signal, only "
        f"{', '.join(OTHER_CONTEXT)} and context-only signals ({context}); per signal: "
        f"{context_verdicts()}. The score only orders leads within a band; it is "
        "uncalibrated and not a probability. Open Evidence to see what was checked for "
        "each address."
    )


def lock_present(path: str | None = None) -> bool:
    """True when the pre-registration lock file exists."""
    return os.path.isfile(path or PROTOCOL_LOCK_DEFAULT)


def exploratory_banner(path: str | None = None) -> str:
    """The "exploratory" banner, or "" when ``docs/PROTOCOL.lock`` exists."""
    if lock_present(path):
        return ""
    return (
        "Exploratory: no PROTOCOL.lock. None of the rules behind this report is locked "
        "as confirmatory; treat the output as exploratory."
    )


_SECRET_OPTION = re.compile(r"(key|token|secret|password|passwd|auth)", re.IGNORECASE)


def scrub_command(argv) -> str:
    """A command line for the record with the value of any key-like option removed
    (``--api-key X`` and ``--api-key=X`` become ``--api-key <hidden>``). The CLI has no
    such option today; this keeps one added later out of the reports. ``--api-csv`` is a
    file path, not a secret, and stays."""
    out, hide = [], False
    for arg in argv:
        if hide:
            out.append("<hidden>")
            hide = False
            continue
        if arg.startswith("--"):
            name, eq, _value = arg.partition("=")
            if _SECRET_OPTION.search(name) and name != "--api-csv":
                if eq:
                    out.append(f"{name}=<hidden>")
                else:
                    out.append(arg)
                    hide = True
                continue
        out.append(arg)
    return " ".join(out)


def commit_id() -> str:
    """Short git commit of the installed code, or ``unknown`` outside a checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=here,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else "unknown"


def sha256_file(path: str) -> str | None:
    """SHA-256 of a file, or None when it cannot be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def label_set_info(path: str | None, name: str = "attribution labels") -> dict:
    """One label set: ``{name, file, sha256, modified_utc}``; ``sha256`` is None when absent."""
    if not path or not os.path.isfile(path):
        return {"name": name, "file": path or "", "sha256": None, "modified_utc": None}
    modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return {
        "name": name,
        "file": os.path.basename(path),
        "sha256": sha256_file(path),
        "modified_utc": modified.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def label_sets_for(network, attribution_dir=None, blockscout_dir=None) -> list[dict]:
    """The label sets a run may have used: the attribution CSV of ``network`` and, when
    ``blockscout_dir`` is given, the cached Blockscout tags."""
    from .attribution import attribution_file
    from .blockscout_labels import cache_path

    sets = [label_set_info(attribution_file(network.name, attribution_dir))]
    if blockscout_dir is not None:
        sets.append(
            label_set_info(cache_path(network.chain_id, blockscout_dir), "Blockscout tags (cache)")
        )
    return sets


def data_sources(network) -> list[str]:
    """The services a run reads from. Host names only: a node URL may carry a key."""
    sources = []
    base = getattr(network, "api_base", None)
    host = urlparse(base).hostname if base else None
    sources.append(
        f"Etherscan-compatible explorer API ({host})" if host else "Etherscan V2 explorer API"
    )
    if getattr(network, "rpc_url", None):
        sources.append("a JSON-RPC node (configured, address not recorded)")
    return sources


def provenance(network=None, command=None, label_sets=None) -> dict:
    """What a case file needs to know about a run (see the module docstring)."""
    from . import __version__

    return {
        "version": __version__,
        "commit": commit_id(),
        "command": scrub_command(command.split())
        if command is not None
        else scrub_command(sys.argv),
        "generated_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_sources": data_sources(network) if network is not None else [],
        "label_sets": list(label_sets or []),
        "protocol_lock": lock_present(),
    }
