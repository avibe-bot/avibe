#!/bin/sh
set -eu

bundle_dir="${1:-}"

fail() {
  echo "error: The bundled mobile UI is missing or incomplete at ${bundle_dir:-<unset>}." >&2
  echo "error: Run 'cd ui && npm run build:mobile' before building Avibe." >&2
  exit 1
}

[ -n "$bundle_dir" ] || fail
[ -r "$bundle_dir/mobile.html" ] || fail
[ -d "$bundle_dir/assets" ] || fail

if [ "${AVIBE_MOBILE_BUNDLE_EXISTENCE_ONLY:-0}" = "1" ]; then
  exit 0
fi

[ -n "$(find "$bundle_dir/assets" -type f -print -quit 2>/dev/null)" ] || fail

references="$(grep -Eo '\./assets/[^"]+' "$bundle_dir/mobile.html" || true)"
[ -n "$references" ] || fail

printf '%s\n' "$references" | while IFS= read -r reference; do
  [ -r "$bundle_dir/${reference#./}" ] || fail
done
