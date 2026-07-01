#!/usr/bin/env bash
# Build a patched IPA suitable for the iOS 18 sideloaded / TrollStored
# install path (where iOS 18's Code Signing Monitor blocks any runtime
# inline hook into __TEXT).
#
# The script is a generic four-step pipeline driven by a recipe:
#
#   1. Extract the input IPA into a working directory.
#   2. Run `python3 -m tools.patch_macho --recipe <name>` against the
#      framework binary the recipe targets (binary patches + cave
#      payloads + LC_LOAD_DYLIB insertion).
#   3. Run `python3 -m tools.patch_plist --recipe <name>` against
#      Info.plist (so the recipe can flip UIFileSharingEnabled etc.).
#   4. Drop the recipe's dylib next to the patched framework so dyld
#      can resolve `@executable_path/Frameworks/<name>.dylib`, and
#      re-zip the bundle.
#
# Re-running with the same input IPA is idempotent — every step no-ops
# when its patch is already applied.
#
# Python interpreter selection: in order, $PYTHON_BIN, then $VIRTUAL_ENV,
# then `<consumer>/.venv/bin/python3`, then `python3` on PATH. The Python
# steps need `lief`, which is normally only available inside the
# consumer's venv, so we autodetect rather than force every operator to
# `source .venv/bin/activate` first.
#
# Usage:
#   tools/build_patched_ipa.sh \
#     --recipe <recipe-name> \
#     --framework <Mach-O basename, e.g. UnityFramework> \
#     --dylib <path/to/Payload.dylib> \
#     --input <clean.ipa> \
#     [--output <out.ipa>]
#
# The script never distributes the input IPA itself — the operator
# supplies one.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: build_patched_ipa.sh --recipe NAME --framework BASENAME --dylib PATH --input IPA [--output IPA]

  --recipe NAME      tools.recipes.<NAME> Python module to apply.
  --framework BASE   Filename of the Mach-O inside Payload/*.app/Frameworks
                     to patch (e.g. UnityFramework).
  --dylib PATH       Path to the payload dylib to drop next to the framework.
                     Its basename must match the recipe's DYLIB_PATH leaf.
  --input IPA        Path to the clean .ipa (decrypted; this script does
                     NOT distribute the IPA itself).
  --output IPA       Optional; defaults to packages/ipa/<basename-of-dylib>.ipa.
  --bundle-id-suffix SUFFIX
                     Optional; if non-empty, append ".<SUFFIX>" to the
                     Info.plist CFBundleIdentifier so the patched IPA
                     installs alongside the original app instead of
                     overwriting it (e.g. "chinlan" turns
                     com.acme.app into com.acme.app.chinlan).
EOF
    exit 64
}

RECIPE=""
FRAMEWORK=""
DYLIB_SRC=""
INPUT_IPA=""
OUTPUT_IPA=""
BUNDLE_ID_SUFFIX=""

while [ $# -gt 0 ]; do
    case "$1" in
        --recipe)            RECIPE="$2"; shift 2;;
        --framework)         FRAMEWORK="$2"; shift 2;;
        --dylib)             DYLIB_SRC="$2"; shift 2;;
        --input)             INPUT_IPA="$2"; shift 2;;
        --output)            OUTPUT_IPA="$2"; shift 2;;
        --bundle-id-suffix)  BUNDLE_ID_SUFFIX="$2"; shift 2;;
        -h|--help)           usage;;
        *)                   echo "error: unknown argument: $1" >&2; usage;;
    esac
done

if [ -z "$RECIPE" ] || [ -z "$FRAMEWORK" ] || [ -z "$DYLIB_SRC" ] || [ -z "$INPUT_IPA" ]; then
    echo "error: --recipe, --framework, --dylib, --input are all required" >&2
    usage
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# KANADE_DIR is the Kanade root (this script's grandparent).
# CONSUMER_DIR is wherever the consumer invoked this script from — its
# repo root by convention, and the directory we resolve recipes against.
SHARED_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
KANADE_DIR="$SHARED_DIR"
CONSUMER_DIR="$(pwd)"
# Python needs to see both: the consumer for `from recipes.<name>` and
# Kanade for `from tools.encode` / `from tools.machoops`.
export PYTHONPATH="${CONSUMER_DIR}:${SHARED_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

# Pick the Python interpreter. tools.patch_macho needs `lief`, which is
# normally installed into the consumer's local virtualenv (uv venv /
# python -m venv) rather than system site-packages. Prefer, in order:
#
#   1. $PYTHON_BIN  (explicit operator override, takes precedence)
#   2. $VIRTUAL_ENV (a venv is already activated in the calling shell)
#   3. <consumer>/.venv/bin/python3 (the convention this repo + every
#      consumer adopts via `uv venv` / pyproject.toml)
#   4. system `python3`
#
# This lets `make ipa` work straight from a freshly-attached devcontainer
# without the operator having to remember `source .venv/bin/activate`
# first, and stays out of the way of every other workflow (existing
# CI / a script-driven build sets VIRTUAL_ENV; an operator with a
# differently-named venv sets PYTHON_BIN).
if [ -n "${PYTHON_BIN:-}" ]; then
    PY="$PYTHON_BIN"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then
    PY="$VIRTUAL_ENV/bin/python3"
elif [ -x "$CONSUMER_DIR/.venv/bin/python3" ]; then
    PY="$CONSUMER_DIR/.venv/bin/python3"
    echo "==> using consumer venv: $PY"
else
    PY="python3"
fi

DYLIB_BASENAME="$(basename "$DYLIB_SRC")"
DYLIB_STEM="${DYLIB_BASENAME%.dylib}"
OUTPUT_IPA="${OUTPUT_IPA:-$CONSUMER_DIR/packages/ipa/${DYLIB_STEM}-patched.ipa}"
WORK_DIR="$CONSUMER_DIR/.theos/ipa_build"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [ ! -f "$INPUT_IPA" ]; then
    echo "error: input IPA not found: $INPUT_IPA" >&2
    exit 1
fi
if [ ! -f "$DYLIB_SRC" ]; then
    echo "error: payload dylib not found: $DYLIB_SRC" >&2
    exit 1
fi
if ! command -v unzip >/dev/null 2>&1; then
    echo "error: unzip not on PATH" >&2
    exit 1
fi
if ! command -v zip >/dev/null 2>&1; then
    echo "error: zip not on PATH" >&2
    exit 1
fi
# Validate the picked interpreter. The check splits on whether $PY is a
# path (contains a /) or a bare command name, because `command -v` has
# subtly different semantics in each case:
#   * absolute / relative path → many shells just check file existence,
#     not the +x bit. So we explicitly require -x for path-like values.
#   * bare command → `command -v` does PATH lookup and only succeeds if
#     an executable is found, which is exactly what we want.
case "$PY" in
    */*)
        if [ ! -x "$PY" ]; then
            echo "error: python interpreter not executable: $PY" >&2
            exit 1
        fi
        ;;
    *)
        if ! command -v "$PY" >/dev/null 2>&1; then
            echo "error: $PY not on PATH" >&2
            exit 1
        fi
        ;;
esac
# Sanity-check that the picked interpreter has `lief` (the only non-stdlib
# dep tools.patch_macho needs). If not, fail loudly with a hint, instead
# of letting `python3 -m tools.patch_macho` ImportError deep in the run.
if ! "$PY" -c "import lief" 2>/dev/null; then
    echo "error: $PY can't import 'lief'." >&2
    echo "       install it into a venv (e.g. 'uv sync' or" >&2
    echo "       'python3 -m pip install lief') and re-run, or set" >&2
    echo "       PYTHON_BIN=<path/to/python> to point at one that has it." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Clean workspace + extract
# ---------------------------------------------------------------------------
echo "==> staging: $WORK_DIR"
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

echo "==> extracting $INPUT_IPA"
unzip -q "$INPUT_IPA" -d "$WORK_DIR"

# Locate the .app — the standard layout is Payload/<name>.app/, but be
# defensive in case the IPA was zipped with extra wrappers.
APP_DIR="$(find "$WORK_DIR/Payload" -maxdepth 1 -mindepth 1 -name "*.app" -type d | head -n 1)"
if [ -z "$APP_DIR" ]; then
    echo "error: no .app bundle found inside Payload/" >&2
    exit 1
fi
APP_NAME="$(basename "$APP_DIR")"
echo "==> found bundle: $APP_NAME"

FRAMEWORK_BIN="$APP_DIR/Frameworks/${FRAMEWORK}.framework/${FRAMEWORK}"
INFO_PLIST="$APP_DIR/Info.plist"

if [ ! -f "$FRAMEWORK_BIN" ]; then
    echo "error: framework Mach-O missing at $FRAMEWORK_BIN" >&2
    exit 1
fi
if [ ! -f "$INFO_PLIST" ]; then
    echo "error: Info.plist missing at $INFO_PLIST" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. patch framework
# ---------------------------------------------------------------------------
echo "==> patching $FRAMEWORK (recipe: $RECIPE)"
"$PY" -m tools.patch_macho --recipe "$RECIPE" "$FRAMEWORK_BIN"

echo "==> verifying LC_LOAD_DYLIB (recipe: $RECIPE)"
"$PY" -m tools.verify_lc_load --recipe "$RECIPE" "$FRAMEWORK_BIN" >/dev/null

# ---------------------------------------------------------------------------
# 3. patch Info.plist
# ---------------------------------------------------------------------------
echo "==> patching Info.plist (recipe: $RECIPE)"
"$PY" -m tools.patch_plist --recipe "$RECIPE" "$INFO_PLIST"

# Optional CFBundleIdentifier rewrite so the patched IPA can live
# side-by-side with the original app instead of overwriting it. Skipped
# when --bundle-id-suffix is empty (the default), so the pipeline stays
# a no-op for the "overwrite the original" workflow.
if [ -n "$BUNDLE_ID_SUFFIX" ]; then
    ORIGINAL_BUNDLE_ID="$("$PY" -c "import plistlib,sys; print(plistlib.load(open(sys.argv[1],'rb')).get('CFBundleIdentifier',''))" "$INFO_PLIST")"
    if [ -z "$ORIGINAL_BUNDLE_ID" ]; then
        echo "error: Info.plist has no CFBundleIdentifier to append suffix to" >&2
        exit 1
    fi
    # Idempotent: if the suffix is already present, don't append it twice.
    case "$ORIGINAL_BUNDLE_ID" in
        *".$BUNDLE_ID_SUFFIX")
            echo "==> CFBundleIdentifier already carries suffix .$BUNDLE_ID_SUFFIX — skipping rewrite"
            ;;
        *)
            NEW_BUNDLE_ID="${ORIGINAL_BUNDLE_ID}.${BUNDLE_ID_SUFFIX}"
            echo "==> rewriting CFBundleIdentifier: $ORIGINAL_BUNDLE_ID -> $NEW_BUNDLE_ID"
            "$PY" -m tools.patch_plist --set "CFBundleIdentifier=${NEW_BUNDLE_ID}" "$INFO_PLIST"
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# 4. inject dylib
# ---------------------------------------------------------------------------
DYLIB_DST="$APP_DIR/Frameworks/${DYLIB_BASENAME}"
echo "==> installing dylib -> Frameworks/${DYLIB_BASENAME}"
cp "$DYLIB_SRC" "$DYLIB_DST"
chmod 0755 "$DYLIB_DST"

# ---------------------------------------------------------------------------
# 5. zip into IPA
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$OUTPUT_IPA")"
echo "==> repacking into $OUTPUT_IPA"
# zip from inside WORK_DIR so the archive root is Payload/, matching the
# canonical IPA layout. -X strips extra metadata that some installers
# choke on; -r recurses into the bundle.
(cd "$WORK_DIR" && zip -qrX "$OUTPUT_IPA" Payload)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "==> done"
ls -la "$OUTPUT_IPA"
echo
echo "Next steps:"
echo "  - TrollStore : AirDrop \"$OUTPUT_IPA\" to the device and open it."
echo "  - Sideloadly : drag-and-drop \"$OUTPUT_IPA\", sign with your Apple ID."
echo "  - AltStore   : the same, through AltServer."
