#!/usr/bin/env python3
"""Diff il2cpp field offsets for named types between two ``dump.cs`` files.

Prologue checks catch a moved method; nothing catches a moved *field*. A
class that gains one member above the one a hook reads silently shifts
every offset below it, and the hook then reads or writes the wrong bytes.

The dump index carries only type and method rows, so this reads the field
declarations straight out of ``dump.cs``, using the index to find where
each type's body starts.

Usage:
    python3 -m tools.verify_offsets \\
        --old assets/1.0.2/dump.cs --old-index assets/1.0.2/dump.cs.index.json \\
        --new assets/1.1.0/dump.cs --new-index assets/1.1.0/dump.cs.index.json \\
        --type BeginnerSupportEvaluator --type ShogiMatchingPlayerStatus

Exit status:
  0  every requested type has identical field offsets
  1  at least one type drifted (or is missing from one side)
  2  bad inputs
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import sys

from tools.verify_sites import load_dump_index, types_by_name

# ``	private readonly int _analysisDepth; // 0x18``
_RE_FIELD = re.compile(r"^\s+(?:\S.*?\s)?(\S+);\s*//\s*(0x[0-9A-Fa-f]+)\s*$")

# How far past the type declaration to keep scanning for fields. Field
# declarations always precede the method block, so the first line that
# looks like a method or the closing brace ends the scan; this is only a
# backstop against a malformed dump.
_MAX_BODY_LINES = 400


def read_fields(dump_path: str, line: int) -> list[tuple[str, str]]:
    """Return ``[(field_name, offset_hex)]`` for the type declared at ``line``."""
    out: list[tuple[str, str]] = []
    with open(dump_path, "r", encoding="utf-8", errors="replace") as fh:
        body = itertools.islice(fh, line, line + _MAX_BODY_LINES)
        for raw in body:
            if raw.startswith("}"):
                break
            m = _RE_FIELD.match(raw.rstrip("\n"))
            if m:
                out.append((m.group(1), m.group(2)))
            elif "// RVA:" in raw:
                break
    return out


def locate(index: dict, type_name: str) -> int | None:
    hits = types_by_name(index).get(type_name, [])
    return hits[0]["line"] if hits else None


def diff_type(args: argparse.Namespace, type_name: str) -> bool:
    """Print a side-by-side field diff. Returns True when they match."""
    old_line = locate(args._old_index, type_name)
    new_line = locate(args._new_index, type_name)
    if old_line is None or new_line is None:
        missing = "old" if old_line is None else "new"
        print(f"  MISSING  {type_name}: not present in the {missing} dump")
        return False

    old = read_fields(args.old, old_line)
    new = read_fields(args.new, new_line)
    old_by_name = dict(old)
    new_by_name = dict(new)

    if old == new:
        print(f"  SAME     {type_name}: {len(old)} field(s) unchanged")
        return True

    print(f"  DRIFT    {type_name}:")
    for name, off in old:
        if name not in new_by_name:
            print(f"      - {off:>6}  {name}  (removed)")
        elif new_by_name[name] != off:
            print(f"      ~ {off:>6} -> {new_by_name[name]:>6}  {name}")
    for name, off in new:
        if name not in old_by_name:
            print(f"      + {off:>6}  {name}  (added)")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, help="Baseline dump.cs.")
    parser.add_argument("--old-index", required=True, help="Baseline dump.cs.index.json.")
    parser.add_argument("--new", required=True, help="Target dump.cs.")
    parser.add_argument("--new-index", required=True, help="Target dump.cs.index.json.")
    parser.add_argument(
        "--type",
        action="append",
        required=True,
        dest="types",
        help="Type name to compare (repeatable).",
    )
    args = parser.parse_args()

    for path in (args.old, args.old_index, args.new, args.new_index):
        if not os.path.isfile(path):
            print(f"error: not found: {path}", file=sys.stderr)
            return 2

    args._old_index = load_dump_index(args.old_index)
    args._new_index = load_dump_index(args.new_index)

    drifted = [t for t in args.types if not diff_type(args, t)]
    print()
    if drifted:
        print(f"{len(drifted)} / {len(args.types)} type(s) need attention")
        return 1
    print(f"all {len(args.types)} type(s) unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
