# IPA-Patch/Shared

Shared library for static IPA patching across IPA-Patch projects.

This repo holds the pieces that are not target-specific:

- `runtime/` — C / Objective-C headers and implementations linked into
  every payload dylib (sandbox-Documents logging sink, il2cpp resolver,
  hook-engine wrapper).
- `tools/` — Python tooling for binary patching.
  - `encode.py` — arm64 instruction encoders (4-byte little-endian
    output, no external assembler needed at build time).
  - `machoops.py`, `driver.py`, `ipa.py`, `plist.py`, `verify.py`
    — pending migration from per-project copies.
- `tests/` — pytest suite. The encoder tests cross-check every
  instruction against `llvm-mc -triple=arm64-apple-ios -show-encoding`.

## Using this repo

Consumer projects add it as a git submodule under `shared/`:

```sh
git submodule add https://github.com/IPA-Patch/Shared.git shared
```

- C/Objective-C sources are included via `-Ishared/runtime`.
- Python tooling is imported with `PYTHONPATH=shared`:

  ```python
  from tools.encode import b_imm, adrp_ldr_x_pair
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
