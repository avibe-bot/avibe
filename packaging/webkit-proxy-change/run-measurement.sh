#!/usr/bin/env bash
set -euo pipefail

label=${1:?runtime label: 17.0, newest, or 18.x}
out=${2:?artifact output directory}
repo_root=$(cd "$(dirname "$0")/../.." && pwd)
probe_dir=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out" "$out/derived" "$out/xcresult" "$probe_dir/generated"
exec > >(tee "$out/runner.log") 2>&1
stage_results="$out/stages.jsonl"
: > "$stage_results"
printf '{"status":"running","label":"%s"}\n' "$label" > "$out/summary.json"
finalize_summary() {
  python3 - "$out/summary.json" "$stage_results" "$1" <<'PY'
import json, sys
p, stages_path, status = sys.argv[1], sys.argv[2], int(sys.argv[3])
data = json.load(open(p))
stages = [json.loads(line) for line in open(stages_path)]
names = ("build-for-testing", "product-validation", "simulator-ready", "install", "test-launch", "direct-minimal", "cold-minimal", "full-matrix")
if not stages:
    cause = data.get("reason", "setup failed before build")
    stages = [{"stage": name, "status": "skipped", "verdict": "unmeasured", "exit": None, "cause": cause} for name in names]
    if data.get("status") != "unavailable":
        stages.insert(0, {"stage": "setup", "status": "failed", "exit": status, "cause": "see runner.log"})
data.update({"status": "unavailable" if data.get("status") == "unavailable" else "passed" if status == 0 else "failed",
             "exit": status, "stages": stages})
json.dump(data, open(p, "w"), sort_keys=True)
print(json.dumps(data, sort_keys=True))
PY
}
trap 'finalize_summary "$?"' EXIT
bounded() { python3 "$probe_dir/validate-stage.py" timeout "$@"; }
git -C "$repo_root" rev-parse HEAD > "$out/commit.txt"

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode_26.6.app/Contents/Developer}"

echo "label=$label"
echo "developer_dir=$DEVELOPER_DIR"
bounded 30 xcodebuild -version | tee "$out/xcode-version.txt"
bounded 30 xcrun simctl list runtimes available | tee "$out/runtime-inventory.txt"

select_runtime() {
  local selector=$1
  bounded 30 xcrun simctl list runtimes available -j | python3 -c '
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
  python3 "$probe_dir/validate-stage.py" timeout 300 bash "$repo_root/packaging/iroh-apple/prepare-ios-runtime.sh" "$runtime_dir"
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

bounded 30 xcrun simctl list runtimes available -j > "$out/runtime-inventory.json"
python3 - "$out/runtime-inventory.json" "$runtime_id" "$out/summary.json" <<'PY'
import json, sys
runtime = next(r for r in json.load(open(sys.argv[1]))["runtimes"] if r["identifier"] == sys.argv[2])
data = json.load(open(sys.argv[3])); data["runtimeBuild"] = runtime.get("buildversion", "unknown")
json.dump(data, open(sys.argv[3], "w"))
PY

# Use loopback aliases plus test-only hosts. The fixture and the browser never
# leave the runner; the names are unique to this matrix and have no public DNS.
marker="# avibe-webkit-proxy-change-${GITHUB_RUN_ID:-local}"
cleanup_hosts() {
  bounded 10 sudo -n sed -i '' "\\|$marker|d" /etc/hosts || true
  bounded 10 sudo -n ifconfig lo0 -alias 127.0.0.2 || true
}
trap 'finalize_summary "$?"; cleanup_hosts' EXIT
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
    bounded 5 sudo -n kill -TERM "$fixture_pid" || true
    for _ in {1..20}; do kill -0 "$fixture_pid" 2>/dev/null || break; sleep 0.1; done
    bounded 5 sudo -n kill -KILL "$fixture_pid" 2>/dev/null || true
  fi
}
trap 'finalize_summary "$?"; cleanup_fixture; cleanup_hosts' EXIT
sudo -n env PATH="$PATH" node "$probe_dir/fixture.mjs" "$out/cert" >"$out/fixture.stdout" 2>&1 &
fixture_pid=$!
echo "$fixture_pid" > "$out/fixture.pid"
for _ in {1..100}; do
  if curl --max-time 1 --silent --fail http://127.0.0.1:18090/health > "$out/fixture-health.json" && \
    python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("ok") is True else 1)' "$out/fixture-health.json"; then break; fi
  sleep 0.1
done
curl --max-time 2 --silent --fail http://127.0.0.1:18090/health | tee "$out/fixture-health.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["ok"] is True and len(d["listening"]) == 7 and len(d["originAddresses"]) == 2' "$out/fixture-health.json"
bounded 20 python3 - "$probe_dir/matrix.json" <<'PY'
import json, socket, sys
for row in json.load(open(sys.argv[1])):
    host = row["id"] + ".webkit-proxy.test"
    expected = "127.0.0.2" if row["origin"] == "h2" else "127.0.0.1"
    actual = {r[4][0] for r in socket.getaddrinfo(host, 443, socket.AF_INET)}
    assert actual == {expected}, (host, expected, actual)
print("Runner host mappings verified (simulator visibility still requires real requests)")
PY

# Choose a stable simulator device type available in the pinned Xcode.
device_type=$(bounded 30 xcrun simctl list devicetypes -j | python3 -c '
import json, sys
items=json.load(sys.stdin).get("devicetypes", [])
for preferred in ("iPhone 15", "iPhone 14", "iPhone 16"):
    for item in items:
        if item.get("name") == preferred:
            print(item["identifier"]); raise SystemExit
raise SystemExit("no supported iPhone device type")
')
device_id=$(bounded 30 xcrun simctl create "WebKit Proxy Change ${GITHUB_RUN_ID:-local} ${label}" "$device_type" "$runtime_id")
echo "$device_id" | tee "$out/device.txt"
cleanup_device() {
  bounded 20 xcrun simctl shutdown "$device_id" >/dev/null 2>&1 || true
  bounded 20 xcrun simctl delete "$device_id" >/dev/null 2>&1 || true
}
trap 'finalize_summary "$?"; cleanup_device; cleanup_fixture; cleanup_hosts' EXIT
bounded 120 xcrun simctl boot "$device_id"
# Migrate while compiling; readiness remains a strict gate before installation.
boot_timeout=180
[[ "$label" == "17.0" ]] && boot_timeout=480

project="$probe_dir/WebKitProxyChange.xcodeproj"
app="$out/derived-data/Build/Products/Debug-iphonesimulator/ProbeHost.app"
blocked_by=""
test_status=0

# Each stage records its own exit, or an explicit unmeasured dependency skip.
run_stage() {
  local stage=$1
  shift
  if [[ -n "$blocked_by" ]]; then
    printf '{"stage":"%s","status":"skipped","verdict":"unmeasured","exit":null,"cause":"%s failed"}\n' "$stage" "$blocked_by" >> "$stage_results"
    return 0
  fi
  set +e
  "$@" 2>&1 | tee "$out/$stage.log"
  local status=${PIPESTATUS[0]}
  set -e
  if [[ "$stage" == "full-matrix" || ( "$status" -eq 0 && ( "$stage" == "test-launch" || "$stage" == *minimal ) ) ]]; then
    set +e
    python3 "$probe_dir/validate-stage.py" "$stage" "$out" "$probe_dir/matrix.json" 2>&1 | tee "$out/$stage-evidence.log"
    local evidence_status=${PIPESTATUS[0]}
    set -e
    if [[ "$status" -eq 0 ]]; then status=$evidence_status; fi
  fi
  if [[ "$status" -ne 0 ]]; then
    blocked_by=$stage
    test_status=$status
    printf '{"stage":"%s","status":"failed","exit":%s,"cause":"see %s.log and evidence log"}\n' "$stage" "$status" "$stage" >> "$stage_results"
  else
    printf '{"stage":"%s","status":"passed","exit":0}\n' "$stage" >> "$stage_results"
  fi
}

validate_products() {
  python3 "$probe_dir/validate-stage.py" bundle "$app" "$out/build-for-testing.log"
}

test_method() {
  local stage=$1 method=$2 seconds=$3
  python3 "$probe_dir/validate-stage.py" timeout "$seconds" \
    xcodebuild test-without-building -project "$project" -scheme WebKitProxyChange \
    -destination "platform=iOS Simulator,id=$device_id" \
    -derivedDataPath "$out/derived-data" \
    -resultBundlePath "$out/xcresult/$stage.xcresult" \
    -parallel-testing-enabled NO -maximum-parallel-testing-workers 1 \
    "-only-testing:WebKitProxyChangeTests/WebKitProxyChangeTests/$method" \
    CODE_SIGNING_ALLOWED=NO
}

run_stage build-for-testing python3 "$probe_dir/validate-stage.py" timeout 600 \
  xcodebuild build-for-testing -project "$project" -scheme WebKitProxyChange \
  -destination "platform=iOS Simulator,id=$device_id" \
  -derivedDataPath "$out/derived-data" CODE_SIGNING_ALLOWED=NO
run_stage product-validation validate_products
run_stage simulator-ready python3 "$probe_dir/validate-stage.py" timeout "$boot_timeout" xcrun simctl bootstatus "$device_id" -b
run_stage install python3 "$probe_dir/validate-stage.py" timeout 60 xcrun simctl install "$device_id" "$app"
run_stage test-launch test_method test-launch testHostLaunch 150
run_stage direct-minimal test_method direct-minimal testDirectTLSMinimal 120
run_stage cold-minimal test_method cold-minimal testColdMinimal 120
run_stage full-matrix test_method full-matrix testMatrix 900

python3 "$probe_dir/validate-stage.py" timeout 30 xcrun simctl spawn "$device_id" log show --style compact --last 30m \
  --predicate 'process == "com.apple.WebKit.Networking" OR process == "com.apple.WebKit.WebContent"' \
  > "$out/webkit-process.log" 2>&1 || true
python3 "$probe_dir/validate-stage.py" timeout 30 xcrun simctl spawn "$device_id" log show --style compact --last 30m \
  --predicate 'eventMessage CONTAINS[c] "assert" OR eventMessage CONTAINS[c] "crash"' \
  > "$out/assertion-crash.log" 2>&1 || true
find "$HOME/Library/Logs/DiagnosticReports" -maxdepth 1 -type f \
  \( -iname '*WebKit*' -o -iname '*Networking*' -o -iname '*WebContent*' \) \
  -newermt "$(date -u -v-2H '+%Y-%m-%d %H:%M:%S')" -print -exec cp {} "$out/derived/" \; 2>/dev/null || true
exit "$test_status"
