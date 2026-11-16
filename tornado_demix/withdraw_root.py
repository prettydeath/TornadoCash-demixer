"""Merkle root a Tornado withdrawal was proven against, read from its decoded calldata.

The root is the ``_root`` argument of the ``withdraw`` call that reached the pool:

* the pool's ``withdraw(bytes _proof, bytes32 _root, bytes32 _nullifierHash, address,
  address, uint256, uint256)`` (selector ``0x21a0adb6``) called on the pool itself;
* the router/proxy ``withdraw(address _tornado, bytes _proof, bytes32 _root, ...)``
  (``0xb438689f``) called on a router the network declares, with ``_tornado`` = the pool;
* either of them inside a Multicall wrapper (Multicall3 ``aggregate``, ``aggregate3``,
  ``aggregate3Value``, ``tryAggregate``, ``blockAndAggregate``, ``tryBlockAndAggregate``;
  Multicall2 uses the same selectors) or a self-call ``multicall(bytes[])``, decoded
  recursively (at most :data:`MAX_DEPTH` levels) with the sub-call's own target.

A decoded call counts only when its ``_nullifierHash`` is the withdrawal's nullifier.

A call inside a frame that lets it fail without reverting the transaction (``aggregate3`` /
``aggregate3Value`` with ``allowFailure`` true, ``tryAggregate`` / ``tryBlockAndAggregate``
with ``requireSuccess`` false, ``multicall(bytes[])`` of an unknown contract, a wrapper
selector called on an address that is not a known Multicall deployment of the chain
(``data/multicalls.csv``: Multicall3 on every chain, Multicall2 and Multicall on Ethereum),
since any contract can answer to that selector and ignore failures, or any frame nested in
one) may have reverted while the real withdrawal ran elsewhere, so its root is not
used: with only such calls the result is ``unverified`` and the root unknown. A call in a
strict frame must have succeeded, and only one withdrawal per nullifier can succeed, so one
distinct strict root is the root (``source`` ``decoded``); several are ``ambiguous``.

Decoding is bounded: at most :data:`MAX_INPUT_BYTES` of calldata, :data:`MAX_VISITED_CALLS`
calls and :data:`MAX_DEPTH` times the input size of sliced sub-call bytes per read. Wrapper
offsets may all point at one element, so without a budget the work would grow as the call
count to the power of the depth; past the budget the result is ``malformed``, root unknown. When no call decodes, the old adjacency search (the 32-byte word before every
byte-aligned occurrence of the nullifier, :func:`candidate_roots`) is kept only as a
diagnostic: its result is ``adjacency`` and the root stays unknown, because a wrapper may
place an older root right before the nullifier in a call that is not the withdrawal and
build the real call elsewhere. The caller never excludes a withdrawal on an unknown root.
"""

from __future__ import annotations

import csv
import os

MULTICALLS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "multicalls.csv")
# Deployed with the same deterministic address on every chain (github.com/mds1/multicall).
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
_MULTICALL_ROWS: list[dict] | None = None


def known_multicall_rows() -> list[dict]:
    """Rows of ``data/multicalls.csv`` (chain_id, address, name, source_url)."""
    global _MULTICALL_ROWS
    if _MULTICALL_ROWS is None:
        with open(MULTICALLS_CSV, newline="", encoding="utf-8") as fh:
            _MULTICALL_ROWS = [
                {k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)
            ]
        for row in _MULTICALL_ROWS:
            row["address"] = row["address"].lower()
    return _MULTICALL_ROWS


def known_multicalls(chain_id) -> set[str]:
    """Multicall addresses whose batch frames are trusted as written on ``chain_id``.

    Multicall3 always (deterministic address); other deployments only on the chain the
    data file lists them for, since the same address may hold another contract elsewhere.
    """
    out = {MULTICALL3}
    if chain_id is None:
        return out
    for row in known_multicall_rows():
        if str(row["chain_id"]) == str(chain_id):
            out.add(row["address"])
    return out


POOL_WITHDRAW = "21a0adb6"  # withdraw(bytes,bytes32,bytes32,address,address,uint256,uint256)
ROUTER_WITHDRAW = (
    "b438689f"  # withdraw(address,bytes,bytes32,bytes32,address,address,uint256,uint256)
)
# selector -> (head word of the call array, fields per tuple, field holding the bytes offset,
# failure tolerance). Field 0 = target; None fields: a bytes[] of calls to the wrapper's own
# target. Tolerance: ("field", i) = per-call allowFailure flag in tuple field i; ("arg", i) =
# head word i is requireSuccess (tolerant when false); "always"; None = strict (reverts).
WRAPPERS = {
    "252dba42": (0, 2, 1, None),  # aggregate((address,bytes)[])
    "c3077fa9": (0, 2, 1, None),  # blockAndAggregate((address,bytes)[])
    "bce38bd7": (1, 2, 1, ("arg", 0)),  # tryAggregate(bool,(address,bytes)[])
    "399542e9": (1, 2, 1, ("arg", 0)),  # tryBlockAndAggregate(bool,(address,bytes)[])
    "82ad56cb": (0, 3, 2, ("field", 1)),  # aggregate3((address,bool,bytes)[])
    "174dea71": (0, 4, 3, ("field", 1)),  # aggregate3Value((address,bool,uint256,bytes)[])
    "ac9650d8": (0, None, None, "always"),  # multicall(bytes[]): semantics of an unknown contract
}
MAX_DEPTH = 4
MAX_INPUT_BYTES = 128 * 1024
MAX_VISITED_CALLS = 2048
# Sub-calls one wrapper may list; a longer array is treated as malformed.
_MAX_CALLS = 1024


class _Malformed(ValueError):
    pass


class _Overflow(Exception):
    """The decoding budget of one read is spent: the whole read is malformed."""


class _Budget:
    def __init__(self, size: int):
        self.calls = 0
        self.bytes = 0
        self.max_bytes = MAX_DEPTH * size + 1024

    def visit(self):
        self.calls += 1
        if self.calls > MAX_VISITED_CALLS:
            raise _Overflow("calls")

    def take(self, n: int):
        self.bytes += n
        if self.bytes > self.max_bytes:
            raise _Overflow("bytes")


def _word(data: bytes, pos: int) -> int:
    if pos < 0 or pos + 32 > len(data):
        raise _Malformed(pos)
    return int.from_bytes(data[pos : pos + 32], "big")


def _address(data: bytes, pos: int) -> str:
    value = _word(data, pos)
    if value >> 160:
        raise _Malformed(pos)
    return "0x{:040x}".format(value)


def _bytes_at(data: bytes, pos: int) -> bytes:
    n = _word(data, pos)
    if n > len(data) - pos - 32:
        raise _Malformed(pos)
    return data[pos + 32 : pos + 32 + n]


def _sub_calls(args: bytes, target: str, spec, budget, trusted) -> list[tuple[str, bytes, bool]]:
    """``(target, calldata, may fail silently)`` of each call listed by a wrapper.

    ``trusted``: the wrapper runs at a known Multicall address, so its flags mean what the
    ABI says; otherwise every sub-call may have failed silently.
    """
    head, fields, data_field, tolerance = spec
    frame_tolerant = (
        not trusted
        or tolerance == "always"
        or (
            isinstance(tolerance, tuple)
            and tolerance[0] == "arg"
            and _word(args, 32 * tolerance[1]) == 0
        )
    )
    start = _word(args, head * 32)
    n = _word(args, start)
    if n > _MAX_CALLS:
        raise _Malformed(start)
    base = start + 32
    out = []
    for i in range(n):
        budget.visit()
        at = base + _word(args, base + 32 * i)
        if fields is None:
            data = _bytes_at(args, at)
            budget.take(len(data))
            out.append((target, data, frame_tolerant))
            continue
        data = _bytes_at(args, at + _word(args, at + 32 * data_field))
        budget.take(len(data))
        tolerant = frame_tolerant
        if isinstance(tolerance, tuple) and tolerance[0] == "field":
            tolerant = tolerant or _word(args, at + 32 * tolerance[1]) != 0
        out.append((_address(args, at), data, tolerant))
    return out


def _withdraw_roots(call, target, nullifier, pool, routers, depth, tolerant, budget, multicalls):
    """``(root, may have failed)`` of the matching withdraw calls in ``call`` (itself or
    nested in wrappers)."""
    budget.visit()
    selector, args = call[:4].hex(), call[4:]
    try:
        if selector == POOL_WITHDRAW and target == pool:
            if _word(args, 64) == nullifier:
                return [(_word(args, 32), tolerant)]
            return []
        if selector == ROUTER_WITHDRAW and target in routers:
            if _address(args, 0) == pool and _word(args, 96) == nullifier:
                return [(_word(args, 64), tolerant)]
            return []
        spec = WRAPPERS.get(selector)
        if spec is None or depth >= MAX_DEPTH:
            return []
        calls = _sub_calls(args, target, spec, budget, target in multicalls)
    except _Malformed:
        return []
    found = []
    for sub_target, sub_call, sub_tolerant in calls:
        found += _withdraw_roots(
            sub_call,
            sub_target,
            nullifier,
            pool,
            routers,
            depth + 1,
            tolerant or sub_tolerant,
            budget,
            multicalls,
        )
    return found


def candidate_roots(input_hex: str, nullifier: str) -> list[int]:
    """Every distinct 32-byte word right before the nullifier at any byte offset.

    The adjacency search, a diagnostic only (see the module text). In order of appearance;
    an occurrence with fewer than 64 hex chars before it adds nothing.
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


def read_root(
    input_hex: str, nullifier: str, to: str | None, pool: str, routers=(), multicalls=None
) -> dict:
    """``{"root", "source", "candidates"}`` for a withdrawal of ``pool`` with ``nullifier``.

    ``to`` is the transaction's target. ``source`` is ``decoded`` (``root`` set),
    ``ambiguous`` (several strict decoded roots, listed in ``candidates``), ``unverified``
    (matching calls only in frames that may have failed silently), ``malformed`` (over the
    decoding budget), ``adjacency`` (no withdraw call decoded; ``candidates`` =
    :func:`candidate_roots`) or ``none``. Every source but ``decoded`` leaves ``root`` None.
    ``multicalls`` are the trusted Multicall addresses (default: :func:`known_multicalls`
    without a chain, Multicall3 only).
    """
    if not isinstance(nullifier, str) or not nullifier.startswith("0x"):
        return {"root": None, "source": "none", "candidates": []}
    pool = (pool or "").lower()
    routers = {r.lower() for r in routers or ()}
    multicalls = known_multicalls(None) if multicalls is None else {m.lower() for m in multicalls}
    if len(input_hex or "") > 2 + 2 * MAX_INPUT_BYTES:
        return {"root": None, "source": "malformed", "candidates": []}
    try:
        call = bytes.fromhex((input_hex or "")[2:])
        wanted = int(nullifier[2:], 16)
    except ValueError:
        call, wanted = b"", None
    found = []
    if to and isinstance(to, str) and wanted is not None:
        try:
            found = _withdraw_roots(
                call, to.lower(), wanted, pool, routers, 0, False, _Budget(len(call)), multicalls
            )
        except _Overflow:
            return {"root": None, "source": "malformed", "candidates": []}
    strict: list[int] = []
    loose: list[int] = []
    for root, tolerant in found:
        bucket = loose if tolerant else strict
        if root not in bucket:
            bucket.append(root)
    if len(strict) == 1:
        return {"root": strict[0], "source": "decoded", "candidates": strict}
    if strict:
        return {"root": None, "source": "ambiguous", "candidates": strict}
    if loose:
        return {"root": None, "source": "unverified", "candidates": loose}
    near = candidate_roots(input_hex, nullifier)
    return {"root": None, "source": "adjacency" if near else "none", "candidates": near}


def withdrawal_read(client, withdrawal: dict, pool: str, routers=(), multicalls=None) -> dict:
    """:func:`read_root` of ``withdrawal`` (a ``decode_withdrawal`` dict) from its transaction."""
    info = client.tx_info(withdrawal["tx_hash"])
    if not isinstance(info, dict):
        return {"root": None, "source": "none", "candidates": []}
    return read_root(
        info.get("input") or "",
        withdrawal["nullifier"],
        info.get("to"),
        pool,
        routers,
        multicalls,
    )


def withdrawal_root(client, withdrawal: dict, pool: str, routers=(), multicalls=None):
    """The decoded root of ``withdrawal`` (int); None when it is not decoded or ambiguous."""
    return withdrawal_read(client, withdrawal, pool, routers, multicalls)["root"]
