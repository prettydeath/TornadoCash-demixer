"""A direct link is a lead only when the linking transaction moved real value, succeeded and
came before the first withdrawal to that address. Zero-value, dust, post-withdrawal and
fake-token links, and counterparties whose contract status is unknown, are context with a
stated reason. Synthetic addresses only (the real-token addresses are public USDC and the
Ethereum pool tokens of the registry)."""

import pytest

from tests.fixtures import demix_result as fx
from tests.test_demix import RELAYER, _FakeClient, _recipient, _tx, _wlog
from tornado_demix import demix
from tornado_demix.heuristics import candidate_reason, candidate_reason_short, ranked_candidates
from tornado_demix.networks import DUST_FLOOR_WEI, load_networks, real_tokens
from tornado_demix.power import window_detectability

FAKE = "0x" + "f" * 40  # an attacker's token contract
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"  # USDC on Ethereum (Circle docs)
R = _recipient(3)  # a withdrawal recipient: first withdrawal at ts 1103
OTHER = "0x" + "9" * 40
BEFORE, AFTER = 1050, 5000


def tok(frm, to, ts, h, contract, value, symbol="TKN", decimals="18"):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 10),
        "hash": h,
        "contractAddress": contract,
        "tokenSymbol": symbol,
        "tokenDecimal": decimals,
    }


def native(frm, to, ts, h, wei):
    return {
        "from": frm,
        "to": to,
        "value": str(wei),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 10),
        "hash": h,
        "gasPrice": "1",
        "isError": "0",
        "txreceipt_status": "1",
    }


class _Client(_FakeClient):
    def __init__(self, txs, logs, tokens, internal=()):
        super().__init__(txs, logs)
        self.tokens = tokens
        self.internal = list(internal)

    def internal_txs(self, addr):
        return self.internal

    def token_transfers(self, addr, contract=None):
        return self.tokens

    def call(self, params):
        return []

    def has_at_least_txs(self, a, n):
        return False


def _check(answer):
    def check(address):
        if answer is None:
            check.failed.add(address)
            check.errors.append(
                {
                    "kind": "lookup_failed",
                    "what": "contract check (eth_getCode)",
                    "address": address,
                }
            )
        return answer

    check.failed = set()
    check.errors = []
    return check


def _run(monkeypatch, txs=(), tokens=(), internal=(), contract=False):
    monkeypatch.setattr(demix, "make_contract_check", lambda url: _check(contract))
    deposit = _tx(fx.POOL_1_ETH, 1.0, 1000, 100, "0xdep")
    logs = [
        _wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15, ts=1100 + i) for i in range(1, 8)
    ]
    client = _Client([deposit, *txs], logs, list(tokens), internal)
    data = demix.run_demix(client, fx.WALLET, network=fx.network(), labels={})
    return data, data["denoms"]["1 ETH"]["signals"][R]


def _row(data, address=R):
    return next(r for r in ranked_candidates(data) if r["address"] == address)


# --- attacks give no lead -----------------------------------------------


def test_zero_value_outgoing_tx_is_context_not_a_lead(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xz0", 0)])
    assert "linked" not in signals and "linked_dust" in signals
    assert _row(data)["band"] == "weak"


def test_one_wei_incoming_tx_is_context_not_a_lead(monkeypatch):
    data, signals = _run(monkeypatch, [native(R, fx.WALLET, BEFORE, "0xz1", 1)])
    assert "linked" not in signals and "linked_dust" in signals


def test_native_value_below_the_dust_floor_is_context(monkeypatch):
    floor = DUST_FLOOR_WEI[1]
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xd", floor - 1)])
    assert "linked" not in signals and "linked_dust" in signals
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xd", floor)])
    assert "linked" in signals


def test_real_value_sent_after_the_withdrawal_is_posthoc_context(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, AFTER, "0xlate", 10**18)])
    assert "linked" not in signals and "linked_posthoc" in signals
    link = data["counterparty_links"][R]
    assert link["position"] == "after" and link["tx"] == "0xlate"


def test_zero_value_after_the_withdrawal_reads_so_in_every_reason(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, AFTER, "0xz2", 0)])
    assert "linked" not in signals
    row = _row(data)
    why = candidate_reason(row)
    assert "0-value" in why and "after the first withdrawal" in why and "context" in why
    short = candidate_reason_short(row)
    assert "0-value" in short and "after the first withdrawal" in short


def test_fake_token_inside_the_depositors_own_tx_gives_no_lead(monkeypatch):
    call = native(fx.WALLET, FAKE, BEFORE, "0xown", 0)
    rows = [
        tok(fx.WALLET, OTHER, BEFORE, "0xown", FAKE, 1000),
        tok(R, fx.WALLET, BEFORE, "0xown", FAKE, 1000),
    ]
    data, signals = _run(monkeypatch, [call], rows)
    assert "linked" not in signals and "linked_unverified_token" in signals
    link = data["counterparty_links"][R]
    assert link["token"] == FAKE and link["qualifies"] is False


def test_failed_contract_check_is_unknown_context_with_an_error(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xv", 10**18)], contract=None)
    assert "linked" not in signals and "linked_unknown_contract" in signals
    assert any(e.get("address") == R for e in data["errors"])
    assert R in data["heuristics"]["linked_unknown_contracts"]


def test_no_contract_endpoint_is_unknown_too_and_recorded(monkeypatch):
    def unknown(address):
        return None

    unknown.failed = set()
    unknown.errors = []
    monkeypatch.setattr(demix, "make_contract_check", lambda url: unknown)
    deposit = _tx(fx.POOL_1_ETH, 1.0, 1000, 100, "0xdep")
    logs = [
        _wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15, ts=1100 + i) for i in range(1, 8)
    ]
    client = _Client([deposit, native(fx.WALLET, R, BEFORE, "0xv", 10**18)], logs, [])
    data = demix.run_demix(client, fx.WALLET, network=fx.network(), labels={})
    assert "linked_unknown_contract" in data["denoms"]["1 ETH"]["signals"][R]
    assert any(e.get("address") == R for e in data["errors"])


def test_failed_internal_transfer_is_no_link(monkeypatch):
    bad = dict(native(R, fx.WALLET, BEFORE, "0xi", 10**18), isError="1")
    data, signals = _run(monkeypatch, internal=[bad])
    assert R not in data["counterparty_addresses"] and "linked" not in signals


# --- genuine links still lead ------------------------------------------------------------


def test_genuine_pre_withdrawal_value_transfer_still_links(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xreal", 10**18)])
    assert "linked" in signals
    assert _row(data)["band"] == "moderate"
    link = data["counterparty_links"][R]
    assert link["tx"] == "0xreal"
    assert link["asset"] == "ETH" and link["amount"] == pytest.approx(1.0)
    assert link["direction"] == "out" and link["block"] == BEFORE // 10
    assert link["ts"] == BEFORE and link["position"] == "before"
    assert link["qualifies"] is True
    why = candidate_reason(_row(data))
    assert "1 ETH" in why and "before the first withdrawal" in why and "UTC" in why


def test_a_real_token_link_counts_and_its_dust_does_not(monkeypatch):
    call = native(fx.WALLET, USDC, BEFORE, "0xusdc", 0)
    paid = tok(fx.WALLET, R, BEFORE, "0xusdc", USDC, 5 * 10**6, "USDC", "6")
    data, signals = _run(monkeypatch, [call], [paid])
    assert "linked" in signals
    assert data["counterparty_links"][R]["asset"] == "USDC"
    dust = tok(fx.WALLET, R, BEFORE, "0xusdc", USDC, 10, "USDC", "6")
    data, signals = _run(monkeypatch, [call], [dust])
    assert "linked" not in signals and "linked_dust" in signals


def test_the_earliest_qualifying_link_is_cited_not_an_earlier_dust_one(monkeypatch):
    txs = [native(fx.WALLET, R, 1010, "0xdust", 0), native(fx.WALLET, R, BEFORE, "0xreal", 10**18)]
    data, signals = _run(monkeypatch, txs)
    assert "linked" in signals
    assert data["counterparty_links"][R]["tx"] == "0xreal"


# --- the registry ------------------------------------------------------------------------


def test_every_network_has_a_dust_floor_and_ethereum_lists_its_pool_tokens():
    nets = load_networks()
    for net in nets.values():
        assert net.dust_floor_wei > 0
    eth = real_tokens(1)
    for pool in nets["ethereum"].pools:
        if pool.token:
            assert pool.token in eth
    for info in eth.values():
        assert info["source_url"].startswith("https://") and info["min_raw"] > 0


# --- placebo / power use the same rule ---------------------------------------------------


def test_power_check_does_not_plant_on_a_zero_value_counterparty(monkeypatch):
    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xz0", 0)])
    got = window_detectability(data, fx.WALLET, is_contract=lambda a: a != R)
    assert got["reason"] == "no_qualifying_link" and got["recipient"] is None


def test_power_check_detects_a_planted_link_to_a_real_counterparty(monkeypatch):
    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xreal", 10**18)])
    got = window_detectability(data, fx.WALLET, is_contract=lambda a: a != R)
    assert got["reason"] == "detected" and got["recipient"] == R


def test_decoy_run_applies_the_same_rule(monkeypatch):
    """A decoy window is run_demix with shifted deposits: a 0-value link stays context."""
    monkeypatch.setattr(demix, "make_contract_check", lambda url: _check(False))
    deposit = _tx(fx.POOL_1_ETH, 1.0, 1000 + 10**6, 100, "0xdep")
    logs = [
        _wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15, ts=1100 + i) for i in range(1, 8)
    ]
    client = _Client([deposit, native(fx.WALLET, R, BEFORE, "0xz0", 0)], logs, [])
    data = demix.run_demix(
        client, fx.WALLET, network=fx.network(), labels={}, deposit_shift_seconds=10**6
    )
    assert "linked" not in data["denoms"]["1 ETH"]["signals"][R]


# --- every report says why the direct link is context ------------------------------------


def test_reports_show_the_zero_value_post_withdrawal_link_as_context(monkeypatch):
    import json
    import re

    from tornado_demix.honest import LIMITATIONS
    from tornado_demix.report_html import build_html_report
    from tornado_demix.report_json import demix_json

    data, _s = _run(monkeypatch, [native(fx.WALLET, R, AFTER, "0xz2", 0)])
    html = build_html_report(data, fx.network())
    start = html.index('<section class="p1"')
    page1 = html[start : html.index("</section>", html.index('class="p1-toc"', start)) + 10]
    text = re.sub(r"<[^>]+>", "", html)
    assert "0-value" in text and "after the first withdrawal to it" in text
    assert "only zero-value or dust" in re.sub(r"<[^>]+>", "", page1)
    doc = json.loads(demix_json(data))
    link = doc["result"]["counterparty_links"][R]
    for key in ("tx", "ts", "block", "direction", "asset", "raw", "position", "qualifies"):
        assert key in link
    row = next(c for c in doc["candidates"] if c["address"] == R)
    assert row["band"] == "weak" and "linked_dust" in row["signals"]
    assert any("dust floor" in item for item in LIMITATIONS)


def test_html_timeline_cites_value_and_position_of_a_lead_link(monkeypatch):
    import re

    from tornado_demix.report_html import build_html_report

    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xreal", 10**18)])
    text = re.sub(r"<[^>]+>", "", build_html_report(data, fx.network()))
    assert "1 ETH from the depositor, block 105" in text
    assert "before the first withdrawal to it" in text


# --- decoy windows compare links at the same timing relative to the deposits -------------

OFF = 10**6


def _shifted(monkeypatch, withdrawal_base, shift, link_ts):
    """Deposit at OFF + 1000 (real time), a real 1 ETH link at ``link_ts`` (real time),
    withdrawals at ``withdrawal_base`` + 101..107; ``shift`` moves the deposit back."""
    monkeypatch.setattr(demix, "make_contract_check", lambda url: _check(False))
    dep = _tx(fx.POOL_1_ETH, 1.0, OFF + 1000, 100, "0xdep")
    link = native(fx.WALLET, R, link_ts, "0xlink", 10**18)
    logs = [
        _wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15, ts=withdrawal_base + 100 + i)
        for i in range(1, 8)
    ]
    data = demix.run_demix(
        _Client([dep, link], logs, []),
        fx.WALLET,
        network=fx.network(),
        labels={},
        deposit_shift_seconds=shift,
    )
    return data, set(data["denoms"]["1 ETH"]["signals"].get(R, ()))


@pytest.mark.parametrize("link_after_deposit", [50, 5000])
def test_target_and_decoy_with_the_same_relative_timing_agree(monkeypatch, link_after_deposit):
    link_ts = OFF + 1000 + link_after_deposit  # 50 s: before R's withdrawal; 5000 s: after
    _t, target = _shifted(monkeypatch, OFF + 1000, 0, link_ts)
    _d, decoy = _shifted(monkeypatch, 1000, OFF, link_ts)
    want = "linked" if link_after_deposit == 50 else "linked_posthoc"
    assert want in target and want in decoy
    assert target & {"linked", "linked_posthoc"} == decoy & {"linked", "linked_posthoc"}


def test_decoy_link_keeps_its_real_time_and_states_the_relative_position(monkeypatch):
    data, signals = _shifted(monkeypatch, 1000, OFF, OFF + 1050)
    link = data["counterparty_links"][R]
    assert link["ts"] == OFF + 1050 and link["position"] == "before"
    assert link["lead_seconds"] == 1103 - 1050


def test_power_check_on_a_decoy_plants_on_a_link_made_after_the_real_deposit(monkeypatch):
    from tornado_demix.placebo import summarize

    decoy, _s = _shifted(monkeypatch, 1000, OFF, OFF + 1050)
    got = window_detectability(decoy, fx.WALLET, is_contract=lambda a: a != R)
    assert got["reason"] == "detected" and got["recipient"] == R
    assert summarize(decoy, decoy, got)["direct_link_verdict"] is None


def test_power_check_names_counterparties_without_a_qualifying_link(monkeypatch):
    from tornado_demix.placebo import summarize

    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xz0", 0)])
    got = window_detectability(data, fx.WALLET, is_contract=lambda a: a != R)
    assert got["reason"] == "no_qualifying_link"
    verdict = summarize(data, data, got)["direct_link_verdict"]
    assert "none can be observed in the real window either" not in verdict
    assert "real value before the planted withdrawal" in verdict


# --- more attacks and genuine links -------------------------------------------------------


def test_link_at_the_same_time_as_the_withdrawal_is_not_before(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, 1103, "0xsame", 10**18)])
    assert "linked" not in signals and "linked_posthoc" in signals


def test_incoming_real_payment_before_the_withdrawal_links_and_says_to_the_depositor(
    monkeypatch,
):
    data, signals = _run(monkeypatch, [native(R, fx.WALLET, BEFORE, "0xin", 10**18)])
    assert "linked" in signals
    assert "to the depositor" in candidate_reason_short(_row(data))


def test_outgoing_link_short_reason_says_from_the_depositor(monkeypatch):
    data, _s = _run(monkeypatch, [native(fx.WALLET, R, AFTER, "0xz2", 0)])
    short = candidate_reason_short(_row(data))
    assert "0-value tx from the depositor after the first withdrawal" in short


def test_an_earlier_root_excluded_withdrawal_moves_the_reference(monkeypatch):
    data, signals = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xreal", 10**18)])
    assert "linked" in signals
    res = data["denoms"]["1 ETH"]
    res["root_excluded_detail"] = {R: [{"ts": 1040, "hash": "0xold", "value": 0.99}]}
    demix.score_collected(data, set(data["counterparty_addresses"]), {"is_contract": _check(False)})
    signals = res["signals"][R]
    assert "linked" not in signals and "linked_posthoc" in signals


def test_internal_transfer_below_the_floor_is_dust(monkeypatch):
    small = native(R, fx.WALLET, BEFORE, "0xi", DUST_FLOOR_WEI[1] - 1)
    data, signals = _run(monkeypatch, internal=[small])
    assert "linked" not in signals and "linked_dust" in signals
    data, signals = _run(monkeypatch, internal=[dict(small, value=str(10**18))])
    assert "linked" in signals


def test_a_link_without_a_timestamp_is_not_known_to_come_first():
    from tornado_demix.heuristics import lead_link, link_status

    links = [{"ts": 0, "tx": "0x", "qualifies": True, "why": None}]
    assert lead_link(links, 1103) is None
    assert link_status(links, 1103) == "linked_posthoc"


def test_an_address_without_link_records_is_no_direct_link():
    from tornado_demix.heuristics import link_status

    assert link_status([], 1103) == "none"


def test_a_creation_row_without_the_depositor_as_a_side_links_nobody(monkeypatch):
    """An internal row from R that creates the depositor (to empty, contractAddress = the
    depositor) names no counterparty: R gets no lead even when it is not a contract."""
    creation = {
        "from": R,
        "to": "",
        "contractAddress": fx.WALLET,
        "value": "0",
        "timeStamp": str(BEFORE),
        "blockNumber": str(BEFORE // 10),
        "hash": "0xcreate",
        "isError": "0",
    }
    data, signals = _run(monkeypatch, internal=[creation])
    assert R not in data["counterparty_addresses"]
    assert not {s for s in signals if s.startswith("linked")}


def test_a_live_run_never_applies_the_old_rule_to_an_address_without_a_record():
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    data["direct_links"] = {}
    apply_heuristics(data, counterparties={fx.BOB}, is_contract=lambda a: False)
    for res in data["denoms"].values():
        assert not {s for s in res["signals"].get(fx.BOB, []) if s.startswith("linked")}


def test_dust_amounts_print_exactly_and_name_the_floor(monkeypatch):
    floor = DUST_FLOOR_WEI[1]
    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xd", floor - 1)])
    why = candidate_reason(_row(data))
    assert "0.000999999999999999 ETH (dust, below the 0.001 ETH floor)" in why
    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xw", 1)])
    why = candidate_reason(_row(data))
    assert "0.000000000000000001 ETH (dust" in why and "e-18" not in why


def test_an_unlisted_token_is_shown_by_address_not_by_its_claimed_symbol(monkeypatch):
    call = native(fx.WALLET, FAKE, BEFORE, "0xown", 0)
    rows = [
        tok(fx.WALLET, OTHER, BEFORE, "0xown", FAKE, 1000, "USDC"),
        tok(R, fx.WALLET, BEFORE, "0xown", FAKE, 1000, "USDC"),
    ]
    data, _s = _run(monkeypatch, [call], rows)
    why = candidate_reason(_row(data))
    assert f"unverified token {FAKE}" in why and "USDC" not in why
    assert data["counterparty_links"][R]["claimed_symbol"] == "USDC"


def test_the_token_list_covers_bridged_and_l2_stablecoins_and_bsc_gnosis():
    assert "0x2791bca1f2de4661ed88a30c99a7a9449aa84174" in real_tokens(137)  # USDC.e
    assert "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8" in real_tokens(42161)  # USDC.e
    assert "0x7f5c764cbc14f9669b88837ca1490cca17c31607" in real_tokens(10)  # USDC.e
    assert "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9" in real_tokens(42161)  # USDT
    assert real_tokens(56) and real_tokens(100)


def test_floors_are_roughly_equal_in_value():
    # CoinGecko USD prices of 2026-11-14, the assumption stated in networks.py
    usd = {1: 1845, 10: 1845, 8453: 1845, 42161: 1845, 56: 598, 137: 0.081, 100: 1.0, 43114: 7.69}
    worth = {c: DUST_FLOOR_WEI[c] / 10**18 * p for c, p in usd.items()}
    assert all(1.0 <= w <= 3.0 for w in worth.values()), worth


# --- offline re-scoring tools say which link rule they applied ---------------------------


def test_offline_rescoring_counts_runs_without_link_records(monkeypatch):
    import copy
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
    import placebo_windows as pw

    monkeypatch.setattr(pw, "LINK_RULE", {})
    new, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xz0", 0)])
    old = copy.deepcopy(new)
    del old["direct_links"]
    old["wallet"] = OTHER
    old["heuristics"]["linked_addresses"].append(("1 ETH", R))  # as a pre-2.23 run had it
    assert "linked" not in pw.narrow(new, 720)["denoms"]["1 ETH"]["signals"][R]
    assert "linked" in pw.narrow(old, 720)["denoms"]["1 ETH"]["signals"][R]
    pw.narrow(old, 72)
    assert pw.link_rule_note().startswith("1 of 2 runs scored with the pre-2.23 direct-link rule")


def test_no_qualifying_link_is_blind_in_the_placebo_verdicts(monkeypatch):
    from tornado_demix.placebo import BLIND_SUFFIX, summarize

    data, _s = _run(monkeypatch, [native(fx.WALLET, R, BEFORE, "0xz0", 0)])
    got = window_detectability(data, fx.WALLET, is_contract=lambda a: a != R)
    verdicts = summarize(data, data, got)["verdicts"]
    assert verdicts["moderate"].endswith(BLIND_SUFFIX)
    assert not verdicts["weak"].endswith(BLIND_SUFFIX)


def test_importing_the_offline_tools_registers_no_exit_note():
    import atexit
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
    import placebo_windows as pw

    assert not hasattr(pw, "_print_link_rule_note")
    assert callable(pw.print_link_rule_note)
    assert "atexit" not in vars(pw) and atexit  # the module no longer imports it


def _group_data():
    """A two-wallet group (WALLET and BOB merged): BOB, a member, receives an early
    withdrawal in the 1 ETH pool (ts 1300, under an hour after the last deposit)."""
    from tornado_demix.heuristics import apply_heuristics

    data = fx.two_pool_result()
    data["wallet"] = ""
    data["wallets"] = [fx.WALLET, fx.BOB]
    data["direct_links"] = {}  # a live run with link records; members have none
    return data, apply_heuristics


@pytest.mark.parametrize(
    "status, expected",
    [(True, None), (None, "linked_unknown_contract"), (False, "linked")],
)
def test_a_merged_member_that_is_a_contract_is_no_lead_in_the_group_view(status, expected):
    data, apply_heuristics = _group_data()
    apply_heuristics(data, counterparties={fx.WALLET, fx.BOB}, is_contract=lambda a: status)
    sig = data["denoms"]["1 ETH"]["signals"].get(fx.BOB, [])
    if status is True:
        # as in 2.22: a contract member is not a direct link, so it earns nothing
        assert "linked" not in sig and "linked_late" not in sig
        assert fx.BOB in data["heuristics"]["linked_contracts"]
        assert fx.BOB not in {
            r["address"] for r in ranked_candidates(data) if "linked" in r["signals"]
        }
    elif status is None:
        assert "linked" not in sig and expected in sig
        assert fx.BOB in data["heuristics"]["linked_unknown_contracts"]
    else:
        assert expected in sig
