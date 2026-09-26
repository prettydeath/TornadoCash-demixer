"""Ethereum-mainnet on-chain constants and event signatures.

All addresses are stored lowercased so comparisons are case-insensitive.

Everything here that names a contract is Ethereum mainnet only; per-chain data
lives on :class:`~tornado_demix.networks.Network`. ``POOLS`` and ``ROUTERS`` feed
only ``networks._builtin_ethereum``.
"""

# Etherscan V2 unified API endpoint (chainid selects the network).
ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1  # Ethereum mainnet

WEI = 10**18

# Tornado.Cash ETH pools on mainnet: denomination (in ETH) -> contract address.
POOLS = {
    0.1: "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc",
    1.0: "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",
    10.0: "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",
    100.0: "0xa160cdab225685da1d56aa342ad8841c3b53f291",
}

# Mainnet deposit proxies. A deposit may go straight to a pool or be routed
# through one of these; in both cases the tx value equals the denomination.
# Other chains declare theirs in the registry's router_address column.
ROUTERS = {
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",  # Tornado.Cash: Router
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",  # Tornado.Cash: Proxy (legacy)
    "0x905b63fff465b9ffbf41dea908ceb12478ec7601",  # Tornado.Cash: Proxy
}

# keccak256 of the pool events (ABIs in tornado_demix.events).
TOPIC_DEPOSIT = "0xa945e51eec50ab98c161376f0db4cf2aeba3ec92755fe2fcd388bdbbb80ff196"
TOPIC_WITHDRAWAL = "0xe9e508bad6d4c3227e881ca19068f099da81b5164dd6d62b2eaf1e8bc6c34931"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
