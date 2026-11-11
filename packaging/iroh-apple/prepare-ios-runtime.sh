#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <output-directory>" >&2
  exit 2
fi

OUTPUT_DIR=$1
mkdir -p "$OUTPUT_DIR"
RUNTIME_LOG="$OUTPUT_DIR/ios-runtime-install.log"
: > "$RUNTIME_LOG"

runtime_info() {
  xcrun simctl list runtimes available -j
}

find_lowest_runtime() {
  python3 -c '
import json
import sys


def version_tuple(value):
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None

candidates = []
for runtime in json.load(sys.stdin).get("runtimes", []):
    if not runtime.get("isAvailable") or runtime.get("platform") != "iOS":
        continue
    version = runtime.get("version", "")
    parsed = version_tuple(version)
    if parsed is None or parsed < (17, 0) or parsed >= (17, 5):
        continue
    identifier = runtime.get("identifier")
    if identifier:
        candidates.append((parsed, version, identifier))
if candidates:
    _, version, identifier = min(candidates)
    print(f"{version}|{identifier}")
    raise SystemExit(0)
raise SystemExit(1)
'
}

write_runtime_env() {
  local selected=$1
  local version=${selected%%|*}
  local identifier=${selected#*|}
  echo "selected iOS $version runtime: $identifier" | tee -a "$RUNTIME_LOG"
  {
    echo "IROH_IOS_RUNTIME_VERSION=$version"
    echo "IROH_IOS_RUNTIME_ID=$identifier"
  } > "$OUTPUT_DIR/ios-runtime.env"
}

# Keep an installed runtime as a fallback, but first try each lower version in
# ascending order. This matters on runner images that ship with 17.4 while a
# lower runtime is still downloadable from the pinned Xcode.
for candidate in 17.0 17.1 17.2 17.3 17.4; do
  selected=""
  if selected=$(runtime_info | find_lowest_runtime); then
    selected_version=${selected%%|*}
    selected_minor=${selected_version#17.}
    selected_minor=${selected_minor%%.*}
    candidate_minor=${candidate#17.}
    if (( selected_minor <= candidate_minor )); then
      write_runtime_env "$selected"
      exit 0
    fi
  fi

  echo "attempting to download iOS $candidate runtime" | tee -a "$RUNTIME_LOG"
  if ! xcodebuild -downloadPlatform iOS -buildVersion "$candidate" >> "$RUNTIME_LOG" 2>&1; then
    echo "iOS $candidate runtime is not available from the pinned Xcode" | tee -a "$RUNTIME_LOG"
  fi

  if selected=$(runtime_info | find_lowest_runtime); then
    selected_version=${selected%%|*}
    selected_minor=${selected_version#17.}
    selected_minor=${selected_minor%%.*}
    candidate_minor=${candidate#17.}
    if (( selected_minor <= candidate_minor )); then
      write_runtime_env "$selected"
      exit 0
    fi
  fi
done

if selected=$(runtime_info | find_lowest_runtime); then
  write_runtime_env "$selected"
  exit 0
fi

echo "could not install or find an iOS 17.x runtime below 17.5" >&2
cat "$RUNTIME_LOG" >&2
exit 1
