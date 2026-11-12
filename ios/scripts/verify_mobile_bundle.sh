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

# Open-source licenses data, generated for the current iroh release pin.
if [ ! -r "$bundle_dir/licenses.json" ]; then
  echo "error: The bundled mobile UI has no licenses.json at $bundle_dir." >&2
  echo "error: Run 'python3 ios/scripts/app_licenses.py generate --bundle ios/Avibe/Resources/MobileApp' after 'npm run build:mobile'." >&2
  exit 1
fi

if [ "${AVIBE_MOBILE_BUNDLE_EXISTENCE_ONLY:-0}" = "1" ]; then
  exit 0
fi

[ -n "$(find "$bundle_dir/assets" -type f -print -quit 2>/dev/null)" ] || fail

references="$(grep -Eo '\./assets/[^"]+' "$bundle_dir/mobile.html" || true)"
[ -n "$references" ] || fail

printf '%s\n' "$references" | while IFS= read -r reference; do
  [ -r "$bundle_dir/${reference#./}" ] || fail
done

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 "$script_dir/app_licenses.py" verify --bundle "$bundle_dir" >/dev/null
