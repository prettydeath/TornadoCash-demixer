"""Transaction reads of the real client, cluster cache, multi funders, placebo checks."""

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix import incomplete
from tornado_demix.deposit_addresses import classify_deposit_address
from tornado_demix.errors import ApiError, ApiKeyError
from tornado_demix.etherscan import EtherscanClient

TX = "0x" + "ab" * 32


def _client(call, rpc=None):
    """A REAL EtherscanClient whose explorer call (and optional RPC) is stubbed."""
    c = EtherscanClient("K" * 34, retries=1)
    c.call = call
    if rpc is not None:
        c._rpc_result = rpc
    return c


def _failing(params):
    raise ApiError("Max rate limit reached")


# ---------------------------------------------------------------- etherscan transaction reads


@pytest.mark.parametrize("method", ["tx_info", "tx_receipt", "deposit_emitters", "tx_sender"])
def test_a_failed_transaction_read_raises(method):
    with pytest.raises(ApiError, match="rate limit"):
        getattr(_client(_failing), method)(TX)


@pytest.mark.parametrize("method", ["tx_info", "tx_receipt", "deposit_emitters", "tx_sender"])
def test_an_unknown_hash_stays_none(method):
    assert getattr(_client(lambda params: None), method)(TX) is None


def test_a_rejected_key_stays_fatal():
    def rejected(params):
        raise ApiKeyError("Invalid API Key")

    with pytest.raises(ApiKeyError):
        _client(rejected).tx_info(TX)


def test_the_rpc_answers_when_the_explorer_failed():
    tx = {"hash": TX, "from": "0x" + "1" * 40}
    assert _client(_failing, rpc=lambda m, p: (True, tx)).tx_info(TX) == tx
    # the node answered null: an unknown hash, not a failure
    assert _client(_failing, rpc=lambda m, p: (True, None)).tx_info(TX) is None
    with pytest.raises(ApiError):
        _client(_failing, rpc=lambda m, p: (False, None)).tx_info(TX)


def test_a_compat_explorer_without_the_proxy_module_is_probed_once():
    calls = []

    def unknown_module(params):
        calls.append(params)
        raise ApiError("Unknown module")

    c = _client(unknown_module)
    c.style = "compat"
    for _ in range(3):
        with pytest.raises(ApiError):
            c.tx_info(TX)
    assert len(calls) == 1  # later reads do not pay the retry budget again


# ---------------------------------------------------------------- callers end in explicit states


def test_a_forwarder_whose_transactions_cannot_be_read_is_unknown_on_the_real_client():
    from tests.test_deposit_addresses import DEPOSIT, two_token_lists

    lists = two_token_lists()

    def call(params):
        if params.get("module") == "proxy":
            raise ApiError("Max rate limit reached")
        return lists.get((params["action"], params["address"]), [])

    info = classify_deposit_address(_client(call), DEPOSIT)
    assert info["kind"] == "unknown" and "rate limit" in info["error"]


def test_a_failed_bridge_receipt_ends_the_branch_as_lookup_failed():
    from tests import test_trace_bridge as tb
    from tornado_demix.trace import trace_funds

    class Source(tb.Source):
        def tx_receipt(self, tx_hash):
            raise ApiError("Max rate limit reached")

    out = trace_funds(
        Source(),
        tb.USER,
        3261.597488,
        start_block=tb.BLOCK - 5,
        token=tb.USDC,
        chain_id=1,
        destinations=tb._dest,
    )
    (end,) = out["terminals"]
    assert end["reason"] == "lookup_failed" and end["address"] == tb.SPOKE
    assert [e["what"] for e in out["errors"]] == ["bridge deposit receipt"]


def test_a_failed_twin_pool_receipt_is_recorded_and_the_pool_stays_ambiguous():
    from tests.test_demix import DAI_TOKEN, ROUTER, _ttx, _twin_network
    from tornado_demix.demix import detect_deposits

    class Client:
        def deposit_emitters(self, tx_hash):
            raise ApiError("Max rate limit reached")

    net = _twin_network()
    errors = []
    deposits = detect_deposits(
        Client(),
        fx.WALLET,
        network=net,
        txs=[],
        internal_txs=[],
        token_txs=[_ttx(ROUTER, 100 * 10**18, 1000, "0xt1", DAI_TOKEN)],
        errors=errors,
    )
    assert deposits[0]["pool_ambiguous"]
    assert errors[0]["what"] == "deposit receipt (twin pool)" and errors[0]["tx"] == "0xt1"


def test_a_failed_broadcaster_read_leaves_the_lead_unverified():
    from tornado_demix.relayer import verify_broadcaster

    lead = {"tx_hash": TX, "to": "0x" + "2" * 40}
    assert verify_broadcaster(_client(_failing), [lead]) == 0
    assert "broadcaster" not in lead


def test_a_window_end_fallback_is_recorded_as_a_note():
    from tornado_demix.demix import _resolve_window_end
    from tornado_demix.errors import BlockLookupError

    class C:
        def block_by_time(self, ts, closest):
            raise BlockLookupError("no block")

        def current_block(self):
            return 99

    notes = []
    assert _resolve_window_end(C(), 1_000, notes) == 99
    assert notes[0]["kind"] == "window_end_fallback" and "no block" in notes[0]["error"]


# ---------------------------------------------------------------- multi shared funders


def test_a_failed_funder_busy_check_is_recorded_and_the_run_goes_on():
    from tornado_demix.multi import shared_funders

    funder = "0x" + "f" * 40

    class C:
        def has_at_least_txs(self, a, n):
            raise ApiError("refused 10000-row page")

    results = {w: {"funders": [funder]} for w in ("0x" + "a" * 40, "0x" + "b" * 40)}
    errors = []
    out = shared_funders(C(), results, errors=errors)
    assert out == {funder: sorted(results)}  # kept, as unchecked
    assert errors[0]["what"] == "funder busy check" and errors[0]["address"] == funder


# ---------------------------------------------------------------- cluster


def test_cluster_does_not_cache_an_incomplete_run_and_carries_its_errors(tmp_path, monkeypatch):
    from tornado_demix import cluster

    def fake_demix(*a, **kw):
        data = {"wallet": fx.WALLET, "vouchers": [], "denoms": {}, "deposits": []}
        return incomplete.attach(data, [incomplete.lookup_failed("history", "timeout")], [])

    monkeypatch.setattr(cluster, "run_demix", fake_demix)
    out = cluster.trace_wallet(object(), fx.WALLET, str(tmp_path), network=fx.network())
    assert out["error_count"] == 1 and out["errors"][0]["what"] == "history"
    assert not list(tmp_path.rglob("layers_*"))


# ---------------------------------------------------------------- placebo


def test_a_failed_contract_check_makes_the_direct_link_unknown(monkeypatch):
    from tornado_demix import placebo

    def check(addr):
        return None

    check.failed = {"0x" + "c" * 40}
    check.errors = [incomplete.lookup_failed("contract check (eth_getCode)", "no answer")]
    monkeypatch.setattr(placebo, "make_contract_check", lambda url: check)
    monkeypatch.setattr(placebo, "run_demix", lambda *a, **kw: {"deposits": [], "denoms": {}})
    monkeypatch.setattr(placebo, "window_detectability", lambda *a, **kw: {"detectable": True})
    seen = {}
    monkeypatch.setattr(
        placebo,
        "summarize",
        lambda t, d, direct_link=None: seen.update(direct_link=direct_link, decoy=d) or {},
    )
    placebo.run_placebo(object(), fx.WALLET, {"deposits": []}, network=fx.network())
    assert seen["direct_link"]["detectable"] is False
    assert seen["direct_link"]["reason"] == "contract_unknown"
    assert seen["decoy"]["errors"][-1]["what"] == "contract check (eth_getCode)"


def test_the_placebo_caveat_does_not_claim_a_direction(monkeypatch):
    from tests.test_placebo import _side
    from tornado_demix import placebo
    from tornado_demix.placebo import summarize

    monkeypatch.setattr(placebo, "ranked_candidates", lambda data: data["rows"])

    out = summarize(dict(_side([], 10), errors=[{}]), dict(_side([], 10), errors=[]))
    assert any("can change that side's counts" in c for c in out["caveats"])


def _cluster_trace(errors=1):
    from tornado_demix import incomplete as inc

    trace = {"wallet": fx.WALLET, "fingerprint": {"1 ETH": 2}, "layers": {}, "clusters": []}
    return inc.attach(trace, [inc.lookup_failed("history", "timeout", address=fx.BOB)] * errors, [])


def test_the_cluster_report_shows_the_notice():
    from tornado_demix.report import build_cluster_report

    html = build_cluster_report([_cluster_trace()], fx.network())
    assert "1 lookup failed — results may be incomplete" in html and fx.BOB in html
    assert "may be incomplete" not in build_cluster_report([_cluster_trace(0)], fx.network())


def test_csv_outputs_add_a_lookup_errors_file_only_when_something_failed(tmp_path):
    import csv

    from tornado_demix.report import write_cluster_csv

    files = write_cluster_csv([_cluster_trace()], str(tmp_path / "a"), fx.network())
    errors_csv = [f for f in files if f.endswith("lookup_errors.csv")]
    assert len(errors_csv) == 1
    with open(errors_csv[0], encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["wallet"] == fx.WALLET and rows[0]["address"] == fx.BOB
    assert rows[0]["what"] == "history" and rows[0]["error"] == "timeout"
    clean = write_cluster_csv([_cluster_trace(0)], str(tmp_path / "b"), fx.network())
    assert not any(f.endswith("lookup_errors.csv") for f in clean)


def test_demix_and_multi_csv_add_the_lookup_errors_file(tmp_path):
    from tests.test_lookup_failures_reports import _demix_data
    from tests.test_report import _multi_corr
    from tornado_demix.report import write_demix_csv, write_multi_csv

    files = write_demix_csv(_demix_data(), str(tmp_path / "d"), fx.network())
    assert any(f.endswith("lookup_errors.csv") for f in files)
    corr = incomplete.attach(_multi_corr(), [incomplete.lookup_failed("x", "y")], [])
    files = write_multi_csv(corr, str(tmp_path / "m"), fx.network())
    assert any(f.endswith("lookup_errors.csv") for f in files)


def test_cli_cluster_prints_the_notice(monkeypatch, capsys):
    import argparse

    from tornado_demix import cli

    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "trace_wallet", lambda *a, **kw: _cluster_trace())
    ns = argparse.Namespace(
        api_csv=None,
        network="ethereum",
        networks_csv=None,
        wallets=[fx.WALLET],
        wallets_csv="",
        out_dir="",
        report="",
        cache_dir=".cache",
        window_days=30,
        fee_lo=0.9,
        fee_hi=0.995,
        gap_hours=24,
        layer_cap=35,
        min_forward_frac=0.01,
    )
    monkeypatch.setattr(cli, "_network", lambda args: fx.network())
    cli.cmd_cluster(ns)
    assert "1 lookup failed - results may be incomplete" in capsys.readouterr().out


def test_one_trace_with_a_failure_a_small_branch_and_a_redeposit():
    from tests.test_lookup_failures import EXIT, HOP1, HOP2, HOP3, POOL, TraceClient, _ends, _t
    from tornado_demix.trace import trace_funds

    client = TraceClient(
        normal=[
            _t("0x" + "f" * 40, EXIT, 100, 5, "0xin"),
            _t(EXIT, HOP1, 60, 10, "0xa"),  # HOP1's history fails
            _t(EXIT, HOP2, 39.5, 11, "0xb"),
            _t(EXIT, HOP3, 0.5, 12, "0xc"),  # 0.5 %: below the threshold
            _t(HOP2, POOL, 0.2, 20, "0xd", data="0xb214faa5"),  # tiny re-deposit
            _t(HOP2, HOP3, 39.3, 21, "0xe"),
        ],
        fail={HOP1},
    )
    out = trace_funds(client, EXIT, 100, tornado={POOL})
    assert _ends(out) == {
        (HOP1, "lookup_failed"): 60,
        (HOP3, "below_threshold"): 0.5,
        (POOL, "tornado_redeposit"): 0.2,
        (HOP3, "not moved on (held or untraced)"): 39.3,
    }
    assert sum(t["amount"] for t in out["terminals"]) == pytest.approx(100)
    assert [(e["what"], e["address"]) for e in out["errors"]] == [("history", HOP1)]
