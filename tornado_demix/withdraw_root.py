"""Merkle root a Tornado withdrawal was proven against, read from its calldata.

Both the pool's ``withdraw(bytes, bytes32 _root, bytes32 _nullifierHash, ...)`` and the
router/proxy variants put ``_root`` as a static word right before ``_nullifierHash``,
so the contract ABI is not decoded: the 32-byte nullifier (from the ``Withdrawal``
event) is searched at any byte offset after the 4-byte selector and the 64 hex chars
before it are the root. Byte offsets, not only word boundaries, because wrappers such
as Multicall3 embed the inner call in a ``bytes`` argument, shifting it by the inner
4-byte selector. A random 256-bit value matching by chance is negligible, so the match
is safe. Every occurrence yields a candidate root, because a wrapper may carry the
nullifier in a call that is not the withdrawal (a Multicall with a no-op call holding
``[old root][nullifier]``): the caller accepts a candidate only after checking it
against the pool's root history (``root_exclusion``), and treats zero or several
valid candidates as unknown.
"""

from __future__ import annotations


def candidate_roots(input_hex: str, nullifier: str) -> list[int]:
    """Every distinct 32-byte word right before the nullifier at any byte offset.

    In order of appearance. An occurrence with fewer than 64 hex chars before it has
    no root and adds nothing.
    """
    body = (input_hex or "")[2:].lower()[8:]
    target = nullifier[2:].lower().rjust(64, "0")
    found: list[int] = []
    i = body.find(target)
    while i != -1:
        if i % 2 == 0 and i >= 64:
            root = int(body[i - 64 : i], 16)
            if root not in found:
                found.append(root)
        i = body.find(target, i + 1)
    return found


def root_from_input(input_hex: str, nullifier: str) -> int | None:
    """The root when exactly one candidate precedes the nullifier, else None."""
    found = candidate_roots(input_hex, nullifier)
    return found[0] if len(found) == 1 else None


def withdrawal_candidate_roots(client, withdrawal: dict) -> list[int]:
    """Candidate roots of ``withdrawal`` (a ``decode_withdrawal`` dict) from its transaction input."""
    info = client.tx_info(withdrawal["tx_hash"])
    if not isinstance(info, dict):
        return []
    return candidate_roots(info.get("input") or "", withdrawal["nullifier"])


def withdrawal_root(client, withdrawal: dict) -> int | None:
    """The only candidate root of ``withdrawal``; None when there is none or several."""
    found = withdrawal_candidate_roots(client, withdrawal)
    return found[0] if len(found) == 1 else None
