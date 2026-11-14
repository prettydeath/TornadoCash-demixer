#!/usr/bin/env python3
"""Verify the test register and count its tests; write a witness or a protocol lock.

    python tools/registry_report.py                       # verify + counts (default register)
    python tools/registry_report.py --witness ../witness.json
    python tools/registry_report.py --lock tools/holdout_report.py tools/decoy_rates.py \
        --out docs/PROTOCOL.lock

The register is .cache/registry/tests.jsonl (or $TORNADO_DEMIX_REGISTRY). Exit status 1 when
the chain is broken. The witness must be written outside the register directory, otherwise it
would not survive tampering with that directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tornado_demix import registry  # noqa: E402


def summarize(path, witness=None):
    """Chain check and counts per status, test family (test_id up to the first dot) and script."""
    status, family, script = Counter(), Counter(), Counter()
    total = 0
    if os.path.isfile(path):
        for _raw, rec in registry._records(path):
            if rec is None:
                continue
            total += 1
            status[rec.get("status")] += 1
            family[str(rec.get("test_id", "")).split(".", 1)[0]] += 1
            script[rec.get("script")] += 1
    return {
        "path": path,
        "total": total,
        "broken": registry.verify_chain(path, witness=witness),
        "partial_tail": registry.partial_tail(path),
        "unlisted": registry.unlisted(path),
        "by_status": dict(status),
        "by_family": dict(family),
        "by_script": dict(script),
    }


def write_lock(scripts, out):
    lines = ["# sha256  script (tools/registry_report.py --lock)"]
    for s in scripts:
        lines.append(f"{registry.sha256_file(s)}  {registry._rel(s)}")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives on Windows
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", nargs="?", default=None, help="register file")
    ap.add_argument("--witness", metavar="OUT", help="write {seq, hash} of the last record to OUT")
    ap.add_argument(
        "--check-witness", metavar="FILE", help="verify the register against a stored witness"
    )
    ap.add_argument("--lock", nargs="+", metavar="SCRIPT", help="write a protocol lock for these")
    ap.add_argument("--out", default=registry.PROTOCOL_LOCK_DEFAULT, help="lock file to write")
    args = ap.parse_args(argv)
    if args.lock:
        write_lock(args.lock, args.out)
        print(f"wrote {args.out} ({len(args.lock)} scripts)")
        return 0
    path = registry._resolve(args.path)
    if args.witness:
        if _inside(args.witness, os.path.dirname(os.path.abspath(path))):
            print("witness must be written outside the register directory", file=sys.stderr)
            return 2
        os.makedirs(os.path.dirname(os.path.abspath(args.witness)), exist_ok=True)
        with open(args.witness, "w", encoding="utf-8") as fh:
            json.dump(registry.witness(path), fh)
    stored = None
    if args.check_witness:
        with open(args.check_witness, encoding="utf-8") as fh:
            stored = json.load(fh)
    s = summarize(path, stored)
    print(f"register: {s['path']}\nrecords: {s['total']}")
    print("chain: " + ("intact" if not s["broken"] else f"BROKEN at seq {s['broken']}"))
    if s["unlisted"]:
        print(f"confirmatory records not listed in the current lock: seq {s['unlisted']}")
    if s["partial_tail"] is not None:
        print(f"last line (seq {s['partial_tail']}) is half-written: reported, appends continue")
    for title, key in (("status", "by_status"), ("family", "by_family"), ("script", "by_script")):
        print(f"by {title}:")
        for k, v in sorted(s[key].items(), key=lambda kv: str(kv[0])):
            print(f"  {k}: {v}")
    return 1 if s["broken"] else 0


if __name__ == "__main__":
    sys.exit(main())
