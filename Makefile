# IPA-Patch/Shared — top-level development tasks.
#
# `uv` runs the Python tooling in an isolated environment so the dev
# dependencies declared in pyproject.toml are reproducible across
# machines and CI. If you prefer plain pytest / ruff installed via pip,
# the commands work the same — just drop the `uv run` prefix.

PYTHON ?= python3
UV     ?= uv

.PHONY: test lint format check

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check tools tests

format:
	$(UV) run ruff format tools tests

check: lint test
