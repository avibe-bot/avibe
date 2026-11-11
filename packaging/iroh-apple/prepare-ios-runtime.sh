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

find_runtime_id() {
  local version=$1
  python3 -c '
import json
import sys

wanted = sys.argv[1]
data = json.load(sys.stdin)
for runtime in data.get("runtimes", []):
    if not runtime.get("isAvailable"):
        continue
    if runtime.get("platform") != "iOS":
        continue
    if runtime.get("version") == wanted:
        print(runtime["identifier"])
        raise SystemExit(0)
raise SystemExit(1)
' "$version"
}

for candidate in 17.0 17.1 17.2 17.3 17.4; do
  runtime_id=""
  if runtime_id=$(runtime_info | find_runtime_id "$candidate"); then
    echo "found installed iOS $candidate runtime: $runtime_id" | tee -a "$RUNTIME_LOG"
    {
      echo "IROH_IOS_RUNTIME_VERSION=$candidate"
      echo "IROH_IOS_RUNTIME_ID=$runtime_id"
    } > "$OUTPUT_DIR/ios-runtime.env"
    exit 0
  fi

  echo "attempting to download iOS $candidate runtime" | tee -a "$RUNTIME_LOG"
  if xcodebuild -downloadPlatform iOS -buildVersion "$candidate" >> "$RUNTIME_LOG" 2>&1; then
    if runtime_id=$(runtime_info | find_runtime_id "$candidate"); then
      echo "downloaded iOS $candidate runtime: $runtime_id" | tee -a "$RUNTIME_LOG"
      {
        echo "IROH_IOS_RUNTIME_VERSION=$candidate"
        echo "IROH_IOS_RUNTIME_ID=$runtime_id"
      } > "$OUTPUT_DIR/ios-runtime.env"
      exit 0
    fi
  else
    echo "iOS $candidate runtime is not available from the pinned Xcode" | tee -a "$RUNTIME_LOG"
  fi
done

echo "could not install or find an iOS 17.x runtime below 17.5" >&2
cat "$RUNTIME_LOG" >&2
exit 1
