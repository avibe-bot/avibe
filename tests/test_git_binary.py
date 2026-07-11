from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core import git_binary
from core.git_binary import ResolvedGit


def test_resolve_git_prefers_vendored(monkeypatch):
    vendored = ResolvedGit(path=Path("/managed/git"), source="vendored")
    monkeypatch.setattr(git_binary, "_resolve_vendored", lambda: vendored)
    monkeypatch.setattr(git_binary.shutil, "which", lambda _name: (_ for _ in ()).throw(AssertionError()))

    assert git_binary.resolve_git() == vendored


def test_resolve_git_uses_system_git_after_vendored(monkeypatch):
    monkeypatch.setattr(git_binary, "_resolve_vendored", lambda: None)
    monkeypatch.setattr(git_binary.shutil, "which", lambda name: "/opt/bin/git" if name == "git" else None)
    monkeypatch.setattr(git_binary.sys, "platform", "linux")

    assert git_binary.resolve_git() == ResolvedGit(path=Path("/opt/bin/git"), source="system")


def test_resolve_git_rejects_macos_shim_without_command_line_tools(monkeypatch):
    calls = []
    monkeypatch.setattr(git_binary, "_resolve_vendored", lambda: None)
    monkeypatch.setattr(git_binary.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(git_binary.sys, "platform", "darwin")
    monkeypatch.setattr(
        git_binary.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=1, stdout="", stderr="missing"),
    )

    assert git_binary.resolve_git() is None
    assert calls[0][0] == ["/usr/bin/xcode-select", "-p"]


def test_resolve_git_accepts_non_shim_macos_git_without_clt_probe(monkeypatch):
    monkeypatch.setattr(git_binary, "_resolve_vendored", lambda: None)
    monkeypatch.setattr(git_binary.shutil, "which", lambda name: "/opt/homebrew/bin/git" if name == "git" else None)
    monkeypatch.setattr(git_binary.sys, "platform", "darwin")
    monkeypatch.setattr(
        git_binary.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CLT probe must not run")),
    )

    assert git_binary.resolve_git() == ResolvedGit(path=Path("/opt/homebrew/bin/git"), source="system")


def test_resolve_git_degrades_when_no_binary_exists(monkeypatch):
    monkeypatch.setattr(git_binary, "_resolve_vendored", lambda: None)
    monkeypatch.setattr(git_binary.shutil, "which", lambda _name: None)

    assert git_binary.resolve_git() is None
