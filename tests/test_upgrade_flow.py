from __future__ import annotations

import ast
import os
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api, cli
from vibe.runtime import ServiceLauncher
from vibe.upgrade import (
    MEMORY_PACKAGE_COMPATIBILITY,
    UpgradePlan,
    build_memory_add_plan,
    build_upgrade_plan,
    configured_memory_enabled,
    execute_upgrade_plan,
    has_newer_version,
    get_current_vibe_bin_dir,
    get_latest_version_info,
    get_restart_command,
    get_restart_environment,
    get_restart_invocation_command,
    get_restart_shell_command,
    get_running_vibe_path,
    get_safe_cwd,
    installed_package_name,
    pinned_package_spec,
    RollbackTarget,
    rollback_target,
)


@pytest.fixture(autouse=True)
def _tree_is_not_an_installed_distribution(monkeypatch):
    """Every test here describes a machine, not the machine running the tests.

    `installed_package_name()` asks this process's own metadata which
    distribution provides `vibe`, and the answer differs by checkout: a developer
    running from a source tree gets nothing, while CI installs the package first
    and gets `avibe-os`. Tests written against the first answer pass locally and
    then fail on a runner for a reason that has nothing to do with the change --
    `test_a_spec_that_cannot_carry_a_pin_is_refused` did exactly that, because an
    installed name outranks the configured spec whose refusal it was asserting.

    Answering `[]` here is the honest state for a checkout, and it is the state
    every test in this module was already written against. Only the ambient
    metadata is removed; the interpreter-path heuristic stays live, so a test can
    still hand in a uv tool path and be answered from it, and a test that wants a
    measured name sets one explicitly.
    """

    monkeypatch.setattr("vibe.upgrade._distributions_providing_this_package", lambda: [])
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)
    monkeypatch.setattr("vibe.upgrade.installed_memory_package_version", lambda: None)
    monkeypatch.setattr("vibe.upgrade.package_mutation_lock", nullcontext)
    monkeypatch.setattr(api, "package_mutation_lock", nullcontext)
    monkeypatch.setattr(cli, "package_mutation_lock", nullcontext)
    monkeypatch.setattr(api, "configured_memory_enabled", lambda: False)
    monkeypatch.setattr(cli, "configured_memory_enabled", lambda: False)


def test_upgrade_preserves_memory_shape_when_config_cannot_be_read(monkeypatch):
    def fail_to_load():
        raise OSError("config is unreadable")

    monkeypatch.setattr("config.v2_config.V2Config.load", fail_to_load)

    assert configured_memory_enabled() is True


@pytest.mark.parametrize(
    ("python_executable", "uv_path", "method"),
    [
        ("/tmp/.local/share/uv/tools/avibe-os/bin/python", "/usr/local/bin/uv", "uv"),
        ("/usr/bin/python3", None, "pip"),
    ],
)
def test_memory_indep_018_upgrade_and_rollback_preserve_optional_package_shape(
    monkeypatch,
    python_executable,
    uv_path,
    method,
):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: True)
    monkeypatch.setattr(
        "vibe.upgrade.installed_memory_package_version",
        lambda: "3.0.14",
    )
    monkeypatch.setattr("vibe.__version__", "3.0.14")
    launcher = ServiceLauncher(
        python="/uv/tools/avibe-os/bin/python",
        main="/uv/tools/avibe-os/lib/python3.13/site-packages/vibe/service_main.py",
    )
    monkeypatch.setattr("vibe.runtime.current_service_launcher", lambda: launcher)
    monkeypatch.setattr("vibe.upgrade.installed_package_name", lambda *args, **kwargs: "avibe-os")

    forward = build_upgrade_plan(
        python_executable=python_executable,
        uv_path=uv_path,
        base_env={"PATH": "/usr/bin"},
    )

    assert forward.method == method
    assert "avibe-os[memory]" in forward.command
    assert forward.preflight_command is not None
    assert "--dry-run" in forward.preflight_command
    assert forward.rollback_to == RollbackTarget(
        version="3.0.14",
        package="avibe-os",
        launcher=launcher,
        memory_package=True,
        memory_version="3.0.14",
    )

    rollback_with_memory = build_upgrade_plan(
        python_executable=python_executable,
        uv_path=uv_path,
        base_env={"PATH": "/usr/bin"},
        version="3.0.14",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.14",
    )
    assert "avibe-os[memory]==3.0.14" in rollback_with_memory.command
    assert "avibe-memory==3.0.14" in rollback_with_memory.command
    assert rollback_with_memory.preflight_command is not None
    assert rollback_with_memory.cleanup_command is None

    rollback_core_only = build_upgrade_plan(
        python_executable=python_executable,
        uv_path=uv_path,
        base_env={"PATH": "/usr/bin"},
        version="3.0.14",
        package_name="avibe-os",
        memory_package=False,
    )
    assert "avibe-os==3.0.14" in rollback_core_only.command
    assert not any("[memory]" in argument for argument in rollback_core_only.command)
    if method == "pip":
        assert rollback_core_only.cleanup_command == [
            python_executable,
            "-m",
            "pip",
            "uninstall",
            "--yes",
            "avibe-memory",
        ]
    else:
        assert rollback_core_only.cleanup_command is None


def test_enabled_upgrade_selects_memory_extra_when_package_is_missing(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
    )

    assert plan.command[-1] == "avibe-os[memory]"
    assert plan.preflight_command is not None


def test_enabled_pip_upgrade_renders_url_extra_as_named_requirement(monkeypatch):
    package_url = "https://downloads.example.test/avibe_os-3.0.14-py3-none-any.whl"
    monkeypatch.setenv("AVIBE_UPGRADE_PACKAGE_SPEC", package_url)
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)
    monkeypatch.setattr(
        "vibe.upgrade.installed_metadata_describes_running_code",
        lambda: True,
    )

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
    )

    named_requirement = f"avibe-os[memory] @ {package_url}"
    assert plan.command[-1] == named_requirement
    assert plan.preflight_command is not None
    assert plan.preflight_command[-1] == named_requirement
    assert f"{package_url}[memory]" not in plan.command
    assert f"{package_url}[memory]" not in plan.preflight_command


def test_enabled_pip_upgrade_retains_local_path_extra_syntax(monkeypatch):
    package_path = "/tmp/avibe_os-3.0.14-py3-none-any.whl"
    monkeypatch.setenv("AVIBE_UPGRADE_PACKAGE_SPEC", package_path)
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)
    monkeypatch.setattr(
        "vibe.upgrade.installed_metadata_describes_running_code",
        lambda: True,
    )

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        memory_enabled=True,
    )

    local_requirement = f"{package_path}[memory]"
    assert plan.command[-1] == local_requirement
    assert plan.preflight_command is not None
    assert plan.preflight_command[-1] == local_requirement


def test_disabled_core_only_upgrade_keeps_the_core_only_shape(monkeypatch):
    monkeypatch.setattr("vibe.upgrade.memory_package_installed", lambda: False)

    plan = build_upgrade_plan(
        python_executable="/usr/bin/python3",
        uv_path=None,
        base_env={"PATH": "/usr/bin"},
        memory_enabled=False,
    )

    assert plan.command[-1] == "avibe-os"
    assert plan.preflight_command is None


def test_memory_preflight_failure_never_runs_the_replacing_install():
    plan = UpgradePlan(
        command=["installer", "replace"],
        env=None,
        method="test",
        preflight_command=["installer", "preflight"],
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 42, stdout="", stderr="incompatible")

    result = execute_upgrade_plan(plan, run=fake_run, capture_output=True, text=True)

    assert result.returncode == 42
    assert calls == [["installer", "preflight"]]


def test_package_plan_holds_the_shared_mutation_lease_for_resolution_and_install(
    monkeypatch,
):
    held = False
    calls: list[tuple[list[str], bool]] = []

    @contextmanager
    def mutation_lock():
        nonlocal held
        assert held is False
        held = True
        try:
            yield
        finally:
            held = False

    def fake_run(command, **kwargs):
        calls.append((command, held))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("vibe.upgrade.package_mutation_lock", mutation_lock)
    plan = UpgradePlan(
        command=["installer", "install"],
        env=None,
        method="test",
        preflight_command=["installer", "preflight"],
    )

    result = execute_upgrade_plan(plan, run=fake_run, capture_output=True, text=True)

    assert result.returncode == 0
    assert calls == [
        (["installer", "preflight"], True),
        (["installer", "install"], True),
    ]
    assert held is False


@pytest.mark.parametrize(
    ("python_executable", "uv_path", "method"),
    [
        ("/tmp/.local/share/uv/tools/avibe-os/bin/python", "/usr/local/bin/uv", "uv"),
        ("/usr/bin/python3", None, "pip"),
    ],
)
def test_memory_add_plan_force_reinstalls_only_the_compatible_memory_distribution(
    monkeypatch,
    python_executable,
    uv_path,
    method,
):
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    plan = build_memory_add_plan(
        version="3.0.14",
        package_name="avibe-os",
        python_executable=python_executable,
        uv_path=uv_path,
        base_env={"PATH": "/usr/bin"},
    )

    assert plan.method == method
    memory_requirement = f"avibe-memory{MEMORY_PACKAGE_COMPATIBILITY}"
    assert memory_requirement in plan.command
    assert plan.preflight_command is not None
    assert "--dry-run" in plan.preflight_command
    assert "--force-reinstall" in plan.command
    assert "--no-deps" in plan.command
    assert memory_requirement in plan.preflight_command
    assert not any(argument.startswith("avibe-os") for argument in plan.command)
    assert plan.cleanup_command is None
    if method == "uv":
        assert plan.command[:3] == ["/usr/local/bin/uv", "pip", "install"]
        assert "tool" not in plan.command
        assert plan.command[plan.command.index("--python") + 1] == python_executable
    else:
        assert plan.command[:4] == [python_executable, "-m", "pip", "install"]


def test_core_only_rollback_removes_memory_before_the_pinned_install():
    plan = UpgradePlan(
        command=["installer", "pinned-core"],
        env=None,
        method="test",
        cleanup_command=["installer", "remove-memory"],
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = execute_upgrade_plan(plan, run=fake_run, capture_output=True, text=True)

    assert result.returncode == 0
    assert calls == [["installer", "remove-memory"], ["installer", "pinned-core"]]


def test_core_only_rollback_cleanup_order_preserves_bundled_record_overlap(tmp_path):
    implementation = tmp_path / "avibe_memory" / "runtime.py"
    implementation.parent.mkdir()
    implementation.write_text("split distribution\n", encoding="utf-8")
    plan = UpgradePlan(
        command=["installer", "pinned-bundled-core"],
        env=None,
        method="test",
        cleanup_command=["installer", "remove-split-memory"],
    )

    def fake_run(command, **kwargs):
        if command == plan.cleanup_command:
            # pip uninstall follows avibe-memory's RECORD, including paths that
            # the pre-split host also owns after it is restored.
            implementation.unlink()
        else:
            implementation.write_text("bundled host\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = execute_upgrade_plan(plan, run=fake_run, capture_output=True, text=True)

    assert result.returncode == 0
    assert implementation.read_text(encoding="utf-8") == "bundled host\n"


def test_core_only_rollback_cleanup_failure_never_mutates_the_host():
    plan = UpgradePlan(
        command=["installer", "pinned-core"],
        env=None,
        method="test",
        cleanup_command=["installer", "remove-memory"],
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 17, stdout="", stderr="cleanup failed")

    result = execute_upgrade_plan(plan, run=fake_run, capture_output=True, text=True)

    assert result.returncode == 17
    assert calls == [["installer", "remove-memory"]]


def test_controller_startup_has_no_python_package_install_path():
    source = (Path(__file__).resolve().parents[1] / "core" / "controller.py").read_text(
        encoding="utf-8"
    )

    assert "build_upgrade_plan" not in source
    assert "execute_upgrade_plan" not in source
    assert "memory_package_installed" not in source
    assert '"pip"' not in source
    assert '"uv", "tool", "install"' not in source


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
#: word is, instead of silently shipping a rollback path that can no-op.
FORCES_THE_INSTALL = {"uv": "--force", "pip": "--force-reinstall"}


def test_a_pinned_plan_never_asks_for_an_upgrade_and_always_forces_the_install(monkeypatch):
    # Two ways for a rollback command to install nothing and still exit 0, one
    # per word, and both silent on every installer path.
    #
    # Asking for an upgrade resolves forward to the exact release being rolled
    # back FROM: uv resolves past the pin, pip declines to move backwards at all.
    #
    # Not forcing leaves the installer free to decide the pin is already
    # satisfied -- and on the case this change exists for, it is. Installing
    # `avibe-os` over a `vibe-remote` machine never uninstalls `vibe-remote`, so
    # that distribution's metadata still stands and still claims the old version,
    # while the files under `vibe/` are the new release's, written over the top.
    # `pip install vibe-remote==<old>` reads as satisfied, pip does nothing, and
    # the supervisor starts the failed generation again and calls it a recovery.
    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)

    uv_plan = build_upgrade_plan(
        python_executable="/tmp/.local/share/uv/tools/avibe-os/bin/python",
        uv_path="/usr/local/bin/uv",
        vibe_path="/custom/bin/vibe",
        base_env={"PATH": "/usr/bin"},
        version="3.0.10",
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
    """The rollback's own no-op, one release later and with a person watching.

    A rollback installs `vibe-remote==3.0.10` over a machine whose forward upgrade
    installed `avibe-os==3.0.11`, and pip never uninstalls the distribution it
    replaced. So the tree ends up holding both: the files under `vibe/` are 3.0.10,
    and `avibe-os` still records 3.0.11 with nothing on disk behind it.

    Then `vibe upgrade` runs. It compares this process's 3.0.10 against the
    published 3.0.11 and decides to install; pip reads `avibe-os==3.0.11` as
    already satisfied and does nothing; the command reports success and the
    machine keeps running 3.0.10 -- silently, indefinitely, and now on the path a
    user invokes by hand.

    So `--force-reinstall` is not a property of pinned plans, it is a property of
    asking for a version that is not the version on disk. Measured from our own
    two answers, never read off the metadata that is the thing in doubt.
    """

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


def test_the_rename_pair_left_by_a_rollback_forces_the_next_install(monkeypatch):
    """The state this check exists for is the one where two distributions are present.

    A rollback across the rename installs `vibe-remote==3.0.10` over a tree whose
    forward upgrade installed `avibe-os==3.0.11`, and pip does not uninstall the
    distribution it replaced. So both are recorded and only one of them can be
    describing the files under `vibe/`. Asking a single distribution -- or
    treating "more than one" as unreadable -- would make this exact machine the
    one machine that answers "metadata agrees", which is how the no-op ships:
    the next upgrade asks pip for a version `avibe-os` already claims, pip does
    nothing, and the instance keeps running the rolled-back code forever.
    """

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


def test_a_rollback_pins_the_distribution_the_install_actually_came_from(monkeypatch):
    # Which distribution published the running version and which one the next
    # upgrade should ask for are different questions, and on a machine that
    # predates the rename they have different answers. Going forward wants the
    # configured spec -- that is what the rename is for. Going back wants this,
    # because `avibe-os==2.x` names a release that was never published under that
    # name, so the rollback resolves to nothing and the instance stays dark.
    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "avibe-os")

    legacy = "/home/ai/.local/share/uv/tools/vibe-remote/bin/python"
    assert pinned_package_spec("2.9.4", python_executable=legacy) == "vibe-remote==2.9.4"
    plan = build_upgrade_plan(
        python_executable=legacy,
        uv_path="/usr/local/bin/uv",
        base_env={"PATH": "/usr/bin"},
        version="2.9.4",
    )
    assert "vibe-remote==2.9.4" in plan.command
    assert not any(argument.startswith("avibe-os") for argument in plan.command)

    # An install whose path says nothing about a distribution -- pip into a shared
    # environment -- has only the configured spec to go on, and gets it.
    assert pinned_package_spec("3.0.10", python_executable="/usr/bin/python3") == "avibe-os==3.0.10"


def test_the_rename_pair_still_names_the_distribution_that_describes_the_code(monkeypatch):
    """Two providers is not an unreadable machine, it is the aftermath of a rollback.

    Installing `avibe-os` over a `vibe-remote` machine never uninstalls the older
    distribution, so once a rollback has crossed the rename the tree holds both
    for good. Every upgrade after that measures its rollback target in this state,
    which is why reading two providers as unanswerable does not cost one recovery
    -- it costs every recovery the machine will ever attempt.

    The metadata is what tells them apart: `avibe-os` records the release that
    failed, `vibe-remote` records the files that are running. Naming neither left
    `package=None`, `pinned_package_spec` fell back to the configured forward
    name, and `avibe-os==2.9.0` was never published under that name -- so the one
    recovery attempt could only fail, on an instance that is already down.
    """

    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "avibe-os")
    monkeypatch.setattr(
        "vibe.upgrade._distributions_providing_this_package",
        lambda: ["avibe-os", "vibe-remote"],
    )
    recorded = {"avibe-os": "3.0.11", "vibe-remote": "2.9.0"}
    monkeypatch.setattr("importlib.metadata.version", lambda name: recorded[name])
    monkeypatch.setattr("vibe.__version__", "2.9.0", raising=False)

    assert installed_package_name() == "vibe-remote"

    # And the consequence the name is measured for: the pin this machine's next
    # failed upgrade rolls back to names a release that exists.
    launcher = ServiceLauncher(python="/uv/tools/vibe-remote/bin/python", main="/uv/tools/vibe-remote/service_main.py")
    monkeypatch.setattr("vibe.runtime.current_service_launcher", lambda: launcher)
    target = rollback_target()
    assert target is not None and target.package == "vibe-remote"
    assert pinned_package_spec(target.version, package_name=target.package) == "vibe-remote==2.9.0"


def test_providers_that_cannot_be_told_apart_are_left_to_the_path(monkeypatch):
    """Metadata answers this only while it distinguishes them.

    Two distributions recording the same release is a vendored or mid-rename
    environment, not a rollback, and there is nothing in the metadata to prefer
    one name over the other. Guessing would put the pin on a distribution that may
    never have published the version -- the same failure, arrived at from the
    opposite direction -- so the interpreter path answers instead, and `None` when
    it cannot either.
    """

    monkeypatch.setattr(
        "vibe.upgrade._distributions_providing_this_package",
        lambda: ["avibe-os", "vibe-remote"],
    )
    monkeypatch.setattr("importlib.metadata.version", lambda name: "3.0.11")
    monkeypatch.setattr("vibe.__version__", "3.0.11", raising=False)

    monkeypatch.setattr("vibe.upgrade.sys.executable", "/usr/bin/python3")
    assert installed_package_name() is None

    monkeypatch.setattr("vibe.upgrade.sys.executable", "/home/ai/.local/share/uv/tools/vibe-remote/bin/python")
    assert installed_package_name() == "vibe-remote"


def test_a_spec_that_cannot_carry_a_pin_is_refused(monkeypatch):
    # The alternative to refusing is falling back to the unpinned spec, which is
    # the reinstall-the-failure command above. Whoever configured a wheel path or
    # an index URL as the upgrade source gets a rollback that fails loudly instead
    # of one that lies. The message must not quote the spec: it can be an index
    # URL carrying credentials, and it is written to a restart log.
    monkeypatch.setenv("VIBE_UPGRADE_PACKAGE_SPEC", "https://user:secret@example.invalid/simple/avibe-os")

    with pytest.raises(ValueError) as refusal:
        pinned_package_spec("3.0.10")
    assert "secret" not in str(refusal.value)

    monkeypatch.setattr("vibe.upgrade.os.path.exists", lambda path: True)
    monkeypatch.setattr("vibe.upgrade.os.access", lambda path, mode: True)
    with pytest.raises(ValueError):
        build_upgrade_plan(
            python_executable="/usr/bin/python3",
            uv_path=None,
            base_env={"PATH": "/usr/bin"},
            version="3.0.10",
        )


def test_a_tree_with_no_published_release_has_no_rollback_target(monkeypatch):
    # A source checkout, an editable install, and a regression build all report
    # the same placeholder version, which names no release. Handing it on as a
    # target buys an index round-trip that fails, and then tells whoever is
    # looking at a dark instance that the rollback mechanism is broken, when the
    # truth is that this install never had a release to go back to.
    from vibe import UNKNOWN_VERSION

    monkeypatch.setattr("vibe.__version__", UNKNOWN_VERSION, raising=False)
    assert rollback_target() is None

    # And a tree that does name a release answers with the distribution and the
    # install too, in one value: all three are read here, in the process that
    # still predates the install, and there is no way to obtain one without the
    # others. The install matters as much as the version -- a rollback across the
    # `vibe-remote` -> `avibe-os` rename reinstalls into a directory this process
    # is not running out of, so a target that named only the version would be
    # restored correctly and then started from the wrong generation.
    launcher = ServiceLauncher(python="/uv/tools/vibe-remote/bin/python", main="/uv/tools/vibe-remote/service_main.py")
    monkeypatch.setattr("vibe.__version__", "3.0.10", raising=False)
    monkeypatch.setattr("vibe.upgrade.installed_package_name", lambda *args, **kwargs: "vibe-remote")
    monkeypatch.setattr("vibe.runtime.current_service_launcher", lambda: launcher)
    assert rollback_target() == RollbackTarget(version="3.0.10", package="vibe-remote", launcher=launcher)


REPO_ROOT = Path(__file__).resolve().parents[1]
#: Everything shipped to a user's machine.
SHIPPED_SOURCE_ROOTS = ("main.py", "config", "core", "modules", "storage", "vibe")


def test_only_the_plan_builder_can_ask_what_this_install_is():
    """One caller, found by looking rather than by remembering.

    The measurement has always been documented as something to take before the
    install and never after, and both upgrade paths took it after anyway --
    inside `if result.returncode == 0:`, describing an install that by then had
    already been overwritten. A rule that lives in a docstring is enforced by
    whoever happens to have read it.

    Moving it into `UpgradePlan` removes the ordering from every caller: a plan
    is what produces the command, so the measurement cannot happen later than the
    install unless someone reaches past the plan. This counts over the whole
    shipped tree so that reaching past it fails here, when it is written, rather
    than on a machine that predates the rename months later.
    """

    callers = {}
    for root in SHIPPED_SOURCE_ROOTS:
        target = REPO_ROOT / root
        for source in [target] if target.is_file() else sorted(target.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            calls = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "rollback_target"
            )
            if calls:
                callers[source.relative_to(REPO_ROOT).as_posix()] = calls

    assert callers == {"vibe/upgrade.py": 1}


def test_the_plan_that_installs_carries_what_it_is_replacing(monkeypatch):
    # A forward plan describes an install that is about to stop existing, so it
    # takes the measurement while there is still something to measure. A pinned
    # plan IS the rollback -- the process building one is the release that failed
    # -- so measuring there would hand the failure forward as its own recovery
    # target, which is the shape of every way this has gone wrong so far.
    launcher = ServiceLauncher(python="/uv/tools/vibe-remote/bin/python", main="/uv/tools/vibe-remote/service_main.py")
    monkeypatch.setattr("vibe.__version__", "3.0.10", raising=False)
    monkeypatch.setattr("vibe.upgrade.installed_package_name", lambda *args, **kwargs: "vibe-remote")
    monkeypatch.setattr("vibe.runtime.current_service_launcher", lambda: launcher)

    forward = build_upgrade_plan(python_executable="/usr/bin/python3", uv_path=None, base_env={"PATH": "/usr/bin"})
    assert forward.rollback_to == RollbackTarget(version="3.0.10", package="vibe-remote", launcher=launcher)

    pinned = build_upgrade_plan(
        python_executable="/usr/bin/python3", uv_path=None, base_env={"PATH": "/usr/bin"}, version="3.0.9"
    )
    assert pinned.rollback_to is None


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


#: A machine that predates the rename, as its own plan describes it. The upgrade
#: paths below cannot be handed this any other way, which is the point: the
#: measurement is a field of the plan precisely so that no caller is left with an
#: ordering to remember, and a test that could still inject it late would be
#: describing a seam that no longer exists.
LEGACY_INSTALL = RollbackTarget(
    version="3.0.10",
    package="vibe-remote",
    launcher=ServiceLauncher("/uv/tools/vibe-remote/bin/python", "/uv/tools/vibe-remote/service_main.py"),
)


def test_do_upgrade_uses_upgrade_plan_env_and_restarts(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env={"UV_TOOL_BIN_DIR": "/custom/bin"},
        method="uv",
        rollback_to=LEGACY_INSTALL,
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(api, "build_upgrade_plan", lambda **kwargs: plan)
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
    safe_cwd = calls["run_kwargs"].get("cwd")
    assert safe_cwd and os.path.isabs(safe_cwd), f"subprocess.run cwd must be an absolute path, got {safe_cwd!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 2.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
        # The install this process was running, taken BEFORE it was replaced and
        # carried here by the plan that replaced it: what the restart reinstalls
        # if it cannot bring the new one up. Version, distribution and launcher
        # together, because a pin needs the first two and a machine that predates
        # the rename does not answer "avibe-os" for either.
        "rollback_to": LEGACY_INSTALL,
    }


def test_do_upgrade_auto_restart_does_not_block_on_runtime_prepare(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    events: list[str] = []

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
        rollback_to=LEGACY_INSTALL,
    )
    calls: dict[str, Any] = {}

    monkeypatch.setattr(cli, "get_latest_version", lambda: {"error": None, "has_update": True, "latest": "2.2.0"})
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: "/custom/bin/vibe")
    monkeypatch.setattr(cli, "build_upgrade_plan", lambda **kwargs: plan)
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
    assert "cwd" in calls["kwargs"], "subprocess.run must specify cwd to avoid stale venv cwd"
    assert os.path.isabs(calls["kwargs"]["cwd"]), f"cwd must be absolute, got {calls['kwargs']['cwd']!r}"
    assert calls["restart_kwargs"] == {
        "delay_seconds": 0.0,
        "vibe_path": "/custom/bin/vibe",
        "trigger": "upgrade",
        "prepare_show_runtime": True,
        # See the do_upgrade case: the restart is handed the install to fall back to.
        "rollback_to": LEGACY_INSTALL,
    }


def test_cmd_upgrade_running_runtime_honors_show_runtime_skip_for_restart(monkeypatch):
    plan = UpgradePlan(
        command=["/usr/local/bin/uv", "tool", "install", "avibe-os", "--upgrade"],
        env=None,
        method="uv",
    )
    calls: dict[str, Any] = {}

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
