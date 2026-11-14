"""Tornado.Cash Merkle tree: MiMCSponge hash, zero values, incremental roots.

Build time, measured 2026-11-12 on a Windows 11 workstation, CPython 3.13.7, pure Python:
one ``hash_left_right`` takes ~0.83 ms and an insert needs 20 of them (~17 ms per deposit).
The Ethereum 1 ETH pool, the largest by deposits (97,587), was built in 29 min 27 s
including the log fetch. Estimates from on-chain ``nextIndex``: 0.1 ETH pool (76,572
deposits) ~23 min, 10 ETH pool (79,699) ~24 min. ``cached_root_index`` makes later runs
incremental.
"""

from __future__ import annotations

import json
import os
from bisect import bisect_left, bisect_right
from functools import lru_cache

from .constants import TOPIC_DEPOSIT
from .errors import ApiError
from .events import decode_deposit

FIELD = 21888242871839275222246405745257275088548364400416034343698204186575808495617
# tornado-core: uint256(keccak256("tornado")) % FIELD_SIZE (same on Ethereum and Polygon pools)
ZERO_VALUE = 21663839004416932945382355908790599225266501822907911457504978515578255421292
LEVELS = 20
ROOT_HISTORY_SIZE_SELECTOR = "0xcd87a3b4"  # ROOT_HISTORY_SIZE()

_CONSTANTS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "mimcsponge_constants.txt"
)


@lru_cache(maxsize=1)
def _constants() -> tuple[int, ...]:
    with open(_CONSTANTS_PATH, encoding="utf-8") as fh:
        cts = tuple(int(line) for line in fh.read().split())
    if len(cts) != 220:
        raise ValueError("mimcsponge_constants.txt must hold 220 constants")
    return cts


def mimc_sponge(xl: int, xr: int, k: int = 0) -> tuple[int, int]:
    """circomlib MiMCSponge permutation (220 rounds, exponent 5) over the BN254 field."""
    cts = _constants()
    n = len(cts)
    for i in range(n):
        t = (xl + k) % FIELD if i == 0 else (xl + k + cts[i]) % FIELD
        t5 = pow(t, 5, FIELD)
        if i < n - 1:
            xl, xr = (xr + t5) % FIELD, xl
        else:
            xr = (xr + t5) % FIELD
    return xl, xr


def hash_left_right(left: int, right: int) -> int:
    """The pool's node hash: two MiMCSponge calls with k = 0."""
    if not (0 <= left < FIELD and 0 <= right < FIELD):
        raise ValueError("value outside the field")
    r, c = mimc_sponge(left, 0)
    r = (r + right) % FIELD
    r, c = mimc_sponge(r, c)
    return r


@lru_cache(maxsize=None)
def zeros(level: int) -> int:
    """Root of an empty subtree of height ``level`` (the contract's ``zeros(level)``)."""
    return ZERO_VALUE if level == 0 else hash_left_right(zeros(level - 1), zeros(level - 1))


def _hex(value: int) -> str:
    return "0x{:064x}".format(value)


class IncrementalTree:
    """The pool's append-only Merkle tree (tornado-core ``filledSubtrees`` algorithm)."""

    def __init__(self) -> None:
        self.size = 0
        self._filled = [zeros(level) for level in range(LEVELS)]
        self.root = zeros(LEVELS)

    def insert(self, leaf: int) -> int:
        """Append ``leaf`` and return the new root."""
        if self.size >= 2**LEVELS:
            raise ValueError("tree is full")
        index = self.size
        node = leaf
        for level in range(LEVELS):
            if index % 2 == 0:
                left, right = node, zeros(level)
                self._filled[level] = node
            else:
                left, right = self._filled[level], node
            node = hash_left_right(left, right)
            index //= 2
        self.size += 1
        self.root = node
        return node

    def state(self) -> dict:
        """JSON-serialisable snapshot: ``{"size", "filled": [hex...], "root": hex}``."""
        return {
            "size": self.size,
            "filled": [_hex(v) for v in self._filled],
            "root": _hex(self.root),
        }

    @classmethod
    def from_state(cls, state: dict) -> IncrementalTree:
        """Rebuild a tree from :meth:`state`; a malformed snapshot raises ValueError."""
        filled = [int(v, 16) for v in state["filled"]]
        if len(filled) != LEVELS or not 0 <= int(state["size"]) <= 2**LEVELS:
            raise ValueError("malformed tree state")
        tree = cls()
        tree.size = int(state["size"])
        tree._filled = filled
        tree.root = int(state["root"], 16)
        if tree.size == 0 and tree.root != zeros(LEVELS):
            raise ValueError("inconsistent tree state: empty tree with a non-empty root")
        if tree.size > 0:
            if _refold_root(tree) != tree.root:
                raise ValueError("inconsistent tree state: root does not match filled subtrees")
        return tree


def _refold_root(tree: IncrementalTree) -> int:
    """Root implied by ``filled`` and ``size`` (the last insert's path)."""
    index = tree.size - 1
    # filled[0] is the last leaf only when it sits on a left slot; otherwise the right
    # slot's value is unknown, so a root check is possible only for left-slot sizes.
    if index % 2 != 0:
        return tree.root
    node = tree._filled[0]
    for level in range(LEVELS):
        if index % 2 == 0:
            left, right = node, zeros(level)
        else:
            left, right = tree._filled[level], node
        node = hash_left_right(left, right)
        index //= 2
    return node


def root_index(
    commitments: list[int], start_state: dict | None = None
) -> tuple[dict[int, int], dict]:
    """``({root: n_leaves}, final_state)`` after inserting ``commitments`` in order.

    A fresh tree contributes its empty root (0 leaves); a resumed one only the roots
    produced by ``commitments``.
    """
    tree = IncrementalTree.from_state(start_state) if start_state else IncrementalTree()
    roots: dict[int, int] = {}
    if not start_state:
        roots[tree.root] = 0
    for leaf in commitments:
        root = tree.insert(leaf)
        roots[root] = tree.size
    return roots, tree.state()


def _read_cache(path: str, pool, chain) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        cache = json.load(fh)
    if cache.get("pool") != pool.address or cache.get("chain") != chain:
        raise ApiError("merkle cache {} belongs to another pool".format(path))
    return cache


def _write_cache(path: str, cache_dir: str, result: dict) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    os.replace(tmp, path)


def _check_leaves(pool, rows, expected, start_block, end_block, path):
    for row in rows:
        if row["leaf_index"] != expected or row["commitment"] is None:
            raise ApiError(
                "{} deposits: leaf index {} where {} was expected (blocks {}-{}); "
                "the log list has a gap or a duplicate; if an earlier run cached a truncated "
                "log list, delete {} and run again".format(
                    pool.key, row["leaf_index"], expected, start_block, end_block, path
                )
            )
        expected += 1


def cached_root_index(client, pool, end_block: int, cache_dir: str, chain=None) -> dict:
    """Root index of ``pool`` up to ``end_block``, resumed from ``cache_dir``.

    The cache is ``<cache_dir>/<chain>_<pool address>.json`` holding
    ``{"pool", "chain", "end_block", "state", "roots": {hex root: n_leaves},
    "blocks": [block of leaf 0, block of leaf 1, ...]}``; only blocks after the cached
    ``end_block`` are fetched. ``blocks`` says how many leaves the pool held at a block
    (see :func:`leaves_at_block`); a cache written without it gets it from the deposit
    logs once, without rebuilding the tree. Leaf indices must run consecutively from
    the cached size, otherwise ``ApiError`` is raised (a hole is a provider gap, not an
    empty range) and the cache is left untouched. A cache that already covers
    ``end_block`` is returned as is.
    """
    chain = chain if chain is not None else getattr(client, "chain_id", 1)
    path = os.path.join(cache_dir, "{}_{}.json".format(chain, pool.address))
    cache = _read_cache(path, pool, chain)
    if cache is not None and "blocks" not in cache:
        logs = client.get_logs(pool.address, TOPIC_DEPOSIT, 0, cache["end_block"])
        rows = sorted((decode_deposit(log) for log in logs), key=lambda r: r["leaf_index"])
        _check_leaves(pool, rows, 0, 0, cache["end_block"], path)
        if len(rows) != cache["state"]["size"]:
            raise ApiError(
                "{} deposits: {} leaves in the log list, {} in the cache {}; delete it "
                "and run again".format(pool.key, len(rows), cache["state"]["size"], path)
            )
        cache["blocks"] = [r["block"] for r in rows]
        _write_cache(path, cache_dir, cache)
    if cache is not None and cache["end_block"] >= end_block:
        return cache
    start_block = cache["end_block"] + 1 if cache else 0
    logs = client.get_logs(pool.address, TOPIC_DEPOSIT, start_block, end_block)
    rows = sorted((decode_deposit(log) for log in logs), key=lambda r: r["leaf_index"])
    _check_leaves(pool, rows, cache["state"]["size"] if cache else 0, start_block, end_block, path)
    new_roots, state = root_index(
        [int(r["commitment"], 16) for r in rows], cache["state"] if cache else None
    )
    roots = dict(cache["roots"]) if cache else {}
    roots.update({_hex(r): n for r, n in new_roots.items()})
    result = {
        "pool": pool.address,
        "chain": chain,
        "end_block": end_block,
        "state": state,
        "roots": roots,
        "blocks": (list(cache["blocks"]) if cache else []) + [r["block"] for r in rows],
    }
    _write_cache(path, cache_dir, result)
    return result


def leaves_at_block(blocks: list[int], block: int) -> tuple[int, int]:
    """``(before, upto)``: leaves inserted in blocks ``< block`` and in blocks ``<= block``.

    Deposits mined in ``block`` itself may come before or after a transaction of that
    block, so the pool held between ``before`` and ``upto`` leaves when it ran.
    """
    return bisect_left(blocks, block), bisect_right(blocks, block)


def root_known_at(n_root: int, blocks: list[int], block: int, history: int) -> bool:
    """Whether the root of a tree with ``n_root`` leaves was still accepted at ``block``.

    The pool keeps the last ``history`` roots (``ROOT_HISTORY_SIZE``): the root after
    ``n`` insertions is accepted while the pool holds fewer than ``n + history`` leaves,
    and not before it holds ``n``. The same-block uncertainty of :func:`leaves_at_block`
    is resolved in favour of acceptance.
    """
    before, upto = leaves_at_block(blocks, block)
    return n_root <= upto and before - n_root < history


def root_history_size(client, pool, cache_dir: str, chain=None) -> int:
    """``ROOT_HISTORY_SIZE()`` read from the pool contract, cached per pool.

    The value is a contract constant, kept in ``<cache_dir>/<chain>_<pool>.history.json``.
    ``ApiError`` when the client cannot make the call or the answer is not a number:
    there is no default, the caller leaves the pool unchecked and records the failure.
    """
    chain = chain if chain is not None else getattr(client, "chain_id", 1)
    path = os.path.join(cache_dir, "{}_{}.history.json".format(chain, pool.address))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                size = json.load(fh).get("root_history_size")
            if isinstance(size, int) and size > 0:
                return size
        except (OSError, ValueError):
            pass
    caller = getattr(client, "eth_call", None)
    if caller is None:
        raise ApiError("this client cannot make eth_call")
    answer = caller(pool.address, ROOT_HISTORY_SIZE_SELECTOR)
    try:
        size = int(answer, 16)
    except (TypeError, ValueError):
        raise ApiError("ROOT_HISTORY_SIZE() answered {!r}".format(answer)) from None
    if not 0 < size <= 2**16:
        raise ApiError("ROOT_HISTORY_SIZE() answered {}".format(size))
    _write_cache(path, cache_dir, {"pool": pool.address, "chain": chain, "root_history_size": size})
    return size
