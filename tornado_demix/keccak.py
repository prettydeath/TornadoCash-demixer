"""Keccak-256 in pure Python (the original Keccak padding 0x01, not SHA3-256's 0x06).

Used for EIP-55 address checksums when ``pycryptodome`` (the ``labels`` extra) is not
installed, so a default install can verify a checksummed address. Not meant for bulk
hashing: it handles short inputs such as a 40-character address body.
"""

from __future__ import annotations

_MASK = (1 << 64) - 1
_RATE = 136  # bytes: (1600 - 2 * 256) / 8

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip

# rotation offsets, indexed [x][y]
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rol(value: int, n: int) -> int:
    n %= 64
    return ((value << n) | (value >> (64 - n))) & _MASK if n else value


def _permute(a: list[int]) -> None:
    """Keccak-f[1600] on 25 lanes, lane (x, y) at index x + 5 * y."""
    for rc in _ROUND_CONSTANTS:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for i in range(25):
            a[i] ^= d[i % 5]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _ROT[x][y])
        for y in range(5):
            row = b[5 * y : 5 * y + 5]
            for x in range(5):
                a[x + 5 * y] = row[x] ^ (~row[(x + 1) % 5] & row[(x + 2) % 5] & _MASK)
        a[0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Keccak-256 digest of ``data`` (the Ethereum hash)."""
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE:
        padded.append(0)
    padded[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(padded), _RATE):
        block = padded[off : off + _RATE]
        for i in range(_RATE // 8):
            state[i] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        _permute(state)
    return b"".join(lane.to_bytes(8, "little") for lane in state[:4])
