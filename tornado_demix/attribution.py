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

``tornado-demix labels fetch`` downloads that dataset on request (see
:func:`fetch_attribution`); each upstream source keeps its own licence.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import urllib.request

from . import config
from .errors import RegistryError
from .networks import DEFAULT_NETWORKS_CSV, load_networks

ATTRIBUTION_ENV = "TORNADO_DEMIX_ATTRIBUTION"

DEFAULT_LABELS_URL = (
    "https://raw.githubusercontent.com/prettydeath/wallet-attribution/main/data/{network}.csv"
)
# The largest chain file is a few megabytes; refuse anything absurd.
MAX_LABELS_BYTES = 64 * 1024 * 1024

# Keyed by (path, mtime): the web UI must not re-read a multi-megabyte file on
# every request, but should pick up an updated one.
_CACHE = {}


def _is_bare_slug(network_name: str) -> bool:
    """A network name is a bare slug; refuse anything that could leave the directory."""
    return not (os.sep in network_name or (os.altsep and os.altsep in network_name))


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
    if not _is_bare_slug(network_name):
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


def default_attribution_dir() -> str:
    """Where ``labels fetch`` writes, and where lookups find the files again.

    ``TORNADO_DEMIX_ATTRIBUTION`` when set, else ``attribution/`` in the first
    config directory (``config/attribution`` unless ``TORNADO_DEMIX_CONFIG`` names
    another), the same places :func:`attribution_file` searches first.
    """
    env_dir = os.environ.get(ATTRIBUTION_ENV, "").strip()
    if env_dir:
        return env_dir
    return os.path.join(config.config_dirs()[0], "attribution")


def _checked_networks(networks) -> list[str]:
    known = load_networks(DEFAULT_NETWORKS_CSV)
    names = list(networks) if networks else sorted(known)
    for name in names:
        if not _is_bare_slug(name) or name not in known:
            raise RegistryError(
                "unknown network '{}'; known: {}".format(name, ", ".join(sorted(known)))
            )
    return names


def _validate_labels(data: bytes) -> tuple[int, int]:
    """Return (rows, exchange rows) of a downloaded table, or raise ValueError."""
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
    fields = {(f or "").strip() for f in (reader.fieldnames or [])}
    if not {"address", "category"} <= fields:
        raise ValueError("header lacks the address and category columns")
    rows = exchange = 0
    for row in reader:
        if not (row.get("address") or "").strip():
            continue
        rows += 1
        if (row.get("category") or "").strip() == "exchange":
            exchange += 1
    if not rows:
        raise ValueError("no rows")
    return rows, exchange


def _write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=directory, prefix=".labels-", suffix=".tmp", delete=False
        ) as fh:
            tmp = fh.name
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fetch_attribution(
    networks=None,
    directory: str | None = None,
    url_template: str = DEFAULT_LABELS_URL,
    timeout: float = 60,
    opener=None,
) -> dict[str, dict]:
    """Download ``<network>.csv`` for each network into ``directory``.

    ``networks`` defaults to every network of the tool; an unknown name raises
    :class:`RegistryError` before anything is downloaded. A network that cannot be
    fetched or does not look like an attribution table gets
    ``status == "failed: ..."`` and no file (an existing file is left as it was);
    the others continue. ``opener`` defaults to ``urllib.request.urlopen``.
    Returns ``{network: {path, rows, exchange, status}}``.
    """
    names = _checked_networks(networks)
    directory = directory or default_attribution_dir()
    opener = opener or urllib.request.urlopen
    results = {}
    for name in names:
        path = os.path.join(directory, "{}.csv".format(name))
        result = {"path": path, "rows": 0, "exchange": 0, "status": "ok"}
        results[name] = result
        try:
            with opener(url_template.format(network=name), timeout=timeout) as resp:
                data = resp.read(MAX_LABELS_BYTES + 1)
            if len(data) > MAX_LABELS_BYTES:
                raise ValueError("file larger than {} bytes".format(MAX_LABELS_BYTES))
            result["rows"], result["exchange"] = _validate_labels(data)
            _write_atomic(path, data)
        except (OSError, ValueError, csv.Error) as exc:  # URLError, HTTPError, timeouts
            result.update(rows=0, exchange=0, status="failed: {}".format(exc))
    return results


def attribution_status(networks=None, directory: str | None = None) -> dict[str, dict]:
    """Per network: the attribution file in use (or None) with its address and
    exchange-label counts, resolved the way a lookup resolves it."""
    status = {}
    for name in _checked_networks(networks):
        path = attribution_file(name, directory)
        labels = load_attribution(name, directory) if path else {}
        status[name] = {
            "path": path,
            "rows": len(labels),
            "exchange": sum(1 for v in labels.values() if v["category"] == "exchange"),
        }
    return status
