#!/usr/bin/env bash
set -euo pipefail

label=${1:?runtime label: 17.0, newest, or 18.x}
out=${2:?artifact output directory}
repo_root=$(cd "$(dirname "$0")/../.." && pwd)
probe_dir=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out" "$out/derived" "$out/xcresult" "$probe_dir/generated"
exec > >(tee "$out/runner.log") 2>&1

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode_26.6.app/Contents/Developer}"

echo "label=$label"
echo "developer_dir=$DEVELOPER_DIR"
xcodebuild -version || true
xcrun simctl list runtimes available || true

select_runtime() {
  local selector=$1
  xcrun simctl list runtimes available -j | python3 -c '
import json, sys
selector = sys.argv[1]
runtimes = []
for r in json.load(sys.stdin).get("runtimes", []):
    if not r.get("isAvailable") or r.get("platform") != "iOS":
        continue
    version = r.get("version", "")
    try: parsed = tuple(int(x) for x in version.split("."))
    except ValueError: continue
    if selector == "17.0" and parsed != (17, 0): continue
    if selector == "18.x" and parsed[:1] != (18,): continue
    runtimes.append((parsed, version, r["identifier"]))
if not runtimes:
    raise SystemExit(1)
parsed, version, identifier = sorted(runtimes)[-1] if selector != "17.0" else sorted(runtimes)[0]
print(f"{version}|{identifier}")
' "$selector"
}

runtime=""
if [[ "$label" == "17.0" ]]; then
  runtime_dir="$out/runtime-install"
  bash "$repo_root/packaging/iroh-apple/prepare-ios-runtime.sh" "$runtime_dir"
  # The shared installer may choose 17.1+ as a fallback; this lane must not
  # silently call that result the requested 17.0 measurement.
  source "$runtime_dir/ios-runtime.env"
  [[ "${IROH_IOS_RUNTIME_VERSION}" == "17.0" ]] || {
    echo "exact iOS 17.0 runtime unavailable; installer selected ${IROH_IOS_RUNTIME_VERSION}" >&2
    exit 3
  }
  runtime="${IROH_IOS_RUNTIME_VERSION}|${IROH_IOS_RUNTIME_ID}"
elif [[ "$label" == "newest" ]]; then
  runtime=$(select_runtime newest)
elif [[ "$label" == "18.x" ]]; then
  if ! runtime=$(select_runtime 18.x); then
    printf '{"status":"unavailable","reason":"no installed iOS 18.x runtime"}\n' | tee "$out/summary.json"
    exit 0
  fi
else
  echo "unknown runtime label: $label" >&2
  exit 2
fi

runtime_version=${runtime%%|*}
runtime_id=${runtime#*|}
printf '%s\n' "$runtime" | tee "$out/runtime.txt"
printf '{"status":"running","label":"%s","runtimeVersion":"%s","runtimeID":"%s"}\n' "$label" "$runtime_version" "$runtime_id" > "$out/summary.json"

# Use loopback aliases plus test-only hosts. The fixture and the browser never
# leave the runner; the names are unique to this matrix and have no public DNS.
marker="# avibe-webkit-proxy-change-${GITHUB_RUN_ID:-local}"
cleanup_hosts() {
  sudo -n sed -i '' "\\|$marker|d" /etc/hosts || true
  sudo -n ifconfig lo0 -alias 127.0.0.2 || true
}
trap cleanup_hosts EXIT
sudo -n ifconfig lo0 alias 127.0.0.2 255.0.0.0
while read -r ip host; do
  [[ -z "$host" ]] && continue
  printf '%s %s %s\n' "$ip" "$host" "$marker" | sudo -n tee -a /etc/hosts >/dev/null
done < <(python3 - "$probe_dir/matrix.json" <<'PY'
import json, sys
for row in json.load(open(sys.argv[1])):
    ip = "127.0.0.2" if row["origin"] == "h2" else "127.0.0.1"
    print(ip, f"{row['id']}.webkit-proxy.test")
PY
)

bash "$probe_dir/generate-cert.sh" "$out/cert"
cp "$out/cert/Anchor.der" "$probe_dir/generated/Anchor.der"
fixture_pid=""
cleanup_fixture() {
  if [[ -n "$fixture_pid" ]] && kill -0 "$fixture_pid" 2>/dev/null; then
    sudo -n kill -TERM "$fixture_pid" || true
    for _ in {1..20}; do kill -0 "$fixture_pid" 2>/dev/null || break; sleep 0.1; done
    sudo -n kill -KILL "$fixture_pid" 2>/dev/null || true
  fi
}
trap 'cleanup_fixture; cleanup_hosts' EXIT
sudo -n env PATH="$PATH" node "$probe_dir/fixture.mjs" "$out/cert" >"$out/fixture.stdout" 2>&1 &
fixture_pid=$!
echo "$fixture_pid" > "$out/fixture.pid"
for _ in {1..100}; do
  if curl --silent --fail http://127.0.0.1:18090/health >/dev/null; then break; fi
  sleep 0.1
done
curl --silent --fail http://127.0.0.1:18090/health | tee "$out/fixture-health.json"

# Choose a stable simulator device type available in the pinned Xcode.
device_type=$(xcrun simctl list devicetypes -j | python3 -c '
import json, sys
items=json.load(sys.stdin).get("devicetypes", [])
for preferred in ("iPhone 15", "iPhone 14", "iPhone 16"):
    for item in items:
        if item.get("name") == preferred:
            print(item["identifier"]); raise SystemExit
raise SystemExit("no supported iPhone device type")
')
device_id=$(xcrun simctl create "WebKit Proxy Change ${GITHUB_RUN_ID:-local} ${label}" "$device_type" "$runtime_id")
echo "$device_id" | tee "$out/device.txt"
cleanup_device() {
  xcrun simctl shutdown "$device_id" >/dev/null 2>&1 || true
  xcrun simctl delete "$device_id" >/dev/null 2>&1 || true
}
trap 'cleanup_device; cleanup_fixture; cleanup_hosts' EXIT
xcrun simctl boot "$device_id"
xcrun simctl bootstatus "$device_id" -b

project="$probe_dir/WebKitProxyChange.xcodeproj"
set +e
xcodebuild build-for-testing -project "$project" -scheme WebKitProxyChange \
  -destination "platform=iOS Simulator,id=$device_id" \
  -derivedDataPath "$out/derived-data" \
  CODE_SIGNING_ALLOWED=NO | tee "$out/build-for-testing.log"
build_status=${PIPESTATUS[0]}
set -e

if [[ "$build_status" -eq 0 ]]; then
  set +e
  xcodebuild test-without-building -project "$project" -scheme WebKitProxyChange \
    -destination "platform=iOS Simulator,id=$device_id" \
    -derivedDataPath "$out/derived-data" \
    -resultBundlePath "$out/xcresult/direct-minimal.xcresult" \
    -parallel-testing-enabled NO -maximum-parallel-testing-workers 1 \
    -only-testing:WebKitProxyChangeTests/testDirectTLSMinimal \
    CODE_SIGNING_ALLOWED=NO | tee "$out/direct-minimal.log"
  direct_status=${PIPESTATUS[0]}
  set -e
else
  direct_status=125
fi

if [[ "$build_status" -eq 0 && "$direct_status" -eq 0 ]]; then
  set +e
  xcodebuild test-without-building -project "$project" -scheme WebKitProxyChange \
    -destination "platform=iOS Simulator,id=$device_id" \
    -derivedDataPath "$out/derived-data" \
    -resultBundlePath "$out/xcresult/cold-minimal.xcresult" \
    -parallel-testing-enabled NO -maximum-parallel-testing-workers 1 \
    -only-testing:WebKitProxyChangeTests/testColdMinimal \
    CODE_SIGNING_ALLOWED=NO | tee "$out/cold-minimal.log"
  minimal_status=${PIPESTATUS[0]}
  set -e
else
  minimal_status=$([[ "$build_status" -ne 0 ]] && echo "$build_status" || echo "$direct_status")
fi

if [[ "$build_status" -eq 0 && "$direct_status" -eq 0 && "$minimal_status" -eq 0 ]]; then
  set +e
  xcodebuild test-without-building -project "$project" -scheme WebKitProxyChange \
    -destination "platform=iOS Simulator,id=$device_id" \
    -derivedDataPath "$out/derived-data" \
    -resultBundlePath "$out/xcresult/full-matrix.xcresult" \
    -parallel-testing-enabled NO -maximum-parallel-testing-workers 1 \
    -only-testing:WebKitProxyChangeTests/testMatrix \
    CODE_SIGNING_ALLOWED=NO | tee "$out/full-matrix.log"
  test_status=${PIPESTATUS[0]}
  set -e
else
  test_status=$([[ "$build_status" -ne 0 ]] && echo "$build_status" || echo "$minimal_status")
fi

xcrun simctl spawn "$device_id" log show --style compact --last 30m \
  --predicate 'process == "com.apple.WebKit.Networking" OR process == "com.apple.WebKit.WebContent"' \
  > "$out/webkit-process.log" 2>&1 || true
xcrun simctl spawn "$device_id" log show --style compact --last 30m \
  --predicate 'eventMessage CONTAINS[c] "assert" OR eventMessage CONTAINS[c] "crash"' \
  > "$out/assertion-crash.log" 2>&1 || true
find "$HOME/Library/Logs/DiagnosticReports" -maxdepth 1 -type f \
  \( -iname '*WebKit*' -o -iname '*Networking*' -o -iname '*WebContent*' \) \
  -newermt "$(date -u -v-2H '+%Y-%m-%d %H:%M:%S')" -print -exec cp {} "$out/derived/" \; 2>/dev/null || true
BUILD_STATUS="$build_status" DIRECT_STATUS="$direct_status" MINIMAL_STATUS="$minimal_status" python3 - "$out/summary.json" "$test_status" <<'PY'
import json, os, sys
p=sys.argv[1]
data=json.load(open(p)); data.update({"status":"passed" if int(sys.argv[2]) == 0 else "failed", "buildExit":int(os.environ.get("BUILD_STATUS", "0")), "directExit":int(os.environ.get("DIRECT_STATUS", "0")), "minimalExit":int(os.environ.get("MINIMAL_STATUS", "0")), "testExit":int(sys.argv[2])})
json.dump(data, open(p, "w"), sort_keys=True); print(json.dumps(data, sort_keys=True))
PY
exit "$test_status"
