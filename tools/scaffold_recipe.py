#!/usr/bin/env python3
"""Scaffold a new patch recipe under ``recipes/<name>.py``.

Writes a template recipe that exposes every name ``tools.patch_macho``
expects (``TARGET_BASENAME``, ``DYLIB_PATH``, ``HOOK_SLOT_RVA``,
``CAVE_REGION``, ``PATCHES``, ``CAVE_PATCHES``, ``PLIST_KEYS``), with
``TODO`` markers for the operator to fill in from the live binary they
are targeting.

Usage:
  python3 -m tools.scaffold_recipe <name>
  python3 -m tools.scaffold_recipe <name> --out path/to/recipe.py
  python3 -m tools.scaffold_recipe <name> --force   # overwrite existing

Defaults to writing ``<cwd>/recipes/<name>.py`` and creating ``recipes/``
if missing.
"""

from __future__ import annotations

import argparse
import os
import sys

TEMPLATE = '''"""Recipe for {{label}}.

Fill in every TODO with values discovered against the target binary
(disassembly, lief inspection, etc.). See IPA-Patch/Shared README for
the contract this module satisfies.
"""

from __future__ import annotations

from tools.encode import (
    add_x_imm,
    adrp,
    b_imm,
    blr_x,
    ldp_off_x,
    ldp_post_x,
    ldr_x_imm,
    stp_off_x,
    stp_pre_x,
)


# ---------------------------------------------------------------------------
# Target identification
# ---------------------------------------------------------------------------

# Filename of the Mach-O to patch (basename, not full path). Used by
# patch_macho.py as a sanity check before any write.
TARGET_BASENAME = "TODO"  # e.g. "UnityFramework"

# Dylib path inserted via LC_LOAD_DYLIB. Convention is
# @executable_path/Frameworks/<YourPayload>.dylib for non-jailbroken
# sideloads; @loader_path/... when loaded transitively.
DYLIB_PATH = "@executable_path/Frameworks/TODO.dylib"


# ---------------------------------------------------------------------------
# Code-cave region.
#
# A (start, end_exclusive) file-offset pair for an r-x zero-fill range
# inside __TEXT. Cave payloads are allocated sequentially from `start`.
# Inspect the target binary to find a safe range (often the tail of
# __TEXT,__oslogstring or __TEXT,__cstring).
# ---------------------------------------------------------------------------

CAVE_REGION = (0x0, 0x0)  # TODO: (cave_start, cave_end_exclusive)


# ---------------------------------------------------------------------------
# Hook slot.
#
# An 8-byte aligned VA inside __DATA,__bss (or __DATA,__common). The
# payload dylib's constructor writes its hook function pointer here on
# load; caves materialize this address and call through it.
#
# Run `python3 -m tools.patch_macho --recipe {{name}} --verify-only TARGET`
# once to print the reserve_hook_slot() suggestion, then pin that here.
# ---------------------------------------------------------------------------

HOOK_SLOT_RVA = 0x0  # TODO


# ---------------------------------------------------------------------------
# Cave payload builder.
#
# Returns a build_payload(cave_va) -> bytes closure that the driver
# calls once per cave entry. Adjust the saved-register set and the
# argument-passing convention to match what the dylib's hook function
# expects.
# ---------------------------------------------------------------------------


def _build_cave_payload(orig_va: int, slot_va: int, displaced_insn: bytes):
    if len(displaced_insn) != 4:
        raise ValueError(
            f"displaced_insn must be exactly 4 bytes; got {len(displaced_insn)}"
        )

    def build(cave_va: int) -> bytes:
        out = bytearray()
        cur = cave_va

        def emit(insn: bytes) -> None:
            nonlocal cur
            out.extend(insn)
            cur += 4

        # --- prologue: save LR + arg registers (X0..X7) ---
        emit(stp_pre_x(29, 30, 31, -0x90))
        emit(stp_off_x(0, 1, 31, 0x10))
        emit(stp_off_x(2, 3, 31, 0x20))
        emit(stp_off_x(4, 5, 31, 0x30))
        emit(stp_off_x(6, 7, 31, 0x40))
        emit(add_x_imm(29, 31, 0))  # MOV X29, SP

        # --- materialize SLOT address; load the published hook pointer ---
        emit(adrp(16, cur, slot_va))
        emit(ldr_x_imm(16, 16, slot_va & 0xFFF))
        emit(blr_x(16))

        # --- restore ---
        emit(ldp_off_x(6, 7, 31, 0x40))
        emit(ldp_off_x(4, 5, 31, 0x30))
        emit(ldp_off_x(2, 3, 31, 0x20))
        emit(ldp_off_x(0, 1, 31, 0x10))
        emit(ldp_post_x(29, 30, 31, 0x90))

        # --- execute the displaced prologue insn verbatim ---
        emit(displaced_insn)

        # --- branch to (orig + 4) ---
        emit(b_imm(cur, orig_va + 4))

        return bytes(out)

    return build


# ---------------------------------------------------------------------------
# PATCHES — inline byte replacements at fixed file offsets.
#
# Each entry: (file_offset, expected_orig_bytes, replacement_bytes, label)
# Usually empty unless you need to overwrite a constant or a leaf
# function with a fixed return value.
# ---------------------------------------------------------------------------

PATCHES: list = []


# ---------------------------------------------------------------------------
# CAVE_PATCHES — redirect 4-byte sites to cave payloads.
#
# Each entry: (site_off, expected_orig_insn, build_payload, label)
# The original 4 bytes at site_off are overwritten with `B <cave_va>`,
# and the cave runs the saved insn before branching back to (site+4).
# ---------------------------------------------------------------------------

# TODO: list the (file_offset, prologue_hex) pairs you want to redirect.
_SITES: list[tuple[int, str, str]] = [
    # (0xDEADBEEF, "fd7bbda9", "Class.method: route to your cave"),
]


CAVE_PATCHES: list = [
    (
        site,
        bytes.fromhex(prologue_hex),
        _build_cave_payload(site, HOOK_SLOT_RVA, bytes.fromhex(prologue_hex)),
        label,
    )
    for site, prologue_hex, label in _SITES
]


# ---------------------------------------------------------------------------
# Info.plist additions (consumed by `python3 -m tools.patch_plist
# --recipe {{name}} <plist>`).
# ---------------------------------------------------------------------------

PLIST_KEYS: dict = {
    # "UIFileSharingEnabled": True,
    # "LSSupportsOpeningDocumentsInPlace": True,
}
'''


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a new IPA-Patch recipe scaffold to disk.",
    )
    parser.add_argument(
        "name",
        help="Recipe slug (becomes the module name; e.g. 'kioukifexporter').",
    )
    parser.add_argument(
        "--out",
        help="Output path (default: ./recipes/<name>.py).",
    )
    parser.add_argument(
        "--label",
        help="Human-readable label inserted into the docstring "
        "(default: the slug verbatim).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the destination if it already exists.",
    )
    args = parser.parse_args()

    if not args.name.isidentifier():
        print(
            f"error: recipe name {args.name!r} must be a valid Python identifier",
            file=sys.stderr,
        )
        return 2

    out_path = args.out or os.path.join("recipes", f"{args.name}.py")
    if os.path.exists(out_path) and not args.force:
        print(
            f"error: {out_path} already exists; pass --force to overwrite",
            file=sys.stderr,
        )
        return 1

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    label = args.label or args.name
    # Use string replacement rather than str.format/Template so the
    # template itself can contain literal `{...}` (Python f-strings,
    # function calls) without escaping every brace.
    content = TEMPLATE.replace("{{name}}", args.name).replace("{{label}}", label)
    with open(out_path, "w") as f:
        f.write(content)

    print(f"wrote {out_path}")
    print("next steps:")
    print(f"  1. fill in TARGET_BASENAME, DYLIB_PATH, CAVE_REGION, HOOK_SLOT_RVA in {out_path}")
    print("  2. populate _SITES with the (offset, prologue_hex, label) tuples")
    print(f"  3. run: python3 -m tools.patch_macho --recipe {args.name} --verify-only TARGET")
    return 0


if __name__ == "__main__":
    sys.exit(main())
