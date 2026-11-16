"""Per-network Tornado.Cash pool registry.

Ethereum mainnet is built in. The full registry ships inside the package at
``tornado_demix/data/networks.csv`` (every address verified on-chain with
``tools/verify_pools.py``); ``config/networks.csv`` overrides it when present.

Columns (all after ``pool_address`` optional)::

    network,chain_id,currency,asset,denomination,pool_address,decimals,
    token_address,api_base,api_style,rpc_url,explorer_addr,explorer_tx,
    router_address

``asset`` defaults to ``currency``, ``decimals`` to 18, and explorer URLs come
from the chain-id table below.

``router_address`` declares a deposit proxy in front of the pools. The values
accumulate across rows, so a chain with several routers lists one per row.
:func:`~tornado_demix.demix.detect_deposits` follows the router path only for a
declared router, so a chain without one detects direct-to-pool deposits only.
Ethereum, Polygon, Avalanche, Arbitrum, Optimism, Gnosis and Base ship verified
routers; BSC ships none, because no proxy there has been verified on-chain.

Never add an address you have not verified: a wrong pool produces confident,
meaningless results.
"""

from __future__ import annotations

import csv
import os
import sys

from . import config
from .constants import CHAIN_ID, ETHERSCAN_API_URL, POOLS, ROUTERS
from .errors import RegistryError
from .pools import Pool, assign_keys

DEFAULT_NETWORKS_CSV = "networks.csv"


def _log(msg):
    """Registry problems go to stderr: a rejected row is a pool the run will
    not search, and the operator has to know."""
    print(msg, file=sys.stderr, flush=True)


# chain id -> (address URL template, tx URL template)
EXPLORERS = {
    1: ("https://etherscan.io/address/", "https://etherscan.io/tx/"),
    10: ("https://optimistic.etherscan.io/address/", "https://optimistic.etherscan.io/tx/"),
    56: ("https://bscscan.com/address/", "https://bscscan.com/tx/"),
    100: ("https://gnosisscan.io/address/", "https://gnosisscan.io/tx/"),
    137: ("https://polygonscan.com/address/", "https://polygonscan.com/tx/"),
    8453: ("https://basescan.org/address/", "https://basescan.org/tx/"),
    42161: ("https://arbiscan.io/address/", "https://arbiscan.io/tx/"),
    43114: ("https://snowtrace.io/address/", "https://snowtrace.io/tx/"),
}

_FALLBACK_EXPLORER = ("https://etherscan.io/address/", "https://etherscan.io/tx/")


# Keyless Etherscan-compatible explorers for chains the free Etherscan V2 tier
# does not serve. Etherscan stays first (a paid key works); the client switches
# to the fallback only when Etherscan refuses the chain. Blockscout throttles
# keyless clients, so heavy use may still need a paid Etherscan plan.
KEYLESS_FALLBACK = {
    8453: "https://base.blockscout.com/api",  # Base
}


# Gas-funder threshold per chain id, in wei of the native coin: a native transfer below
# it that precedes a deposit address's first token sweep is taken for a gas top-up
# (``deposit_addresses._gas_funder``; context, never scored). No price feed: a fixed
# amount of each coin, a few times what a handful of token transfers cost there.
# Polygon from observed exchange top-ups of 0.0155-1.6 POL; the other values are
# estimates of the same order (exploratory, not measured).
GAS_FUND_WEI = {
    1: 10**16,  # Ethereum: 0.01 ETH
    10: 10**16,  # Optimism: 0.01 ETH
    56: 10**16,  # BSC: 0.01 BNB
    100: 10**18,  # Gnosis: 1 xDAI
    137: 2 * 10**18,  # Polygon: 2 POL
    8453: 10**16,  # Base: 0.01 ETH
    42161: 10**16,  # Arbitrum: 0.01 ETH
    43114: 5 * 10**17,  # Avalanche: 0.5 AVAX
}
DEFAULT_GAS_FUND_WEI = 10**16  # a chain not in the table: 0.01 coin


# Dust floor per chain id, in wei of the native coin. A direct link (``linked``) is a lead
# only when the linking native or internal transfer moved at least this much; below the
# floor it is context (``linked_dust``), never scored. The floor is the price of a planted
# link, not a barrier: an outgoing transfer above it still needs only the depositor's key,
# so whoever controls the depositor can create one to any address for about the floor plus
# the gas of a plain transfer (e.g. about 0.001 ETH plus 21,000 gas on Ethereum). It stops
# free 0-value and 1-wei links only. The values are roughly equal in value, about
# 0.001 ETH each, from CoinGecko USD prices of 2026-11-14 (ETH 1,845; BNB 598; POL 0.081;
# xDAI 1.00; AVAX 7.69), rounded; prices move and the floors do not follow them (a
# judgement call, not measured). Listed tokens have floors of the same value
# (``data/real_tokens.csv``: 2 USD of a stablecoin, 0.001 WETH, ...).
DUST_FLOOR_WEI = {
    1: 10**15,  # Ethereum: 0.001 ETH
    10: 10**15,  # Optimism: 0.001 ETH
    56: 3 * 10**15,  # BSC: 0.003 BNB
    100: 2 * 10**18,  # Gnosis: 2 xDAI
    137: 20 * 10**18,  # Polygon: 20 POL
    8453: 10**15,  # Base: 0.001 ETH
    42161: 10**15,  # Arbitrum: 0.001 ETH
    43114: 25 * 10**16,  # Avalanche: 0.25 AVAX
}
DEFAULT_DUST_FLOOR_WEI = 10**15  # a chain not in the table: 0.001 of its coin

# Real tokens per chain (``data/real_tokens.csv``: address, symbol, decimals, the dust
# floor in whole tokens and a source URL from the issuer's docs or the verified explorer
# token page). A token transfer makes a lead-eligible direct link only for a listed token
# and an amount at or above its floor; any other token can be deployed by anyone and its
# Transfer events say whatever its code says (``linked_unverified_token``, context).
REAL_TOKENS_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "real_tokens.csv"
)


def _load_real_tokens() -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    with open(REAL_TOKENS_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            decimals = int(row["decimals"])
            # min_units is a decimal string ("0.00002"); scaled without float rounding
            whole, _, frac = row["min_units"].strip().partition(".")
            frac = (frac + "0" * decimals)[:decimals]
            min_raw = int(whole or "0") * 10**decimals + int(frac or "0")
            out.setdefault(int(row["chain_id"]), {})[row["address"].strip().lower()] = {
                "symbol": row["symbol"].strip(),
                "decimals": decimals,
                "min_raw": min_raw,
                "source_url": row["source_url"].strip(),
            }
    return out


_REAL_TOKENS: dict[int, dict[str, dict]] | None = None


def sender_floors(chain_id: int) -> dict[str, int]:
    """See :attr:`Network.sender_floors`."""
    floors = {token: info["min_raw"] for token, info in real_tokens(chain_id).items()}
    floors["eth"] = DUST_FLOOR_WEI.get(int(chain_id), DEFAULT_DUST_FLOOR_WEI)
    return floors


def real_tokens(chain_id: int) -> dict[str, dict]:
    """``{token address: {symbol, decimals, min_raw, source_url}}`` of the listed real
    tokens of a chain (see :data:`REAL_TOKENS_CSV`); empty for a chain with none."""
    global _REAL_TOKENS
    if _REAL_TOKENS is None:
        _REAL_TOKENS = _load_real_tokens()
    return _REAL_TOKENS.get(int(chain_id), {})


# How a report names a chain (the registry keys are lower-case identifiers).
DISPLAY_NAMES = {
    "ethereum": "Ethereum",
    "bsc": "BSC",
    "polygon": "Polygon",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "gnosis": "Gnosis",
    "avalanche": "Avalanche",
    "base": "Base",
    "zksync": "ZKsync",
}


def display_name(name: str) -> str:
    """The chain name for a reader: ``bsc`` -> ``BSC``; an unknown CSV chain as given."""
    return DISPLAY_NAMES.get((name or "").lower(), name)


class Network:
    """A chain plus the Tornado pool contracts deployed on it."""

    def __init__(
        self,
        name: str,
        chain_id: int | str,
        currency: str,
        pool_list: list[Pool],
        routers: set[str] | list[str] | None = None,
        api_base: str = "",
        api_style: str = "v2",
        rpc_url: str = "",
        explorer_addr: str = "",
        explorer_tx: str = "",
    ) -> None:
        self.name = name
        self.chain_id = int(chain_id)
        self.currency = currency
        self.pools = assign_keys(list(pool_list))
        self.by_key = {p.key: p for p in self.pools}
        self.by_address = {p.address: p for p in self.pools}
        self.routers = {r.lower() for r in (routers or [])}
        self.rpc_url = rpc_url

        default_addr, default_tx = EXPLORERS.get(self.chain_id, _FALLBACK_EXPLORER)
        self.explorer_addr = explorer_addr or default_addr
        self.explorer_tx = explorer_tx or default_tx

        # Data provider. Empty api_base means Etherscan V2 (style "v2").
        # A non-empty api_base with style "compat" routes to an Etherscan-
        # compatible explorer (Blockscout, Routescan), which needs no paid key.
        self.api_base = api_base
        self.api_style = api_style if api_base else "v2"

    @property
    def display_name(self) -> str:
        """The chain name for a reader (see :func:`display_name`)."""
        return display_name(self.name)

    @property
    def assets(self) -> list[str]:
        """Sorted unique asset tickers, native currency first."""
        seen = []
        for pool in self.pools:
            if pool.asset not in seen:
                seen.append(pool.asset)
        native = [a for a in seen if a == self.currency]
        others = sorted(a for a in seen if a != self.currency)
        return native + others

    @property
    def gas_fund_wei(self) -> int:
        """Gas-funder threshold of this chain (see :data:`GAS_FUND_WEI`)."""
        return GAS_FUND_WEI.get(self.chain_id, DEFAULT_GAS_FUND_WEI)

    @property
    def dust_floor_wei(self) -> int:
        """Smallest native value of a lead-eligible direct link (see :data:`DUST_FLOOR_WEI`)."""
        return DUST_FLOOR_WEI.get(self.chain_id, DEFAULT_DUST_FLOOR_WEI)

    @property
    def real_tokens(self) -> dict[str, dict]:
        """Listed real tokens of this chain (:func:`real_tokens`)."""
        return real_tokens(self.chain_id)

    @property
    def sender_floors(self) -> dict[str, int]:
        """Smallest raw amount per asset that makes a sender of a deposit address
        (:func:`tornado_demix.deposit_addresses.swept_senders`): ``"eth"`` (the native
        coin, whatever its name) at :attr:`dust_floor_wei`, each listed real token at its
        ``min_raw``. An unlisted token has no floor here."""
        return sender_floors(self.chain_id)

    def pools_for_asset(self, asset: str) -> list[Pool]:
        """Every pool taking ``asset``, ordered by denomination."""
        return sorted((p for p in self.pools if p.asset == asset), key=lambda p: p.denom)

    @property
    def entry_points(self) -> set[str]:
        """Addresses a deposit may be sent to (pools + routers/proxies)."""
        return set(self.by_address) | self.routers

    def addr_url(self, address: str) -> str:
        """Block-explorer URL for an address on this chain."""
        return self.explorer_addr + str(address).lower()

    def tx_url(self, tx_hash: str) -> str:
        """Block-explorer URL for a transaction on this chain."""
        return self.explorer_tx + str(tx_hash)

    def client_kwargs(self) -> dict:
        """Keyword args for EtherscanClient to reach this network's provider."""
        return {
            "chain_id": self.chain_id,
            "base_url": self.api_base or ETHERSCAN_API_URL,
            "style": self.api_style,
            "rpc_url": self.rpc_url,
            "fallback_url": KEYLESS_FALLBACK.get(self.chain_id, "") if not self.api_base else "",
        }

    def __repr__(self):
        return "Network({}, chain_id={}, {} pools, {})".format(
            self.name, self.chain_id, len(self.pools), self.currency
        )


def _builtin_ethereum():
    """Ethereum mainnet - the verified, built-in default (native pools only)."""
    return Network(
        "ethereum",
        CHAIN_ID,
        "ETH",
        [Pool(addr, denom, "ETH", 18, None) for denom, addr in sorted(POOLS.items())],
        ROUTERS,
    )


def _builtins():
    return {"ethereum": _builtin_ethereum()}


ETHEREUM = _builtin_ethereum()


# Denomination and decimals are range-checked because ``float()`` accepts "nan":
# `abs(value - nan) > nan * 0.005` is False for every value, so every transfer to
# that pool would count as a deposit. No ERC-20 exceeds 18 decimals; 36 is a
# generous bound that still converts to a float.
MAX_DECIMALS = 36


def _row_pool(row, currency, on_error=None):
    """Build a Pool from one CSV row, or None if the row is unusable.

    ``on_error`` is called with a short reason for each rejected row so the
    caller can report it rather than silently dropping a pool.
    """

    def reject(reason):
        if on_error is not None:
            on_error(reason)
        return None

    addr = (row.get("pool_address") or "").strip().lower()
    denom_raw = (row.get("denomination") or "").strip()
    if not addr or not denom_raw:
        return None  # a blank row is not an error
    if not config.is_address(addr):
        return reject("pool_address {!r} is not a 0x address".format(addr))
    try:
        denom = float(denom_raw)
    except ValueError:
        return reject("denomination {!r} is not a number".format(denom_raw))
    if denom != denom or denom in (float("inf"), float("-inf")):
        return reject("denomination {!r} is not finite".format(denom_raw))
    if denom <= 0:
        return reject("denomination {!r} is not positive".format(denom_raw))

    asset = (row.get("asset") or "").strip() or currency
    decimals_raw = (row.get("decimals") or "").strip()
    if decimals_raw:
        try:
            decimals = int(decimals_raw)
        except ValueError:
            return reject("decimals {!r} is not an integer".format(decimals_raw))
        if not 0 <= decimals <= MAX_DECIMALS:
            return reject("decimals {!r} is outside 0..{}".format(decimals_raw, MAX_DECIMALS))
    else:
        decimals = 18

    token = (row.get("token_address") or "").strip().lower() or None
    if token and not config.is_address(token):
        return reject("token_address {!r} is not a 0x address".format(token))
    return Pool(addr, denom, asset, decimals, token)


def _row_router(row):
    """Return the row's router address lowercased, or None if absent/invalid."""
    router = (row.get("router_address") or "").strip().lower()
    if config.is_address(router):
        return router
    return None


def load_networks(csv_path: str | None = None) -> dict[str, Network]:
    """Return {name: Network}, built-ins merged with any CSV-defined chains.

    Rows for a network already built in extend its pool table, so a user can
    add a denomination or an ERC-20 pool without touching the code. A row whose
    pool address already exists replaces that pool.
    """
    networks = _builtins()

    resolved = config.resolve(
        csv_path or DEFAULT_NETWORKS_CSV, allow_example=True, allow_packaged=True
    )
    if not resolved or not os.path.exists(resolved):
        return networks

    collected = {}
    # utf-8-sig: a file saved by Excel starts with a BOM, which would otherwise
    # rename the first column and make every row look nameless.
    rejected = []
    with open(resolved, newline="", encoding="utf-8-sig") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            name = (row.get("network") or "").strip().lower()
            if not name or name.startswith("#"):
                continue
            currency = (row.get("currency") or "ETH").strip()
            chain_raw = (row.get("chain_id") or "1").strip()
            try:
                int(chain_raw)
            except ValueError:
                rejected.append(
                    "{}:{} {}: chain_id {!r} is not an integer".format(
                        resolved, lineno, name, chain_raw
                    )
                )
                continue
            reasons = []
            pool = _row_pool(row, currency, on_error=reasons.append)
            if pool is None:
                for reason in reasons:
                    rejected.append("{}:{} {}: {}".format(resolved, lineno, name, reason))
                continue
            entry = collected.setdefault(
                name,
                {
                    "chain_id": chain_raw,
                    "currency": currency,
                    "api_base": "",
                    "api_style": "",
                    "rpc_url": "",
                    "explorer_addr": "",
                    "explorer_tx": "",
                    "pools": [],
                    "routers": set(),
                },
            )
            for field in ("api_base", "api_style", "rpc_url", "explorer_addr", "explorer_tx"):
                value = (row.get(field) or "").strip()
                if value:
                    entry[field] = value.lower() if field == "api_style" else value
            router = _row_router(row)
            if router:
                entry["routers"].add(router)
            duplicate = next((p for p in entry["pools"] if p.address == pool.address), None)
            if duplicate is not None:
                # Two rows for one contract cannot both be right, and by_address
                # would silently keep only one of them.
                rejected.append(
                    "{}:{} {}: pool_address {} already declared at "
                    "denomination {:g} {}; keeping the first".format(
                        resolved, lineno, name, pool.address, duplicate.denom, duplicate.asset
                    )
                )
                continue
            entry["pools"].append(pool)

    if rejected:
        _log("[!] {} unusable row(s) in the pool registry - NOT loaded:".format(len(rejected)))
        for reason in rejected:
            _log("    {}".format(reason))

    for name, entry in collected.items():
        style = entry["api_style"] or ("compat" if entry["api_base"] else "v2")
        base = networks.get(name)
        if base is not None:
            merged = {p.address: p for p in base.pools}
            merged.update({p.address: p for p in entry["pools"]})
            networks[name] = Network(
                name,
                base.chain_id,
                base.currency,
                list(merged.values()),
                set(base.routers) | entry["routers"],
                api_base=entry["api_base"] or base.api_base,
                api_style=entry["api_style"] or base.api_style,
                rpc_url=entry["rpc_url"] or base.rpc_url,
                explorer_addr=entry["explorer_addr"] or "",
                explorer_tx=entry["explorer_tx"] or "",
            )
        else:
            networks[name] = Network(
                name,
                entry["chain_id"],
                entry["currency"],
                entry["pools"],
                entry["routers"],
                api_base=entry["api_base"],
                api_style=style,
                rpc_url=entry["rpc_url"],
                explorer_addr=entry["explorer_addr"],
                explorer_tx=entry["explorer_tx"],
            )
    return networks


def get_network(name: str | None, csv_path: str | None = None) -> Network:
    """Look up a network by name or chain id, with a helpful error listing what exists."""
    name = (name or "ethereum").strip().lower()
    networks = load_networks(csv_path)
    if name.isdecimal() and name not in networks:
        for network in networks.values():
            if network.chain_id == int(name):
                return network
    if name not in networks:
        raise RegistryError(
            "Unknown network '{}'. Available: {}. Add more in {} "
            "(network,chain_id,currency,asset,denomination,pool_address,"
            "decimals,token_address,...,router_address).".format(
                name, ", ".join(sorted(networks)), csv_path or DEFAULT_NETWORKS_CSV
            )
        )
    return networks[name]
