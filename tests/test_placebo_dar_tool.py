import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
import placebo_dar  # noqa: E402

from tornado_demix.networks import get_network  # noqa: E402


class _Client:
    def outgoing_txs(self, wallet):
        return []

    def token_transfers(self, wallet):
        return []


@pytest.mark.parametrize("chain", ["ethereum", "polygon"])
def test_deposit_scan_uses_the_network_gas_funder_threshold_and_an_errors_list(
    tmp_path, monkeypatch, chain
):
    network = get_network(chain)
    seen = {}

    def fake(client, wallet, txs, is_contract, labels, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(placebo_dar, "depositor_deposit_addresses", fake)
    placebo_dar.deposit_addresses(
        _Client(), network, "0x" + "1" * 40, {}, lambda a: False, cache=str(tmp_path)
    )
    assert seen["gas_fund_wei"] == network.gas_fund_wei
    assert isinstance(seen["errors"], list)


def test_deposit_scan_with_a_failed_lookup_is_not_cached(tmp_path, monkeypatch):
    def fake(client, wallet, txs, is_contract, labels, errors=None, **kw):
        errors.append({"what": "deposit address check"})
        return []

    monkeypatch.setattr(placebo_dar, "depositor_deposit_addresses", fake)
    with pytest.raises(RuntimeError):
        placebo_dar.deposit_addresses(
            _Client(), get_network("ethereum"), "0x" + "1" * 40, {}, None, cache=str(tmp_path)
        )
    assert list(tmp_path.iterdir()) == []
