"""Withdrawal/Deposit log decoding, against hand-built ABI payloads."""

from tornado_demix.events import decode_deposit, decode_withdrawal

RECIPIENT = "0x1111111111111111111111111111111111111111"
RELAYER = "0x2222222222222222222222222222222222222222"
NULLIFIER = "ab" * 32


def _withdrawal_log(to=RECIPIENT, relayer=RELAYER, fee=10**16):
    data = ("0" * 24 + to[2:]) + NULLIFIER + format(fee, "064x")
    return {
        "data": "0x" + data,
        "topics": ["0xtopic0", "0x" + "0" * 24 + relayer[2:]],
        "gasPrice": "0x3b9aca00",
        "transactionHash": "0xwithdrawal",
        "blockNumber": "0x64",
        "timeStamp": "0x5f5e100",
    }


def test_recipient_comes_from_the_first_word():
    """Reading the adjacent word instead returns the nullifier as an address."""
    assert decode_withdrawal(_withdrawal_log())["to"] == RECIPIENT


def test_nullifier_comes_from_the_second_word():
    assert decode_withdrawal(_withdrawal_log())["nullifier"] == "0x" + NULLIFIER


def test_fee_comes_from_the_third_word_unconverted():
    assert decode_withdrawal(_withdrawal_log(fee=10**16))["fee_wei"] == 10**16


def test_relayer_comes_from_the_indexed_topic():
    assert decode_withdrawal(_withdrawal_log())["relayer"] == RELAYER


def test_block_and_timestamp_are_decoded_from_hex():
    w = decode_withdrawal(_withdrawal_log())
    assert w["block"] == 100
    assert w["ts"] == 100000000


def test_gas_price_is_decoded_from_hex():
    assert decode_withdrawal(_withdrawal_log())["gas_price"] == 10**9


def test_a_zero_relayer_is_self_relayed():
    log = _withdrawal_log(relayer="0x" + "0" * 40)
    assert decode_withdrawal(log)["self_relayed"] is True


def test_a_named_relayer_taking_no_fee_is_not_self_relayed():
    """The relayer paid the gas; the recipient is tied to nothing.

    Folding this into self_relayed labelled such withdrawals "recipient paid
    own gas", which is false, and promoted them to the strongest lead the tool
    reports.
    """
    decoded = decode_withdrawal(_withdrawal_log(fee=0))
    assert decoded["self_relayed"] is False
    assert decoded["zero_fee_relayed"] is True


def test_no_relayer_is_self_relayed_whatever_the_fee():
    zero = "0x" + "0" * 40
    assert decode_withdrawal(_withdrawal_log(relayer=zero, fee=0))["self_relayed"] is True
    assert decode_withdrawal(_withdrawal_log(relayer=zero))["self_relayed"] is True


def test_a_broadcaster_starts_out_explicitly_unverified():
    """An unchecked lead must never read as a checked one."""
    decoded = decode_withdrawal(_withdrawal_log(relayer="0x" + "0" * 40))
    assert decoded["broadcaster"] is None
    assert decoded["broadcaster_status"] == "unverified"


def test_a_relayed_withdrawal_is_not_self_relayed():
    assert decode_withdrawal(_withdrawal_log())["self_relayed"] is False


def test_a_missing_relayer_topic_does_not_raise():
    log = _withdrawal_log()
    log["topics"] = ["0xtopic0"]
    assert decode_withdrawal(log)["self_relayed"] is True


def test_deposit_decodes_commitment_and_leaf_index():
    log = {
        "data": "0x" + format(7, "064x") + format(1700000000, "064x"),
        "topics": ["0xtopic0", "0x" + "cd" * 32],
        "transactionHash": "0xdeposit",
        "blockNumber": "0x1",
        "timeStamp": "0x2",
    }
    d = decode_deposit(log)
    assert d["leaf_index"] == 7
    assert d["commitment"] == "0x" + "cd" * 32
