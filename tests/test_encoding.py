"""Every file the toolkit writes is UTF-8, whatever the operator's locale.

Python opens files in ``locale.getpreferredencoding()`` unless told otherwise.
On a non-English Windows install that is a legacy ANSI codepage - cp1251,
cp1252, cp932 - so a report written without an explicit encoding is not the
UTF-8 its own ``<meta charset>`` claims, and a CSV is unreadable on any machine
with a different codepage. Neither failure raises: the file is written, it is
simply wrong. These tests pin the encoding at every writer and reader.
"""

import csv
import json
import os

import pytest

from tests.fixtures import demix_result as fx
from tornado_demix import networks
from tornado_demix.cluster import ForwardCache
from tornado_demix.config import PACKAGE_DATA
from tornado_demix.report import _write, build_html_report, write_demix_csv, write_html_report

SHIPPED_REGISTRY = os.path.join(PACKAGE_DATA, "networks.csv")


def test_the_html_report_is_valid_utf8_as_it_declares(tmp_path):
    """The report always emits U+00B7 in its own header; cp1251 makes that 0xB7."""
    path = tmp_path / "report.html"
    write_html_report(fx.two_pool_result(), str(path), fx.network())
    raw = path.read_bytes()
    assert b'charset="utf-8"' in raw
    raw.decode("utf-8")  # would raise on a locale-encoded write


def test_the_report_really_does_contain_non_ascii():
    """Guard the test above: it only proves anything while this holds."""
    html = build_html_report(fx.two_pool_result(), fx.network())
    assert any(ord(c) > 127 for c in html)


def test_csv_is_utf8_and_survives_a_value_outside_the_ansi_codepage(tmp_path):
    path = str(tmp_path / "x.csv")
    _write(path, ["network", "address"], [["以太坊", "0xabc"]])
    with open(path, newline="", encoding="utf-8-sig") as fh:
        assert list(csv.reader(fh))[1] == ["以太坊", "0xabc"]


def test_csv_has_no_bom_so_grep_and_pandas_see_a_clean_header(tmp_path):
    """A BOM would surface as a stray character before the first column name."""
    path = str(tmp_path / "x.csv")
    _write(path, ["network"], [["ethereum"]])
    raw = open(path, "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.startswith(b"network")


def test_demix_csvs_are_utf8(tmp_path):
    out = str(tmp_path / "out")
    for path in write_demix_csv(fx.two_pool_result(), out, fx.network()):
        open(path, "rb").read().decode("utf-8")


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_a_cell_a_spreadsheet_would_evaluate_is_neutralised(tmp_path, prefix):
    path = str(tmp_path / "x.csv")
    _write(path, ["asset"], [[prefix + "cmd|'/c calc'!A1"]])
    with open(path, newline="", encoding="utf-8-sig") as fh:
        cell = list(csv.reader(fh))[1][0]
    assert cell.startswith("'")
    assert not cell.startswith(prefix)


def test_the_registry_parses_even_with_an_excel_written_bom(tmp_path):
    """A registry round-tripped through Excel gains a BOM.

    Read as plain utf-8 the header's first column becomes '\ufeffnetwork', so
    every row reports no network name and is skipped - a registry that silently
    holds nothing.
    """
    src = open(SHIPPED_REGISTRY, encoding="utf-8").read()
    path = tmp_path / "networks.csv"
    path.write_text(src, encoding="utf-8-sig")
    nets = networks.load_networks(str(path))
    assert sorted(nets) == [
        "arbitrum",
        "avalanche",
        "base",
        "bsc",
        "ethereum",
        "gnosis",
        "optimism",
        "polygon",
    ]
    assert sum(len(n.pools) for n in nets.values()) == 55


def test_the_forward_cache_round_trips_utf8(tmp_path):
    path = str(tmp_path / "fwd.json")
    cache = ForwardCache(path)
    cache.put("1 ETH:0xabc", [{"to": "0xdef", "asset": "ETH"}])
    assert json.loads(open(path, encoding="utf-8").read())
    assert ForwardCache(path).get("1 ETH:0xabc") == [{"to": "0xdef", "asset": "ETH"}]


def test_wallets_csv_with_a_bom_still_loads(tmp_path):
    from tornado_demix import config

    path = tmp_path / "wallets.csv"
    path.write_text("address,label\n0x%040x,suspect-1\n" % 1, encoding="utf-8-sig")
    assert config.load_wallets(str(path)) == ["0x" + "0" * 39 + "1"]


def test_api_csv_with_a_bom_still_loads(tmp_path):
    from tornado_demix import config

    path = tmp_path / "api.csv"
    path.write_text("service,api_key\netherscan,KEY123\n", encoding="utf-8-sig")
    assert config.load_api_key(str(path)) == "KEY123"


def test_no_shipped_module_opens_a_file_without_an_encoding():
    """A new `open(path, "w")` reintroduces the whole class of defect."""
    offenders = []
    for folder in ("tornado_demix", "tools", "webui"):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            full = os.path.join(folder, name)
            for lineno, line in enumerate(open(full, encoding="utf-8"), 1):
                if (
                    "open(" in line
                    and "encoding=" not in line
                    and not line.lstrip().startswith("#")
                    and "io.StringIO" not in line
                ):
                    offenders.append("{}:{}".format(full, lineno))
    assert offenders == []
