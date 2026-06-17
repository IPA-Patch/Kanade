# Kanade — Kernel-Agnostic Native ARM64 Dylib Embedder

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" />
  <img alt="platform" src="https://img.shields.io/badge/platform-iOS%2018--26-lightgrey?style=flat-square" />
  <img alt="arch" src="https://img.shields.io/badge/arch-arm64-555?style=flat-square" />
  <img alt="org" src="https://img.shields.io/badge/org-IPA--Patch-ff66a3?style=flat-square" />
</p>

---

Kanade is the host-side Python tooling for [IPA-Patch](https://github.com/IPA-Patch) tweaks — it rewrites the target Mach-O binary **on disk before signing**, embedding code caves and dylib load commands so that runtime hook delivery never touches `__TEXT`. This is the only viable approach on iOS 18–26 sideloaded targets, where the kernel Code Signing Monitor kills any process that writes to its own `__TEXT` segment at runtime.

The **runtime C/ObjC side** (slot resolution, image lookup, orig-trampoline recovery) lives in [IPA-Patch/Chinlan](https://github.com/IPA-Patch/Chinlan). Kanade and Chinlan are designed to be used together.

## Why static patching

On iOS 18–26, runtime inline-rewrite hooking (MSHookFunction, Dobby, Frida) is killed by CSM on non-jailbroken targets. The IPA-Patch approach:

1. Each hook site's first instruction is overwritten with `B <cave>`.
2. A small trampoline cave is appended to the binary's `__TEXT` zero-fill tail.
3. The cave reads a function pointer from a `__DATA,__bss` slot at runtime and calls the tweak dylib through it.
4. The tweak dylib, on load, writes its hook address into that slot — a `__DATA` write, which CSM allows.

Steps 1–3 happen at build time on the host. This repo provides the Python tooling that performs them.

## What's in this repo

- `encode.py` — pure-Python ARM64 instruction encoders (4-byte little-endian output, no external assembler needed at build time).
- `machoops.py` — generic Mach-O ops: `add_lc_load_dylib`, `reserve_hook_slot`, `assert_slot_in_bss`, `iter_thin_binaries`. Backed by `lief`.
- `caves.py` — idempotent patch engine: `apply_patches(target, patches, cave_patches, cave_region)`. Applies inline byte patches and routes one-instruction sites to code-cave payloads.
- `patch_macho.py` — CLI driver: `python3 -m tools.patch_macho --recipe NAME TARGET`. The recipe module owns every per-target constant.
- `patch_plist.py` — CLI: `python3 -m tools.patch_plist --recipe NAME PLIST` (or `--set KEY=VALUE` for one-off overrides).
- `verify_lc_load.py` — post-patch smoke test that confirms the expected `LC_LOAD_DYLIB` is present in the patched binary.
- `verify_sites.py` — cross-checks hook-site RVAs against a `dump.cs` index to catch RVA drift after a target app update.
- `build_patched_ipa.sh` — end-to-end IPA pipeline: decrypt → patch Mach-O → patch plist → inject dylib → repack → sign. Driven by `--recipe / --framework / --dylib / --input`.
- `scaffold_recipe.py` — scaffolds a new per-target recipe module.

`tools` is a [PEP 420](https://peps.python.org/pep-0420/) namespace package. Consumer projects can add `tools/recipes/<name>.py` alongside `shared/` and import everything through the same `tools.*` namespace.

## Usage

Add both submodules to your consumer project:

```sh
git submodule add https://github.com/IPA-Patch/KANADE.git shared
git submodule add https://github.com/IPA-Patch/Chinlan.git Sources/Chinlan
```

Wire the Python tooling via `PYTHONPATH`:

```sh
PYTHONPATH=shared:. python3 -m tools.patch_macho --recipe recipes.yourtweak TARGET
PYTHONPATH=shared:. python3 -m tools.verify_sites \
    --recipe recipes.yourtweak \
    --index  assets/dump.cs.index.json \
    --ipa    assets/YourApp.ipa
```

Or via `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["shared", "."]
```

Write your recipe at `tools/recipes/yourtweak.py`. It is imported as `tools.recipes.yourtweak` — the namespace package merges `shared/tools/` and `./tools/` automatically:

```python
from tools.encode import b_imm               # from shared/tools
from tools.recipes.yourtweak import PATCHES  # from ./tools/recipes/
```

Per-target constants that live in the recipe: cave addresses, site RVAs, expected prologue bytes, dylib name, `LC_LOAD_DYLIB` path, plist keys.

See the [Chinlan README](https://github.com/IPA-Patch/Chinlan) for the runtime wiring (slot table, `publish_*()` helpers, bootstrap constructor).

## Checking for RVA drift after a target update

When the target app updates, its RVAs will change. Run `verify_sites` against the new build before patching:

```sh
PYTHONPATH=shared:. python3 -m tools.verify_sites \
  --recipe recipes.yourtweak \
  --index  assets/dump.cs.index.json \
  --ipa    assets/YourApp-new.ipa
```

Any mismatch is reported with the expected vs. found prologue bytes. Update the RVAs in your `ChinlanSites.h` and recipe to match, then re-verify.

## Development

```sh
make test       # pytest
make lint       # ruff check
make format     # ruff format
```

The encoder golden values were generated with the LLVM assembler shipped in [theos](https://github.com/theos/theos)'s linux iphone toolchain. To regenerate one:

```sh
$THEOS/toolchain/linux/iphone/bin/llvm-mc \
    -triple=arm64-apple-ios -show-encoding <<< 'stp x29, x30, [sp, #-0x90]!'
```

Update the matching `bytes.fromhex(...)` literal in `tests/test_encode.py`.

## License

MIT — see [LICENSE](LICENSE).
