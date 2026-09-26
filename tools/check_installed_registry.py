#!/usr/bin/env python3
"""Assert that an *installed* tornado-demix can see its whole pool registry.

Run this from a directory that has no ``config/`` and is not the source tree -
which is the situation `pip install tornado-demix` actually leaves you in.

The registry used to live outside the package, at ``config/networks.csv.example``,
and nothing copied it into the wheel. An installed copy therefore fell back to
the four built-in Ethereum pools: 51 of the 55 silently absent, mainnet reduced
from 28 pools to 4, and every ``--network`` but ethereum an "unknown network"
error. ``pip install -e .`` cannot catch that, because an editable install still
resolves paths into the source tree - which is exactly how it reached a release
candidate. This check exists to make that impossible to ship again.
"""

import os
import sys

EXPECTED_NETWORKS = 8
EXPECTED_POOLS = 55
EXPECTED_MAINNET_POOLS = 28


def main():
    from tornado_demix import __version__, config
    from tornado_demix.networks import load_networks

    if os.path.isdir("config"):
        sys.exit(
            "run this from a directory with no ./config, so the packaged "
            "registry is the only thing that can satisfy the lookup"
        )

    nets = load_networks()
    pools = sum(len(n.pools) for n in nets.values())
    mainnet = len(nets["ethereum"].pools) if "ethereum" in nets else 0

    problems = []
    if not os.path.isdir(config.PACKAGE_DATA):
        problems.append("package data directory missing: {}".format(config.PACKAGE_DATA))
    if len(nets) != EXPECTED_NETWORKS:
        problems.append(
            "expected {} networks, got {}: {}".format(EXPECTED_NETWORKS, len(nets), sorted(nets))
        )
    if pools != EXPECTED_POOLS:
        problems.append("expected {} pools, got {}".format(EXPECTED_POOLS, pools))
    if mainnet != EXPECTED_MAINNET_POOLS:
        problems.append("expected {} mainnet pools, got {}".format(EXPECTED_MAINNET_POOLS, mainnet))

    if problems:
        sys.exit("installed package is incomplete:\n  " + "\n  ".join(problems))

    print(
        "tornado-demix {} installed at {}".format(__version__, os.path.dirname(config.PACKAGE_DATA))
    )
    print(
        "  {} networks, {} pools ({} on mainnet, assets: {})".format(
            len(nets), pools, mainnet, ", ".join(nets["ethereum"].assets)
        )
    )


if __name__ == "__main__":
    main()
