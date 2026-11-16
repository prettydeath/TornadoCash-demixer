"""Hash-chained register of every computed statistical test.

Each evaluation tool appends one JSON line per test result, so the number of tests actually
run (exploratory sweeps included) is known and can feed a multiple-testing correction.
Records are chained: ``prev_hash`` is the SHA-256 of the previous line's exact bytes and
``hash`` covers the record itself, so an edited or deleted line is detected by
:func:`verify_chain`. A truncated tail is not detectable from the file alone; keep a
:func:`witness` (last seq + hash) somewhere else and pass it to :func:`verify_chain`. A full
rewrite from the genesis line (every hash recomputed) is not detectable from the file either:
only an external anchor shows it, a witness published where the writer cannot change it
(a release asset, a Zenodo record, an OpenTimestamps proof of the witness file).
A confirmatory record whose script hash the current lock does not list is reported by
:func:`unlisted`, not as a broken chain (the lock should only ever be appended to). A last
line that was cut off mid-write (no newline) is reported by :func:`partial_tail`; it does
not stop later appends.

Each record also carries ``modules``: the SHA-256 of every ``tornado_demix`` module loaded
when it was written (the library code the script ran with). A record is ``confirmatory`` only
when the protocol lock lists the SHA-256 of the script that produced it and every one of those
module hashes (module lines are tagged ``[module]`` and never make a file a confirmatory
script); with no lock file everything is ``exploratory``. The lock is looked up from the
register's location (:func:`lock_for`), not from the working directory. The lock is a plain
file: anyone with write access can change it, so it too needs an external anchor. Stdlib only; the append is
guarded by an exclusive lock on a sidecar file (msvcrt on Windows, fcntl elsewhere).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

REGISTRY_PATH_DEFAULT = ".cache/registry/tests.jsonl"
PROTOCOL_LOCK_DEFAULT = "docs/PROTOCOL.lock"
ENV_PATH = "TORNADO_DEMIX_REGISTRY"
GENESIS = "0" * 64

try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None
    import fcntl


class RegistryError(RuntimeError):
    """The register on disk cannot be extended (unreadable last line)."""


@contextlib.contextmanager
def _exclusive(lock_file):
    """Cross-process exclusive lock on ``lock_file`` (created if missing)."""
    os.makedirs(os.path.dirname(os.path.abspath(lock_file)), exist_ok=True)
    with open(lock_file, "a+b") as fh:  # bytes, no encoding=
        if msvcrt is not None:
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _clean(x):
    """JSON-safe copy: non-finite floats and non-string keys become strings."""
    if isinstance(x, float) and not math.isfinite(x):
        return str(x)
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _canon(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str, allow_nan=False
    )


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as fh:  # bytes, no encoding=
        return _sha(fh.read())


MODULE_TAG = "[module]"


def _lock_lines(lock_path):
    """``(sha256, is_module)`` per line of a protocol lock (``#`` starts a comment)."""
    if not lock_path or not os.path.isfile(lock_path):
        return []
    out = []
    with open(lock_path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split("#", 1)[0].split()
            if parts:
                out.append((parts[0].lower(), MODULE_TAG in parts[1:]))
    return out


def locked_hashes(lock_path):
    """Script hashes listed in a protocol lock (``sha256  path`` per line, ``#`` comments)."""
    return {h for h, module in _lock_lines(lock_path) if not module}


def locked_module_hashes(lock_path):
    """Library module hashes listed in a protocol lock (lines tagged ``[module]``)."""
    return {h for h, module in _lock_lines(lock_path) if module}


def _package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def library_hashes():
    """``{"tornado_demix/<file>.py": sha256}`` of every package module loaded now."""
    pkg = _package_dir()
    root = os.path.dirname(pkg)
    out = {}
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f or not f.endswith(".py"):
            continue
        f = os.path.abspath(f)
        if not f.startswith(pkg + os.sep):
            continue
        try:
            out[os.path.relpath(f, root).replace(os.sep, "/")] = sha256_file(f)
        except OSError:
            continue
    return dict(sorted(out.items()))


def package_files():
    """Every ``.py`` file of the package, for a protocol lock."""
    pkg = _package_dir()
    out = []
    for folder, _dirs, files in os.walk(pkg):
        out += [os.path.join(folder, f) for f in files if f.endswith(".py")]
    return sorted(out)


def _last_line(fh):
    """Last non-empty line of an open binary file (without newline), or None."""
    size = fh.seek(0, os.SEEK_END)
    chunk = 4096
    while True:
        start = max(0, size - chunk)
        fh.seek(start)
        lines = [x for x in fh.read(size - start).split(b"\n") if x]
        if len(lines) >= 2 or start == 0:
            return lines[-1] if lines else None
        chunk *= 4


def lock_for(register_path):
    """Default protocol lock of a register, found from the register and not from the cwd.

    A register in the standard layout ``<project>/.cache/registry/<file>`` uses
    ``<project>/docs/PROTOCOL.lock``; any other register uses ``PROTOCOL.lock`` beside it.
    """
    folder = os.path.dirname(os.path.abspath(register_path))
    parent = os.path.dirname(folder)
    if os.path.basename(folder) == "registry" and os.path.basename(parent) == ".cache":
        return os.path.join(os.path.dirname(parent), PROTOCOL_LOCK_DEFAULT)
    return os.path.join(folder, "PROTOCOL.lock")


def _resolve(path):
    return path or os.environ.get(ENV_PATH) or REGISTRY_PATH_DEFAULT


def _rel(script_path):
    try:
        rel = os.path.relpath(script_path)
    except ValueError:  # other drive on Windows
        rel = script_path
    return rel.replace(os.sep, "/")


def record(test_id, script_path, params, result, *, path=None, lock_path=None, modules=None):
    """Append one test result to the register and return the record.

    ``modules`` ({path: sha256}) defaults to :func:`library_hashes`.
    """
    path = _resolve(path)
    try:
        digest = sha256_file(script_path)
    except OSError:
        digest = None
    modules = library_hashes() if modules is None else dict(modules)
    lock = lock_for(path) if lock_path is None else lock_path
    locked = bool(digest) and digest in locked_hashes(lock)
    if locked:
        locked = set(modules.values()) <= locked_module_hashes(lock)
    status = "confirmatory" if locked else "exploratory"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _exclusive(path + ".lock"):
        with open(path, "ab+") as fh:  # bytes, no encoding=
            last = _last_line(fh)
            if last is None:
                seq, prev = 0, GENESIS
            else:
                try:
                    seq = int(json.loads(last)["seq"]) + 1
                except (ValueError, KeyError, TypeError) as exc:
                    if not _unterminated(fh):
                        raise RegistryError(f"{path}: last line is not a register record") from exc
                    # cut off mid-write: it keeps its slot (verify_chain lists it), the
                    # new record starts on its own line and the chain goes on
                    seq = _line_count(fh)
                if _unterminated(fh):
                    fh.write(b"\n")
                prev = _sha(last)
            rec = {
                "seq": seq,
                "utc": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "test_id": test_id,
                "script": _rel(script_path),
                "script_sha256": digest,
                "modules": modules,
                "params": _clean(params),
                "result": _clean(result),
                "status": status,
                "prev_hash": prev,
            }
            rec["hash"] = _sha(_canon(rec).encode("utf-8"))
            fh.seek(0, os.SEEK_END)
            fh.write(_canon(rec).encode("utf-8") + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
    return rec


def record_safe(*args, **kwargs):
    """:func:`record` for tools: the register is a side output, so a failure only warns."""
    try:
        return record(*args, **kwargs)
    except (OSError, RegistryError) as exc:
        print(f"warning: test register not updated: {exc}", file=sys.stderr)
        return None


def _unterminated(fh):
    """True when the open binary file does not end with a newline."""
    size = fh.seek(0, os.SEEK_END)
    if size == 0:
        return False
    fh.seek(size - 1)
    return fh.read(1) != b"\n"


def _line_count(fh):
    fh.seek(0)
    return sum(1 for raw in fh.read().split(b"\n") if raw)


def _records(path):
    """(line bytes, parsed record or None) per non-empty line."""
    with open(path, "rb") as fh:  # bytes, no encoding=
        for raw in fh.read().split(b"\n"):
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                rec = rec if isinstance(rec, dict) else None
            except ValueError:
                rec = None
            yield raw, rec


def partial_tail(path):
    """Seq slot of a last line cut off mid-write (unparseable, no newline), else None."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:  # bytes, no encoding=
        data = fh.read()
    if not data or data.endswith(b"\n"):
        return None
    tail = [x for x in data.split(b"\n") if x]
    try:
        rec = json.loads(tail[-1])
    except ValueError:
        return len(tail) - 1
    return None if isinstance(rec, dict) else len(tail) - 1


def unlisted(path, lock_path=None):
    """Seq numbers of ``confirmatory`` records the lock no longer covers.

    A record is listed when the lock does not list its script hash or one of its library
    module hashes. Not a chain error: the lock may have been replaced or shortened after the
    record was written. The lock should be append-only; an entry removed from it shows up here.
    """
    if not os.path.isfile(path):
        return []
    lock = lock_for(path) if lock_path is None else lock_path
    locked, modules = locked_hashes(lock), locked_module_hashes(lock)

    def covered(rec):
        mods = rec.get("modules") or {}
        mods = mods.values() if isinstance(mods, dict) else []
        return str(rec.get("script_sha256")) in locked and all(str(h) in modules for h in mods)

    return sorted(
        rec["seq"]
        for _raw, rec in _records(path)
        if rec is not None
        and rec.get("status") == "confirmatory"
        and isinstance(rec.get("seq"), int)
        and not covered(rec)
    )


def _witnesses(witness):
    """A witness, a witness file path, or a list of either, as a list of dicts.

    ``ValueError`` for a given witness that is not ``{"seq": int, "hash": str}`` (an
    unreadable file raises ``OSError``): a wrong file must never pass as a verification. The
    witness of an empty register (``seq`` None, genesis hash) checks nothing and is skipped.
    """
    if witness is None:
        return []
    items = witness if isinstance(witness, (list, tuple)) else [witness]
    out = []
    for w in items:
        if isinstance(w, (str, os.PathLike)):
            with open(w, encoding="utf-8") as fh:
                w = json.load(fh)
        if not isinstance(w, dict):
            raise ValueError(f"witness is not an object: {w!r:.60}")
        seq, digest = w.get("seq"), w.get("hash")
        if seq is None and digest == GENESIS:
            continue
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError(f"witness has no integer seq: {w!r:.80}")
        if not isinstance(digest, str) or not digest:
            raise ValueError(f"witness has no hash: {w!r:.80}")
        out.append(w)
    return out


def verify_chain(path, witness=None):
    """Seq numbers of broken records; [] = intact.

    A record is broken when its hash, ``prev_hash`` or seq is wrong. ``witness`` is a
    ``{"seq", "hash"}`` from :func:`witness`, the path of a JSON file holding one, or a list
    of either: a register that lacks the witnessed record, or holds another one under that
    seq, is broken at the witness seq (a truncated tail, or a chain rewritten after the
    witness was taken). Without an external witness a full rewrite from genesis verifies
    clean. A half-written last line is not listed here, see :func:`partial_tail`.
    """
    witnesses = _witnesses(witness)
    if not os.path.isfile(path):
        return sorted({w["seq"] for w in witnesses})
    tail = partial_tail(path)
    broken, prev, expected = set(), GENESIS, 0
    hashes = {}
    for raw, rec in _records(path):
        if rec is None:
            if expected != tail:
                broken.add(expected)
            prev, expected = _sha(raw), expected + 1
            continue
        seq = rec.get("seq")
        seq = seq if isinstance(seq, int) else expected
        body = {k: v for k, v in rec.items() if k != "hash"}
        try:
            bad = rec.get("hash") != _sha(_canon(body).encode("utf-8"))
        except (TypeError, ValueError):
            bad = True
        if bad or rec.get("prev_hash") != prev or seq != expected:
            broken.add(seq)
        hashes[seq] = rec.get("hash")
        prev, expected = _sha(raw), seq + 1
    for w in witnesses:
        if hashes.get(w["seq"]) != w.get("hash"):
            broken.add(w["seq"])
    return sorted(broken)


def witness(path):
    """``{"seq", "hash"}`` of the last record; keep it outside the register directory."""
    last, seen = None, False
    tail = partial_tail(path)
    if os.path.isfile(path):
        for n, (_raw, rec) in enumerate(_records(path)):
            if n == tail:
                continue
            last, seen = rec, True
    if not seen:
        return {"seq": None, "hash": GENESIS}
    if last is None or "seq" not in last or "hash" not in last:
        raise RegistryError(f"{path}: last line is not a register record")
    return {"seq": last["seq"], "hash": last["hash"]}
