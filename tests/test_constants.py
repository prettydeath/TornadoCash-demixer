"""Ethereum-only constants that must not come back.

Each name below was an attractive nuisance: importable, plausibly named, and
correct for 4 of the 31 shipped pools. ``EXPLORER_ADDR``/``EXPLORER_TX`` are
the hardcoded etherscan links this branch replaced with per-network URLs, and
``NON_DOWNSTREAM`` is the Ethereum-only filter that let the mixer contract
itself be reported as a cluster reconvergence point on the other seven chains.
Reaching for any of them again is a regression, so the import must fail rather
than quietly return an Ethereum answer.
"""

from tornado_demix import constants


def test_the_ethereum_only_traps_are_gone():
    for name in ("EXPLORER_ADDR", "EXPLORER_TX", "NON_DOWNSTREAM", "POOL_BY_ADDR", "DENOMINATIONS"):
        assert not hasattr(constants, name), name


def test_the_mainnet_builtin_inputs_are_still_there():
    """POOLS and ROUTERS are live - networks._builtin_ethereum reads them."""
    assert len(constants.POOLS) == 4
    assert len(constants.ROUTERS) == 3
    assert all(a.islower() for a in constants.POOLS.values())
    assert all(a.islower() for a in constants.ROUTERS)
