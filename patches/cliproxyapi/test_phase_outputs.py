"""Real verifier phase/output consumers with only compiler/wire processes faked."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from budgets import PHASES
from fixture import source_digest
from test_execution_inputs import fake_go
import verify


@pytest.fixture
def phases(tmp_path, monkeypatch):
    toolchain, archive, pin = fake_go(tmp_path, monkeypatch)
    context = SimpleNamespace(
        root=tmp_path, state=tmp_path / "cache-state", source=tmp_path / "source",
        fixture=tmp_path / "fixture", recipe=tmp_path / "recipe", calls=[],
        fail_at=None, timeout_at=None, elapsed=0, before_execution=None, after_execution=None,
    )
    for path in (context.source, context.state, context.fixture, context.recipe, tmp_path / "runs"):
        path.mkdir()
    (context.fixture / "uv.lock").write_text("test-frozen-lock")
    (context.fixture / "pyproject.toml").write_text("test-frozen-project")
    (context.recipe / "prerequisites.json").write_text(json.dumps({
        "linux_arm64_go": pin, "linux_arm64_uv": {"version": "setup-only", "sha256": "test-only"},
    }))
    (context.source / "main.go").write_text("fake engine source; no Go process runs")
    receipt = {
        "source_sha": "test-base", "sha256": {"catalog": "test-catalog"},
        "avibe_fixture_base": "test-fixture", "avibe_fixture_tree": "test-tree",
        "avibe_fixture_archive_sha256": "test-archive",
        "avibe_fixture_source_sha256": source_digest(context.fixture),
    }
    monkeypatch.setattr(verify, "HERE", context.recipe)
    monkeypatch.setattr(verify, "verify_inputs", lambda source: receipt)
    monkeypatch.setattr(verify, "verify_candidate", lambda source: "test-patch")
    monkeypatch.setattr(verify, "python_identity", lambda *_: {"trust": "test-only trusted base"})
    monkeypatch.setattr(verify.shutil, "which", lambda _name: str(toolchain / "bin/go"))

    def version(command, **kwargs):
        assert command == [str(toolchain / "bin/go"), "version"]
        assert kwargs["env"]["GOTOOLCHAIN"] == "local"
        assert kwargs["timeout"] == PHASES[context.phase].input_seconds
        return "go version go1.26.4 linux/arm64\n"

    monkeypatch.setattr(verify.subprocess, "check_output", version)

    def command(command, **kwargs):
        if command[0] == "git":
            assert "--reverse" in command and "--check" in command
            return subprocess.CompletedProcess(command, 0)
        assert kwargs["close_fds"] and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == PHASES[context.phase].command_seconds
        context.calls.append([str(part) for part in command])
        context.elapsed += kwargs["timeout"] - 0.01
        if context.before_execution:
            context.before_execution()
        kwargs["stdout"].write(b"exclusive fake command diagnostics\n")
        if str(command[0]) == str(toolchain / "bin/go") and command[1] == "build":
            Path(command[command.index("-o") + 1]).write_bytes(b"fake exact diagnostic binary")
        elif len(command) > 1 and str(command[1]).endswith("wire_matrix.py"):
            wire = Path(command[command.index("--state") + 1])
            wire.mkdir()
            for name in ("matrix", "integration", "lifecycle"):
                (wire / (name + ".json")).write_text(json.dumps({"fake": name}))
        if context.after_execution:
            context.after_execution()
        if context.timeout_at == len(context.calls):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 9 if context.fail_at == len(context.calls) else 0)

    monkeypatch.setattr(verify.subprocess, "run", command)

    def invoke(phase, name, *, build=None, existing=False):
        context.phase = phase
        output = tmp_path / "runs" / name
        if not existing:
            output.mkdir()
        proof = {
            "phase": phase, "budget": PHASES[phase].receipt(),
            "receipt": name, "source": str(context.source), "state": str(context.state),
            "fixture": str(context.fixture), "recipe": str(context.recipe), "output": str(output),
            "toolchain": str(toolchain), "go_archive": str(archive), "python_env": "test-venv",
            "network": "none" if phase == "build" else "loopback",
            "selected_build": None if build is None else {
                "output": str(build), "receipt": build.name, "parent_receipt_sha256": "test-parent-receipt",
            },
        }
        monkeypatch.setattr(verify, "namespace_receipt", lambda: proof)
        argv = ["verify.py", phase, "--source", str(context.source), "--state", str(context.state),
                "--fixture", str(context.fixture)]
        if build is not None:
            argv += ["--build", str(build)]
        monkeypatch.setattr(sys, "argv", argv)
        context.output, context.proof = output, proof
        verify.main()
        return output

    context.invoke, context.toolchain, context.archive = invoke, toolchain, archive
    return context


@pytest.mark.parametrize("phase", ["test", "build", "wire"])
def test_repeat_actual_phase_never_replaces_previous_evidence(phases, phase):
    build = phases.invoke("build", "selected-build.json") if phase == "wire" else None
    first = phases.invoke(phase, "first-" + phase + ".json", build=build)
    before = source_digest(first)
    build_before = source_digest(build) if build else None
    second = phases.invoke(phase, "second-" + phase + ".json", build=build)
    assert first != second and source_digest(first) == before
    if build:
        assert source_digest(build) == build_before
        result = json.loads((second / "wire.json").read_text())
        assert result["selected_build"]["receipt"] == build.name
        assert result["selected_build"]["parent_receipt_sha256"] == "test-parent-receipt"
    for output in (first, second):
        assert json.loads((output / "inputs-before.json").read_text()) == json.loads((output / "inputs-after.json").read_text())
    assert not (phases.state / "build.json").exists()
    assert not (phases.state / "bin").exists()


@pytest.mark.parametrize("phase", ["test", "build", "wire"])
def test_same_invocation_refuses_before_overwriting_any_file(phases, phase):
    build = phases.invoke("build", "selected-build.json") if phase == "wire" else None
    output = phases.invoke(phase, "one-only.json", build=build)
    before, calls = source_digest(output), len(phases.calls)
    with pytest.raises(FileExistsError):
        phases.invoke(phase, "one-only.json", build=build, existing=True)
    assert source_digest(output) == before and len(phases.calls) == calls


@pytest.mark.parametrize("phase", ["test", "build", "wire"])
@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_partial_failure_preserves_prior_run_and_current_diagnostics(phases, phase, failure):
    build = phases.invoke("build", "selected-build.json") if phase == "wire" else None
    previous = phases.invoke(phase, "previous.json", build=build)
    before = source_digest(previous)
    if failure == "nonzero":
        phases.fail_at = len(phases.calls) + 1
    else:
        phases.timeout_at = len(phases.calls) + 1
    with pytest.raises((SystemExit, subprocess.TimeoutExpired)):
        phases.invoke(phase, "failed.json", build=build)
    assert source_digest(previous) == before
    assert (phases.output / (phase + "-0.log")).read_bytes() == b"exclusive fake command diagnostics\n"
    assert (phases.output / "inputs-after.json").is_file()
    assert not (phases.output / "build.json").exists()
    assert not (phases.output / "wire.json").exists()


@pytest.mark.parametrize("mutation", ["binary", "log", "receipt", "stale-recipe", "bin-link"])
def test_wire_rejects_stale_or_substituted_selected_build(phases, mutation):
    build = phases.invoke("build", "selected-build.json")
    if mutation == "binary":
        (build / "bin/cli-proxy-api").write_bytes(b"substituted")
    elif mutation == "log":
        (build / "build-0.log").write_bytes(b"lost old evidence")
    elif mutation == "receipt":
        value = json.loads((build / "build.json").read_text())
        value["invocation"] = "another-parent.json"
        (build / "build.json").write_text(json.dumps(value))
    elif mutation == "stale-recipe":
        (phases.recipe / "changed.py").write_text("changed recipe")
    else:
        (build / "bin").rename(build / "old-bin")
        (build / "bin").symlink_to(build / "old-bin")
    before, calls = source_digest(build), len(phases.calls)
    with pytest.raises(RuntimeError):
        phases.invoke("wire", "refused-wire.json", build=build)
    assert len(phases.calls) == calls and source_digest(build) == before


def test_substituted_same_version_compiler_refuses_before_any_go_execution(phases):
    (phases.toolchain / "bin/go").write_bytes(b"substitute that claims go1.26.4")
    with pytest.raises(RuntimeError, match="extraction"):
        phases.invoke("build", "refused-compiler.json")
    assert not phases.calls and not (phases.output / "bin").exists()


def test_inputs_are_rechecked_after_failed_command(phases):
    phases.fail_at = 1
    phases.after_execution = lambda: (phases.toolchain / "src/runtime/runtime.go").write_bytes(b"changed runtime")
    with pytest.raises(RuntimeError, match="extraction"):
        phases.invoke("build", "changed-inputs.json")
    assert (phases.output / "build-0.log").is_file()
    assert not (phases.output / "build.json").exists()


def test_near_limit_sequential_commands_fit_parent_and_driver_budget(phases):
    phases.invoke("test", "three-near-deadlines.json")
    budget = PHASES["test"]
    assert len(phases.calls) == budget.commands == 3
    assert phases.elapsed > 1799
    assert phases.elapsed < budget.candidate_seconds < budget.namespace_seconds < budget.driver_seconds
    assert budget.namespace_seconds - budget.candidate_seconds >= budget.preflight_seconds + budget.cleanup_seconds


def test_later_command_log_collision_is_not_truncated(phases):
    def collision():
        if len(phases.calls) == 1:
            (phases.output / "test-1.log").write_bytes(b"preexisting collision")

    phases.after_execution = collision
    with pytest.raises(FileExistsError):
        phases.invoke("test", "log-collision.json")
    assert len(phases.calls) == 1
    assert (phases.output / "test-1.log").read_bytes() == b"preexisting collision"


@pytest.mark.parametrize("phase,network", [("test", "loopback"), ("build", "none"), ("wire", "loopback")])
def test_documented_outer_caller_uses_the_same_complete_budget(tmp_path, monkeypatch, phase, network):
    # The maintained README is an executable caller, not another timeout owner.
    readme = (Path(__file__).parent / "README.md").read_text()
    caller = readme.split("run_phase() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY\n}", 1)[0]
    build = str(tmp_path / "runs/prior-build.json") if phase == "wire" else ""
    monkeypatch.setattr(sys, "argv", ["-", str(tmp_path), str(tmp_path / "fixture"),
                                     phase, network, "one-phase.json", build])
    monkeypatch.setattr(sys, "path", list(sys.path))
    calls = []

    def observed(command, **kwargs):
        calls.append(command)
        assert kwargs == {"check": True, "close_fds": True, "timeout": PHASES[phase].driver_seconds}
        assert command[command.index("--phase") + 1] == phase
        assert command[command.index("--network") + 1] == network
        if build:
            assert command.count("--build") == 2
            assert command[command.index("--build") + 1] == build
        else:
            assert "--build" not in command
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", observed)
    exec(compile(caller, "<maintained README caller>", "exec"), {})
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["test", "build"])
def test_documented_phase_sequence_stops_at_first_failure(failure):
    readme = (Path(__file__).parent / "README.md").read_text()
    sequence = readme.split("run_phase test loopback test-unique.json", 1)[1].split("\n```", 1)[0]
    sequence = "run_phase test loopback test-unique.json" + sequence
    # Only the harmless function below runs; no recipe/Go/sudo process exists.
    command = "run_phase() { printf '%s\\n' \"$1\"; test \"$1\" != \"$failure\"; }\n" + sequence
    result = subprocess.run(
        ["/bin/bash", "-c", command], env={"PATH": "/usr/bin:/bin", "failure": failure},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, close_fds=True, timeout=3,
    )
    assert result.returncode != 0
    assert result.stdout.splitlines() == (["test"] if failure == "test" else ["test", "build"])
