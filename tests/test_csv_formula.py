"""CSV cells that a spreadsheet could evaluate as a formula are escaped."""

import csv

import pytest

from tornado_demix import report_csv


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(A1)",
        "\t=1+1",
        "\r=1+1",
        "\n=1+1",
        "﻿=1+1",
        "\x00=1+1",
        "\x1f=1+1",
        "​=1+1",
        " =1+1",
        " =1+1",
        "\x07text",
        "﻿text",
    ],
)
def test_a_cell_that_may_start_a_formula_gets_an_apostrophe(value):
    assert report_csv._sanitize_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["", "abc", "0x" + "a" * 40, "a=b", "1.5", 3, -2, None, 0.25])
def test_plain_cells_are_unchanged(value):
    assert report_csv._sanitize_cell(value) == value


def test_header_and_rows_are_both_escaped_in_the_file(tmp_path):
    path = tmp_path / "x.csv"
    report_csv._write(str(path), ["=h", "ok"], [["﻿=cmd", "fine"], ["@x", "-y"]])
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["'=h", "ok"]
    assert rows[1] == ["'﻿=cmd", "fine"]
    assert rows[2] == ["'@x", "'-y"]
