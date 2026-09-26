"""Configuration loaders and config-file resolution.

The Etherscan API key and the list of wallets to analyse are read from CSV
files so that no secret is ever hard-coded or passed on the command line.

Search order, so the toolkit behaves the same from the repository root, from
``webui/`` or as an installed package::

    $TORNADO_DEMIX_CONFIG/<name>      when the variable is set - and nothing
                                      else is searched
    ./config/<name>                   otherwise, relative to the current dir
    <repository root>/config/<name>   otherwise
    <package>/data/<name>             finally, for files that ship with the
                                      package (the pool registry) - searched
                                      even when the env var is set

``TORNADO_DEMIX_CONFIG`` scopes the search rather than prepending to it: a case
directory without its own ``wallets.csv`` must raise, not fall through to
another case's file.

``*.example`` files hold placeholders and are never used as a fallback, so a
forgotten ``wallets.csv`` fails loudly. Only the registry opts in with
``allow_example=True``.

api.csv format (header required):

    service,api_key
    etherscan,YOUR_ETHERSCAN_KEY

wallets.csv format (header required, extra columns are ignored):

    address,label
    0x019b5bb2051797e33f726d0e7a8cb9b9c2003ac2,case-42-wallet-1
"""

from __future__ import annotations

import csv
import os
import re

from .errors import ConfigError

# Environment variable pointing at a directory of config files.
CONFIG_ENV = "TORNADO_DEMIX_CONFIG"

# File *names*, not paths. Resolved through resolve() at call time.
DEFAULT_API_CSV = "api.csv"
DEFAULT_WALLETS_CSV = "wallets.csv"

# <repo root>/config, derived from this module's location. Present in a source
# checkout; absent from an installed wheel, which is why PACKAGE_DATA exists.
_PACKAGE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
)

# Data shipped *inside* the package, and therefore present in a wheel: the
# on-chain-verified pool registry. Declared in pyproject as package-data.
PACKAGE_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def config_dirs() -> list[str]:
    """Return the config directories to search, most specific first.

    When ``TORNADO_DEMIX_CONFIG`` is set it is the *only* directory searched:
    naming a case directory must not silently borrow another case's files.
    """
    env_dir = os.environ.get(CONFIG_ENV, "").strip()
    if env_dir:
        return [env_dir]
    return [os.path.join("config"), _PACKAGE_CONFIG]


def resolve(name: str, allow_example: bool = False, allow_packaged: bool = False) -> str | None:
    """Return the path to config file ``name``, or None if it does not exist.

    ``name`` is a bare file name such as ``"api.csv"``. A caller that already
    holds an explicit path should pass it straight through instead.

    ``allow_example=True`` accepts a ``.example`` sibling. ``allow_packaged=True``
    adds :data:`PACKAGE_DATA` as a final fallback, searched even when
    ``TORNADO_DEMIX_CONFIG`` is set: the pool registry is not a case input but a
    verified constant that ships with the code.
    """
    if not name:
        return None
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.exists(name) else None
    for directory in config_dirs():
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
        if allow_example:
            example = candidate + ".example"
            if os.path.exists(example):
                return example
    if allow_packaged:
        packaged = os.path.join(PACKAGE_DATA, name)
        if os.path.exists(packaged):
            return packaged
    return None


def load_api_key(csv_path: str | None = None, service: str = "etherscan") -> str:
    """Return the API key for ``service``.

    Resolution order:
      1. the ``service`` row of the resolved api.csv (columns: service, api_key);
      2. the ``ETHERSCAN_API_KEY`` environment variable as a fallback.

    Raises :class:`~tornado_demix.errors.ConfigError` with a helpful
    message if no key can be found.
    """
    path = resolve(csv_path or DEFAULT_API_CSV)
    key = None
    if path and os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if (row.get("service") or "").strip().lower() == service.lower():
                    key = (row.get("api_key") or "").strip()
                    break
    if not key:
        key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    if not key:
        searched = ", ".join(config_dirs())
        raise ConfigError(
            "No API key found. Add a '{}' row to {} (columns: service,api_key) "
            "in one of: {}; or set ETHERSCAN_API_KEY, or set {} to a config "
            "directory.".format(service, csv_path or DEFAULT_API_CSV, searched, CONFIG_ENV)
        )
    return key


_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def is_address(value: str) -> bool:
    """True for a 0x-prefixed string of exactly 40 hex digits."""
    return bool(_ADDRESS_RE.fullmatch(value))


def load_wallets(csv_path: str | None = None) -> list[str]:
    """Return a list of lowercased wallet addresses from a CSV file.

    The file must have an ``address`` column; a 0x-prefixed 40-hex value is
    required. Blank rows and comment rows (starting with '#') are skipped.
    """
    path = resolve(csv_path or DEFAULT_WALLETS_CSV)
    if not path or not os.path.exists(path):
        raise ConfigError(
            "Wallets CSV not found: {}. Searched: {}".format(
                csv_path or DEFAULT_WALLETS_CSV, ", ".join(config_dirs())
            )
        )
    wallets = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            addr = (row.get("address") or "").strip().lower()
            if not addr or addr.startswith("#"):
                continue
            if is_address(addr):
                wallets.append(addr)
    if not wallets:
        raise ConfigError("No valid addresses in {}".format(path))
    return wallets
