"""Tests for ``tools.port_recipe``.

The tool rewrites a recipe's SITES table against a new dump. Three
properties are load-bearing and easy to break:

  * row ORDER, comments, and every non-RVA column survive the rewrite
    (cave payloads are allocated in declaration order, so a reordered
    table silently mispoints every orig-call trampoline),
  * an unresolved label is never guessed — the row keeps its old values,
    gains a TODO marker, and the exit status is non-zero, and
  * the RVA and prologue columns actually get the new values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.port_recipe import PortError, find_sites_node, main, rewrite_row

BASE_RECIPE = '''\
"""Recipe for the old build."""

from recipes.common import CAVE_ENTRY

BUILD = 12

# fmt: off
SITES = [
    # --- a section comment that must survive ---
    (0x1000, "aaaaaaaa", "HOOK_A", CAVE_ENTRY, "Foo.Alpha"),
    (0x2000, "bbbbbbbb", "HOOK_B", CAVE_ENTRY, "Foo.Beta"),
    (0x3000, "cccccccc", "HOOK_C", CAVE_ENTRY, "Gone.Vanished"),
]
# fmt: on
'''


def _index(entries: dict[str, list[tuple[str, str]]]) -> dict:
    """Build a minimal dump index: {type: [(method_name, rva_hex)]}."""
    return {
        "images": [],
        "types": [
            {
                "name": name,
                "namespace": "",
                "kind": "class",
                "tdi": i,
                "line": 1,
                "methods": [
                    {"sig": f"public void {m}()", "rva": rva, "line": 2}
                    for m, rva in methods
                ],
            }
            for i, (name, methods) in enumerate(entries.items())
        ],
    }


@pytest.fixture
def workspace(tmp_path: Path) -> dict:
    recipe = tmp_path / "v_old.py"
    recipe.write_text(BASE_RECIPE)

    index = tmp_path / "dump.cs.index.json"
    index.write_text(
        json.dumps(_index({"Foo": [("Alpha", "0x9000"), ("Beta", "0xA000")]}))
    )

    # A stand-in Mach-O: the tool reads 4 bytes at each resolved RVA.
    macho = tmp_path / "UnityFramework"
    blob = bytearray(0xB000)
    blob[0x9000:0x9004] = b"\x11\x22\x33\x44"
    blob[0xA000:0xA004] = b"\x55\x66\x77\x88"
    macho.write_bytes(bytes(blob))

    return {"recipe": recipe, "index": index, "macho": macho, "out": tmp_path / "v_new.py"}


def _run(ws: dict, monkeypatch) -> int:
    monkeypatch.setattr(
        "sys.argv",
        [
            "port_recipe",
            "--base-recipe", str(ws["recipe"]),
            "--index", str(ws["index"]),
            "--macho", str(ws["macho"]),
            "--out", str(ws["out"]),
        ],
    )
    return main()


def test_resolved_rows_get_new_rva_and_prologue(workspace, monkeypatch):
    _run(workspace, monkeypatch)
    out = workspace["out"].read_text()
    assert '(0x9000, "11223344", "HOOK_A"' in out
    assert '(0xA000, "55667788", "HOOK_B"' in out


def test_unresolved_row_is_marked_and_left_alone(workspace, monkeypatch):
    rc = _run(workspace, monkeypatch)
    out = workspace["out"].read_text()
    assert rc == 1
    assert '(0x3000, "cccccccc", "HOOK_C"' in out
    assert "TODO(port_recipe): unresolved" in out


def test_row_order_and_comments_survive(workspace, monkeypatch):
    _run(workspace, monkeypatch)
    out = workspace["out"].read_text()
    assert "# --- a section comment that must survive ---" in out
    assert "# fmt: off" in out and "# fmt: on" in out
    assert out.index("HOOK_A") < out.index("HOOK_B") < out.index("HOOK_C")
    assert 'BUILD = 12' in out


def test_missing_sites_table_is_an_error(tmp_path):
    import ast

    module = ast.parse("PATCHES = []\n")
    with pytest.raises(PortError):
        find_sites_node(module)


def test_rewrite_row_replaces_only_the_first_two_columns():
    line = '    (0x5C3C29C, "fc6fbaa9", "HOOK_X", CAVE_ENTRY, "Foo.Bar"),\n'
    got = rewrite_row(line, 0x5C3C29C, 0x6B58E3C, "deadbeef")
    assert got == '    (0x6B58E3C, "deadbeef", "HOOK_X", CAVE_ENTRY, "Foo.Bar"),\n'


def test_rewrite_row_rejects_a_row_whose_rva_it_cannot_find():
    with pytest.raises(PortError):
        rewrite_row('    (0x1, "aa", "H", CAVE_ENTRY, "F.B"),\n', 0xDEAD, 0x1, "bb")
