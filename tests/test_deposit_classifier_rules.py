"""Deposit-address classifier: spoof resistance, sponsored (EIP-7702) sweeps, sender rule,
kinds and names, per-network gas-funder threshold. Synthetic addresses only."""

import pytest

from tornado_demix import deposit_addresses as da
from tornado_demix.deposit_addresses import (
    FEW_SENDERS,
    MAX_DEPOSIT_SENDERS,
    MAX_DEPOSIT_TXS,
    classify_deposit_address,
    depositor_deposit_addresses,
    kind_label,
    shared_deposit_hits,
    target_label,
)

W = "0x" + "a" * 40  # depositor
EXIT = "0x" + "b" * 40
X = "0x" + "c" * 40  # deposit address
HOT = "0x" + "d" * 40
HOT2 = "0x" + "e" * 40
USDC = "0x" + "1" * 40
FAKE = "0x" + "f" * 40
SPONSOR = "0x" + "3" * 40
FUNDER = "0x" + "5" * 40
LAB = {HOT: {"category": "exchange", "entity": "Binance", "source": "etherscan-labels"}}


def tx(frm, to, value=10**18, ts=1000, h=None):
    return {
        "from": frm,
        "to": to,
        "value": str(value),
        "timeStamp": str(ts),
        "blockNumber": str(ts // 2),
        "hash": h or f"0x{abs(hash((frm, to, value, ts))) % (1 << 64):016x}",
    }


def tok(frm, to, value=10**8, ts=1000, h=None, contract=USDC):
    row = tx(frm, to, value, ts, h)
    row["contractAddress"] = contract
    return row


class Client:
    def __init__(self, lists, busy=(), infos=None):
        self.lists, self.busyset, self.infos = lists, set(busy), infos or {}
        self.calls, self.info_calls = [], []

    def call(self, p):
        self.calls.append((p["action"], p["address"]))
        return self.lists.get((p["action"], p["address"]), [])

    def has_at_least_txs(self, a, n):
        return a in self.busyset

    def tx_info(self, h):
        self.info_calls.append(h)
        return self.infos.get(h)


def eoa(_a):
    return False


def deposit(n=3, target=HOT, senders=(W,), sweep=True):
    """Each sender pays X in USDC; X sweeps each payment itself 600 s later."""
    txl, tkl = [], []
    for i, s in enumerate(senders * n if len(senders) == 1 else senders):
        t = 10_000 + i * 100_000
        tkl.append(tok(s, X, 10**8, t, h=f"0xin{i:04d}"))
        if sweep:
            txl.append(tx(X, USDC, 0, t + 600, h=f"0xsw{i:04d}"))
            tkl.append(tok(X, target, 10**8, t + 600, h=f"0xsw{i:04d}"))
    return {("txlist", X): txl, ("tokentx", X): tkl}


# --- requirement 3: spoofs and sponsored sweeps -------------------------------------


def test_spoofed_transfers_from_the_depositor_do_not_displace_a_real_deposit_address():
    looks = [f"0x{0xD00D000 + i:040x}" for i in range(30)]
    wallet_rows = [
        tok(W, a, 1, 100 + i, h=f"0xsp{i:03d}", contract=FAKE) for i, a in enumerate(looks * 2)
    ]
    wallet_rows.append(tx(W, X, 10**18, 50, h="0xreal"))
    client = Client(deposit())
    found = depositor_deposit_addresses(client, W, wallet_rows, eoa, LAB)
    assert [f["address"] for f in found] == [X]
    assert not any(a in looks for _action, a in client.calls)


def test_a_token_transfer_the_depositor_really_sent_is_a_counterparty():
    sent = [tx(W, USDC, 0, 50, h="0xmine"), tok(W, X, 10**8, 50, h="0xmine")]
    client = Client(deposit())
    found = depositor_deposit_addresses(client, W, sent, eoa, LAB)
    assert [f["address"] for f in found] == [X]


def test_eip7702_sponsored_sweeps_listed_in_txlist_are_the_address_s_own():
    txl, tkl = [], []
    for i in range(4):
        t = 10_000 + i * 100_000
        tkl.append(tok(W, X, 10**8, t, h=f"0xin{i}"))
        txl.append(tx(SPONSOR, X, 0, t + 600, h=f"0xsp{i}"))  # tx.to == X, sent by a sponsor
        tkl.append(tok(X, HOT, 10**8, t + 600, h=f"0xsp{i}"))
    client = Client({("txlist", X): txl, ("tokentx", X): tkl})
    info = classify_deposit_address(client, X, eoa, LAB, lambda a: False)
    assert info["kind"] == "exchange_deposit" and info["evidence"] is True
    assert info["senders"] == [W]


def test_eip7702_sponsored_sweeps_found_by_the_transaction_target():
    tkl, infos = [], {}
    for i in range(4):
        t = 10_000 + i * 100_000
        tkl.append(tok(W, X, 10**8, t, h=f"0xin{i}"))
        tkl.append(tok(X, HOT, 10**8, t + 600, h=f"0xsp{i}"))
        infos[f"0xsp{i}"] = {"from": SPONSOR, "to": X}
    client = Client({("txlist", X): [], ("tokentx", X): tkl}, infos=infos)
    info = classify_deposit_address(client, X, eoa, LAB, lambda a: False)
    assert info is not None and info["kind"] == "exchange_deposit"
    assert info["sponsored"] is True


def test_fake_token_and_zero_value_senders_are_not_senders():
    lists = deposit()
    for i in range(MAX_DEPOSIT_SENDERS + 5):
        spoofer = f"0x{0xBAD000 + i:040x}"
        lists[("tokentx", X)].append(tok(spoofer, X, 1000, 20_000_000 + i, contract=FAKE))
        lists[("tokentx", X)].append(tok(spoofer, X, 0, 20_000_000 + i))
    lists[("tokentx", X)].append(tok(EXIT, X, 5 * 10**21, 30_000_000, contract=FAKE))
    info = classify_deposit_address(Client(lists), X, eoa, LAB, lambda a: False)
    assert info is not None and info["senders"] == [W]
    assert shared_deposit_hits([info], {EXIT}, W) == {}


def test_a_transfer_after_the_last_sweep_of_its_asset_is_not_a_sender():
    lists = deposit()
    lists[("tokentx", X)].append(tok(EXIT, X, 10**8, 90_000_000))  # never swept
    info = classify_deposit_address(Client(lists), X, eoa, LAB, lambda a: False)
    assert info["senders"] == [W]


def test_a_poisoner_batch_contract_does_not_make_a_victim_a_forwarder():
    lists = {
        ("txlist", X): [],
        ("tokentx", X): [
            tok(W, X, 10**8, 1000, h="0xreal_in"),
            tok(X, HOT2, 10**8, 2000, h="0xp1", contract=FAKE),
            tok(X, HOT2, 10**8, 3000, h="0xp2", contract="0x" + "8" * 40),
        ],
    }
    batch = "0x" + "6" * 40
    infos = {"0xp1": {"from": SPONSOR, "to": batch}, "0xp2": {"from": SPONSOR, "to": batch}}
    info = classify_deposit_address(Client(lists, infos=infos), X, eoa, None, lambda a: False)
    assert info is None


# --- requirement 5: sender count ----------------------------------------------------


def many(n, share_to_hot=1.0, labels=LAB):
    senders = tuple(f"0x{0x5E0000 + i:040x}" for i in range(n))
    lists = deposit(senders=senders)
    # move part of the sweeps to other, quiet destinations
    sweeps = [r for r in lists[("tokentx", X)] if r["from"] == X]
    moved = round(len(sweeps) * (1 - share_to_hot))
    for i, r in enumerate(sweeps[:moved]):
        r["to"] = f"0x{0x7A0000 + i:040x}"
    return lists, senders


def test_an_exchange_deposit_address_with_a_few_hundred_one_off_senders_is_admitted():
    lists, senders = many(142)
    info = classify_deposit_address(Client(lists), X, eoa, LAB, lambda a: False)
    assert info["kind"] == "exchange_deposit" and len(info["senders"]) == 142


def test_more_than_max_senders_is_a_hub():
    lists, _ = many(MAX_DEPOSIT_SENDERS + 1)
    assert classify_deposit_address(Client(lists), X, eoa, LAB, lambda a: False) is None


def test_a_busy_hub_that_pays_out_elsewhere_is_rejected_but_a_small_one_is_not():
    lists, _ = many(120, share_to_hot=0.85)
    assert classify_deposit_address(Client(lists), X, eoa, LAB, lambda a: False) is None
    lists, _ = many(FEW_SENDERS, share_to_hot=0.85)
    info = classify_deposit_address(Client(lists), X, eoa, LAB, lambda a: False)
    assert info is not None and info["kind"] == "exchange_deposit"


# --- requirement 7: kinds and names -------------------------------------------------


def test_an_unlabelled_busy_target_is_deposit_like_not_exchange():
    info = classify_deposit_address(Client(deposit(), busy={HOT}), X, eoa, None, {HOT}.__contains__)
    assert info["kind"] == "deposit_like" and info["service"] == "unknown"
    assert info["evidence"] is False and info["exchange"] == ""
    for text in (kind_label(info), target_label(info)):
        assert "exchange" not in text.lower()
    # still context: listed with evidence_only=False, never as evidence
    info["senders"] = [W, EXIT]
    assert shared_deposit_hits([info], {EXIT}, W) == {}
    assert shared_deposit_hits([info], {EXIT}, W, evidence_only=False) == {EXIT: [X]}


def test_a_target_labelled_non_exchange_is_a_service_deposit():
    lab = {HOT: {"category": "gambling", "entity": "somecasino", "source": "vendor"}}
    info = classify_deposit_address(Client(deposit()), X, eoa, lab, {HOT}.__contains__)
    assert info["kind"] == "service_deposit" and info["service"] == "somecasino"
    assert "exchange" not in kind_label(info).lower()


def test_exchange_names_are_case_folded_and_deduplicated():
    labs = {
        HOT: {"category": "exchange", "entity": "binance", "source": "etherscan-labels"},
        HOT2: {"category": "exchange", "entity": "Binance", "source": "blockscout"},
    }
    lists = deposit(4)
    for i, r in enumerate([r for r in lists[("tokentx", X)] if r["from"] == X]):
        r["to"] = HOT if i % 2 else HOT2
    info = classify_deposit_address(Client(lists), X, eoa, labs, lambda a: False)
    assert info["exchange"] == "Binance"


def test_a_cut_token_history_is_flagged():
    tkl, txl = [], []
    for i in range(MAX_DEPOSIT_TXS // 2):
        t = 10_000 + i * 1000
        tkl.append(tok(W, X, 10**8, t, h=f"0xin{i}"))
        tkl.append(tok(X, HOT, 10**8, t + 60, h=f"0xsw{i}"))
        txl.append(tx(X, USDC, 0, t + 60, h=f"0xsw{i}"))
    info = classify_deposit_address(
        Client({("txlist", X): txl[:900], ("tokentx", X): tkl}), X, eoa, LAB, lambda a: False
    )
    assert info["incomplete"] is True
    assert "cut" in kind_label(info)


def test_a_counterparty_labelled_as_a_deposit_address_is_examined():
    labs = dict(LAB)
    labs[X] = {
        "category": "exchange",
        "entity": "shapeshift",
        "label": "ShapeShift: Binance Deposit",
    }
    client = Client(deposit())
    found = depositor_deposit_addresses(client, W, [tx(W, X)], eoa, labs)
    assert [f["address"] for f in found] == [X]
    labs[X] = {"category": "exchange", "entity": "binance", "label": "Binance: Hot Wallet"}
    client = Client(deposit())
    assert depositor_deposit_addresses(client, W, [tx(W, X)], eoa, labs) == []
    assert ("txlist", X) not in client.calls


# --- requirement 4: gas funder per network ------------------------------------------


def gas(value):
    lists = deposit()
    lists[("txlist", X)].insert(0, tx(FUNDER, X, value, 5_000, h="0xgas"))
    return lists


def test_gas_funder_threshold_is_a_parameter():
    pol = 16 * 10**17  # 1.6 POL top-up
    info = classify_deposit_address(Client(gas(pol)), X, eoa, LAB, lambda a: False)
    assert info["gas_funder"] is None  # default 0.01 coin
    info = classify_deposit_address(
        Client(gas(pol)), X, eoa, LAB, lambda a: False, gas_fund_wei=2 * 10**18
    )
    assert info["gas_funder"] == FUNDER
    assert FUNDER not in info["senders"]  # native gas is not swept: not a sender


def test_every_registry_network_has_a_gas_funder_threshold():
    from tornado_demix.networks import GAS_FUND_WEI, load_networks

    nets = load_networks()
    for net in nets.values():
        assert net.gas_fund_wei == GAS_FUND_WEI.get(net.chain_id, da.GAS_FUND_WEI) > 0
        assert net.chain_id in GAS_FUND_WEI, net.name
    assert nets["polygon"].gas_fund_wei == 2 * 10**18
    assert nets["ethereum"].gas_fund_wei == 10**16


def test_depositor_scan_passes_the_network_threshold():
    client = Client(gas(16 * 10**17))
    found = depositor_deposit_addresses(client, W, [tx(W, X)], eoa, LAB, gas_fund_wei=2 * 10**18)
    assert found[0]["gas_funder"] == FUNDER


@pytest.mark.parametrize("kind", ["exchange_deposit", "deposit_like", "service_deposit"])
def test_kind_label_names(kind):
    text = kind_label({"kind": kind, "service": "unknown"})
    assert ("exchange" in text) == (kind == "exchange_deposit")


def test_only_the_sponsored_transactions_actually_read_are_taken_as_own():
    tkl, infos = [], {}
    for i in range(5):
        t = 10_000 + i * 100_000
        tkl.append(tok(W, X, 10**8, t, h=f"0xin{i}"))
        # the first three are read (MAX_EXECUTOR_LOOKUPS) and are self-calls; the last two
        # are not read and must not be taken as the address's own
        tkl.append(tok(X, HOT if i < 3 else HOT2, 10**8, t + 600, h=f"0xsp{i}"))
        infos[f"0xsp{i}"] = {"from": SPONSOR, "to": X}
    client = Client({("txlist", X): [], ("tokentx", X): tkl}, infos=infos)
    info = classify_deposit_address(client, X, eoa, LAB, lambda a: False)
    assert info["kind"] == "exchange_deposit" and info["sweep_targets"] == [HOT]
    assert len(client.info_calls) == 3
