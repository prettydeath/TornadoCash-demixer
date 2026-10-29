"""Count matches must fit a voucher's own window; fee fingerprints group self-relayed exits."""

from collections import Counter, defaultdict

from tornado_demix.heuristics import (
    apply_heuristics,
    candidate_evidence,
    count_in_voucher_order,
)
from tornado_demix.relayer import (
    fee_fingerprints,
    fee_key,
    fee_label,
    fee_profile,
    record_fee_profiles,
    verify_broadcaster,
)

DAY = 86400
JUNE = 1_780_000_000  # one 1-note voucher
AUG17 = JUNE + 74 * DAY  # 8-note voucher
AUG19 = AUG17 + 2 * DAY  # 7-note voucher
POOL = "100 ETH"


def _vouchers():
    return [
        {"pool_key": POOL, "count": 1, "first_ts": JUNE, "last_ts": JUNE},
        {"pool_key": POOL, "count": 8, "first_ts": AUG17, "last_ts": AUG17 + 180},
        {"pool_key": POOL, "count": 7, "first_ts": AUG19, "last_ts": AUG19 + 180},
    ]


PARAMS = {"window_days": 30, "exit_window_hours": None}


def test_withdrawals_before_a_voucher_do_not_match_it():
    june = [JUNE + DAY + i * 600 for i in range(7)]
    assert not count_in_voucher_order(_vouchers(), PARAMS, POOL, june)


def test_withdrawals_inside_the_voucher_window_match():
    after = [AUG19 + 3 * DAY + i * 600 for i in range(7)]
    assert count_in_voucher_order(_vouchers(), PARAMS, POOL, after)


def test_a_count_of_one_voucher_size_needs_that_voucher():
    # 8 withdrawals after 19.08 fit the 8-note voucher of 17.08 (window still open).
    eight = [AUG19 + DAY + i * 600 for i in range(8)]
    assert count_in_voucher_order(_vouchers(), PARAMS, POOL, eight)


def test_the_sum_of_vouchers_needs_no_withdrawal_before_the_first_deposit():
    sixteen = [JUNE + i * DAY for i in range(16)]
    assert count_in_voucher_order(_vouchers(), PARAMS, POOL, sixteen)
    early = [JUNE - DAY] + sixteen[1:]
    assert not count_in_voucher_order(_vouchers(), PARAMS, POOL, early)


def test_exit_window_hours_shortens_the_window():
    late = [AUG19 + 5 * DAY + i * 60 for i in range(7)]
    assert count_in_voucher_order(_vouchers(), PARAMS, POOL, late)
    assert not count_in_voucher_order(
        _vouchers(), {"window_days": 30, "exit_window_hours": 72}, POOL, late
    )


def _data(voucher_order):
    detail = defaultdict(list)
    june_addr = "0x" + "a" * 40
    aug_addr = "0x" + "b" * 40
    for i in range(7):
        detail[june_addr].append(_rec(JUNE + DAY + i * 600, i))
        detail[aug_addr].append(_rec(AUG19 + DAY + i * 600, 100 + i))
    for k in range(20):
        detail["0x%040x" % (k + 1)].append(_rec(AUG17 + k * 900, 200 + k))
    res = {
        "pool_key": POOL,
        "denom": 100.0,
        "asset": "ETH",
        "decimals": 18,
        "pool": "0xpool",
        "mode": "events",
        "counts": Counter({a: len(r) for a, r in detail.items()}),
        "detail": detail,
        "withdrawals": [],
        "target_counts": [1, 7, 8, 16],
        "unique_recipients": len(detail),
    }
    params = dict(PARAMS, mode="events")
    if voucher_order:
        params["voucher_order"] = True
    return (
        {
            "wallet": "0xw",
            "params": params,
            "deposits": [{"pool_key": POOL, "gas_price": 1}],
            "vouchers": _vouchers(),
            "denoms": {POOL: res},
        },
        june_addr,
        aug_addr,
    )


def _rec(ts, n):
    return {
        "value": 100.0,
        "ts": ts,
        "hash": "0x%064x" % (n + 1),
        "relayer": "0xrelayer",
        "fee": 0.0,
        "asset": "ETH",
        "self_relayed": False,
        "gas_price": 0,
    }


def test_apply_heuristics_drops_out_of_order_count_matches():
    data, june_addr, aug_addr = _data(voucher_order=True)
    apply_heuristics(data, set())
    sig = data["denoms"][POOL]["signals"]
    assert "count_match" not in sig[june_addr]
    assert "count_match" in sig[aug_addr]
    detail = next(
        e["detail"]
        for e in candidate_evidence(data, POOL, june_addr)
        if e["signal"] == "count_match"
    )
    assert "not counted" in detail


def test_cached_runs_without_the_flag_rescore_as_measured():
    data, june_addr, _ = _data(voucher_order=False)
    apply_heuristics(data, set())
    assert "count_match" in data["denoms"][POOL]["signals"][june_addr]


# ---------------------------------------------------------------- fee fingerprint


def _tx(sender, prio_gwei, gas):
    return {
        "from": sender,
        "type": "0x2",
        "maxPriorityFeePerGas": hex(int(prio_gwei * 1e9)),
        "maxFeePerGas": hex(int((prio_gwei + 0.1) * 1e9)),
        "gasPrice": hex(int((prio_gwei + 0.05) * 1e9)),
        "gas": hex(gas),
    }


class _InfoClient:
    def __init__(self, txs):
        self.txs = txs

    def tx_info(self, tx_hash):
        return self.txs.get(tx_hash)


class _SenderOnlyClient:
    def tx_sender(self, tx_hash):
        return "0xbroadcaster"


def _lead(to, tx, pool=POOL):
    return {"to": to, "tx_hash": tx, "pool_key": pool}


def test_fee_profile_reads_type2_fields():
    p = fee_profile(_tx("0xa", 3.0, 428318))
    assert p["priority_fee_wei"] == 3_000_000_000
    assert p["gas_limit"] == 428318
    assert fee_label(fee_key(p)) == "priority 3 gwei, gas ~428k"


def test_verify_broadcaster_records_fee_profile_from_one_lookup():
    a, b = "0x" + "1" * 40, "0x" + "2" * 40
    txs = {"t1": _tx(a, 3.0, 428318), "t2": _tx(b, 3.0, 428302), "t3": _tx(b, 0.15, 535434)}
    leads = [_lead(a, "t1"), _lead(b, "t2"), _lead(b, "t3")]
    assert verify_broadcaster(_InfoClient(txs), leads) == 3
    assert leads[0]["broadcaster_status"] == "self"
    assert all("fee_profile" in lead for lead in leads)


def test_sender_only_clients_still_work():
    leads = [_lead("0x" + "3" * 40, "t9")]
    assert verify_broadcaster(_SenderOnlyClient(), leads) == 1
    assert leads[0]["broadcaster_status"] == "other"
    assert "fee_profile" not in leads[0]


def test_identical_fee_settings_group_across_recipients():
    a, b, c = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "4" * 40
    txs = {
        "t1": _tx(a, 3.0, 428318),
        "t2": _tx(b, 3.0, 428302),  # same priority fee, same 1k gas bucket
        "t3": _tx(c, 0.15, 535434),
    }
    leads = [_lead(a, "t1"), _lead(b, "t2"), _lead(c, "t3")]
    verify_broadcaster(_InfoClient(txs), leads)
    groups = fee_fingerprints(leads)
    shared = [g for g in groups if g["shared"]]
    assert len(shared) == 1
    assert shared[0]["recipients"] == sorted([a, b])
    assert shared[0]["label"] == "priority 3 gwei, gas ~428k"


def test_record_fee_profiles_stores_them_in_the_result():
    a = "0x" + "1" * 40
    leads = [_lead(a, "t1")]
    verify_broadcaster(_InfoClient({"t1": _tx(a, 3.0, 428318)}), leads)
    data = {}
    record_fee_profiles(data, leads)
    assert data["fee_profiles"]["t1"]["gas_limit"] == 428318
    assert data["fee_fingerprints"][0]["shared"] is False


def test_context_withdrawals_link_to_a_lead_but_never_stand_alone():
    from tornado_demix.relayer import fingerprint_context

    lead_addr, ctx_addr, other = "0x" + "1" * 40, "0x" + "5" * 40, "0x" + "6" * 40
    rec = lambda h, ts: {"hash": h, "ts": ts, "self_relayed": True}  # noqa: E731
    data = {
        "denoms": {
            POOL: {
                "detail": {
                    lead_addr: [rec("t1", 1)],
                    ctx_addr: [rec("c1", 2), rec("c2", 3), rec("c3", 4)],
                    other: [rec("o1", 5), rec("o2", 6)],
                }
            }
        }
    }
    leads = [_lead(lead_addr, "t1")]
    context = fingerprint_context(data, leads)
    assert {c["tx_hash"] for c in context} == {"c1", "c2", "c3", "o1", "o2"}
    assert context[0]["to"] == ctx_addr  # most withdrawals first
    txs = {
        "t1": _tx(lead_addr, 3.0, 428318),
        "c1": _tx(ctx_addr, 3.0, 428302),
        "c2": _tx(ctx_addr, 3.0, 428318),
        "c3": _tx(ctx_addr, 0.208, 428332),
        "o1": _tx(other, 0.5, 300000),
        "o2": _tx("0x" + "7" * 40, 0.5, 300100),
    }
    client = _InfoClient(txs)
    verify_broadcaster(client, leads)
    verify_broadcaster(client, context)
    groups = fee_fingerprints(leads + context)
    shared = [g for g in groups if g["shared"]]
    assert len(shared) == 1 and shared[0]["recipients"] == sorted([lead_addr, ctx_addr])
    # Two context-only recipients sharing settings are not reported.
    assert not any(g["shared"] and lead_addr not in g["recipients"] for g in groups)
