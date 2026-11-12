#!/usr/bin/env bash
# Production stage/control owner. Sourcing defines functions only; no tools run.
measurement_stage_names=(build-for-testing product-validation simulator-ready install test-launch lifecycle controls-health direct-minimal cold-minimal full-matrix instance-health instance-routes)

finalize_summary() {
  local status=$1 shell_status=$1 report_status=0 summary_status=0
  if [[ "${test_status:-0}" -ne 0 ]]; then status=$test_status; fi
  python3 "$probe_dir/instance_evidence.py" "$out" "$probe_dir/instance-cases.json" || report_status=$?
  if [[ "$status" -eq 0 && "$report_status" -ne 0 ]]; then status=$report_status; fi
  python3 - "$out/summary.json" "$stage_results" "$status" "$report_status" "$shell_status" "${measurement_stage_names[@]}" <<'PY' || summary_status=$?
import json, sys
p, stages_path, raw_status, report_status, shell_status, *names = sys.argv[1:]
status, report_status, shell_status = int(raw_status), int(report_status), int(shell_status)
data = json.load(open(p))
stages = [json.loads(line) for line in open(stages_path)]
unavailable = data.get("status") == "unavailable"
if not stages and not unavailable:
    stages.append({"stage":"setup", "status":"failed", "exit":status or 66, "cause":"see runner.log"})
seen = {row["stage"] for row in stages}
for name in names:
    if name not in seen:
        stages.append({"stage":name, "status":"skipped", "verdict":"unmeasured", "exit":None,
                       "cause":data.get("reason", "execution stopped before stage")})
if shell_status and shell_status != status:
    stages.append({"stage":"runner-exit", "status":"failed", "exit":shell_status,
                   "cause":"later shell error; first accumulated stage failure retained"})
if report_status:
    stages.append({"stage":"instance-report", "status":"failed", "exit":report_status,
                   "cause":"partial instance report generation failed; see runner.log"})
failed = [row for row in stages if row["status"] == "failed"]
if status == 0 and failed:
    status = failed[0]["exit"] or 66
if status == 0 and not unavailable and any(row["status"] != "passed" for row in stages):
    status = 66
state = "unavailable" if unavailable and status == 0 else "passed" if status == 0 else "failed"
data.update(status=state, exit=status, shellExit=shell_status, stages=stages)
json.dump(data, open(p, "w"), sort_keys=True)
print(json.dumps(data, sort_keys=True))
raise SystemExit(status)
PY
  # Summary normally returns the final run status. On writer failure, never
  # leave a stale running/passed summary next to a nonzero process exit.
  if [[ "$summary_status" -ne 0 ]]; then
    if [[ "$status" -eq 0 ]]; then status=$summary_status; fi
    python3 - "$out" "$stage_results" "$status" "$summary_status" "$shell_status" "$report_status" "${measurement_stage_names[@]}" <<'PY' || true
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "summary.json"
try: data = json.loads(p.read_text())
except (OSError, ValueError): data = {}
if not isinstance(data, dict): data = {}
status, summary_status, shell_status, report_status = map(int, sys.argv[3:7])
if data.get("status") != "failed" or data.get("exit") != status:
    # Do not repeat the strict read that may have broken the primary writer.
    # Keep every independently usable record, including rows after a bad line.
    stages, recovery = [], []
    try:
        with open(sys.argv[2], errors="replace") as ledger:
            for number, line in enumerate(ledger, 1):
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("stage"), str) or row.get("status") not in ("passed", "failed", "skipped"):
                        raise ValueError("unusable stage record")
                    stages.append(row)
                except (ValueError, TypeError) as error:
                    recovery.append(f"line {number}: {error}")
    except OSError as error:
        recovery.append(f"ledger read: {error}")
    seen = {row["stage"] for row in stages}
    stages.extend({"stage":name,"status":"skipped","verdict":"unmeasured","exit":None,
                   "cause":"execution stopped or stage ledger unavailable"} for name in sys.argv[7:] if name not in seen)
    if shell_status and shell_status != status:
        stages.append({"stage":"runner-exit", "status":"failed", "exit":shell_status,
                       "cause":"later shell error; first accumulated stage failure retained"})
    if report_status:
        stages.append({"stage":"instance-report", "status":"failed", "exit":report_status,
                       "cause":"partial instance report generation failed; see runner.log"})
    stages.append({"stage":"summary-finalization", "status":"failed", "exit":summary_status,
                   "cause":"summary generation failed; recovered available stage rows", "ledgerRecovery":recovery})
    data.update(status="failed", exit=status, shellExit=shell_status, stages=stages)
    p.write_text(json.dumps(data, sort_keys=True))
PY
  fi
  return "$status"
}

finish_measurement() {
  local status=$1
  trap - EXIT
  set +e
  finalize_summary "$status"
  status=$?
  # These functions are registered only after their resources exist.
  if declare -F cleanup_device >/dev/null; then cleanup_device; fi
  if declare -F cleanup_fixture >/dev/null; then cleanup_fixture; fi
  if declare -F cleanup_hosts >/dev/null; then cleanup_hosts; fi
  exit "$status"
}

# A fixed dependency list, not an implicit global stop flag. Each stage keeps its
# own failure/cause, while test_status retains the first failure for process exit.
run_stage() {
  local stage=$1 dependencies=$2
  shift 2
  local reason status evidence_status
  local pipeline_status=()
  reason=$(python3 - "$stage_results" "$dependencies" <<'PY'
import json, sys
rows = {row["stage"]:row for row in map(json.loads, open(sys.argv[1]))}
blocked = [name for name in sys.argv[2].split() if rows.get(name, {}).get("status") != "passed"]
print(", ".join(f"{name} {rows.get(name, {}).get('status', 'missing')}" for name in blocked))
PY
  ) || return $?
  if [[ -n "$reason" ]]; then
    python3 - "$stage_results" "$stage" "$reason" <<'PY'
import json, sys
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"stage":sys.argv[2], "status":"skipped", "verdict":"unmeasured", "exit":None,
                        "cause":"prerequisite: " + sys.argv[3]}) + "\n")
PY
    return 0
  fi
  set +e
  "$@" 2>&1 | tee "$out/$stage.log"
  pipeline_status=("${PIPESTATUS[@]}")
  status=${pipeline_status[0]}
  if [[ "$status" -eq 0 ]]; then status=${pipeline_status[1]}; fi
  set -e
  # Preserve partial observation reports even when execution itself failed.
  if [[ "$stage" == "lifecycle" || "$stage" == "full-matrix" || "$stage" == "instance-routes" || ( "$status" -eq 0 && ( "$stage" == "test-launch" || "$stage" == *minimal ) ) ]]; then
    set +e
    python3 "$probe_dir/validate-stage.py" "$stage" "$out" "$probe_dir/matrix.json" 2>&1 | tee "$out/$stage-evidence.log"
    pipeline_status=("${PIPESTATUS[@]}")
    evidence_status=${pipeline_status[0]}
    if [[ "$evidence_status" -eq 0 ]]; then evidence_status=${pipeline_status[1]}; fi
    set -e
    if [[ "$status" -eq 0 ]]; then status=$evidence_status; fi
  fi
  if [[ "$status" -ne 0 ]]; then
    if [[ "$test_status" -eq 0 ]]; then test_status=$status; fi
    printf '{"stage":"%s","status":"failed","exit":%s,"cause":"see %s.log and evidence log"}\n' "$stage" "$status" "$stage" >> "$stage_results"
  else
    printf '{"stage":"%s","status":"passed","exit":0}\n' "$stage" >> "$stage_results"
  fi
}

fixture_health() {
  local stage=$1
  # Fresh one-shot health, not a reused initial snapshot or retry to success.
  curl --max-time 2 --silent --show-error --fail http://127.0.0.1:18090/health > "$out/$stage.json" || return $?
  python3 - "$out/$stage.json" <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))
assert h["ok"] is True and h["cases"] == 27 and h["lifecycleCases"] == 3 and h["instanceCases"] == 10
assert set(h["listening"]) == {443,18443,18444,18080,18082,18083,18090}
assert set(h["originAddresses"]) == {"127.0.0.1:443","127.0.0.2:443"}
print(json.dumps(h, sort_keys=True))
PY
}

wait_lifecycle_event() {
  local case_id=$1 step=$2 status
  for _ in {1..160}; do
    if python3 - "$out/cert/fixture.jsonl" "$case_id" "$step" <<'PY'
import json, sys
path, case_id, step = sys.argv[1:]
events = []
try:
    with open(path) as f:
        for line in f:
            try: events.append(json.loads(line))
            except json.JSONDecodeError: break  # A writer may be appending the final record.
except FileNotFoundError:
    pass
rows = [e for e in events if e.get("event") == "client" and e.get("case") == case_id]
errors = [e for e in rows if e.get("step") == "lifecycle_error"]
if errors:
    print("lifecycle explicit error: " + json.dumps(errors[-1], sort_keys=True), file=sys.stderr)
    raise SystemExit(65)
raise SystemExit(0 if any(e.get("step") == step for e in rows) else 75)
PY
    then return 0; else status=$?; fi
    if [[ "$status" -ne 75 ]]; then return "$status"; fi
    sleep 0.25
  done
  echo "lifecycle event timeout: $case_id/$step" >&2
  return 124
}

lifecycle_case() {
  local case_id=$1 store_id=$2 process_log=$3
  if [[ "$case_id" == "lifecycle-relaunch" ]]; then
    bounded 30 xcrun simctl launch "$device_id" com.avibe.webkit-probe \
      --lifecycle-case "$case_id" --lifecycle-phase prepare \
      --lifecycle-store-id "$store_id" --lifecycle-control-url http://127.0.0.1:18090 \
      >> "$process_log" 2>&1 || return $?
    wait_lifecycle_event "$case_id" lifecycle_ready || return $?
    bounded 30 xcrun simctl terminate "$device_id" com.avibe.webkit-probe >> "$process_log" 2>&1 || return $?
    bounded 30 xcrun simctl launch "$device_id" com.avibe.webkit-probe \
      --lifecycle-case "$case_id" --lifecycle-phase resume \
      --lifecycle-store-id "$store_id" --lifecycle-control-url http://127.0.0.1:18090 \
      >> "$process_log" 2>&1 || return $?
  else
    bounded 30 xcrun simctl launch "$device_id" com.avibe.webkit-probe \
      --lifecycle-case "$case_id" --lifecycle-phase single \
      --lifecycle-store-id "$store_id" --lifecycle-control-url http://127.0.0.1:18090 \
      >> "$process_log" 2>&1 || return $?
  fi
  wait_lifecycle_event "$case_id" lifecycle_done
}

run_lifecycle() {
  local process_log="$out/lifecycle-process.log" result=0 status case_id store_id
  : > "$process_log"
  for case_id in lifecycle-reload lifecycle-recreate lifecycle-relaunch; do
    store_id=$(python3 -c 'import uuid; print(uuid.uuid4())') || return $?
    # Initial/final termination is cleanup; errors (including no running App)
    # remain in process_log. A failed prepare never reaches measured resume.
    bounded 30 xcrun simctl terminate "$device_id" com.avibe.webkit-probe >> "$process_log" 2>&1 || true
    if lifecycle_case "$case_id" "$store_id" "$process_log"; then status=0; else status=$?; fi
    printf '%s exit=%s\n' "$case_id" "$status"
    if [[ "$result" -eq 0 && "$status" -ne 0 ]]; then result=$status; fi
    bounded 30 xcrun simctl terminate "$device_id" com.avibe.webkit-probe >> "$process_log" 2>&1 || true
  done
  return "$result"
}

run_measurement_stages() {
  run_stage build-for-testing "" python3 "$probe_dir/validate-stage.py" timeout 600 \
    xcodebuild build-for-testing -project "$project" -scheme WebKitProxyChange \
    -destination "platform=iOS Simulator,id=$device_id" \
    -derivedDataPath "$out/derived-data" CODE_SIGNING_ALLOWED=NO
  run_stage product-validation "build-for-testing" validate_products
  run_stage simulator-ready "product-validation" python3 "$probe_dir/validate-stage.py" timeout "$boot_timeout" xcrun simctl bootstatus "$device_id" -b
  run_stage install "simulator-ready" python3 "$probe_dir/validate-stage.py" timeout 60 xcrun simctl install "$device_id" "$app"
  run_stage test-launch "install" test_method test-launch testHostLaunch 150
  run_stage lifecycle "test-launch" run_lifecycle
  # Lifecycle is an observation branch. Its failure is retained, not a global
  # blocker. Recheck the real fixture before another branch consumes it.
  run_stage controls-health "test-launch" fixture_health controls-health
  run_stage direct-minimal "controls-health" test_method direct-minimal testDirectTLSMinimal 120
  run_stage cold-minimal "direct-minimal" test_method cold-minimal testColdMinimal 120
  run_stage full-matrix "cold-minimal" test_method full-matrix testMatrix 900
  # Matrix failure cannot starve independent instance data. This fresh health
  # gate is mandatory even when the matrix passed; failures never reset status.
  run_stage instance-health "cold-minimal" fixture_health instance-health
  run_stage instance-routes "cold-minimal instance-health" test_method instance-routes testInstanceRoutes 2700 InstanceRouteTests
}
