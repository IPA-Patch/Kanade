#!/usr/bin/env python3
"""Cross-check recipe ``_SITES`` against ``dump.cs.index.json``.

For every recipe row we look up the named method in the structured
dump-index and confirm:

  1. The label resolves to exactly one method whose RVA matches the
     recipe's ``site_off``.
  2. The clean Mach-O's 4 bytes at that offset match the recipe's
     ``prologue_hex`` column (catches stale prologues after a KIOU
     update).

Use as a smoke test after editing ``_SITES`` or after a target
binary refresh, and as a CI gate so a recipe out of sync with the
dump fails before patching.

The dump-index is a JSON document produced from a Il2CppDumper
``dump.cs`` run: each type carries ``methods[]`` rows with the
method signature, the source line, and the RVA. See
``assets/dump.cs.index.json`` for the canonical schema.

Exit status:
  0  every site matches its dump entry
  1  one or more mismatches (label not unique, RVA drift, prologue drift)
  2  bad inputs (missing files, recipe failed to import, …)
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import zipfile
from typing import Iterable

# ---------------------------------------------------------------------------
# Site label parsing.
#
# Recipe rows label hooks like "TitleScene+<OnActivateAsync>d__10.MoveNext"
# or "ResolvedBeginnerSupport.get_Enabled". The il2cpp dumper writes the
# state-machine struct as a NESTED type so its display name in the index
# is "TitleScene.<OnActivateAsync>d__10". The recipe uses '+' to mark
# the nested boundary, which is the C# decoration convention; we treat
# '+' and '.' equivalently when matching against the dump.
#
# Splitting the LAST '.' off the label gives us (type_name, method_name)
# in every case we care about.
# ---------------------------------------------------------------------------


def split_label(label: str) -> tuple[str | None, str]:
    """Return ``(type_name, method_name)`` from a recipe label.

    ``+`` in the type segment is normalised to ``.`` so it lines up with
    the dump-index's nested-type spelling.

    Labels without a ``.`` (e.g. plain ``SelectCharacterAsync``) return
    ``type_name=None``; the caller then scans every type for a method
    matching ``method_name`` — slow but unambiguous, and the only sane
    fallback when the recipe author didn't bother to qualify.

    Constructor entries are spelled either ``Foo.ctor`` (recipe) or
    ``.ctor`` (dump signature). We normalise the recipe form to
    ``.ctor`` so the dump match works without special-casing.
    """
    if "." not in label:
        return None, label
    type_name, method_name = label.rsplit(".", 1)
    type_name = type_name.replace("+", ".")
    if method_name == "ctor":
        method_name = ".ctor"
    return type_name, method_name


# ---------------------------------------------------------------------------
# Dump lookup.
# ---------------------------------------------------------------------------


def load_dump_index(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def types_by_name(index: dict) -> dict[str, list[dict]]:
    """Bucket types by their bare ``name`` so we don't have to re-scan
    the 20k+ row list per lookup."""
    out: dict[str, list[dict]] = {}
    for t in index.get("types", []):
        out.setdefault(t["name"], []).append(t)
    return out


def _sig_matches(sig: str, method_name: str) -> bool:
    """True if ``sig`` declares ``method_name``.

    The dump-index ``sig`` column carries the full C# decl, e.g.

      ``private void pb::Google.Protobuf.IBufferMessage.InternalMergeFrom(ref ParseContext input)``
      ``public void .ctor(IHomeUtilityView view)``
      ``private void UnityEngine.EventSystems.IPointerClickHandler.OnPointerClick(PointerEventData)``
      ``private void MoveNext()``

    The method name is always immediately followed by ``(`` and
    preceded by either a space (plain method) or a ``.`` (explicit
    interface implementation, ``.ctor``, ``.cctor``). Matching on
    ``" name("`` OR ``".name("`` covers every shape we have without
    accepting a substring that happens to embed the method name in a
    longer identifier.
    """
    return f" {method_name}(" in sig or f".{method_name}(" in sig


def find_method(
    by_name: dict[str, list[dict]], type_name: str | None, method_name: str
) -> tuple[dict, dict] | None:
    """Return ``(type_record, method_record)`` for the first method
    whose ``sig`` declares ``method_name`` on the named type.

    When ``type_name`` is ``None`` (caller couldn't parse it out of the
    recipe label) we scan every type and return the first hit. Slow
    but unambiguous for recipe rows that omit the qualifier.
    """
    if type_name is None:
        for t in by_name.values():
            for tt in t:
                for m in tt.get("methods", []):
                    if _sig_matches(m.get("sig", ""), method_name):
                        return tt, m
        return None
    candidates = by_name.get(type_name, [])
    for t in candidates:
        for m in t.get("methods", []):
            if _sig_matches(m.get("sig", ""), method_name):
                return t, m
    return None


# ---------------------------------------------------------------------------
# Prologue check.
#
# The recipe rows carry the expected 4-byte prologue as a lowercase hex
# string. To check it, we either read from a Mach-O on disk OR pull the
# UnityFramework slice out of an .ipa. Both shapes occur in practice:
# the clean .ipa lives under assets/, and a patched Mach-O sits under
# .theos/ipa_build/ during a build.
# ---------------------------------------------------------------------------


def _read_macho_bytes(path: str, offset: int, n: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(n)


def _read_ipa_macho_bytes(
    ipa_path: str, framework_basename: str, offset: int, n: int
) -> bytes:
    """Pull the requested bytes from ``<framework_basename>`` inside the
    given .ipa, without unpacking the whole archive. Only the first
    matching entry is read; .ipa files only ever ship one slice per
    framework, so that's the right behaviour.
    """
    with zipfile.ZipFile(ipa_path) as zf:
        for name in zf.namelist():
            tail = name.rsplit("/", 1)[-1]
            if tail == framework_basename:
                with zf.open(name) as fh:
                    fh.read(offset)  # ZipExtFile has no random seek
                    return fh.read(n)
    raise SystemExit(
        f"error: {framework_basename!r} not found in {ipa_path}"
    )


def read_prologue(args: argparse.Namespace, offset: int) -> bytes | None:
    """Return the 4-byte prologue at ``offset`` from whichever target
    the caller pointed at. Returns ``None`` if no target was supplied
    (the prologue check is then skipped — RVA-only mode).
    """
    if args.macho:
        return _read_macho_bytes(args.macho, offset, 4)
    if args.ipa:
        return _read_ipa_macho_bytes(args.ipa, args.framework, offset, 4)
    return None


# ---------------------------------------------------------------------------
# Main verify loop.
# ---------------------------------------------------------------------------


def _load_recipe(name: str):
    if "." not in name:
        name = f"recipes.{name}"
    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise SystemExit(f"error: failed to import recipe {name!r}: {e}") from e


def verify(args: argparse.Namespace) -> int:
    recipe = _load_recipe(args.recipe)
    sites: Iterable[tuple[int, int, str, str]] = getattr(recipe, "_SITES", [])
    if not sites:
        print(f"error: recipe {args.recipe!r} has no _SITES table", file=sys.stderr)
        return 2

    index = load_dump_index(args.index)
    by_name = types_by_name(index)

    fail = 0
    total = 0
    for slot_index, site_off, prologue_hex, label in sites:
        total += 1
        try:
            type_name, method_name = split_label(label)
        except ValueError as e:
            print(f"  FAIL  slot[{slot_index:>2}] {label!r}: {e}")
            fail += 1
            continue

        hit = find_method(by_name, type_name, method_name)
        if hit is None:
            print(
                f"  FAIL  slot[{slot_index:>2}] {label!r}: "
                f"not found in dump (type={type_name!r}, method={method_name!r})"
            )
            fail += 1
            continue
        _t, m = hit
        dump_rva = int(m["rva"], 0)
        if dump_rva != site_off:
            print(
                f"  FAIL  slot[{slot_index:>2}] {label!r}: "
                f"recipe RVA 0x{site_off:X} != dump RVA 0x{dump_rva:X} "
                f"(sig: {m['sig']!r}, dump.cs line {m['line']})"
            )
            fail += 1
            continue

        # RVA matches the dump. Confirm the prologue too if the operator
        # gave us a binary to read from; this catches a stale recipe
        # after the target binary has been replaced underneath us.
        actual = read_prologue(args, site_off)
        if actual is None:
            print(
                f"  OK    slot[{slot_index:>2}] {label}: RVA 0x{site_off:X} "
                f"(dump.cs line {m['line']})"
            )
            continue

        actual_hex = actual.hex()
        if actual_hex != prologue_hex.lower():
            print(
                f"  FAIL  slot[{slot_index:>2}] {label!r}: "
                f"recipe prologue {prologue_hex} != actual {actual_hex} "
                f"@ 0x{site_off:X}"
            )
            fail += 1
            continue

        print(
            f"  OK    slot[{slot_index:>2}] {label}: RVA 0x{site_off:X} "
            f"prologue {actual_hex}"
        )

    print()
    if fail:
        print(f"{fail} / {total} site(s) failed verification")
        return 1
    print(f"all {total} site(s) verified")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cross-check recipe _SITES against dump.cs.index.json",
    )
    parser.add_argument(
        "--recipe",
        required=True,
        help="Recipe module to verify (e.g. recipes.kioueditor).",
    )
    parser.add_argument(
        "--index",
        required=True,
        help="Path to dump.cs.index.json produced from Il2CppDumper output.",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--macho",
        help="Path to a Mach-O file to verify prologue bytes against.",
    )
    src.add_argument(
        "--ipa",
        help="Path to an .ipa whose UnityFramework slice will be opened to "
        "verify prologue bytes.",
    )
    parser.add_argument(
        "--framework",
        default="UnityFramework",
        help="Basename of the Mach-O inside the .ipa (default: UnityFramework).",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.index):
        print(f"error: dump index not found: {args.index}", file=sys.stderr)
        return 2
    if args.macho and not os.path.isfile(args.macho):
        print(f"error: macho not found: {args.macho}", file=sys.stderr)
        return 2
    if args.ipa and not os.path.isfile(args.ipa):
        print(f"error: ipa not found: {args.ipa}", file=sys.stderr)
        return 2

    return verify(args)


if __name__ == "__main__":
    sys.exit(main())
