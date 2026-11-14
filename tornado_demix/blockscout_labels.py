"""Blockscout metadata tags as an opt-in address-label source, with label lineage.

The wallet-attribution set is thin on some chains (on Polygon a 10-address re-test
found one Binance wallet; the Blockscout metadata service tagged Binance, Bybit,
Coinbase and Crypto.com among them). The service is free and keyless:

    GET https://metadata.services.blockscout.com/api/v1/metadata
        ?chainId=<id>&addresses=<a1,...,a20>&tagsLimit=20

Facts the code relies on:

* at most 20 addresses per call, and the answer is keyed by the *checksummed*
  (EIP-55) address, a lowercase lookup finds nothing. Checksumming uses
  ``pycryptodome`` (``labels`` extra) when installed and the bundled pure-Python
  keccak otherwise (no new runtime dependency).
* ``/api/v2/addresses/<a>`` on Blockscout instances returns ``metadata: null`` even
  when the service has tags, so it is not used.

Classification is deliberately conservative: ``exchange`` only when the tag name
starts with a brand from :data:`EXCHANGE_BRANDS` (followed by ``:``, a space or a
digit), carries a wallet word (``hot``, ``cold``, ``deposit``, ``wallet N``, or is a
numbered name such as "Coinbase 16") and has no veto word (exploit, hack, phish,
scam, drainer, coin, token, dex, aggregator, router, bridge, smart wallet, contract,
deployer). Tags of exploiters ("Bybit Exploiter 1"), assets ("Binance Coin"), wallet
products ("Coinbase Smart Wallet", "Bitget Wallet") and DEX contracts ("OKX DEX:
Aggregator") carry a brand but are not an exchange's own wallet. A generic "DEPOSIT
ADDRESS" tag is ``deposit``; everything else is ``other`` with the tag name as
entity. A cached row written by an older, looser rule is re-checked from its tag when
the cache is read.

Lineage: Blockscout tags are often copied from Etherscan, and commercial tools copy
Blockscout. A second label that comes from the same lineage is a copy, not
corroboration, so :func:`independent_sources` counts Etherscan- and
Blockscout-derived labels as ONE lineage (``etherscan/blockscout``).
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
import time
from datetime import datetime, timezone

from .errors import TornadoDemixError

METADATA_URL = "https://metadata.services.blockscout.com/api/v1/metadata"
BATCH = 20
TAGS_LIMIT = 20
ATTEMPTS = 3
TIMEOUT = 30
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

DEFAULT_DIR = os.path.join(".cache", "labels", "blockscout")
CSV_COLUMNS = ("address", "category", "entity", "tag", "fetched_utc")

SOURCE = "blockscout"
SHARED_LINEAGE = "etherscan/blockscout"

# (tag prefix as written by Blockscout, entity shown). Longest prefix first so
# "Gate.io" is tried before "Gate". Explicit and small on purpose.
EXCHANGE_BRANDS = (
    ("Crypto.com", "Crypto.com"),
    ("Gate.io", "Gate"),
    ("Binance", "Binance"),
    ("Coinbase", "Coinbase"),
    ("Bitfinex", "Bitfinex"),
    ("Bitget", "Bitget"),
    ("KuCoin", "KuCoin"),
    ("Kraken", "Kraken"),
    ("Huobi", "HTX"),
    ("Bybit", "Bybit"),
    ("MEXC", "MEXC"),
    ("Gate", "Gate"),
    ("HTX", "HTX"),
    ("OKX", "OKX"),
)
_BRANDS = "|".join(re.escape(b) for b, _e in EXCHANGE_BRANDS)
_BRAND_RE = re.compile(r"^(?:" + _BRANDS + r")(?=[:\s\d])", re.IGNORECASE)
_ENTITY_OF = {b.lower(): e for b, e in EXCHANGE_BRANDS}
# Veto words: a brand prefix does not make an exploiter, a coin or token, a DEX, router,
# bridge, smart-wallet product, contract or deployer an exchange wallet.
_NOT_A_WALLET = re.compile(
    r"\b(?:exploit|hack|phish|scam|drainer)\w*"
    r"|\b(?:coin|token|dex|aggregator|router|bridge|contract|deployer)s?\b"
    r"|smart\s*wallet|\bcommerce\b|\bstaking\b"
    r"|\b(?:web3|bitget|coinbase|okx|binance|trust|exodus)\s+wallet\b",
    re.IGNORECASE,
)
# Wallet words: what an exchange's own address is called ("Hot Wallet", "Cold",
# "Deposit", "Wallet 131"), or a numbered brand name ("Coinbase 16", "Okx 7").
_WALLET_WORD = re.compile(r"\b(?:hot|cold|deposit)\b|\bwallet\s*\d+\b", re.IGNORECASE)
_NUMBERED = re.compile(r"^(?:" + _BRANDS + r")[\s:]*\d+\s*$", re.IGNORECASE)


class BlockscoutError(TornadoDemixError):
    """The metadata service could not be read; never to be taken as "no tags"."""


_sleep = time.sleep  # replaced in tests


def _load_keccak():
    try:
        from Crypto.Hash import keccak
    except ImportError:
        from .keccak import keccak256

        return keccak256

    def keccak256(data: bytes) -> bytes:
        return keccak.new(digest_bits=256, data=data).digest()

    return keccak256


def to_checksum(address: str) -> str:
    """EIP-55 mixed-case form of a 20-byte hex address (case of the input ignored)."""
    body = address[2:] if address[:2] in ("0x", "0X") else address
    if not re.fullmatch(r"[0-9a-fA-F]{40}", body):
        raise ValueError("not a 20-byte hex address: {!r}".format(address))
    body = body.lower()
    digest = _load_keccak()(body.encode("ascii")).hex()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) > 7 else c for i, c in enumerate(body)
    )


def _get_with_retry(session, params):
    last = "no attempt"
    for attempt in range(ATTEMPTS):
        if attempt:
            _sleep(2**attempt)
        try:
            resp = session.get(METADATA_URL, params=params, timeout=TIMEOUT)
        except Exception as exc:  # requests exceptions carry the URL; keep the type only
            last = type(exc).__name__
            continue
        if resp.status_code == 200:
            return resp
        last = "HTTP {}".format(resp.status_code)
        if resp.status_code not in RETRY_STATUSES:
            break
    raise BlockscoutError("Blockscout metadata request failed ({})".format(last))


def fetch_tags(chain_id: int, addresses, session=None) -> dict[str, list[dict]]:
    """Tags per address: ``{lower_address: [tag dict, ...]}``; untagged addresses are absent.

    Queries 20 checksummed addresses per call; retries 429/5xx (3 attempts, backoff)
    and raises :class:`BlockscoutError` rather than return a partial answer.
    """
    wanted = []
    for a in addresses:
        low = a.strip().lower()
        if low and low not in wanted:
            wanted.append(low)
    if session is None:
        import requests

        session = requests.Session()
    out: dict[str, list[dict]] = {}
    for i in range(0, len(wanted), BATCH):
        batch = wanted[i : i + BATCH]
        params = {
            "chainId": int(chain_id),
            "addresses": ",".join(to_checksum(a) for a in batch),
            "tagsLimit": TAGS_LIMIT,
        }
        resp = _get_with_retry(session, params)
        try:
            body = resp.json()
            found = body["addresses"]
            if not isinstance(found, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError) as exc:
            raise BlockscoutError("unexpected Blockscout metadata response") from exc
        asked = set(batch)
        for addr, entry in found.items():
            low = addr.lower()
            tags = (entry or {}).get("tags") or []
            if low in asked and tags:
                out[low] = list(tags)
    return out


def _classify(name: str, tag_type: str) -> tuple[str, str]:
    """(category, entity) of one tag."""
    if "deposit address" in name.lower() and (tag_type or "").lower() in ("generic", ""):
        return "deposit", name
    m = _BRAND_RE.match(name)
    if (
        m
        and not _NOT_A_WALLET.search(name)
        and (_WALLET_WORD.search(name) or _NUMBERED.match(name))
    ):
        return "exchange", _ENTITY_OF[m.group(0).lower()]
    return "other", name


_RANK = {"exchange": 0, "deposit": 1, "other": 2}


def to_labels(tags: dict[str, list[dict]]) -> dict[str, dict]:
    """Label dicts from :func:`fetch_tags` output; one label per address.

    The strongest class wins (exchange, then deposit, then other); ties keep the
    first tag. ``tag`` keeps the original name.
    """
    labels = {}
    for addr, items in tags.items():
        best = None
        for t in items or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            category, entity = _classify(name, t.get("tagType") or "")
            if best is None or _RANK[category] < _RANK[best[0]]:
                best = (category, entity, name)
        if best:
            labels[addr.lower()] = {
                "category": best[0],
                "entity": best[1],
                "source": SOURCE,
                "lineage": SOURCE,
                "tag": best[2],
            }
    return labels


def lineage_of(source: str) -> str:
    """Lineage of a source name: Etherscan and Blockscout labels share one."""
    low = (source or "").strip().lower()
    if "etherscan" in low or "blockscout" in low:
        return SHARED_LINEAGE
    return low or "unknown"


def independent_sources(label: dict | None) -> int:
    """Number of distinct lineages behind a label (copies are not corroboration)."""
    if not label:
        return 0
    entries = label.get("sources")
    if not entries:
        entries = [label] if (label.get("source") or label.get("lineage")) else []
    return len(
        {
            lineage_of(e.get("lineage") or e.get("source") or "")
            for e in entries
            if e.get("agrees", True)
        }
    )


def _same_entity(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return bool(a and b) and (a == b or a.startswith(b + " ") or b.startswith(a + " "))


def merge_labels(base: dict[str, dict] | None, extra: dict[str, dict]) -> dict[str, dict]:
    """Add ``extra`` (Blockscout) labels to ``base`` (wallet-attribution) without copying
    the shared cache.

    The base label wins; an address only the extra source knows is added. A merged
    label lists its ``sources`` and carries ``independent_sources``; an extra label
    naming a different entity is listed with ``agrees: False`` and not counted.
    """
    merged = dict(base or {})
    for addr, lab in extra.items():
        cur = merged.get(addr)
        if cur is None:
            merged[addr] = dict(lab, sources=[_src(lab, True)], independent_sources=1)
            continue
        new = dict(cur)
        if not new.get("category"):
            new["category"], new["entity"] = lab["category"], lab["entity"]
            # the category is the extra source's: the classifier must know where it came from
            new["category_source"] = lab.get("source", SOURCE)
        agrees = _same_entity(new.get("entity", ""), lab.get("entity", ""))
        base_src = {
            "source": cur.get("source", ""),
            "lineage": lineage_of(cur.get("source", "")),
            "entity": cur.get("entity", ""),
            "category": cur.get("category", ""),
        }
        new["sources"] = list(cur.get("sources") or [base_src]) + [_src(lab, agrees)]
        new["independent_sources"] = independent_sources(new)
        merged[addr] = new
    return merged


def _src(lab: dict, agrees: bool) -> dict:
    return {
        "source": lab.get("source", SOURCE),
        "lineage": lab.get("lineage", SOURCE),
        "entity": lab.get("entity", ""),
        "category": lab.get("category", ""),
        "agrees": agrees,
    }


def cache_path(chain_id: int, directory: str | None = None) -> str:
    return os.path.join(directory or DEFAULT_DIR, "{}.csv".format(int(chain_id)))


def load_cache(chain_id: int, directory: str | None = None) -> dict[str, dict]:
    """Cached labels for a chain (``{}`` when no cache exists)."""
    path = cache_path(chain_id, directory)
    if not os.path.exists(path):
        return {}
    labels = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            addr = (row.get("address") or "").strip().lower()
            if addr:
                category = (row.get("category") or "").strip()
                entity = (row.get("entity") or "").strip()
                tag = (row.get("tag") or "").strip()
                if category == "exchange" and tag and _classify(tag, "name")[0] != "exchange":
                    # written by an older, looser rule (e.g. "Bybit Exploiter 1")
                    category, entity = "other", tag
                labels[addr] = {
                    "category": category,
                    "entity": entity,
                    "source": SOURCE,
                    "lineage": SOURCE,
                    "tag": (row.get("tag") or "").strip(),
                    "fetched_utc": (row.get("fetched_utc") or "").strip(),
                }
    return labels


def write_cache(
    chain_id: int,
    labels: dict[str, dict],
    directory: str | None = None,
    now: str | None = None,
    path: str | None = None,
) -> str:
    """Write ``<directory>/<chainId>.csv`` (or ``path``) atomically; returns its path."""
    path = path or cache_path(chain_id, directory)
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=os.path.dirname(path) or ".",
            prefix=".bs-",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp = fh.name
            w = csv.writer(fh)
            w.writerow(CSV_COLUMNS)
            for addr in sorted(labels):
                lab = labels[addr]
                w.writerow(
                    [
                        addr,
                        lab["category"],
                        lab["entity"],
                        lab.get("tag", ""),
                        lab.get("fetched_utc") or stamp,
                    ]
                )
        os.replace(tmp, path)
    except BaseException:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def fetch_and_cache(
    chain_id: int, addresses, session=None, directory: str | None = None, now: str | None = None
) -> dict[str, dict]:
    """Fetch tags, add them to the chain's cache file, return the whole cache."""
    fresh = to_labels(fetch_tags(chain_id, addresses, session=session))
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for lab in fresh.values():
        lab["fetched_utc"] = stamp
    cache = load_cache(chain_id, directory)
    cache.update(fresh)
    write_cache(chain_id, cache, directory, now=stamp)
    return cache


def with_blockscout(base: dict[str, dict] | None, chain_id: int, directory: str | None = None):
    """``base`` plus the cached Blockscout labels of ``chain_id`` (opt-in call site)."""
    cached = load_cache(chain_id, directory)
    return merge_labels(base, cached) if cached else base
