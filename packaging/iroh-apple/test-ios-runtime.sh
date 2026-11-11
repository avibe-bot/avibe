#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <iroh-ffi-checkout> <output-directory>" >&2
  exit 2
fi

SOURCE_DIR=$1
OUTPUT_DIR=$2
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$SCRIPT_DIR/versions.env"

: "${IROH_IOS_RUNTIME_VERSION:?IROH_IOS_RUNTIME_VERSION must be set}"
: "${IROH_IOS_RUNTIME_ID:?IROH_IOS_RUNTIME_ID must be set}"

runtime_json="$OUTPUT_DIR/ios-runtimes-at-test.json"
xcrun simctl list runtimes available -j > "$runtime_json"
python3 - "$runtime_json" "$IROH_IOS_RUNTIME_VERSION" "$IROH_IOS_RUNTIME_ID" <<'PY'
import json
import sys

path, wanted_version, wanted_id = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
for runtime in data.get("runtimes", []):
    if (
        runtime.get("identifier") == wanted_id
        and runtime.get("version") == wanted_version
        and runtime.get("platform") == "iOS"
        and runtime.get("isAvailable")
    ):
        raise SystemExit(0)
raise SystemExit(
    f"requested iOS runtime {wanted_version} ({wanted_id}) is not available"
)
PY

# Use only a device type explicitly advertised by the selected runtime. A newer
# runner image can have iPhone types that cannot boot an older iOS runtime.
device_type=$(python3 - "$runtime_json" "$IROH_IOS_RUNTIME_ID" <<'PY'
import json
import sys

path, runtime_id = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
for runtime in data.get("runtimes", []):
    if runtime.get("identifier") != runtime_id:
        continue
    for device in runtime.get("supportedDeviceTypes", []):
        identifier = device if isinstance(device, str) else device.get("identifier")
        if identifier:
            print(identifier)
            raise SystemExit(0)
    raise SystemExit(f"runtime {runtime_id} advertises no supported device types")
raise SystemExit(f"runtime {runtime_id} is missing from {path}")
PY
)

device_name="Iroh iOS ${IROH_IOS_RUNTIME_VERSION} runtime test"
device_id=$(xcrun simctl create "$device_name" "$device_type" "$IROH_IOS_RUNTIME_ID")
cleanup() {
  xcrun simctl shutdown "$device_id" >/dev/null 2>&1 || true
  xcrun simctl delete "$device_id" >/dev/null 2>&1 || true
}
trap cleanup EXIT

xcrun simctl boot "$device_id"
xcrun simctl bootstatus "$device_id" -b

cd "$SOURCE_DIR"
log_path="$OUTPUT_DIR/ios-runtime-test.log"
set +e
arch -arm64 xcodebuild test \
  -scheme IrohLib \
  -only-testing:IrohLibTests \
  -destination "platform=iOS Simulator,id=$device_id" \
  -derivedDataPath "$OUTPUT_DIR/derived-ios-runtime" \
  IPHONEOS_DEPLOYMENT_TARGET="$IOS_DEPLOYMENT_TARGET" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  DEVELOPMENT_TEAM= \
  2>&1 | tee "$log_path"
status=${PIPESTATUS[0]}
set -e

# A linker warning about a newer deployment target invalidates this result even
# if xcodebuild happened to return zero.
if grep -Eiq "was built for (a )?newer .*version .*than .*being linked|deployment target.{0,40}(higher|newer)" "$log_path"; then
  echo "iOS runtime test failed: newer deployment-target warning appeared in xcodebuild output" >&2
  status=1
fi

cat > "$OUTPUT_DIR/ios-runtime-test.json" <<EOF
{
  "runtime_version": "$IROH_IOS_RUNTIME_VERSION",
  "runtime_identifier": "$IROH_IOS_RUNTIME_ID",
  "device_type": "$device_type",
  "xcodebuild_exit_code": $status
}
EOF

if [[ $status -ne 0 ]]; then
  exit "$status"
fi

echo "iOS runtime test passed on iOS $IROH_IOS_RUNTIME_VERSION ($IROH_IOS_RUNTIME_ID)"
