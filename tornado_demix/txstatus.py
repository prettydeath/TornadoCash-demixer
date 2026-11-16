"""Success of explorer transaction rows, and the reason name of a partly indexed answer.

A mined transaction that failed keeps its nominal ``value`` in ``txlist`` but moved
nothing. ``txlist`` marks it with ``isError`` 1 (the call reverted) or
``txreceipt_status`` 0 (the receipt status, Byzantium and later; empty before), and
``txlistinternal`` with ``isError`` 1. ``tokentx`` rows need no test: they are logs,
which a failed transaction does not keep.
"""

from __future__ import annotations

# ``EtherscanClient.truncated`` reason of a lookup the explorer answered while part of
# the range was still being indexed (Blockscout status "2"): the rows are valid, the
# list may lack the newest ones.
PARTIAL_INDEXING = "partial_indexing"


def failed(row: dict) -> bool:
    """True for a row of a failed transaction (``isError`` 1 or ``txreceipt_status`` 0)."""
    return str(row.get("isError", "0")).strip() == "1" or (
        str(row.get("txreceipt_status", "1")).strip() == "0"
    )
