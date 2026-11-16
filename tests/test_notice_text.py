"""Each notice-only result explains its own notice: the RPC-fallback sentence only for the
RPC fallback, the early-profile notes their own text, and the CLI line never ends in a bare colon."""

from tornado_demix import cli
from tornado_demix.incomplete import notice_lines, notice_only, summary
from tornado_demix.report_html import _incomplete_html

ADDR = "0x" + "ab" * 20
GAS = "a node log has no gas price"
WINDOW = "early profile not applied: window shorter than 72 h"
POOL = "early profile not applied: pool uncertain"
HISTORY = "early profile not applied: withdrawal history incomplete"


def _rpc():
    return {
        "errors": [],
        "truncated": [
            {"action": "getLogs", "address": ADDR, "reason": "rpc_fallback_no_gas_price"}
        ],
    }


def _early(note):
    return {"errors": [], "truncated": [], "heuristics": {"early_profile_note": note}}


def test_rpc_fallback_keeps_its_sentence():
    html = _incomplete_html(_rpc())
    assert notice_only(_rpc()) and GAS in html


def test_early_profile_window_note_has_its_own_text():
    data = _early(WINDOW)
    assert notice_only(data)
    html = _incomplete_html(data)
    assert GAS not in html
    assert "72 h" in html and "early-withdrawal profile" in html


def test_every_early_profile_note_is_explained_without_the_gas_sentence():
    for note in (WINDOW, POOL, HISTORY):
        text = " ".join(notice_lines(_early(note)))
        assert text and GAS not in text


def test_both_notices_explain_both():
    data = _rpc()
    data["heuristics"] = {"early_profile_note": WINDOW}
    html = _incomplete_html(data)
    assert GAS in html and "early-withdrawal profile" in html


def test_cli_notice_line_is_not_a_bare_colon(capsys):
    data = _early(WINDOW)
    cli._print_incomplete(data)
    out = capsys.readouterr().out
    head = [ln for ln in out.splitlines() if ln.startswith("[!]")][0]
    assert head == f"[!] {summary(data, dash='-')}:"
    body = out.split(head, 1)[1].strip()
    assert body and "72 h" in body


def test_cli_rpc_notice_unchanged(capsys):
    cli._print_incomplete(_rpc())
    out = capsys.readouterr().out
    assert "read from the RPC node" in out and "gas-price signal" in out
