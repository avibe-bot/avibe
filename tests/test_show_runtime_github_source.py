"""The github-source provider must not rebuild a commit it already built.

Every ``vibe runtime prepare`` used to re-run ``npm ci`` and ``npm run build``
against a checkout that ``git fetch`` had just confirmed was unchanged, which
is around forty seconds on each regression update. A marker recording the
commit that produced the artifacts on disk turns that into one shallow fetch.
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

    assert harness.manager._install_github_runtime()

    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]
    source_dir = harness.manager._github_source_dir()
    assert harness.manager._read_github_build_marker(source_dir) == git("rev-parse", "HEAD", cwd=upstream)


def test_unchanged_upstream_reuses_the_build_instead_of_repeating_it(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime()
    harness.npm_commands.clear()

    command = harness.manager._install_github_runtime()

    assert command  # the runtime is still usable
    assert harness.npm_commands == []
    assert harness.manager._install_reason is None


def test_a_new_upstream_commit_is_still_picked_up(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime()
    harness.npm_commands.clear()

    (upstream / "package.json").write_text('{"name": "runtime", "version": "2"}\n', encoding="utf-8")
    git("commit", "-am", "two", cwd=upstream)

    assert harness.manager._install_github_runtime()

    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]
    source_dir = harness.manager._github_source_dir()
    assert harness.manager._read_github_build_marker(source_dir) == git("rev-parse", "HEAD", cwd=upstream)


def test_force_install_rebuilds_even_when_the_commit_matches(tmp_path: Path) -> None:
    upstream = make_upstream(tmp_path)
    harness = Harness(tmp_path, upstream)
    assert harness.manager._install_github_runtime()
    harness.npm_commands.clear()
    harness.manager.force_install = True

    assert harness.manager._install_github_runtime()

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
    assert result["reason"] == "runtime_install_failed"
    assert result["command"] is None
    assert result["status"]["installed"] is True
    assert result["status"]["command"] == installed["command"]


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
    assert harness.manager._install_github_runtime() is None

    source_dir = harness.manager._github_source_dir()
    assert harness.manager._read_github_build_marker(source_dir) is None

    harness.manager._run_install_command = real_run  # type: ignore[method-assign]
    harness.npm_commands.clear()
    assert harness.manager._install_github_runtime()
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
    assert harness.manager._install_github_runtime()
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
    assert harness.manager._install_github_runtime()
    assert [command[1:] for command in harness.npm_commands] == [["ci"], ["run", "build"]]
