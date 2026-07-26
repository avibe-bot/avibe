#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  echo "usage: create-macos-dmg.sh <Avibe.app> <output.dmg>" >&2
  exit 2
fi

app=$1
output=$2

if [ ! -d "$app/Contents/MacOS" ]; then
  echo "input is not a macOS application bundle: $app" >&2
  exit 2
fi

staging=$(mktemp -d "${TMPDIR:-/tmp}/avibe-dmg.XXXXXX")
trap 'rm -rf "$staging"' EXIT HUP INT TERM

cp -R "$app" "$staging/Avibe.app"
ln -s /Applications "$staging/Applications"
mkdir -p "$(dirname "$output")"
rm -f "$output"

# Tauri's decorated DMG helper drives Finder through AppleScript, which is
# brittle on headless CI and hardened developer machines. A plain compressed
# image has the same install semantics and no GUI dependency.
hdiutil create \
  -volname Avibe \
  -srcfolder "$staging" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -ov \
  "$output"

hdiutil verify "$output"
