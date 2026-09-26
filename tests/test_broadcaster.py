"""Who actually broadcast a self-relayed withdrawal.

The report calls a self-relayed exit "recipient paid own gas" and, in the
docs, "a direct, non-probabilistic link". The code never established either.
``withdraw()`` may be called by anyone, so with no relayer named in the proof
the sender is an inference until the transaction itself is read.
"""

from tornado_demix.relayer import verify_broadcaster

RECIPIENT = "0x" + "a" * 40
STRANGER = "0x" + "b" * 40


def _lead(tx_hash="0xw1", to=RECIPIENT):
    return {
        "to": to,
        "tx_hash": tx_hash,
        "self_relayed": True,
        "broadcaster": None,
        "broadcaster_status": "unverified",
    }


class Client:
    def __init__(self, senders):
        self.senders = senders
        self.calls = []

    def tx_sender(self, tx_hash):
        self.calls.append(tx_hash)
        return self.senders.get(tx_hash)


def test_a_sender_that_is_the_recipient_is_confirmed_self():
    leads = [_lead()]
    client = Client({"0xw1": RECIPIENT})
    assert verify_broadcaster(client, leads) == 1
    assert leads[0]["broadcaster_status"] == "self"
    assert leads[0]["broadcaster"] == RECIPIENT


def test_a_different_sender_is_reported_as_other_not_dropped():
    """A funded third party is a lead in its own right."""
    leads = [_lead()]
    assert verify_broadcaster(Client({"0xw1": STRANGER}), leads) == 1
    assert leads[0]["broadcaster_status"] == "other"
    assert leads[0]["broadcaster"] == STRANGER


def test_case_differences_do_not_make_a_stranger():
    leads = [_lead(to=RECIPIENT.upper())]
    verify_broadcaster(Client({"0xw1": RECIPIENT}), leads)
    assert leads[0]["broadcaster_status"] == "self"


def test_a_provider_that_cannot_serve_the_tx_leaves_it_unverified():
    leads = [_lead()]
    assert verify_broadcaster(Client({}), leads) == 0
    assert leads[0]["broadcaster_status"] == "unverified"
    assert leads[0]["broadcaster"] is None


def test_a_client_without_tx_sender_is_not_an_error():
    leads = [_lead()]
    assert verify_broadcaster(object(), leads) == 0
    assert leads[0]["broadcaster_status"] == "unverified"


def test_one_lookup_per_transaction_however_many_leads_share_it():
    leads = [_lead("0xw1"), _lead("0xw1"), _lead("0xw2")]
    client = Client({"0xw1": RECIPIENT, "0xw2": STRANGER})
    verify_broadcaster(client, leads)
    assert client.calls == ["0xw1", "0xw2"]


def test_the_lookup_count_is_capped():
    leads = [_lead("0xw%d" % i) for i in range(10)]
    client = Client({"0xw%d" % i: RECIPIENT for i in range(10)})
    assert verify_broadcaster(client, leads, limit=3) == 3
    assert len(client.calls) == 3
    assert leads[5]["broadcaster_status"] == "unverified"


def test_a_lead_with_no_tx_hash_is_skipped():
    leads = [{"to": RECIPIENT, "broadcaster_status": "unverified"}]
    assert verify_broadcaster(Client({}), leads) == 0
