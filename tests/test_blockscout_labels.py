"""Blockscout metadata as a label source: checksums, batching, retries, classes, lineage."""

import pytest

from tornado_demix import blockscout_labels as bl

# The four test vectors of the EIP-55 specification text.
EIP55 = [
    "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
    "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
    "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
    "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
]


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Session:
    """Replays queued responses; once empty, answers each query from ``tags_for``."""

    def __init__(self, responses=None, tags_for=None):
        self.responses = list(responses or [])
        self.calls = []
        self.tags_for = tags_for

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        if self.responses:
            return self.responses.pop(0)
        out = {}
        for addr in params["addresses"].split(","):
            tags = self.tags_for(addr) if self.tags_for else []
            if tags:
                out[addr] = {"tags": tags}
        return _Resp(200, {"addresses": out})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bl, "_sleep", lambda s: None)


@pytest.mark.parametrize("checksummed", EIP55)
def test_checksum_matches_eip55_vectors(checksummed):
    assert bl.to_checksum(checksummed.lower()) == checksummed
    assert bl.to_checksum("0x" + checksummed[2:].upper()) == checksummed


def test_checksum_rejects_malformed():
    with pytest.raises(ValueError):
        bl.to_checksum("0x1234")


def test_batches_of_twenty_and_checksummed_query():
    addrs = ["0x{:040x}".format(i + 1) for i in range(45)]
    s = _Session()
    assert bl.fetch_tags(137, addrs, session=s) == {}
    assert len(s.calls) == 3
    assert [len(c["addresses"].split(",")) for c in s.calls] == [20, 20, 5]
    assert all(c["chainId"] == 137 and c["tagsLimit"] == 20 for c in s.calls)
    assert s.calls[0]["addresses"].split(",") == [bl.to_checksum(a) for a in addrs[:20]]


def test_keys_come_back_lowercase_and_unrequested_dropped():
    a = EIP55[0]
    tag = {"name": "Binance: Hot Wallet 2", "tagType": "name"}
    s = _Session([_Resp(200, {"addresses": {a: {"tags": [tag]}, EIP55[1]: {"tags": [tag]}}})])
    assert bl.fetch_tags(137, [a.lower()], session=s) == {a.lower(): [tag]}


def test_duplicates_are_queried_once():
    s = _Session()
    bl.fetch_tags(137, [EIP55[0], EIP55[0].lower()], session=s)
    assert len(s.calls[0]["addresses"].split(",")) == 1


def test_retry_then_success():
    a = EIP55[0]
    ok = _Resp(200, {"addresses": {a: {"tags": [{"name": "Coinbase 16", "tagType": "name"}]}}})
    s = _Session([_Resp(429), _Resp(503), ok])
    assert list(bl.fetch_tags(137, [a], session=s)) == [a.lower()]
    assert len(s.calls) == 3


def test_retry_exhausted_raises_never_reads_as_no_tags():
    s = _Session([_Resp(500), _Resp(502), _Resp(429)])
    with pytest.raises(bl.BlockscoutError):
        bl.fetch_tags(137, [EIP55[0]], session=s)
    assert len(s.calls) == 3


def test_client_error_raises_without_retry():
    s = _Session([_Resp(400, {"message": "bad"})])
    with pytest.raises(bl.BlockscoutError):
        bl.fetch_tags(137, [EIP55[0]], session=s)
    assert len(s.calls) == 1


def test_malformed_body_raises():
    with pytest.raises(bl.BlockscoutError):
        bl.fetch_tags(137, [EIP55[0]], session=_Session([_Resp(200, {"oops": 1})]))


@pytest.mark.parametrize(
    "name,tag_type,category,entity",
    [
        ("Binance: Hot Wallet 2", "name", "exchange", "Binance"),
        ("Coinbase 16", "name", "exchange", "Coinbase"),
        ("Bybit: Wallet 131", "name", "exchange", "Bybit"),
        ("OKX 5", "name", "exchange", "OKX"),
        ("Crypto.com: Hot Wallet", "name", "exchange", "Crypto.com"),
        ("Crypto.com 3", "name", "exchange", "Crypto.com"),
        ("KuCoin: Hot", "name", "exchange", "KuCoin"),
        ("Kraken 4", "name", "exchange", "Kraken"),
        ("Gate.io: Wallet", "name", "other", "Gate.io: Wallet"),
        ("Huobi 12", "name", "exchange", "HTX"),
        ("HTX: Hot Wallet", "name", "exchange", "HTX"),
        ("binance: cold", "name", "exchange", "Binance"),
        ("Binance Smart Chain: Token", "name", "other", "Binance Smart Chain: Token"),
        ("Binance: Bridge", "name", "other", "Binance: Bridge"),
        ("Coinbase Router", "name", "other", "Coinbase Router"),
        ("Bybit: Contract 2", "name", "other", "Bybit: Contract 2"),
        ("Binancefake 1", "name", "other", "Binancefake 1"),
        ("Binance", "name", "other", "Binance"),
        ("Uniswap V3: Router", "protocol", "other", "Uniswap V3: Router"),
        ("DEPOSIT ADDRESS", "generic", "deposit", "DEPOSIT ADDRESS"),
    ],
)
def test_brand_classification_table(name, tag_type, category, entity):
    addr = "0x" + "1" * 40
    lab = bl.to_labels({addr: [{"name": name, "tagType": tag_type}]})[addr]
    assert (lab["category"], lab["entity"]) == (category, entity)
    assert lab["source"] == "blockscout" and lab["lineage"] == "blockscout"
    assert lab["tag"] == name


def test_exchange_beats_deposit_beats_other_and_empty_skipped():
    a, b, c = ("0x" + ch * 40 for ch in "123")
    out = bl.to_labels(
        {
            a: [{"name": "DEPOSIT ADDRESS", "tagType": "generic"}, {"name": "Binance 8"}],
            b: [{"name": "Foo"}, {"name": "DEPOSIT ADDRESS", "tagType": "generic"}],
            c: [],
        }
    )
    assert out[a]["category"] == "exchange" and out[b]["category"] == "deposit"
    assert c not in out


def test_independent_sources_counts_lineages():
    bs = {"source": "blockscout", "lineage": "blockscout"}
    assert bl.independent_sources(bs) == 1
    assert bl.independent_sources({"sources": [{"source": "etherscan"}, bs]}) == 1
    assert bl.independent_sources({"sources": [{"source": "etherscan-labels"}, bs]}) == 1
    assert bl.independent_sources({"sources": [{"source": "vendor-x"}, bs]}) == 2
    assert bl.independent_sources({"sources": [{"source": "vendor-x"}, {"source": "other"}]}) == 2
    assert bl.independent_sources({}) == 0


def test_merge_keeps_base_and_marks_same_lineage_as_one():
    a = "0x" + "1" * 40
    base = {a: {"entity": "Binance", "category": "exchange", "source": "etherscan", "label": "x"}}
    merged = bl.merge_labels(base, bl.to_labels({a: [{"name": "Binance: Hot Wallet 2"}]}))
    assert merged[a]["entity"] == "Binance" and merged[a]["label"] == "x"
    assert merged[a]["independent_sources"] == 1
    assert [s["source"] for s in merged[a]["sources"]] == ["etherscan", "blockscout"]
    assert "sources" not in base[a]  # input not mutated (shared cache)


def test_merge_different_lineage_counts_two_and_new_address_added():
    a, b = "0x" + "1" * 40, "0x" + "2" * 40
    base = {a: {"entity": "Binance", "category": "exchange", "source": "vendor-x"}}
    extra = bl.to_labels({a: [{"name": "Binance 1"}], b: [{"name": "Coinbase 2"}]})
    merged = bl.merge_labels(base, extra)
    assert merged[a]["independent_sources"] == 2
    assert merged[b]["category"] == "exchange" and merged[b]["independent_sources"] == 1


def test_merge_conflicting_entity_does_not_corroborate():
    a = "0x" + "1" * 40
    base = {a: {"entity": "Kraken", "category": "exchange", "source": "vendor-x"}}
    merged = bl.merge_labels(base, bl.to_labels({a: [{"name": "Binance 1"}]}))
    assert merged[a]["entity"] == "Kraken" and merged[a]["independent_sources"] == 1


def test_cache_roundtrip(tmp_path):
    a = "0x" + "1" * 40
    labels = bl.to_labels({a: [{"name": "Binance 1"}]})
    path = bl.write_cache(137, labels, directory=str(tmp_path), now="2026-11-12T00:00:00Z")
    assert path.endswith("137.csv")
    assert (
        open(path, encoding="utf-8").readline().strip() == "address,category,entity,tag,fetched_utc"
    )
    assert bl.load_cache(137, directory=str(tmp_path))[a]["entity"] == "Binance"
    assert bl.load_cache(1, directory=str(tmp_path)) == {}


def test_fetch_and_cache_accumulates(tmp_path):
    a, b = EIP55[0].lower(), EIP55[1].lower()
    s1 = _Session(tags_for=lambda x: [{"name": "Binance 1"}] if x.lower() == a else [])
    bl.fetch_and_cache(137, [a], session=s1, directory=str(tmp_path))
    s2 = _Session(tags_for=lambda x: [{"name": "Coinbase 9"}] if x.lower() == b else [])
    assert set(bl.fetch_and_cache(137, [b], session=s2, directory=str(tmp_path))) == {a, b}


@pytest.mark.live
def test_live_polygon_binance_and_coinbase():
    binance = "0xe7804c37c13166ff0b37f5ae0bb07a3aebb6e245"
    coinbase = "0x9810762578accf1f314320cca5b72506ae7d7630"
    labels = bl.to_labels(bl.fetch_tags(137, [binance, coinbase]))
    assert (labels[binance]["category"], labels[binance]["entity"]) == ("exchange", "Binance")
    assert (labels[coinbase]["category"], labels[coinbase]["entity"]) == ("exchange", "Coinbase")


def test_cli_load_labels_is_opt_in(tmp_path, monkeypatch):
    from tornado_demix import cli

    a = "0x" + "1" * 40
    bl.write_cache(
        137,
        bl.to_labels({a: [{"name": "Binance 1"}]}),
        directory=str(tmp_path / "labels" / "blockscout"),
    )
    monkeypatch.setattr(cli, "load_attribution", lambda *a, **k: {})
    net = type("N", (), {"name": "polygon", "chain_id": 137})()
    args = cli.build_parser().parse_args(["demix", "0x" + "2" * 40, "--cache-dir", str(tmp_path)])
    assert cli._load_labels(args, net) == {}
    args = cli.build_parser().parse_args(
        ["demix", "0x" + "2" * 40, "--cache-dir", str(tmp_path), "--labels-blockscout"]
    )
    assert cli._load_labels(args, net)[a]["category"] == "exchange"


def test_cli_labels_blockscout_command(tmp_path, monkeypatch, capsys):
    from tornado_demix import cli

    a = "0x" + "1" * 40
    monkeypatch.setattr(
        cli, "fetch_tags", lambda chain, addrs, session=None: {a: [{"name": "Coinbase 16"}]}
    )
    args = cli.build_parser().parse_args(
        [
            "labels",
            "blockscout",
            "--network",
            "polygon",
            "--address",
            a,
            "--out",
            str(tmp_path / "o.csv"),
        ]
    )
    args.func(args)
    assert bl.load_cache(137, directory=str(tmp_path / "x")) == {}
    rows = (tmp_path / "o.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "address,category,entity,tag,fetched_utc" and a in rows[1]
    assert "1 of 1" in capsys.readouterr().out
