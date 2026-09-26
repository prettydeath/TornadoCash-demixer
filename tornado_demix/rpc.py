"""Minimal JSON-RPC client for on-chain verification.

Two jobs, both needing no API key:

* Pool verification. ``denomination()``, ``token()``, ``levels()`` and
  ``nextIndex()`` prove that an address is a fixed-denomination Tornado pool and
  say which asset it takes, in one call per getter.

* The EIP-1559 probe for the unique-gas-price heuristic. After London the
  ``gasPrice`` on a log is base fee plus tip, mostly a property of the block, so
  unrelated wallets match by chance. The test is per block: ``baseFeePerGas``
  absent (pre-London) or zero (BSC reports 0x0) means the sender chose the price.
  A failed lookup or a missing endpoint disables the signal rather than assuming
  it holds.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

# keccak256(signature)[:4], precomputed so the package needs no keccak library.
# A wrong selector makes every genuine pool look like "not a Tornado pool";
# tests/test_rpc.py re-derives them wherever a keccak implementation is installed.
SELECTORS = {
    "denomination": "0x8bca6d16",
    "token": "0xfc0c546a",
    "levels": "0x4ecf518b",
    "nextIndex": "0xfc7e9c6f",
}

ZERO_WORD = "0" * 64


class _Unknown:
    """Sentinel: the base fee could not be determined."""

    def __repr__(self):
        return "UNKNOWN"


# Returned by base_fee() when the lookup failed. Distinct from None (a genuine
# pre-EIP-1559 block, where the sender chose the gas price).
UNKNOWN = _Unknown()


class RpcClient:
    """A tiny JSON-RPC 2.0 client over HTTP."""

    def __init__(self, url: str, timeout: float = 25, retries: int = 3) -> None:
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()

    def call(self, method: str, params: list) -> Any:
        """Return the ``result`` field, or None on error after retries."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(self.retries):
            try:
                payload = self.session.post(self.url, json=body, timeout=self.timeout).json()
            except (OSError, ValueError):
                time.sleep(0.6 * (attempt + 1))
                continue
            if isinstance(payload, dict) and "error" in payload:
                return None
            if isinstance(payload, dict):
                return payload.get("result")
        return None

    def eth_call(self, to: str, data: str) -> str | None:
        """Perform an eth_call at the latest block. Returns hex str or None."""
        result = self.call("eth_call", [{"to": to, "data": data}, "latest"])
        if not result or result == "0x":
            return None
        return result

    def call_uint(self, to: str, selector: str) -> int | None:
        """Call a no-argument getter returning a uint. Returns int or None."""
        result = self.eth_call(to, selector)
        if not result:
            return None
        try:
            return int(result, 16)
        except ValueError:
            return None

    def call_address(self, to: str, selector: str) -> str | None:
        """Call a no-argument getter returning an address.

        Returns a lowercased address, or None when the call fails or the
        returned word is zero (which is how a native pool answers token()).
        """
        result = self.eth_call(to, selector)
        if not result:
            return None
        word = result[2:].rjust(64, "0")[-64:]
        if word == ZERO_WORD:
            return None
        return "0x" + word[-40:]

    def base_fee(self, block_number: int) -> int | None | _Unknown:
        """Return the block's baseFeePerGas as an int.

        * ``None``    - the field is absent: the block predates EIP-1559.
        * ``0``       - present and zero (BSC); the sender still chose the price.
        * an ``int``  - a real base fee dominates the effective price.
        * ``UNKNOWN`` - the lookup failed.
        """
        block = self.call("eth_getBlockByNumber", [hex(int(block_number)), False])
        if not isinstance(block, dict):
            return UNKNOWN
        raw = block.get("baseFeePerGas")
        if raw is None:
            return None
        try:
            return int(raw, 16)
        except (TypeError, ValueError):
            return UNKNOWN


def user_chosen_gas_price(base_fee: int | None | _Unknown) -> bool:
    """True when a block's effective gas price is chosen by the sender.

    ``base_fee`` is the value returned by :meth:`RpcClient.base_fee`.
    :data:`UNKNOWN` is neither ``None`` nor ``0``, so a failed lookup is False.
    """
    return base_fee is None or base_fee == 0


def make_contract_check(rpc_url: str) -> Callable[[str], bool | None]:
    """Return ``check(address) -> True | False | None`` via ``eth_getCode``.

    None means unknown (no endpoint, or the lookup failed). Memoised per address.
    """
    if not rpc_url:
        return lambda address: None
    client = RpcClient(rpc_url)
    cache = {}

    def check(address):
        if address not in cache:
            code = client.call("eth_getCode", [address, "latest"])
            cache[address] = None if not isinstance(code, str) else code not in ("0x", "0x0")
        return cache[address]

    return check


def make_gas_price_gate(
    rpc_url: str, log: Callable[[str], None] | None = None
) -> Callable[[int], bool]:
    """Return ``gate(block_number) -> bool`` for the unique-gas-price heuristic.

    The gate answers, per block, whether the heuristic's assumption (the sender
    chose the gas price) holds; see :func:`user_chosen_gas_price`. With no
    ``rpc_url`` it answers False for everything and says so once.

    Results are memoised per block. Only withdrawals that already matched a
    deposit's gas price are asked about, so a run touches few blocks.
    """
    if not rpc_url:
        if log:
            log(
                "[!] no RPC endpoint for this network: the unique-gas-price "
                "heuristic is disabled (it cannot be validated post-EIP-1559)"
            )
        return lambda block_number: False

    client = RpcClient(rpc_url)
    cache = {}

    def gate(block_number):
        if not block_number:
            return False
        if block_number not in cache:
            cache[block_number] = user_chosen_gas_price(client.base_fee(block_number))
        return cache[block_number]

    return gate
