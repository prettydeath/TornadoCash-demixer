"""A withdrawal sender's direct link to the depositor is placed against the withdrawals it
would explain: a link made after them says so (with both dates), and "unknown" stays only
when those times are not known. Synthetic addresses (modelled on a public case where the
sender's only link to the depositor came months after the withdrawals it sent)."""

import re
from datetime import datetime, timezone

from tests.fixtures import demix_result as fx
from tornado_demix.heuristics import (
    apply_heuristics,
    candidate_reason,
    link_description,
    ranked_candidates,
)
from tornado_demix.report_html import build_html_report

SENDER = "0x" + "5e" * 20
LINK_TX = "0x" + "3f" * 32


def _utc(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


W1, W2 = _utc(2020, 10, 23, 14, 0), _utc(2020, 10, 26, 6, 59)
LINK_TS = _utc(2021, 3, 2, 2, 50)


def _link(ts=LINK_TS):
    return {
        "amount": 0.2,
        "asset": "ETH",
        "block": 11956196,
        "decimals": 18,
        "direction": "in",
        "first_withdrawal_ts": None,
        "floor_raw": 10**15,
        "kind": "native",
        "lead_seconds": None,
        "links": 1,
        "position": "unknown",
        "qualifies": True,
        "raw": 2 * 10**17,
        "token": None,
        "ts": ts,
        "tx": LINK_TX,
        "why": None,
    }


def _data(link_ts=LINK_TS, known=True):
    data = fx.two_pool_result()
    res = data["denoms"]["1 ETH"]
    for rec, ts in zip(res["detail"][fx.ALICE], (W1, W2)):
        rec["ts"] = ts
    apply_heuristics(data, counterparties=set())
    res["signals"][fx.ALICE] = sorted(set(res["signals"].get(fx.ALICE, [])) | {"linked_sender"})
    data.setdefault("heuristics", {})["linked_senders"] = [
        ["1 ETH", fx.ALICE, SENDER, rec["hash"] if known else "0x" + "0" * 63 + str(i)]
        for i, rec in enumerate(res["detail"][fx.ALICE])
    ]
    data["counterparty_links"] = {SENDER: _link(link_ts)}
    return data


def _alice(data):
    return next(r for r in ranked_candidates(data) if r["address"] == fx.ALICE)


def test_post_hoc_sender_link_says_it_came_after_the_withdrawals():
    why = candidate_reason(_alice(_data()))
    assert "2021-03-02" in why
    assert "after the withdrawals it would explain (2020-10-23..26)" in why
    assert "context" in why
    assert "time of the first withdrawal to it unknown" not in why


def test_html_timeline_row_says_the_same():
    text = re.sub(r"<[^>]+>", "", build_html_report(_data(), fx.network()))
    assert "after the withdrawals it would explain (2020-10-23..26)" in text
    assert "time of the first withdrawal to it unknown" not in text


def test_link_before_the_withdrawals_is_stated_as_before():
    why = candidate_reason(_alice(_data(link_ts=_utc(2020, 10, 20))))
    assert "before the withdrawals it would explain" in why


def test_unknown_only_when_the_withdrawal_times_are_unknown():
    # the sent withdrawals are not among the recorded ones, so their times are unknown
    why = candidate_reason(_alice(_data(known=False)))
    assert "time of the first withdrawal to it unknown" in why
    assert link_description(_link()).endswith("time of the first withdrawal to it unknown")
