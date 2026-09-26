"""Deposit detection, voucher clustering, pool-keyed signals and leads."""

from tests.fixtures import demix_result as fx
from tornado_demix.constants import TOPIC_WITHDRAWAL, ZERO_ADDRESS
from tornado_demix.demix import cluster_vouchers, detect_deposits, run_demix
from tornado_demix.heuristics import apply_heuristics, cross_method
from tornado_demix.networks import Network
from tornado_demix.pools import Pool
from tornado_demix.relayer import self_relayed_candidates

RELAYER = "0x" + "cc" * 20


def _tx(to, value_eth, ts, block, tx_hash, gas_price=1, is_error="0"):
    return {
        "from": fx.WALLET,
        "to": to,
        "value": str(int(value_eth * 10**18)),
        "timeStamp": str(ts),
        "blockNumber": str(block),
        "hash": tx_hash,
        "gasPrice": str(gas_price),
        "isError": is_error,
    }


def _wlog(to, nullifier, relayer, fee_wei, ts=1100, block=100, gas_price=1):
    """Build a raw Withdrawal log the way the logs endpoint returns them.

    ``nullifier`` is a 64-hex string; ``relayer`` == ZERO_ADDRESS (or a zero
    fee) marks the withdrawal self-relayed.
    """
    data = ("0" * 24 + to[2:]) + nullifier + format(fee_wei, "064x")
    return {
        "data": "0x" + data,
        "topics": [TOPIC_WITHDRAWAL, "0x" + "0" * 24 + relayer[2:]],
        "gasPrice": hex(gas_price),
        "transactionHash": "0x" + nullifier,
        "blockNumber": hex(block),
        "timeStamp": hex(ts),
    }


class _FakeClient:
    """A client that replays a fixed tx list and a fixed set of Withdrawal logs."""

    def __init__(self, txs, logs):
        self._txs = txs
        self._logs = logs

    def internal_txs(self, addr):

        return []

    def outgoing_txs(self, addr):
        return self._txs

    def block_by_time(self, ts, closest="before"):
        return int(ts)

    def get_logs(self, address, topic0, start_block, end_block):
        return self._logs


def _recipient(i):
    return "0x" + format(i, "040x")


def _headline_single_note_client():
    """The audit's headline case: one 0.1 ETH deposit (count 1); 40 recipients
    each hit exactly once; the first 20 self-relayed.

    Pre-fix this makes candidates_by_count[1] == all 40 recipients, so 20
    self-relayed leads fall out of a voucher that cannot discriminate at all.
    """
    deposit = _tx(fx.POOL_01_ETH, 0.1, 1000, 10, "0xdep")
    logs = []
    for i in range(1, 41):
        self_relayed = i <= 20
        relayer = ZERO_ADDRESS if self_relayed else RELAYER
        fee_wei = 0 if self_relayed else 10**15
        logs.append(_wlog(_recipient(i), format(i, "064x"), relayer, fee_wei))
    return _FakeClient([deposit], logs)


def test_single_note_candidates_by_count_is_gated_at_source():
    """A count every recipient shares (count 1 on a single-note voucher)
    contributes no candidates: the vacuous count is emptied at the source."""
    data = run_demix(_headline_single_note_client(), fx.WALLET, network=fx.network())
    res = data["denoms"]["0.1 ETH"]
    assert res["target_counts"] == [1]
    # The vacuous count is present but empties to no candidates.
    assert all(addrs == [] for addrs in res["candidates_by_count"].values())


def test_single_note_yields_no_self_relayed_leads():
    """The 20 self-relayed withdrawals must not surface as leads: they are only
    "candidates" by virtue of a count that discriminates nobody."""
    data = run_demix(_headline_single_note_client(), fx.WALLET, network=fx.network())
    assert self_relayed_candidates(data) == []


def test_discriminating_count_still_populates_and_leads():
    """Positive control: one recipient at count 3 among 40 (a 3-note voucher)
    is a genuinely discriminating count, so it still populates and, being
    self-relayed, still yields a lead."""
    deposits = [_tx(fx.POOL_01_ETH, 0.1, 1000 + i * 10, 10 + i, "0xdep%d" % i) for i in range(3)]
    winner = _recipient(1)
    logs = []
    # winner: 3 self-relayed withdrawals -> count 3
    for k in range(3):
        logs.append(_wlog(winner, format(9000 + k, "064x"), ZERO_ADDRESS, 0))
    # 39 other recipients hit once, relayed (not self)
    for i in range(2, 41):
        logs.append(_wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15))
    data = run_demix(_FakeClient(deposits, logs), fx.WALLET, network=fx.network())
    res = data["denoms"]["0.1 ETH"]
    assert res["target_counts"] == [3]
    assert res["candidates_by_count"][3] == [winner]
    leads = self_relayed_candidates(data)
    assert {lead["to"] for lead in leads} == {winner}


def test_detect_deposits_tags_the_pool_key():
    net = fx.network()
    txs = [
        _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xd1"),
        _tx(fx.POOL_01_ETH, 0.1, 1020, 12, "0xd3"),
    ]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=txs)
    assert [d["pool_key"] for d in deposits] == ["1 ETH", "0.1 ETH"]
    assert [d["asset"] for d in deposits] == ["ETH", "ETH"]


def test_deposit_to_the_wrong_pool_for_its_value_is_ignored():
    """1 ETH sent to the 0.1 ETH pool is not a 0.1 ETH deposit."""
    net = fx.network()
    txs = [_tx(fx.POOL_01_ETH, 1.0, 1000, 10, "0xd1")]
    assert detect_deposits(None, fx.WALLET, network=net, txs=txs) == []


def test_two_pools_at_one_denomination_stay_distinct():
    """Avalanche runs two live 10 AVAX pools, and both must be reachable.

    Inferring the pool from the transferred value alone always resolves 10 AVAX
    to whichever contract is scanned first, so a deposit sent straight to the
    other one is dropped. That is the exact case the pool model exists for.
    """
    pool_a = "0x1111000000000000000000000000000000000010"
    pool_b = "0x2222000000000000000000000000000000000010"
    net = Network(
        "avalanche", 43114, "AVAX", [Pool(pool_a, 10.0, "AVAX"), Pool(pool_b, 10.0, "AVAX")]
    )
    assert net.by_address[pool_a].key == "10 AVAX"
    assert net.by_address[pool_b].key == "10 AVAX#2"

    txs = [_tx(pool_b, 10.0, 1000, 10, "0xd1"), _tx(pool_a, 10.0, 1010, 11, "0xd2")]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=txs)
    assert [d["pool_key"] for d in deposits] == ["10 AVAX#2", "10 AVAX"]


def test_failed_and_incoming_transactions_are_ignored():
    net = fx.network()
    txs = [
        _tx(fx.POOL_1_ETH, 1.0, 1000, 10, "0xbad", is_error="1"),
        {
            "from": fx.ALICE,
            "to": fx.POOL_1_ETH,
            "value": str(10**18),
            "timeStamp": "1001",
            "blockNumber": "11",
            "hash": "0xin",
            "gasPrice": "1",
            "isError": "0",
        },
    ]
    assert detect_deposits(None, fx.WALLET, network=net, txs=txs) == []


def test_cluster_vouchers_groups_by_pool_key_and_gap():
    deposits = [
        {"pool_key": "1 ETH", "denom": 1.0, "asset": "ETH", "ts": 0, "block": 1},
        {"pool_key": "1 ETH", "denom": 1.0, "asset": "ETH", "ts": 3600, "block": 2},
        {"pool_key": "1 ETH", "denom": 1.0, "asset": "ETH", "ts": 3600 + 25 * 3600, "block": 3},
        {"pool_key": "0.1 ETH", "denom": 0.1, "asset": "ETH", "ts": 10, "block": 4},
    ]
    vouchers = cluster_vouchers(deposits, gap_hours=24)
    sizes = {(v["pool_key"], v["first_ts"]): v["count"] for v in vouchers}
    assert sizes == {("1 ETH", 0): 2, ("1 ETH", 3600 + 25 * 3600): 1, ("0.1 ETH", 10): 1}


def test_signals_do_not_leak_between_pools():
    """Regression: a signal earned in one pool must not appear in another.

    ALICE is self-relayed and count-matched in the 1 ETH pool, and is a plain
    single recipient in the 0.1 ETH pool. Keying signals by address alone
    reported self_relayed against her 0.1 ETH row too, inflating confidence
    and manufacturing a cross-method match.
    """
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())

    one = data["denoms"]["1 ETH"]
    tenth = data["denoms"]["0.1 ETH"]

    assert "self_relayed" in one["signals"][fx.ALICE]
    assert "count_match" in one["signals"][fx.ALICE]
    assert "self_relayed" not in tenth["signals"][fx.ALICE]
    assert tenth["confidence"][fx.ALICE] < one["confidence"][fx.ALICE]


def test_cross_method_does_not_invent_matches_across_pools():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties=set())
    matched = {(r["address"], r["denom"]) for r in cross_method(data)}
    assert (fx.ALICE, 0.1) not in matched


def test_self_relayed_leads_name_the_pool_without_overloading_denom():
    """A lead carries pool_key, and ``denom`` stays the float it always was."""
    data = fx.two_pool_result()
    data["denoms"]["1 ETH"]["withdrawals"] = [
        {"to": fx.ALICE, "self_relayed": True, "tx_hash": "0xa1", "ts": 1100},
        {"to": fx.BOB, "self_relayed": True, "tx_hash": "0xb1", "ts": 1300},
    ]

    leads = self_relayed_candidates(data)

    # BOB is self-relayed here but hit once against a 2-note voucher, so he is
    # not a count-matched candidate and is not a lead.
    assert [lead["to"] for lead in leads] == [fx.ALICE]
    assert leads[0]["pool_key"] == "1 ETH"
    assert leads[0]["denom"] == 1.0
    assert leads[0]["asset"] == "ETH"


def test_linked_signal_is_still_applied_per_pool():
    data = fx.two_pool_result()
    apply_heuristics(data, counterparties={fx.BOB})
    assert "linked" in data["denoms"]["1 ETH"]["signals"][fx.BOB]
    assert "linked" not in data["denoms"]["1 ETH"]["signals"][fx.ALICE]


DAI_TOKEN = "0x6b175474e89094c44da98b954eedeac495271d0f"
USDC_TOKEN = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI_POOL = "0xd4b88df4d29f5cedd6857912842cff3b20c8cfa3"
USDC_POOL = "0xd96f2b1c14db8458374d9aca76e26c3d18364307"


def _token_network():
    """Ethereum with one native pool and two same-denomination token pools."""
    from tornado_demix.networks import Network
    from tornado_demix.pools import Pool

    return Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(fx.POOL_1_ETH, 1.0, "ETH", 18, None),
            Pool(DAI_POOL, 100, "DAI", 18, DAI_TOKEN),
            Pool(USDC_POOL, 100, "USDC", 6, USDC_TOKEN),
        ],
    )


def _ttx(to, raw, ts, tx_hash, contract, sender=fx.WALLET):
    return {
        "from": sender,
        "to": to,
        "value": str(raw),
        "timeStamp": str(ts),
        "blockNumber": "10",
        "hash": tx_hash,
        "contractAddress": contract,
        "tokenDecimal": "18",
        "tokenSymbol": "DAI",
        "gasPrice": "7",
    }


def test_token_deposit_is_detected_with_its_own_asset():
    net = _token_network()
    token_txs = [_ttx(DAI_POOL, 100 * 10**18, 1000, "0xt1", DAI_TOKEN)]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs)
    assert len(deposits) == 1
    assert deposits[0]["pool_key"] == "100 DAI"
    assert deposits[0]["asset"] == "DAI"
    assert deposits[0]["via"] == "token"
    assert deposits[0]["gas_price"] == 7


def test_same_denomination_different_token_do_not_collide():
    """100 DAI and 100 USDC are different pools at the same number."""
    net = _token_network()
    token_txs = [
        _ttx(DAI_POOL, 100 * 10**18, 1000, "0xt1", DAI_TOKEN),
        _ttx(USDC_POOL, 100 * 10**6, 1100, "0xt2", USDC_TOKEN),
    ]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs)
    assert sorted(d["pool_key"] for d in deposits) == ["100 DAI", "100 USDC"]


def test_token_amount_must_match_exactly():
    """No tolerance band: token amounts are exact integers."""
    net = _token_network()
    off_by_one = 100 * 10**18 - 1
    token_txs = [_ttx(DAI_POOL, off_by_one, 1000, "0xt1", DAI_TOKEN)]
    assert detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs) == []


def test_wrong_token_to_a_pool_is_not_a_deposit():
    """A USDC transfer to the DAI pool is not a 100 DAI deposit."""
    net = _token_network()
    token_txs = [_ttx(DAI_POOL, 100 * 10**6, 1000, "0xt1", USDC_TOKEN)]
    assert detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs) == []


def test_incoming_token_transfer_is_not_a_deposit():
    net = _token_network()
    token_txs = [_ttx(fx.WALLET, 100 * 10**18, 1000, "0xt1", DAI_TOKEN, sender=DAI_POOL)]
    assert detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs) == []


ROUTER = "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b"


def _token_network_router():
    """Like _token_network, but with a Tornado router registered."""
    from tornado_demix.networks import Network
    from tornado_demix.pools import Pool

    return Network(
        "ethereum",
        1,
        "ETH",
        [
            Pool(fx.POOL_1_ETH, 1.0, "ETH", 18, None),
            Pool(DAI_POOL, 100, "DAI", 18, DAI_TOKEN),
            Pool(USDC_POOL, 100, "USDC", 6, USDC_TOKEN),
        ],
        routers=[ROUTER],
    )


def test_token_deposit_via_router_is_detected():
    """An ERC-20 deposit routed through the Tornado router lists the router as
    ``to`` in tokentx, not the pool. It must still be detected, with the pool
    inferred from (token contract, exact amount)."""
    net = _token_network_router()
    token_txs = [_ttx(ROUTER, 100 * 10**18, 1000, "0xt1", DAI_TOKEN)]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs)
    assert len(deposits) == 1
    assert deposits[0]["pool_key"] == "100 DAI"
    assert deposits[0]["asset"] == "DAI"
    assert deposits[0]["via"] == "token-router"


def test_token_via_router_disambiguates_by_token_contract():
    """At a shared denomination the token contract still picks the right pool
    even when both deposits arrive through the router."""
    net = _token_network_router()
    token_txs = [
        _ttx(ROUTER, 100 * 10**18, 1000, "0xt1", DAI_TOKEN),
        _ttx(ROUTER, 100 * 10**6, 1100, "0xt2", USDC_TOKEN),
    ]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs)
    assert sorted(d["pool_key"] for d in deposits) == ["100 DAI", "100 USDC"]


def test_token_via_router_wrong_amount_is_ignored():
    """A router transfer whose amount is no pool's denomination is not a deposit."""
    net = _token_network_router()
    token_txs = [_ttx(ROUTER, 100 * 10**18 - 1, 1000, "0xt1", DAI_TOKEN)]
    assert detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=token_txs) == []


def test_native_and_token_deposits_come_back_together_in_time_order():
    net = _token_network()
    txs = [_tx(fx.POOL_1_ETH, 1.0, 2000, 20, "0xn1")]
    token_txs = [_ttx(DAI_POOL, 100 * 10**18, 1000, "0xt1", DAI_TOKEN)]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=txs, token_txs=token_txs)
    assert [d["pool_key"] for d in deposits] == ["100 DAI", "1 ETH"]


def test_no_token_query_when_the_network_has_no_token_pools():
    """A native-only chain must not pay for a tokentx call."""
    calls = []

    class Spy:
        def internal_txs(self, addr):
            return []

        def outgoing_txs(self, addr):
            calls.append("txlist")
            return []

        def token_transfers(self, addr, contract=None):
            calls.append("tokentx")
            return []

    detect_deposits(Spy(), fx.WALLET, network=fx.network())
    assert calls == ["txlist"]


def test_malformed_token_rows_are_skipped_not_fatal():
    """tokentx comes from an external API; a bad row must not abort the scan."""
    net = _token_network()
    good = _ttx(DAI_POOL, 100 * 10**18, 1200, "0xgood", DAI_TOKEN)
    bad_rows = [
        dict(good, value=None, hash="0xnull"),
        dict(good, value="not-a-number", hash="0xnan"),
        dict(good, to=None, hash="0xnoto"),
        dict(good, contractAddress=None, hash="0xnocontract"),
    ]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], token_txs=bad_rows + [good])
    assert [d["hash"] for d in deposits] == ["0xgood"]


def test_withdrawal_fee_uses_the_pools_decimals():
    """A 6-decimal token fee must not be divided by 1e18."""
    from tornado_demix.events import decode_withdrawal
    from tornado_demix.pools import Pool

    log = {
        "data": "0x" + "0" * 24 + "aa" * 20 + "00" * 32 + format(2 * 10**6, "064x"),
        "topics": ["0xtopic", "0x" + "0" * 24 + "bb" * 20],
        "gasPrice": "0x1",
        "transactionHash": "0xw1",
        "blockNumber": "0x1",
        "timeStamp": "0x1",
    }
    w = decode_withdrawal(log)
    assert w["fee_wei"] == 2 * 10**6
    assert "fee_eth" not in w

    usdc = Pool("0x" + "1" * 40, 1000, "USDC", 6, "0x" + "2" * 40)
    assert usdc.to_units(w["fee_wei"]) == 2.0


def test_relayer_stats_report_fees_in_asset_units():
    from tornado_demix.relayer import analyze_relayers

    stats = analyze_relayers(
        [
            {"to": "0xa", "relayer": "0xr", "fee": 2.0, "asset": "USDC", "self_relayed": False},
            {"to": "0xb", "relayer": "0xr", "fee": 4.0, "asset": "USDC", "self_relayed": False},
        ]
    )
    entry = stats["relayers"]["0xr"]
    assert entry["avg_fee"] == 3.0
    assert entry["total_fee"] == 6.0
    assert entry["asset"] == "USDC"


# Contract-wallet deposits
# txlist holds only EOA-signed transactions. A deposit made through a Safe or
# any other smart-contract wallet reaches the pool as an internal transfer, so
# looking only at txlist reported the subject as having no Tornado activity at
# all - and contract wallets are common among the entities this tool is used on.
def _internal(to, raw, ts, tx_hash, sender=None):
    return {
        "from": sender or fx.WALLET,
        "to": to,
        "value": str(raw),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 12),
        "hash": tx_hash,
        "isError": "0",
    }


def test_a_deposit_made_by_a_contract_wallet_is_detected():
    net = fx.network()
    pool = net.by_key["1 ETH"]
    internal = [_internal(pool.address, 10**18, 1_700_000_000, "0xsafe1")]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], internal_txs=internal)
    assert [d["hash"] for d in deposits] == ["0xsafe1"]
    assert deposits[0]["pool_key"] == "1 ETH"


def test_an_internal_deposit_is_labelled_as_such():
    """A reader must be able to tell how the deposit reached the pool."""
    net = fx.network()
    pool = net.by_key["1 ETH"]
    deposits = detect_deposits(
        None,
        fx.WALLET,
        network=net,
        txs=[],
        internal_txs=[_internal(pool.address, 10**18, 1, "0xsafe1")],
    )
    assert deposits[0]["via"] == "pool-internal"


def test_internal_transfers_obey_the_same_filters_as_normal_ones():
    net = fx.network()
    pool = net.by_key["1 ETH"]
    rows = [
        _internal(pool.address, 10**18, 1, "0xgood"),
        dict(_internal(pool.address, 10**18, 2, "0xfailed"), isError="1"),
        _internal(pool.address, 10**18, 3, "0xincoming", sender="0xother"),
        _internal(pool.address, 5 * 10**17, 4, "0xwrongvalue"),
        _internal("0x" + "9" * 40, 10**18, 5, "0xnotapool"),
    ]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=[], internal_txs=rows)
    assert [d["hash"] for d in deposits] == ["0xgood"]


def test_normal_and_internal_deposits_are_merged_in_time_order():
    net = fx.network()
    pool = net.by_key["1 ETH"]
    normal = [
        {
            "from": fx.WALLET,
            "to": pool.address,
            "value": str(10**18),
            "timeStamp": "200",
            "blockNumber": "2",
            "hash": "0xnormal",
            "gasPrice": "1",
            "isError": "0",
        }
    ]
    internal = [_internal(pool.address, 10**18, 100, "0xinternal")]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=normal, internal_txs=internal)
    assert [d["hash"] for d in deposits] == ["0xinternal", "0xnormal"]
    assert [d["via"] for d in deposits] == ["pool-internal", "pool"]


def test_a_none_client_analyses_only_what_it_was_given():
    """The offline contract: nothing is fetched, nothing raises."""
    assert detect_deposits(None, fx.WALLET, network=fx.network()) == []


# Alt-chain native deposits made through a router
# On Polygon and Avalanche essentially every deposit goes through a Tornado
# router, so with no router registered detect_deposits saw ZERO deposits on
# those chains. The routers were re-verified on-chain and now ship in the
# registry; a native deposit whose ``to`` is the router must be detected, with
# the pool inferred from the value.
POLYGON_ROUTER = "0x0d5550d52428e7e3175bfc9550207e4ad3859b17"
AVAX_ROUTER_A = "0x171fb28ebffcb2737e530e1fd48cb4ef12e5031e"
AVAX_ROUTER_B = "0x0d5550d52428e7e3175bfc9550207e4ad3859b17"


def test_shipped_polygon_exposes_its_verified_router():
    from tornado_demix.networks import get_network

    assert POLYGON_ROUTER in get_network("polygon").routers


def test_shipped_avalanche_exposes_both_verified_routers():
    from tornado_demix.networks import get_network

    routers = get_network("avalanche").routers
    assert AVAX_ROUTER_A in routers
    assert AVAX_ROUTER_B in routers


def test_polygon_native_deposit_via_router_is_detected():
    """A 100 MATIC send to the Polygon router is a 100 MATIC deposit."""
    from tornado_demix.networks import get_network

    net = get_network("polygon")
    txs = [_tx(POLYGON_ROUTER, 100.0, 1000, 10, "0xpoly")]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=txs)
    assert [d["pool_key"] for d in deposits] == ["100 MATIC"]
    assert deposits[0]["via"] == "router"
    assert deposits[0]["asset"] == "MATIC"


def test_avalanche_native_deposit_via_router_is_detected():
    """A 500 AVAX send to the Avalanche router is a 500 AVAX deposit."""
    from tornado_demix.networks import get_network

    net = get_network("avalanche")
    txs = [_tx(AVAX_ROUTER_A, 500.0, 1000, 10, "0xavax")]
    deposits = detect_deposits(None, fx.WALLET, network=net, txs=txs)
    assert [d["pool_key"] for d in deposits] == ["500 AVAX"]
    assert deposits[0]["via"] == "router"


# Search-window upper bound is clamped to the chain head
# A voucher's window ends window_days after the last deposit. For a fresh deposit
# that is a timestamp in the FUTURE, and resolving it with closest="after" makes
# the live explorer raise "Block timestamp too far in the future", aborting the
# whole pool. The end must clamp to the current block instead.
class _WindowClient:
    """Records block_by_time calls; serves a fixed current block and no logs."""

    def __init__(self, current=999):
        self.current = current
        self.by_time = []

    def block_by_time(self, ts, closest="before"):
        self.by_time.append((int(ts), closest))
        return int(ts)  # 1 block per second stand-in

    def current_block(self):
        return self.current

    def get_logs(self, address, topic0, start_block, end_block):
        return []


def test_a_future_window_end_clamps_to_the_current_block(monkeypatch):
    from tornado_demix import demix

    now = 1_700_000_000
    monkeypatch.setattr(demix.time, "time", lambda: now)
    pool = fx.network().by_key["1 ETH"]
    # Last deposit essentially now, so the 30-day window ends in the future.
    voucher = {
        "pool_key": "1 ETH",
        "count": 1,
        "first_ts": now - 100,
        "last_ts": now - 100,
        "first_block": 1,
        "deposits": [],
    }
    client = _WindowClient(current=999)

    res = demix.analyze_denom_events(client, pool, [voucher], window_days=30)

    window = res["windows"][0]
    assert window["end_block"] == 999  # clamped to the head
    # The future timestamp was never sent to the "after" lookup that would fail.
    assert all(closest != "after" for _ts, closest in client.by_time)
    assert window["start_block"] == now - 100  # start still resolved by time


def test_a_past_window_end_is_resolved_by_time_unchanged(monkeypatch):
    from tornado_demix import demix

    now = 1_700_000_000
    monkeypatch.setattr(demix.time, "time", lambda: now)
    pool = fx.network().by_key["1 ETH"]
    # A deposit long ago: the whole window is in the past, nothing to clamp.
    voucher = {
        "pool_key": "1 ETH",
        "count": 1,
        "first_ts": 1000,
        "last_ts": 1000,
        "first_block": 1,
        "deposits": [],
    }

    class NoClamp(_WindowClient):
        def current_block(self):
            raise AssertionError("must not clamp a window that ends in the past")

    client = NoClamp()
    res = demix.analyze_denom_events(client, pool, [voucher], window_days=30)

    window = res["windows"][0]
    assert window["end_block"] == 1000 + 30 * 86400  # block_by_time("after")
    assert (1000 + 30 * 86400, "after") in client.by_time


def test_a_future_end_that_still_reaches_the_lookup_falls_back_on_the_error(monkeypatch):
    """Belt and braces: if a future end does reach block_by_time and it raises,
    the pool falls back to the current block rather than aborting."""
    from tornado_demix import demix
    from tornado_demix.errors import BlockLookupError

    # Pin now BELOW the window end so the proactive check does not fire, forcing
    # the except-path to be the one that saves the pool.
    monkeypatch.setattr(demix.time, "time", lambda: 0)
    pool = fx.network().by_key["1 ETH"]
    voucher = {
        "pool_key": "1 ETH",
        "count": 1,
        "first_ts": 1000,
        "last_ts": 1000,
        "first_block": 1,
        "deposits": [],
    }

    class Raising(_WindowClient):
        def block_by_time(self, ts, closest="before"):
            if closest == "after":
                raise BlockLookupError("Block timestamp too far in the future")
            return int(ts)

    client = Raising(current=777)
    res = demix.analyze_denom_events(client, pool, [voucher], window_days=30)
    assert res["windows"][0]["end_block"] == 777


def test_without_the_router_a_polygon_router_deposit_is_missed():
    """Regression guard: strip the router and the very same deposit vanishes -
    this is exactly the zero-deposits bug the registration fixes."""
    from tornado_demix.networks import Network
    from tornado_demix.pools import Pool

    net = Network(
        "polygon",
        137,
        "MATIC",
        [Pool("0x1e34a77868e19a6647b1f2f47b51ed72dede95dd", 100.0, "MATIC")],
    )  # no routers registered
    txs = [_tx(POLYGON_ROUTER, 100.0, 1000, 10, "0xpoly")]
    assert detect_deposits(None, fx.WALLET, network=net, txs=txs) == []


def test_a_none_client_does_not_fetch_token_transfers_on_a_chain_with_token_pools():
    from tornado_demix.networks import get_network

    assert detect_deposits(None, fx.WALLET, network=get_network("ethereum"), txs=[]) == []
