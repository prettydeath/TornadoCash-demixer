"""The Pool value object.

A denomination is not a usable identity for a pool: Avalanche runs two 10 AVAX
and two 100 AVAX contracts, and ``100 DAI``, ``100 USDC`` and ``100 USDT`` share
the number 100. :func:`assign_keys` gives each :class:`Pool` a stable string key
('1 ETH', '10 AVAX#2') that the rest of the package uses instead.
"""

from __future__ import annotations

from decimal import Decimal


def _fmt_denom(denom):
    """Render a denomination without an exponent or trailing zeros.

    ``{:g}`` would render the 5,000,000 cDAI pool as '5e+06' and could merge
    distinct pools under one label. 8 decimals covers every registered pool.
    """
    text = "{:.8f}".format(denom).rstrip("0").rstrip(".")
    return text or "0"


class Pool:
    """One fixed-denomination Tornado pool contract."""

    def __init__(
        self, address: str, denom: float, asset: str, decimals: int = 18, token: str | None = None
    ) -> None:
        """``denom`` is in whole asset units (0.1, 10000); ``token`` is the ERC-20
        contract, or None for a native pool."""
        self.address = address.lower()
        self.denom = float(denom)
        self.asset = asset
        self.decimals = int(decimals)
        self.token = token.lower() if token else None
        self.key = self.label  # replaced by assign_keys() when in a Network

    @property
    def is_native(self) -> bool:
        """True when the pool takes the chain's native currency."""
        return self.token is None

    @property
    def label(self) -> str:
        """Human-readable denomination, e.g. '1 ETH' or '10000 DAI'."""
        return "{} {}".format(_fmt_denom(self.denom), self.asset)

    @property
    def raw_denom(self) -> int:
        """The denomination in the asset's smallest unit, as an int."""
        return self.to_raw(self.denom)

    def to_raw(self, units: float) -> int:
        """Convert whole asset units to the token's smallest integer unit.

        Exact arithmetic via Decimal: ``100000.0 * 10**18`` rounds to
        99999999999999991611392, and an exact-match deposit check would then miss
        every deposit into the 100000 DAI pool. The float goes through its string
        form so the intended decimal value is used.
        """
        return int((Decimal(str(units)) * (10**self.decimals)).to_integral_value())

    def to_units(self, raw: int) -> float:
        """Smallest unit -> whole asset units, as a float.

        Divided in Decimal: ``10**23 / float(10**18)`` is 99999.99999999999.
        """
        return float(Decimal(int(raw)) / (10**self.decimals))

    def __repr__(self):
        kind = "native" if self.is_native else "token={}".format(self.token)
        return "Pool({}, {}, {})".format(self.label, self.address, kind)


def assign_keys(pool_list: list[Pool]) -> list[Pool]:
    """Populate ``pool.key`` for every pool, uniquely and deterministically.

    The key is the pool's label ('1 ETH'). When a chain holds several contracts
    at the same label, the one with the lowest address keeps the bare label and
    the rest take '#2', '#3', ... suffixes. Sorting by address makes the
    assignment independent of the order rows appear in the registry CSV, so a
    key stays stable across runs and across reorderings of the file.

    Returns the list it was given, mutated in place.
    """
    by_label = {}
    for pool in pool_list:
        by_label.setdefault(pool.label, []).append(pool)
    for label, group in by_label.items():
        group.sort(key=lambda p: p.address)
        for index, pool in enumerate(group):
            pool.key = label if index == 0 else "{}#{}".format(label, index + 1)
    return pool_list
