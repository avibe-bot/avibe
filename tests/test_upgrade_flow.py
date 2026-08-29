from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api, cli
from vibe.upgrade import (
    MemoryRequirementUnreadableError,
    UpgradePlan,
    build_upgrade_plan,
    configured_memory_enabled,
    has_newer_version,
    get_current_vibe_bin_dir,
    get_latest_version_info,
    get_restart_command,
    get_restart_environment,
    get_restart_invocation_command,
    get_restart_shell_command,
    get_running_vibe_path,
    get_safe_cwd,
    pinned_package_spec,
    execute_upgrade_plan,
)


@pytest.fixture(autouse=True)
def _tree_is_not_an_installed_distribution(monkeypatch):
    """Every test here describes a machine, not the machine running the tests.

    Installed provider metadata differs between a source checkout and CI. Empty
    provider metadata keeps forward-plan tests deterministic; tests for a mixed
    provider state replace it explicitly.
    """

    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: [])
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)



def test_build_upgrade_plan_uses_uv_and_preserves_tool_bin_dir(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "uv"
    assert plan.command == ["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"]
    assert plan.env is not None
    assert plan.env["UV_TOOL_BIN_DIR"] == "/custom/bin"
    assert plan.env["PATH"] == "/usr/bin"


def test_build_upgrade_plan_forces_legacy_uv_tool_install(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/vibe-remote/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "uv"
    assert plan.command == ["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade", "--force"]


def test_build_upgrade_plan_uses_pip_for_non_uv_install():
    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "pip"
    assert plan.command == ["/usr/bin/python3", "-m", "pip", "install", "--upgrade", "avibe-os"]
    assert plan.env == {"PATH": "/usr/bin"}


def test_build_upgrade_plan_uses_env_package_spec(monkeypatch):
    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "/tmp/vibe_remote-9999.0.0-py3-none-any.whl")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/vibe-remote/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.command == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        "/tmp/vibe_remote-9999.0.0-py3-none-any.whl",
        "--upgrade",
        "--force",
    ]


#: How each installer spells "do it even though you believe it is already done".
#: Written as a mapping rather than as two assertions so that an installer added
#: to `build_upgrade_plan` later fails this test until someone decides what its
#: word is, instead of silently shipping an exact install that can no-op.
FORCES_THE_INSTALL = {"uv": "--force", "pip": "--force-reinstall"}


def test_an_exact_plan_never_asks_for_an_upgrade_and_always_forces_the_install(monkeypatch):
    # The explicit Memory install targets the running core release. Asking for an
    # upgrade would widen that request, while omitting force lets the installer
    # declare the already-present core requirement satisfied without applying the
    # matching optional package.
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    uv_plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
        package_name="avibe-os",
    )
    assert uv_plan.method == "uv"
    assert uv_plan.command == [
        "/usr/local/bin/uv",
        "tool",
        "install",
        "avibe-os==3.0.10",
        # Unconditional here: the version already installed is the newer one, so
        # without it uv reports the tool as satisfied and installs nothing.
        "--force",
    ]
    assert "--upgrade" not in uv_plan.command

    pip_plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
        package_name="avibe-os",
    )
    assert pip_plan.method == "pip"
    assert pip_plan.command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        "avibe-os==3.0.10",
    ]

    for plan in (uv_plan, pip_plan):
        assert "--upgrade" not in plan.command
        assert FORCES_THE_INSTALL[plan.method] in plan.command


def _metadata_records(monkeypatch, distribution: str, version: str) -> None:
    """One distribution provides `vibe`, and its metadata records `version`."""

    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: [distribution])
    monkeypatch.setattr("importlib.metadata.version", lambda name: version if name == distribution else "0")


def test_a_forward_upgrade_forces_the_install_once_metadata_stops_describing_the_code(monkeypatch):
    """A forward install cannot trust stale metadata over the running files."""

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    _metadata_records(monkeypatch, "avibe-os", "3.0.11")
    monkeypatch.setattr("vibe.__version__", "3.0.10")

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == "pip"
    # Still an upgrade: the version being asked for is whatever the index has
    # newest. Forced as well, because the metadata pip would consult to decide is
    # describing a release that is not what is running.
    assert "--upgrade" in plan.command
    assert "--force-reinstall" in plan.command


def test_a_stale_rename_pair_forces_the_next_install(monkeypatch):
    """Two disagreeing distribution records cannot make an upgrade a no-op."""

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "vibe.upgrade._distributions_providing_this_package",
        lambda: ["avibe-os", "vibe-remote"],
    )
    recorded = {"avibe-os": "3.0.11", "vibe-remote": "3.0.10"}
    monkeypatch.setattr("importlib.metadata.version", lambda name: recorded[name])
    monkeypatch.setattr("vibe.__version__", "3.0.10")

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert "--force-reinstall" in plan.command


def test_a_forward_upgrade_on_an_undamaged_install_is_left_to_the_installer(monkeypatch):
    """Forcing is the exception, and has to stay one.

    `--force-reinstall` makes pip rebuild and rewrite every dependency in the
    tree, so making it unconditional turns each routine upgrade into a much longer
    and much wider write on a machine nobody is watching. The ordinary case is an
    install whose metadata and files agree, and there the installer's own
    already-satisfied judgement is correct and cheaper.
    """

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    _metadata_records(monkeypatch, "avibe-os", "3.0.11")
    monkeypatch.setattr("vibe.__version__", "3.0.11")

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert "--upgrade" in plan.command
    assert "--force-reinstall" not in plan.command


@pytest.mark.parametrize(
    ("case", "distributions", "recorded", "running"),
    [
        # A source checkout or an editable install: nothing published to compare.
        ("no distribution provides the package", [], "3.0.11", "3.0.11"),
        # Mid-rename, or a vendored environment. Both are asked, and here both
        # agree with the code, so there is nothing to force -- the count of
        # distributions is not by itself a disagreement.
        ("two distributions provide it and both agree", ["avibe-os", "vibe-remote"], "3.0.11", "3.0.11"),
        # A regression build. Its version describes a tree, not a release, so a
        # disagreement with published metadata is expected rather than evidence.
        ("the running version names no release", ["avibe-os"], "3.0.11", "0.0.0.dev0+abc1234"),
        ("the recorded version names no release", ["avibe-os"], "0.0.0.dev0+abc1234", "3.0.11"),
    ],
)
def test_an_unknown_install_shape_is_never_forced_on_a_guess(monkeypatch, case, distributions, recorded, running):
    """Only a disagreement between two published releases is evidence.

    Every other answer is an environment this measurement cannot read, and the
    honest response to one is to leave the installer alone rather than to force a
    full reinstall on a machine whose shape we guessed at. Stated as the property
    -- unknown means unforced -- so a shape nobody has met yet inherits it.
    """

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: distributions)
    monkeypatch.setattr("importlib.metadata.version", lambda name: recorded)
    monkeypatch.setattr("vibe.__version__", running)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
    )

    assert "--force-reinstall" not in plan.command, case


def test_exact_package_spec_uses_the_explicit_distribution() -> None:
    assert pinned_package_spec("3.0.10", package_name="avibe-os") == "avibe-os==3.0.10"



def test_build_upgrade_plan_finds_uv_outside_current_path(monkeypatch):
    monkeypatch.setattr(
        "vibe.upgrade.shutil.which",
        lambda command, path=None: None if command == "uv" else "/custom/bin/vibe",
    )
    monkeypatch.setattr(
        "vibe.upgrade.os.path.exists",
        lambda path: path in {"/home/test/.local/bin/uv", "/custom/bin/vibe"},
    )
    monkeypatch.setattr(
        "vibe.upgrade.os.access",
        lambda path, mode: path in {"/home/test/.local/bin/uv", "/custom/bin/vibe"},
    )

    plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/test"},
    )

    assert plan.method == "uv"
    assert plan.command == ["/home/test/.local/bin/uv", "tool", "install", "avibe-os", "--upgrade"]


def test_get_current_vibe_bin_dir_resolves_launcher_target(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "vibe.upgrade.os.path.islink",
        lambda path: path in {"/usr/local/bin/vibe", "/home/test/.local/bin/vibe"},
    )
    monkeypatch.setattr(
        "vibe.upgrade.os.readlink",
        lambda path: {
            "/usr/local/bin/vibe": "/home/test/.local/bin/vibe",
            "/home/test/.local/bin/vibe": "/home/test/.local/share/uv/tools/vibe-remote/bin/vibe",
        }[path],
    )

    bin_dir = get_current_vibe_bin_dir(vibe_path="/usr/local/bin/vibe")

    assert bin_dir == "/home/test/.local/bin"


def test_get_latest_version_info_uses_override_metadata_url(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text('{"info": {"version": "9999.0.0"}}', encoding="utf-8")
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.0")

    assert info == {"current": "2.2.0", "latest": "9999.0.0", "has_update": True, "error": None}


def test_get_latest_version_info_ignores_prerelease_for_stable_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.8rc1"},
          "releases": {
            "2.2.7": [{}],
            "2.2.8rc1": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.7")

    assert info == {"current": "2.2.7", "latest": "2.2.7", "has_update": False, "error": None}


def test_get_latest_version_info_allows_newer_prerelease_for_prerelease_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.8rc2"},
          "releases": {
            "2.2.7": [{}],
            "2.2.8rc1": [{}],
            "2.2.8rc2": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.8rc1")

    assert info == {"current": "2.2.8rc1", "latest": "2.2.8rc2", "has_update": True, "error": None}


def test_get_latest_version_info_allows_newer_dotted_dev_for_prerelease_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.9.dev2"},
          "releases": {
            "2.2.8": [{}],
            "2.2.9.dev1": [{}],
            "2.2.9.dev2": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.9.dev1")

    assert info == {"current": "2.2.9.dev1", "latest": "2.2.9.dev2", "has_update": True, "error": None}


def test_get_latest_version_info_detects_post_release_for_stable_current(monkeypatch, tmp_path):
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "2.2.8.post1"},
          "releases": {
            "2.2.8": [{}],
            "2.2.8.post1": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("2.2.8")

    assert info == {"current": "2.2.8", "latest": "2.2.8.post1", "has_update": True, "error": None}


def test_has_newer_version_handles_prerelease_without_packaging():
    assert has_newer_version("2.2.8rc2", "2.2.8rc1") is True
    assert has_newer_version("2.2.8", "2.2.8rc2") is True
    assert has_newer_version("2.2.8.post1", "2.2.8") is True
    assert has_newer_version("2.2.8rc1", "2.2.8") is False
    assert has_newer_version("2.2.9.dev2", "2.2.9.dev1") is True


def test_has_newer_version_ignores_local_build_segment():
    # Regression: a source/dev install reports a setuptools-scm local version
    # such as "3.0.4rc4.dev0+gf6ca08af6.d20260624". The old parser could not
    # parse it and fell back to comparing only pure-digit components, so it
    # ranked the build below the latest stable on PyPI ("3.0.3"). That made the
    # updater "upgrade" on every cycle, restart, and DM "updated to 3.0.3" once
    # a minute forever. The local segment must be ignored and the dev/rc build
    # must sort correctly.
    local_build = "3.0.4rc4.dev0+gf6ca08af6.d20260624"
    assert has_newer_version("3.0.3", local_build) is False
    assert has_newer_version(local_build, "3.0.3") is True
    assert has_newer_version("3.0.4rc4", local_build) is True
    assert has_newer_version(local_build, "3.0.4rc4") is False
    assert has_newer_version("3.0.4+meta", "3.0.4") is False
    assert has_newer_version("3.0.4", "3.0.4+meta") is False
    assert has_newer_version("2.2.9rc1.dev2", "2.2.9rc1.dev1") is True
    # Two local builds of the same release are incomparable -> treated equal.
    assert has_newer_version("3.0.4+build2", "3.0.4+build1") is False
    assert has_newer_version("3.0.4+build1", "3.0.4+build2") is False


def test_has_newer_version_orders_dev_before_prerelease():
    # A dev release of a final sorts before that release's alphas/betas/rcs.
    assert has_newer_version("2.2.9a1", "2.2.9.dev1") is True
    assert has_newer_version("2.2.9.dev1", "2.2.9a1") is False


def test_get_latest_version_info_no_update_for_local_dev_build(monkeypatch, tmp_path):
    # Integration-level guard for the notification/restart loop: a local dev
    # build whose release is ahead of everything on PyPI must report no update.
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "3.0.3"},
          "releases": {
            "3.0.2": [{}],
            "3.0.3": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("3.0.4rc4.dev0+gf6ca08af6.d20260624")

    assert info["has_update"] is False


def test_get_latest_version_info_offers_rc_to_local_dev_build(monkeypatch, tmp_path):
    # A dev build is treated as a pre-release, so a matching-release rc on PyPI
    # is a legitimate upgrade and must be offered. Locks in the allow_prereleases
    # policy so a future change can't silently regress it.
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {
          "info": {"version": "3.0.4rc1"},
          "releases": {
            "3.0.3": [{}],
            "3.0.4rc1": [{}]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata_path.as_uri())

    info = get_latest_version_info("3.0.4.dev0+gf6ca08af6.d20260624")

    assert info == {
        "current": "3.0.4.dev0+gf6ca08af6.d20260624",
        "latest": "3.0.4rc1",
        "has_update": True,
        "error": None,
    }


def test_get_running_vibe_path_prefers_cached_launcher(monkeypatch):
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", "/custom/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: "/other/bin/vibe")

    resolved = get_running_vibe_path(argv0="vibe")

    assert resolved == "/custom/bin/vibe"


def test_get_running_vibe_path_preserves_launcher_symlink(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "vibe.upgrade.shutil.which",
        lambda *args, **kwargs: "/home/test/.local/bin/vibe",
    )

    resolved = get_running_vibe_path(argv0="vibe")

    assert resolved == "/home/test/.local/bin/vibe"


def test_get_running_vibe_path_skips_stale_cached_launcher(monkeypatch):
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", "/stale/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: path != "/stale/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: path != "/stale/bin/vibe")
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: "/fresh/bin/vibe")

    resolved = get_running_vibe_path(argv0="vibe")

    assert resolved == "/fresh/bin/vibe"


def test_get_restart_command_falls_back_to_python_module(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)

    command = get_restart_command(python_executable="/usr/bin/python3", argv0="python")

    assert command == ["/usr/bin/python3", "-c", "from vibe.cli import main; main()"]


def test_restart_invocation_command_adds_explicit_restart(monkeypatch, tmp_path):
    vibe_path = tmp_path / "bin" / "vibe"
    vibe_path.parent.mkdir()
    vibe_path.write_text("#!/bin/sh\n", encoding="utf-8")
    vibe_path.chmod(0o755)
    monkeypatch.setenv("VIBE_CURRENT_EXECUTABLE", str(vibe_path))

    command = get_restart_invocation_command()

    assert command == [str(vibe_path), "restart"]


def test_restart_shell_command_adds_explicit_restart(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)

    command = get_restart_shell_command(python_executable="/usr/bin/python3", argv0="python")

    assert command == "/usr/bin/python3 -c 'from vibe.cli import main; main()' restart"


def test_get_restart_environment_adds_source_root_for_python_fallback(monkeypatch):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)

    env = get_restart_environment(argv0="python", base_env={"PYTHONPATH": "/existing/path"})

    source_root = str(Path(__file__).resolve().parents[1])
    assert env is not None
    assert env["PYTHONPATH"] == f"{source_root}{os.pathsep}/existing/path"


def test_get_restart_environment_normalizes_relative_pythonpath_entries(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBE_CURRENT_EXECUTABLE", raising=False)
    monkeypatch.setattr("vibe.upgrade.shutil.which", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)

    env = get_restart_environment(argv0="python", base_env={"PYTHONPATH": f".{os.pathsep}src"})

    source_root = str(Path(__file__).resolve().parents[1])
    assert env is not None
    assert env["PYTHONPATH"] == f"{source_root}{os.pathsep}{tmp_path}{os.pathsep}{tmp_path / 'src'}"



def test_do_upgrade_uses_upgrade_plan_env_and_restarts(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env={"UV_TOOL_BIN_DIR": "/custom/bin"},
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "get_version_info", lambda: {"latest": "3.0.15"})
    monkeypatch.setattr(
        api,
        "build_upgrade_plan",
        lambda **kwargs: calls.setdefault("plan_kwargs", kwargs) and plan,
    )
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", lambda **kwargs: calls.setdefault("restart_kwargs", kwargs))

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["run_cmd"] = cmd
            calls["run_kwargs"] = kwargs
        else:
            raise AssertionError(f"unexpected subprocess command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is True
    assert calls["run_cmd"] == plan.command
    assert calls["run_kwargs"]["capture_output"] is True
    assert calls["run_kwargs"]["text"] is True
    assert calls["run_kwargs"]["timeout"] == 120
    assert calls["run_kwargs"]["env"] == plan.env
    assert calls["plan_kwargs"]["target_version"] == "3.0.15"
    safe_cwd = calls["run_kwargs"].get("cwd")
    assert safe_cwd and os.path.isabs(safe_cwd), f"subprocess.run cwd must be an absolute path, got {safe_cwd!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 2.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
    }


def test_memory_indep_025_failed_upgrade_is_terminal_and_retryable(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/bin/python3", "-m", "pip", "install", "--upgrade", "avibe-os"],
        env=None,
        method="pip",
    )
    restart = Mock(return_value={"job_id": "retry-restart"})
    install = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                plan.command,
                1,
                stdout="",
                stderr="resolver rejected the release",
            ),
            subprocess.CompletedProcess(
                plan.command,
                0,
                stdout="installed on retry",
                stderr="",
            ),
        ]
    )

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "get_version_info", lambda: {"latest": "3.0.15"})
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", restart)
    monkeypatch.setattr(api.subprocess, "run", install)

    failed = api.do_upgrade(auto_restart=True)
    succeeded = api.do_upgrade(auto_restart=True)

    assert failed == {
        "ok": False,
        "message": "Upgrade failed",
        "output": "resolver rejected the release",
        "restarting": False,
    }
    assert succeeded == {
        "ok": True,
        "message": "Upgrade successful. Restarting...",
        "output": "installed on retry",
        "restarting": True,
    }
    assert [call.args[0] for call in install.call_args_list] == [
        plan.command,
        plan.command,
    ]
    restart.assert_called_once()

def test_do_upgrade_blocks_mutation_when_memory_requirement_is_unreadable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api,
        "configured_memory_enabled",
        Mock(side_effect=MemoryRequirementUnreadableError()),
    )
    plan = Mock(side_effect=AssertionError("unreadable requirement built a plan"))
    monkeypatch.setattr(api, "build_upgrade_plan", plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")

    result = api.do_upgrade()

    assert result["ok"] is False
    assert result["reason"] == "memory_requirement_unreadable"
    assert result["restarting"] is False
    plan.assert_not_called()


def test_do_upgrade_auto_restart_does_not_block_on_runtime_prepare(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    events: list[str] = []

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", lambda **kwargs: events.append("restart") or {"job_id": "restart"})

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            events.append("upgrade")
        else:
            raise AssertionError(f"unexpected subprocess command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert events == ["upgrade", "restart"]


def test_do_upgrade_running_runtime_honors_show_runtime_skip_for_restart(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "1")
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(api, "schedule_restart", lambda **kwargs: calls.setdefault("restart_kwargs", kwargs))
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is True
    assert calls["restart_kwargs"]["prepare_show_runtime"] is False


def test_do_upgrade_reports_restart_scheduling_failure_as_partial_success(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)

    def fail_restart(**kwargs):
        raise RuntimeError("bad launcher")

    monkeypatch.setattr(api, "schedule_restart", fail_restart)
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is False
    assert result["message"] == "Upgrade successful, but restart scheduling failed. Please restart vibe."
    assert "Restart scheduling failed" in result["output"]
    assert "bad launcher" in result["output"]


def test_do_upgrade_without_auto_restart_prepares_runtime(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: True)

    def fail_restart(**kwargs):
        raise AssertionError("schedule_restart should not run when auto_restart is disabled")

    monkeypatch.setattr(api, "schedule_restart", fail_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["upgrade_cmd"] = cmd
            calls["upgrade_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        if cmd == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]:
            calls["runtime_prepare_cmd"] = cmd
            calls["runtime_prepare_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="runtime ready", stderr="")
        raise AssertionError(f"unexpected subprocess command: {cmd}")

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    result = api.do_upgrade(auto_restart=False)

    assert result["ok"] is True
    assert result["restarting"] is False
    assert result["output"] == "done\n\nruntime ready"
    assert calls["runtime_prepare_cmd"] == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]
    assert calls["runtime_prepare_kwargs"]["capture_output"] is True
    assert calls["runtime_prepare_kwargs"]["text"] is True
    assert calls["runtime_prepare_kwargs"]["timeout"] == 600  # prepare now budgets for Show Runtime + askill
    assert calls["runtime_prepare_kwargs"]["cwd"] == calls["upgrade_kwargs"]["cwd"]


def test_do_upgrade_keeps_runtime_stopped_when_it_was_not_running(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(api, "_runtime_process_was_running", lambda: False)

    def fail_restart(**kwargs):
        raise AssertionError("schedule_restart should not run when Avibe was not running")

    monkeypatch.setattr(api, "schedule_restart", fail_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["upgrade_cmd"] = cmd
            calls["upgrade_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        if cmd == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]:
            calls["runtime_prepare_cmd"] = cmd
            calls["runtime_prepare_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="runtime ready", stderr="")
        raise AssertionError(f"unexpected subprocess command: {cmd}")

    monkeypatch.setattr(api.subprocess, "run", fake_run)

    result = api.do_upgrade(auto_restart=True)

    assert result["ok"] is True
    assert result["restarting"] is False
    assert result["message"] == "Upgrade successful. Please restart vibe."
    assert calls["runtime_prepare_cmd"] == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]


def test_api_runtime_process_was_running_checks_service_and_ui_pid_files(monkeypatch, tmp_path):
    service_pid_path = tmp_path / "service.pid"
    ui_pid_path = tmp_path / "ui.pid"
    service_pid_path.write_text("111", encoding="utf-8")
    ui_pid_path.write_text("222", encoding="utf-8")

    monkeypatch.setattr(api.paths, "get_runtime_pid_path", lambda: service_pid_path)
    monkeypatch.setattr(api.paths, "get_runtime_ui_pid_path", lambda: ui_pid_path)

    from vibe import runtime

    service_running = False
    ui_running = False

    def fake_ui_running(pid_path):
        return pid_path == ui_pid_path and ui_running

    monkeypatch.setattr(runtime, "service_process_running", lambda: service_running)
    monkeypatch.setattr(runtime, "ui_pid_file_points_to_running_ui", fake_ui_running)

    assert api._runtime_process_was_running() is False
    ui_running = True
    assert api._runtime_process_was_running() is True
    ui_running = False
    service_running = True
    assert api._runtime_process_was_running() is True


def test_cli_runtime_process_was_running_uses_service_process_state(monkeypatch):
    service_running = False

    monkeypatch.setattr(cli.runtime, "service_process_running", lambda: service_running)
    monkeypatch.setattr(cli.runtime, "ui_pid_file_points_to_running_ui", lambda: False)

    assert cli._runtime_process_was_running() is False
    service_running = True
    assert cli._runtime_process_was_running() is True


def test_cmd_upgrade_uses_upgrade_plan_env(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env={"UV_TOOL_BIN_DIR": "/custom/bin"},
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(
        cli,
        "build_upgrade_plan",
        lambda **kwargs: calls.setdefault("plan_kwargs", kwargs) and plan,
    )
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)

    def fake_schedule_restart(**kwargs):
        calls["restart_kwargs"] = kwargs
        return {"job_id": "restart"}

    monkeypatch.setattr(cli, "schedule_restart", fake_schedule_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
        else:
            raise AssertionError(f"unexpected subprocess command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.cmd_upgrade()

    assert result == 0
    assert calls["cmd"] == plan.command
    assert calls["kwargs"]["capture_output"] is True
    assert calls["kwargs"]["text"] is True
    assert calls["kwargs"]["env"] == plan.env
    assert calls["plan_kwargs"]["target_version"] == "2.2.0"
    assert "cwd" in calls["kwargs"], "subprocess.run must specify cwd to avoid stale venv cwd"
    assert os.path.isabs(calls["kwargs"]["cwd"]), f"cwd must be absolute, got {calls['kwargs']['cwd']!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 0.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
    }


def test_cmd_upgrade_running_runtime_honors_show_runtime_skip_for_restart(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setenv("VIBE_INSTALL_SKIP_SHOW_RUNTIME", "true")
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)

    def fake_schedule_restart(**kwargs):
        calls["restart_kwargs"] = kwargs
        return {"job_id": "restart"}

    monkeypatch.setattr(cli, "schedule_restart", fake_schedule_restart)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    assert cli.cmd_upgrade() == 0
    assert calls["restart_kwargs"]["prepare_show_runtime"] is False


def test_cmd_upgrade_reports_restart_scheduling_failure_as_partial_success(monkeypatch, capsys):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)

    def fail_restart(**kwargs):
        raise RuntimeError("bad launcher")

    monkeypatch.setattr(cli, "schedule_restart", fail_restart)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="done", stderr=""),
    )

    assert cli.cmd_upgrade() == 2
    output = capsys.readouterr().out
    assert "Upgrade installed, but restart scheduling failed." in output
    assert "Restart error: bad launcher" in output
    assert "Run `vibe restart` to use the new version." in output
    assert "Upgrade failed" not in output


def test_cmd_upgrade_keeps_runtime_stopped_when_it_was_not_running(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: False)

    def fail_restart(**kwargs):
        raise AssertionError("schedule_restart should not run when Avibe was not running")

    monkeypatch.setattr(cli, "schedule_restart", fail_restart)

    def fake_run(cmd, **kwargs):
        if cmd == plan.command:
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")
        if cmd == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]:
            calls["runtime_prepare_cmd"] = cmd
            calls["runtime_prepare_kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="runtime ready", stderr="")
        raise AssertionError(f"unexpected subprocess command: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.cmd_upgrade() == 0
    assert calls["cmd"] == plan.command
    assert calls["runtime_prepare_cmd"] == ["/custom/bin/vibe", "runtime", "prepare", "--strict"]


def test_cmd_upgrade_skips_install_when_already_latest(monkeypatch):
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": False, "latest": "2.2.0"})

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called when already latest")

    monkeypatch.setattr(cli.subprocess, "run", fail_run)

    assert cli.cmd_upgrade() == 0


def test_cmd_upgrade_blocks_mutation_when_memory_requirement_is_unreadable(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(
        cli,
        "configured_memory_enabled",
        Mock(side_effect=MemoryRequirementUnreadableError()),
    )
    plan = Mock(side_effect=AssertionError("unreadable requirement built a plan"))
    monkeypatch.setattr(cli, "build_upgrade_plan", plan)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")

    assert cli.cmd_upgrade() == 1
    assert "persisted Memory requirement" in capsys.readouterr().out
    plan.assert_not_called()


@pytest.mark.parametrize("source", ["local", "direct"])
def test_cmd_upgrade_metadata_failure_uses_exact_memory_artifact(monkeypatch, tmp_path, capsys, source):
    artifact = tmp_path / "avibe_os-3.1.0-py3-none-any.whl"
    package_spec = (
        str(artifact)
        if source == "local"
        else "avibe-os @ https://example.test/releases/avibe_os-3.1.0-py3-none-any.whl"
    )
    calls: dict[str, Any] = {}

    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", package_spec)
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: True)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: {"job_id": "restart"})
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    def execute(plan, **kwargs):
        calls["plan"] = plan
        return subprocess.CompletedProcess(plan.command, 0, stdout="done", stderr="")

    monkeypatch.setattr(cli, "execute_upgrade_plan", execute)

    assert cli.cmd_upgrade() == 0
    assert "Attempting upgrade anyway..." in capsys.readouterr().out
    assert "avibe-memory==3.1.0" in calls["plan"].command


def test_cmd_upgrade_metadata_failure_refuses_unversioned_memory_source(monkeypatch, capsys):
    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "avibe-os")
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: True)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(
        cli,
        "execute_upgrade_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("upgrade must not execute")),
    )

    assert cli.cmd_upgrade() == 1
    output = capsys.readouterr().out
    assert "Attempting upgrade anyway..." not in output
    assert "Upgrade failed" in output


def test_cmd_upgrade_metadata_failure_keeps_core_only_fallback(monkeypatch, capsys):
    calls: dict[str, Any] = {}

    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "avibe-os")
    monkeypatch.setattr(
        cli,
        "get_latest_version",
        lambda: {"error": "metadata unavailable", "has_update": False, "latest": None},
    )
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "_runtime_process_was_running", lambda: True)
    monkeypatch.setattr(cli, "schedule_restart", lambda **kwargs: {"job_id": "restart"})
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    def execute(plan, **kwargs):
        calls["plan"] = plan
        return subprocess.CompletedProcess(plan.command, 0, stdout="done", stderr="")

    monkeypatch.setattr(cli, "execute_upgrade_plan", execute)

    assert cli.cmd_upgrade() == 0
    assert "Attempting upgrade anyway..." in capsys.readouterr().out
    assert calls["plan"].command[-1] == "avibe-os"


def test_memory_enabled_forward_plan_locks_memory_to_target_release(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)
    monkeypatch.setattr("vibe.upgrade.installed_metadata_describes_running_code", lambda: True)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        memory_package=True,
        memory_version="3.0.14",
        target_version="3.1.0",
        package_spec="avibe-os>=3.1,<3.2",
    )

    target = "avibe-os[memory]<3.2,>=3.1"
    memory = "avibe-memory==3.1.0"
    assert plan.command == ["/usr/bin/python3", "-m", "pip", "install", "--upgrade", target, memory]
    assert plan.preflight_command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--upgrade",
        target,
        memory,
    ]
    assert plan.preflight_fallback_command == [
        "/usr/bin/python3",
        "-m",
        "pip",
        "download",
        "--dest",
        "{avibe-pip-download-destination}",
        target,
        memory,
    ]
    assert "<3.1" not in " ".join(plan.command + plan.preflight_command)


def test_memory_enabled_forward_plan_requires_target_release(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError, match="target release version"):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            memory_package=True,
            package_spec="avibe-os",
        )


@pytest.mark.parametrize(
    ("package_spec", "target_version"),
    [
        ("avibe-os==3.1.0", "3.1.0"),
        (
            "avibe-os @ https://example.test/releases/avibe_os-3.1.1-py3-none-any.whl",
            "3.1.1",
        ),
        ("/fixtures/avibe_os-3.1.2.tar.gz", "3.1.2"),
    ],
)
def test_memory_enabled_forward_plan_derives_target_from_versioned_spec(
    monkeypatch, package_spec, target_version
):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        package_spec=package_spec,
    )

    memory_pin = f"avibe-memory=={target_version}"
    assert memory_pin in plan.command
    assert plan.preflight_command is not None and memory_pin in plan.preflight_command
    assert plan.preflight_fallback_command is not None and memory_pin in plan.preflight_fallback_command


@pytest.mark.parametrize(
    "package_spec",
    [
        "/fixtures/avibe_os-3.2.0-py3-none-any.whl",
        "avibe-os @ https://example.test/releases/avibe_os-3.2.0-py3-none-any.whl",
        "avibe-os==3.2.0",
    ],
)
def test_exact_package_spec_overrides_conflicting_memory_target(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        target_version="3.1.0",
        package_spec=package_spec,
    )

    for command in (plan.command, plan.preflight_command, plan.preflight_fallback_command):
        assert command is not None
        assert "avibe-memory==3.2.0" in command
        assert "avibe-memory==3.1.0" not in command


def test_uv_exact_artifact_overrides_conflicting_memory_target(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.is_uv_tool_install", lambda executable: True)
    monkeypatch.setattr("vibe.upgrade.is_legacy_uv_tool_install", lambda executable: False)
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: "/usr/bin/uv")
    monkeypatch.setattr("vibe.upgrade.get_current_vibe_bin_dir", lambda vibe_path: None)

    plan = build_upgrade_plan(
        python_executable="/tools/avibe/bin/python",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        target_version="3.1.0",
        package_spec="/fixtures/avibe_os-3.2.0-py3-none-any.whl",
    )

    assert "avibe-memory==3.2.0" in plan.command
    assert "avibe-memory==3.1.0" not in plan.command
    assert plan.preflight_command is not None
    assert "avibe-memory==3.2.0" in plan.preflight_command
    assert "avibe-memory==3.1.0" not in plan.preflight_command


@pytest.mark.parametrize(
    "package_spec",
    [
        "avibe-os",
        "avibe-os>=3.1,<3.2",
        "avibe-os==3.1.*",
    ],
)
def test_named_package_spec_accepts_compatible_memory_target(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        target_version="3.1.4",
        package_spec=package_spec,
    )

    assert "avibe-memory==3.1.4" in plan.command


def test_named_package_spec_rejects_incompatible_memory_target(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError, match="target release version"):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            target_version="3.2.0",
            package_spec="avibe-os>=3.1,<3.2",
        )


@pytest.mark.parametrize(
    "package_spec",
    [
        "avibe-os",
        "avibe-os>=3.1,<3.2",
        "avibe-os==3.1.*",
        "git+https://example.test/avibe.git@v3.1.0",
        "avibe-os @ git+https://example.test/avibe.git@v3.1.0",
        "/fixtures/avibe.whl",
    ],
)
def test_memory_enabled_forward_plan_rejects_unversioned_fallback_specs(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError, match="target release version"):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            package_spec=package_spec,
        )


@pytest.mark.parametrize(
    "package_spec",
    [
        "git+https://example.test/avibe.git@v3.1.0",
        "avibe-os @ git+https://example.test/avibe.git@v3.1.0",
        "https://user:secret@example.test/download",
        "avibe-os @ https://user:secret@example.test/download",
        "/fixtures/avibe.whl",
    ],
)
def test_opaque_memory_source_rejects_metadata_without_exposing_spec(monkeypatch, package_spec):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    with pytest.raises(ValueError) as refusal:
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            base_env={"PATH": "/usr/bin"},
            memory_enabled=True,
            target_version="3.1.0",
            package_spec=package_spec,
        )

    assert str(refusal.value) == "A Memory-preserving upgrade requires a target release version"


def test_memory_preflight_failure_does_not_mutate_package(monkeypatch):
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=[
            "python",
            "-m",
            "pip",
            "download",
            "--dest",
            "{avibe-pip-download-destination}",
            "avibe-os[memory]",
        ],
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing memory")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 1
    assert calls == [plan.preflight_command]


def test_legacy_pip_fallback_resolves_target_extra_before_install(tmp_path):
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=[
            "python",
            "-m",
            "pip",
            "download",
            "--dest",
            "{avibe-pip-download-destination}",
            "avibe-os[memory]",
        ],
    )
    calls: list[list[str]] = []
    scratch: Path | None = None

    def run(command, **kwargs):
        nonlocal scratch
        calls.append(command)
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="no such option: --dry-run")
        if "download" in command:
            scratch = Path(command[command.index("--dest") + 1])
            assert scratch.is_dir()
            assert "--no-deps" not in command
            return subprocess.CompletedProcess(command, 0, stdout="resolved", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 0
    assert [command[3] for command in calls] == ["install", "download", "install"]
    assert scratch is not None and not scratch.exists()


def test_legacy_pip_fallback_failure_stops_before_install():
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=[
            "python",
            "-m",
            "pip",
            "download",
            "--dest",
            "{avibe-pip-download-destination}",
            "avibe-os[memory]",
        ],
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="no such option: --dry-run")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing target Memory wheel")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 1
    assert len(calls) == 2
    assert calls[1][3] == "download"
    assert "--no-deps" not in calls[1]
    assert plan.command not in calls


def test_pip_preflight_does_not_fallback_on_resolver_failure():
    plan = UpgradePlan(
        command=["python", "-m", "pip", "install", "avibe-os[memory]"],
        env={},
        method="pip",
        preflight_command=["python", "-m", "pip", "install", "--dry-run", "avibe-os[memory]"],
        preflight_fallback_command=["python", "-m", "pip", "download", "avibe-os[memory]"],
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="No matching distribution found")

    result = execute_upgrade_plan(plan, run=run)

    assert result.returncode == 1
    assert calls == [plan.preflight_command]


def test_unreadable_memory_config_blocks_package_plan(monkeypatch):
    monkeypatch.setattr("config.v2_config.V2Config.load", lambda: (_ for _ in ()).throw(ValueError("bad config")))

    with pytest.raises(MemoryRequirementUnreadableError):
        configured_memory_enabled()


def test_missing_first_run_config_does_not_block_package_plan(monkeypatch):
    monkeypatch.setattr(
        "config.v2_config.V2Config.load",
        lambda: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert configured_memory_enabled() is False


def test_uv_forward_plan_locks_memory_to_target_release(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.is_uv_tool_install", lambda executable: True)
    monkeypatch.setattr("vibe.upgrade.is_legacy_uv_tool_install", lambda executable: False)
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: "/usr/bin/uv")
    monkeypatch.setattr("vibe.upgrade.get_current_vibe_bin_dir", lambda vibe_path: None)

    plan = build_upgrade_plan(
        python_executable="/tools/avibe/bin/python",
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
        memory_package=True,
        target_version="3.1.0",
        package_spec="avibe-os>=3.1,<3.2",
    )

    target = "avibe-os[memory]<3.2,>=3.1"
    memory = "avibe-memory==3.1.0"
    assert plan.command == [
        "/usr/bin/uv",
        "tool",
        "install",
        target,
        "--with",
        memory,
        "--upgrade",
        "--force",
    ]
    assert plan.preflight_command == [
        "/usr/bin/uv",
        "pip",
        "install",
        "--dry-run",
        "--python",
        "/tools/avibe/bin/python",
        "--upgrade",
        target,
        memory,
    ]


def test_explicit_memory_install_keeps_exact_package_version(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.find_uv_binary", lambda **kwargs: None)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        base_env={"PATH": "/usr/bin"},
        version="3.0.14",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.14",
    )

    assert "avibe-os==3.0.14" in plan.command
    assert "avibe-os[memory]" not in " ".join(plan.command)
    assert "avibe-memory==3.0.14" in plan.command
    assert plan.preflight_command is not None
    assert "avibe-memory==3.0.14" in plan.preflight_command


def test_with_memory_extra_preserves_vcs_url_and_local_specs(tmp_path, monkeypatch):
    from vibe.upgrade import _with_memory_extra

    assert _with_memory_extra("git+https://example.test/avibe.git@abc123#subdirectory=src") == (
        "avibe-os[memory] @ git+https://example.test/avibe.git@abc123#subdirectory=src"
    )
    assert _with_memory_extra("https://example.test/avibe.whl") == "avibe-os[memory] @ https://example.test/avibe.whl"
    assert _with_memory_extra("file:///tmp/avibe.whl") == "avibe-os[memory] @ file:///tmp/avibe.whl"
    assert _with_memory_extra("/fixtures/avibe.whl") == "avibe-os[memory] @ file:///fixtures/avibe.whl"

    monkeypatch.chdir(tmp_path)
    relative_artifact = Path("dist/avibe.whl")
    assert _with_memory_extra(str(relative_artifact)) == (
        f"avibe-os[memory] @ {(tmp_path / relative_artifact).resolve().as_uri()}"
    )


def test_get_safe_cwd_returns_absolute_existing_dir():
    cwd = get_safe_cwd()
    assert os.path.isabs(cwd)
    assert os.path.isdir(cwd)


def test_get_safe_cwd_falls_back_when_home_invalid(monkeypatch):
    monkeypatch.setenv("HOME", "/nonexistent_dir_for_test")
    cwd = get_safe_cwd()
    assert os.path.isabs(cwd)
    assert os.path.isdir(cwd)
    assert cwd != "/nonexistent_dir_for_test"
