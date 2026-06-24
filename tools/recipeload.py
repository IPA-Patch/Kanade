#!/usr/bin/env python3
"""Shared recipe-module loader for the patch/verify tools.

A recipe is a Python module on ``PYTHONPATH`` describing what to patch.
Every tool that takes ``--recipe`` resolves the name the same way through
``load_recipe`` so the behaviour can never drift between tools again.

Resolution order for a ``--recipe`` value:
  * a fully-qualified module path (contains a dot) is imported as-is, e.g.
    ``recipes.kioukifexporter``;
  * a bare name is tried as ``recipes.<name>`` first (the common case — a
    sub-module of the consumer's ``recipes/`` package), then as ``<name>``
    directly, which handles consumers that expose the whole recipe surface
    through a ``recipes`` package ``__init__.py`` (``--recipe recipes``).

Only a genuinely-absent candidate triggers the fallback: a
``ModuleNotFoundError`` raised from *inside* a recipe that does exist (a
typo'd import within it) is re-raised unchanged, so a real error is never
masked as "recipe not found".
"""

from __future__ import annotations

import importlib
from types import ModuleType


def load_recipe(name: str) -> ModuleType:
    candidates = [name] if "." in name else [f"recipes.{name}", name]
    last_missing: ModuleNotFoundError | None = None
    for candidate in candidates:
        try:
            return importlib.import_module(candidate)
        except ModuleNotFoundError as e:
            # Swallow only "this candidate does not exist", which means the
            # missing module is the candidate itself or one of its ancestor
            # packages (e.g. candidate "recipes.foo.bar" with "recipes.foo"
            # absent). If e.name is a descendant or unrelated module, the
            # candidate imported but something *inside* it is missing — re-raise
            # so the real cause is not hidden as "recipe not found".
            missing = e.name or ""
            if candidate == missing or candidate.startswith(f"{missing}."):
                last_missing = e
                continue
            raise
    raise SystemExit(f"error: failed to import recipe {name!r}: {last_missing}")
