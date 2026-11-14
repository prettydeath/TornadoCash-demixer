"""Merkle-root exclusion of candidate withdrawals (run_demix(root_check=True))."""

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix import root_exclusion
from tornado_demix.constants import TOPIC_DEPOSIT, TOPIC_WITHDRAWAL, ZERO_ADDRESS
from tornado_demix.demix import run_demix
from tornado_demix.errors import ApiError
from tornado_demix.heuristics import candidate_reason, ranked_candidates

RELAYER = "0x" + "cc" * 20
WINNER = "0x" + format(1, "040x")
OTHER = "0x" + format(2, "040x")
SEL = "21a0adb6"


def _word(h):
    return h.rjust(64, "0")


def _recipient(i):
    return "0x" + format(i, "040x")


def _tx(to, value_eth, ts, block, tx_hash):
    return {
        "from": fx.WALLET,
        "to": to,
        "value": str(int(value_eth * 10**18)),
        "timeStamp": str(ts),
        "blockNumber": str(block),
        "hash": tx_hash,
        "gasPrice": "1",
        "isError": "0",
    }


def _wlog(to, nullifier, relayer, fee_wei, ts, block):
    data = ("0" * 24 + to[2:]) + nullifier + format(fee_wei, "064x")
    return {
        "data": "0x" + data,
        "topics": [TOPIC_WITHDRAWAL, "0x" + "0" * 24 + relayer[2:]],
        "gasPrice": hex(1),
        "transactionHash": "0x" + nullifier,
        "blockNumber": hex(block),
        "timeStamp": hex(ts),
    }


def _dlog(leaf, tx_hash, block, ts):
    return {
        "data": "0x" + _word(format(leaf, "x")) + _word(format(ts, "x")),
        "topics": [TOPIC_DEPOSIT, "0x" + format(leaf + 1, "064x")],
        "transactionHash": tx_hash,
        "blockNumber": hex(block),
        "timeStamp": hex(ts),
    }


def _input(root, nullifier):
    return "0x" + SEL + _word("e0") + format(root, "064x") + nullifier + _word("0") * 3


class RootClient:
    """Fake explorer: the wallet's three 0.1 ETH deposits are leaves 10, 11, 12."""

    def __init__(self, withdrawal_roots, extra_txs=(), with_other=False, gap_withdrawal=False):
        # withdrawal_roots: {winner nullifier index k: root int}; others get root 999
        self.roots = withdrawal_roots
        self.tx_info_calls = []
        deposits = [
            _tx(fx.POOL_01_ETH, 0.1, 1000 + i * 10, 10 + i, "0xdep%d" % i) for i in range(3)
        ]
        self.txs = deposits + list(extra_txs)
        self.wlogs = []
        for k in range(3):
            self.wlogs.append(
                _wlog(WINNER, format(9000 + k, "064x"), ZERO_ADDRESS, 0, 1100 + k, 1100 + k)
            )
        if with_other:  # a second count-3 recipient: its discrimination depends on the field
            for k in range(3):
                self.wlogs.append(
                    _wlog(OTHER, format(9100 + k, "064x"), RELAYER, 10**15, 1100 + k, 1100 + k)
                )
        if gap_withdrawal:  # mined at block 5, before leaf 5 (the decoy's emulated note)
            self.wlogs.append(_wlog(WINNER, format(9003, "064x"), ZERO_ADDRESS, 0, 1100, 5))
        for i in range(3, 41):
            self.wlogs.append(_wlog(_recipient(i), format(i, "064x"), RELAYER, 10**15, 1100, 1100))
        self.dlogs = [_dlog(i, "0xold%d" % i, 1 + i, 900 + i) for i in range(10)]
        self.dlogs += [_dlog(10 + i, "0xdep%d" % i, 10 + i, 1000 + i * 10) for i in range(3)]
        self.dlogs += [_dlog(13 + i, "0xnew%d" % i, 20 + i, 1200 + i) for i in range(3)]
        self.dlogs.append(_dlog(16, "0xlast", 30, 1300))

    def internal_txs(self, addr):
        return []

    def outgoing_txs(self, addr):
        return self.txs

    def block_by_time(self, ts, closest="before"):
        return int(ts)

    def current_block(self):
        return 10_000

    def get_logs(self, address, topic0, start_block, end_block):
        src = self.dlogs if topic0 == TOPIC_DEPOSIT else self.wlogs
        return [log for log in src if start_block <= int(log["blockNumber"], 16) <= end_block]

    def tx_info(self, tx_hash):
        self.tx_info_calls.append(tx_hash)
        nullifier = tx_hash[2:]
        k = int(nullifier, 16) - 9000
        root = self.roots.get(k, 999)
        return {"input": _input(root, nullifier)}


def _index(monkeypatch, mapping, history=100, leaves=41):
    """Root index of the fake pool: ``leaves`` insertions, one per block from block 1."""
    roots = {"0x" + format(r, "064x"): n for r, n in mapping.items()}
    blocks = [1 + i for i in range(leaves)]
    monkeypatch.setattr(
        root_exclusion,
        "cached_root_index",
        lambda client, pool, end_block, cache_dir, chain=None: {"roots": roots, "blocks": blocks},
    )
    monkeypatch.setattr(
        root_exclusion, "root_history_size", lambda client, pool, cache_dir, chain=None: history
    )


def _linked_client(withdrawal_roots, **kwargs):
    # a plain transfer to WINNER makes it a direct counterparty: an early exit is a lead
    return RootClient(withdrawal_roots, extra_txs=[_tx(WINNER, 0.5, 1050, 15, "0xfund")], **kwargs)


def _addresses(data):
    return {r["address"] for r in ranked_candidates(data)}


def test_baseline_winner_is_a_candidate_without_root_check():
    client = _linked_client({})
    data = run_demix(client, fx.WALLET, network=fx.network())
    assert WINNER in _addresses(data)
    assert "root_excluded" not in data["heuristics"]
    assert client.tx_info_calls == []
    assert "root_check" not in data


def test_fully_excluded_candidate_leaves_the_ranking(monkeypatch):
    # every winner withdrawal proves against a root holding 5 leaves <= min leaf 10
    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501, 1: 501, 2: 501})
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert WINNER not in _addresses(data)
    assert data["denoms"]["0.1 ETH"]["candidates_by_count"][3] == []
    rows = [r for r in data["heuristics"]["root_excluded"] if r[1] == WINNER]
    assert len(rows) == 3
    pool_key, addr, tx_hash, n_root, max_leaf = rows[0]
    assert (pool_key, n_root, max_leaf) == ("0.1 ETH", 5, 12)
    assert tx_hash.startswith("0x")
    block = data["root_check"]["excluded_candidates"]
    assert [(b["pool_key"], b["address"]) for b in block] == [("0.1 ETH", WINNER)]
    assert data["root_check"]["incomplete"] is False


def test_partially_excluded_candidate_is_recounted(monkeypatch):
    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501})
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    rows = [r for r in ranked_candidates(data) if r["address"] == WINNER]
    assert len(rows) == 1
    assert rows[0]["hits"] == 2
    assert "root_excluded" in rows[0]["signals"]
    assert [r[1] for r in data["heuristics"]["root_excluded"]] == [WINNER]
    assert data["root_check"]["excluded_candidates"] == []
    # the lead itself is unchanged: root_excluded is context, not a score
    base = run_demix(_linked_client({}), fx.WALLET, network=fx.network())
    base_row = next(r for r in ranked_candidates(base) if r["address"] == WINNER)
    assert rows[0]["band"] == base_row["band"]


def test_exclusion_is_not_a_lead_and_not_scored():
    from tornado_demix.heuristics import CONTEXT_SIGNALS, LEAD_SIGNALS, SIGNAL_WEIGHTS

    assert "root_excluded" not in LEAD_SIGNALS
    assert "root_excluded" not in SIGNAL_WEIGHTS
    assert "root_excluded" not in CONTEXT_SIGNALS


def test_note_partially_excluded_is_context_only(monkeypatch):
    # n_root 11: holds leaf 10 but not 11, 12 -> notes with leaf >= 11 excluded; nothing removed
    _index(monkeypatch, {511: 11, 999: 40})
    client = _linked_client({0: 511, 1: 511, 2: 511})
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    row = next(r for r in ranked_candidates(data) if r["address"] == WINNER)
    assert row["hits"] == 3
    assert data["heuristics"]["root_excluded"] == []
    partial = data["heuristics"]["root_partial"]
    assert len(partial) == 3
    assert partial[0][3:] == (11, 12)
    detail = " ".join(e["detail"] for e in row["evidence"] if e["signal"] == "root_partial")
    assert "leaf >= 11" in detail
    assert "root_partial" in row["signals"] and "root_excluded" not in row["signals"]
    assert "not counted" not in candidate_reason(row)


def test_unknown_root_is_kept_and_counted(monkeypatch):
    _index(monkeypatch, {})
    client = _linked_client({0: 501, 1: 501, 2: 501})
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert WINNER in _addresses(data)
    stats = data["root_check"]
    assert stats["roots_found"] == 0 and stats["roots_not_found"] == 3


def test_call_limit_marks_the_check_incomplete(monkeypatch):
    _index(monkeypatch, {501: 5, 999: 40})
    monkeypatch.setattr(root_exclusion, "ROOT_CHECK_LIMIT", 2)
    client = _linked_client({0: 501, 1: 501, 2: 501})
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert len(client.tx_info_calls) <= 2
    assert data["root_check"]["incomplete"] is True
    assert data["root_check_incomplete"] is True
    # an unfinished candidate is never excluded
    assert WINNER in _addresses(data)


def test_deposit_without_a_leaf_skips_the_pool(monkeypatch):
    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501, 1: 501, 2: 501})
    client.dlogs = [log for log in client.dlogs if not log["transactionHash"].startswith("0xdep")]
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert WINNER in _addresses(data)
    assert data["root_check"]["incomplete"] is True
    assert "0.1 ETH" in data["root_check"]["unchecked_pools"]


def test_wallet_leaves_real_mode_reads_the_leaf_by_tx_hash():
    client = RootClient({})
    pool = fx.network().by_key["0.1 ETH"]
    deposits = [
        {"hash": "0xdep0", "block": 10, "ts": 1000},
        {"hash": "0xdep2", "block": 12, "ts": 1020},
    ]
    assert root_exclusion.wallet_leaves(client, pool, deposits, shifted=False) == [10, 12]


def test_wallet_leaves_decoy_mode_counts_insertions_before_the_shifted_block():
    client = RootClient({})
    pool = fx.network().by_key["0.1 ETH"]
    # shifted deposit at ts 5 -> block 5 (the fake maps ts to block); leaves 0..4 precede it
    shifted = [{"hash": "0xdep1", "block": 11, "ts": 5}]
    assert root_exclusion.wallet_leaves(client, pool, shifted, shifted=True) == [5]
    # a later shifted deposit sees the ten old leaves and the wallet's own three
    assert root_exclusion.wallet_leaves(
        client, pool, [{"hash": "0xdep1", "block": 11, "ts": 15}], shifted=True
    ) == [13]


def test_decoy_mode_uses_the_emulated_leaf_not_the_real_one(monkeypatch):
    # deposits shifted 995 s back: ts 5 / 15 / 25 -> emulated leaves 5 / 13 / 16, not 10 / 11 / 12
    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501, 1: 501, 2: 501})
    data = run_demix(
        client, fx.WALLET, network=fx.network(), root_check=True, deposit_shift_seconds=995
    )
    assert data["root_check"]["wallet_leaves"]["0.1 ETH"] == [5, 13, 16]


def test_root_check_off_adds_nothing_to_the_result():
    data = run_demix(_linked_client({}), fx.WALLET, network=fx.network(), root_check=False)
    assert "root_check" not in data and "root_check_incomplete" not in data
    assert set(data["heuristics"]) == {
        "deposit_gas_prices",
        "gas_price_matches",
        "linked_addresses",
        "linked_late",
        "linked_contracts",
        "linked_senders",
        "shared_deposits",
        "early_profiles",
    }
    res = data["denoms"]["0.1 ETH"]
    assert "field_counts" not in res and "root_excluded_detail" not in res
    assert res["total_qualifying_withdrawals"] == 41 and res["unique_recipients"] == 39
    assert res["signals"][WINNER] == ["count_match", "linked", "self_relayed"]
    assert [(r["address"], r["band"], r["hits"]) for r in ranked_candidates(data)] == [
        (WINNER, "moderate", 3)
    ]


@pytest.mark.parametrize(
    "n_root, expected",
    [(5, "excluded"), (10, "excluded"), (11, "partial"), (12, "partial"), (13, "kept")],
)
def test_classify_boundaries(n_root, expected):
    assert root_exclusion.classify(n_root, [10, 11, 12]) == expected


def _excluded_data(monkeypatch):
    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501, 1: 501, 2: 501})
    return run_demix(client, fx.WALLET, network=fx.network(), root_check=True)


def test_html_report_lists_the_excluded_candidate(monkeypatch):
    from tornado_demix.report_html import build_html_report

    html = build_html_report(_excluded_data(monkeypatch), fx.network())
    assert "Excluded by Merkle root" in html
    assert WINNER in html
    plain = build_html_report(
        run_demix(_linked_client({}), fx.WALLET, network=fx.network()), fx.network()
    )
    assert "Excluded by Merkle root" not in plain


def test_cli_has_a_root_check_flag():
    from tornado_demix.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["demix", fx.WALLET, "--root-check"]).root_check is True
    assert parser.parse_args(["demix", fx.WALLET]).root_check is False


def test_web_ui_passes_the_flag_and_shows_the_block(monkeypatch):
    from webui import app as webapp

    data = _excluded_data(monkeypatch)
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return data

    monkeypatch.setattr(webapp, "run_demix", fake_run)
    monkeypatch.setattr(webapp, "load_attribution", lambda name, *a, **k: {})
    blocks, _table, _reports = webapp._run_demix(
        object(), [fx.WALLET], fx.network(), window_days=30, mode="events", root_check=True
    )
    assert seen["root_check"] is True
    assert blocks[0]["root_check"]["excluded"][0]["address"] == WINNER
    with webapp.app.test_request_context():
        from flask import render_template

        page = render_template(
            "index.html",
            **{
                **webapp._base_context(),
                "result": {
                    "kind": "demix",
                    "blocks": blocks,
                    "n_wallets": 1,
                    "network": "ethereum",
                },
            },
        )
    assert 'name="root_check"' in page
    assert "Excluded by Merkle root" in page
    assert WINNER in page


def test_cmd_demix_passes_root_check_to_run_demix(monkeypatch, tmp_path):
    from tornado_demix import cli

    seen = {}
    data = {"wallet": fx.WALLET, "vouchers": [], "denoms": {}, "unresolved": [], "deposits": []}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return data

    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "run_demix", fake_run)
    parsed = cli.build_parser().parse_args(
        ["demix", fx.WALLET, "--root-check", "--cache-dir", str(tmp_path)]
    )
    cli.cmd_demix(parsed)
    assert seen["root_check"] is True
    assert seen["root_cache_dir"] == str(tmp_path / "merkle")


def test_web_post_passes_root_check(monkeypatch):
    from tests import test_webui
    from webui import app as webapp

    seen = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return [], [], {}

    monkeypatch.setenv("ETHERSCAN_API_KEY", "FAKE-KEY-FOR-TESTS")
    monkeypatch.setattr(webapp, "EtherscanClient", test_webui._QuietClient)
    monkeypatch.setattr(webapp, "_run_demix", fake)
    client = webapp.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf"] = "t"
    client.post("/", data={"wallets": fx.WALLET, "csrf": "t", "root_check": "1"})
    assert seen.get("root_check") is True


def test_decoy_withdrawal_in_the_gap_is_not_checked(monkeypatch):
    # emulated note 5 is inserted at block 6; the block-5 withdrawal (root n=5) predates it
    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501, 3: 501}, gap_withdrawal=True)
    data = run_demix(
        client, fx.WALLET, network=fx.network(), root_check=True, deposit_shift_seconds=995
    )
    rc = data["root_check"]
    assert rc["decoy_gap_skipped"] == 1
    excluded = data["heuristics"]["root_excluded"]
    assert len(excluded) == 1 and excluded[0][3] == 5  # the later one is still excluded
    assert "0x" + format(9003, "064x") not in [e[2] for e in excluded]


def test_removal_leaves_other_candidates_unchanged(monkeypatch):
    _index(monkeypatch, {501: 5, 999: 40})
    base = run_demix(_linked_client({}, with_other=True), fx.WALLET, network=fx.network())
    data = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}, with_other=True),
        fx.WALLET,
        network=fx.network(),
        root_check=True,
    )
    assert WINNER not in _addresses(data)
    b = base["denoms"]["0.1 ETH"]
    d = data["denoms"]["0.1 ETH"]
    for key in ("signals", "confidence", "discrimination"):
        assert d[key][OTHER] == b[key][OTHER]
    assert d["unique_recipients"] == b["unique_recipients"]
    assert d["total_qualifying_withdrawals"] == b["total_qualifying_withdrawals"]
    old = next(r for r in ranked_candidates(base) if r["address"] == OTHER)
    new = next(r for r in ranked_candidates(data) if r["address"] == OTHER)
    assert (new["band"], new["confidence"], new["discrimination"]) == (
        old["band"],
        old["confidence"],
        old["discrimination"],
    )


def test_partly_excluded_candidate_that_loses_its_count_match_is_listed(monkeypatch):
    _index(monkeypatch, {501: 5, 999: 40})
    client = RootClient({0: 501})  # no link: admitted by the count match alone
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert WINNER not in _addresses(data)
    dropped = data["root_check"]["dropped_after_recount"]
    assert [(d["address"], d["remaining"]) for d in dropped] == [(WINNER, 2)]
    assert data["root_check"]["excluded_candidates"] == []
    from tornado_demix.report_html import build_html_report

    assert "Dropped after recount" in build_html_report(data, fx.network())


def _multicall_input(first_root, real_root, nullifier):
    """A wrapper call: a no-op first call carrying [root][nullifier], then the withdrawal."""
    decoy = "00000000" + format(first_root, "064x") + nullifier
    real = SEL + _word("e0") + format(real_root, "064x") + nullifier + _word("0") * 3
    return "0x" + "82ad56cb" + _word("20") + _word("2") + decoy + real


class MulticallClient(RootClient):
    """Every winner withdrawal is wrapped; first call carries ``first``, the real one 999."""

    def __init__(self, first, **kwargs):
        super().__init__({}, **kwargs)
        self.first = first

    def tx_info(self, tx_hash):
        self.tx_info_calls.append(tx_hash)
        nullifier = tx_hash[2:]
        return {"input": _multicall_input(self.first, 999, nullifier)}


def test_multicall_forged_old_root_does_not_exclude(monkeypatch):
    # old root 501 (5 leaves) is 36 insertions behind: outside a history of 30; the real
    # root 999 holds 40 leaves. The forged first occurrence must not exclude the withdrawal.
    _index(monkeypatch, {501: 5, 999: 40}, history=30)
    client = MulticallClient(501, extra_txs=[_tx(WINNER, 0.5, 1050, 15, "0xfund")])
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert WINNER in _addresses(data)
    assert data["heuristics"]["root_excluded"] == []
    assert data["root_check"]["roots_found"] == 3


def test_two_valid_candidates_leave_the_root_unknown(monkeypatch):
    # both roots are inside a history of 100: which one proved the withdrawal is unknown
    _index(monkeypatch, {501: 5, 999: 40}, history=100)
    client = MulticallClient(501, extra_txs=[_tx(WINNER, 0.5, 1050, 15, "0xfund")])
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    assert WINNER in _addresses(data)
    rc = data["root_check"]
    assert rc["roots_ambiguous"] == 3 and rc["roots_found"] == 0
    assert data["heuristics"]["root_excluded"] == []


def test_root_older_than_the_history_is_unknown_not_excluded(monkeypatch):
    # 5 leaves, 41 inserted: 36 behind. History 100 accepts it (excluded), history 30 does not.
    _index(monkeypatch, {501: 5, 999: 40}, history=100)
    wide = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}), fx.WALLET, network=fx.network(), root_check=True
    )
    assert len(wide["heuristics"]["root_excluded"]) == 3
    _index(monkeypatch, {501: 5, 999: 40}, history=30)
    narrow = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}), fx.WALLET, network=fx.network(), root_check=True
    )
    assert narrow["heuristics"]["root_excluded"] == []
    assert WINNER in _addresses(narrow)
    assert narrow["root_check"]["roots_outside_history"] == 3
    assert narrow["root_check"]["history_sizes"] == {"0.1 ETH": 30}


def _unreadable(client, pool, cache_dir, chain=None):
    raise ApiError("eth_call not answered")


def test_history_size_unreadable_leaves_the_pool_unchecked_with_an_error_record(monkeypatch):
    # no default is substituted: a too-small guess would reject a real older root
    _index(monkeypatch, {501: 5, 999: 40})
    monkeypatch.setattr(root_exclusion, "root_history_size", _unreadable)
    data = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}), fx.WALLET, network=fx.network(), root_check=True
    )
    rc = data["root_check"]
    assert data["heuristics"]["root_excluded"] == []
    assert rc["unchecked_pools"] == ["0.1 ETH"] and rc["incomplete"] is True
    assert rc["history_sizes"] == {} and "history_default" not in rc
    notes = [e for e in data["errors"] if e["what"].startswith("ROOT_HISTORY_SIZE")]
    assert len(notes) == 1 and notes[0]["kind"] == "lookup_failed"
    assert notes[0]["what"] == "ROOT_HISTORY_SIZE" and notes[0]["address"]
    assert data["error_count"] == len(data["errors"]) >= 1


def test_no_default_history_size_exists():
    assert not hasattr(root_exclusion, "DEFAULT_HISTORY_SIZE")


def test_failed_tree_and_transaction_reads_are_recorded_in_errors(monkeypatch):
    def no_tree(client, pool, end_block, cache_dir, chain=None):
        raise ApiError("log list has a gap")

    monkeypatch.setattr(root_exclusion, "cached_root_index", no_tree)
    data = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}), fx.WALLET, network=fx.network(), root_check=True
    )
    tree = [e for e in data["errors"] if e["what"] == "merkle tree"]
    assert len(tree) == 1 and "gap" in tree[0]["error"]

    _index(monkeypatch, {501: 5, 999: 40})
    client = _linked_client({0: 501, 1: 501, 2: 501})

    def failing(tx_hash):
        raise ApiError("transaction {} not read".format(tx_hash))

    client.tx_info = failing
    data = run_demix(client, fx.WALLET, network=fx.network(), root_check=True)
    txs = [e for e in data["errors"] if e["what"] == "root check transaction"]
    assert len(txs) == 3 and all(e["tx"].startswith("0x") for e in txs)


def test_cache_without_insertion_blocks_is_unchecked(monkeypatch):
    monkeypatch.setattr(
        root_exclusion,
        "cached_root_index",
        lambda client, pool, end_block, cache_dir, chain=None: {"roots": {}},
    )
    data = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}), fx.WALLET, network=fx.network(), root_check=True
    )
    assert data["root_check"]["unchecked_pools"] == ["0.1 ETH"]


def test_history_sizes_and_unknown_causes_are_shown_in_every_view(monkeypatch, capsys):
    from tornado_demix import cli
    from tornado_demix.report_html import build_html_report
    from webui import app as webapp

    _index(monkeypatch, {501: 5, 999: 40}, history=30)
    data = run_demix(
        _linked_client({0: 501, 1: 501, 2: 501}), fx.WALLET, network=fx.network(), root_check=True
    )
    html = build_html_report(data, fx.network())
    assert "ROOT_HISTORY_SIZE: 0.1 ETH 30" in html and "outside the pool's root history 3" in html
    view = webapp._root_check_view(data, fx.network())
    assert view["history"] == "0.1 ETH 30" and view["outside_history"] == 3
    assert data["root_check"]["roots_outside_history"] == 3  # JSON export carries the counts
    monkeypatch.setattr(cli.config, "load_api_key", lambda path: "stub-key")
    monkeypatch.setattr(cli, "EtherscanClient", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "run_demix", lambda *a, **kw: data)
    cli.cmd_demix(cli.build_parser().parse_args(["demix", fx.WALLET, "--root-check"]))
    out = capsys.readouterr().out
    assert "[0.1 ETH] ROOT_HISTORY_SIZE 30" in out and "outside the pool's root history 3" in out
