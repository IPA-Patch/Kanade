#!/usr/bin/env python3
"""Re-resolve a recipe's ``SITES`` table against a new target's dump index.

When the target app ships a new build every RVA moves. This tool takes the
recipe you already trust, looks each row's label up in the new
``dump.cs.index.json``, and rewrites the RVA and prologue columns from the
new binary — leaving row order, comments, and every other column alone.

Row order is load-bearing: cave payloads are allocated in declaration order
while the runtime dispatcher indexes by hook id, so the two only agree when
position equals id. Rows are therefore never reordered, dropped, or merged.

Rows whose label does not resolve are emitted verbatim with a ``TODO``
marker appended and reported on stderr; the exit status is non-zero so a
half-ported recipe can't be mistaken for a finished one.

Usage:
    python3 -m tools.port_recipe \\
        --base-recipe vendor/KIOU-Hook/recipes/v1_0_2.py \\
        --index       assets/1.1.0/dump.cs.index.json \\
        --ipa         assets/1.1.0/Kiou-1.1.0.ipa \\
        --out         vendor/KIOU-Hook/recipes/v1_1_0.py

Exit status:
  0  every row resolved
  1  one or more rows need manual attention
  2  bad inputs
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

from tools.verify_sites import (
    _read_ipa_macho_bytes,
    _read_macho_bytes,
    find_method,
    load_dump_index,
    split_label,
    types_by_name,
)

TODO_MARKER = "TODO(port_recipe): unresolved"


class PortError(Exception):
    """Raised when the base recipe can't be parsed."""


# ---------------------------------------------------------------------------
# Base-recipe parsing.
#
# The recipe is parsed, never imported: importing pulls in ``tools.encode``
# and the ``recipes`` package, which forces a working sibling checkout just
# to read a table of integers. ``tools.check_recipes`` in KIOU-Hook takes
# the same approach for the same reason.
# ---------------------------------------------------------------------------


def find_sites_node(module: ast.Module) -> ast.List:
    """Return the ``SITES = [...]`` list literal from a parsed recipe."""
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "SITES":
                if not isinstance(node.value, ast.List):
                    raise PortError("SITES: expected a list literal")
                return node.value
    raise PortError("SITES: not found at module level")


def row_label(row: ast.Tuple) -> str:
    """Return the label column (last element) of a SITES row."""
    if len(row.elts) < 5:
        raise PortError(f"SITES row on line {row.lineno}: expected 5 columns")
    last = row.elts[-1]
    if not isinstance(last, ast.Constant) or not isinstance(last.value, str):
        raise PortError(f"SITES row on line {row.lineno}: label is not a string")
    return last.value


def iter_rows(sites: ast.List) -> list[ast.Tuple]:
    rows = []
    for elt in sites.elts:
        if not isinstance(elt, ast.Tuple):
            raise PortError(f"SITES element on line {elt.lineno} is not a tuple")
        rows.append(elt)
    return rows


# ---------------------------------------------------------------------------
# Source rewriting.
#
# The rewrite is textual so that comments, blank lines, and the hand-tuned
# column alignment in the base recipe survive. Only the two leading columns
# of each row's source line are replaced.
# ---------------------------------------------------------------------------


def rewrite_row(source_line: str, old_rva: int, new_rva: int, prologue: str) -> str:
    """Replace the RVA and prologue columns in one row's source text.

    The RVA is matched on its hex spelling as it appears in the source
    (recipes write ``0x5C3C29C``), and the prologue is the first quoted
    string after it.
    """
    old_hex = f"0x{old_rva:X}"
    head, sep, tail = source_line.partition(old_hex)
    if not sep:
        raise PortError(f"could not locate {old_hex} in row source: {source_line!r}")
    quote = tail.find('"')
    end = tail.find('"', quote + 1)
    if quote < 0 or end < 0:
        raise PortError(f"could not locate prologue column in row: {source_line!r}")
    return f'{head}0x{new_rva:X}{tail[:quote]}"{prologue}"{tail[end + 1:]}'


def read_prologue(args: argparse.Namespace, offset: int) -> bytes:
    if args.macho:
        return _read_macho_bytes(args.macho, offset, 4)
    return _read_ipa_macho_bytes(args.ipa, args.framework, offset, 4)


def port(args: argparse.Namespace) -> int:
    with open(args.base_recipe, "r", encoding="utf-8") as fh:
        source = fh.read()
    lines = source.splitlines(keepends=True)

    module = ast.parse(source, filename=args.base_recipe)
    rows = iter_rows(find_sites_node(module))

    by_name = types_by_name(load_dump_index(args.index))

    unresolved: list[tuple[int, str]] = []
    for row in rows:
        label = row_label(row)
        old_rva = ast.literal_eval(row.elts[0])
        type_name, method_name, param_types = split_label(label)
        hit = find_method(by_name, type_name, method_name, param_types=param_types)
        if hit is None:
            unresolved.append((row.lineno, label))
            idx = row.lineno - 1
            lines[idx] = lines[idx].rstrip("\n") + f"  # {TODO_MARKER}\n"
            continue
        new_rva = int(hit[1]["rva"], 0)
        prologue = read_prologue(args, new_rva).hex()
        idx = row.lineno - 1
        lines[idx] = rewrite_row(lines[idx], old_rva, new_rva, prologue)

    out = "".join(lines)
    if args.out == "-":
        sys.stdout.write(out)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"wrote {args.out}", file=sys.stderr)

    print(
        f"{len(rows) - len(unresolved)} / {len(rows)} row(s) resolved",
        file=sys.stderr,
    )
    for lineno, label in unresolved:
        print(f"  UNRESOLVED  line {lineno}: {label}", file=sys.stderr)
    return 1 if unresolved else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-recipe",
        required=True,
        help="Path to the recipe whose SITES table is being re-resolved.",
    )
    parser.add_argument(
        "--index",
        required=True,
        help="dump.cs.index.json for the NEW target build.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--macho", help="Path to the new target's Mach-O.")
    src.add_argument("--ipa", help="Path to the new target's .ipa.")
    parser.add_argument(
        "--framework",
        default="UnityFramework",
        help="Basename of the Mach-O inside the .ipa (default: UnityFramework).",
    )
    parser.add_argument(
        "--out",
        default="-",
        help="Where to write the ported recipe ('-' for stdout, the default).",
    )
    args = parser.parse_args()

    for path in (args.base_recipe, args.index, args.macho, args.ipa):
        if path and not os.path.isfile(path):
            print(f"error: not found: {path}", file=sys.stderr)
            return 2

    try:
        return port(args)
    except PortError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
