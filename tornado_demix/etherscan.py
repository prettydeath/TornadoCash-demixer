"""Minimal Etherscan V2 API client with pagination and rate-limit handling."""

from __future__ import annotations

import re
import sys
import time
from typing import Any

import requests

from .constants import CHAIN_ID, ETHERSCAN_API_URL, WEI
from .errors import ApiError, ApiKeyError, BlockLookupError

# BlockLookupError is re-exported: callers import it from here.
__all__ = [
    "EtherscanClient",
    "BlockLookupError",
    "ApiError",
    "ApiKeyError",
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
        retries: int = 5,
        base_url: str = ETHERSCAN_API_URL,
        style: str = "v2",
    ) -> None:
        self.api_key = api_key
        self.chain_id = chain_id
        self.pause = pause  # optional fixed delay between calls
        self.retries = retries
        # Provider style:
        #   "v2"     -> Etherscan V2 (one host, chainid selects the chain)
        #   "compat" -> an Etherscan-compatible explorer at its own base URL
        #               (Blockscout, Routescan, classic scan). No chainid param.
        self.base_url = base_url
        self.style = style
        self.session = requests.Session()

    def call(self, params: dict) -> Any:
        """Perform one API call and return the ``result`` field.

        Returns an empty list only for a *definite* empty answer - the
        provider replied and said there are no records. Raises
        :class:`~tornado_demix.errors.ApiKeyError` if the key is rejected, and
        :class:`~tornado_demix.errors.ApiError` if the provider never gave a
        usable answer within ``retries`` attempts.

        Returning ``[]`` from an exhausted retry loop would make an expired key,
        a spent quota or an outage indistinguishable from "this wallet touched no
        Tornado pools".
        """
        query = dict(params)
        if self.style == "v2":
            query["chainid"] = self.chain_id
            query["apikey"] = self.api_key
        elif self.api_key:  # compat explorers ignore an unknown key
            query["apikey"] = self.api_key

        last_error = "no attempt completed"
        for attempt in range(self.retries):
            if self.pause:
                time.sleep(self.pause)
            try:
                resp = self.session.get(self.base_url, params=query, timeout=40)
                payload = resp.json()
            except (OSError, ValueError) as exc:
                # Network failure (requests errors are OSErrors) or a body that is not
                # JSON: back off and retry.
                last_error = "{}: {}".format(type(exc).__name__, exc)
                time.sleep(1.5 * (attempt + 1))
                continue

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
                    time.sleep(0.8 * (attempt + 1))
                    continue
                # A null result means the node has never seen this hash; it is
                # returned as None so it stays distinct from an empty list.
                return result

            if status == "1":
                return result
            if isinstance(result, str) and (
                "rate limit" in result.lower() or "max calls" in result.lower()
            ):
                last_error = "rate limited: {}".format(result[:120])
                time.sleep(1.2 * (attempt + 1))
                continue
            if isinstance(result, str) and _is_key_rejection(result):
                raise ApiKeyError(
                    _redact(
                        "the data provider rejected the API key: {}".format(result), self.api_key
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
                return []
            last_error = "status={} message={} result={}".format(
                status, message[:80], str(result)[:80]
            )
            time.sleep(0.8 * (attempt + 1))

        raise ApiError(
            _redact(
                "{} ({}) gave no usable answer after {} attempt(s) for {}: {}".format(
                    self.base_url,
                    query.get("action", "?"),
                    self.retries,
                    query.get("address", query.get("timestamp", "?")),
                    last_error,
                ),
                self.api_key,
            )
        )

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

    def get_logs(self, address: str, topic0: str, start_block: int, end_block: int) -> list[dict]:
        """Return every event log for ``address`` matching ``topic0``.

        Same paging strategy as :meth:`fetch_all`; logs are de-duplicated by
        (transaction hash, log index).
        """
        query = {
            "module": "logs",
            "action": "getLogs",
            "address": address.lower(),
            "topic0": topic0,
        }
        rows = self._paged(
            query, ("fromBlock", "toBlock"), start_block, end_block, lambda b: int(b, 16)
        )
        return _dedupe(rows, lambda r: (r.get("transactionHash"), r.get("logIndex")))

    def _paged(self, query, range_keys, start_block, end_block, parse_block):
        """Collect every row of ``query`` between two blocks.

        Pages through up to MAX_PAGES pages of PAGE_SIZE rows. When all pages
        are full the range is split at the last block seen: rows before it are
        kept and the next pass starts at that block, so none of its rows is lost.
        """
        low_key, high_key = range_keys
        collected, low = [], start_block
        while True:
            rows, overflow = [], True
            for page in range(1, MAX_PAGES + 1):
                chunk = self.call(
                    {**query, low_key: low, high_key: end_block, "page": page, "offset": PAGE_SIZE}
                )
                rows.extend(chunk or [])
                if not chunk or len(chunk) < PAGE_SIZE:
                    overflow = False
                    break
            if not overflow:
                return collected + rows
            max_block = max(parse_block(r["blockNumber"]) for r in rows)
            if max_block <= low:
                return collected + rows  # one block overflows the cap on its own
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
        with closest="after" rejects a future timestamp.
        """
        result = self.call({"module": "proxy", "action": "eth_blockNumber"})
        try:
            return int(result, 16)
        except (TypeError, ValueError) as exc:
            raise BlockLookupError(
                "could not read the current block number: provider returned {!r}".format(result)
            ) from exc

    def outgoing_txs(self, address: str) -> list[dict]:
        """Return every normal transaction sent from ``address``."""
        return self.fetch_all("txlist", address)

    def tx_sender(self, tx_hash: str) -> str | None:
        """Return the address that broadcast ``tx_hash``, or None.

        None covers both an unknown hash and a provider that cannot serve the
        proxy module, so the caller records the lead as unverified rather than
        failing the run.
        """
        try:
            result = self.call(
                {"module": "proxy", "action": "eth_getTransactionByHash", "txhash": tx_hash}
            )
        except ApiError:
            return None
        if isinstance(result, dict):
            sender = result.get("from")
            return sender.lower() if sender else None
        return None

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
        )
    )


def _dedupe(rows, key):
    """Drop rows whose key was already seen (or is empty), keeping order."""
    seen, unique = set(), []
    for row in rows:
        k = key(row)
        if k and k not in seen:
            seen.add(k)
            unique.append(row)
    return unique
