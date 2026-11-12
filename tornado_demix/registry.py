"""Hash-chained register of every computed statistical test.

Each evaluation tool appends one JSON line per test result, so the number of tests actually
run (exploratory sweeps included) is known and can feed a multiple-testing correction.
Records are chained: ``prev_hash`` is the SHA-256 of the previous line's exact bytes and
``hash`` covers the record itself, so an edited or deleted line is detected by
:func:`verify_chain`. A truncated tail is not detectable from the file alone; keep a
:func:`witness` (last seq + hash) somewhere else.

A record is ``confirmatory`` only when the protocol lock lists the SHA-256 of the script that
produced it; with no lock file everything is ``exploratory``. The status depends only on the script file's hash:
changes in library code the script calls are not covered. Stdlib only; the append is
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


def locked_hashes(lock_path):
    """Script hashes listed in a protocol lock (``sha256  path`` per line, ``#`` comments)."""
    if not lock_path or not os.path.isfile(lock_path):
        return set()
    out = set()
    with open(lock_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line.split()[0].lower())
    return out


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


def _resolve(path):
    return path or os.environ.get(ENV_PATH) or REGISTRY_PATH_DEFAULT


def _rel(script_path):
    try:
        rel = os.path.relpath(script_path)
    except ValueError:  # other drive on Windows
        rel = script_path
    return rel.replace(os.sep, "/")


def record(test_id, script_path, params, result, *, path=None, lock_path=None):
    """Append one test result to the register and return the record."""
    path = _resolve(path)
    try:
        digest = sha256_file(script_path)
    except OSError:
        digest = None
    lock = PROTOCOL_LOCK_DEFAULT if lock_path is None else lock_path
    status = "confirmatory" if digest and digest in locked_hashes(lock) else "exploratory"
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
                    raise RegistryError(f"{path}: last line is not a register record") from exc
                prev = _sha(last)
            rec = {
                "seq": seq,
                "utc": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "test_id": test_id,
                "script": _rel(script_path),
                "script_sha256": digest,
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


def verify_chain(path):
    """Seq numbers of broken records (hash, prev_hash or seq gap); [] = intact."""
    if not os.path.isfile(path):
        return []
    broken, prev, expected = set(), GENESIS, 0
    for raw, rec in _records(path):
        if rec is None:
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
        prev, expected = _sha(raw), seq + 1
    return sorted(broken)


def witness(path):
    """``{"seq", "hash"}`` of the last record; keep it outside the register directory."""
    last, seen = None, False
    if os.path.isfile(path):
        for _raw, rec in _records(path):
            last, seen = rec, True
    if not seen:
        return {"seq": None, "hash": GENESIS}
    if last is None or "seq" not in last or "hash" not in last:
        raise RegistryError(f"{path}: last line is not a register record")
    return {"seq": last["seq"], "hash": last["hash"]}
