#!/bin/bash
# Build "Stowaway.app" from launcher.applescript with this
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
sed -e "s|__SERVER_SCRIPT__|$PWD/server.py|" \
    -e "s|__PROJECT_DIR__|$PWD|" launcher.applescript > "$TMP/launcher.applescript"

rm -rf "$DEST/Stowaway.app"
osacompile -s -o "$DEST/Stowaway.app" "$TMP/launcher.applescript"

# The app was called "Cleanup Dashboard.app" before the rename. Left in place
# it still runs and still works, so you get two apps doing the same thing.
if [ -d "$DEST/Cleanup Dashboard.app" ]; then
  echo "Note: $DEST/Cleanup Dashboard.app is the pre-rename app. Delete it."
fi

echo "Installed: $DEST/Stowaway.app"
echo "Open it from Spotlight (Cmd-Space, type \"Stowaway\") or the $DEST folder."
