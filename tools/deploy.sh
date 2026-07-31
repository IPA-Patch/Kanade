#!/usr/bin/env bash
# Ship a patched IPA to an iOS device and install it through TrollStore.
#
# Two-step pipeline:
#
#   1. scp the IPA into the device's /tmp/.
#   2. Invoke trollstorehelper install force <ipa> over SSH.
#   3. Relaunch the app so the just-installed build takes over the
#      running instance (open → uiopen fallback → manual instruction).
#
# TrollStore vs TrollStore Lite: the trollstorehelper binary lives in
# different places per install flavour. Rootless JB devices with
# TrollStore keep it inside /var/jb/Applications/TrollStore*.app/, and
# TrollStore Lite specifically uses /var/jb/Applications/TrollStoreLite.app/.
# If neither is present, the binary can also live inside TrollStore.app's
# own container bundle under /var/containers/Bundle/Application/<UUID>/.
# When --helper is not supplied, the script SSH-finds it, preferring
# the /var/jb/Applications/TrollStore*.app/ entries so stale
# TrollStorePersistenceHelper.app leftovers from a prior JB session
# don't win.
#
# Remote staging path: /var/mobile/Documents/. trollstorehelper runs in
# a sandbox that CANNOT read /tmp/ — an IPA staged there is rejected
# with return code 166 ("IPA does not exist or is not accessible"),
# even though the file is physically present. Documents/ is one of
# the few locations both root (scp target) and mobile (trollstorehelper
# runtime) can access; the script chowns after scp so mobile can read.
#
# trollstorehelper commonly kicks SpringBoard mid-install, which yanks
# the SSH session and returns exit 255 even though the install itself
# succeeded. This script tolerates that specific exit — the caller
# should confirm the app is on the home screen. Any other non-zero
# exit is propagated so a genuine failure fails the target.
#
# Usage:
#   shared/tools/deploy.sh \
#     --ipa <path/to/patched.ipa> \
#     --host <device-host> \
#     --port <ssh-port> \
#     --bundle-id <com.example.app> \
#     [--user <ssh-user>] \
#     [--helper <trollstorehelper path on device>] \
#     [--process-name <friendly name shown on relaunch>]

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: deploy.sh --ipa IPA --host HOST --port PORT --bundle-id BID
                 [--user USER] [--helper PATH] [--process-name NAME]

  --ipa IPA          Path to the patched .ipa on the local filesystem.
  --host HOST        Device host/IP reachable over SSH (e.g. host.docker.internal).
  --port PORT        SSH port (matches THEOS_DEVICE_PORT).
  --bundle-id BID    CFBundleIdentifier to relaunch after install
                     (e.g. com.neconome.shogi).
  --user USER        SSH username (defaults to root).
  --helper PATH      trollstorehelper path on the device. If omitted, the
                     script SSH-finds it under /var/jb/Applications and
                     /var/containers/Bundle/Application (TrollStore Lite).
  --process-name NM  Human-readable app name used in the manual-launch
                     fallback message. Defaults to the bundle id.
EOF
    exit 64
}

IPA=""
HOST=""
PORT=""
BUNDLE_ID=""
SSH_USER="root"
HELPER=""
PROCESS_NAME=""

while [ $# -gt 0 ]; do
    case "$1" in
        --ipa)          IPA="$2"; shift 2;;
        --host)         HOST="$2"; shift 2;;
        --port)         PORT="$2"; shift 2;;
        --bundle-id)    BUNDLE_ID="$2"; shift 2;;
        --user)         SSH_USER="$2"; shift 2;;
        --helper)       HELPER="$2"; shift 2;;
        --process-name) PROCESS_NAME="$2"; shift 2;;
        -h|--help)      usage;;
        *)              echo "error: unknown argument: $1" >&2; usage;;
    esac
done

if [ -z "$IPA" ] || [ -z "$HOST" ] || [ -z "$PORT" ] || [ -z "$BUNDLE_ID" ]; then
    echo "error: --ipa, --host, --port, --bundle-id are all required" >&2
    usage
fi

if [ ! -f "$IPA" ]; then
    echo "error: IPA not found: $IPA" >&2
    exit 1
fi

if [ -z "$PROCESS_NAME" ]; then
    PROCESS_NAME="$BUNDLE_ID"
fi

IPA_NAME="$(basename "$IPA")"
# /var/mobile/Documents/ is the sandbox-visible staging area for
# trollstorehelper — /tmp/ is off-limits and yields error 166.
REMOTE_IPA="/var/mobile/Documents/$IPA_NAME"
# The target process name for `killall` before install. `open BID` after
# install would otherwise front-restore the old in-memory instance and
# the just-installed binary would never dyld_load — the operator sees
# "the tweak didn't take effect" symptoms. Killing first forces iOS to
# spawn a fresh process against the new .app on relaunch.
KILL_TARGET="$PROCESS_NAME"

# ---------------------------------------------------------------------------
# Resolve trollstorehelper path (Lite / regular TrollStore).
#
# Discovery preference order:
#   1. /var/jb/Applications/TrollStore*.app/trollstorehelper — the live
#      helper on JB-rootless. Prefer TrollStoreLite.app over generic
#      TrollStore.app when both exist. Filter out
#      TrollStorePersistenceHelper.app: on Lite it never gets installed,
#      and a leftover directory from a previous non-Lite JB session
#      often has a helper binary whose entitlements have been
#      invalidated (invoking it SIGKILLs with exit 137 / ssh 255).
#   2. /var/containers/Bundle/Application/<UUID>/TrollStore*.app/... —
#      fallback for setups without a rootless JB where the .app lives
#      inside the App container tree.
# ---------------------------------------------------------------------------
if [ -z "$HELPER" ]; then
    echo "==> discovering trollstorehelper on $HOST"
    # Emit every candidate helper under known TrollStore-owning trees,
    # drop the PersistenceHelper leftover (stale entitlements → SIGKILL),
    # and let the sort rank TrollStoreLite ahead of plain TrollStore so
    # a Lite install wins on a device that carries both.
    HELPER=$(ssh -p "$PORT" "$SSH_USER@$HOST" \
        "find /var/jb/Applications /var/containers/Bundle/Application \
              -maxdepth 4 -type f -name trollstorehelper 2>/dev/null \
         | grep -v 'PersistenceHelper' \
         | awk 'BEGIN{FS=\"/\"} {for(i=1;i<=NF;i++) if(\$i ~ /^TrollStoreLite\\.app\$/){print \"0 \"\$0; next}} {print \"1 \"\$0}' \
         | sort -k1,1 \
         | awk '{print \$2}' \
         | head -n1") || true
fi

if [ -z "$HELPER" ]; then
    echo "error: trollstorehelper not found on device" >&2
    echo "       pass --helper <path> or install TrollStore / TrollStore Lite" >&2
    exit 1
fi
echo "==> helper: $HELPER"

# ---------------------------------------------------------------------------
# Ship + install.
# ---------------------------------------------------------------------------
REMOTE_DIR="$(dirname "$REMOTE_IPA")"
echo "==> scp $IPA_NAME -> $SSH_USER@$HOST:$REMOTE_DIR/"
scp -q -P "$PORT" "$IPA" "$SSH_USER@$HOST:$REMOTE_IPA"
# trollstorehelper runs as mobile; scp lands the file as root, so
# hand it over so the sandbox can actually read it.
ssh -p "$PORT" "$SSH_USER@$HOST" "chown mobile:mobile '$REMOTE_IPA' 2>/dev/null || true"

echo "==> killall $KILL_TARGET (silent if not running)"
ssh -p "$PORT" "$SSH_USER@$HOST" "killall '$KILL_TARGET' 2>/dev/null || true"

echo "==> trollstorehelper install force $REMOTE_IPA"
set +e
ssh -p "$PORT" "$SSH_USER@$HOST" "$HELPER install force $REMOTE_IPA"
rc=$?
set -e
if [ "$rc" -eq 255 ]; then
    echo "  (ssh exit 255 — trollstorehelper commonly restarts SpringBoard mid-install; continuing)"
elif [ "$rc" -ne 0 ]; then
    echo "error: trollstorehelper exited $rc" >&2
    exit "$rc"
fi

# ---------------------------------------------------------------------------
# Relaunch the just-installed app.
# ---------------------------------------------------------------------------
echo "==> launching $PROCESS_NAME ($BUNDLE_ID)"
ssh -p "$PORT" "$SSH_USER@$HOST" "sleep 1; \
    (open '$BUNDLE_ID' 2>/dev/null \
        || uiopen '$BUNDLE_ID://' 2>/dev/null \
        || echo 'no launcher tool; start $PROCESS_NAME manually')"
