#!/usr/bin/env python3
"""Run Il2CppDumper on every IPA under assets/ that is missing dump.cs
or dump.cs.index.json, and produce both files in assets/<version>/.

Usage:
    python3 shared/tools/dump.py [--force]

Options:
    --force   Re-run even when dump.cs already exists (always rebuilds index).

Requirements:
    - dotnet 8.x on PATH
    - vendor/Il2CppDumper/Il2CppDumper.dll (net8-patched runtimeconfig)
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

# shared/tools/ is two levels below the consumer repo root,
# so walk up three levels: tools → shared → consumer root.
_HERE = os.path.dirname(os.path.abspath(__file__))         # shared/tools
_SHARED = os.path.dirname(_HERE)                            # shared
REPO_ROOT = os.path.dirname(_SHARED)                        # consumer repo root
ASSETS_DIR = os.path.join(REPO_ROOT, "assets")
BUILD_INDEX = os.path.join(_HERE, "build_dump_index.py")

# Il2CppDumper is cached under .cache/ (gitignored) and downloaded on first use.
_IL2CPP_VERSION = "v6.7.46"
_IL2CPP_URL = (
    f"https://github.com/Perfare/Il2CppDumper/releases/download/"
    f"{_IL2CPP_VERSION}/Il2CppDumper-net7-{_IL2CPP_VERSION}.zip"
)
_CACHE_DIR = os.path.join(REPO_ROOT, ".cache", "Il2CppDumper")
DUMPER_DLL = os.path.join(_CACHE_DIR, "Il2CppDumper.dll")

# runtimeconfig patched to net8 so dotnet-sdk-8.0 can run the net7 binary.
_RUNTIMECONFIG = """{
  "runtimeOptions": {
    "tfm": "net8.0",
    "framework": {
      "name": "Microsoft.NETCore.App",
      "version": "8.0.0"
    },
    "configProperties": {
      "System.Reflection.Metadata.MetadataUpdater.IsSupported": false,
      "System.Runtime.InteropServices.BuiltInComInterop.IsSupported": true
    }
  }
}
"""


def _ensure_dumper() -> None:
    """Download and cache Il2CppDumper if not already present."""
    if os.path.exists(DUMPER_DLL):
        return
    os.makedirs(_CACHE_DIR, exist_ok=True)
    zip_path = os.path.join(_CACHE_DIR, "Il2CppDumper.zip")
    print(f"  downloading Il2CppDumper {_IL2CPP_VERSION} …", file=sys.stderr)
    urllib.request.urlretrieve(_IL2CPP_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(_CACHE_DIR)
    os.unlink(zip_path)
    # Patch runtimeconfig so dotnet 8 accepts the net7 binary.
    rc = os.path.join(_CACHE_DIR, "Il2CppDumper.runtimeconfig.json")
    with open(rc, "w") as f:
        f.write(_RUNTIMECONFIG)
    print(f"  cached at {_CACHE_DIR}", file=sys.stderr)

FRAMEWORK_PATH = "Payload/KIOU.app/Frameworks/UnityFramework.framework/UnityFramework"
METADATA_PATH = "Payload/KIOU.app/Data/Managed/Metadata/global-metadata.dat"
PLIST_PATH = "Payload/KIOU.app/Info.plist"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def version_from_ipa(ipa_path: str) -> str:
    """Read CFBundleShortVersionString from the IPA's Info.plist."""
    with zipfile.ZipFile(ipa_path) as z:
        names = z.namelist()
        # find the Info.plist — depth-2 entry like Payload/APP.app/Info.plist
        plists = [n for n in names if n.endswith("Info.plist") and n.count("/") == 2]
        if not plists:
            return os.path.basename(os.path.dirname(ipa_path))
        with z.open(plists[0]) as f:
            pl = plistlib.load(f)
        return pl.get("CFBundleShortVersionString", "unknown")


def find_targets(force: bool) -> list[dict]:
    """Return list of {ipa, ver_dir, version, needs_dump, needs_index}."""
    targets = []
    for entry in sorted(os.listdir(ASSETS_DIR)):
        ver_dir = os.path.join(ASSETS_DIR, entry)
        if not os.path.isdir(ver_dir):
            continue
        ipas = [f for f in os.listdir(ver_dir) if f.endswith(".ipa")]
        if not ipas:
            continue
        ipa_path = os.path.join(ver_dir, ipas[0])
        dump_cs = os.path.join(ver_dir, "dump.cs")
        dump_idx = os.path.join(ver_dir, "dump.cs.index.json")
        needs_dump = force or not os.path.exists(dump_cs)
        needs_index = not os.path.exists(dump_idx)
        if not needs_dump and not needs_index:
            continue
        version = version_from_ipa(ipa_path)
        targets.append({
            "ipa": ipa_path,
            "ver_dir": ver_dir,
            "version": version,
            "needs_dump": needs_dump,
            "needs_index": needs_index,
        })
    return targets


def run_dumper(ipa_path: str, ver_dir: str) -> str:
    """Extract framework + metadata from IPA, run Il2CppDumper, copy dump.cs
    to ver_dir. Returns the path to the produced dump.cs."""
    with tempfile.TemporaryDirectory(prefix="il2cpp_") as tmp:
        print(f"  extracting {os.path.basename(ipa_path)} …")
        with zipfile.ZipFile(ipa_path) as z:
            z.extract(FRAMEWORK_PATH, tmp)
            z.extract(METADATA_PATH, tmp)

        fw = os.path.join(tmp, FRAMEWORK_PATH)
        meta = os.path.join(tmp, METADATA_PATH)

        # Il2CppDumper always writes to the directory that contains
        # Il2CppDumper.dll, regardless of cwd or the output argument.
        # We copy the DLL into a temp subdir so we can capture the output
        # without polluting the cache.
        dumper_dir = os.path.dirname(DUMPER_DLL)
        run_dir = os.path.join(tmp, "dumper")
        shutil.copytree(dumper_dir, run_dir)
        run_dll = os.path.join(run_dir, os.path.basename(DUMPER_DLL))

        print("  running Il2CppDumper …")
        result = subprocess.run(
            ["dotnet", run_dll, fw, meta],
            capture_output=True,
            text=True,
            input="\n",   # satisfy the "Press any key" prompt when stdin is a pipe
        )
        # Il2CppDumper exits non-zero from the Console.ReadKey crash — that's OK
        # as long as dump.cs was produced.
        produced = os.path.join(run_dir, "dump.cs")
        if not os.path.exists(produced):
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            die("Il2CppDumper did not produce dump.cs")

        dst = os.path.join(ver_dir, "dump.cs")
        shutil.copy2(produced, dst)
        print(f"  wrote {dst}")
        return dst


def build_index(dump_cs: str, ver_dir: str) -> None:
    """Run build_dump_index.py to produce dump.cs.index.json."""
    dst = os.path.join(ver_dir, "dump.cs.index.json")
    result = subprocess.run(
        [sys.executable, BUILD_INDEX, dump_cs, "-o", dst],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        die("build_dump_index.py failed")
    print(result.stderr.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-dump even when dump.cs already exists")
    args = parser.parse_args()

    if shutil.which("dotnet") is None:
        die("dotnet not on PATH — install dotnet-sdk-8.0")

    _ensure_dumper()

    targets = find_targets(args.force)
    if not targets:
        print("all versions already have dump.cs and dump.cs.index.json")
        return 0

    for t in targets:
        print(f"\n[{t['version']}]  {t['ver_dir']}")
        dump_cs = os.path.join(t["ver_dir"], "dump.cs")

        if t["needs_dump"]:
            dump_cs = run_dumper(t["ipa"], t["ver_dir"])
        else:
            print(f"  skip dump  ({dump_cs} exists)")

        build_index(dump_cs, t["ver_dir"])

    print("\ndone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
