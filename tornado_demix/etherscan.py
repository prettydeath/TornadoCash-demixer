"""Minimal Etherscan V2 API client with pagination and rate-limit handling."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
import uuid
from collections import Counter
from typing import Any

import requests

from .constants import CHAIN_ID, ETHERSCAN_API_URL, TOPIC_DEPOSIT, WEI
from .errors import ApiError, ApiKeyError, BlockLookupError, PermanentApiError

_fetch = urllib.request.urlopen  # module-level so tests can replace it

# BlockLookupError is re-exported: callers import it from here.
__all__ = [
    "EtherscanClient",
    "BlockLookupError",
    "ApiError",
    "ApiKeyError",
    "PermanentApiError",
    "MAX_BLOCK",
    "PAGE_SIZE",
    "MAX_PAGES",
]


# Etherscan returns at most PAGE_SIZE rows per request and at most MAX_PAGES
# pages (10,000 rows) for a single query; larger result sets require narrowing
# the block range. Both limits are handled by ``fetch_all`` below.
PAGE_SIZE = 1000
MAX_PAGES = 10

# Upper sentinel for "no end block". It must exceed every supported chain's head:
# an endblock below the head is not an error for the explorer, it simply returns
# no rows, which reads as "no transactions". The 99999999 used in Etherscan's own
# examples is already below the head of BSC, Optimism and Arbitrum. 10^12 is
# accepted by Etherscan V2, Blockscout and Routescan.
MAX_BLOCK = 999_999_999_999

# Total time one call may spend backing off before it gives up (seconds). Each wait
# doubles from BACKOFF_BASE up to BACKOFF_CAP, with random jitter; ``--max-wait``.
DEFAULT_MAX_WAIT = 120.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0

# A cached answer must end at least this many blocks below the chain head: a block
# that deep is no longer reorganised, so the same query gives the same rows.
CACHE_SAFE_DEPTH = 64

# JSON-RPC eth_getLogs fallback: the first block span asked of the node (halved when
# the provider reports a range or result limit), the most requests one lookup may
# spend, and how long the node is preferred after the explorer failed (seconds).
RPC_LOG_SPAN = 100_000  # first span of a client; the span that last worked is remembered
RPC_MAX_SPAN = 1_000_000
RPC_MAX_REQUESTS = 10_000
# A failed head lookup that only serves the cache is not repeated for this long (seconds).
HEAD_RETRY_SECONDS = 60
# Reason of the notice recorded when a log list was read from the node.
RPC_NOTICE = "rpc_fallback_no_gas_price"
RPC_PREFER_SECONDS = 300


def _log(msg):
    """Write progress to stderr so it never pollutes stdout / piped output."""
    print(msg, file=sys.stderr, flush=True)


# Ways a provider says "your key is the problem". After a few "Invalid API Key"
# answers Etherscan switches to "Too many invalid api key attempts", which must
# also stop the retry loop. A bare "missing" is avoided on purpose: it would match
# a benign "no records found, missing data".
_KEY_REJECTIONS = (
    "invalid api key",  # "Invalid API Key", "Missing/Invalid API Key"
    "too many invalid api key",  # the lockout after a few of the above
    "api key not valid",
    "missing api key",
)


def _is_key_rejection(result):
    lowered = result.lower()
    return any(phrase in lowered for phrase in _KEY_REJECTIONS)


# requests puts the full URL, query string included, into its exception text.
_APIKEY_PARAM = re.compile(r"(apikey=)[^&\s'\")]+", re.IGNORECASE)


def _redact(text, api_key):
    """Remove the API key from an error message before it is raised or shown."""
    text = _APIKEY_PARAM.sub(r"\1***", str(text))
    return text.replace(api_key, "***") if api_key else text


class _Budget:
    """The allowance of one retry loop: ``max_wait`` seconds of wall time.

    Sleeps and the attempts themselves (a timeout is up to 40 s) both count, and the
    allowance only decides whether ANOTHER attempt may start: an attempt that has
    begun always gets its full timeout. A provider that hangs ends the loop after
    about ``max_wait`` seconds plus one timeout, not after ``retries`` full timeouts.
    """

    def __init__(self, client, attempts):
        self.client, self.attempts = client, attempts
        self.t0 = time.monotonic()
        self.spent = 0.0  # seconds slept

    def used(self):
        return max(self.spent, time.monotonic() - self.t0)

    def left(self):
        return self.client.max_wait - self.used()

    def wait(self, attempt, retry_after=None):
        """Back off before the next attempt; False when there is none left to make.

        Exponential from ``BACKOFF_BASE`` to ``BACKOFF_CAP`` with jitter; a provider's
        Retry-After can only lengthen the wait, never shorten it.
        """
        if attempt + 1 >= self.attempts or self.left() <= 0:
            return False
        seconds = min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt) * random.uniform(0.5, 1.0)
        if retry_after is not None:
            seconds = max(seconds, retry_after)
        seconds = min(seconds, self.left())
        time.sleep(seconds)
        self.spent += seconds
        self.client.waited += seconds
        return True


class EtherscanClient:
    """Thin wrapper around the Etherscan V2 unified API.

    A single :class:`requests.Session` is reused for connection pooling. Soft
    errors (rate limits, transient failures) are retried with linear backoff.
    """

    def __init__(
        self,
        api_key: str,
        chain_id: int = CHAIN_ID,
        pause: float = 0.0,
        retries: int = 10,
        base_url: str = ETHERSCAN_API_URL,
        style: str = "v2",
        rpc_url: str = "",
        fallback_url: str = "",
        max_wait: float = DEFAULT_MAX_WAIT,
        cache_dir: str | None = None,
        progress_every: int = 0,
    ) -> None:
        self.api_key = api_key
        self.chain_id = chain_id
        self.pause = pause  # optional fixed delay before every attempt (shared free keys)
        self.retries = retries  # attempts per call; max_wait bounds the time spent waiting
        self.max_wait = max_wait
        # Optional on-disk response cache (see _cache_file). Never holds errors or the key.
        self.cache_dir = cache_dir or None
        # Progress: every ``progress_every`` calls ``progress(stats)`` is invoked, or a
        # line goes to stderr when ``progress`` is None. 0 = silent.
        self.progress_every = progress_every
        self.progress = None
        self.calls = 0  # call() invocations, cache hits included
        self.cache_hits = 0
        self.waited = 0.0  # seconds spent in backoff
        self.rpc_fallbacks = 0  # getLogs lookups served by the node instead of the explorer
        self._logs_rpc_until = 0.0  # monotonic time until which getLogs goes to the node first
        self._block_ts: dict[int, int] = {}
        self.rpc_calls = 0  # JSON-RPC requests to rpc_url
        self._head_failed_until = 0.0  # monotonic: no head lookup for the cache before this
        self._rpc_span = RPC_LOG_SPAN  # block span that last worked on the node
        self._rpc_span_ceiling = RPC_MAX_SPAN  # lowered when the node reports a limit
        # (address, topic0) -> first block with a log, learned only from unfiltered node walks that began at block 0;
        # a caller that knows a pool's deploy block may set it here.
        self.first_blocks: dict[tuple[str, str], int] = {}
        # Provider style:
        #   "v2"     -> Etherscan V2 (one host, chainid selects the chain)
        #   "compat" -> an Etherscan-compatible explorer at its own base URL
        #               (Blockscout, Routescan, classic scan). No chainid param.
        self.base_url = base_url
        self.style = style
        # Optional JSON-RPC node: the last resort for the two lookups that need the
        # proxy module, which Blockscout does not serve.
        self.rpc_url = rpc_url
        # Optional keyless Etherscan-compatible explorer for a chain the free
        # Etherscan tier refuses ("Free API access is not supported for this
        # chain"): the client switches to it once and stays there.
        self.fallback_url = fallback_url
        self._proxy_proven = False  # a proxy call has succeeded on this provider
        self._proxy_dead = False  # a compat provider that does not serve the proxy
        self.session = requests.Session()
        # Lists cut off by the explorer's paging window (one block with more rows
        # than can be paged); see _paged. Empty when every list is complete.
        self.truncated: list[dict] = []
        self._head = None  # (block, time) of the last current_block answer

    def call(self, params: dict) -> Any:
        """Perform one API call and return the ``result`` field.

        Returns an empty list only for a *definite* empty answer - the
        provider replied and said there are no records. Raises
        :class:`~tornado_demix.errors.ApiKeyError` if the key is rejected,
        :class:`~tornado_demix.errors.PermanentApiError` for a refusal retrying
        cannot fix (chain not on the plan, malformed address), and
        :class:`~tornado_demix.errors.ApiError` if the provider never gave a
        usable answer within ``retries`` attempts or ``max_wait`` seconds of
        backoff. Only transient trouble (rate limit, HTTP 429/5xx, timeout, a body
        that is not JSON) is retried, with exponential backoff and jitter.

        Returning ``[]`` from an exhausted retry loop would make an expired key,
        a spent quota or an outage indistinguishable from "this wallet touched no
        Tornado pools".
        """
        query = dict(params)
        if query.get("module") == "logs" and "blockscout" in self.base_url.lower():
            # Blockscout answers "No logs found" to topic1..3 filters even when
            # matching logs exist: ask with topic0 only (get_logs matches the rest)
            query = {k: v for k, v in query.items() if not _INDEXED_TOPIC.match(k)}
        if self.style == "v2":
            query["chainid"] = self.chain_id
        cache_path = self._cache_file(query)  # before the key is added: it is not in the key
        if self.style == "v2":
            query["apikey"] = self.api_key
        elif self.api_key:  # compat explorers ignore an unknown key
            query["apikey"] = self.api_key
        self._tick()
        if cache_path:
            cached = _cache_read(cache_path)
            if cached is not _MISS:
                self.cache_hits += 1
                return cached

        last_error = "no attempt completed"
        budget = _Budget(self, self.retries)

        for attempt in range(self.retries):
            if attempt and budget.left() <= 0:
                break
            if self.pause:
                time.sleep(self.pause)
            try:
                resp = self.session.get(self.base_url, params=query, timeout=40)
                code = getattr(resp, "status_code", 200)
                if code == 429 or (isinstance(code, int) and code >= 500):
                    last_error = "HTTP {}".format(code)
                    if budget.wait(attempt, _retry_after(resp)):
                        continue
                    break
                payload = resp.json()
            except _BAD_URL as exc:
                raise PermanentApiError(
                    _redact(
                        "the provider URL {} is not usable: {}".format(self.base_url, exc),
                        self.api_key,
                    )
                ) from None
            except (OSError, ValueError) as exc:
                # Network failure (requests errors are OSErrors) or a body that is not
                # JSON: back off and retry.
                last_error = "{}: {}".format(type(exc).__name__, exc)
                if budget.wait(attempt):
                    continue
                break

            status = payload.get("status")
            message = str(payload.get("message", ""))
            result = payload.get("result")

            # The proxy module (module=proxy) answers with a raw JSON-RPC envelope
            # that has no top-level "status"/"message", so it is handled before
            # the classic Etherscan checks below.
            if status is None and "jsonrpc" in payload:
                error = payload.get("error")
                if error is not None:
                    last_error = "proxy error: {}".format(str(error)[:120])
                    if budget.wait(attempt):
                        continue
                    break
                # A null result means the node has never seen this hash; it is
                # returned as None so it stays distinct from an empty list.
                return result

            if status == "1":
                if cache_path and isinstance(result, list):
                    _cache_write(cache_path, self.cache_dir, result)
                return result
            # Blockscout answers status "2" with the rows it has when part of the
            # range is still being indexed ("Some internal transactions within this
            # block range have not yet been processed"). The rows are valid; the
            # list may lack the newest internal transactions. Not cached: the list
            # can grow.
            if status == "2" and isinstance(result, list) and "not yet been processed" in message:
                return result
            text = result if isinstance(result, str) else ""
            blob = (text + " " + message).lower()
            if any(phrase in blob for phrase in _RATE_LIMITS):
                last_error = "rate limited: {}".format((text or message)[:120])
                if budget.wait(attempt):
                    continue
                break
            if (
                "free api access is not supported" in blob
                and self.fallback_url
                and self.base_url != self.fallback_url
            ):
                self.base_url, self.style = self.fallback_url, "compat"
                return self.call(params)
            if text and _is_key_rejection(text):
                raise ApiKeyError(
                    _redact("the data provider rejected the API key: {}".format(text), self.api_key)
                )
            if any(phrase in blob for phrase in _PERMANENT):
                raise PermanentApiError(
                    _redact(
                        "{} refused {} for {}: {}{}".format(
                            self.base_url,
                            query.get("action", "?"),
                            query.get("address", query.get("timestamp", "?")),
                            (text or message)[:160],
                            _permanent_hint(blob),
                        ),
                        self.api_key,
                    )
                )
            # Benign "nothing here" answers. Checked after the key and rate-limit
            # cases so an auth failure never reads as an empty history. A null
            # result without such a message is an error (e.g. a query timeout).
            if (
                "no transactions found" in message.lower()
                or "no records found" in message.lower()
                or result == []
            ):
                if cache_path:
                    _cache_write(cache_path, self.cache_dir, [])
                return []
            last_error = "status={} message={} result={}".format(
                status, message[:80], str(result)[:80]
            )
            if budget.wait(attempt):
                continue
            break

        raise ApiError(
            _redact(
                "{} ({}) gave no usable answer after {} attempt(s), {:.0f}s of backoff, "
                "for {}: {}".format(
                    self.base_url,
                    query.get("action", "?"),
                    attempt + 1,
                    budget.spent,
                    query.get("address", query.get("timestamp", "?")),
                    last_error,
                ),
                self.api_key,
            )
        )

    def _tick(self, rpc=False):
        """Count one explorer call (or JSON-RPC request) and report progress every
        ``progress_every`` of them."""
        if rpc:
            self.rpc_calls += 1
        else:
            self.calls += 1
        if self.progress_every and (self.calls + self.rpc_calls) % self.progress_every == 0:
            stats = self.stats()
            if self.progress:
                self.progress(stats)
            else:
                _log(
                    "[*] {calls} explorer calls ({cache_hits} from cache, {rpc_calls} "
                    "node requests, {waited:.0f}s waiting)".format(**stats)
                )

    def stats(self) -> dict:
        """Counters of this client: calls, cache hits, seconds waited, RPC fallbacks."""
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "rpc_calls": self.rpc_calls,
            "waited": round(self.waited, 1),
            "rpc_fallbacks": self.rpc_fallbacks,
        }

    def _cache_file(self, query):
        """Path of the cache entry for ``query``, or None when it must not be cached.

        Only account and log queries whose block range ends at least
        ``CACHE_SAFE_DEPTH`` blocks below the head are cached: their answer no
        longer changes. The key is the provider URL plus every request parameter
        except the API key, hashed; the key is never written to a name or a file.
        """
        if not self.cache_dir or query.get("module") not in ("account", "logs"):
            return None
        end = query.get("endblock", query.get("toBlock"))
        try:
            end = int(end)
        except (TypeError, ValueError):
            return None  # "latest" or no end block: the answer can still grow
        head = self._head_for_cache()
        if head is None or end > head - CACHE_SAFE_DEPTH:
            return None
        material = json.dumps(
            {
                "base": self.base_url,
                "q": {k: str(v) for k, v in query.items() if k.lower() != "apikey"},
            },
            sort_keys=True,
        )
        return os.path.join(self.cache_dir, hashlib.sha256(material.encode()).hexdigest() + ".json")

    def _head_for_cache(self):
        """Chain head for the cache decision, or None (then nothing is cached).

        The lookup serves only the cache, so it is one attempt per source and a
        failure is remembered for ``HEAD_RETRY_SECONDS``: a throttled proxy module
        must not cost a backoff on every cacheable call.
        """
        if self._head and time.time() - self._head[1] < 60:
            return self._head[0]
        if time.monotonic() < self._head_failed_until:
            return None
        saved, self.retries = self.retries, 1
        try:
            return self.current_block()
        except (ApiError, BlockLookupError):
            self._head_failed_until = time.monotonic() + HEAD_RETRY_SECONDS
            return None
        finally:
            self.retries = saved

    def fetch_all(
        self,
        action: str,
        address: str,
        start_block: int = 0,
        end_block: int = MAX_BLOCK,
        extra: dict | None = None,
    ) -> list[dict]:
        """Return every ``account/<action>`` row for ``address`` in a range.

        Handles both Etherscan limits: it pages through up to ``MAX_PAGES``
        pages of ``PAGE_SIZE`` rows, and when a query overflows that cap it
        splits the block range and continues from the last block seen.

        Rows re-fetched across a range split are dropped, but rows that merely
        share a transaction hash are kept: one transaction can carry several
        internal or token transfers (a Safe depositing three notes at once).
        Blockscout's ``transactionHash`` field is copied to ``hash``.

        ``extra`` is merged into every page's query - ``tokentx`` uses it to
        pass ``contractaddress``.
        """
        query = {"module": "account", "action": action, "address": address.lower(), "sort": "asc"}
        if extra:
            query.update(extra)
        rows = self._paged(query, ("startblock", "endblock"), start_block, end_block, int)
        for row in rows:
            if not row.get("hash") and row.get("transactionHash"):
                row["hash"] = row["transactionHash"]
        return _dedupe(rows, _row_identity)

    def get_logs(
        self,
        address: str,
        topic0: str,
        start_block: int,
        end_block: int,
        topics: dict[int, str] | None = None,
    ) -> list[dict]:
        """Return every event log for ``address`` matching ``topic0``.

        ``topics`` adds exact matches on the indexed topics, ``{1: topic1, 2: topic2}``;
        each is ANDed with topic0. Same paging strategy as :meth:`fetch_all`; logs
        are de-duplicated by (transaction hash, log index).
        """
        query = {
            "module": "logs",
            "action": "getLogs",
            "address": address.lower(),
            "topic0": topic0,
        }
        indexes = sorted(topics or {})
        for i in indexes:
            query[f"topic{i}"] = topics[i]
            query[f"topic0_{i}_opr"] = "and"
        for a, b in zip(indexes, indexes[1:]):
            query[f"topic{a}_{b}_opr"] = "and"
        rows = self._logs_rows(query, address, topic0, topics, start_block, end_block)
        if indexes:
            # checked here too: a provider may ignore the filters (see call)
            wanted = {i: topics[i].lower() for i in indexes}
            if any(not isinstance(r.get("topics"), list) for r in rows):
                # rows that cannot be matched: not "no such log", a failed lookup
                raise ApiError(
                    f"{self.base_url} (getLogs) returned log rows without topics for {address}"
                )
            rows = [r for r in rows if _topics_match(r, wanted)]
        return _dedupe(rows, lambda r: (r.get("transactionHash"), r.get("logIndex")))

    def _logs_rows(self, query, address, topic0, topics, start_block, end_block):
        """Raw getLogs rows: the explorer, or the JSON-RPC node when the explorer fails.

        When the explorer has failed after its retries (throttling, an outage, a chain
        it refuses) and ``rpc_url`` is set, the lookup is repeated with
        ``eth_getLogs`` against the node (:meth:`_rpc_logs`); the rows have the
        explorer's shape. For ``RPC_PREFER_SECONDS`` after such a failure the node is
        asked first, so a throttled explorer does not cost its whole backoff budget
        on every lookup. A rejected API key never falls back, and a node that cannot
        answer leaves the explorer's own error. Every list read from the node is
        recorded in ``self.truncated`` as a notice (reason ``RPC_NOTICE``): its rows
        carry no gas price.
        """
        if self.rpc_url and time.monotonic() < self._logs_rpc_until:
            try:
                rows = self._rpc_logs(address, topic0, topics, start_block, end_block)
            except ApiError:
                pass  # the explorer is tried below
            else:
                self._note_rpc_fallback(address)
                return rows
        try:
            return self._paged(
                query, ("fromBlock", "toBlock"), start_block, end_block, lambda b: int(b, 16)
            )
        except ApiKeyError:
            raise
        except ApiError as explorer_error:
            if not self.rpc_url:
                raise
            try:
                rows = self._rpc_logs(address, topic0, topics, start_block, end_block)
            except ApiError:
                raise explorer_error from None
            self._logs_rpc_until = time.monotonic() + RPC_PREFER_SECONDS
            self._note_rpc_fallback(address)
            _log(
                "[!] explorer getLogs failed ({}); read the logs from the JSON-RPC node".format(
                    str(explorer_error)[:120]
                )
            )
            return rows

    def _note_rpc_fallback(self, address):
        """Count a list read from the node and record the notice."""
        self.rpc_fallbacks += 1
        self.truncated.append(
            {
                "action": "getLogs",
                "address": address,
                "block": None,
                "limit": None,
                "reason": RPC_NOTICE,
            }
        )

    def _rpc_logs(self, address, topic0, topics, start_block, end_block):
        """``eth_getLogs`` against ``rpc_url`` in explorer row shape.

        The range is clamped to the node's head, and its start is moved up to the first
        block known to hold such a log (``first_blocks``, learned from an earlier
        unfiltered walk that began at block 0, or set by the caller). Spans start at the span that last worked
        on this node (``RPC_LOG_SPAN`` at first), halve when the node reports a block
        range or result limit (the halved span becomes this client's ceiling) and double
        again after two clean requests up to that ceiling. A node answer that says
        rate limit / HTTP 429 is backed off with the usual budget, not read as a range
        limit. Each log becomes ``{address, topics, data, blockNumber, timeStamp,
        logIndex, transactionHash, transactionIndex, gasPrice, gasUsed}`` with hex
        strings like Etherscan's getLogs. A node log carries no timestamp (unless the
        node adds ``blockTimestamp``): it is read once per block with
        ``eth_getBlockByNumber`` and kept in memory. ``gasPrice`` and ``gasUsed`` are
        ``"0x"``, which ``events.py`` reads as 0, so the unique-gas-price signal has
        nothing to match on rows from the node. Raises :class:`ApiError` when the node
        cannot answer or a timestamp is missing: a row without a timestamp must not
        become time 0.
        """
        ok, head_hex, err = self._rpc_call("eth_blockNumber", [])
        head = _hex_int(head_hex) if ok else None
        if head is None:
            raise ApiError("JSON-RPC node gave no block number: {}".format(err or head_hex))
        filter_topics = [topic0]
        for i in range(1, max(topics or {0: 0}) + 1):
            filter_topics.append((topics or {}).get(i))
        hint_key = (address.lower(), topic0)
        end = min(end_block, head)
        low = max(start_block, self.first_blocks.get(hint_key, 0))
        span = min(self._rpc_span, self._rpc_span_ceiling)
        rows, requests_left, failures, clean = [], RPC_MAX_REQUESTS, 0, 0
        budget = _Budget(self, self.retries)
        while low <= end:
            requests_left -= 1
            if requests_left < 0:
                raise ApiError(
                    "JSON-RPC eth_getLogs: range {}-{} needs more than {} requests".format(
                        start_block, end, RPC_MAX_REQUESTS
                    )
                )
            high = min(low + span - 1, end)
            ok, result, err = self._rpc_call(
                "eth_getLogs",
                [
                    {
                        "address": address.lower(),
                        "fromBlock": hex(low),
                        "toBlock": hex(high),
                        "topics": filter_topics,
                    }
                ],
            )
            if ok and isinstance(result, list):
                rows.extend(result)
                low, failures, clean = high + 1, 0, clean + 1
                budget = _Budget(self, self.retries)
                self._rpc_span = span
                if clean >= 2:
                    span, clean = min(span * 2, self._rpc_span_ceiling), 0
                continue
            clean = 0
            if not ok and _node_rate_limited(err):
                if budget.wait(failures):
                    failures += 1
                    continue
                raise ApiError(
                    "JSON-RPC node kept rate limiting eth_getLogs for blocks {}-{}: {}".format(
                        low, high, err
                    )
                )
            if not ok and span > 1 and _range_limit(err):
                span = max(1, span // 2)
                self._rpc_span, self._rpc_span_ceiling = span, min(self._rpc_span_ceiling, span)
                continue
            failures += 1
            if failures >= 3:
                raise ApiError(
                    "JSON-RPC eth_getLogs failed for blocks {}-{}: {}".format(
                        low, high, err or "unexpected answer"
                    )
                )
            time.sleep(0.8 * failures)
        if rows and not topics and start_block == 0 and hint_key not in self.first_blocks:
            # Only a walk that began at block 0 shows where the history starts; a walk
            # from a later block would record a first log that is not the first.
            self.first_blocks[hint_key] = min(_hex_int(r.get("blockNumber")) or end for r in rows)
        return [self._rpc_row(log) for log in rows]

    def _rpc_row(self, log):
        """One node log in explorer row shape (see :meth:`_rpc_logs`)."""
        block = log.get("blockNumber")
        stamp = log.get("blockTimestamp") or hex(self._block_timestamp(block))
        return {
            "address": log.get("address"),
            "topics": log.get("topics") or [],
            "data": log.get("data") or "0x",
            "blockNumber": block,
            "timeStamp": stamp,
            "gasPrice": "0x",
            "gasUsed": "0x",
            "logIndex": log.get("logIndex"),
            "transactionHash": log.get("transactionHash"),
            "transactionIndex": log.get("transactionIndex"),
        }

    def _block_timestamp(self, block_hex):
        """Timestamp of a block through ``eth_getBlockByNumber``; cached per block."""
        number = _hex_int(block_hex)
        if number is None:
            raise ApiError("JSON-RPC log without a block number: {!r}".format(block_hex))
        if number not in self._block_ts:
            budget = _Budget(self, self.retries)
            for attempt in range(self.retries):
                ok, block, err = self._rpc_call("eth_getBlockByNumber", [hex(number), False])
                stamp = _hex_int(block.get("timestamp")) if ok and isinstance(block, dict) else None
                if stamp is not None:
                    self._block_ts[number] = stamp
                    break
                if not (_node_rate_limited(err) and budget.wait(attempt)):
                    if attempt >= 1 or not _node_rate_limited(err):
                        raise ApiError(
                            "JSON-RPC node gave no timestamp for block {}: {}".format(number, err)
                        )
                    time.sleep(0.8)
            else:
                raise ApiError("JSON-RPC node gave no timestamp for block {}".format(number))
        return self._block_ts[number]

    def _paged(self, query, range_keys, start_block, end_block, parse_block):
        """Collect every row of ``query`` between two blocks.

        A page shorter than PAGE_SIZE is NOT taken as the last one: the explorer
        can answer a short page in the middle of a result set (observed:
        1000 / 1000 / 999 / 386 rows). The walk ends only on an empty page.
        Pages are numbered 1..MAX_PAGES, so ``page * offset`` never exceeds the
        explorer's 10,000-row window. When all MAX_PAGES pages are non-empty the
        range is continued by a block cursor: rows before the last block seen
        are kept and the next pass starts at that block (inclusive), so none of
        its rows is lost; rows re-fetched at the boundary are removed by the
        callers' de-duplication (``_dedupe``).

        A single block holding more rows than the window pages stays cut off. It
        is logged to stderr and also appended to ``self.truncated`` as a dict
        ``{"action", "address", "block", "limit"}`` so callers can show it.
        """
        low_key, high_key = range_keys
        collected, low = [], start_block
        while True:
            rows, overflow, first = [], True, None
            for page in range(1, MAX_PAGES + 1):
                chunk = self.call(
                    {**query, low_key: low, high_key: end_block, "page": page, "offset": PAGE_SIZE}
                )
                if not chunk:
                    overflow = False
                    break
                if first is not None and chunk[0] == first:
                    # The provider ignored ``page`` and repeated the previous page.
                    overflow = False
                    break
                first = chunk[0]
                rows.extend(_number_repeats(chunk))
            if not overflow:
                return collected + rows
            max_block = max(parse_block(r["blockNumber"]) for r in rows)
            if max_block <= low:
                # One block holds more rows than the provider pages; the rest is lost.
                limit = MAX_PAGES * PAGE_SIZE
                _log(f"[!] block {max_block} holds at least {limit} rows; the list is truncated")
                self.truncated.append(
                    {
                        "action": query.get("action", ""),
                        "address": query.get("address", ""),
                        "block": max_block,
                        "limit": limit,
                        "reason": "truncated",
                    }
                )
                return collected + rows
            collected.extend(r for r in rows if parse_block(r["blockNumber"]) < max_block)
            low = max_block

    def block_by_time(self, timestamp: float, closest: str = "before") -> int:
        """Return the block number closest to a UNIX timestamp.

        Raises :class:`BlockLookupError` if the response cannot be resolved.
        Etherscan answers with a bare numeric string; Blockscout answers with a
        ``{"blockNumber": "..."}`` object. Both are accepted.
        """
        result = self.call(
            {
                "module": "block",
                "action": "getblocknobytime",
                "timestamp": int(timestamp),
                "closest": closest,
            }
        )
        raw = result
        if isinstance(result, dict):
            raw = result.get("blockNumber")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise BlockLookupError(
                "could not resolve timestamp {} to a block (closest={}): "
                "provider returned {!r}".format(timestamp, closest, result)
            ) from exc

    def current_block(self) -> int:
        """Return the chain's latest block number.

        Used to clamp a search window that ends in the future: block-by-time
        with closest="after" rejects a future timestamp. Etherscan answers through
        the proxy module; Blockscout has none and has ``block/eth_block_number``
        instead; a configured JSON-RPC node is the last resort.
        """
        now = time.time()
        if self._head and now - self._head[1] < 60:
            return self._head[0]
        result = self._proxy({"module": "proxy", "action": "eth_blockNumber"})
        head = _hex_int(result)
        if head is None and self.style == "compat":
            try:
                result = self.call({"module": "block", "action": "eth_block_number"})
            except ApiError:
                result = None
            head = _hex_int(result)
        if head is None:
            result = self._rpc("eth_blockNumber", [])
            head = _hex_int(result)
        if head is None:
            raise BlockLookupError(
                "could not read the current block number: provider returned {!r}".format(result)
            )
        self._head = (head, now)
        return head

    def _proxy(self, params: dict) -> Any:
        """One proxy-module call; None when the provider cannot answer it.

        A compat explorer without the proxy module ("Unknown module") is probed
        once with a single attempt and then skipped, so the retry backoff is not
        paid on every hash. A rejected API key is raised.
        """
        return self._proxy_result(params)[1]

    def _proxy_result(self, params: dict) -> tuple[str | None, Any]:
        """(error or None, result) of one proxy-module call; see :meth:`_proxy`."""
        if self._proxy_dead:
            return "the explorer does not serve the proxy module", None
        probe = self.style == "compat" and not self._proxy_proven
        retries = self.retries
        if probe:
            self.retries = 1
        try:
            result = self.call(params)
        except ApiKeyError:
            raise
        except ApiError as exc:
            if probe:
                self._proxy_dead = True
            return str(exc) or type(exc).__name__, None
        finally:
            self.retries = retries
        self._proxy_proven = True
        return None, result

    def _tx_lookup(self, action: str, tx_hash: str) -> dict | None:
        """``eth_getTransactionByHash`` / ``eth_getTransactionReceipt`` through the
        explorer's proxy module, then the node in ``rpc_url``.

        Returns the object, None when a source answered that the hash is unknown,
        and raises :class:`ApiError` when no source answered at all: a failed read
        must not look like an unknown transaction.
        """
        error, result = self._proxy_result({"module": "proxy", "action": action, "txhash": tx_hash})
        if isinstance(result, dict):
            return result
        answered = error is None and result is None
        if error is None and result is not None:
            error = "unexpected answer {!r}".format(str(result)[:80])
        ok, node = self._rpc_result(action, [tx_hash])
        if isinstance(node, dict):
            return node
        if answered or (ok and node is None):
            return None
        raise ApiError(
            _redact("transaction {} not read ({}): {}".format(tx_hash, action, error), self.api_key)
        )

    def _rpc(self, method: str, params: list) -> Any:
        """One JSON-RPC call to ``rpc_url``; None when unset or unanswered."""
        return self._rpc_result(method, params)[1]

    def _rpc_result(self, method: str, params: list) -> tuple[bool, Any]:
        """(answered, result) of one JSON-RPC call; answered is False when ``rpc_url``
        is unset, the call failed or the node returned an error."""
        ok, result, _ = self._rpc_call(method, params)
        return ok, result

    def _rpc_call(self, method: str, params: list) -> tuple[bool, Any, str]:
        """(answered, result, error text) of one JSON-RPC call to ``rpc_url``.

        The error text never contains the node URL: some providers carry their key
        in the path.
        """
        if not self.rpc_url:
            return False, None, "no RPC endpoint"
        self._tick(rpc=True)
        if self.pause:
            time.sleep(self.pause)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        request = urllib.request.Request(
            self.rpc_url,
            data=body.encode(),
            headers={"Content-Type": "application/json", "User-Agent": "tornado-demix"},
        )
        try:
            with _fetch(request, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
        except (OSError, ValueError) as exc:
            text = "{}: {}".format(type(exc).__name__, exc)
            return False, None, text.replace(self.rpc_url, "<rpc>")[:200]
        if not isinstance(payload, dict):
            return False, None, "unexpected answer"
        if payload.get("error"):
            return False, None, str(payload["error"]).replace(self.rpc_url, "<rpc>")[:200]
        return True, payload.get("result"), ""

    def eth_call(self, to: str, data: str) -> str | None:
        """``eth_call`` at the latest block through the explorer's proxy module, then the
        node in ``rpc_url``. Raises :class:`ApiError` when no source answered."""
        error, result = self._proxy_result(
            {"module": "proxy", "action": "eth_call", "to": to, "data": data, "tag": "latest"}
        )
        if isinstance(result, str) and result.startswith("0x") and len(result) > 2:
            return result
        ok, node = self._rpc_result("eth_call", [{"to": to, "data": data}, "latest"])
        if isinstance(node, str) and node.startswith("0x") and len(node) > 2:
            return node
        raise ApiError(
            _redact("eth_call to {} not answered: {}".format(to, error or result), self.api_key)
        )

    def outgoing_txs(self, address: str) -> list[dict]:
        """Return every normal transaction sent from ``address``."""
        return self.fetch_all("txlist", address)

    def tx_sender(self, tx_hash: str) -> str | None:
        """Return the address that broadcast ``tx_hash``, or None for an unknown hash.

        Raises :class:`ApiError` when neither the explorer nor the node in
        ``rpc_url`` answered (see :meth:`tx_info`).
        """
        result = self.tx_info(tx_hash)
        if isinstance(result, dict):
            sender = result.get("from")
            return sender.lower() if sender else None
        return None

    def tx_info(self, tx_hash: str) -> dict | None:
        """Return the transaction object for ``tx_hash`` (sender, fees, gas limit), or None.

        Tries the explorer's proxy module first, then the node in ``rpc_url``. None
        means a source answered that the hash is unknown; :class:`ApiError` means no
        source answered.
        """
        return self._tx_lookup("eth_getTransactionByHash", tx_hash)

    def tx_receipt(self, tx_hash: str) -> dict | None:
        """Return the receipt of ``tx_hash`` (status, logs), or None.

        Tries the explorer's proxy module first, then the node in ``rpc_url``. None
        means an unknown hash; :class:`ApiError` means no source answered.
        """
        return self._tx_lookup("eth_getTransactionReceipt", tx_hash)

    def deposit_emitters(self, tx_hash: str) -> set[str] | None:
        """Addresses that emitted a Tornado Deposit event in the transaction; None for an
        unknown hash. Raises :class:`ApiError` when the receipt could not be read."""
        receipt = self.tx_receipt(tx_hash)
        if receipt is None:
            return None
        return {
            (log.get("address") or "").lower()
            for log in receipt.get("logs") or []
            if (log.get("topics") or [None])[0] == TOPIC_DEPOSIT
        }

    def first_activity(self, address: str) -> int | None:
        """Timestamp of the earliest normal or internal transaction of ``address``.

        One single-row query per list, so the cost does not grow with the
        address's history. None when the address has no transactions at all.
        """
        firsts = []
        for action in ("txlist", "txlistinternal"):
            rows = self.call(
                {
                    "module": "account",
                    "action": action,
                    "address": address.lower(),
                    "page": 1,
                    "offset": 1,
                    "sort": "asc",
                }
            )
            if rows and isinstance(rows, list) and rows[0].get("timeStamp"):
                firsts.append(int(rows[0]["timeStamp"]))
        return min(firsts) if firsts else None

    def has_at_least_txs(self, address: str, n: int) -> bool:
        """True when ``address`` sent or received at least ``n`` normal transactions.

        Asks for the single row at position ``n`` (page ``n`` of one row): explorers
        cap a page at 1,000 rows, so asking for ``n`` rows at once would answer
        False for every ``n`` above the cap. One query whatever the history size.
        """
        rows = self.call(
            {
                "module": "account",
                "action": "txlist",
                "address": address.lower(),
                "page": n,
                "offset": 1,
                "sort": "asc",
            }
        )
        return isinstance(rows, list) and len(rows) >= 1

    def internal_txs(self, address: str) -> list[dict]:
        """Return internal transactions touching ``address``.

        ``txlist`` holds only EOA-signed transactions; a deposit made by a Safe
        or another contract wallet reaches the pool as an internal transfer.
        """
        return self.fetch_all("txlistinternal", address)

    def internal_from(self, contract: str, start_block: int, end_block: int) -> list[dict]:
        """Return internal txs sent FROM ``contract`` within a block range."""
        contract = contract.lower()
        rows = self.fetch_all("txlistinternal", contract, start_block, end_block)
        return [r for r in rows if (r.get("from") or "").lower() == contract]

    def outgoing_in_window(
        self, address: str, start_ts: int, end_ts: int, include_internal: bool = True
    ) -> list[dict]:
        """Return ETH sends FROM ``address`` between two timestamps.

        Used for forward-tracing. Combines normal and (optionally) internal
        transactions. Each item is a dict: to, value (ETH), ts, hash.
        """
        address = address.lower()
        actions = ["txlist"] + (["txlistinternal"] if include_internal else [])
        sends = []
        for action in actions:
            for tx in self.fetch_all(action, address):
                if (tx.get("from") or "").lower() != address:
                    continue
                if tx.get("isError") == "1":
                    continue
                ts = int(tx.get("timeStamp", 0))
                if ts < start_ts or ts > end_ts:
                    continue
                sends.append(
                    {
                        "to": (tx.get("to") or "").lower(),
                        "value": int(tx.get("value", "0")) / WEI,
                        "ts": ts,
                        "hash": tx.get("hash", ""),
                    }
                )
        return sends

    def token_transfers(
        self,
        address: str,
        contract: str | None = None,
        start_block: int = 0,
        end_block: int = MAX_BLOCK,
    ) -> list[dict]:
        """Return every ERC-20 transfer touching ``address``.

        ``contract`` narrows the query to one token. Deposit detection does
        *not* pass it: mainnet runs pools in six assets, so one unnarrowed query
        is cheaper than six narrowed ones and the filtering happens locally
        against the pool set. The parameter is here for callers tracing a single
        token forward, where the narrowing is worth a request.
        """
        extra = {"contractaddress": contract.lower()} if contract else None
        return self.fetch_all("tokentx", address, start_block, end_block, extra=extra)

    def outgoing_token_in_window(
        self, address: str, contract: str, start_ts: int, end_ts: int
    ) -> list[dict]:
        """Return ERC-20 sends FROM ``address`` between two timestamps.

        Each item is a dict: to, raw, ts, hash. ``raw`` stays an integer in the
        token's base units; a float would lose precision on an 18-decimal token.
        """
        address = address.lower()
        sends = []
        for tx in self.token_transfers(address, contract):
            if (tx.get("from") or "").lower() != address:
                continue
            ts = int(tx.get("timeStamp", 0))
            if ts < start_ts or ts > end_ts:
                continue
            sends.append(
                {
                    "to": (tx.get("to") or "").lower(),
                    "raw": int(tx.get("value", "0")),
                    "ts": ts,
                    "hash": tx.get("hash", ""),
                }
            )
        return sends


# Throttling answers that deserve a retry.
_RATE_LIMITS = ("rate limit", "max calls", "too many requests", "max rate")

# What a JSON-RPC node says when it throttles (HTTP 429 reaches us as an OSError text).
_NODE_THROTTLE = _RATE_LIMITS + ("429", "rate-limit", "throttl", "capacity", "slow down")


def _node_rate_limited(error):
    lowered = str(error).lower()
    return any(word in lowered for word in _NODE_THROTTLE)


# requests refuses a malformed provider URL before sending anything.
_BAD_URL = (
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
)

# Refusals no retry fixes. Checked after the key rejection and the keyless fallback.
_PERMANENT = (
    "free api access is not supported",
    "invalid address",
    "unknown module",
    "unknown action",
    "missing or invalid action name",
)

_MISS = object()


def _permanent_hint(blob):
    if "free api access is not supported" in blob:
        return (
            " (this chain is not covered by the free Etherscan plan: "
            "use a paid key or a network with a keyless explorer)"
        )
    return ""


def _retry_after(resp):
    """Seconds named by a Retry-After header (capped), or None."""
    try:
        seconds = float((getattr(resp, "headers", None) or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return min(max(seconds, 0.0), BACKOFF_CAP)


def _cache_read(path):
    """The cached result at ``path``, or _MISS (absent, unreadable or not ours)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _MISS
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        return data["result"]
    return _MISS


def _cache_write(path, directory, result):
    """Store ``result`` atomically; a cache that cannot be written is simply absent."""
    tmp = "{}.{}.tmp".format(path, uuid.uuid4().hex)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"result": result}, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass


_INDEXED_TOPIC = re.compile(r"^topic([1-3]|\d_\d_opr)$")


def _topics_match(row, wanted):
    """True when the log row's indexed topics equal ``wanted`` ({index: topic})."""
    topics = [str(t).lower() for t in row.get("topics") or []]
    return all(i < len(topics) and topics[i] == t for i, t in wanted.items())


# Block-range or result-size limits of a node. Deliberately no bare "limit"/"exceed":
# "rate limit exceeded" is throttling and is checked first (_node_rate_limited).
_RANGE_LIMITS = (
    "block range",
    "range is too",
    "range too",
    "too wide",
    "returned more than",
    "response size",
    "limit of",
    "limited to",
    "query exceeds",
    "exceeds the max",
    "max results",
    "too many results",
    "too many logs",
)


def _range_limit(error):
    """True when a node's error says the block range or result set was too big."""
    lowered = str(error).lower()
    return any(word in lowered for word in _RANGE_LIMITS)


def _hex_int(value):
    """Parse a 0x-prefixed hex string, or None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _row_identity(row):
    """What distinguishes two account rows; None for a row without a hash."""
    if not row.get("hash"):
        return None
    return tuple(
        row.get(field)
        for field in (
            "hash",
            "traceId",
            "index",
            "logIndex",
            "from",
            "to",
            "value",
            "contractAddress",
            "_repeat",
        )
    )


def _number_repeats(chunk):
    """Tag identical token-transfer rows within one page (0, 1, ...).

    A token transfer row carries no log index, so two transfers of the same
    amount in one transaction look identical. Within a page they are distinct
    transfers; a row repeated across pages is a re-fetch and keeps _repeat 0.
    Known limit: identical rows split across a page boundary therefore collapse
    into one after de-duplication.
    Other rows are unique by hash or trace id and are left alone.
    """
    seen = Counter()
    for row in chunk:
        if isinstance(row, dict) and "tokenDecimal" in row:
            key = (
                row.get("hash"),
                row.get("from"),
                row.get("to"),
                row.get("value"),
                row.get("contractAddress"),
            )
            row["_repeat"] = seen[key]
            seen[key] += 1
    return chunk


def _dedupe(rows, key):
    """Drop rows whose key was already seen (or is empty), keeping order."""
    seen, unique, keyless = set(), [], 0
    for row in rows:
        k = key(row)
        if not k:
            keyless += 1
        elif k not in seen:
            seen.add(k)
            unique.append(row)
    if keyless:
        _log("[!] dropped {} row(s) without a transaction hash".format(keyless))
    return unique
