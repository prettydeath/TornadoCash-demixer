"""Address attribution: loading a per-network label table and looking addresses up."""

import os

from tornado_demix.attribution import (
    ATTRIBUTION_ENV,
    attribution_file,
    format_label,
    label_of,
    load_attribution,
)

ROUTER = "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b"
CEX = "0x" + "a" * 40


def _write(dir_path, network, rows):
    path = os.path.join(dir_path, network + ".csv")
    header = "address,network,entity,label,category,source,source_url,confidence,last_updated\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header)
        for r in rows:
            fh.write(",".join(r) + "\n")
    return path


def test_load_maps_lowercased_addresses_to_labels(tmp_path):
    _write(
        str(tmp_path),
        "ethereum",
        [
            [
                ROUTER.upper(),
                "ethereum",
                "blocked",
                "Tornado.Cash: Router",
                "mixer",
                "eth-labels",
                "http://x",
                "medium",
                "2026-07-17",
            ],
            [
                CEX,
                "ethereum",
                "Binance",
                "Binance: Hot Wallet",
                "exchange",
                "eth-labels",
                "http://x",
                "high",
                "2026-07-17",
            ],
        ],
    )
    labels = load_attribution("ethereum", directory=str(tmp_path))
    assert set(labels) == {ROUTER.lower(), CEX.lower()}
    assert labels[ROUTER.lower()]["category"] == "mixer"
    assert labels[CEX.lower()]["entity"] == "Binance"


def test_label_of_is_case_insensitive(tmp_path):
    _write(
        str(tmp_path),
        "ethereum",
        [[ROUTER, "ethereum", "blocked", "Tornado.Cash: Router", "mixer", "s", "u", "medium", "d"]],
    )
    labels = load_attribution("ethereum", directory=str(tmp_path))
    assert label_of(labels, ROUTER.upper())["label"] == "Tornado.Cash: Router"
    assert label_of(labels, CEX) is None
    assert label_of({}, ROUTER) is None


def test_missing_dataset_returns_empty_not_error(tmp_path):
    # No file for this network -> empty dict, callers degrade to unlabelled.
    assert load_attribution("ethereum", directory=str(tmp_path)) == {}


def test_format_label_reads_label_then_category():
    assert (
        format_label({"label": "Tornado.Cash: Router", "category": "mixer"})
        == "Tornado.Cash: Router (mixer)"
    )
    assert (
        format_label({"entity": "Binance", "label": "", "category": "exchange"})
        == "Binance (exchange)"
    )
    assert format_label(None) == ""
    assert format_label({"label": "X", "category": ""}) == "X"


def test_explicit_directory_scopes_and_does_not_fall_through(tmp_path, monkeypatch):
    # An env dataset exists, but an explicit directory without the file must NOT
    # silently borrow it - a label the report calls authoritative must come from
    # the named place.
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    _write(
        str(env_dir),
        "ethereum",
        [[ROUTER, "ethereum", "blocked", "Tornado.Cash: Router", "mixer", "s", "u", "medium", "d"]],
    )
    monkeypatch.setenv(ATTRIBUTION_ENV, str(env_dir))
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert attribution_file("ethereum", directory=str(empty_dir)) is None
    assert load_attribution("ethereum", directory=str(empty_dir)) == {}


def test_env_var_resolution(tmp_path, monkeypatch):
    _write(
        str(tmp_path),
        "ethereum",
        [[ROUTER, "ethereum", "blocked", "Tornado.Cash: Router", "mixer", "s", "u", "medium", "d"]],
    )
    monkeypatch.setenv(ATTRIBUTION_ENV, str(tmp_path))
    labels = load_attribution("ethereum")  # no explicit directory
    assert ROUTER.lower() in labels


def test_config_dir_resolution(tmp_path, monkeypatch):
    attrib = tmp_path / "attribution"
    attrib.mkdir()
    _write(
        str(attrib),
        "ethereum",
        [[ROUTER, "ethereum", "blocked", "Tornado.Cash: Router", "mixer", "s", "u", "medium", "d"]],
    )
    monkeypatch.delenv(ATTRIBUTION_ENV, raising=False)
    monkeypatch.setenv("TORNADO_DEMIX_CONFIG", str(tmp_path))
    labels = load_attribution("ethereum")
    assert ROUTER.lower() in labels


def test_a_network_name_with_a_path_separator_is_refused():
    assert attribution_file("../etc/passwd") is None
    assert attribution_file("a/b") is None


def test_load_is_cached_by_path(tmp_path):
    _write(
        str(tmp_path),
        "ethereum",
        [[ROUTER, "ethereum", "blocked", "R", "mixer", "s", "u", "m", "d"]],
    )
    first = load_attribution("ethereum", directory=str(tmp_path))
    second = load_attribution("ethereum", directory=str(tmp_path))
    assert first is second  # served from cache


def test_format_label_falls_back_to_labelled_when_nameless():
    assert format_label({"label": "", "entity": "", "category": "mixer"}) == "labelled (mixer)"


def test_a_row_without_an_address_is_skipped(tmp_path):
    path = os.path.join(str(tmp_path), "ethereum.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "address,network,entity,label,category,source,source_url,confidence,last_updated\n"
        )
        fh.write(",ethereum,x,y,exchange,s,u,h,d\n")  # blank address
        fh.write(f"{ROUTER},ethereum,blocked,R,mixer,s,u,m,d\n")
    labels = load_attribution("ethereum", directory=str(tmp_path))
    assert set(labels) == {ROUTER.lower()}


def test_commas_inside_quoted_fields_parse_correctly(tmp_path):
    # A label with an embedded comma (e.g. "Coinbase, Inc.") must not shift columns.
    path = os.path.join(str(tmp_path), "ethereum.csv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "address,network,entity,label,category,source,source_url,confidence,last_updated\n"
        )
        fh.write(
            f'{CEX},ethereum,"Coinbase, Inc.","Coinbase, Inc.: Deposit",'
            f"exchange,src,url,high,2026-07-17\n"
        )
    labels = load_attribution("ethereum", directory=str(tmp_path))
    assert labels[CEX.lower()]["category"] == "exchange"
    assert labels[CEX.lower()]["entity"] == "Coinbase, Inc."
