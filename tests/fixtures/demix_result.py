"""Hand-built demix results, for testing consumers without any network."""

from collections import Counter, defaultdict

from tornado_demix.networks import Network
from tornado_demix.pools import Pool

POOL_1_ETH = "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936"
POOL_01_ETH = "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc"

WALLET = "0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2"
ALICE = "0xaaaa000000000000000000000000000000000001"
BOB = "0xbbbb000000000000000000000000000000000002"


def network():
    """A two-pool Ethereum stand-in."""
    return Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(POOL_01_ETH, 0.1, "ETH", 18, None),
            Pool(POOL_1_ETH, 1.0, "ETH", 18, None),
        ],
    )


def _background(n, prefix):
    """``n`` unrelated recipients with one relayed withdrawal each, so the
    recipient field is large enough for a count match to count."""
    return {
        "0x%s%037x" % (prefix[2:], i): [
            {
                "value": 0.98,
                "ts": 1500 + i,
                "hash": "%s%d" % (prefix, i),
                "relayer": "0xr",
                "fee": 0.02,
                "asset": "ETH",
                "self_relayed": False,
                "gas_price": 23000000000,
            }
        ]
        for i in range(n)
    }


def _res(pool, records_by_addr, target_counts):
    counts = Counter({a: len(r) for a, r in records_by_addr.items()})
    detail = defaultdict(list)
    detail.update(records_by_addr)
    return {
        "pool_key": pool.key,
        "denom": pool.denom,
        "asset": pool.asset,
        "decimals": pool.decimals,
        "pool": pool.address,
        "mode": "events",
        "window_blocks": [1, 2],
        "window_ts": [1000, 2000],
        "total_qualifying_withdrawals": sum(counts.values()),
        "unique_recipients": len(counts),
        "counts": counts,
        "detail": detail,
        "withdrawals": [],
        "target_counts": target_counts,
        "candidates_by_count": {n: [a for a, c in counts.items() if c == n] for n in target_counts},
    }


def two_pool_result():
    """A wallet with a 2-note 1 ETH voucher and a 1-note 0.1 ETH voucher.

    ALICE is count-matched in the 1 ETH pool and also appears once in the
    0.1 ETH pool. Under the v1 address-keyed signal map, ALICE's 1 ETH
    signals leaked into the 0.1 ETH pool's report.
    """
    net = network()
    one = net.by_key["1 ETH"]
    tenth = net.by_key["0.1 ETH"]

    result_one = _res(
        one,
        {
            ALICE: [
                {
                    "value": 0.99,
                    "ts": 1100,
                    "hash": "0xa1",
                    "relayer": "0x0",
                    "fee": 0.01,
                    "asset": "ETH",
                    "self_relayed": True,
                    "gas_price": 41000000000,
                },
                {
                    "value": 0.99,
                    "ts": 1200,
                    "hash": "0xa2",
                    "relayer": "0x0",
                    "fee": 0.01,
                    "asset": "ETH",
                    "self_relayed": True,
                    "gas_price": 41000000000,
                },
            ],
            BOB: [
                {
                    "value": 0.98,
                    "ts": 1300,
                    "hash": "0xb1",
                    "relayer": "0xr",
                    "fee": 0.02,
                    "asset": "ETH",
                    "self_relayed": False,
                    "gas_price": 22000000000,
                }
            ],
            **_background(4, "0xbg"),
        },
        target_counts=[2],
    )

    result_tenth = _res(
        tenth,
        {
            ALICE: [
                {
                    "value": 0.099,
                    "ts": 1400,
                    "hash": "0xa3",
                    "relayer": "0xr",
                    "fee": 0.001,
                    "asset": "ETH",
                    "self_relayed": False,
                    "gas_price": 19000000000,
                }
            ],
        },
        target_counts=[1],
    )

    return {
        "wallet": WALLET,
        "params": {
            "window_days": 30,
            "mode": "events",
            "network": "ethereum",
            "currency": "ETH",
            "gap_hours": 24,
        },
        "deposits": [
            {
                "pool_key": "1 ETH",
                "denom": 1.0,
                "asset": "ETH",
                "ts": 1000,
                "block": 10,
                "hash": "0xd1",
                "to": POOL_1_ETH,
                "via": "pool",
                "gas_price": 41000000000,
            },
            {
                "pool_key": "1 ETH",
                "denom": 1.0,
                "asset": "ETH",
                "ts": 1010,
                "block": 11,
                "hash": "0xd2",
                "to": POOL_1_ETH,
                "via": "pool",
                "gas_price": 41000000000,
            },
            {
                "pool_key": "0.1 ETH",
                "denom": 0.1,
                "asset": "ETH",
                "ts": 1020,
                "block": 12,
                "hash": "0xd3",
                "to": POOL_01_ETH,
                "via": "pool",
                "gas_price": 41000000000,
            },
        ],
        "vouchers": [
            {
                "pool_key": "0.1 ETH",
                "denom": 0.1,
                "asset": "ETH",
                "count": 1,
                "first_ts": 1020,
                "last_ts": 1020,
                "first_block": 12,
                "deposits": [],
            },
            {
                "pool_key": "1 ETH",
                "denom": 1.0,
                "asset": "ETH",
                "count": 2,
                "first_ts": 1000,
                "last_ts": 1010,
                "first_block": 10,
                "deposits": [],
            },
        ],
        "denoms": {"1 ETH": result_one, "0.1 ETH": result_tenth},
    }
