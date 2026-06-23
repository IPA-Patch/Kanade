#!/usr/bin/env python3
"""Parse an Il2CppDumper ``dump.cs`` file and emit ``dump.cs.index.json``.

The index schema matches the one consumed by ``verify_sites.py``:

  {
    "dump":   "<absolute path to dump.cs>",
    "images": [{"idx": 0, "name": "mscorlib.dll", "type_start": 0}, ...],
    "types":  [
      {
        "name":      "RuntimeClassHandle",
        "namespace": "Mono",
        "kind":      "struct",          // class | struct | enum | interface
        "tdi":       18,                // TypeDefIndex
        "line":      634,               // 1-based line of the type declaration
        "methods":   [
          {
            "sig":  "internal void .ctor(RuntimeStructs.MonoClass* value)",
            "rva":  "0x543C404",
            "line": 645                 // 1-based line of the sig (not the RVA comment)
          }
        ]
      }
    ]
  }

Types without methods (pure enums, field-only structs, empty classes) are
included with an empty ``methods`` list so that line-number lookups still work.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_RE_IMAGE = re.compile(
    r"^//\s*Image\s+(\d+):\s+(\S+)\s+-\s+(\d+)"
)
# Matches the type declaration line, e.g.:
#   internal class Foo // TypeDefIndex: 7
#   public sealed class Foo<T> // TypeDefIndex: 42
#   internal enum Interop.Error // TypeDefIndex: 1
#   internal interface IFoo // TypeDefIndex: 99
#   internal struct SafeGPtrArrayHandle : IDisposable // TypeDefIndex: 38
#   private class Outer.Inner : Base, IFoo // TypeDefIndex: 43
_RE_TYPE = re.compile(
    r"^(?:.*?\s)?(class|struct|enum|interface)\s+(\S+?)(?:<[^>]*>)?(?:\s*:[^/]*)?\s*//\s*TypeDefIndex:\s*(\d+)",
    re.ASCII,
)
# Fallback for anonymous/compiler-generated types like:
#   internal sealed class <>f__AnonymousType0<<Label>j__TPar> // TypeDefIndex: N
_RE_TYPE_ANON = re.compile(
    r"^.*\s(class|struct|enum|interface)\s+(<\S*>)\s*//\s*TypeDefIndex:\s*(\d+)"
)
_RE_NAMESPACE = re.compile(r"^//\s*Namespace:\s*(.*)")
_RE_RVA = re.compile(r"^[\t ]*//\s*RVA:\s*(0x[0-9A-Fa-f]+)")
# A method signature follows the RVA comment on the *next non-blank line*.
# We capture the trimmed text up to (but not including) the trailing " { }".
_RE_SIG = re.compile(r"^[\t ]+(.+?)\s*\{")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse(path: str) -> dict:
    images: list[dict] = []
    types: list[dict] = []

    current_ns: str = ""
    current_type: dict | None = None
    pending_rva: str | None = None   # RVA seen on the line above

    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")

            # -- Image header ------------------------------------------------
            m = _RE_IMAGE.match(line)
            if m:
                images.append({
                    "idx":        int(m.group(1)),
                    "name":       m.group(2),
                    "type_start": int(m.group(3)),
                })
                pending_rva = None
                continue

            # -- Namespace comment -------------------------------------------
            m = _RE_NAMESPACE.match(line)
            if m:
                current_ns = m.group(1).strip()
                pending_rva = None
                continue

            # -- Type declaration --------------------------------------------
            m = _RE_TYPE.match(line) or _RE_TYPE_ANON.match(line)
            if m:
                kind      = m.group(1)
                type_name = m.group(2)
                tdi       = int(m.group(3))
                current_type = {
                    "name":      type_name,
                    "namespace": current_ns,
                    "kind":      kind,
                    "tdi":       tdi,
                    "line":      lineno,
                    "methods":   [],
                }
                types.append(current_type)
                pending_rva = None
                continue

            # -- RVA comment -------------------------------------------------
            m = _RE_RVA.match(line)
            if m:
                pending_rva = m.group(1)
                continue

            # -- Method signature (follows an RVA comment) -------------------
            if pending_rva is not None and current_type is not None:
                m = _RE_SIG.match(line)
                if m:
                    current_type["methods"].append({
                        "sig":  m.group(1),
                        "rva":  pending_rva,
                        "line": lineno,
                    })
                    pending_rva = None
                    continue

            # Anything else clears a stale pending RVA
            if line.strip() and not line.strip().startswith("//"):
                pending_rva = None

    return {
        "dump":   os.path.abspath(path),
        "images": images,
        "types":  types,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse Il2CppDumper dump.cs → dump.cs.index.json",
    )
    parser.add_argument("dump_cs", help="Path to dump.cs")
    parser.add_argument(
        "-o", "--output",
        help="Output path (default: <dump_cs_dir>/dump.cs.index.json)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.dump_cs):
        print(f"error: {args.dump_cs!r} not found", file=sys.stderr)
        return 1

    out = args.output or os.path.join(os.path.dirname(args.dump_cs), "dump.cs.index.json")

    print(f"parsing {args.dump_cs} …", file=sys.stderr)
    index = parse(args.dump_cs)

    print(
        f"  {len(index['images'])} images, {len(index['types'])} types",
        file=sys.stderr,
    )

    with open(out, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))

    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
