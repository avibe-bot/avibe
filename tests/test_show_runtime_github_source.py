"""The github-source provider builds replacements without mutating published bytes.

Each update is cloned and built in a same-parent staging directory. A marker
recording the commit that produced the published artifacts lets an unchanged
clone be discarded before ``npm ci`` instead of rebuilding the same revision.
"""

from __future__ import annotations

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
def resolvable_toolchain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
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
    source_status = harness.manager.status()["github_source"]
    assert source_status["built_revision"] == expected_revision
    assert source_status["managed_revision"] == expected_revision
    assert source_status["ownership"] == "proven_managed"
    assert source_status["destruction_safe"] is True


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


def test_tracked_checkout_change_refuses_replacement_but_preserves_installed_runtime(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    installed = harness.manager.prepare()
    source_dir = harness.manager._github_source_dir()
    (source_dir / "package.json").write_text('{"name":"locally-edited"}\n', encoding="utf-8")

    result = harness.manager.prepare(force=True)

    assert result["ok"] is False
    assert result["reason"] == "runtime_github_source_dirty"
    assert result["command"] == installed["command"]
    assert result["status"]["install"]["state"] == "installed"
    assert result["status"]["github_source"]["ownership"] == "proven_managed"
    assert result["status"]["github_source"]["destruction_safe"] is False
    assert result["status"]["github_source"]["blocking_paths"] == ["package.json"]
    assert Path(installed["command"][-1]).exists()


def test_first_install_never_publishes_checkout_without_creation_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    real_record = harness.manager._published_bytes_owner.record_github_checkout

    def fail_published_record(*args, **kwargs):
        if kwargs.get("revision") is not None:
            return False
        return real_record(*args, **kwargs)

    monkeypatch.setattr(
        harness.manager._published_bytes_owner,
        "record_github_checkout",
        fail_published_record,
    )

    attempt = harness.manager._install_github_runtime()

    assert attempt.command is None
    assert attempt.operation_reason == "runtime_github_source_update_failed"
    assert not harness.manager._github_source_dir().exists()


def test_force_install_fails_to_publish_when_old_checkout_cannot_be_removed(
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
    assert attempt.operation_reason == "runtime_github_source_update_failed"
    assert harness.manager._install_reason is None
    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]


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
    assert result["status"]["install"]["state"] == "installed"
    assert result["status"]["reason"] is None
    assert result["status"]["command"] == installed["command"]


def test_failed_github_build_does_not_publish_checkout_revision_as_artifact_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vibe import cli

    upstream = make_upstream(tmp_path)
    installed = Harness(tmp_path, upstream)
    first = installed.manager.prepare()
    source_dir = installed.manager._github_source_dir()
    old_command = first["command"]
    (upstream / "package.json").write_text('{"name": "runtime", "version": "2"}\n', encoding="utf-8")
    git("commit", "-am", "upstream update", cwd=upstream)
    target_revision = git("rev-parse", "HEAD", cwd=upstream)
    attempt = Harness(tmp_path, upstream)
    real_run = attempt._run

    def fail_npm_ci(command: list[str], *, cwd: Path | None = None) -> bool:
        if Path(command[0]).name == "npm" and command[1:] == ["ci"]:
            attempt.npm_commands.append(command)
            return False
        return real_run(command, cwd=cwd)

    attempt.manager._run_install_command = fail_npm_ci  # type: ignore[method-assign]
    result = attempt.manager.prepare(automatic=True)

    assert result["ok"] is True
    assert result["command"] == old_command
    published_revision = attempt.manager._read_github_build_marker(source_dir)
    assert published_revision
    assert published_revision != target_revision
    assert result["status"]["github_source"]["built_revision"] == published_revision
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "en")
    cli._print_runtime_status(result["status"])
    output = capsys.readouterr().out
    assert target_revision not in output
    assert "prepared from" not in output
    assert "serving local revision" not in output


def test_failed_forced_build_preserves_published_command_before_retry(tmp_path: Path) -> None:
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
    assert forced["status"]["install"]["state"] == "installed"
    assert forced["command"] == installed["command"]
    assert retried["ok"] is True
    assert retried["command"] == installed["command"]
    assert harness.manager._managed_command == installed["command"]
    assert [command[1:] for command in harness.npm_commands] == [
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


@pytest.mark.parametrize("failing_step", [["ci"], ["run", "build"]])
def test_a_failed_staged_rebuild_preserves_the_published_marker(
    tmp_path: Path,
    failing_step: list[str],
) -> None:
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

    assert harness.manager._read_github_build_marker(harness.manager._github_source_dir()) == built

    harness.manager._run_install_command = real_run  # type: ignore[method-assign]
    harness.manager.force_install = False
    harness.npm_commands.clear()
    assert harness.manager._install_github_runtime().command
    assert harness.npm_commands == []
