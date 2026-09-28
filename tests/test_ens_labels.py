"""ENS-labelled pairs: the offline part of tools/ens_labels.py."""

import os
import sys

import pytest

pytest.importorskip("Crypto", reason="the labelling tool needs pycryptodome")
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import ens_labels  # noqa: E402


def _a(i):
    return "0x" + format(i, "040x")


DEP, REC, OTHER, SERVICE = _a(1), _a(2), _a(3), _a(99)


@pytest.fixture(autouse=True)
def _no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ens_labels, "CACHE", str(tmp_path))


def test_namehash_matches_the_ens_reference_values():
    assert ens_labels.namehash("").hex() == "00" * 32
    assert (
        ens_labels.namehash("eth").hex()
        == "93cdeb708b7545dc668eb9280176169d1c33cfd8ed6f04690a0bcc88a93fc4ae"
    )
    assert (
        ens_labels.namehash("foo.eth").hex()
        == "de9b09fd7c5f901e23a3f19fecc54828e9c848539801e86591bd9801b019f84f"
    )


def _universe(deposit_ts=1000, withdrawal_ts=2000):
    return {
        "deposits": [["1 ETH", DEP, deposit_ts, "0xd"]],
        "withdrawals": [["1 ETH", REC, withdrawal_ts, "0xw"], ["1 ETH", OTHER, 3000, "0xo"]],
    }


def test_a_depositor_controlling_the_recipients_name_is_a_pair():
    names = {REC: "bob.eth", DEP: ""}
    ctrl = {"bob.eth": [DEP]}
    (pair,) = ens_labels.build_pairs(_universe(), names, ctrl)
    assert (pair["depositor"], pair["recipient"], pair["pool_key"]) == (DEP, REC, "1 ETH")
    assert pair["reasons"] == ["controls the other's primary name"]
    assert pair["days_between"] == round(1000 / 86400, 1)


def test_names_under_one_second_level_name_link_the_addresses():
    names = {DEP: "alice.eth", REC: "hot.alice.eth"}
    (pair,) = ens_labels.build_pairs(_universe(), names, {})
    assert pair["reasons"] == ["names under one .eth name (alice.eth)"]


def test_a_withdrawal_before_the_deposit_is_not_a_pair():
    names = {REC: "bob.eth"}
    assert ens_labels.build_pairs(_universe(5000, 2000), names, {"bob.eth": [DEP]}) == []


def test_a_subdomain_service_links_nobody():
    users = [_a(10 + i) for i in range(ens_labels.MAX_SHARED_PARENT + 1)]
    names = {u: f"u{i}.wallet.eth" for i, u in enumerate(users)}
    names[DEP] = "d.wallet.eth"
    names[REC] = "r.wallet.eth"
    assert ens_labels.build_pairs(_universe(), names, {}) == []


def test_an_owner_of_many_names_is_a_service_not_a_person():
    names = {_a(10 + i): f"n{i}.eth" for i in range(ens_labels.MAX_SHARED_PARENT + 1)}
    names[REC] = "bob.eth"
    ctrl = {n: [DEP] for n in names.values()}
    assert ens_labels.build_pairs(_universe(), names, ctrl) == []


def test_evaluation_counts_recall_precision_and_the_linked_share():
    import evaluate_labels

    def data(signals_by_addr):
        res = {
            "denom": 1.0,
            "asset": "ETH",
            "counts": {a: 1 for a in signals_by_addr},
            "detail": {
                a: [{"value": 1.0, "ts": 5, "self_relayed": False}] for a in signals_by_addr
            },
            "signals": {a: s for a, s in signals_by_addr.items()},
            "confidence": {a: 0.4 for a in signals_by_addr},
            "discrimination": {a: 1.0 for a in signals_by_addr},
            "unique_recipients": 10,
        }
        return {"denoms": {"1 ETH": res}}

    results = {DEP: data({REC: ["linked"], OTHER: ["gas_price"]})}
    pairs = [{"pool_key": "1 ETH", "depositor": DEP, "recipient": REC}]
    full = evaluate_labels.evaluate(results, pairs, drop_linked=False)
    assert full["reachable"] == 1 and full["found_with_linked"] == 1
    assert full["moderate"]["found"] == 1 and full["moderate"]["candidates"] == 2
    assert full["moderate"]["precision_lower_bound"] == 0.5
    bare = evaluate_labels.evaluate(results, pairs, drop_linked=True)
    assert bare["weak"]["found"] == 0 and bare["moderate"]["candidates"] == 1
