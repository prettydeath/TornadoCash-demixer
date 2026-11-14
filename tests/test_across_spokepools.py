"""tools/across_spokepools.py: the destination SpokePools of the bridge table against
the Across hub contract's crossChainContracts(chainId), offline over a fake RPC."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import across_spokepools  # noqa: E402

from tornado_demix.bridges import load_bridges  # noqa: E402

HUB = "0xc186fa914353c44b2e33ebe05f21846f1048beda"
ADAPTER = "0x" + "aa" * 20


class FakeRpc:
    """Answers crossChainContracts(id) from {chain id: spoke pool}; records the calls."""

    def __init__(self, spokes, fail=()):
        self.spokes, self.fail, self.calls = spokes, set(fail), []

    def eth_call(self, to, data):
        self.calls.append((to, data))
        chain = int(data[10:], 16)
        if chain in self.fail:
            return None
        spoke = self.spokes.get(chain)
        if spoke is None:
            return "0x" + "00" * 64
        return "0x" + ADAPTER[2:].rjust(64, "0") + spoke[2:].rjust(64, "0")


def test_selector_is_cross_chain_contracts_uint256():
    assert across_spokepools.SELECTOR == "0x09474ae2"
    try:
        from Crypto.Hash import keccak
    except ImportError:
        return
    digest = keccak.new(digest_bits=256, data=b"crossChainContracts(uint256)").hexdigest()
    assert across_spokepools.SELECTOR == "0x" + digest[:8]


def test_the_shipped_table_agrees_with_the_hub_answers_of_2026_11_13():
    table = load_bridges()
    answers = {
        1: "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5",
        10: "0x6f26bf09b1c792e3228e5467807a900a503c0281",
        56: "0x4e8e101924ede233c13e2d8622dc8aed2872d505",
        137: "0x9295ee1d8c5b022be115a2ad3c30c72e34e7f096",
        8453: "0x09aea4b2242abc8bb4bb78d537a67a245a7bec64",
        42161: "0xe35e9842fceaca96570b734083f4a58e8f7c5f2a",
        43114: "0xfe9d541c92e4e90437c7152a00244886de37a658",
    }
    rpc = FakeRpc(answers)
    rows = across_spokepools.compare(rpc, table, [1, 10, 56, 100, 137, 8453, 42161, 43114])
    by_chain = {r["chain_id"]: r for r in rows}
    assert {c for c, r in by_chain.items() if r["status"] == "ok"} == set(answers)
    assert by_chain[100]["status"] == "not on the hub"
    assert all(to == HUB for to, _d in rpc.calls)
    assert rpc.calls[0][1] == "0x09474ae2" + "{:064x}".format(1)


def test_a_new_or_changed_pool_and_a_failed_call_are_reported():
    table = load_bridges()
    moved = "0x" + "bb" * 20
    rpc = FakeRpc({10: moved, 59144: "0x" + "cc" * 20}, fail={56})
    rows = {r["chain_id"]: r for r in across_spokepools.compare(rpc, table, [10, 56, 59144])}
    assert rows[10]["status"] == "differs" and rows[10]["hub"] == moved
    assert rows[10]["table"] == ["0x6f26bf09b1c792e3228e5467807a900a503c0281"]
    assert rows[56]["status"] == "call failed"
    assert rows[59144]["status"] == "missing from the table"
    assert across_spokepools.csv_row(rows[59144], "2026-11-13").startswith(
        "59144,0x" + "cc" * 20 + ',across,"SpokePool (crossChainContracts(59144) on 2026-11-13)"'
    )


def test_main_exits_non_zero_on_a_difference(capsys):
    table = load_bridges()
    ok = across_spokepools.main(
        ["--chains", "10"], rpc=FakeRpc({10: "0x" + "bb" * 20}), table=table
    )
    assert ok == 1
    assert "differs" in capsys.readouterr().out
    same = FakeRpc({10: "0x6f26bf09b1c792e3228e5467807a900a503c0281"})
    assert across_spokepools.main(["--chains", "10"], rpc=same, table=table) == 0
