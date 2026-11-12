"""Pure tests of the sourced production runner, with external operations faked.

Never execute run-measurement.sh: its setup starts real tools/listeners. The inert
helper owns the one production graph. Only tool/transport boundaries are replaced.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
RUNNER = (HERE / "run-measurement.sh").read_text()
# Reuse actual tool-invocation wrappers too; python3 below intercepts the boundary.
WRAPPERS = RUNNER.split("validate_products() {", 1)[1].split("\nrun_measurement_stages\n", 1)[0]
WRAPPERS = "validate_products() {" + WRAPPERS

FAKES = r'''
record() { printf '%s\n' "$*" >> "$out/calls"; }
fail_code() {
  local pair
  for pair in ${FAILURES:-}; do
    if [[ "${pair%%:*}" == "$1" ]]; then return "${pair#*:}"; fi
  done
  return 0
}
# This fake does not forward any tool invocation supplied to timeout. Real Python
# is used only for the helper's JSON ledger/wait/summary and pure report generator.
python3() {
  if [[ "${1:-}" == "$probe_dir/validate-stage.py" ]]; then
    shift
    local name
    if [[ "$1" == timeout ]]; then
      case "$3 ${4:-} ${5:-}" in
        'xcodebuild build-for-testing '*) name=build-for-testing ;;
        'xcrun simctl bootstatus') name=simulator-ready ;;
        'xcrun simctl install') name=install ;;
        'xcodebuild test-without-building '*)
          local arg
          for arg in "$@"; do
            case "$arg" in
              -only-testing:*/testHostLaunch) name=test-launch ;;
              -only-testing:*/testDirectTLSMinimal) name=direct-minimal ;;
              -only-testing:*/testColdMinimal) name=cold-minimal ;;
              -only-testing:*/testMatrix) name=full-matrix ;;
              -only-testing:*/testInstanceRoutes) name=instance-routes ;;
            esac
          done ;;
        *) record "UNEXPECTED timeout $*"; return 99 ;;
      esac
      record "execute $name"
      fail_code "$name"; return $?
    elif [[ "$1" == bundle ]]; then
      record 'execute product-validation'; fail_code product-validation; return $?
    else
      record "validate $1"; fail_code "validate-$1"; return $?
    fi
  fi
  if [[ "${1:-}" == "$probe_dir/instance_evidence.py" && "${REPORT_FAIL:-0}" != 0 ]]; then
    record 'report failed'; return "$REPORT_FAIL"
  fi
  # Summary writer fault: consume its source, then fail without inventing passed.
  if [[ "${1:-}" == - && "${2:-}" == "$out/summary.json" && "${SUMMARY_FAIL:-0}" != 0 ]]; then
    command "$REAL_PYTHON" -c 'import sys; sys.stdin.read()'
    return "$SUMMARY_FAIL"
  fi
  command "$REAL_PYTHON" "$@"
}
xcrun() { record 'FORBIDDEN xcrun'; return 99; }
xcodebuild() { record 'FORBIDDEN xcodebuild'; return 99; }
node() { record 'FORBIDDEN node'; return 99; }
sudo() { record 'FORBIDDEN sudo'; return 99; }
curl() {
  record "health $stage"
  fail_code "$stage" || return $?
  if [[ "${BAD_HEALTH:-}" == "$stage" ]]; then printf '{"ok":false}\n'; return; fi
  printf '%s\n' '{"ok":true,"cases":27,"lifecycleCases":3,"instanceCases":10,"listening":[443,18443,18444,18080,18082,18083,18090],"originAddresses":["127.0.0.1:443","127.0.0.2:443"]}'
}
sleep() { record 'sleep'; }
tee() {
  if [[ "${TEE_FAIL:-}" == "$*" ]]; then
    command "$REAL_PYTHON" -c 'import sys; sys.stdin.read()'
    return 39
  fi
  command tee "$@"
}
bounded() {
  # Lifecycle's real control flow reaches this fake instead of simctl.
  shift
  if [[ "$1 $2" != 'xcrun simctl' ]]; then record 'UNEXPECTED bounded'; return 99; fi
  local action=$3 case_id='' phase='' flag
  shift 3
  record "lifecycle-tool $action $*"
  [[ "$action" == launch ]] || return 0
  while [[ $# -gt 0 ]]; do
    flag=$1; shift
    case "$flag" in
      --lifecycle-case) case_id=$1; shift ;;
      --lifecycle-phase) phase=$1; shift ;;
    esac
  done
  fail_code "launch-$case_id-$phase" || return $?
  command "$REAL_PYTHON" - "$out/cert/fixture.jsonl" "$case_id" "$phase" <<'PY'
import json, os, sys
path, case, phase = sys.argv[1:]
mode = os.environ.get('LIFECYCLE_MODE', 'pass')
error = mode == 'error-all' or (mode == 'error-prepare' and phase == 'prepare')
if mode != 'missing':
    step = 'lifecycle_error' if error else 'lifecycle_ready' if phase == 'prepare' else 'lifecycle_done'
    with open(path, 'a') as f:
        f.write(json.dumps({'event':'client','case':case,'phase':phase,'step':step,
                            'result':{'error':'routeAPrecondition'} if error else {'newTrafficUsesRouteB':False}})+'\n')
PY
}
cleanup_device() { record 'cleanup device'; }
cleanup_fixture() { record 'cleanup fixture'; }
cleanup_hosts() { record 'cleanup hosts'; }
'''


class RunnerControlTests(unittest.TestCase):
    def run_shell(self, body="run_measurement_stages\nexit \"$test_status\"", events=(), **overrides):
        with tempfile.TemporaryDirectory(prefix="webkit-runner-pure-") as temp:
            out = Path(temp)
            (out / "cert").mkdir()
            (out / "cert/fixture.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
            (out / "stages.jsonl").write_text("")
            (out / "summary.json").write_text('{"status":"running","label":"pure"}')
            env = {**os.environ, "out":str(out), "probe_dir":str(HERE), "REAL_PYTHON":os.sys.executable,
                   "FAILURES":"", "LIFECYCLE_MODE":"pass", "REPORT_FAIL":"0", "SUMMARY_FAIL":"0", "BAD_HEALTH":"", "TEE_FAIL":"", **overrides}
            script = '\n'.join([
                'set -euo pipefail', 'source "$probe_dir/measurement-stages.sh"',
                'stage_results="$out/stages.jsonl"; test_status=0',
                'project=fake; device_id=fake; app=fake; boot_timeout=180', FAKES, WRAPPERS,
                "trap 'finish_measurement \"$?\"' EXIT", body,
            ])
            p = subprocess.run(["/bin/bash", "-c", script], env=env, text=True, capture_output=True, timeout=40)
            calls = (out / "calls").read_text().splitlines() if (out / "calls").exists() else []
            self.assertFalse(any("FORBIDDEN" in c or "UNEXPECTED" in c for c in calls), (calls,p.stderr))
            summary = json.loads((out / "summary.json").read_text())
            partial = json.loads((out / "instance-observations.json").read_text()) if (out / "instance-observations.json").exists() else None
            logs = {path.name:path.read_text() for path in out.glob("*.log")}
            return p, summary, calls, partial, logs

    def test_inert_source(self):
        p = subprocess.run(["/bin/bash", "-c", 'set -eu; source "$1"', "pure", str(HERE / "measurement-stages.sh")],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual((p.returncode,p.stdout,p.stderr),(0,"",""))
        self.assertEqual(RUNNER.count("\nrun_measurement_stages\n"),1)
        self.assertNotIn("blocked_by=",RUNNER)

    def test_complete_fixed_graph_and_exit_partial_report(self):
        p,s,c,partial,_ = self.run_shell()
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(s["status"],"passed")
        self.assertEqual(len(s["stages"]),12)
        self.assertTrue(all(r["status"] == "passed" for r in s["stages"]))
        self.assertLess(c.index("health instance-health"),c.index("execute instance-routes"))
        self.assertEqual(partial["measuredCases"],0)  # Fake execution is never route evidence.
        self.assertEqual(len(partial["cases"]),10)
        self.assertEqual(c[-3:],["cleanup device","cleanup fixture","cleanup hosts"])

    def test_every_shared_gate_failure_blocks_true_dependents(self):
        gates=["build-for-testing","product-validation","simulator-ready","install","test-launch"]
        for i,gate in enumerate(gates):
            with self.subTest(gate=gate):
                p,s,c,partial,_ = self.run_shell(FAILURES=f"{gate}:31")
                self.assertEqual(p.returncode,31,p.stderr)
                self.assertEqual(s["status"],"failed")
                for r in s["stages"][i+1:]: self.assertEqual(r["status"],"skipped",r)
                self.assertFalse(any(x.startswith("health ") or x.startswith("lifecycle-tool ") for x in c))
                self.assertNotIn("execute instance-routes",c)
                self.assertEqual(partial["measuredCases"],0)

    def test_lifecycle_failure_does_not_block_controls_or_instance(self):
        p,s,c,_,logs = self.run_shell(LIFECYCLE_MODE="error-all",FAILURES="validate-lifecycle:66")
        rows={r["stage"]:r for r in s["stages"]}
        self.assertEqual(p.returncode,65)
        self.assertEqual(rows["lifecycle"]["exit"],65)
        self.assertEqual(rows["instance-routes"]["status"],"passed")
        self.assertIn("health controls-health",c)
        self.assertIn("health instance-health",c)
        self.assertIn("validate lifecycle",c)
        self.assertIn("routeAPrecondition",logs["lifecycle.log"])
        self.assertNotIn("sleep",c)
        self.assertFalse(any("--lifecycle-phase resume" in x for x in c))

    def test_control_and_health_failures_block_exact_consumers(self):
        for gate in ("controls-health","direct-minimal","cold-minimal","instance-health"):
            with self.subTest(gate=gate):
                p,s,c,_,_ = self.run_shell(FAILURES=f"{gate}:32")
                rows={r["stage"]:r for r in s["stages"]}
                self.assertEqual(p.returncode,32,p.stderr)
                self.assertEqual(rows["instance-routes"]["status"],"skipped")
                self.assertIn(gate if gate == "instance-health" else "cold-minimal",rows["instance-routes"]["cause"])
                self.assertEqual(rows["full-matrix"]["status"],"passed" if gate == "instance-health" else "skipped")
                self.assertNotIn("execute instance-routes",c)
        p,s,_,_,_ = self.run_shell(BAD_HEALTH="instance-health")
        self.assertNotEqual(p.returncode,0)
        self.assertEqual(next(r for r in s["stages"] if r["stage"] == "instance-health")["status"],"failed")

    def test_matrix_failure_needs_fresh_health_but_does_not_starve_instance(self):
        for health_fails in (False,True):
            with self.subTest(health_fails=health_fails):
                failures="full-matrix:33 validate-full-matrix:66" + (" instance-health:34" if health_fails else "")
                p,s,c,_,_ = self.run_shell(FAILURES=failures)
                rows={r["stage"]:r for r in s["stages"]}
                self.assertEqual(p.returncode,33)
                self.assertEqual(rows["full-matrix"]["exit"],33)
                self.assertEqual(rows["instance-routes"]["status"],"skipped" if health_fails else "passed")
                self.assertLess(c.index("execute full-matrix"),c.index("health instance-health"))

    def test_late_success_multiple_failures_never_clear_first_failure(self):
        p,s,c,_,_ = self.run_shell(LIFECYCLE_MODE="error-all",FAILURES="validate-lifecycle:66 full-matrix:33")
        self.assertEqual(p.returncode,65)
        self.assertEqual(s["exit"],65)
        self.assertEqual([(r["stage"],r["exit"]) for r in s["stages"] if r["status"] == "failed"],[("lifecycle",65),("full-matrix",33)])
        self.assertEqual(s["stages"][-1]["status"],"passed")
        self.assertIn("execute instance-routes",c)

    def test_prepare_failure_never_resumes_and_other_cases_continue(self):
        p,s,c,_,logs = self.run_shell(LIFECYCLE_MODE="error-prepare")
        self.assertEqual(p.returncode,65)
        launches=[x for x in c if x.startswith("lifecycle-tool launch")]
        self.assertEqual(len(launches),3)
        self.assertFalse(any("--lifecycle-phase resume" in x for x in launches))
        self.assertIn("lifecycle-reload exit=0",logs["lifecycle.log"])
        self.assertIn("lifecycle-recreate exit=0",logs["lifecycle.log"])
        self.assertIn("lifecycle-relaunch exit=65",logs["lifecycle.log"])

    def test_positive_false_error_and_missing_wait_are_distinct(self):
        case="lifecycle-relaunch"
        done={"event":"client","case":case,"step":"lifecycle_done","result":{"newTrafficUsesRouteB":False}}
        error={"event":"client","case":case,"step":"lifecycle_error","result":{"error":"routeAPrecondition"}}
        body='if wait_lifecycle_event lifecycle-relaunch lifecycle_done; then s=0; else s=$?; fi\nprintf "%s" "$s" > "$out/wait.log"\nexit "$s"'
        for events,expected in (([done],0),([done,error],65),([],124)):
            with self.subTest(expected=expected):
                p,_,c,_,logs = self.run_shell(body,events=events)
                self.assertEqual(logs["wait.log"],str(expected))
                # Direct wait test deliberately never runs stages: EXIT still marks incomplete.
                self.assertNotEqual(p.returncode,0)
                self.assertEqual(c.count("sleep"),160 if expected == 124 else 0)

    def test_launch_failure_never_waits_or_resumes(self):
        p,s,c,_,logs=self.run_shell(FAILURES="launch-lifecycle-relaunch-prepare:35")
        self.assertEqual(p.returncode,35)
        self.assertFalse(any("--lifecycle-phase resume" in x for x in c))
        self.assertNotIn("sleep",c)
        self.assertIn("lifecycle-relaunch exit=35",logs["lifecycle.log"])

    def test_validator_only_failure_blocks_dependents(self):
        p,s,c,_,_ = self.run_shell(FAILURES="validate-cold-minimal:66")
        self.assertEqual(p.returncode,66)
        rows={r["stage"]:r for r in s["stages"]}
        self.assertEqual(rows["cold-minimal"]["status"],"failed")
        self.assertEqual(rows["instance-routes"]["status"],"skipped")
        self.assertNotIn("health instance-health",c)

    def test_log_write_failure_cannot_be_published_as_stage_pass(self):
        body='TEE_FAIL="$out/full-matrix.log"\nrun_measurement_stages\nexit "$test_status"'
        p,s,c,_,_=self.run_shell(body)
        self.assertEqual(p.returncode,39)
        rows={r["stage"]:r for r in s["stages"]}
        self.assertEqual(rows["full-matrix"]["status"],"failed")
        self.assertEqual(rows["instance-routes"]["status"],"passed")

    def test_first_stage_failure_survives_later_shell_exit(self):
        p,s,c,partial,_=self.run_shell('run_measurement_stages\nexit 41',LIFECYCLE_MODE="error-all")
        self.assertEqual(p.returncode,65)
        self.assertEqual(s["exit"],65)
        self.assertEqual(s["shellExit"],41)
        rows={r["stage"]:r for r in s["stages"]}
        self.assertEqual(rows["lifecycle"]["exit"],65)
        self.assertEqual(rows["runner-exit"]["exit"],41)
        self.assertEqual(rows["instance-routes"]["status"],"passed")
        self.assertIsNotNone(partial)
        self.assertEqual(c[-3:],["cleanup device","cleanup fixture","cleanup hosts"])
        p,s,_,_,_=self.run_shell('run_measurement_stages\nexit 41')
        self.assertEqual((p.returncode,s["exit"],s["shellExit"]),(41,41,41))

    def test_summary_recovers_broken_ledgers_and_stale_states(self):
        corrupt = r'''
run_measurement_stages
python3 - "$out" "$FAULT" "$STALE_STATE" <<'PY'
import json, pathlib, sys
out, fault, stale = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
p = out / 'stages.jsonl'
if fault in ('partial', 'malformed', 'unusable'):
    rows = p.read_text().splitlines(keepends=True)
    bad = '{"stage":' if fault == 'partial' else 'not json\n' if fault == 'malformed' else '[]\n'
    # A malformed middle record must not hide later complete failure rows.
    if fault == 'partial': p.write_text(''.join(rows) + bad)
    else: p.write_text(''.join(rows[:2]) + bad + ''.join(rows[2:]))
elif fault == 'missing': p.unlink()
elif fault == 'unreadable':
    p.unlink()
    p.mkdir()  # Opening a directory as a ledger fails regardless of test uid.
(out / 'summary.json').write_text(json.dumps({'status':stale, 'exit':0, 'label':'pure'}))
PY
exit 43
'''
        for previous_failure in (False,True):
            for stale in ("running","passed"):
                for fault in ("partial","malformed","unusable","missing","unreadable"):
                    with self.subTest(previous_failure=previous_failure,stale=stale,fault=fault):
                        p,s,c,partial,_=self.run_shell(corrupt,FAULT=fault,STALE_STATE=stale,
                                                    LIFECYCLE_MODE="error-all" if previous_failure else "pass")
                        expected=65 if previous_failure else 43
                        self.assertEqual((p.returncode,s["exit"]),(expected,expected),p.stderr)
                        self.assertEqual(s["status"],"failed")
                        self.assertEqual(s["shellExit"],43)
                        self.assertEqual(partial["expectedCases"],10)
                        self.assertEqual(c[-3:],["cleanup device","cleanup fixture","cleanup hosts"])
                        rows={r["stage"]:r for r in s["stages"]}
                        self.assertTrue(rows["summary-finalization"]["ledgerRecovery"])
                        self.assertTrue({"controls-health","instance-health","instance-routes"} <= rows.keys())
                        if fault in ("partial","malformed","unusable"):
                            self.assertEqual(rows["instance-routes"]["status"],"passed")
                            self.assertEqual(rows["lifecycle"]["exit"],65 if previous_failure else 0)
                        if previous_failure:
                            self.assertEqual(rows["runner-exit"]["exit"],43)

    def test_finalization_failure_and_incomplete_exit_remain_nonzero(self):
        for options in ({"REPORT_FAIL":"37"},{"SUMMARY_FAIL":"38"}):
            with self.subTest(options=options):
                p,s,c,partial,_=self.run_shell(**options)
                self.assertEqual(p.returncode,int(next(iter(options.values()))),p.stderr)
                self.assertNotEqual(s["status"],"passed")
                self.assertEqual(c[-3:],["cleanup device","cleanup fixture","cleanup hosts"])
                if "REPORT_FAIL" in options:
                    self.assertIsNone(partial)
                    self.assertEqual(s["stages"][-1]["stage"],"instance-report")
        p,s,_,_,_=self.run_shell(LIFECYCLE_MODE="error-all",SUMMARY_FAIL="38")
        self.assertEqual(p.returncode,65)
        self.assertEqual(s["status"],"failed")
        self.assertEqual(s["exit"],65)
        self.assertEqual(next(r for r in s["stages"] if r["stage"] == "lifecycle")["exit"],65)
        self.assertEqual(s["stages"][-1]["stage"],"summary-finalization")
        p,s,_,partial,_=self.run_shell('exit 41')
        self.assertEqual(p.returncode,41)
        self.assertEqual(s["status"],"failed")
        self.assertEqual(len(partial["cases"]),10)
        names={r["stage"] for r in s["stages"]}
        self.assertTrue({"controls-health","instance-health","instance-routes"} <= names)


if __name__ == "__main__":
    unittest.main()
