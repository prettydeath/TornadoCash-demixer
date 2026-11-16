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


def _module_lines():
    """Lock lines for the library modules loaded now (what a record will carry)."""
    return "".join(f"{h}  {m}  [module]\n" for m, h in reg.library_hashes().items())


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
    lock.write_text(f"# locked\n{digest}  s.py\n" + _module_lines(), encoding="utf-8")
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
    # the library modules are locked too, marked as modules (not confirmatory scripts)
    modules = reg.locked_module_hashes(str(out))
    assert reg.sha256_file(reg.__file__) in modules and digest not in modules


def test_env_var_overrides_default(tmp_path, monkeypatch):
    p = tmp_path / "e" / "t.jsonl"
    monkeypatch.setenv("TORNADO_DEMIX_REGISTRY", str(p))
    reg.record("a.b", THIS, {}, {}, lock_path=str(tmp_path / "no.lock"))
    assert p.exists()


def _locked(tmp_path, path):
    """A lock listing THIS script, next to a register at <tmp>/proj/.cache/registry/."""
    lock = tmp_path / "proj" / "docs" / "PROTOCOL.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{reg.sha256_file(THIS)}  s.py\n" + _module_lines(), encoding="utf-8")
    return str(lock)


def test_default_lock_is_found_from_the_register_not_the_cwd(tmp_path, monkeypatch):
    lock = _locked(tmp_path, None)
    register = tmp_path / "proj" / ".cache" / "registry" / "tests.jsonl"
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert os.path.samefile(reg.lock_for(str(register)), lock)
    rec = reg.record("a.b", THIS, {}, {}, path=str(register))
    assert rec["status"] == "confirmatory"
    # a register outside that layout keeps its lock beside it
    other = tmp_path / "plain" / "t.jsonl"
    assert reg.lock_for(str(other)) == str(tmp_path / "plain" / "PROTOCOL.lock")


def test_unlisted_confirmatory_record_is_not_a_broken_chain(tmp_path, path, capsys):
    lock = _locked(tmp_path, path)
    reg.record("a.b", THIS, {}, {}, path=path, lock_path=lock)
    assert reg.unlisted(path, lock) == []
    empty = tmp_path / "empty.lock"
    empty.write_text("# nothing\n", encoding="utf-8")
    assert reg.unlisted(path, str(empty)) == [0]
    assert reg.verify_chain(path) == []  # the chain itself is intact
    # an exploratory record whose script was locked later is not listed either
    _rec(path, 1)
    assert reg.unlisted(path, lock) == []
    # the report prints it and the exit code is unaffected
    assert registry_report.main([path]) == 0
    assert "not listed in the current lock" in capsys.readouterr().out


def test_verify_against_a_witness_detects_a_truncated_tail(path):
    for i in range(4):
        _rec(path, i)
    w = reg.witness(path)
    assert reg.verify_chain(path, witness=w) == []
    lines = _lines(path)
    with open(path, "wb") as fh:
        fh.write(b"\n".join(lines[:2]) + b"\n")
    assert reg.verify_chain(path) == []  # intact from the file alone
    assert reg.verify_chain(path, witness=w) == [3]
    assert reg.verify_chain(path, witness={"seq": 0, "hash": "0" * 64}) == [0]


def test_a_half_written_last_line_is_reported_and_does_not_block_appends(path, capsys):
    _rec(path, 0)
    _rec(path, 1)
    with open(path, "ab") as fh:
        fh.write(b'{"seq":2,"utc":"2026-')  # killed mid-write, no newline
    assert reg.verify_chain(path) == []
    assert reg.partial_tail(path) == 2
    assert reg.witness(path)["seq"] == 1
    nxt = _rec(path, 3)  # not blocked
    assert nxt["seq"] == 3
    assert reg.partial_tail(path) is None
    assert reg.verify_chain(path) == [2]  # the damaged slot stays visible, never silent


def test_report_prints_a_partial_tail_without_failing(path, capsys):
    _rec(path, 0)
    with open(path, "ab") as fh:
        fh.write(b'{"seq":1,"ut')
    assert registry_report.main([path]) == 0
    assert "half-written" in capsys.readouterr().out


def test_report_checks_the_witness_file(path, tmp_path, capsys):
    for i in range(3):
        _rec(path, i)
    wfile = tmp_path / "w" / "w.json"
    wfile.parent.mkdir()
    assert registry_report.main([path, "--witness", str(wfile)]) == 0
    lines = _lines(path)
    with open(path, "wb") as fh:
        fh.write(b"\n".join(lines[:1]) + b"\n")
    assert registry_report.main([path, "--check-witness", str(wfile)]) == 1
    assert "BROKEN" in capsys.readouterr().out


# --- library modules in each record; external witness -------------------------------


def test_record_carries_the_hashes_of_the_library_modules_in_use(path):
    import tornado_demix.heuristics  # noqa: F401  (loaded, so it is in use)

    rec = _rec(path, 0)
    mods = rec["modules"]
    assert mods["tornado_demix/registry.py"] == reg.sha256_file(reg.__file__)
    assert "tornado_demix/heuristics.py" in mods
    assert all(k.startswith("tornado_demix/") for k in mods)
    assert reg.verify_chain(path) == []


def test_changed_library_module_is_detected_in_the_record(path):
    rec = _rec(path, 0)
    lines = _lines(path)
    forged = lines[0].replace(rec["modules"]["tornado_demix/registry.py"].encode(), b"0" * 64)
    with open(path, "wb") as fh:
        fh.write(forged + b"\n")
    assert reg.verify_chain(path) == [0]


def test_confirmatory_needs_the_library_modules_locked_too(path, tmp_path):
    script = tmp_path / "s.py"
    script.write_text("print(1)\n", encoding="utf-8")
    lock = tmp_path / "PROTOCOL.lock"
    mods = {"tornado_demix/a.py": "a" * 64, "tornado_demix/b.py": "b" * 64}

    def go():
        return reg.record("a.b", str(script), {}, {}, path=path, lock_path=str(lock), modules=mods)

    digest = reg.sha256_file(str(script))
    line_a = f"{'a' * 64}  tornado_demix/a.py  [module]\n"
    line_b = f"{'b' * 64}  tornado_demix/b.py  [module]\n"
    lock.write_text(f"{digest}  s.py\n", encoding="utf-8")
    assert go()["status"] == "exploratory"  # library not locked
    lock.write_text(f"{digest}  s.py\n" + line_a, encoding="utf-8")
    assert go()["status"] == "exploratory"  # one module changed or not locked
    lock.write_text(f"{digest}  s.py\n" + line_a + line_b, encoding="utf-8")
    assert go()["status"] == "confirmatory"
    # a module hash alone never makes its file a confirmatory script
    assert "a" * 64 not in reg.locked_hashes(str(lock))


def _rewrite_from_genesis(path, n):
    """Rewrite the whole chain with other values, every hash recomputed."""
    os.remove(path)
    for i in range(n):
        reg.record(f"fam.t{i}", THIS, {"i": i + 100}, {"r": -1}, path=path, lock_path=path + ".x")


def test_full_rewrite_is_only_detected_with_an_external_witness(path, tmp_path):
    for i in range(3):
        _rec(path, i)
    w = reg.witness(path)
    wfile = tmp_path / "published" / "witness.json"
    wfile.parent.mkdir()
    wfile.write_text(json.dumps(w), encoding="utf-8")
    _rewrite_from_genesis(path, 3)
    assert reg.verify_chain(path) == []  # the file alone cannot show it
    assert reg.verify_chain(path, witness=w) == [2]
    assert reg.verify_chain(path, witness=str(wfile)) == [2]  # a witness file path
    assert reg.verify_chain(path, witness=[str(wfile), reg.witness(path)]) == [2]


def test_verify_chain_with_several_witnesses_checks_each(path):
    _rec(path, 0)
    w0 = reg.witness(path)
    _rec(path, 1)
    w1 = reg.witness(path)
    assert reg.verify_chain(path, witness=[w0, w1]) == []
    assert reg.verify_chain(path, witness=[w0, {"seq": 1, "hash": "f" * 64}]) == [1]


def test_report_accepts_several_witness_files(path, tmp_path, capsys):
    _rec(path, 0)
    a = tmp_path / "a.json"
    a.write_text(json.dumps(reg.witness(path)), encoding="utf-8")
    _rec(path, 1)
    b = tmp_path / "b.json"
    b.write_text(json.dumps(reg.witness(path)), encoding="utf-8")
    assert registry_report.main([path, "--check-witness", str(a), str(b)]) == 0
    _rewrite_from_genesis(path, 2)
    assert registry_report.main([path, "--check-witness", str(a), str(b)]) == 1


def test_report_docs_explain_the_external_anchor():
    doc = registry_report.__doc__
    assert "OpenTimestamps" in doc and "full rewrite" in doc


@pytest.mark.parametrize(
    "bad",
    [{}, {"hash": "a" * 64}, {"seq": "2", "hash": "a" * 64}, {"seq": 1}, {"seq": True, "hash": ""}],
)
def test_a_witness_without_seq_and_hash_is_an_error_not_intact(path, bad):
    _rec(path, 0)
    with pytest.raises(ValueError):
        reg.verify_chain(path, witness=bad)


def test_report_exits_2_on_an_unusable_witness_file(path, tmp_path, capsys):
    _rec(path, 0)
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    nohash = tmp_path / "nohash.json"
    nohash.write_text("{}", encoding="utf-8")
    for f in (empty, nohash, tmp_path / "missing.json"):
        assert registry_report.main([path, "--check-witness", str(f)]) == 2
        out = capsys.readouterr()
        assert "intact" not in out.out


def test_the_witness_of_an_empty_register_is_accepted(path):
    assert reg.verify_chain(path, witness=reg.witness(path)) == []


def test_unlisted_reports_a_confirmatory_record_whose_modules_left_the_lock(path, tmp_path):
    script = tmp_path / "s.py"
    script.write_text("print(1)\n", encoding="utf-8")
    lock = tmp_path / "PROTOCOL.lock"
    mods = {"tornado_demix/a.py": "a" * 64}
    digest = reg.sha256_file(str(script))
    lock.write_text(f"{digest}  s.py\n{'a' * 64}  tornado_demix/a.py  [module]\n", "utf-8")
    rec = reg.record("a.b", str(script), {}, {}, path=path, lock_path=str(lock), modules=mods)
    assert rec["status"] == "confirmatory"
    assert reg.unlisted(path, str(lock)) == []
    lock.write_text(f"{digest}  s.py\n", "utf-8")  # module line removed
    assert reg.unlisted(path, str(lock)) == [0]
