"""The methodology's bridge table names every bridge and every end state of the code."""

import csv
import os

import pytest

from tornado_demix import bridges, bridges_canonical, bridges_cctp, bridges_named, swaps_cow, trace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# display names in the table for the protocol keys of data/bridges.csv
PROTOCOL_TEXT = {
    "across": "Across",
    "hop": "Hop",
    "synapse": "Synapse",
    "stargate": "Stargate",
    "multichain": "Multichain",
    "wormhole": "Wormhole",
    "celer": "Celer",
    "debridge": "deBridge",
    "relay": "Relay",
    "cctp": "Circle CCTP",
    "polygon_pos": "Polygon PoS",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "base": "Base",
    "zksync": "ZKsync",
    "scroll": "Scroll",
    "gnosis_omnibridge": "Gnosis Omnibridge",
    "thorchain": "THORChain",
    "lifi": "LI.FI",
    "socket": "Socket",
    "squid": "Squid",
    "rango": "Rango",
    "linea": "Linea",
    "starknet": "Starknet",
    "polygon_zkevm": "Polygon zkEVM",
    "blast": "Blast",
    "mantle": "Mantle",
    "connext": "Connext",
    "hyphen": "Hyphen",
    "mayan": "Mayan",
    "maya": "Maya",
    "layerswap": "Layerswap",
    "near-intents": "NEAR Intents",
    "unit": "Unit",
    "hyperliquid": "Hyperliquid",
}

STATES = {
    bridges.FILLED,
    bridges.NOT_FOLLOWED,
    bridges.NOT_DECODED,
    bridges.NOT_TIED,
    bridges.FILL_NOT_FOUND,
    bridges.DEST_CHAIN_NOT_SUPPORTED,
    bridges.DEST_POOL_NOT_LISTED,
    bridges.LOOKUP_FAILED,
    bridges.DEPOSIT_EXPIRED,
    bridges.BRIDGE_OR_SWAP_UNKNOWN,
    bridges_canonical.CREDITED,
    bridges_canonical.L2_CREDIT_NOT_FOUND,
    bridges_cctp.MINTED,
    bridges_cctp.MINT_NOT_FOUND,
    bridges_cctp.MINTED_OFF_EVM,
    bridges_named.DEST_NOT_ON_CHAIN,
    bridges_named.FULFIL_NOT_FOLLOWED,
    bridges_named.VAULT_PAYOUT,
    bridges_named.OFF_CHAIN_MATCHING,
    bridges_named.CREDITED_OFF_EVM,
    bridges_named.ORDER_NOT_DECODED,
    bridges_named.SAME_CHAIN,
    bridges_named.PAYOUT_NOT_FOUND,
    swaps_cow.SETTLED,
    swaps_cow.NOT_TIED,
    swaps_cow.PAYOUT_NOT_FOUND,
    "below_minimum",
}
# end reasons of trace.py that name a bridge state in words
REASONS = {
    trace.NOT_TIED,
    trace.SWAPPED_UNREAD,
    trace.HANDLER_NOT_TIED,
    trace.HANDLER_REST,
}


@pytest.fixture(scope="module")
def table_text():
    with open(os.path.join(ROOT, "docs", "METHODOLOGY.md"), encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("**Bridge table.**")
    end = text.index("Selectors and topics are re-derived", start)
    return text[start:end]


def test_every_end_state_of_the_code_is_in_the_bridge_table(table_text):
    missing = sorted(s for s in STATES if f"`{s}`" not in table_text)
    assert not missing, missing


def test_every_end_reason_of_the_trace_is_in_the_bridge_table(table_text):
    missing = sorted(r for r in REASONS if r not in table_text)
    assert not missing, missing


def test_every_protocol_of_the_table_data_is_in_the_bridge_table(table_text):
    with open(bridges.BRIDGES_CSV, newline="", encoding="utf-8") as fh:
        protocols = {r["protocol"] for r in csv.DictReader(fh)}
    assert protocols <= set(PROTOCOL_TEXT), sorted(protocols - set(PROTOCOL_TEXT))
    missing = sorted(p for p in protocols if PROTOCOL_TEXT[p] not in table_text)
    assert not missing, missing
