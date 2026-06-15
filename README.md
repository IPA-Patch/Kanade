# IPA-Patch/Shared

Shared library for static IPA patching across IPA-Patch projects.

This repo holds the pieces that are not target-specific:

- `runtime/` — C / Objective-C headers and implementations linked into
  every payload dylib (sandbox-Documents logging sink, il2cpp resolver,
  hook-engine wrapper). _Pending migration from consumer projects._
- `tools/` — Python tooling for binary patching. The package is a PEP
  420 namespace package, so consumer projects can layer their own
  `tools/recipes/<name>.py` modules on top.
  - `encode.py` — arm64 instruction encoders (4-byte little-endian
    output, no external assembler needed at build time).
  - `machoops.py` — `add_lc_load_dylib`, `reserve_hook_slot`,
    `assert_slot_in_bss`, `iter_thin_binaries`. Generic Mach-O ops
    backed by `lief`.
  - `caves.py` — `apply_patches(target, patches, cave_patches,
    cave_region)`: idempotent engine that applies inline byte patches
    and routes one-instruction sites to code-cave payloads.
  - `patch_macho.py` — CLI driver: `python3 -m tools.patch_macho
    --recipe NAME TARGET`. The recipe module owns every per-target
    constant.
  - `patch_plist.py` — CLI: `python3 -m tools.patch_plist --recipe
    NAME PLIST` or `--set KEY=VALUE`.
  - `verify_lc_load.py` — CLI: post-patch smoke test that a target
    Mach-O loads the expected dylib.
  - `build_patched_ipa.sh` — end-to-end IPA pipeline driven by
    `--recipe / --framework / --dylib / --input`.
- `tests/` — pytest suite. The encoder tests cross-check every
  instruction against `llvm-mc -triple=arm64-apple-ios -show-encoding`.

## Using this repo

Consumer projects add it as a git submodule under `shared/`:

```sh
git submodule add https://github.com/IPA-Patch/Shared.git shared
```

- C/Objective-C sources are included via `-Ishared/runtime`.
- Python tooling is imported with `shared/` on `sys.path` (for example,
  via pyproject's `tool.pytest.ini_options.pythonpath` or by exporting
  `PYTHONPATH=shared`). Because `tools` is a PEP 420 namespace package,
  the consumer project can put its own recipes at
  `tools/recipes/<name>.py` and they merge with `shared/tools/`
  at import time:

  ```python
  from tools.encode import b_imm                    # from shared/tools
  from tools.recipes.kioukifexporter import PATCHES # from consumer repo
  ```

Per-target recipes (cave addresses, fork sites, prologue bytes, dylib
names) live in the consumer project, not here.

## Development

```sh
make test       # pytest
make lint       # ruff check
make format     # ruff format
```

The encoder golden values were generated with the LLVM assembler
shipped in [theos](https://github.com/theos/theos)'s linux iphone
toolchain. To regenerate one, run:

```sh
$THEOS/toolchain/linux/iphone/bin/llvm-mc \
    -triple=arm64-apple-ios -show-encoding <<< 'stp x29, x30, [sp, #-0x90]!'
```

and update the matching `bytes.fromhex(...)` literal in
`tests/test_encode.py`.
