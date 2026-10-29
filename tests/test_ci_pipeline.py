from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts.ci_scenario_evidence import pytest_commands


ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())["jobs"]


def _run_isolated_unit_files(
    tmp_path: Path,
    sources: dict[str, str],
    *,
    timeout: str = "15",
    timings: dict[str, float] | None = None,
    lanes: int | None = None,
):
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    for name, source in sources.items():
        (tmp_path / "tests" / name).write_text(source, encoding="utf-8")
    shutil.copyfile(ROOT / "scripts/ci_unit_test_shards.py", tmp_path / "scripts/ci_unit_test_shards.py")
    shutil.copyfile(ROOT / "scripts/ci_pytest_metrics.py", tmp_path / "scripts/ci_pytest_metrics.py")
    if timings is not None:
        (tmp_path / "scripts" / "ci_unit_test_timings.json").write_text(
            json.dumps({"durations_seconds": timings}), encoding="utf-8",
        )
    command = ["bash", str(ROOT / "scripts/ci_unit_tests.sh")]
    if lanes is not None:
        command = [sys.executable, str(ROOT / "scripts/ci_unit_test_lanes.py"), "0", "1", str(lanes)]
    return subprocess.run(
        command, cwd=tmp_path,
        env={**os.environ, "PYTHON": sys.executable, "CI_TEST_FILE_TIMEOUT_SECONDS": timeout,
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=40,
    )


def test_unit_shard_has_an_outer_deadline_without_weakening_failure_propagation() -> None:
    shard = _jobs()["unit-test-shards"]
    assert shard["timeout-minutes"] == 20
    assert shard["strategy"]["fail-fast"] is False
    assert not shard.get("continue-on-error")
    assert all(not step.get("continue-on-error") for step in shard["steps"])


@pytest.mark.parametrize("phase", ["collection", "test", "shutdown"])
def test_unit_file_watchdog_dumps_stacks_and_continues_fail_closed(tmp_path: Path, phase: str) -> None:
    sources = {
        "collection": "import time\ntime.sleep(3600)\n",
        "test": "import time\ndef test_stuck():\n    time.sleep(3600)\n",
        "shutdown": (
            "import threading\n"
            "def test_stuck():\n"
            "    threading.Thread(target=threading.Event().wait).start()\n"
        ),
    }
    result = _run_isolated_unit_files(tmp_path, {
        "test_a_stuck.py": sources[phase],
        "test_b_pass.py": "def test_after_stuck_file():\n    pass\n",
    }, timeout="3")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Starting tests/test_a_stuck.py (timeout 3s)." in result.stdout
    assert "Timeout (0:00:03)!" in result.stderr
    assert "most recent call first" in result.stderr
    assert "Finished tests/test_b_pass.py" in result.stdout
    assert "test_after_stuck_file PASSED" in result.stdout
    assert "FAILED files:\n  tests/test_a_stuck.py" in result.stdout
    assert "All unit test files passed." not in result.stdout
    # The dump names whichever frame the main thread held at the deadline, which
    # in a file whose cost is spread across many tests is an ordinary cheap one.
    # Saying so is the difference between reading the trace as evidence and
    # reading it as a verdict on the test it happens to name.
    assert "^ tests/test_a_stuck.py hit its 3s watchdog." in result.stdout
    assert "No recorded duration for this file, so it ran on the 3s floor" in result.stdout
    assert "not necessarily where it stalled" in result.stdout
    records = _metrics(result)
    assert records[-1]["file"] == "tests/test_b_pass.py"
    if phase == "shutdown":
        assert records[0]["file"] == "tests/test_a_stuck.py"
        assert records[0]["boundary"] == "pytest_returned_before_interpreter_shutdown"
    else:
        assert len(records) == 1


def test_unit_file_watchdog_budget_follows_the_file_own_recorded_cost(tmp_path: Path) -> None:
    """A file the snapshot knows is slow gets headroom measured against itself.

    The fixed budget this replaces could not tell "hung" from "slow": it was
    generous for a 1s file and thin for the suite's slowest, so a uniformly
    slow runner killed the slowest file first and named it as the culprit.
    """
    result = _run_isolated_unit_files(
        tmp_path,
        {
            "test_a_stuck.py": "import time\ndef test_stuck():\n    time.sleep(3600)\n",
            "test_b_pass.py": "def test_after_stuck_file():\n    pass\n",
        },
        timeout="4",
        timings={"tests/test_a_stuck.py": 1.0},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    # 8x its own second, not the 4s floor the same run gives an unknown file.
    assert "Starting tests/test_a_stuck.py (timeout 8s, budgeted from its recorded 1s)." in result.stdout
    assert "Timeout (0:00:08)!" in result.stderr
    assert "^ tests/test_a_stuck.py hit its 8s watchdog." in result.stdout
    assert "That budget is a multiple of this file's own recorded 1s" in result.stdout
    # An unknown file keeps the floor, and the line it prints is unchanged.
    assert "Starting tests/test_b_pass.py (timeout 4s)." in result.stdout
    assert "test_after_stuck_file PASSED" in result.stdout
    assert "FAILED files:\n  tests/test_a_stuck.py" in result.stdout


@pytest.mark.parametrize("failed", [False, True])
def test_unit_file_runner_preserves_assertions_and_integration_selection(tmp_path: Path, failed: bool) -> None:
    result = _run_isolated_unit_files(tmp_path, {
        "test_assertion.py": f"def test_assertion():\n    assert {not failed}\n",
        "test_integration.py": "import pytest\n@pytest.mark.integration\ndef test_excluded():\n    assert False\n",
    })
    assert result.returncode == int(failed), result.stdout + result.stderr
    assert "Ran 2 unit test file(s), one process each" in result.stdout
    assert "No unit tests collected (skipped):\n  tests/test_integration.py" in result.stdout
    records = _metrics(result)
    assert [(row["file"], row["exit_code"]) for row in records] == [
        ("tests/test_assertion.py", int(failed)), ("tests/test_integration.py", 5),
    ]


def test_unit_shard_lanes_run_planned_files_concurrently_and_fail_closed(tmp_path: Path) -> None:
    """Lanes keep one process per file and every failure, and run files at the same time.

    Each barrier file waits for the other's marker: run one after the other, the
    first would time out. The signal file guards the inherited SIGINT disposition
    that shell background jobs would silently turn into "ignored".
    """
    barrier = (
        "import pathlib, time\n"
        f"ROOT = pathlib.Path({str(tmp_path / 'barrier')!r})\n"
        "def test_meets_the_other_lane():\n"
        "    ROOT.mkdir(exist_ok=True)\n"
        "    (ROOT / '{me}').touch()\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not (ROOT / '{other}').exists():\n"
        "        assert time.monotonic() < deadline, 'the other lane never ran alongside'\n"
        "        time.sleep(0.05)\n"
    )
    result = _run_isolated_unit_files(
        tmp_path,
        {
            "test_barrier_a.py": barrier.format(me="a", other="b"),
            "test_barrier_b.py": barrier.format(me="b", other="a"),
            "test_fails.py": "def test_fails():\n    assert False\n",
            "test_signal.py": (
                "import signal\n"
                "def test_sigint_keeps_python_default():\n"
                "    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler\n"
            ),
        },
        timings={
            "tests/test_barrier_a.py": 10.0, "tests/test_barrier_b.py": 10.0,
            "tests/test_fails.py": 1.0, "tests/test_signal.py": 1.0,
        },
        lanes=2,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    finished = [line for line in result.stdout.splitlines() if " Finished tests/" in line]
    assert sorted(line.split()[3] for line in finished) == [
        "tests/test_barrier_a.py", "tests/test_barrier_b.py", "tests/test_fails.py", "tests/test_signal.py",
    ]
    assert {line.split()[1] for line in finished} == {"0]", "1]"}
    assert result.stdout.count("test_meets_the_other_lane PASSED") == 2
    assert "test_sigint_keeps_python_default PASSED" in result.stdout
    assert "FAILED files:\n[lane 0]   tests/test_fails.py" in result.stdout
    assert "Failed lane(s): [0]." in result.stdout


def _metrics(result) -> list[dict]:
    prefix = "CI_TEST_METRICS "
    return [json.loads(line.removeprefix(prefix)) for line in result.stderr.splitlines() if line.startswith(prefix)]


def test_file_metrics_observe_real_phases_and_waited_child_cpu(tmp_path: Path) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_metrics.py": (
        "import subprocess, sys, time, pytest\n"
        "time.sleep(0.03)\n"
        "@pytest.fixture\n"
        "def prepared():\n"
        "    time.sleep(0.03)\n"
        "    yield\n"
        "    time.sleep(0.03)\n"
        "def test_work(prepared):\n"
        "    started = time.process_time()\n"
        "    while time.process_time() - started < 0.03: pass\n"
        "    subprocess.run([sys.executable, '-c', "
        "'import time; start=time.process_time()\\nwhile time.process_time()-start<0.03: pass'], check=True)\n"
    )})
    assert result.returncode == 0, result.stdout + result.stderr
    record, = _metrics(result)
    assert record["schema_version"] == 1
    assert record["boundary"] == "pytest_returned_before_interpreter_shutdown"
    assert record["file"] == "tests/test_metrics.py"
    assert record["exit_code"] == 0
    assert record["phase_counts"] == {"collection": 1, "setup": 1, "call": 1, "teardown": 1}
    assert all(value >= 0.02 for value in record["phase_seconds"].values())
    assert record["outside_phases_seconds"] >= 0
    assert record["wall_seconds"] >= sum(record["phase_seconds"].values())
    observation = record["wait_observation"]
    assert observation["interval"] == "metrics_initialization_to_pytest_return"
    assert 0.02 <= observation["launcher_thread_cpu_seconds"] <= observation["wall_seconds"]
    assert observation["wall_seconds"] <= record["wall_seconds"]
    assert observation["linux_scheduler"]["scope"] == "launcher_thread"
    assert observation["linux_host_pressure"]["scope"] == "host"
    assert "full_seconds" not in observation["linux_host_pressure"]["cpu"]
    if sys.platform != "win32":
        usage = record["process_usage"]
        for scope in ("self", "waited_children"):
            assert usage[scope]["user_cpu_seconds"] + usage[scope]["system_cpu_seconds"] >= 0.02
        assert usage["self"]["peak_rss_bytes"] > 0
    if sys.platform == "linux":
        assert record["linux_proc_io"]["rchar"] > 0
        assert record["cpu_affinity_count"] >= 1
    assert {row["phase"] for row in record["slowest_phases"]} == {"setup", "call", "teardown"}


def test_file_metrics_do_not_double_count_subtests_or_grow_with_test_count(tmp_path: Path) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_subtests.py": (
        "import pytest\n"
        "@pytest.mark.parametrize('case', range(8))\n"
        "def test_subtests(case, subtests):\n"
        "    for item in range(4):\n"
        "        with subtests.test(item=item):\n"
        "            assert item < 4\n"
    )})
    assert result.returncode == 0, result.stdout + result.stderr
    record, = _metrics(result)
    assert record["phase_counts"] == {"collection": 1, "setup": 8, "call": 8, "teardown": 8}
    assert len(record["slowest_phases"]) == 5


def test_file_metrics_do_not_turn_collection_errors_into_success(tmp_path: Path) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_collection.py": "raise RuntimeError('collection failed')\n"})
    assert result.returncode == 1
    record, = _metrics(result)
    assert record["exit_code"] == 2
    assert record["phase_counts"] == {"collection": 1, "setup": 0, "call": 0, "teardown": 0}


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_file_metrics_preserve_fixture_failures(tmp_path: Path, phase: str) -> None:
    setup = "    raise RuntimeError('setup failed')\n" if phase == "setup" else ""
    teardown = "    raise RuntimeError('teardown failed')\n" if phase == "teardown" else ""
    result = _run_isolated_unit_files(tmp_path, {"test_fixture.py": (
        "import pytest\n@pytest.fixture\ndef broken():\n" + setup + "    yield\n" + teardown
        + "def test_fixture(broken): pass\n"
    )})
    assert result.returncode == 1
    record, = _metrics(result)
    assert record["exit_code"] == 1
    assert record["phase_counts"]["call"] == (0 if phase == "setup" else 1)
    assert record["phase_counts"][phase] == 1


def test_file_metrics_mark_unavailable_platform_counters_explicitly(monkeypatch) -> None:
    from scripts import ci_pytest_metrics

    monkeypatch.setattr(ci_pytest_metrics, "resource", None)
    assert ci_pytest_metrics.process_usage() is None

    def denied(_path):
        raise PermissionError("proc is unavailable")

    monkeypatch.setattr(ci_pytest_metrics.Path, "read_text", denied)
    monkeypatch.setattr(ci_pytest_metrics.os, "sched_getaffinity", denied, raising=False)
    assert ci_pytest_metrics.linux_io() is None
    stream = io.StringIO()
    metrics = ci_pytest_metrics.FileMetrics("tests/test_unavailable.py", ci_pytest_metrics.time.perf_counter())
    metrics.emit(stream, 0)
    payload = json.loads(stream.getvalue().removeprefix("CI_TEST_METRICS "))
    assert payload["cpu_affinity_count"] is None
    observation = payload["wait_observation"]
    assert observation["linux_scheduler"]["enabled_at_start"] is None
    assert observation["linux_scheduler"]["runqueue_seconds"] is None
    assert observation["linux_host_pressure"]["io"] == {"some_seconds": None, "full_seconds": None}


@pytest.mark.parametrize("enabled,contents,expected", [
    ("1", "3000000000 7000000000 12", {"enabled": True, "run_ns": 3000000000, "runqueue_ns": 7000000000}),
    ("0", "3 0 12", {"enabled": False, "run_ns": 3, "runqueue_ns": 0}),
    ("unknown", "3 4 12", None),
    ("1", "3 4", None),
    ("1", "3 bad 12", None),
    ("1", "3 -4 12", None),
])
def test_wait_scheduler_parses_only_known_nonnegative_counters(monkeypatch, enabled, contents, expected):
    from scripts import ci_pytest_metrics as metrics

    paths = {
        "/proc/sys/kernel/sched_schedstats": enabled,
        "/proc/thread-self/schedstat": contents,
    }
    monkeypatch.setattr(metrics.Path, "read_text", lambda path: paths[str(path)])
    assert metrics.linux_scheduler() == expected


@pytest.mark.parametrize("resource_name,contents,expected", [
    ("cpu", "some avg10=1.00 avg60=0.00 avg300=0.00 total=123\nfull total=0", {"some": 123}),
    ("io", "some total=123\nfull total=100", {"some": 123, "full": 100}),
    ("memory", "some total=2\nfull total=1", {"some": 2, "full": 1}),
    ("cpu", "some total=0", {"some": 0}),
    ("io", "some total=123", None),
    ("io", "some total=-1\nfull total=0", None),
    ("cpu", "some total=123\nsome total=124", None),
    ("cpu", "some avg10=1.00", None),
    ("cpu", "some total=invalid", None),
    ("cpu", "", None),
])
def test_wait_pressure_has_explicit_host_scope_and_units(monkeypatch, resource_name, contents, expected):
    from scripts import ci_pytest_metrics as metrics

    def read(path):
        assert str(path) == f"/proc/pressure/{resource_name}"
        return contents

    monkeypatch.setattr(metrics.Path, "read_text", read)
    assert metrics.linux_pressure(resource_name) == expected


def _wait_counter_snapshot(*, multiplier=1, thread=7, enabled=True):
    return {
        "wall": 20 * multiplier, "cpu": 2 * multiplier, "thread": thread,
        "scheduler": {"enabled": enabled, "run_ns": 2_000_000_000 * multiplier,
                      "runqueue_ns": 3_000_000_000 * multiplier},
        "pressure": {name: {"some": 9_000_000 * multiplier, "full": 5_000_000 * multiplier}
                     for name in ("cpu", "io", "memory")},
    }


def test_wait_interval_deltas_do_not_mix_thread_and_host_counters():
    from scripts.ci_pytest_metrics import wait_observation

    result = wait_observation(_wait_counter_snapshot(), _wait_counter_snapshot(multiplier=2))
    assert result["wall_seconds"] == 20
    assert result["launcher_thread_cpu_seconds"] == 2
    assert result["linux_scheduler"] == {
        "scope": "launcher_thread", "enabled_at_start": True, "enabled_at_end": True,
        "run_seconds": 2, "runqueue_seconds": 3,
    }
    assert result["linux_host_pressure"] == {
        "scope": "host", "cpu": {"some_seconds": 9},
        "io": {"some_seconds": 9, "full_seconds": 5},
        "memory": {"some_seconds": 9, "full_seconds": 5},
    }


@pytest.mark.parametrize("first_enabled,last_enabled", [(False, False), (False, True), (True, False), (None, True)])
def test_wait_interval_never_treats_disabled_accounting_as_no_wait(first_enabled, last_enabled):
    from scripts.ci_pytest_metrics import wait_observation

    result = wait_observation(_wait_counter_snapshot(enabled=first_enabled),
                              _wait_counter_snapshot(multiplier=2, enabled=last_enabled))
    assert result["linux_scheduler"]["enabled_at_start"] is first_enabled
    assert result["linux_scheduler"]["enabled_at_end"] is last_enabled
    assert result["linux_scheduler"]["runqueue_seconds"] is None
    assert result["linux_scheduler"]["run_seconds"] is None
    assert result["launcher_thread_cpu_seconds"] == 2


def test_wait_interval_rejects_reset_counters_and_changed_thread_identity():
    from scripts.ci_pytest_metrics import wait_observation

    reset = wait_observation(_wait_counter_snapshot(multiplier=2), _wait_counter_snapshot())
    assert reset["linux_scheduler"]["runqueue_seconds"] is None
    assert reset["linux_host_pressure"]["io"]["some_seconds"] is None
    changed = wait_observation(_wait_counter_snapshot(), _wait_counter_snapshot(multiplier=2, thread=8))
    assert changed["launcher_thread_cpu_seconds"] is None
    assert changed["linux_scheduler"]["runqueue_seconds"] is None
    assert changed["linux_host_pressure"]["io"]["some_seconds"] == 9


@pytest.mark.parametrize("timeout", ["0", "00", "-1", "invalid"])
def test_unit_file_runner_rejects_invalid_deadline(tmp_path: Path, timeout: str) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_one.py": "def test_one():\n    pass\n"}, timeout=timeout)
    assert result.returncode == 2
    assert "must be a positive integer" in result.stderr


def test_ui_checks_run_once_without_fencing_artifact_consumers() -> None:
    jobs = _jobs()
    checks = jobs["ui-checks"]
    build = jobs["build-linux-artifacts"]
    assert checks.get("needs") == "changes"
    assert build.get("needs") == "changes"
    assert "ui-checks" in jobs["unit-tests"]["needs"]
    assert "ui-browser-checks" in jobs["unit-tests"]["needs"]
    assert "unit-test-shards" in jobs["unit-tests"]["needs"]
    browsers = jobs["ui-browser-checks"]
    assert browsers.get("needs") == "changes"

    check_steps = [step for step in checks["steps"] if "run" in step]
    build_steps = [step for step in build["steps"] if step.get("working-directory") == "ui"]
    assert all(step.get("working-directory") == "ui" for step in check_steps)
    # The parts run on separate runners: each runs the shared steps with its own
    # commands, and together they run every UI check exactly once.
    shared = "\n".join(step["run"] for step in check_steps).splitlines()
    parts = checks["strategy"]["matrix"]["include"]
    assert checks["strategy"]["fail-fast"] is False
    assert shared.count("${{ matrix.commands }}") == 1
    assert "npm ci" in shared
    check_commands = [line.strip() for part in parts for line in part["commands"].splitlines()]
    build_commands = "\n".join(step["run"] for step in build_steps).splitlines()
    for command in ("npm run validate:theme", "npm run lint", "npm run typecheck:tests", "npm test"):
        assert check_commands.count(command) == 1
        assert command not in build_commands
    assert build_commands.count("npm run build") == 1
    assert "npm ci" in build_commands
    for job in (checks, browsers, build):
        assert job["if"] == "needs.changes.outputs.docs_only == 'false'"
        assert not job.get("continue-on-error")
        assert all(not step.get("if") and not step.get("continue-on-error") for step in job["steps"])
    for name in ("install-upgrade-shards", "windows-install-smoke-runners"):
        assert jobs[name]["needs"] == "build-linux-artifacts"


def test_windows_platform_checks_leave_only_latest_installer_smoke_on_prs() -> None:
    jobs = _jobs()
    assert jobs["windows-model-hub"]["if"] == "github.event_name != 'pull_request'"
    assert jobs["windows-control-ipc"]["needs"] == ["changes", "build-linux-artifacts"]
    assert jobs["windows-control-ipc"]["if"] == (
        "needs.changes.outputs.docs_only == 'false' && "
        "(github.event_name != 'pull_request' || needs.changes.outputs.windows_control_ipc == 'true')"
    )
    skill_step = next(
        step for step in jobs["windows-control-ipc"]["steps"]
        if step.get("name") == "Verify Skill discovery keeps real directory identities"
    )
    assert "tests/test_managed_skills.py" in skill_step["run"]
    assert "zero_directory_entry_identities" in skill_step["run"]
    assert jobs["windows-agent"]["if"] == (
        "needs.changes.outputs.docs_only == 'false' && github.event_name != 'pull_request'"
    )

    smoke = jobs["windows-install-smoke-runners"]
    assert smoke["strategy"]["matrix"]["runner"] == (
        "${{ fromJSON(github.event_name == 'pull_request' && '[\"windows-latest\"]' || '[\"windows-latest\",\"windows-11-arm\"]') }}"
    )
    smoke_steps = {step["name"]: step for step in smoke["steps"] if "name" in step}
    assert "if" not in smoke_steps["Run Windows installer smoke test"]
    assert jobs["windows-install-smoke"]["needs"] == ["changes", "windows-install-smoke-runners"]
    assert jobs["windows-install-smoke"]["if"] == "always()"


def test_install_suites_run_independently_without_losing_checks() -> None:
    jobs = _jobs()
    shards = jobs["install-upgrade-shards"]
    assert shards["strategy"]["fail-fast"] is False
    suites = shards["strategy"]["matrix"]["suite"]
    assert len(suites) == len(set(suites)) == 1
    assert not shards.get("if")
    assert not shards.get("continue-on-error")
    assert all(not step.get("continue-on-error") for step in shards["steps"])
    steps = {step["name"]: step for step in shards["steps"] if "name" in step}
    test_steps = [steps["Run install and upgrade regressions"]]
    for suite in suites:
        selected = [step for step in test_steps if step["if"] == f"matrix.suite == '{suite}'"]
        assert len(selected) == 1, f"Every suite must select exactly one regression command: {suite}"
    # The installer consumes the built core wheel.
    prepare = steps["Prepare Show Runtime manifest for fixture wheels"]
    assert not prepare.get("if")
    assert "python scripts/prepare_local_show_runtime_manifest.py" in prepare["run"]
    for test_file in (
        "tests/test_upgrade_flow.py", "tests/test_install_script.py",
        "tests/e2e/test_install_command.py",
    ):
        assert test_file in test_steps[0]["run"]
    assert "docker info" in test_steps[0]["run"]
    assert jobs["install-upgrade-regression"]["needs"] == ["changes", "install-upgrade-shards"]


def test_distribution_contracts_consume_same_run_artifacts_without_fencing_other_installers() -> None:
    jobs = _jobs()
    build = jobs["build-linux-artifacts"]
    installers = jobs["install-upgrade-shards"]
    uploads = {
        step["with"]["name"]: step
        for step in build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")
    }
    assert uploads["vibe-wheel-linux"]["with"]["path"] == "dist/*.whl"
    companion = uploads["vibe-package-contracts-linux"]
    assert set(companion["with"]["path"].splitlines()) == {
        "dist/*.tar.gz",
    }
    assert companion["with"]["if-no-files-found"] == "error"
    assert not companion.get("if") and not companion.get("continue-on-error")
    downloads = [
        step for step in installers["steps"]
        if step.get("with", {}).get("name") == "vibe-package-contracts-linux"
    ]
    download, = downloads
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["if"] == "matrix.suite == 'installer'"
    assert download["with"] == {"name": "vibe-package-contracts-linux", "path": "dist"}
    assert not download.get("continue-on-error")
    owners = [
        (name, step) for name, job in jobs.items() for step in job["steps"]
        if "tests/test_distribution_artifacts.py" in step.get("run", "")
    ]
    (owner, contracts), = owners
    assert owner == "install-upgrade-shards"
    assert contracts["if"] == "matrix.suite == 'installer'"
    assert installers["strategy"]["matrix"]["suite"].count("installer") == 1
    assert installers["steps"].index(download) < installers["steps"].index(contracts)
    build_environment = next(step["env"] for step in build["steps"] if step.get("name") == "Build package artifact")
    assert contracts["env"] == {"AVIBE_PACKAGE_CONTRACT_VERSION": build_environment["AVIBE_PACKAGE_CONTRACT_VERSION"]}
    assert 'AVIBE_CORE_WHEEL="$(ls dist/avibe_os-*.whl)"' in contracts["run"]
    assert 'AVIBE_CORE_SDIST="$(ls dist/avibe_os-*.tar.gz)"' in contracts["run"]
    assert "pytest tests/test_distribution_artifacts.py -v -ra" in contracts["run"]
    assert not contracts.get("continue-on-error")
    assert jobs["install-upgrade-regression"]["needs"] == ["changes", "install-upgrade-shards"]
    assert jobs["windows-install-smoke-runners"]["needs"] == "build-linux-artifacts"
    # Branch protection requires `windows-install-smoke`; the per-runner matrix
    # reports under other names, so the required name gates on all of them.
    assert jobs["windows-install-smoke"]["needs"] == ["changes", "windows-install-smoke-runners"]
    assert jobs["windows-install-smoke"]["if"] == "always()"


@pytest.mark.parametrize(("pytest_exit", "summary"), [(0, "21 passed"), (1, "1 failed"), (0, "14 passed, 7 skipped")])
def test_distribution_contract_command_fails_on_errors_and_skips(tmp_path, pytest_exit, summary):
    for directory, filename in (("dist", "avibe_os-test.whl"), ("dist", "avibe_os-test.tar.gz")):
        (tmp_path / directory).mkdir(exist_ok=True)
        (tmp_path / directory / filename).touch()
    binary = tmp_path / "bin"
    binary.mkdir()
    pytest_stub = binary / "pytest"
    pytest_stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{summary}'\nexit {pytest_exit}\n")
    pytest_stub.chmod(0o755)
    step = next(
        step for step in _jobs()["install-upgrade-shards"]["steps"]
        if step.get("name") == "Verify built distribution contracts"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=tmp_path,
        env={**os.environ, "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path)},
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) == (pytest_exit == 0 and "skipped" not in summary), result.stdout + result.stderr


def test_hosted_wire_gate_judges_a_pull_requests_engine_change_and_blocks_merge(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    jobs = workflow["jobs"]
    job = jobs["model-hub-hosted-wire"]
    # A failure reaches branch protection through the required aggregate.
    assert "model-hub-hosted-wire" in jobs["unit-tests"]["needs"]
    # Labeling re-runs the gate, which reads the labels as they are when it runs.
    assert {"labeled", "unlabeled"} <= set(workflow[True]["pull_request"]["types"])
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert job["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    steps = job["steps"]
    detect = next(index for index, step in enumerate(steps) if step.get("id") == "manifest")
    for step in steps[detect + 1:]:
        assert "steps.manifest.outputs.changed == 'true'" in step["if"], step
    gate, = [step for step in steps if "scripts/model_hub_hosted_wire_gate.py" in step.get("run", "")]
    assert '--base "$BASE_SHA"' in gate["run"] and '--pull-request "$PR_NUMBER"' in gate["run"]
    # The downloaded engines and the cases run where only loopback answers.
    assert gate["run"].split().count("--loopback-only") == 1
    assert gate["env"] == {"GH_TOKEN": "${{ github.token }}", "PR_NUMBER": "${{ github.event.pull_request.number }}"}

    repo = tmp_path / "repo"
    manifest = repo / "vibe/model_hub_runtime/cliproxyapi_manifest.json"
    manifest.parent.mkdir(parents=True)

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit() -> str:
        git("add", "-A")
        git("-c", "user.name=CI Fixture", "-c", "user.email=ci@example.invalid", "commit", "-qm", "fixture")
        return git("rev-parse", "HEAD")

    def changed(base: str, head: str, event: str = "pull_request") -> tuple[int, str]:
        git("checkout", "-q", head)
        output = tmp_path / "output"
        output.write_text("")
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", steps[detect]["run"]], cwd=repo,
            env={**os.environ, "BASE_SHA": base, "GITHUB_EVENT_NAME": event, "GITHUB_OUTPUT": str(output)},
            capture_output=True, text=True,
        )
        return result.returncode, output.read_text()

    git("init", "-q")
    manifest.write_text('{"asset_release_tag": "model-hub-engine-v8.0.23-3"}\n')
    base = commit()
    (repo / "README.md").write_text("unrelated\n")
    unrelated = commit()
    manifest.write_text('{"asset_release_tag": "model-hub-engine-v8.0.28-1"}\n')
    engine = commit()

    assert changed(base, unrelated) == (0, "changed=false\n")
    assert changed(base, engine) == (0, "changed=true\n")
    # The label lives on the pull request, so a push is not judged again.
    assert changed("", engine, event="push") == (0, "changed=false\n")
    # An unreachable or missing base fails the job; it never reads as "unchanged".
    for missing in ("0" * 40, ""):
        status, output = changed(missing, engine)
        assert status != 0 and output == ""


def test_every_linux_engine_e2e_runner_has_only_loopback() -> None:
    """Hosted and engine E2E cases start a real engine; on Linux only the namespace keeps it off the network."""
    release = yaml.safe_load((ROOT / ".github/workflows/model-hub-e2e.yml").read_text())
    runs = [step["run"] for job in release["jobs"].values() for step in job["steps"] if "e2e_model_hub" in step.get("run", "")]
    assert runs, "the release E2E runs no engine scenarios"
    for run in runs:
        assert run.lstrip().startswith("bash scripts/ci_loopback_only.sh python -m pytest -m e2e_model_hub"), run
    gate, = [step for step in _jobs()["model-hub-hosted-wire"]["steps"]
             if "scripts/model_hub_hosted_wire_gate.py" in step.get("run", "")]
    assert "--loopback-only" in gate["run"].split()


# How GitHub runs a step's script on Linux: the default shell has no pipefail;
# `shell: bash` adds it (docs: "Defaults: bash -e {0}" vs "bash --noprofile --norc -eo pipefail {0}").
_GITHUB_LINUX_SHELLS = {
    None: ["bash", "-e"],
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
}


def _workflow_steps():
    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        data = yaml.safe_load(workflow.read_text())
        default = ((data.get("defaults") or {}).get("run") or {}).get("shell")
        for job_id, job in (data.get("jobs") or {}).items():
            job_default = ((job.get("defaults") or {}).get("run") or {}).get("shell") or default
            for step in job.get("steps") or []:
                yield workflow.name, job_id, step, step.get("shell") or job_default


def test_steps_that_tee_a_command_keep_its_exit_status() -> None:
    """A `cmd | tee log` pipeline exits with tee's status unless pipefail is on, hiding cmd's failure."""

    unguarded = [
        f"{workflow} {job_id}: {step.get('name')}"
        for workflow, job_id, step, shell in _workflow_steps()
        if "| tee" in (step.get("run") or "")
        and shell != "bash"
        and "pipefail" not in (step.get("run") or "")
    ]
    assert unguarded == []


@pytest.mark.parametrize(
    ("pytest_exit", "evidence_exit", "expected"),
    [(0, 0, 0), (1, 0, 1), (0, 1, 1), (2, 1, 2)],
    ids=["green", "pytest-failed", "evidence-failed", "both-failed"],
)
def test_release_e2e_gate_fails_when_its_tests_or_evidence_fail(
    tmp_path: Path, pytest_exit: int, evidence_exit: int, expected: int,
) -> None:
    """The release E2E step, run as GitHub runs it, exits with pytest's failure, else the gate's.

    Its pytest output goes through `| tee`; before the step ran with pipefail every
    failing release E2E run (three cases from 2026-10-27) still concluded success.
    """

    release = yaml.safe_load((ROOT / ".github/workflows/model-hub-e2e.yml").read_text())
    step, = [step for job in release["jobs"].values() for step in job["steps"]
             if "e2e_model_hub" in step.get("run", "")]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ci_loopback_only.sh").write_text(
        f'echo "pytest ran: $*"\nexit {pytest_exit}\n', encoding="utf-8",
    )
    (tmp_path / "scripts" / "ci_scenario_evidence.py").write_text(
        f"raise SystemExit({evidence_exit})\n", encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")

    result = subprocess.run(
        [*_GITHUB_LINUX_SHELLS[step.get("shell")], str(script)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path)},
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == expected, result.stdout + result.stderr
    assert "pytest ran: python -m pytest -m e2e_model_hub" in (tmp_path / "model-hub-e2e.log").read_text()


@pytest.mark.parametrize("gate_name", ["unit-tests", "install-upgrade-regression", "windows-install-smoke"])
def test_required_gates_accept_skips_only_for_proven_docs_and_fail_closed(tmp_path: Path, gate_name: str) -> None:
    gate = _jobs()[gate_name]
    assert gate["if"] == "always()"
    assert "changes" in gate["needs"]
    step, = gate["steps"]
    assert step["env"]["SCOPE_RESULT"] == "${{ needs.changes.result }}"
    assert step["env"]["DOCS_ONLY"] == "${{ needs.changes.outputs.docs_only }}"
    producer_env = {name: value for name, value in step["env"].items()
                    if name not in {"SCOPE_RESULT", "DOCS_ONLY"}}
    producers = set(gate["needs"]) - {"changes"}
    assert set(producer_env.values()) == {f"${{{{ needs['{name}'].result }}}}" for name in producers}

    cases = []
    for docs_only, expected in (("false", "success"), ("true", "skipped")):
        good = {"SCOPE_RESULT": "success", "DOCS_ONLY": docs_only,
                **dict.fromkeys(producer_env, expected)}
        cases.append((good, True))
        # Each producer must match the classified scope, independently.
        for name in producer_env:
            for outcome in ("success", "failure", "cancelled", "skipped", ""):
                if outcome != expected:
                    cases.append(({**good, name: outcome}, False))
        for scope_result in ("failure", "cancelled", "skipped", ""):
            cases.append(({**good, "SCOPE_RESULT": scope_result}, False))
        for invalid in ("", "TRUE", "unknown"):
            cases.append(({**good, "DOCS_ONLY": invalid}, False))
    for results, passes in cases:
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]], cwd=tmp_path,
            env={**os.environ, **results}, capture_output=True, text=True, check=False,
        )
        assert (result.returncode == 0) == passes, (results, result.stdout, result.stderr)


WEB_FETCH_TESTS = (
    "-m pytest -q tests/test_model_hub_fetch_policy.py tests/test_model_hub_fetch_transport.py "
    "tests/test_model_hub_fetch_tls.py tests/test_model_hub_local_fetch.py "
    "tests/test_desktop_runtime_bundle_builder.py -k not packaged_cli_survives"
)


@pytest.mark.parametrize("failing", [(), ("3.11", "3.13")])
def test_web_fetch_runtime_verifies_every_python_in_its_own_interpreter(tmp_path: Path, failing: tuple) -> None:
    """One ubuntu job covers all five Pythons; every version runs, and each failure names its version."""
    job = _jobs()["web-fetch-runtime"]
    legs = job["strategy"]["matrix"]["include"]
    assert job["strategy"]["fail-fast"] is False
    assert [leg["os"] for leg in legs].count("ubuntu-latest") == 1
    assert {(leg["os"], version) for leg in legs for version in leg["python"].split()} == {
        *(("ubuntu-latest", version) for version in ("3.10", "3.11", "3.12", "3.13", "3.14")),
        *((runner, "3.12") for runner in ("ubuntu-24.04-arm", "macos-15-intel", "macos-15", "windows-latest")),
    }
    step, = [step for step in job["steps"] if "PYTHON_VERSIONS" in step.get("env", {})]
    assert step["env"]["PYTHON_VERSIONS"] == "${{ matrix.python }}"
    # The evidence gate reads what this job selects from the workflow text.
    assert pytest_commands(ROOT / ".github/workflows/lint.yml")["web-fetch-runtime"] == [(
        "tests/test_model_hub_fetch_policy.py", "tests/test_model_hub_fetch_transport.py",
        "tests/test_model_hub_fetch_tls.py", "tests/test_model_hub_local_fetch.py",
        "tests/test_desktop_runtime_bundle_builder.py", "-k", "not packaged_cli_survives",
    )]

    # Stub uv and one interpreter per version; record every call they receive.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    (bin_dir / "uv").write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "python find" ]; then for last; do :; done; echo "$STUB_BIN/python$last"; exit 0; fi\n'
        f'echo "uv $*" >> {calls}\n'
    )
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        (bin_dir / f"python{version}").write_text(
            "#!/bin/sh\n"
            f'echo "python{version} $*" >> {calls}\n'
            f'if [ "$2" = pytest ] && [ "{version in failing}" = True ]; then exit 1; fi\n'
        )
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "avibe_os-1-py3-none-any.whl").touch()

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "STUB_BIN": str(bin_dir),
             "PYTHON_VERSIONS": legs[0]["python"]},
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == (1 if failing else 0), result.stdout + result.stderr
    log = calls.read_text().splitlines()
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        interpreter = f"{bin_dir}/python{version}"
        assert log.count(f"python{version} {WEB_FETCH_TESTS}") == 1
        assert log.count(
            f"uv pip install --system --python {interpreter} --require-hashes --only-binary=:all: "
            "--no-binary claude-agent-sdk --no-binary http-ece -r fetch-requirements.txt"
        ) == 1
        assert log.count(f"uv pip install --system --python {interpreter} --no-deps dist/avibe_os-1-py3-none-any.whl") == 1
        assert log.count(f"uv pip install --system --python {interpreter} pytest pytest-asyncio jsonschema") == 1
        named = f"::error::web-fetch-runtime failed on Python {version}" in result.stdout
        assert named == (version in failing)
    if failing:
        assert "web-fetch-runtime failed on Python: 3.11 3.13" in result.stdout


def test_lint_replaces_only_the_same_pr_or_branch_and_runs_no_release_listener() -> None:
    # A shared global group would cancel unrelated PRs; master must cancel its
    # own obsolete full run. Trigger scope is part of that scheduling contract.
    workflow = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": True,
    }
    assert set(workflow[True]) == {"pull_request", "push"}
    assert workflow[True]["push"]["branches"] == ["master"]


def test_docs_scope_gates_all_work_without_filtering_away_required_checks() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())
    events = workflow[True]  # PyYAML YAML 1.1 reads the Actions `on` key as True.
    # Default activity types plus the labeling that re-runs the hosted wire gate.
    assert events["pull_request"] == {"types": ["opened", "synchronize", "reopened", "labeled", "unlabeled"]}
    assert not any(key.startswith("paths") for key in events["push"])
    jobs = workflow["jobs"]
    required = {"ruff", "unit-tests", "show-router-integration", "memory-insight-contract",
                "migration-release-guard", "npm-wrapper", "build-linux-artifacts",
                "install-upgrade-regression", "windows-install-smoke"}
    assert required <= jobs.keys()
    gates = {"unit-tests", "install-upgrade-regression", "windows-install-smoke"}
    for name, job in jobs.items():
        if name == "changes" or name in gates:
            continue
        # Every expensive root depends on scope; artifact consumers may depend
        # on the gated producer. No new job can silently evade docs selection.
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        if "changes" in needs:
            assert "needs.changes.outputs.docs_only == 'false'" in job["if"]
        else:
            assert needs == ["build-linux-artifacts"], name
            assert "always()" not in job.get("if", "")
        assert not job.get("continue-on-error")
    scope = jobs["changes"]
    assert scope["outputs"]["docs_only"] == "${{ steps.scope.outputs.docs_only }}"
    assert scope["outputs"]["windows_control_ipc"] == "${{ steps.scope.outputs.windows_control_ipc }}"
    assert not scope.get("if") and not scope.get("continue-on-error")
    command, = [s for s in scope["steps"] if s.get("id") == "scope"]
    assert command["run"] == "python3 scripts/ci_docs_only.py"
    assert not command.get("continue-on-error")
    guidance, = [s for s in scope["steps"] if "test_repository_guidance.py" in s.get("run", "")]
    assert guidance["if"] == "steps.scope.outputs.docs_only == 'true'"
    assert not guidance.get("continue-on-error")
    assert "--noconftest" in guidance["run"]
