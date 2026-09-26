"""Address attribution: label known addresses (exchanges, mixers, sanctioned...).

A candidate or a next hop is more actionable once named. This module loads a
per-network attribution table (the aggregated public dataset at
``prettydeath/wallet-attribution``, one CSV per chain) and looks addresses up in
it, turning "high-activity service, trace boundary" into "Tornado.Cash: Router
(mixer)".

The dataset is large (megabytes per chain) and lives outside this package, so it
is never bundled. Point the tool at a checkout of it with the
``TORNADO_DEMIX_ATTRIBUTION`` environment variable (or ``--attribution-dir``),
set to the directory holding ``<network>.csv`` files. Without it, lookups return
nothing and every caller degrades to unlabelled output; the feature is additive.

Columns read: ``address,entity,label,category,source,confidence``; others are ignored.
"""

from __future__ import annotations

import csv
import os

from . import config

ATTRIBUTION_ENV = "TORNADO_DEMIX_ATTRIBUTION"

# Keyed by (path, mtime): the web UI must not re-read a multi-megabyte file on
# every request, but should pick up an updated one.
_CACHE = {}


def attribution_file(network_name: str, directory: str | None = None) -> str | None:
    """Resolve the attribution CSV for ``network_name``, or None.

    An explicit ``directory`` scopes the search: the file must live there or
    nothing is loaded. It never falls through to another source, so a label the
    report calls authoritative comes from the place the caller named, not from a
    different table (mirrors the config-dir scoping rule).

    Without an explicit directory, ``TORNADO_DEMIX_ATTRIBUTION`` is tried, then
    an ``attribution/`` sub-directory of each config dir. The file is named
    ``<network>.csv`` (matching the wallet-attribution layout).
    """
    # A network name is a bare slug; refuse anything that could leave the directory.
    if os.sep in network_name or (os.altsep and os.altsep in network_name):
        return None
    name = "{}.csv".format(network_name)
    if directory:
        candidate = os.path.join(directory, name)
        return candidate if os.path.exists(candidate) else None
    roots = []
    env_dir = os.environ.get(ATTRIBUTION_ENV, "").strip()
    if env_dir:
        roots.append(env_dir)
    for cfg in config.config_dirs():
        roots.append(os.path.join(cfg, "attribution"))
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def load_attribution(network_name: str, directory: str | None = None) -> dict[str, dict]:
    """Return ``{address_lower: label_dict}`` for ``network_name``.

    ``label_dict`` has entity, label, category, source and confidence. Returns an
    empty dict when no dataset is configured or the file is absent, so callers
    need no special-casing. Result is cached by path + mtime.
    """
    path = attribution_file(network_name, directory)
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    key = (path, mtime)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    labels = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            addr = (row.get("address") or "").strip().lower()
            if not addr:
                continue
            labels[addr] = {
                "entity": (row.get("entity") or "").strip(),
                "label": (row.get("label") or "").strip(),
                "category": (row.get("category") or "").strip(),
                "source": (row.get("source") or "").strip(),
                "confidence": (row.get("confidence") or "").strip(),
            }
    _CACHE[key] = labels
    return labels


def label_of(labels: dict[str, dict] | None, address: str | None) -> dict | None:
    """Return the label dict for ``address`` (case-insensitive), or None."""
    if not labels or not address:
        return None
    return labels.get(address.lower())


def format_label(entry: dict | None) -> str:
    """One-line human form: 'Tornado.Cash: Router (mixer)', or '' if none."""
    if not entry:
        return ""
    name = entry.get("label") or entry.get("entity") or "labelled"
    category = entry.get("category")
    return "{} ({})".format(name, category) if category else name
