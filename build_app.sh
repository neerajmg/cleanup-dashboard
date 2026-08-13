#!/bin/bash
# Build "Cleanup Dashboard.app" from launcher.applescript with this
# project's location baked in, and install it into /Applications
# (or ~/Applications if /Applications isn't writable).
set -euo pipefail
cd "$(dirname "$0")"

if [ -w /Applications ]; then
  DEST="/Applications"
else
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sed "s|__SERVER_SCRIPT__|$PWD/server.py|" launcher.applescript > "$TMP/launcher.applescript"

rm -rf "$DEST/Cleanup Dashboard.app"
osacompile -s -o "$DEST/Cleanup Dashboard.app" "$TMP/launcher.applescript"

echo "Installed: $DEST/Cleanup Dashboard.app"
echo "Open it from Spotlight (Cmd-Space, type \"Cleanup\") or the $DEST folder."
