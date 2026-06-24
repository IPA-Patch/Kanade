"""Tests for ``tools.recipeload.load_recipe``.

The loader resolves a ``--recipe`` value to a module. Its contract has
three branches that every patch/verify tool depends on, plus one safety
property that previously bit us:

  * a dotted name imports as-is,
  * a bare name resolves ``recipes.<name>`` first,
  * a bare name that is itself a package (``recipes``) falls back to the
    bare import, and
  * a ModuleNotFoundError raised *inside* an existing recipe is NOT
    swallowed as "recipe not found" — the real cause must surface.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from tools.recipeload import load_recipe


def _write(pkg_root: Path, relpath: str, body: str) -> None:
    target = pkg_root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body))


@pytest.fixture
def recipes_on_path(tmp_path, monkeypatch):
    """Build a throwaway ``recipes/`` package on a fresh sys.path entry."""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write(tmp_path, "recipes/__init__.py", 'DYLIB_PATH = "from-package"\n')
    _write(tmp_path, "recipes/kioutest.py", 'DYLIB_PATH = "from-submodule"\n')
    # A recipe that exists but imports a module that does not.
    _write(
        tmp_path,
        "recipes/broken.py",
        "import a_module_that_does_not_exist  # noqa: F401\n",
    )
    # Drop any cached imports from a previous test run.
    for mod in [m for m in sys.modules if m == "recipes" or m.startswith("recipes.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return tmp_path


def test_dotted_name_imports_as_is(recipes_on_path):
    mod = load_recipe("recipes.kioutest")
    assert mod.DYLIB_PATH == "from-submodule"


def test_bare_name_prefers_recipes_submodule(recipes_on_path):
    mod = load_recipe("kioutest")
    assert mod.DYLIB_PATH == "from-submodule"


def test_bare_package_name_falls_back_to_direct_import(recipes_on_path):
    # "recipes" -> "recipes.recipes" (absent) -> "recipes" (the package)
    mod = load_recipe("recipes")
    assert mod.DYLIB_PATH == "from-package"


def test_inner_import_error_is_not_masked(recipes_on_path):
    # The recipe exists; the missing module is inside it. We must see the
    # real ModuleNotFoundError, not a misleading "recipe not found".
    with pytest.raises(ModuleNotFoundError) as exc:
        load_recipe("recipes.broken")
    assert exc.value.name == "a_module_that_does_not_exist"


def test_missing_recipe_raises_systemexit(recipes_on_path):
    with pytest.raises(SystemExit) as exc:
        load_recipe("does_not_exist_anywhere")
    assert "does_not_exist_anywhere" in str(exc.value)
