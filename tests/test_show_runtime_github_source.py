"""The github-source provider must not rebuild a commit it already built.

Every ``vibe runtime prepare`` used to re-run ``npm ci`` and ``npm run build``
against a checkout that ``git fetch`` had just confirmed was unchanged, which
is around forty seconds on each regression update. A marker recording the
commit that produced the artifacts on disk turns that into one shallow fetch.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core import show_runtime


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return result.stdout.strip()


def make_upstream(tmp_path: Path) -> Path:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "-b", "main", cwd=upstream)
    (upstream / "package.json").write_text('{"name": "runtime"}\n', encoding="utf-8")
    (upstream / ".gitignore").write_text("node_modules\npackages/runtime/dist\n", encoding="utf-8")
    git("add", "-A", cwd=upstream)
    git("commit", "-m", "one", cwd=upstream)
    return upstream


def checkout_record(manager: show_runtime.ShowRuntimeManager, source_dir: Path) -> show_runtime._GitHubCheckoutRecord:
    record = manager._read_github_checkout_record(source_dir)
    assert record is not None
    return record


class Harness:
    """A manager wired to a local upstream, with npm simulated and git real."""

    def __init__(self, tmp_path: Path, upstream: Path, *, force_install: bool = False) -> None:
        self.npm_commands: list[list[str]] = []
        self.manager = show_runtime.ShowRuntimeManager(
            runtime_dir=tmp_path / "runtime",
            runtime_source="github-source",
            github_repo=str(upstream),
            github_ref="main",
            force_install=force_install,
        )
        self.manager._run_install_command = self._run  # type: ignore[method-assign]

    def _run(self, command: list[str], *, cwd: Path | None = None) -> bool:
        if Path(command[0]).name == "npm":
            self.npm_commands.append(command)
            if command[1:] == ["run", "build"]:
                cli = Path(cwd or ".") / "packages" / "runtime" / "dist" / "cli.js"
                cli.parent.mkdir(parents=True, exist_ok=True)
                cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            return True
        return subprocess.run(command, cwd=cwd, capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def resolvable_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    real_resolve = show_runtime._resolve_command
    monkeypatch.setattr(show_runtime, "_resolve_node_command", lambda: ["/usr/bin/node"])
    monkeypatch.setattr(
        show_runtime,
        "_resolve_command",
        lambda name: ["/usr/bin/npm"] if name == "npm" else real_resolve(name),
    )


def test_first_install_builds_and_records_the_commit_it_built(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)

    assert harness.manager._install_github_runtime().command

    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]
    source_dir = harness.manager._github_source_dir()
    expected_revision = git("rev-parse", "HEAD", cwd=upstream)
    assert harness.manager._read_github_build_marker(source_dir) == expected_revision
    assert checkout_record(harness.manager, source_dir).revision == expected_revision


def test_unchanged_upstream_reuses_the_build_instead_of_repeating_it(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime().command
    harness.npm_commands.clear()

    command = harness.manager._install_github_runtime().command

    assert command  # the runtime is still usable
    assert harness.npm_commands == []
    assert harness.manager._install_reason is None


def test_a_new_upstream_commit_is_still_picked_up(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime().command
    harness.npm_commands.clear()

    (upstream / "package.json").write_text('{"name": "runtime", "version": "2"}\n', encoding="utf-8")
    git("commit", "-am", "two", cwd=upstream)

    assert harness.manager._install_github_runtime().command

    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]
    source_dir = harness.manager._github_source_dir()
    assert harness.manager._read_github_build_marker(source_dir) == git("rev-parse", "HEAD", cwd=upstream)


def test_force_install_rebuilds_even_when_the_commit_matches(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime().command
    harness.npm_commands.clear()

    assert harness.manager._install_github_runtime(force=True).command

    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]


def test_force_install_refuses_locally_modified_source_without_touching_the_runtime(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    assert installed["ok"] is True
    source_dir = harness.manager._github_source_dir()
    cli_path = Path(installed["command"][1])
    original_cli = cli_path.read_text(encoding="utf-8")
    (source_dir / "package.json").write_text('{"name": "locally-edited"}\n', encoding="utf-8")
    harness.npm_commands.clear()

    result = harness.manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_dirty"
    assert result["status"]["installed"] is True
    assert (source_dir / "package.json").read_text(encoding="utf-8") == '{"name": "locally-edited"}\n'
    assert cli_path.read_text(encoding="utf-8") == original_cli
    assert harness.npm_commands == []


def test_force_install_refuses_local_commits_without_moving_the_checkout_or_runtime(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    assert installed["ok"] is True
    source_dir = harness.manager._github_source_dir()
    cli_path = Path(installed["command"][1])
    original_cli = cli_path.read_text(encoding="utf-8")
    (source_dir / "local.txt").write_text("local commit\n", encoding="utf-8")
    git("add", "local.txt", cwd=source_dir)
    git("commit", "-m", "local work", cwd=source_dir)
    local_revision = git("rev-parse", "HEAD", cwd=source_dir)
    harness.npm_commands.clear()

    result = harness.manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_revision_changed"
    assert result["status"]["installed"] is True
    assert git("rev-parse", "HEAD", cwd=source_dir) == local_revision
    assert (source_dir / "local.txt").read_text(encoding="utf-8") == "local commit\n"
    assert cli_path.read_text(encoding="utf-8") == original_cli
    assert harness.npm_commands == []


def test_automatic_update_builds_local_commit_without_moving_the_checkout(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    installed = Harness(tmp_path, upstream)
    initial = installed.manager.prepare()
    assert initial["ok"] is True
    source_dir = installed.manager._github_source_dir()
    managed_revision = checkout_record(installed.manager, source_dir).revision
    (source_dir / "local.txt").write_text("local commit\n", encoding="utf-8")
    git("add", "local.txt", cwd=source_dir)
    git("commit", "-m", "local work", cwd=source_dir)
    local_revision = git("rev-parse", "HEAD", cwd=source_dir)
    (upstream / "package.json").write_text('{"name": "runtime", "version": "2"}\n', encoding="utf-8")
    git("commit", "-am", "upstream update", cwd=upstream)
    target_revision = git("rev-parse", "HEAD", cwd=upstream)
    automatic = Harness(tmp_path, upstream)

    result = automatic.manager.prepare(automatic=True)

    assert result["ok"] is True
    assert git("rev-parse", "HEAD", cwd=source_dir) == local_revision
    assert checkout_record(automatic.manager, source_dir).revision == managed_revision
    assert automatic.manager._read_github_build_marker(source_dir) is None
    assert [command[1:] for command in automatic.npm_commands] == [["ci"], ["run", "build"]]
    assert result["status"]["reason"] is None
    assert result["status"]["github_source"]["update"] == {
        "state": "skipped",
        "reason": "runtime_github_source_revision_changed",
        "current_revision": local_revision,
        "target_revision": target_revision,
    }

    fresh_status = Harness(tmp_path, upstream).manager.status()
    assert fresh_status["reason"] is None
    assert fresh_status["github_source"]["update"] == result["status"]["github_source"]["update"]

    automatic.manager.status()
    Harness(tmp_path, upstream).manager._clear_github_source_update(source_dir)
    assert automatic.manager.status()["github_source"]["update"] is None


def test_checkout_update_writes_pending_before_moving_head_and_heals_forward(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    source_dir = harness.manager._github_source_dir()
    original_revision = checkout_record(harness.manager, source_dir).revision
    (upstream / "package.json").write_text('{"name": "runtime", "version": "2"}\n', encoding="utf-8")
    git("commit", "-am", "upstream update", cwd=upstream)
    target_revision = git("rev-parse", "HEAD", cwd=upstream)
    updater = Harness(tmp_path, upstream)
    original_write = updater.manager._write_github_checkout_record
    writes: list[show_runtime._GitHubCheckoutRecord] = []

    def fail_normalization(source: Path, record: show_runtime._GitHubCheckoutRecord) -> bool:
        writes.append(record)
        if record.revision == target_revision and record.pending is None:
            return False
        return original_write(source, record)

    monkeypatch.setattr(updater.manager, "_write_github_checkout_record", fail_normalization)

    result = updater.manager.prepare()

    assert result["ok"] is True
    assert result["command"] == installed["command"]
    assert git("rev-parse", "HEAD", cwd=source_dir) == target_revision
    pending = checkout_record(updater.manager, source_dir)
    assert pending == show_runtime._GitHubCheckoutRecord(original_revision, pending=target_revision)
    assert writes[:2] == [
        show_runtime._GitHubCheckoutRecord(original_revision, pending=target_revision),
        show_runtime._GitHubCheckoutRecord(target_revision),
    ]

    fresh = Harness(tmp_path, upstream)
    healed = fresh.manager.prepare()

    assert healed["ok"] is True
    assert checkout_record(fresh.manager, source_dir) == show_runtime._GitHubCheckoutRecord(target_revision)
    assert fresh.npm_commands == []


def test_checkout_update_refuses_before_moving_head_when_pending_record_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    source_dir = harness.manager._github_source_dir()
    original_revision = git("rev-parse", "HEAD", cwd=source_dir)
    original_cli = Path(installed["command"][1]).read_text(encoding="utf-8")
    (upstream / "package.json").write_text('{"name": "runtime", "version": "2"}\n', encoding="utf-8")
    git("commit", "-am", "upstream update", cwd=upstream)
    target_revision = git("rev-parse", "HEAD", cwd=upstream)
    original_write = harness.manager._write_github_checkout_record

    def fail_pending(source: Path, record: show_runtime._GitHubCheckoutRecord) -> bool:
        if record.pending:
            return False
        return original_write(source, record)

    monkeypatch.setattr(harness.manager, "_write_github_checkout_record", fail_pending)
    harness.npm_commands.clear()

    result = harness.manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_update_failed"
    assert result["install"]["state"] == "installed"
    assert result["install"]["reason"] is None
    assert result["status"]["reason"] is None
    assert result["status"]["github_source"]["update"] == {
        "state": "skipped",
        "reason": "runtime_github_source_update_failed",
        "current_revision": original_revision,
        "target_revision": target_revision,
    }
    assert git("rev-parse", "HEAD", cwd=source_dir) == original_revision
    assert checkout_record(harness.manager, source_dir) == show_runtime._GitHubCheckoutRecord(original_revision)
    assert Path(installed["command"][1]).read_text(encoding="utf-8") == original_cli
    assert harness.npm_commands == []


def test_force_install_adopts_a_proven_legacy_checkout_revision(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    assert installed["ok"] is True
    source_dir = harness.manager._github_source_dir()
    harness.manager._github_checkout_marker_path(source_dir).unlink()
    expected_revision = harness.manager._read_github_build_marker(source_dir)
    harness.npm_commands.clear()

    result = harness.manager.prepare(force=True)

    assert result["ok"] is True
    assert expected_revision
    assert checkout_record(harness.manager, source_dir).revision == expected_revision
    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]


def test_force_install_refuses_an_unverified_legacy_checkout_without_touching_the_runtime(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    assert installed["ok"] is True
    source_dir = harness.manager._github_source_dir()
    cli_path = Path(installed["command"][1])
    original_cli = cli_path.read_text(encoding="utf-8")
    harness.manager._github_checkout_marker_path(source_dir).unlink()
    harness.manager._github_build_marker_path(source_dir).unlink()
    original_revision = git("rev-parse", "HEAD", cwd=source_dir)
    harness.npm_commands.clear()

    result = harness.manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_revision_unverified"
    assert result["status"]["installed"] is True
    assert git("rev-parse", "HEAD", cwd=source_dir) == original_revision
    assert cli_path.read_text(encoding="utf-8") == original_cli
    assert harness.npm_commands == []

    explicit = Harness(tmp_path, upstream)
    prepared = explicit.manager.prepare()
    assert prepared["ok"] is True
    assert explicit.manager._read_github_checkout_record(source_dir) is None
    assert prepared["status"]["reason"] is None
    assert prepared["status"]["github_source"]["update"]["state"] == "skipped"
    assert prepared["status"]["github_source"]["update"]["reason"] == "runtime_github_source_revision_unverified"

    still_unverified = Harness(tmp_path, upstream).manager.prepare(force=True)
    assert still_unverified["ok"] is False
    assert still_unverified["reason"] == "runtime_github_source_revision_unverified"

    shutil.rmtree(source_dir)
    recreated = Harness(tmp_path, upstream).manager.prepare()
    assert recreated["ok"] is True
    assert checkout_record(Harness(tmp_path, upstream).manager, source_dir).revision == original_revision

    repaired = Harness(tmp_path, upstream).manager.prepare(force=True)
    assert repaired["ok"] is True
    assert repaired["status"]["github_source"]["update"] is None


def test_force_install_fails_before_build_when_old_output_remains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime().command
    harness.npm_commands.clear()
    monkeypatch.setattr(show_runtime.shutil, "rmtree", lambda _path: None)

    attempt = harness.manager._install_github_runtime(force=True)

    assert attempt.command is None
    assert attempt.operation_reason == "runtime_install_failed"
    assert harness.manager._install_reason is None
    assert harness.npm_commands == []


def test_forced_update_failure_reports_failed_operation_and_installed_state(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    assert installed["ok"] is True

    def fail_update(_command: list[str], *, cwd: Path | None = None) -> bool:
        del cwd
        harness.manager._install_reason = "runtime_install_failed"
        return False

    harness.manager._run_install_command = fail_update  # type: ignore[method-assign]

    result = harness.manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_update_failed"
    assert result["command"] == installed["command"]
    assert result["install"]["state"] == "installed"
    assert result["install"]["reason"] is None
    assert result["status"]["installed"] is True
    assert result["status"]["reason"] is None
    assert result["status"]["github_source"]["update"]["reason"] == "runtime_github_source_update_failed"
    assert result["status"]["command"] == installed["command"]


def test_failed_forced_build_invalidates_the_cached_command_before_retry(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    assert installed["ok"] is True
    real_run = harness._run

    def fail_the_build(command: list[str], *, cwd: Path | None = None) -> bool:
        if command[1:] == ["run", "build"]:
            harness.npm_commands.append(command)
            harness.manager._install_reason = "runtime_install_failed"
            return False
        return real_run(command, cwd=cwd)

    harness.manager._run_install_command = fail_the_build  # type: ignore[method-assign]
    harness.npm_commands.clear()

    forced = harness.manager.prepare(force=True)
    retried = harness.manager.prepare()

    assert forced["ok"] is False
    assert forced["status"]["installed"] is False
    assert retried["ok"] is False
    assert retried["reason"] == "runtime_install_failed"
    assert harness.manager._managed_command is None
    assert [command[1:] for command in harness.npm_commands] == [
        ["ci"],
        ["run", "build"],
        ["ci"],
        ["run", "build"],
    ]


@pytest.mark.parametrize(
    "delegate_outcome",
    (
        subprocess.CompletedProcess(args=["git", "fetch"], returncode=1),
        OSError("git spawn failed"),
        subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=300),
    ),
)
def test_github_delegate_failures_are_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delegate_outcome: subprocess.CompletedProcess | Exception,
) -> None:
    manager = show_runtime.ShowRuntimeManager(runtime_dir=tmp_path / "runtime")
    manager.runtime_dir.mkdir(parents=True)

    def run_delegate(*_args, **_kwargs):
        if isinstance(delegate_outcome, Exception):
            raise delegate_outcome
        return delegate_outcome

    monkeypatch.setattr(show_runtime.subprocess, "run", run_delegate)

    assert manager._run_install_command(["git", "fetch"]) is False
    assert manager._install_reason == "runtime_install_failed"


def test_a_failed_build_leaves_no_marker_licensing_a_skip(tmp_path: Path) -> None:
    """A marker may only ever describe artifacts that exist right now.

    Otherwise a build that died halfway would make every later prepare skip the
    repair it needs.
    """
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    real_run = harness._run

    def fail_the_build(command: list[str], *, cwd: Path | None = None) -> bool:
        if command[1:] == ["run", "build"]:
            harness.npm_commands.append(command)
            return False
        return real_run(command, cwd=cwd)

    harness.manager._run_install_command = fail_the_build  # type: ignore[method-assign]
    assert harness.manager._install_github_runtime().command is None

    source_dir = harness.manager._github_source_dir()
    assert harness.manager._read_github_build_marker(source_dir) is None

    harness.manager._run_install_command = real_run  # type: ignore[method-assign]
    harness.npm_commands.clear()
    assert harness.manager._install_github_runtime().command
    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]


# The complete rebuild sequence, asserted by
# ``test_first_install_builds_and_records_the_commit_it_built``. Each step
# replaces part of the artifact the marker describes, so each one failing must
# leave the marker gone.
@pytest.mark.parametrize("failing_step", [["ci"], ["run", "build"]])
def test_a_failed_rebuild_retracts_the_marker_it_had_already_earned(tmp_path: Path, failing_step: list[str]) -> None:
    """The marker describes the artifact on disk, and a rebuild replaces it.

    A rebuild at the commit already recorded is the case that bites: the marker
    still names that commit while ``npm ci`` has emptied node_modules, so the
    next ordinary prepare would fetch the same commit, match the marker, find
    the stale entry point still resolving, and skip the repair forever.
    """
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime().command
    built = harness.manager._read_github_build_marker(harness.manager._github_source_dir())
    assert built

    real_run = harness._run

    def fail_one_step(command: list[str], *, cwd: Path | None = None) -> bool:
        if command[1:] == failing_step:
            harness.npm_commands.append(command)
            return False
        return real_run(command, cwd=cwd)

    harness.manager._run_install_command = fail_one_step  # type: ignore[method-assign]
    harness.manager.force_install = True
    harness.npm_commands.clear()
    harness.manager._install_github_runtime()

    assert harness.manager._read_github_build_marker(harness.manager._github_source_dir()) is None

    harness.manager._run_install_command = real_run  # type: ignore[method-assign]
    harness.manager.force_install = False
    harness.npm_commands.clear()
    assert harness.manager._install_github_runtime().command
    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]
