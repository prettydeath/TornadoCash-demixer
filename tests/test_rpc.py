"""JSON-RPC helper: selectors, decoding, and the EIP-1559 gate."""

import pytest

from tornado_demix import rpc


class FakeSession:
    """Stands in for requests.Session, returning queued JSON payloads."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        payload = self.payloads.pop(0)

        class Response:
            def json(self_inner):
                return payload

        return Response()


def _client(payloads):
    client = rpc.RpcClient("https://rpc.example.test")
    client.session = FakeSession(payloads)
    return client


def _keccak256():
    """Return a keccak-256 function, or None if no implementation is installed.

    The package's runtime dependency is `requests` alone, so no keccak library
    may be added for this. Where one happens to be available the selectors are
    derived; where it is not, only the pinning test below runs.
    """
    try:
        from Crypto.Hash import keccak as _pycryptodome

        return lambda data: _pycryptodome.new(digest_bits=256, data=data).digest()
    except ImportError:
        pass
    try:
        import sha3  # pysha3

        return lambda data: sha3.keccak_256(data).digest()
    except ImportError:
        pass
    try:
        from eth_hash.auto import keccak as _eth_hash

        return _eth_hash
    except ImportError:
        return None


def test_selectors_match_keccak256_of_the_signature():
    """Derive each selector rather than asserting the constant against itself.

    A typo in SELECTORS silently rejects every pool: eth_call to a wrong
    4-byte selector reverts, call_uint returns None, and verify_via_rpc reports
    "not a Tornado pool" for a contract that is one. Nothing else in the suite
    would notice.
    """
    keccak256 = _keccak256()
    if keccak256 is None:
        pytest.skip("no keccak-256 implementation available in this environment")
    for name, selector in rpc.SELECTORS.items():
        signature = "{}()".format(name).encode("ascii")
        assert selector == "0x" + keccak256(signature)[:4].hex(), name


def test_selectors_are_pinned_against_accidental_edits():
    """A change-detector, deliberately.

    These four literals are the values verified against mainnet pool
    0x47CE0C6e on 2026-08-15. This test does not prove they are the right
    keccak prefixes - test_selectors_match_keccak256_of_the_signature does
    that, where a keccak library exists. It only ensures nobody edits them by
    hand without meaning to.
    """
    assert rpc.SELECTORS == {
        "denomination": "0x8bca6d16",
        "token": "0xfc0c546a",
        "levels": "0x4ecf518b",
        "nextIndex": "0xfc7e9c6f",
    }


def test_call_uint_decodes_a_word():
    client = _client([{"result": "0x" + format(10**17, "064x")}])
    assert client.call_uint("0x" + "1" * 40, rpc.SELECTORS["denomination"]) == 10**17


def test_call_address_returns_none_for_the_zero_word():
    client = _client([{"result": "0x" + "0" * 64}])
    assert client.call_address("0x" + "1" * 40, rpc.SELECTORS["token"]) is None


def test_call_address_decodes_a_padded_address():
    word = "0" * 24 + "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    client = _client([{"result": "0x" + word}])
    assert client.call_address("0x" + "1" * 40, rpc.SELECTORS["token"]) == (
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    )


def test_call_returns_none_on_rpc_error():
    client = _client(
        [
            {"error": {"message": "execution reverted"}},
            {"error": {"message": "execution reverted"}},
            {"error": {"message": "execution reverted"}},
        ]
    )
    assert client.eth_call("0x" + "1" * 40, rpc.SELECTORS["denomination"]) is None


def test_base_fee_absent_means_pre_1559():
    client = _client([{"result": {"number": "0xc5d488"}}])
    assert client.base_fee(12964999) is None


def test_base_fee_present_is_returned():
    client = _client([{"result": {"number": "0xc5d489", "baseFeePerGas": "0x3b9aca00"}}])
    assert client.base_fee(12965000) == 10**9


def test_base_fee_zero_is_distinct_from_absent():
    """BSC reports baseFeePerGas = 0x0 - present but zero."""
    client = _client([{"result": {"number": "0x1", "baseFeePerGas": "0x0"}}])
    assert client.base_fee(1) == 0


def test_base_fee_is_unknown_when_the_call_fails():
    """A failed lookup must not look like a pre-EIP-1559 block."""
    client = _client([{"error": {"message": "boom"}}])
    assert client.base_fee(12965000) is rpc.UNKNOWN


def test_base_fee_is_unknown_when_the_value_is_unparseable():
    client = _client([{"result": {"number": "0x1", "baseFeePerGas": "not-hex"}}])
    assert client.base_fee(1) is rpc.UNKNOWN


def test_unknown_disables_the_gas_price_heuristic():
    """Failing open would run a pre-London heuristic on unknown data."""
    assert rpc.user_chosen_gas_price(rpc.UNKNOWN) is False


@pytest.mark.parametrize(
    "base_fee,expected",
    [
        (None, True),  # pre-London: gasPrice is entirely user-chosen
        (0, True),  # BSC: base fee is zero, so gasPrice is user-chosen
        (1, False),  # any non-zero base fee dominates the effective price
        (10**9, False),
    ],
)
def test_user_chosen_gas_price_gate(base_fee, expected):
    assert rpc.user_chosen_gas_price(base_fee) is expected


# The EIP-1559 gate: the unique-gas-price signal may only be credited where the
# sender chose the gas price.
@pytest.mark.parametrize(
    "base_fee, allowed",
    [
        pytest.param(None, True, id="pre-london block"),
        pytest.param(0, True, id="zero base fee (BSC)"),
        pytest.param(30_000_000_000, False, id="real base fee"),
        pytest.param("unknown", False, id="failed lookup"),
    ],
)
def test_the_gate_allows_only_a_sender_chosen_gas_price(monkeypatch, base_fee, allowed):
    from tornado_demix import rpc

    value = rpc.UNKNOWN if base_fee == "unknown" else base_fee

    class Client:
        def base_fee(self, block):
            return value

    monkeypatch.setattr(rpc, "RpcClient", lambda url: Client())
    assert rpc.make_gas_price_gate("https://rpc.example")(1) is allowed


def test_without_an_rpc_url_the_signal_is_disabled_not_assumed():
    from tornado_demix.rpc import make_gas_price_gate

    said = []
    gate = make_gas_price_gate("", log=said.append)
    assert gate(12345) is False
    assert said and "disabled" in said[0]


def test_the_gate_asks_each_block_once(monkeypatch):
    from tornado_demix import rpc

    class Client:
        def __init__(self):
            self.asked = []

        def base_fee(self, block):
            self.asked.append(block)
            return None

    client = Client()
    monkeypatch.setattr(rpc, "RpcClient", lambda url: client)
    gate = rpc.make_gas_price_gate("https://rpc.example")
    gate(7), gate(7), gate(8)
    assert client.asked == [7, 8]


def test_a_gated_gas_price_match_is_not_credited():
    """End to end through apply_heuristics: the gate must actually bite."""
    from tornado_demix.heuristics import apply_heuristics

    data = {
        "deposits": [{"gas_price": 42}],
        "denoms": {
            "1 ETH": {
                "counts": {"0xa": 1},
                "unique_recipients": 1,
                "target_counts": [1],
                "detail": {"0xa": [{"gas_price": 42, "hash": "0xw", "block": 99}]},
            }
        },
    }
    apply_heuristics(data, set(), gas_gate=lambda block: False)
    assert data["denoms"]["1 ETH"]["signals"]["0xa"] == []

    apply_heuristics(data, set(), gas_gate=lambda block: True)
    assert "gas_price" in data["denoms"]["1 ETH"]["signals"]["0xa"]


def test_contract_check_reads_eth_getcode(monkeypatch):
    from tornado_demix import rpc

    class Client:
        def call(self, method, params):
            assert method == "eth_getCode"
            return {"0xc": "0x6080", "0xe": "0x"}.get(params[0])

    monkeypatch.setattr(rpc, "RpcClient", lambda url: Client())
    check = rpc.make_contract_check("https://rpc.example")
    assert check("0xc") is True
    assert check("0xe") is False
    assert check("0xfailed") is None


def test_contract_check_without_an_endpoint_is_unknown():
    from tornado_demix.rpc import make_contract_check

    assert make_contract_check("")("0xabc") is None
