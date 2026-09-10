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

# Signature-aware staging. A Developer ID (or any identity-bearing) signature
# is copied as-is: re-signing with an ad-hoc identity here would strip it and
# the DMG would ship unsigned. An ad-hoc or absent signature means the bundle
# is an acceptance artifact, and the disposable copy is ad-hoc signed so macOS
# can still verify its structure.
signature=$(codesign -dv "$app" 2>&1 | sed -n 's/^Signature=//p' || true)
team=$(codesign -dv "$app" 2>&1 | sed -n 's/^TeamIdentifier=//p' || true)

staging=$(mktemp -d "${TMPDIR:-/tmp}/avibe-dmg.XXXXXX")
trap 'rm -rf "$staging"' EXIT HUP INT TERM

cp -R "$app" "$staging/Avibe.app"
ln -s /Applications "$staging/Applications"
mkdir -p "$(dirname "$output")"
rm -f "$output"

if [ "$signature" = "adhoc" ] || [ -z "$signature" ] || [ "$team" = "not set" ]; then
  # A linker-signed executable does not seal the resources copied into the app
  # bundle. Ad-hoc sign the disposable DMG copy so macOS can verify its
  # structure. This is not Developer ID signing and does not bypass
  # Gatekeeper/notarization.
  codesign --force --deep --sign - "$staging/Avibe.app"
  codesign --verify --deep --strict "$staging/Avibe.app"
else
  # Sealing the copy must not invalidate the identity signature: verify the
  # staging copy still validates against the same team before imaging it.
  codesign --verify --deep --strict "$staging/Avibe.app"
fi

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
