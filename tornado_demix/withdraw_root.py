"""Merkle root a Tornado withdrawal was proven against, read from its calldata.

Both the pool's ``withdraw(bytes, bytes32 _root, bytes32 _nullifierHash, ...)`` and the
router/proxy variants put ``_root`` as a static word right before ``_nullifierHash``,
so the contract ABI is not decoded: the 32-byte nullifier (from the ``Withdrawal``
event) is searched at any byte offset after the 4-byte selector and the 64 hex chars
before it are the root. Byte offsets, not only word boundaries, because wrappers such
as Multicall3 embed the inner call in a ``bytes`` argument, shifting it by the inner
4-byte selector. A random 256-bit value matching by chance is negligible, so the match
is safe. None when the nullifier is absent or fewer than 64 hex chars precede it.
"""

from __future__ import annotations


def root_from_input(input_hex: str, nullifier: str) -> int | None:
    """Root (64 hex chars) preceding the nullifier at any byte offset, or None."""
    body = (input_hex or "")[2:].lower()[8:]
    target = nullifier[2:].lower().rjust(64, "0")
    i = body.find(target)
    while i != -1:
        if i % 2 == 0:
            return int(body[i - 64 : i], 16) if i >= 64 else None
        i = body.find(target, i + 1)
    return None


def withdrawal_root(client, withdrawal: dict) -> int | None:
    """Root of ``withdrawal`` (a ``decode_withdrawal`` dict) via its transaction input."""
    info = client.tx_info(withdrawal["tx_hash"])
    if not isinstance(info, dict):
        return None
    return root_from_input(info.get("input") or "", withdrawal["nullifier"])
