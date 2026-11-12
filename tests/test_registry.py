"""Hash-chained test register: chain, status, locking, witness, report."""

import hashlib
import json
import multiprocessing
import os
import sys

import pytest

from tornado_demix import registry as reg

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)

import registry_report  # noqa: E402

THIS = os.path.abspath(__file__)


def _rec(path, i=0, **kw):
    kw.setdefault("lock_path", str(os.path.join(os.path.dirname(path), "none.lock")))
    return reg.record(f"fam.t{i}", THIS, {"i": i}, {"r": i * 0.5}, path=path, **kw)


def _lines(path):
    with open(path, "rb") as fh:
        return fh.read().split(b"\n")[:-1]


def _worker(path, n):
    for i in range(n):
        reg.record("conc.t", THIS, {"i": i}, {"x": i}, path=path, lock_path=path + ".nolock")


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "reg" / "tests.jsonl")  # directory does not exist yet


def test_first_record_and_link(path):
    a = _rec(path, 0)
    b = _rec(path, 1)
    assert a["seq"] == 0 and a["prev_hash"] == "0" * 64
    assert b["seq"] == 1
    first = _lines(path)[0]
    assert b["prev_hash"] == hashlib.sha256(first).hexdigest()
    assert a["utc"].endswith("Z")
    assert a["script_sha256"] and len(a["script_sha256"]) == 64
    assert json.loads(first)["hash"] == a["hash"]


def test_verify_intact_flip_and_delete(path):
    for i in range(5):
        _rec(path, i)
    assert reg.verify_chain(path) == []
    lines = _lines(path)
    flipped = lines[2].replace(b'"i":2', b'"i":9')
    assert flipped != lines[2]
    with open(path, "wb") as fh:
        fh.write(b"\n".join(lines[:2] + [flipped] + lines[3:]) + b"\n")
    assert reg.verify_chain(path) == [2, 3]
    with open(path, "wb") as fh:
        fh.write(b"\n".join(lines[:2] + lines[3:]) + b"\n")
    assert reg.verify_chain(path) == [3]
    with open(path, "wb") as fh:
        fh.write(b"\n".join([lines[0], lines[2], lines[1]] + lines[3:]) + b"\n")
    assert reg.verify_chain(path) == [1, 2, 3]


def test_witness_raises_on_unparseable_last_line(path):
    _rec(path, 0)
    with open(path, "ab") as fh:
        fh.write(b"not json\n")
    with pytest.raises(reg.RegistryError):
        reg.witness(path)


def test_status_without_and_with_lock(path, tmp_path):
    script = tmp_path / "s.py"
    script.write_text("print(1)\n", encoding="utf-8")
    lock = tmp_path / "PROTOCOL.lock"

    def go():
        return reg.record("a.b", str(script), {}, {}, path=path, lock_path=str(lock))

    assert go()["status"] == "exploratory"  # no lock file
    digest = go()["script_sha256"]
    lock.write_text(f"# locked\n{digest}  s.py\n", encoding="utf-8")
    assert go()["status"] == "confirmatory"
    script.write_text("print(2)\n", encoding="utf-8")
    assert go()["status"] == "exploratory"


def test_nonfinite_and_unserialisable(path):
    r = reg.record(
        "a.b",
        THIS,
        {"p": {1, 2}, "o": object},
        {"x": float("nan"), "y": float("inf"), "z": [float("-inf")]},
        path=path,
        lock_path=path + ".nolock",
    )
    assert r["result"] == {"x": "nan", "y": "inf", "z": ["-inf"]}
    assert reg.verify_chain(path) == []


def test_concurrent_appends(path):
    os.makedirs(os.path.dirname(path))
    ctx = multiprocessing.get_context("spawn")
    ps = [ctx.Process(target=_worker, args=(path, 20)) for _ in range(2)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(120)
        assert p.exitcode == 0
    assert len(_lines(path)) == 40
    assert reg.verify_chain(path) == []
    assert [json.loads(x)["seq"] for x in _lines(path)] == list(range(40))


def test_witness_and_report(path, tmp_path, capsys):
    for i in range(3):
        _rec(path, i)
    w = reg.witness(path)
    assert w["seq"] == 2 and w["hash"] == json.loads(_lines(path)[-1])["hash"]
    s = registry_report.summarize(path)
    assert s["broken"] == [] and s["total"] == 3
    assert s["by_status"] == {"exploratory": 3}
    assert s["by_family"] == {"fam": 3}
    assert list(s["by_script"].values()) == [3]
    out = tmp_path / "elsewhere" / "w.json"
    assert registry_report.main([path, "--witness", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == w
    inside = os.path.join(os.path.dirname(path), "w.json")
    assert registry_report.main([path, "--witness", inside]) != 0


def test_report_flags_broken_chain(path, capsys):
    for i in range(3):
        _rec(path, i)
    lines = _lines(path)
    with open(path, "wb") as fh:
        fh.write(b"\n".join([lines[0], lines[1].replace(b'"i":1', b'"i":7'), lines[2]]) + b"\n")
    assert registry_report.main([path]) == 1
    assert "BROKEN" in capsys.readouterr().out


def test_lock_file_creation(tmp_path):
    script = tmp_path / "s.py"
    script.write_bytes(b"x = 1\n")
    out = tmp_path / "PROTOCOL.lock"
    assert registry_report.main(["--lock", str(script), "--out", str(out)]) == 0
    digest = reg.sha256_file(str(script))
    assert digest in out.read_text(encoding="utf-8")
    assert reg.locked_hashes(str(out)) == {digest}


def test_env_var_overrides_default(tmp_path, monkeypatch):
    p = tmp_path / "e" / "t.jsonl"
    monkeypatch.setenv("TORNADO_DEMIX_REGISTRY", str(p))
    reg.record("a.b", THIS, {}, {}, lock_path=str(tmp_path / "no.lock"))
    assert p.exists()
