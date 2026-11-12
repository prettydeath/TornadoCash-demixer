#!/usr/bin/env python3
"""Generate the 220 MiMCSponge round constants (circomlib ``mimcsponge.js``).

Seed ``"mimcsponge"``, ``c = keccak256(c)`` chained, each reduced mod the BN254
scalar field; the first and last constants are zero. Needs ``pycryptodome``
(the ``dev`` extra). Output: one decimal number per line.

Usage
-----
    python tools/gen_mimc_constants.py            # rewrite the shipped file
    python tools/gen_mimc_constants.py --check    # exit 1 if the file differs
"""

from __future__ import annotations

import argparse
import os
import sys

from Crypto.Hash import keccak

FIELD = 21888242871839275222246405745257275088548364400416034343698204186575808495617
ROUNDS = 220
TARGET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tornado_demix",
    "data",
    "mimcsponge_constants.txt",
)


def k256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def constants(n: int = ROUNDS) -> list[int]:
    cts = [0] * n
    c = k256(b"mimcsponge")
    for i in range(1, n):
        c = k256(c)
        cts[i] = int.from_bytes(c, "big") % FIELD
    cts[0] = cts[-1] = 0
    return cts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with the shipped file")
    args = ap.parse_args(argv)
    text = "".join(f"{c}\n" for c in constants())
    if args.check:
        with open(TARGET, encoding="utf-8") as fh:  # universal newlines: CRLF checkouts
            same = fh.read().split() == text.split()
        print("up to date" if same else "DIFFERS from the generator")
        return 0 if same else 1
    with open(TARGET, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"wrote {ROUNDS} constants to {TARGET}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
