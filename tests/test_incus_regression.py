from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "incus_regression.py"
SPEC = importlib.util.spec_from_file_location("incus_regression", SCRIPT_PATH)
incus_regression = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = incus_regression
SPEC.loader.exec_module(incus_regression)

MASTER_NAMES = ("avr-master", "avibe-master")


def daemon_listing(*names: str):
    """Build a ``Runner.names`` stand-in that answers from a fixed inventory.

    Stubs describe what the daemon enumerated rather than a bare yes/no, because
    the runner has to tell "absent" apart from "could not ask": a stub that can
    only say True or False cannot express the difference the code now depends on.
    """

    def listing(self, command, *, what):
        return list(names)

    return listing


def stub_incus_result(returncode: int, *, stdout: str = "", stderr: str = ""):
    """Stand in for `subprocess.run` so the real `Runner` sees a given incus result."""

    def run(command, **kwargs):
        return subprocess.CompletedProcess(list(command), returncode, stdout=stdout, stderr=stderr)

    return run


def test_master_target_uses_stable_project_instance_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_PORT", raising=False)
    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.project == "avr-master"
    assert target.instance == "avibe-master"
    assert target.host_port == 15130


def test_master_target_uses_env_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_PORT", "15131")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.host_port == 15131


def test_master_target_ignores_legacy_env_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_PORT", raising=False)
    monkeypatch.setenv("THREE_REGRESSION_PORT", "15132")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.host_port == 15130


def test_master_target_uses_env_bind_host_after_env_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_PORT_BIND_HOST", "0.0.0.0")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host=None,
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.ui_host == "0.0.0.0"


def test_master_target_prefers_port_bind_host_over_container_ui_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_UI_HOST", "127.0.0.1")
    monkeypatch.setenv("REGRESSION_PORT_BIND_HOST", "0.0.0.0")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host=None,
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.ui_host == "0.0.0.0"


def test_master_target_accepts_legacy_ui_host_as_bind_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_PORT_BIND_HOST", raising=False)
    monkeypatch.setenv("REGRESSION_UI_HOST", "0.0.0.0")

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="master",
            slug=None,
            host_port=None,
            ui_host=None,
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo"),
        dry_run=True,
    )

    assert target.ui_host == "0.0.0.0"


def test_worktree_target_slug_includes_path_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incus_regression, "branch_name", lambda repo_root: "feature/Show Runtime")
    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug=None,
            host_port=15234,
            ui_host="127.0.0.1",
            ui_port=5123,
            remote=None,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        Path("/tmp/repo-a"),
        dry_run=True,
    )

    assert target.project.startswith("avr-wt-feature-show-runtime-")
    assert target.instance.startswith("avibe-wt-feature-show-runtime-")
    assert target.host_port == 15234


def test_explicit_worktree_slug_round_trips_generated_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path("/tmp/regression-source-identity")
    monkeypatch.setattr(incus_regression, "branch_name", lambda _repo_root: "fix/regression-source-identity")

    generated = incus_regression.worktree_slug(repo_root)

    assert len(generated) == 33
    assert incus_regression.worktree_slug(repo_root, generated) == generated


def test_remote_worktree_target_requires_an_explicit_host_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incus_regression, "branch_name", lambda repo_root: "feature/demo")
    monkeypatch.setattr(
        incus_regression,
        "ensure_host_port_available",
        lambda host, port: (_ for _ in ()).throw(AssertionError("should not preflight remote ports")),
    )

    with pytest.raises(incus_regression.RegressionError) as excinfo:
        incus_regression.resolve_target(
            argparse.Namespace(
                target="worktree",
                slug=None,
                host_port=None,
                ui_host="127.0.0.1",
                ui_port=5123,
                remote="lab",
                worktree_port_start=15200,
                worktree_port_end=15200,
            ),
            Path("/tmp/repo-a"),
            dry_run=False,
        )

    assert "--host-port is required" in str(excinfo.value)


def test_remote_worktree_target_uses_the_explicit_host_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit port is the only port a remote worktree gets, local row or not."""
    runtime = tmp_path / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    (runtime / "worktrees.json").write_text(
        json.dumps({"schema_version": 1, "worktrees": {"demo-branch": {"host_port": 15234}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug="demo-branch",
            host_port=15300,
            ui_host="127.0.0.1",
            ui_port=5123,
            remote="lab",
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        tmp_path,
        dry_run=False,
    )

    assert target.host_port == 15300


def test_remote_worktree_maintenance_target_does_not_inherit_a_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status --remote` on a slug this machine also uses reports no port, not the local one."""
    runtime = tmp_path / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    (runtime / "worktrees.json").write_text(
        json.dumps({"schema_version": 1, "worktrees": {"demo-branch": {"host_port": 15234}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug="demo-branch",
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            remote="lab",
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        tmp_path,
        dry_run=False,
        allocate_port=False,
    )

    assert target.host_port == 0


def test_worktree_target_reuses_mapped_port_without_allocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    (runtime / "worktrees.json").write_text(
        json.dumps({"schema_version": 1, "worktrees": {"demo-branch": {"host_port": 15234}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "allocate_worktree_port", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should reuse mapped port")))

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug="demo-branch",
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            remote=None,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        tmp_path,
        dry_run=False,
    )

    assert target.host_port == 15234


def test_worktree_maintenance_target_does_not_allocate_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(incus_regression, "allocate_worktree_port", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not allocate for maintenance")))

    target = incus_regression.resolve_target(
        argparse.Namespace(
            target="worktree",
            slug="missing-branch",
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            remote=None,
            worktree_port_start=15200,
            worktree_port_end=15399,
        ),
        tmp_path,
        dry_run=False,
        allocate_port=False,
    )

    assert target.host_port == 0


def test_cloud_init_configures_systemd_service_without_source_code() -> None:
    data = incus_regression.cloud_init_user_data()

    assert "#cloud-config" in data
    assert "name: avibe" in data
    assert "Description=Avibe regression service" in data
    assert "Environment=VIBE_DEPLOYMENT_ENV=regression" in data
    assert "Environment=VIBE_BUILD_METADATA_PATH=/var/lib/avibe-regression/metadata.json" in data
    assert "Environment=AVIBE_ALLOW_DEV_STATE_MIGRATION=1" in data
    assert "EnvironmentFile=-/etc/avibe-regression.env" in data
    assert "ExecStart=/opt/avibe/venv/bin/python scripts/incus_regression_supervisor.py" in data
    assert "Delegate=yes" in data
    assert "MemoryAccounting=yes" in data
    assert "/opt/avibe/source" in data
    assert "/home/avibe/.vibe_remote" in data
    assert "libreoffice-nogui" not in data
    assert json.dumps(incus_regression.legacy_image_ownership_command()) in data


def test_service_directory_preparation_preserves_existing_descendants(tmp_path: Path) -> None:
    roots = (tmp_path / "home", tmp_path / "install", tmp_path / "metadata")
    script = incus_regression.prepare_service_directories_command()
    for original, replacement in zip(
        (incus_regression.SERVICE_HOME, "/opt/avibe", incus_regression.METADATA_DIR), roots,
    ):
        script = script.replace(original, shlex.quote(str(replacement)))
    script = script.replace("avibe:avibe", f"{os.getuid()}:{os.getgid()}")
    subprocess.run(["bash", "-c", script], check=True)
    owned_roots = {path for root in roots for path in (root, *root.rglob("*"))}
    for root in owned_roots:
        nested = root / "existing" / "future-runtime"
        nested.mkdir(parents=True)
        private = nested / "private"
        private.write_text("preserved", encoding="utf-8")
        private.chmod(0o600)
        (nested / "link").symlink_to(private)
    roots[0].chmod(0o700)
    descendants = {path for root in roots for path in root.rglob("*")} - owned_roots

    def metadata(path: Path) -> tuple[int, int, int, int]:
        value = path.lstat()
        return value.st_uid, value.st_gid, value.st_mode, value.st_ctime_ns

    before = {path: metadata(path) for path in descendants}
    subprocess.run(["bash", "-c", script], check=True)

    assert {path: metadata(path) for path in descendants} == before
    assert stat.S_IMODE(roots[0].stat().st_mode) == 0o700
    assert all(path.stat().st_uid == os.getuid() for path in owned_roots)


def test_legacy_image_repair_only_visits_root_owned_backend_installs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    files = {}
    for name in (".local/bin", ".npm-global", ".npm", ".avibe/runtime", ".local/share/future-runtime"):
        directory = home / name
        directory.mkdir(parents=True)
        files[name] = directory / "existing"
        files[name].write_text("preserved", encoding="utf-8")
    (home / ".npmrc").write_text("prefix=preserved", encoding="utf-8")
    (home / ".opencode").symlink_to(home / ".avibe/runtime", target_is_directory=True)
    (home / ".npm-global/link").symlink_to(files[".avibe/runtime"])
    script = incus_regression.legacy_image_ownership_command().replace(
        incus_regression.SERVICE_HOME, str(home),
    ).replace("avibe:avibe", f"{os.getuid()}:{os.getgid()}")
    # Emulate legacy image ownership without requiring root on the test host.
    stat = 'stat() { case "$3" in */.npm-global|*/.npmrc|*/.opencode) echo 0;; *) echo 1;; esac; };\n'
    before = {name: path.stat().st_ctime_ns for name, path in files.items()}
    subprocess.run(["bash", "-c", stat + script], check=True)

    assert files[".npm-global"].stat().st_ctime_ns != before[".npm-global"]
    assert {name: path.stat().st_ctime_ns for name, path in files.items() if name != ".npm-global"} == {
        name: value for name, value in before.items() if name != ".npm-global"
    }
    assert all(path.read_text(encoding="utf-8") == "preserved" for path in files.values())
    assert (home / ".opencode").is_symlink()
    migrated = files[".npm-global"].stat().st_ctime_ns
    subprocess.run(["bash", "-c", "stat() { echo 1; };\n" + script], check=True)
    assert files[".npm-global"].stat().st_ctime_ns == migrated


def test_project_config_marks_regression_target() -> None:
    target = incus_regression.RegressionTarget(
        target="worktree",
        slug="demo-branch",
        project="avr-wt-demo-branch",
        instance="avibe-wt-demo-branch",
        host_port=15200,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    config = incus_regression.project_create_config(target)

    assert "restricted=true" in config
    assert "restricted.devices.proxy=allow" in config
    assert "user.avibe_regression.target=worktree" in config
    assert "user.avibe_regression.host_port=15200" in config


def test_tenant_exec_exports_regression_guard_override() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    command = " ".join(incus_regression.tenant_exec(target, "/opt/avibe/venv/bin/vibe status"))

    assert "[ ! -f /etc/avibe-regression.env ] || . /etc/avibe-regression.env" in command
    assert "VIBE_DEPLOYMENT_ENV=regression" in command
    assert "AVIBE_ALLOW_DEV_STATE_MIGRATION=1" in command
    assert "VIBE_INTERNAL_DISPATCH_SOCKET=/tmp/vibe_remote/dispatch.sock" in command


def test_remote_ref_prefixes_resource_names_only() -> None:
    assert incus_regression.remote_ref("lab", "demo") == "lab:demo"
    assert incus_regression.remote_ref(None, "demo") == "demo"
    assert incus_regression.remote_ref("lab") == "lab:"
    assert incus_regression.optional_remote_ref(None) == []
    assert incus_regression.optional_remote_ref("lab") == ["lab:"]


def test_incus_command_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCUS_CMD", "sudo incus")

    assert incus_regression.incus("info") == ["sudo", "incus", "info"]


def test_require_incus_uses_command_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCUS_CMD", "/custom/incus --debug")
    monkeypatch.setattr(incus_regression.shutil, "which", lambda executable: executable if executable == "/custom/incus" else None)

    incus_regression.require_incus()


def test_require_incus_reports_missing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCUS_CMD", "/missing/incus")
    monkeypatch.setattr(incus_regression.shutil, "which", lambda executable: None)

    with pytest.raises(incus_regression.RegressionError, match="/missing/incus"):
        incus_regression.require_incus()


def test_runner_reports_command_timeout(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    command = ["incus", "exec", "avibe-master"]

    def time_out(actual_command, **kwargs):
        assert kwargs["timeout"] == incus_regression.SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(actual_command, kwargs["timeout"])

    monkeypatch.setattr(incus_regression.subprocess, "run", time_out)

    with pytest.raises(incus_regression.RegressionError, match="timed out after 300 seconds"):
        incus_regression.Runner().run(
            command,
            timeout=incus_regression.SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS,
        )

    assert "Command timed out after 300 seconds" in capsys.readouterr().err


def test_existence_comes_from_the_names_the_daemon_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = json.dumps([{"name": "avr-master"}, {"name": "avr-wt-demo-branch"}])
    monkeypatch.setattr(incus_regression.subprocess, "run", stub_incus_result(0, stdout=listing))

    names = incus_regression.Runner().names(incus_regression.incus("project", "list"), what="Incus projects")

    assert names == ["avr-master", "avr-wt-demo-branch"]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"other": "ignored"}, id="no-name"),
        pytest.param({"name": None}, id="null-name"),
        pytest.param({"name": 7}, id="non-string-name"),
        pytest.param("avr-master", id="not-an-object"),
    ],
)
def test_an_entry_the_runner_cannot_read_makes_the_whole_listing_unanswerable(
    monkeypatch: pytest.MonkeyPatch, entry
) -> None:
    """An unparseable entry is a listing nobody enumerated, not one entry fewer.

    Dropping it leaves an inventory that looks complete and happens to be
    missing whatever the runner could not read -- which reads as a confirmed
    absence, and `reconcile --yes` acts on a confirmed absence by releasing host
    ports that are still in use.
    """
    listing = json.dumps([{"name": "avr-master"}, entry])
    monkeypatch.setattr(incus_regression.subprocess, "run", stub_incus_result(0, stdout=listing))

    with pytest.raises(incus_regression.RegressionError, match="Unreadable entry"):
        incus_regression.Runner().names(incus_regression.incus("project", "list"), what="Incus projects")


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"returncode": 1, "stderr": "Error: unix.socket: connect: connection refused\n"}, id="daemon-down"),
        pytest.param({"returncode": 1, "stderr": "Error: Failed to begin transaction: context deadline exceeded\n"}, id="daemon-stalled"),
        pytest.param({"returncode": 1, "stdout": "boom on stdout\n"}, id="detail-on-stdout"),
        pytest.param({"returncode": 1}, id="silent-failure"),
        pytest.param({"returncode": 0, "stdout": "not json"}, id="unparseable"),
        pytest.param({"returncode": 0, "stdout": '{"name": "avr-master"}'}, id="not-a-listing"),
    ],
)
@pytest.mark.parametrize(
    "ask",
    [
        pytest.param(lambda runner: incus_regression.project_exists(runner, None, "avr-master"), id="project"),
        pytest.param(
            lambda runner: incus_regression.instance_exists(runner, None, "avr-master", "avibe-master"), id="instance"
        ),
    ],
)
def test_unanswerable_existence_question_raises_instead_of_reporting_absence(
    monkeypatch: pytest.MonkeyPatch, result: dict, ask
) -> None:
    """Whatever keeps the daemon from producing a listing, the answer is an error.

    `incus` exits non-zero both when an object is genuinely absent and when the
    daemon is unreachable or stalled, so a boolean answer derived from exit
    status silently turns "cannot tell" into "not there" — and every caller then
    sets out to create what already exists.
    """
    monkeypatch.setattr(incus_regression.subprocess, "run", stub_incus_result(**result))

    with pytest.raises(incus_regression.RegressionError):
        ask(incus_regression.Runner())


def test_unanswerable_existence_question_reports_the_daemon_detail_and_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        incus_regression.subprocess,
        "run",
        stub_incus_result(1, stderr="Error: Failed to begin transaction: context deadline exceeded\n"),
    )

    with pytest.raises(incus_regression.RegressionError) as excinfo:
        incus_regression.project_exists(incus_regression.Runner(), None, "avr-master")

    message = str(excinfo.value)
    assert "context deadline exceeded" in message
    assert "INCUS_CMD" in message


def test_absent_project_answers_without_listing_inside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []

    def run(command, **kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, stdout=json.dumps([{"name": "avr-other"}]))

    monkeypatch.setattr(incus_regression.subprocess, "run", run)

    assert incus_regression.instance_exists(incus_regression.Runner(), None, "avr-master", "avibe-master") is False
    assert commands == [incus_regression.incus("project", "list", "--format", "json")]


def test_default_base_image_alias_is_not_remote_syntax() -> None:
    assert ":" not in incus_regression.DEFAULT_IMAGE


def test_proxy_device_uses_remote_instance_ref() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    args = incus_regression.proxy_device_args(target, remote="lab")

    assert args[3] == "lab:avibe-master"
    assert "listen=tcp:127.0.0.1:15130" in args
    assert "connect=tcp:127.0.0.1:5123" in args


def proxy_device_runner(observed: dict[str, str] | None, *, listable: bool = True):
    """A runner that answers device queries the way Incus does.

    `config device list` is the observation that can distinguish an absent
    device from an unreachable daemon: `listable=False` fails it the way a
    daemon that cannot be reached does, while `observed=None` is a listing the
    daemon completed which simply has no `ui` device in it. `config device get`
    answers from `observed`.
    """

    commands = []

    class RecordingRunner:
        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, *, check=True, capture=False, **kwargs):
            commands.append((command, check))
            if "list" in command and "device" in command:
                if not listable:
                    return subprocess.CompletedProcess(
                        command, 1, stdout="", stderr="Error: Failed to fetch instance: Instance not found"
                    )
                return subprocess.CompletedProcess(command, 0, stdout="root\n" if observed is None else "root\nui\n")
            if "get" in command and "ui" in command:
                if observed is None or command[-1] not in observed:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="Error: Device doesn't exist")
                return subprocess.CompletedProcess(command, 0, stdout=f"{observed[command[-1]]}\n")
            return subprocess.CompletedProcess(command, 0, stdout="")

    return RecordingRunner(), commands


def test_matching_proxy_device_is_left_alone() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    runner, commands = proxy_device_runner(
        {"listen": "tcp:127.0.0.1:15130", "connect": "tcp:127.0.0.1:5123"}
    )

    incus_regression.ensure_proxy_device(runner, target, remote=None)

    # A device that already forwards the wanted endpoints must not be touched:
    # removing it drops the port forward, and every `up` used to do exactly that.
    mutations = [command for command, _ in commands if {"add", "remove", "set"} & set(command)]
    assert mutations == []


def test_mismatched_proxy_device_is_updated_in_place() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15131,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    runner, commands = proxy_device_runner(
        {"listen": "tcp:127.0.0.1:15130", "connect": "tcp:127.0.0.1:5123"}
    )

    incus_regression.ensure_proxy_device(runner, target, remote=None)

    rendered = [" ".join(command) for command, _ in commands]
    assert (
        "incus --project avr-master config device set avibe-master ui"
        " listen=tcp:127.0.0.1:15131 connect=tcp:127.0.0.1:5123" in rendered
    )
    # An in-place set leaves no window in which the instance has no ui device,
    # so a repointed port never needs the device removed first.
    assert not any("remove" in command for command, _ in commands)


def test_absent_proxy_device_is_added_without_a_removal() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15131,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    runner, commands = proxy_device_runner(None)

    incus_regression.ensure_proxy_device(runner, target, remote=None)

    rendered = [" ".join(command) for command, _ in commands]
    assert any(
        "incus --project avr-master config device add avibe-master ui proxy"
        " listen=tcp:127.0.0.1:15131" in command
        for command in rendered
    )
    # There is nothing to remove: the daemon listed this instance's devices and
    # `ui` was not among them.
    assert not any("remove" in command for command, _ in commands)


def test_unlistable_devices_abort_before_anything_is_mutated() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15131,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    runner, commands = proxy_device_runner(
        {"listen": "tcp:127.0.0.1:15130", "connect": "tcp:127.0.0.1:5123"}, listable=False
    )

    with pytest.raises(incus_regression.RegressionError) as excinfo:
        incus_regression.ensure_proxy_device(runner, target, remote=None)

    # A daemon that will not answer is the one case where acting is worst: the
    # instance may well have a working `ui` device that nobody can see.
    assert "Instance not found" in str(excinfo.value)
    assert [command for command, _ in commands if {"add", "remove", "set"} & set(command)] == []


def test_listed_device_with_unreadable_endpoints_aborts() -> None:
    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15131,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    runner, commands = proxy_device_runner({"listen": "tcp:127.0.0.1:15130"})

    with pytest.raises(incus_regression.RegressionError) as excinfo:
        incus_regression.ensure_proxy_device(runner, target, remote=None)

    assert "would not report its connect" in str(excinfo.value)
    assert [command for command, _ in commands if {"add", "remove", "set"} & set(command)] == []


def test_existing_instance_is_not_reinitialised() -> None:
    commands = []

    class RecordingRunner:
        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, *, check=True, **kwargs):
            commands.append((command, check))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15131,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.ensure_project_and_instance(
        RecordingRunner(),
        target,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
    )

    rendered = [" ".join(command) for command, _ in commands]
    assert not any(" init " in f" {command} " for command in rendered)
    assert not any("soffice" in command or "libreoffice" in command for command in rendered)
    assert not any("chown -hR" in command or "chown -R" in command for command in rendered)


def test_build_base_uses_publishable_temp_instance() -> None:
    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    args = argparse.Namespace(
        dry_run=True,
        remote=None,
        source_image="images:ubuntu/24.04/cloud",
        temp_instance="avibe-regression-base-build",
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
    )

    original_runner = incus_regression.Runner
    try:
        incus_regression.Runner = RecordingRunner
        assert incus_regression.cmd_build_base(args) == 0
    finally:
        incus_regression.Runner = original_runner

    joined = "\n".join(" ".join(command) for command in commands)
    assert "--ephemeral" not in joined
    assert "incus launch images:ubuntu/24.04/cloud avibe-regression-base-build --storage default --network incusbr0" in joined
    assert "https://deb.nodesource.com/setup_20.x" in joined
    assert "useradd --create-home --shell /bin/bash --groups sudo avibe" in joined
    backend_install = joined.split("sudo -H -u avibe -- bash -s <<'AVIBE_BACKENDS'\n", 1)[1].split("\nAVIBE_BACKENDS", 1)[0]
    assert 'avibe_home="$HOME"' in backend_install
    assert 'npm install -g @anthropic-ai/claude-code @openai/codex' in backend_install
    assert "https://askill.sh | sh -s -- -b /usr/local/bin" in joined
    assert ".npm-global" in joined
    assert 'ln -sf "$avibe_home/.npm-global/bin/claude" "$avibe_home/.local/bin/claude"' in joined
    assert 'ln -sf "$avibe_home/.npm-global/bin/codex" "$avibe_home/.local/bin/codex"' in joined
    assert 'curl -fsSL https://opencode.ai/install | bash -s -- --no-modify-path' in backend_install
    assert 'ln -sf "$avibe_home/.opencode/bin/opencode" "$avibe_home/.local/bin/opencode"' in joined
    # Backends must not be root-global: the non-root avibe user owns them and self-updates.
    assert "/usr/local/bin/opencode" not in joined
    assert "cloud-init clean --logs || true" in joined
    assert "incus publish avibe-regression-base-build --alias avibe-regression-base-current" in joined
    subprocess.run(["bash", "-n"], input=next(command[-1] for command in commands if "apt-get update" in command[-1]), text=True, check=True)


def test_source_exclude_drops_runtime_and_dependency_dirs() -> None:
    assert incus_regression.should_exclude(".runtime/state.json")
    assert incus_regression.should_exclude("ui/node_modules/pkg/index.js")
    assert incus_regression.should_exclude("ui/dist/assets/app.js")
    assert not incus_regression.should_exclude("ui/dist/assets/app.js", include_ui_dist=True)
    assert incus_regression.should_exclude("pkg/__pycache__/x.pyc")
    assert incus_regression.should_exclude(".env")
    assert incus_regression.should_exclude(".env.regression")
    assert incus_regression.should_exclude(".env.three-regression")
    assert incus_regression.should_exclude(".env.e2e")
    assert incus_regression.should_exclude("ui/.env.local")
    assert incus_regression.should_exclude("api/.env.preview.local")
    assert not incus_regression.should_exclude("vibe/ui_server.py")


def test_root_build_output_is_excluded_without_capturing_ui_dist() -> None:
    """``/dist`` is anchored so it cannot decide ``ui/dist``'s fate.

    ``ui/dist`` is the front-end bundle that ``--no-build-ui`` ships on
    purpose; the Python wheel output at the repository root never is.
    """
    assert incus_regression.should_exclude("dist/avibe-3.0.12.tar.gz")
    assert incus_regression.should_exclude("dist")
    assert not incus_regression.should_exclude("ui/dist/assets/app.js", include_ui_dist=True)
    assert not incus_regression.should_exclude("vibe/dist_helpers.py")


def test_source_tar_drops_a_virtualenv_whatever_it_is_named(tmp_path: Path) -> None:
    """Virtualenvs are recognised by ``pyvenv.cfg``, not by a list of names.

    They hold host-native binaries that are useless inside the container, and
    the repository has carried both ``venv`` and ``.venv`` at the same time.
    """
    for name in ("venv", ".venv", "env", "tools/sandbox"):
        root = tmp_path / name
        (root / "lib").mkdir(parents=True)
        (root / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        (root / "lib" / "libpython.dylib").write_text("mach-o\n", encoding="utf-8")
    (tmp_path / "vibe").mkdir()
    (tmp_path / "vibe" / "cli.py").write_text("print('ok')\n", encoding="utf-8")

    with tarfile.open(fileobj=io.BytesIO(incus_regression.build_source_tar(tmp_path))) as tar:
        names = set(tar.getnames())

    assert "vibe/cli.py" in names
    assert not [name for name in names if "pyvenv.cfg" in name or "libpython" in name]


def test_source_tar_keeps_a_plain_directory_that_merely_looks_like_a_virtualenv(tmp_path: Path) -> None:
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "settings.py").write_text("DEBUG = False\n", encoding="utf-8")

    with tarfile.open(fileobj=io.BytesIO(incus_regression.build_source_tar(tmp_path))) as tar:
        assert "env/settings.py" in tar.getnames()


def write_ui_builder_stage(root: Path, *, external: Sequence[str] = ()) -> None:
    """Give a fixture repo root the Dockerfile stage the UI fingerprint reads.

    ``compute_fingerprints`` takes the build inputs living outside ``ui/`` from
    the ``ui-builder`` stage's ``COPY`` lines, so a fixture without that stage is
    not a checkout the runner could be pointed at.
    """
    copies = "".join(f"COPY {relative} /app/{relative}\n" for relative in external)
    (root / "Dockerfile").write_text(
        "FROM node:20-slim AS ui-builder\n"
        "WORKDIR /app/ui\n"
        "COPY ui/package.json ui/package-lock.json ./\n"
        f"{copies}"
        "COPY ui/ .\n"
        "RUN npm run build\n"
        "\nFROM python:3.12-slim AS base\n"
        "COPY pyproject.toml /app/pyproject.toml\n",
        encoding="utf-8",
    )


def test_ui_source_fingerprint_covers_every_ui_input_and_no_output(tmp_path: Path) -> None:
    """State the property: sources count, outputs and dependencies do not.

    Listing the config files that matter is how ``postcss.config.js`` and
    ``ui/scripts/`` were left out, which lets an unchanged fingerprint ship a
    stale bundle now that the UI is no longer rebuilt unconditionally.
    """
    write_ui_builder_stage(tmp_path)
    ui = tmp_path / "ui"
    (ui / "src").mkdir(parents=True)
    (ui / "src" / "main.tsx").write_text("export {}\n", encoding="utf-8")
    baseline = incus_regression.compute_fingerprints(tmp_path)["ui_source"]

    for relative in ("postcss.config.js", "eslint.config.js", "agentation.d.ts", "scripts/build.mjs"):
        path = ui / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// one\n", encoding="utf-8")
        added = incus_regression.compute_fingerprints(tmp_path)["ui_source"]
        assert added != baseline, f"{relative} is a build input but is outside the fingerprint"
        baseline = added

    for produced in incus_regression.UI_NON_SOURCE_DIRS:
        (ui / produced).mkdir(parents=True, exist_ok=True)
        (ui / produced / "generated").write_text("noise\n", encoding="utf-8")
    assert incus_regression.compute_fingerprints(tmp_path)["ui_source"] == baseline


def test_ui_fingerprint_covers_the_build_inputs_that_live_outside_ui(tmp_path: Path) -> None:
    """The UI build's input set is the files it reads, not the directory they sit in.

    ``ui/src/lib/messageTypes.ts`` imports the repository-root message-type
    catalog and Vite inlines it into the browser bundle, so a commit touching
    only that catalog changes the artifact. A ``ui/``-only hash reads identical
    and skips the rebuild, leaving the environment with a backend on the new
    message-type policy and a front end still applying the old one.
    """
    (tmp_path / "ui" / "src").mkdir(parents=True)
    (tmp_path / "ui" / "src" / "messageTypes.ts").write_text(
        "import catalog from '../../vibe/message_types.json';\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "vibe" / "message_types.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"version": 1}\n', encoding="utf-8")
    write_ui_builder_stage(tmp_path, external=["vibe/message_types.json"])

    before = incus_regression.compute_fingerprints(tmp_path)["ui_source"]
    catalog.write_text('{"version": 2}\n', encoding="utf-8")

    assert incus_regression.compute_fingerprints(tmp_path)["ui_source"] != before


def test_this_checkout_declares_the_message_type_catalog_as_a_ui_build_input() -> None:
    """The fixture above proves the mechanism; this proves the answer is real.

    Read against this repository rather than a synthetic tree, so the one import
    that actually escapes ``ui/`` today has to be in the set.
    """
    repo_root = Path(incus_regression.__file__).resolve().parent.parent

    assert "vibe/message_types.json" in incus_regression.ui_external_build_inputs(repo_root)


def test_declaring_nothing_outside_ui_is_an_answer_but_an_unreadable_stage_is_not(tmp_path: Path) -> None:
    """An empty set is a real answer here, and a missing declaration must not be.

    The escaping inputs are read from a declaration the repository maintains --
    the ``ui-builder`` stage builds from a context holding only ``ui/``, and
    ``npm run build`` fails when its ``COPY`` lines and the real imports
    disagree -- so "the stage declares nothing outside ``ui/``" is a state that
    can legitimately hold. A Dockerfile that no longer has the stage is
    different in kind: answering "nothing" there would narrow the input set to a
    hash that keeps matching, and the UI would never be rebuilt again.
    """
    write_ui_builder_stage(tmp_path)
    assert incus_regression.ui_external_build_inputs(tmp_path) == []

    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim AS base\nCOPY pyproject.toml /app/pyproject.toml\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError):
        incus_regression.ui_external_build_inputs(tmp_path)


def test_source_tar_excludes_regression_secret_file(tmp_path: Path) -> None:
    (tmp_path / ".env.regression").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "vibe").mkdir()
    (tmp_path / "vibe" / "ui_server.py").write_text("print('ok')\n", encoding="utf-8")

    payload = incus_regression.build_source_tar(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        names = set(archive.getnames())
        ui_server = archive.extractfile("vibe/ui_server.py")
        assert ui_server is not None
        content = ui_server.read()

    assert ".env.regression" not in names
    assert b"print('ok')" in content


def test_source_tar_excludes_all_local_env_files(tmp_path: Path) -> None:
    (tmp_path / ".env.e2e").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".env.preview.local").write_text("SECRET=2\n", encoding="utf-8")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / ".env.local").write_text("SECRET=3\n", encoding="utf-8")
    (tmp_path / "vibe").mkdir()
    (tmp_path / "vibe" / "ui_server.py").write_text("print('ok')\n", encoding="utf-8")

    payload = incus_regression.build_source_tar(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        names = set(archive.getnames())

    assert ".env.e2e" not in names
    assert ".env.preview.local" not in names
    assert "ui/.env.local" not in names


def test_source_tar_can_include_existing_ui_dist_when_build_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "ui" / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "ui" / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (tmp_path / "ui" / "dist" / "assets" / "app.js").write_text("console.log('ok')\n", encoding="utf-8")

    payload = incus_regression.build_source_tar(tmp_path, include_ui_dist=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        names = set(archive.getnames())

    assert "ui/dist/index.html" in names
    assert "ui/dist/assets/app.js" in names


def test_sync_source_clears_stale_files_even_without_clean(tmp_path: Path) -> None:
    commands = []

    class RecordingRunner:
        dry_run = True

        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.sync_source(RecordingRunner(), target, tmp_path, remote=None, clean=False)

    joined = "\n".join(" ".join(command) for command in commands)
    assert f"find {incus_regression.SOURCE_DIR} -mindepth 1 -maxdepth 1 ! -name ui -exec rm -rf" in joined
    assert f"find {incus_regression.SOURCE_DIR}/ui -mindepth 1 -maxdepth 1 " in joined
    for kept in incus_regression.UI_NON_SOURCE_DIRS:
        assert f"! -name {kept}" in joined


def test_source_sync_writes_as_service_user_and_preserves_unshipped_trees(tmp_path: Path) -> None:
    source = tmp_path / "source"
    deployed = tmp_path / "deployed"
    source.mkdir()
    deployed.mkdir()
    (source / "ui").mkdir()
    (source / "ui" / "entry.js").write_text("new source", encoding="utf-8")
    (source / "tool").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (source / "tool").chmod(0o755)
    (source / "tool-link").symlink_to("tool")
    seed_source_tree(deployed)
    preserved = {
        path: path.lstat()
        for name in incus_regression.UI_NON_SOURCE_DIRS
        for path in (deployed / "ui" / name).rglob("*")
    }
    archive_commands = []

    class LocalRunner:
        dry_run = False

        def run(self, command, **kwargs):
            shell_index = command.index("-lc") + 1
            script = command[shell_index].replace(incus_regression.SOURCE_DIR, str(deployed))
            if "input_bytes" in kwargs:
                archive_commands.append(command)
                # Only the generated extraction runs locally, inside the fixture root.
                script = script[script.index("tar --no-same-owner"):]
            return subprocess.run(["bash", "-c", script], input=kwargs.get("input_bytes"), check=True)

    target = incus_regression.RegressionTarget("master", "master", "avr-master", "avibe-master", 15130, "127.0.0.1", 5123)
    incus_regression.sync_source(LocalRunner(), target, source, remote=None, clean=False)

    assert len(archive_commands) == 1
    assert ["sudo", "-H", "-u", "avibe"] == archive_commands[0][archive_commands[0].index("sudo"):][:4]
    assert (deployed / "ui" / "entry.js").read_text() == "new source"
    assert stat.S_IMODE((deployed / "tool").stat().st_mode) == 0o755
    assert (deployed / "tool-link").readlink() == Path("tool")
    assert (deployed / "tool").stat().st_uid == os.getuid()
    assert not (deployed / "stale.py").exists()
    assert all(path.lstat().st_ctime_ns == before.st_ctime_ns for path, before in preserved.items())


def test_sync_source_without_clean_keeps_every_ui_non_source_dir(tmp_path: Path) -> None:
    """A sync must leave ui/node_modules and ui/dist for the fingerprints to judge.

    Deleting them makes ``npm ci`` and ``npm run build`` unconditional, which is
    the single largest cost of an update whose front end did not change.
    """
    script = run_sync_wipe(tmp_path, clean=False)

    assert sorted(p.name for p in (tmp_path / "ui").iterdir()) == sorted(incus_regression.UI_NON_SOURCE_DIRS)
    assert not (tmp_path / "stale.py").exists()
    assert not (tmp_path / "stale-dir").exists()
    assert not (tmp_path / "ui" / "src").exists()
    assert script  # the wipe really ran rather than silently matching nothing


def test_sync_source_with_clean_wipes_the_ui_dirs_too(tmp_path: Path) -> None:
    run_sync_wipe(tmp_path, clean=True)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("include_ui_dist", [False, True])
def test_a_sync_only_preserves_what_its_own_archive_does_not_ship(tmp_path: Path, include_ui_dist: bool) -> None:
    """Preservation is for what the sync does not carry; equality is for what it does.

    ``tar`` extracts over what is already there and never deletes, so preserving
    a directory the archive also ships leaves whatever the host deleted -- a
    renamed chunk, a dropped public asset -- served next to the new bundle. Both
    sides are read out of the real code: the shipped set from the archive the
    same call would send, the preserved set from the wipe command it emits. A
    change to either one alone breaks this.
    """
    seed_source_tree(tmp_path)
    with tarfile.open(
        fileobj=io.BytesIO(incus_regression.build_source_tar(tmp_path, include_ui_dist=include_ui_dist))
    ) as tar:
        shipped = {name for name in incus_regression.UI_NON_SOURCE_DIRS if f"ui/{name}" in tar.getnames()}

    script = run_sync_wipe(tmp_path, clean=False, include_ui_dist=include_ui_dist)
    preserved = {name for name in incus_regression.UI_NON_SOURCE_DIRS if f"! -name {name}" in script}

    assert preserved == set(incus_regression.UI_NON_SOURCE_DIRS) - shipped
    assert shipped or not include_ui_dist  # the archive really carries ui/dist in that mode
    for name in shipped:
        assert not (tmp_path / "ui" / name).exists(), f"ui/{name} survived a sync that overwrites it"
    for name in preserved:
        assert (tmp_path / "ui" / name / "marker").exists(), f"ui/{name} was wiped for the build to recreate"


def seed_source_tree(source_root: Path) -> None:
    """A deployed source tree holding stale files, sources, and the UI's non-source dirs."""
    for relative in ("stale.py", "stale-dir/old.txt", "ui/src/App.tsx"):
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    for name in incus_regression.UI_NON_SOURCE_DIRS:
        (source_root / "ui" / name).mkdir(parents=True, exist_ok=True)
        (source_root / "ui" / name / "marker").write_text("x", encoding="utf-8")


def run_sync_wipe(source_root: Path, *, clean: bool, include_ui_dist: bool = False) -> str:
    """Run sync_source's wipe command against a real directory tree.

    The wipe is a shell one-liner, so asserting on its text only proves we
    wrote what we meant to write. Executing it against a populated tree is what
    proves it deletes the stale files and keeps the expensive ones.
    """
    seed_source_tree(source_root)

    commands: list[list[str]] = []

    class RecordingRunner:
        dry_run = True

        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    incus_regression.sync_source(
        RecordingRunner(),
        target,
        source_root,
        remote=None,
        clean=clean,
        include_ui_dist=include_ui_dist,
    )

    script = next(
        command[-1]
        for command in commands
        if command[-1].startswith("mkdir -p") and "-exec rm -rf" in command[-1]
    )
    subprocess.run(
        ["bash", "-lc", script.replace(incus_regression.SOURCE_DIR, str(source_root))],
        check=True,
    )
    return script


def test_ui_public_assets_are_part_of_source_fingerprint(tmp_path: Path) -> None:
    write_ui_builder_stage(tmp_path)
    (tmp_path / "ui" / "src").mkdir(parents=True)
    (tmp_path / "ui" / "public").mkdir(parents=True)
    (tmp_path / "ui" / "public" / "push-sw.js").write_text("one\n", encoding="utf-8")

    before = incus_regression.compute_fingerprints(tmp_path)["ui_source"]
    (tmp_path / "ui" / "public" / "push-sw.js").write_text("two\n", encoding="utf-8")
    after = incus_regression.compute_fingerprints(tmp_path)["ui_source"]

    assert before != after


def test_legacy_voice_realtime_build_flag_does_not_change_ui_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_ui_builder_stage(tmp_path)
    monkeypatch.setenv("REGRESSION_VOICE_REALTIME_ENABLED", "false")
    before = incus_regression.compute_fingerprints(tmp_path)["ui_source"]

    monkeypatch.setenv("REGRESSION_VOICE_REALTIME_ENABLED", "true")
    after = incus_regression.compute_fingerprints(tmp_path)["ui_source"]

    assert before == after


def test_runtime_env_payload_maps_show_runtime_and_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_SHOW_RUNTIME_ARCHIVE_PATH", "/tmp/show-runtime.tgz")
    monkeypatch.setenv("REGRESSION_SLACK_CHANNEL", "C123")
    monkeypatch.setenv("REGRESSION_VOICE_REALTIME_ENABLED", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    payload = incus_regression.runtime_env_payload().decode()

    assert "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0" in payload
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS=0.0.0.dev0" in payload
    assert "AVIBE_ALLOW_DEV_STATE_MIGRATION=1" in payload
    assert "VIBE_SHOW_RUNTIME_SOURCE=archive" in payload
    assert "VIBE_SHOW_RUNTIME_ARCHIVE_PATH=/home/avibe/.cache/avibe-regression/vibe-show-runtime-node.tgz" in payload
    assert "VIBE_SHOW_RUNTIME_ARCHIVE_PATH=/tmp/show-runtime.tgz" not in payload
    assert "REGRESSION_SLACK_CHANNEL=C123" in payload
    assert "VITE_VOICE_REALTIME_ENABLED" not in payload
    assert "REGRESSION_VOICE_REALTIME_ENABLED" not in payload
    assert "OPENAI_API_KEY=sk-test" in payload


@pytest.mark.parametrize("legacy_source", ["github", "github-source"])
def test_runtime_env_payload_migrates_legacy_github_source(
    monkeypatch: pytest.MonkeyPatch,
    legacy_source: str,
) -> None:
    monkeypatch.setenv("REGRESSION_SHOW_RUNTIME_SOURCE", legacy_source)

    payload = incus_regression.runtime_env_payload().decode()

    assert "VIBE_SHOW_RUNTIME_SOURCE=archive" in payload
    assert f"VIBE_SHOW_RUNTIME_SOURCE={legacy_source}" not in payload


def test_runtime_env_payload_ignores_legacy_regression_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REGRESSION_SHOW_RUNTIME_ARCHIVE_PATH", raising=False)
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)
    monkeypatch.setenv("THREE_REGRESSION_SHOW_RUNTIME_ARCHIVE_PATH", "/tmp/legacy.tgz")
    monkeypatch.setenv("THREE_REGRESSION_SLACK_CHANNEL", "CLEGACY")

    payload = incus_regression.runtime_env_payload().decode()

    assert "VIBE_SHOW_RUNTIME_ARCHIVE_PATH=/home/avibe/.cache/avibe-regression/vibe-show-runtime-node.tgz" in payload
    assert "REGRESSION_SLACK_CHANNEL=CLEGACY" not in payload
    assert "THREE_REGRESSION_SLACK_CHANNEL" not in payload


def test_runtime_env_payload_forces_container_ui_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGRESSION_UI_HOST", "192.168.2.3")
    monkeypatch.setenv("THREE_REGRESSION_UI_HOST", "10.1.2.3")

    payload = incus_regression.runtime_env_payload().decode()

    assert "REGRESSION_UI_HOST=127.0.0.1" in payload
    assert "REGRESSION_UI_HOST=192.168.2.3" not in payload
    assert "REGRESSION_UI_HOST=10.1.2.3" not in payload


def test_load_env_file_accepts_export_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env.regression"
    env_file.write_text("export REGRESSION_SLACK_CHANNEL=C123\n", encoding="utf-8")
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)

    loaded = incus_regression.load_env_file(tmp_path, env_file)

    assert loaded == env_file
    assert incus_regression.os.environ["REGRESSION_SLACK_CHANNEL"] == "C123"


def test_load_env_file_ignores_legacy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env.three-regression"
    env_file.write_text("export THREE_REGRESSION_SLACK_CHANNEL=C123\n", encoding="utf-8")
    monkeypatch.delenv("REGRESSION_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("THREE_REGRESSION_SLACK_CHANNEL", raising=False)

    loaded = incus_regression.load_env_file(tmp_path, None)

    assert loaded is None
    assert "THREE_REGRESSION_SLACK_CHANNEL" not in incus_regression.os.environ


def test_require_runtime_seed_env_fails_fast_for_blank_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "  ")

    with pytest.raises(SystemExit) as excinfo:
        incus_regression.require_runtime_seed_env()

    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_require_runtime_seed_env_checks_platform_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "REGRESSION_SLACK_BOT_TOKEN",
        "REGRESSION_SLACK_APP_TOKEN",
        "REGRESSION_DISCORD_BOT_TOKEN",
        "REGRESSION_FEISHU_APP_ID",
        "REGRESSION_FEISHU_APP_SECRET",
    ):
        monkeypatch.setenv(key, "set")
    monkeypatch.setenv("REGRESSION_FEISHU_APP_SECRET", "")

    with pytest.raises(SystemExit) as excinfo:
        incus_regression.require_runtime_seed_env()

    assert "REGRESSION_FEISHU_APP_SECRET" in str(excinfo.value)


def test_require_runtime_seed_env_rejects_legacy_platform_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set")
    monkeypatch.setenv("OPENAI_API_KEY", "set")
    for key in incus_regression.required_platform_seed_envs():
        monkeypatch.delenv(key, raising=False)
        legacy_key = "THREE_" + key
        monkeypatch.setenv(legacy_key, "legacy")

    with pytest.raises(SystemExit) as excinfo:
        incus_regression.require_runtime_seed_env()

    assert "REGRESSION_SLACK_BOT_TOKEN" in str(excinfo.value)


def test_prepare_state_skips_existing_state_without_reset() -> None:
    commands = []

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.run_prepare_state(RecordingRunner(), target, reset_mode="none", remote=None)

    joined = "\n".join(" ".join(command) for command in commands)
    assert "test -f /home/avibe/.avibe/config/config.json" in joined
    assert "prepare_regression.py" not in joined


def test_prepare_state_reseeds_when_reset_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.run_prepare_state(RecordingRunner(), target, reset_mode="config", remote=None)

    joined = "\n".join(" ".join(command) for command in commands)
    assert "rm -rf /home/avibe/.avibe/config /home/avibe/.avibe/state /home/avibe/.avibe/runtime" in joined
    assert "rm -rf /home/avibe/.regression-seed" in joined
    assert "prepare_regression.py" in joined
    copy_command = next(command for command in commands if "cp -a" in " ".join(command))
    assert "sudo -H -u avibe" in " ".join(copy_command)
    assert "--no-preserve=ownership" in " ".join(copy_command)
    assert "chown" not in " ".join(copy_command)


def test_prepare_state_reset_all_deletes_target_home_before_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.run_prepare_state(RecordingRunner(), target, reset_mode="all", remote=None)

    joined = "\n".join(" ".join(command) for command in commands)
    assert "rm -rf /home/avibe/.avibe /home/avibe/.vibe_remote" in joined
    assert "/home/avibe/.codex" in joined
    assert "ln -sfn /home/avibe/.avibe /home/avibe/.vibe_remote" in joined


def test_guard_paired_master_reset_rejects_remote_access_state() -> None:
    commands = []

    class PairingRunner:
        dry_run = False

        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "paired"}')

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="pairing state is present"):
        incus_regression.guard_paired_master_reset(
            PairingRunner(),
            target,
            reset_mode="config",
            allow_reset_paired_master=False,
            remote=None,
        )

    joined = "\n".join(" ".join(command) for command in commands)
    assert "/home/avibe/.avibe/config/config.json" in joined


def test_remote_pairing_probe_detects_nested_vibe_cloud_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "remote_access": {
                    "provider": "vibe_cloud",
                    "vibe_cloud": {
                        "enabled": True,
                        "public_url": "https://test-app.avibe.bot",
                        "instance_id": "inst_123",
                        "tunnel_token": "token_123",
                    },
                }
            }
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", incus_regression.remote_pairing_probe_script()],
        check=True,
        capture_output=True,
        env={**os.environ, "AVIBE_REMOTE_PAIRING_CONFIG_PATH": str(config_path)},
        text=True,
    )

    assert json.loads(result.stdout)["state"] == "paired"


def test_remote_pairing_probe_detects_legacy_only_config(tmp_path: Path) -> None:
    missing_new_config = tmp_path / ".avibe" / "config" / "config.json"
    legacy_config = tmp_path / ".vibe_remote" / "config" / "config.json"
    legacy_config.parent.mkdir(parents=True)
    legacy_config.write_text(
        json.dumps(
            {
                "remote_access": {
                    "provider": "vibe_cloud",
                    "vibe_cloud": {
                        "public_url": "https://test-app.avibe.bot",
                    },
                }
            }
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", incus_regression.remote_pairing_probe_script()],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "AVIBE_REMOTE_PAIRING_CONFIG_PATHS": os.pathsep.join([str(missing_new_config), str(legacy_config)]),
        },
        text=True,
    )

    assert json.loads(result.stdout)["state"] == "paired"


def test_guard_paired_master_reset_fails_closed_when_probe_fails() -> None:
    class BrokenProbeRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="venv missing")

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="could not be verified safely"):
        incus_regression.guard_paired_master_reset(
            BrokenProbeRunner(),
            target,
            reset_mode="config",
            allow_reset_paired_master=False,
            remote=None,
        )


def test_guard_paired_master_reset_fails_closed_when_probe_json_is_invalid() -> None:
    class InvalidJsonRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="not json")

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="could not be verified safely"):
        incus_regression.guard_paired_master_reset(
            InvalidJsonRunner(),
            target,
            reset_mode="all",
            allow_reset_paired_master=False,
            remote=None,
        )


def test_guard_paired_master_reset_fails_closed_when_config_is_unreadable() -> None:
    class UnreadableConfigRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "unknown"}')

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    with pytest.raises(incus_regression.RegressionError, match="could not be verified safely"):
        incus_regression.guard_paired_master_reset(
            UnreadableConfigRunner(),
            target,
            reset_mode="config",
            allow_reset_paired_master=False,
            remote=None,
        )


def test_guard_paired_master_reset_allows_verified_unpaired_config() -> None:
    class UnpairedRunner:
        dry_run = False

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "unpaired"}')

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.guard_paired_master_reset(
        UnpairedRunner(),
        target,
        reset_mode="config",
        allow_reset_paired_master=False,
        remote=None,
    )


def test_guard_paired_master_reset_allows_explicit_override() -> None:
    class FailingRunner:
        dry_run = False

        def run(self, command, **kwargs):
            raise AssertionError("override should skip remote status probing")

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.guard_paired_master_reset(
        FailingRunner(),
        target,
        reset_mode="all",
        allow_reset_paired_master=True,
        remote=None,
    )


def test_guard_paired_master_reset_ignores_worktree_targets() -> None:
    class FailingRunner:
        dry_run = False

        def run(self, command, **kwargs):
            raise AssertionError("worktree resets are not protected by master pairing guard")

    target = incus_regression.RegressionTarget(
        target="worktree",
        slug="feature",
        project="avr-wt-feature",
        instance="avibe-wt-feature",
        host_port=15200,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.guard_paired_master_reset(
        FailingRunner(),
        target,
        reset_mode="all",
        allow_reset_paired_master=False,
        remote=None,
    )


def test_write_runtime_env_uses_stdin_not_command_line() -> None:
    commands = []
    inputs = []

    class RecordingRunner:
        dry_run = False

        def run(self, command, *, input_bytes=None, **kwargs):
            commands.append(command)
            inputs.append(input_bytes)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.write_runtime_env(RecordingRunner(), target, remote="lab")

    joined_command = " ".join(commands[0])
    assert commands[0][:5] == ["incus", "--project", "avr-master", "exec", "lab:avibe-master"]
    assert "chown root:avibe /etc/avibe-regression.env" in joined_command
    assert "chmod 0640 /etc/avibe-regression.env" in joined_command
    assert b"VIBE_SHOW_RUNTIME_SOURCE" in inputs[0]
    assert "OPENAI_API_KEY" not in joined_command


def reconcile_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: dict,
    projects: tuple[str, ...],
    instances: tuple[dict, ...],
) -> tuple[Path, list]:
    """Point `reconcile` at a repo with `entries` recorded and `projects` in Incus.

    A record in `instances` may omit `project`, in which case it is placed in the
    project its name implies -- `incus list` always reports one, and only a test
    about misplacement needs to say which. That default substitutes one prefix
    for the other rather than minting a name, so a fixture is free to describe an
    environment whose name the runner would have refused to create: that is
    precisely the environment enumerating from Incus exists to find.
    """

    repo = tmp_path / "repo"
    runtime = repo / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    (runtime / "worktrees.json").write_text(
        json.dumps({"schema_version": 1, "worktrees": entries}), encoding="utf-8"
    )

    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        def names(self, command, *, what):
            commands.append(command)
            return list(projects)

        def records(self, command, *, what):
            commands.append(command)
            return [
                {
                    "project": incus_regression.WORKTREE_PROJECT_PREFIX
                    + str(item["name"]).removeprefix(incus_regression.WORKTREE_INSTANCE_PREFIX),
                    **dict(item),
                }
                for item in instances
            ]

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", RecordingRunner)
    return runtime / "worktrees.json", commands


def test_reconcile_reports_environments_incus_holds_without_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mapping_path, commands = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={"tracked": {"path": str(tmp_path / "checkout"), "host_port": 52900, "branch": "fix/one"}},
        projects=("default", "avr-master", "avr-wt-tracked", "avr-wt-orphan"),
        instances=(
            {"name": "avibe-master", "status": "Running"},
            {"name": "avibe-wt-orphan", "status": "Running"},
            {"name": "avibe-wt-tracked", "status": "Stopped"},
        ),
    )

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=False, dry_run=False, remote=None)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    # Enumerating from Incus is the point: an environment created outside the
    # runner has no metadata row, so walking the mapping cannot see it at all.
    assert "orphan  [project, Running]  no runner metadata" in out
    assert "delete --target worktree --slug orphan --yes" in out
    assert "tracked  [project, Stopped]" in out
    assert "delete --target worktree --slug tracked --yes" in out
    # The master environment is not a worktree environment and is never listed.
    assert "2 worktree regression environment(s)" in out
    assert not any(line.startswith("  master ") for line in out.splitlines())
    # Reporting must never delete an environment.
    assert not any("delete" in command for command in commands)
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert set(payload["worktrees"]) == {"tracked"}


def test_reconcile_forgets_metadata_for_environments_incus_no_longer_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `gone` records a path that still exists, which is exactly the case the old
    # staleness criterion could not see: the path is the checkout the runner was
    # invoked from, not the environment. Incus is what settles it.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    mapping_path, commands = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={
            "gone": {"path": str(checkout), "host_port": 52901, "commit": "b69d287d32f6aaaa"},
            "kept": {"path": str(checkout), "host_port": 52902, "branch": "fix/two"},
        },
        projects=("avr-wt-kept",),
        instances=({"name": "avibe-wt-kept", "status": "Running"},),
    )

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=True, dry_run=False, remote=None)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "gone  port 52901, detached at b69d287d32f6" in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert set(payload["worktrees"]) == {"kept"}
    # Only metadata changes. The environment Incus still holds keeps running.
    assert not any("delete" in command for command in commands)


def test_reconcile_dry_run_keeps_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={"gone": {"path": str(tmp_path / "checkout"), "host_port": 52903}},
        projects=(),
        instances=(),
    )

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=False, dry_run=True, remote=None)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "needs --yes" in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert set(payload["worktrees"]) == {"gone"}


@pytest.mark.parametrize(
    ("yes", "dry_run"),
    [(yes, dry_run) for yes in (False, True) for dry_run in (False, True) if not (yes and not dry_run)],
    ids=lambda value: f"{value}",
)
def test_reconcile_reports_stale_metadata_without_writing_or_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], yes: bool, dry_run: bool
) -> None:
    """Having something to report is what this command is for, not a failure.

    Asserted over every combination that withholds the write -- the whole product
    of the two flags minus the one that asks for it, so a mode cannot be left out
    of the enumeration. `reconcile` used to raise here, which made the documented
    plain command exit non-zero for exactly the case it exists to show: a `&&`
    chain, a CI step, or anyone reading `$?` cannot tell that from the command
    breaking, while the report it just printed says the run went fine. The one
    writing combination is `test_reconcile_forgets_metadata_for_environments…`.
    """
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={"gone": {"path": str(tmp_path / "checkout"), "host_port": 52904}},
        projects=(),
        instances=(),
    )
    before = mapping_path.read_bytes()

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=yes, dry_run=dry_run, remote=None)
    )

    assert exit_code == 0
    assert "gone  port 52904" in capsys.readouterr().out
    assert mapping_path.read_bytes() == before


def test_reconcile_enumerates_through_a_real_listing_under_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `Runner.names` answers [] for a dry run by contract. If reconcile enumerated
    # through a dry-run runner it would read every recorded environment as gone
    # and offer to forget all of it, so it must build its own real runner.
    seen = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            seen.append(dry_run)
            self.dry_run = dry_run

        def names(self, command, *, what):
            return ["avr-wt-kept"]

        def records(self, command, *, what):
            return [{"name": "avibe-wt-kept", "status": "Running", "project": "avr-wt-kept"}]

    repo = tmp_path / "repo"
    (repo / ".runtime" / "incus-regression").mkdir(parents=True)
    (repo / ".runtime" / "incus-regression" / "worktrees.json").write_text(
        json.dumps({"schema_version": 1, "worktrees": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", RecordingRunner)

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=False, dry_run=True, remote=None)
    )

    assert exit_code == 0
    assert seen == [False]
    assert "kept  [project, Running]" in capsys.readouterr().out


def test_reconcile_keeps_a_project_whose_instance_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Half a footprint is still a footprint. The project owns the slug and has to
    # be reclaimed, and its row still holds the host port, so neither the listing
    # nor the metadata may treat this as an environment Incus no longer has.
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={"half": {"path": str(tmp_path / "checkout"), "host_port": 52905, "branch": "fix/half"}},
        projects=("avr-wt-half",),
        instances=(),
    )

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=True, dry_run=False, remote=None)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "half  [project, no instance]" in out
    assert "delete --target worktree --slug half --yes" in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert set(payload["worktrees"]) == {"half"}


def test_reconcile_does_not_offer_to_delete_a_misplaced_instance_by_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `delete --slug` derives the project from the slug, so an instance living
    # somewhere else is not reachable by it. Printing the command anyway promises
    # a removal that would report success and leave the instance running.
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={},
        projects=(),
        instances=({"name": "avibe-wt-misplaced", "status": "Running", "project": "default"},),
    )

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=True, dry_run=False, remote=None)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "misplaced  [no project, Running in default]" in out
    assert "delete --target worktree --slug misplaced" not in out
    assert "reclaim it by hand" in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {}


def test_reconcile_reports_every_instance_sharing_one_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Incus scopes instance names per project, so one name can be two instances.

    A single observation slot keeps whichever the listing mentioned last. If that
    is the convention-project one, the report offers a delete command and says
    nothing about the instance it cannot reach -- the disk stays occupied by
    something the operator was told had been enumerated.
    """
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={},
        projects=("avr-wt-doubled",),
        instances=(
            {"name": "avibe-wt-doubled", "status": "Stopped", "project": "default"},
            {"name": "avibe-wt-doubled", "status": "Running", "project": "avr-wt-doubled"},
        ),
    )

    exit_code = incus_regression.cmd_reconcile(argparse.Namespace(yes=True, dry_run=False, remote=None))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "doubled  [project, Stopped in default, Running]" in out
    # Both statements are true of this environment at once, so it earns both: the
    # command that reclaims what the convention covers, and the warning about what
    # it does not.
    assert "delete --target worktree --slug doubled" in out
    assert "instance lives in project default, not avr-wt-doubled" in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {}


def test_reconcile_lists_names_the_runner_would_not_mint_and_offers_only_runnable_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every command the report prints is one the runner accepts, whatever Incus holds.

    An environment the runner did not create had nothing constraining its name,
    so the names below are ones Incus accepts and `--slug` does not. Deriving
    those names from the slug a second time ran them back through the validating
    minter, and `reconcile` raised before printing anything at all -- blinding the
    one command whose purpose is finding environments nobody tracked.

    The command assertion is a property over the whole report rather than a list
    of rejected shapes: too short, too long, a trailing hyphen, and whatever else
    Incus permits form an open set, and the member a list omits is the one that
    ships. Parsing the commands back out of the output also covers report lines
    that do not exist yet.
    """
    too_long = "l" * 41
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={},
        projects=("avr-wt-ab", f"avr-wt-{too_long}", "avr-wt-fine"),
        instances=(
            {"name": "avibe-wt-ab", "status": "Running"},
            {"name": "avibe-wt-fine", "status": "Stopped"},
        ),
    )

    exit_code = incus_regression.cmd_reconcile(argparse.Namespace(yes=True, dry_run=False, remote=None))
    out = capsys.readouterr().out

    assert exit_code == 0
    # Reported at all, which is the regression: three environments exist and the
    # command used to raise on the first one it could not have created.
    assert "3 worktree regression environment(s)" in out
    for slug in ("ab", too_long, "fine"):
        assert f"  {slug}  [" in out

    offered = []
    for line in out.splitlines():
        printed = line.strip()
        if not printed.startswith("python3 scripts/incus_regression.py"):
            continue
        argv = shlex.split(printed)
        if "--slug" in argv:
            offered.append(argv[argv.index("--slug") + 1])
    assert offered == ["fine"]
    for slug in offered:
        # Raises if the report offered a command `delete` would reject.
        incus_regression.validate_slug(slug)

    # What the runner cannot name, it names the objects for instead. A command
    # that exits on its own argument is not a reclamation path.
    assert "  ab: not a slug this runner accepts" in out
    assert "Reclaim by hand: avr-wt-ab, avr-wt-ab/avibe-wt-ab." in out
    assert f"Reclaim by hand: avr-wt-{too_long}." in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {}


def test_a_prune_keeps_a_row_exactly_while_a_run_holds_its_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row survives a `--yes` prune exactly while its slug's lock is held.

    `up` records the slug and its port before it creates the project and the
    instance, so a row in that state has no footprint yet and never had one:
    reading "no footprint" as "stale" releases a port a live `up` is about to
    bind. What identifies such a row is not in the row. It is the update lock the
    run holds from before it writes the row until the environment is built, which
    the kernel drops however that run ends.

    So every recorded shape is seeded against both answers, and only the lock is
    allowed to move the outcome. Seeding rather than listing the exempt shapes is
    the point: each rule that read the row was wrong about a shape its author had
    not thought of. Comparing the two stamps was wrong for a re-reserved slug,
    because `reserve` merges and the previous run's `updated_at` survives, so any
    clock that put it after the new `reserved_at` read a live reservation as
    finished. Requiring a claim was wrong for the reservation v3.0.12 writes,
    which has no claim at all -- seeded here, since a released runner's rows are
    a shipped shape this code has to live with.
    """
    shapes = {
        "no stamps at all": {},
        "reserved, never completed": {"reserved_at": "2026-08-20T05:32:29.994595+00:00"},
        "reserved by a released runner": {
            "reserved_at": "2026-08-20T05:32:29.994595+00:00",
            "branch": "fix/built-by-v3-0-12",
        },
        "completed after reserving": {
            "reserved_at": "2026-08-20T05:32:29.994595+00:00",
            "updated_at": "2026-08-20T05:41:11.000000+00:00",
        },
        "completion older than the reservation over it": {
            "reserved_at": "2026-08-20T05:41:11.000000+00:00",
            "updated_at": "2026-08-20T05:32:29.994595+00:00",
        },
        "completion dated in the future": {
            "reserved_at": "2026-08-20T05:32:29.994595+00:00",
            "updated_at": "2099-01-01T00:00:00.000000+00:00",
        },
        "stamps nothing can parse": {"reserved_at": "yesterday", "updated_at": "soon"},
        "completed, no reservation left": {"updated_at": "2026-08-20T05:41:11.000000+00:00"},
    }

    entries: dict[str, dict] = {}
    building: set[str] = set()
    port = 53000
    for index, (_shape, recorded) in enumerate(sorted(shapes.items())):
        for claimed in (True, False):
            for locked in (True, False):
                slug = f"{'busy' if locked else 'idle'}-{index}-{'claimed' if claimed else 'bare'}"
                entries[slug] = {"path": str(tmp_path / "checkout"), "host_port": port, **recorded}
                port += 1
                if claimed:
                    entries[slug]["claim"] = f"claim-of-{slug}"
                if locked:
                    building.add(slug)
    assert len(entries) == 4 * len(shapes) and len(building) == 2 * len(shapes)

    mapping_path, _ = reconcile_fixture(
        tmp_path, monkeypatch, entries=entries, projects=(), instances=()
    )

    with contextlib.ExitStack() as stack:
        for slug in sorted(building):
            # Held from this process on purpose: `flock` belongs to the open file
            # description, not the process, so the probe's own descriptor
            # conflicts with these exactly as another run's would. A test that
            # had to fork to hold a lock would be testing the fork.
            lock_path = incus_regression.target_lock_path(
                tmp_path / "repo", None, f"{incus_regression.WORKTREE_PROJECT_PREFIX}{slug}"
            )
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(lock_path.open("w", encoding="utf-8"))
            incus_regression.fcntl.flock(handle.fileno(), incus_regression.fcntl.LOCK_EX)
        exit_code = incus_regression.cmd_reconcile(argparse.Namespace(yes=True, dry_run=False, remote=None))
        out = capsys.readouterr().out
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert set(payload["worktrees"]) == building
    # And each row is reported under what happened to it, so a kept one is not
    # silence and a dropped one is not a surprise. Read out of the report by
    # section rather than asserted line by line: the sections are what the
    # operator acts on, and every seeded row has to appear in exactly one.
    reported: dict[str, set[str]] = {"kept": set(), "dropped": set()}
    section = None
    for line in out.splitlines():
        if line.endswith("is still holding:"):
            section = "kept"
        elif line.endswith("no longer has:"):
            section = "dropped"
        elif not line.strip():
            section = None
        elif section and line.startswith("  "):
            slug = line.strip().split()[0]
            if slug in entries:
                reported[section].add(slug)
    assert reported == {"kept": building, "dropped": set(entries) - building}


def test_a_slug_is_in_flight_whenever_the_kernel_cannot_say_it_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a lock this probe took itself answers "nobody holds this slug".

    A missing lock file is the one real absence: nothing has ever locked that
    slug. Reading it must not create it, or the absence would be spent by the act
    of asking. Everything else -- a lock somebody holds, a file this user cannot
    open, a platform with no `flock` -- is a question left unanswered, and those
    answer "held" for the same reason every unanswered question in `reconcile`
    keeps the row.
    """
    repo = tmp_path / "repo"
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    lock_path = incus_regression.target_lock_path(repo, None, "avr-wt-demo")

    assert incus_regression.target_run_in_flight(repo, None, "avr-wt-demo") is False
    assert not lock_path.exists()

    lock_path.parent.mkdir(parents=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        # An `up` that ended left its lock file behind; the file is not the claim.
        assert incus_regression.target_run_in_flight(repo, None, "avr-wt-demo") is False
        incus_regression.fcntl.flock(handle.fileno(), incus_regression.fcntl.LOCK_EX)
        assert incus_regression.target_run_in_flight(repo, None, "avr-wt-demo") is True
    assert incus_regression.target_run_in_flight(repo, None, "avr-wt-demo") is False

    real_open = os.open

    def refuse(path, flags, *rest):
        if str(path) == str(lock_path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *rest)

    with monkeypatch.context() as unreadable:
        unreadable.setattr(incus_regression.os, "open", refuse)
        assert incus_regression.target_run_in_flight(repo, None, "avr-wt-demo") is True

    monkeypatch.setattr(incus_regression, "fcntl", None)
    assert incus_regression.target_run_in_flight(repo, None, "avr-wt-demo") is True


def test_a_lock_answers_for_one_daemon_and_local_runs_keep_the_released_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An environment is a daemon and a project -- for locks as much as for rows.

    Project names are per-daemon and every remote has the same ones, so a lock
    keyed on the project alone leaves a `--remote` run indistinguishable from a
    local one holding that slug, and a local `reconcile` reads it as a reason to
    keep a stale row and its port. Asserted over every ordered pair of
    authorities rather than over the one crossing that was reported: a lock held
    under one authority is never evidence under another, and a lock held under
    the same one always is.

    The local path is a cross-version contract on top of that. Every released
    runner locks at `locks/<project>.lock`, and sharing that exact file is the
    whole reason a lock can answer for a run this code did not start, so no
    authority may be spelled into it.
    """
    repo = tmp_path / "repo"
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)

    assert incus_regression.target_lock_path(repo, None, "avr-wt-demo") == (
        incus_regression.runtime_root(repo) / "locks" / "avr-wt-demo.lock"
    )

    authorities = (None, "lab", "other")
    for holder in authorities:
        # Held from this process: `flock` belongs to the open file description, so
        # the probe's own descriptor conflicts exactly as another run's would.
        with incus_regression.target_update_lock(repo, holder, "avr-wt-demo", dry_run=False):
            for asker in authorities:
                assert incus_regression.target_run_in_flight(repo, asker, "avr-wt-demo") is (
                    asker == holder
                )


def test_a_remote_that_is_not_one_name_cannot_leave_the_locks_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--remote` is spelled into a daemon reference and into a lock filename.

    Each holds exactly one name, so the invariant is about what survives the
    parser rather than about which spellings are listed here: whatever it accepts
    puts its lock inside the locks directory. Refused at the normalizer because
    that is the one point both readers are downstream of, and refused as an
    argparse error because `main` catches `RegressionError` only after the parser
    has already run.
    """
    repo = tmp_path / "repo"
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    locks = incus_regression.runtime_root(repo) / "locks"

    # Not vacuous: an ordinary name still gets through, whitespace and all.
    assert incus_regression.normalized_remote("  lab  ") == "lab"
    assert incus_regression.normalized_remote("   ") is None

    for value in ("lab", "lab-2", "lab.local", "a/b", "../evil", "a\\b", "lab:extra", ".", ".."):
        try:
            name = incus_regression.normalized_remote(value)
        except argparse.ArgumentTypeError:
            continue
        assert incus_regression.target_lock_path(repo, name, "avr-wt-demo").parent == locks


def test_reconcile_decides_under_the_mapping_lock_even_when_writing_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The listing, the classification and the write are one decision. A dry run
    # still takes the real lock: every read it makes is real, and its report is
    # what the next `--yes` acts on.
    held = []
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={"gone": {"path": str(tmp_path / "checkout"), "host_port": 52908}},
        projects=(),
        instances=(),
    )

    @contextlib.contextmanager
    def recording_lock(repo_root, *, dry_run):
        held.append(dry_run)
        yield

    monkeypatch.setattr(incus_regression, "worktree_mapping_lock", recording_lock)
    incus_regression.cmd_reconcile(argparse.Namespace(yes=False, dry_run=True, remote=None))

    assert held == [False]
    assert set(json.loads(mapping_path.read_text(encoding="utf-8"))["worktrees"]) == {"gone"}


def test_a_command_takes_the_mapping_lock_only_for_the_daemon_it_describes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock on `worktrees.json` is a claim about one authority, like its rows.

    The commands had learned that about the rows without learning it about the
    lock. `reconcile --remote` held it across two listings of another daemon,
    where this file exposes no rows and writes none, so a slow or unreachable
    remote blocked every local `up` from reserving a port for as long as the
    listing took -- protecting nothing, since nothing of this file's was in the
    span. `up --remote` holds the same span for the same reason.

    Asserted for both commands, in both authorities, and from inside the span
    rather than at the call that opens it: what matters is whether the lock is
    really held while the command is waiting on a daemon.
    """
    held: list[bool] = []
    reconcile_fixture(tmp_path, monkeypatch, entries={}, projects=(), instances=())

    def record_and_report_nothing(runner, metadata):
        held.append(bool(incus_regression._held_mapping_locks))
        return []

    def record_and_stop(*call_args, **kwargs):
        held.append(bool(incus_regression._held_mapping_locks))
        raise RuntimeError("far enough")

    monkeypatch.setattr(incus_regression, "worktree_environments", record_and_report_nothing)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "resolve_target", record_and_stop)

    for remote in (None, "lab"):
        incus_regression.cmd_reconcile(argparse.Namespace(yes=False, dry_run=False, remote=remote))
        with pytest.raises(RuntimeError):
            # `up` names its update lock from the arguments alone, so the stub
            # carries the identity even though the mapping is never reached.
            incus_regression.cmd_up(
                argparse.Namespace(env_file=None, dry_run=False, remote=remote, target="worktree", slug="demo")
            )

    assert held == [True, True, False, False]


def test_reconcile_against_a_remote_reports_one_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report about one daemon annotates nothing from a file about another.

    `worktrees.json` reserves ports on this machine and records what this
    machine's daemon holds, so a remote inventory and these rows are two
    authorities. Reading them into one report attributes a local row's port,
    branch, and commit to whatever remote environment happens to share its slug,
    and lists local-only rows as environments the remote has lost. So the rows
    are absent from a remote report entirely -- said once, rather than implied by
    every line reading "no runner metadata" -- and `--yes` has nothing to drop.

    The `delete` commands it prints carry `--remote`, or they would name the same
    slug on the wrong daemon.
    """
    mapping_path, _ = reconcile_fixture(
        tmp_path,
        monkeypatch,
        entries={
            # Same slug on both daemons: the case where lending provenance across
            # authorities produces a plausible, wrong line rather than a visible
            # mismatch.
            "elsewhere": {"path": str(tmp_path / "checkout"), "host_port": 52909, "branch": "local/work"},
            "local-only": {"path": str(tmp_path / "checkout"), "host_port": 52910},
        },
        projects=("avr-wt-elsewhere",),
        instances=({"name": "avibe-wt-elsewhere", "status": "Running"},),
    )

    exit_code = incus_regression.cmd_reconcile(
        argparse.Namespace(yes=True, dry_run=False, remote="lab")
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "--slug elsewhere --yes --remote lab" in out
    assert "Runner metadata is not shown: it describes the local Incus daemon, not remote lab." in out
    assert "52909" not in out
    assert "local/work" not in out
    assert "local-only" not in out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert set(payload["worktrees"]) == {"elsewhere", "local-only"}


def test_delete_round_trips_generated_worktree_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = repo / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    slug = "fix-regression-source-id-3200ccd6"
    project = f"avr-wt-{slug}"
    instance = f"avibe-wt-{slug}"
    mapping_path = runtime / "worktrees.json"
    mapping_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "worktrees": {
                    slug: {
                        "path": str(tmp_path / "missing"),
                        "project": project,
                        "instance": instance,
                        "host_port": 15205,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda _repo_root: repo)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda _repo_root, _env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", RecordingRunner)

    exit_code = incus_regression.cmd_delete(
        argparse.Namespace(
            target="worktree",
            slug=slug,
            env_file=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
            dry_run=False,
            remote=None,
            yes=True,
        )
    )

    assert exit_code == 0
    assert commands == [
        ["incus", "--project", project, "delete", instance, "--force"],
        ["incus", "project", "delete", project],
    ]
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {}


def test_delete_against_a_remote_keeps_the_local_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A removal somewhere else is no evidence about the row describing this machine.

    `worktrees.json` reserves host ports here and records what this machine's
    daemon holds. The same slug can be in use on both daemons, so dropping the
    local row because the remote copy was deleted releases a port a live local
    environment is bound to.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = repo / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    slug = "shared-slug"
    row = {"path": str(repo), "project": f"avr-wt-{slug}", "instance": f"avibe-wt-{slug}", "host_port": 15207}
    mapping_path = runtime / "worktrees.json"
    mapping_path.write_text(json.dumps({"schema_version": 1, "worktrees": {slug: row}}), encoding="utf-8")

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda _repo_root: repo)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda _repo_root, _env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", RecordingRunner)

    exit_code = incus_regression.cmd_delete(
        argparse.Namespace(
            target="worktree",
            slug=slug,
            env_file=None,
            host_port=None,
            ui_host="127.0.0.1",
            ui_port=5123,
            worktree_port_start=15200,
            worktree_port_end=15399,
            dry_run=False,
            remote="lab",
            yes=True,
        )
    )

    assert exit_code == 0
    assert "Kept the local metadata" in capsys.readouterr().out
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {slug: row}


def _delete_args(slug: str, **overrides) -> argparse.Namespace:
    fields = {
        "target": "worktree",
        "slug": slug,
        "env_file": None,
        "host_port": None,
        "ui_host": "127.0.0.1",
        "ui_port": 5123,
        "worktree_port_start": 15200,
        "worktree_port_end": 15399,
        "dry_run": False,
        "remote": None,
        "yes": True,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


def test_deleting_a_slug_another_run_holds_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live run owns its slug, so a delete of it stops instead of taking half of it.

    The half is what makes this worth refusing rather than racing. An `up`
    reserves under this lock and builds for minutes inside it, so a delete
    arriving in that window finds no objects to remove and would remove the row
    anyway -- and `complete` will not restore a row that is no longer the one its
    run reserved, so the environment would finish built, untracked, with its host
    port free for the next slug to take.
    """
    repo = tmp_path / "repo"
    (repo / ".runtime" / "incus-regression").mkdir(parents=True)
    slug = "held-by-someone-else"
    row = {"path": str(repo), "project": f"avr-wt-{slug}", "instance": f"avibe-wt-{slug}", "host_port": 15211}
    mapping_path = repo / ".runtime" / "incus-regression" / "worktrees.json"
    mapping_path.write_text(json.dumps({"schema_version": 1, "worktrees": {slug: row}}), encoding="utf-8")
    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda _repo_root: repo)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda _repo_root, _env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", RecordingRunner)

    with incus_regression.target_update_lock(repo, None, f"avr-wt-{slug}", dry_run=False):
        with pytest.raises(incus_regression.RegressionError) as excinfo:
            incus_regression.cmd_delete(_delete_args(slug))

    assert "Another run holds the regression update lock" in str(excinfo.value)
    assert commands == []
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {slug: row}


def test_delete_holds_the_slug_lock_across_the_objects_and_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stated as "the lock is held while each half changes", not as where the `with` sits.

    Both halves are one change to what the slug names, so an interruption between
    them is what has to be impossible -- an environment removed with its row kept
    strands a port, and a row removed with the environment kept hides one. The
    probe is the same one `reconcile` uses, which cannot be satisfied by holding
    the lock somewhere nearby: `flock` conflicts with this process too, so it
    answers for the moment it is asked.
    """
    repo = tmp_path / "repo"
    (repo / ".runtime" / "incus-regression").mkdir(parents=True)
    slug = "deleted-under-lock"
    project = f"avr-wt-{slug}"
    row = {"path": str(repo), "project": project, "instance": f"avibe-wt-{slug}", "host_port": 15212}
    mapping_path = repo / ".runtime" / "incus-regression" / "worktrees.json"
    mapping_path.write_text(json.dumps({"schema_version": 1, "worktrees": {slug: row}}), encoding="utf-8")
    observed = []

    def held() -> bool:
        return incus_regression.target_run_in_flight(repo, None, project)

    class ProbingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, **kwargs):
            observed.append(("objects", held()))
            return subprocess.CompletedProcess(command, 0)

    forget = incus_regression.WorktreeMetadata.forget

    def probing_forget(self, slugs):
        observed.append(("row", held()))
        return forget(self, slugs)

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: repo)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda _repo_root: repo)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda _repo_root, _env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ProbingRunner)
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "forget", probing_forget)

    assert incus_regression.cmd_delete(_delete_args(slug)) == 0

    assert observed == [("objects", True), ("objects", True), ("row", True)]
    # Not vacuous: the same probe is False once the command has released it.
    assert held() is False
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert payload["worktrees"] == {}


def test_metadata_about_another_daemon_neither_reads_nor_writes_the_local_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every accessor is bound to the daemon it describes, so `--remote` reaches nothing.

    Stated over the accessor rather than over the commands that use it. A
    predicate each command was expected to consult came first, and being a
    question is what made it forgettable: `up --remote` never asked, and so it
    reserved a host port on this machine, overwrote whatever live local row shared
    its slug, and left that reservation behind for good. Asserting it here covers
    every present and future caller, because the file has no other way in.
    """
    repo = tmp_path / "repo"
    runtime = repo / ".runtime" / "incus-regression"
    runtime.mkdir(parents=True)
    mapping_path = runtime / "worktrees.json"
    recorded = {
        "schema_version": 1,
        "worktrees": {"demo": {"host_port": 15234, "branch": "local/work", "claim": "seeded"}},
    }
    mapping_path.write_text(json.dumps(recorded), encoding="utf-8")
    monkeypatch.setattr(incus_regression, "git_common_root", lambda _repo_root: repo)
    before = mapping_path.read_bytes()

    metadata = incus_regression.WorktreeMetadata(repo, "lab")
    target = incus_regression.RegressionTarget(
        target="worktree",
        slug="demo",
        project="avr-wt-demo",
        instance="avibe-wt-demo",
        host_port=15234,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    assert metadata.owned is False
    assert metadata.rows() == {}
    assert metadata.allocated_ports() == set()
    assert metadata.port_for("demo") is None

    # The seeded row carries the claim these writes name, so each of them would
    # land if authority were ignored -- the file is unchanged because of who the
    # accessor is bound to, not because the writes had nothing to match.
    metadata.reserve(target)
    metadata.complete(target, "seeded")
    metadata.release(target, "seeded")
    metadata.forget(["demo"])
    metadata.mutate(lambda worktrees: worktrees.clear())

    assert mapping_path.read_bytes() == before

    # The mirror: the same calls through the owning accessor do reach the file,
    # so the reads and writes above are scoped by authority rather than broken.
    owner = incus_regression.WorktreeMetadata(repo)
    assert owner.port_for("demo") == 15234
    owner.forget(["demo"])
    assert json.loads(mapping_path.read_text(encoding="utf-8"))["worktrees"] == {}


def test_the_mapping_file_has_exactly_one_way_in() -> None:
    """`worktrees.json` is reachable only through the accessor bound to its daemon.

    The enumeration is asserted rather than described, so a sixth access point
    added outside the owner fails here instead of costing a review round. This is
    the whole reason the authority check cannot be forgotten: a caller has no name
    for the file, only for an accessor that already knows which daemon it is
    evidence about.
    """
    import ast

    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WorktreeMetadata"
    )
    inside = {id(node) for node in ast.walk(owner)}
    readers = {"_load_worktree_mapping", "_write_worktree_mapping"}
    strays = sorted(
        {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in readers and id(node) not in inside
        }
    )

    assert strays == [], f"reached outside WorktreeMetadata: {strays}"


def test_the_mapping_lock_nests_without_deadlocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`flock` is owned by an open file description, not by a process.

    A writer that takes the lock itself is nested inside every command that
    already holds it across a decision, so a second `open` plus `flock` would
    block forever on a lock this very process holds -- and a deadlocked runner
    looks like a hung daemon, not like a bug here.
    """
    repo = tmp_path / "repo"
    (repo / ".runtime" / "incus-regression").mkdir(parents=True)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda _repo_root: repo)
    written = []

    with incus_regression.worktree_mapping_lock(repo, dry_run=False):
        incus_regression.WorktreeMetadata(repo).mutate(lambda worktrees: worktrees.update({"nested": {"host_port": 1}}))
        written.append(json.loads((repo / ".runtime" / "incus-regression" / "worktrees.json").read_text(encoding="utf-8")))

    assert written == [{"schema_version": 1, "worktrees": {"nested": {"host_port": 1}}}]
    # The outer span released the lock on the way out, so the next acquisition is
    # a real one rather than a leaked no-op.
    with incus_regression.worktree_mapping_lock(repo, dry_run=False):
        pass


def test_up_skips_host_port_preflight_for_existing_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_ui_builder_stage(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ui" / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "ui" / "src").mkdir()

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: (_ for _ in ()).throw(AssertionError("should not preflight existing instance")))
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "stop_service_for_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_runtime_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "sync_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", lambda *args, **kwargs: set())
    monkeypatch.setattr(incus_regression, "run_prepare_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "restart_and_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0


def test_up_defers_master_port_preflight_until_after_instance_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ui_builder_stage(tmp_path)

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def fail_if_resolve_target_preflights(repo_root, ui_host, start, end, *, dry_run, preflight):
        if preflight:
            raise AssertionError("master target resolution must not preflight ports")
        return start

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "allocate_worktree_port", fail_if_resolve_target_preflights)
    monkeypatch.setattr(
        incus_regression,
        "ensure_host_port_available",
        lambda host, port: (_ for _ in ()).throw(AssertionError("should not preflight existing master instance")),
    )
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "stop_service_for_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_runtime_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "sync_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", lambda *args, **kwargs: set())
    monkeypatch.setattr(incus_regression, "run_prepare_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "restart_and_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0


def test_up_checks_host_port_preflight_for_new_local_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NewRemoteRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing()

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRemoteRunner)
    preflight_calls = []
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: preflight_calls.append((host, port)))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "stop_service_for_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_runtime_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", lambda *args, **kwargs: set())
    monkeypatch.setattr(incus_regression, "run_prepare_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "write_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "restart_and_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", lambda *args, **kwargs: None)

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert preflight_calls == [("127.0.0.1", 15130)]


def test_up_stops_when_the_daemon_cannot_say_whether_the_target_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable or stalled daemon aborts `up` before it touches anything.

    This is the shape that broke a real run: a starved daemon failed the instance
    lookup, the runner read that as "instance absent", and the host-port preflight
    then tripped over the port the still-live instance's own proxy device holds.
    The port was never the problem — the unanswered question was.
    """
    touched = []

    def refuse(name):
        def wrapper(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"{name} ran while the daemon's answer was unknown")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(
        incus_regression.subprocess,
        "run",
        stub_incus_result(1, stderr="Error: Failed to begin transaction: context deadline exceeded\n"),
    )
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", refuse("ensure_host_port_available"))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", refuse("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", refuse("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", refuse("stop_service_for_update"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    with pytest.raises(incus_regression.RegressionError, match="context deadline exceeded"):
        incus_regression.cmd_up(args)

    assert touched == []


def test_up_checks_seed_env_before_target_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class NewRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing()

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()
            if name == "require_runtime_seed_env":
                raise SystemExit("missing")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    with pytest.raises(SystemExit):
        incus_regression.cmd_up(args)

    assert calls == ["require_runtime_seed_env"]


def test_up_checks_platform_seed_env_before_existing_reset_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()
            if name == "require_runtime_seed_env":
                raise SystemExit("missing platform")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="config",
    )

    with pytest.raises(SystemExit):
        incus_regression.cmd_up(args)

    assert calls == ["require_runtime_seed_env"]


def test_up_rejects_paired_master_reset_before_instance_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout='{"state": "paired"}')

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="config",
        allow_reset_paired_master=False,
    )

    with pytest.raises(incus_regression.RegressionError, match="pairing state is present"):
        incus_regression.cmd_up(args)

    assert calls == ["require_runtime_seed_env"]


def test_up_dry_run_does_not_require_seed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class DryRunRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing()

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()
            if name == "require_runtime_seed_env":
                raise AssertionError("dry-run should not require seed secrets")

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "Runner", DryRunRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", record("require_runtime_seed_env"))
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", record("complete_worktree_metadata"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=True,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert "require_runtime_seed_env" not in calls
    assert not (tmp_path / ".runtime" / "incus-regression" / "worktrees.json").exists()


def test_up_stops_old_service_before_mutating_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    env_file = tmp_path / ".env.regression"
    env_file.write_text("OPENAI_API_KEY=set\n", encoding="utf-8")

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "migrate_legacy_backend_runtimes", record("migrate_legacy_backend_runtimes"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", record("complete_worktree_metadata"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert calls[:4] == [
        "ensure_project_and_instance",
        "stop_service_for_update",
        "write_runtime_env",
        "migrate_legacy_backend_runtimes",
    ]
    assert calls.index("stop_service_for_update") < calls.index("migrate_legacy_backend_runtimes")
    assert calls.index("migrate_legacy_backend_runtimes") < calls.index("sync_source")
    assert calls.index("sync_source") < calls.index("update_dependencies_and_build")
    assert calls.index("normalize_runtime_config") < calls.index("restart_and_verify")
    assert calls.index("prepare_show_runtime") < calls.index("restart_and_verify")


def test_up_preserves_runtime_env_when_existing_target_has_no_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", record("complete_worktree_metadata"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert "write_runtime_env" not in calls
    assert calls.index("stop_service_for_update") < calls.index("sync_source")


def test_up_rewrites_runtime_env_when_env_file_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    env_file = tmp_path / ".env.regression"
    env_file.write_text("OPENAI_API_KEY=set\n", encoding="utf-8")

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()

        return wrapper

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "complete", record("complete_worktree_metadata"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0
    assert "write_runtime_env" in calls


def test_up_reserves_worktree_port_under_both_locks_that_protect_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every row `up` writes is written under the mapping lock and its slug's lock.

    Two locks, two properties. The mapping lock makes a read-modify-write of
    `worktrees.json` atomic. The slug's update lock is what tells `reconcile` this
    run exists at all, so a row written outside it is a row a concurrent
    `reconcile --yes` may prune while this run is still building against it -- and
    a second `up` on the same slug could merge its own port over this one's before
    either had finished. Recording the depth each write saw states that as one
    property over all writers, rather than naming the two that exist today.
    """
    calls = []
    held = []
    building = []
    locked = []
    writes = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing("avr-wt-demo-branch", "avibe-wt-demo-branch")

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def mapping_lock(repo_root, *, dry_run):
        class Lock:
            def __enter__(self):
                calls.append("mapping_lock_enter")
                held.append("lock")

            def __exit__(self, exc_type, exc, tb):
                held.pop()
                calls.append("mapping_lock_exit")

        return Lock()

    # Records the depth every write sees rather than naming the writers, so a
    # write added later is covered without editing this test.
    original_write = incus_regression._write_worktree_mapping

    def recording_write(repo_root, payload):
        writes.append((len(held), len(building)))
        original_write(repo_root, payload)

    monkeypatch.setattr(incus_regression, "_write_worktree_mapping", recording_write)

    def target_lock(repo_root, remote, project, *, dry_run):
        class Lock:
            def __enter__(self):
                calls.append("target_lock_enter")
                locked.append(project)
                building.append(project)

            def __exit__(self, exc_type, exc, tb):
                building.pop()
                calls.append("target_lock_exit")

        return Lock()

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "update_dependencies_and_build":
                return set()

        return wrapper

    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    # Port allocation probes real host ports, so pin the preflight: this test is
    # about reserving the mapping under the lock, not about which ports happen to
    # be free on the machine running it.
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: None)
    monkeypatch.setattr(incus_regression, "worktree_mapping_lock", mapping_lock)
    original_reserve = incus_regression.WorktreeMetadata.reserve

    def reserve(self, target, **kwargs):
        calls.append("reserve_worktree_metadata")
        return original_reserve(self, target, **kwargs)

    monkeypatch.setattr(incus_regression, "target_update_lock", target_lock)
    monkeypatch.setattr(incus_regression.WorktreeMetadata, "reserve", reserve)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", record("update_dependencies_and_build"))
    monkeypatch.setattr(incus_regression, "run_prepare_state", record("run_prepare_state"))
    monkeypatch.setattr(incus_regression, "normalize_runtime_config", record("normalize_runtime_config"))
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))
    monkeypatch.setattr(incus_regression, "restart_and_verify", record("restart_and_verify"))
    monkeypatch.setattr(incus_regression, "prepare_show_runtime", record("prepare_show_runtime"))

    args = argparse.Namespace(
        target="worktree",
        slug="demo-branch",
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    assert incus_regression.cmd_up(args) == 0

    assert calls[:3] == ["target_lock_enter", "mapping_lock_enter", "reserve_worktree_metadata"]
    # The reservation and the completion stamp are two writes, and `up` releases
    # the mapping lock between them, so the mapping's own writer has to take it.
    # Both depths are asserted for every write: whoever performs it, no write to
    # `worktrees.json` happens outside the mapping lock, and none happens outside
    # the lock that proves this run is the one holding the slug.
    assert len(writes) == 2
    assert all(mapping_depth >= 1 and slug_depth >= 1 for mapping_depth, slug_depth in writes)
    payload = json.loads((tmp_path / ".runtime" / "incus-regression" / "worktrees.json").read_text(encoding="utf-8"))
    mapping = payload["worktrees"]["demo-branch"]
    assert mapping["host_port"] == 15200
    assert mapping["project"] == "avr-wt-demo-branch"
    assert "updated_at" in mapping
    # The lock is named before the row exists, so it is asserted against the row:
    # a lock on any other name would be held while a different environment built.
    assert locked == [mapping["project"]]


@pytest.mark.parametrize("holds_project", [False, True], ids=["daemon-empty", "daemon-holds-project"])
@pytest.mark.parametrize("recorded", [False, True], ids=["new-row", "existing-row"])
@pytest.mark.parametrize(
    ("failure_type", "recovery_error"),
    [
        (RuntimeError, None),
        (KeyboardInterrupt, None),
        (RuntimeError, OSError),
        (RuntimeError, KeyboardInterrupt),
        (RuntimeError, subprocess.CalledProcessError),
    ],
)
@pytest.mark.parametrize(
    "fail_at",
    [
        "require_runtime_seed_env",
        "ensure_project_and_instance",
        "stop_service_for_update",
        "sync_source",
        "prepare_show_runtime",
        "restart_and_verify",
        "health_check",
    ],
)
def test_up_gives_its_row_back_only_when_the_daemon_says_nothing_came_of_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_at: str,
    recorded: bool,
    holds_project: bool,
    failure_type: type[BaseException],
    recovery_error: type[BaseException] | None,
) -> None:
    """A failed `up` drops its row exactly when the daemon holds no project for it.

    The expectation is derived from what the daemon reports, not from a table of
    steps, because that is the whole rule: releasing is safe while nothing binds
    this port, and only the daemon knows. Asserting the step instead would be
    asserting a prediction -- a run that fails inside
    `ensure_project_and_instance` may have created the project or may have
    failed on the listing that decides whether to, and those are opposite
    answers from one step.

    The row's own age is on the same assertion for the same reason. It used to
    decide the outcome, on the theory that a pre-existing row is a claim older
    than the run -- but "older" was standing in for "something exists", and here
    those come apart: a row whose environment was deleted behind the runner's
    back is not a claim about anything, while a project the daemon holds must
    keep its port whether this run recorded it or not.

    Left behind, an abandoned reservation reads as one still in flight, which
    `reconcile` refuses to prune by design, so its port stayed reserved until
    someone deleted the slug by hand.
    """
    calls = []
    recovery_locks = []
    service_commands = []
    restart_and_verify = incus_regression.restart_and_verify
    original_error = failure_type(f"{fail_at} failed")
    inventory = ("avr-wt-demo-branch",) if holds_project else ()

    class NewRunner(incus_regression.Runner):
        names = daemon_listing(*inventory)

    def run_command(command, **kwargs):
        script = command[-1]
        service_commands.append(script)
        if "/health" in script:
            raise original_error
        if script in {
            "systemctl daemon-reload",
            f"systemctl enable --now {incus_regression.SERVICE_NAME}",
            f"systemctl restart {incus_regression.SERVICE_NAME}",
        }:
            return subprocess.CompletedProcess(command, 0)
        calls.append("restart_service_after_failed_update")
        recovery_locks.append(incus_regression.target_run_in_flight(tmp_path, None, "avr-wt-demo-branch"))
        assert command[-1] == f"systemctl start {incus_regression.SERVICE_NAME}"
        assert kwargs["check"] is True
        if recovery_error is subprocess.CalledProcessError:
            raise subprocess.CalledProcessError(1, command)
        if recovery_error is not None:
            raise recovery_error("recovery failed")
        return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)
            if name == "restart_and_verify" and fail_at == "health_check":
                calls.append("health_check")
                return restart_and_verify(*args, **kwargs)
            if name == fail_at:
                raise original_error
            if name == "update_dependencies_and_build":
                return set()

        return wrapper

    mapping_path = tmp_path / ".runtime" / "incus-regression" / "worktrees.json"
    if recorded:
        mapping_path.parent.mkdir(parents=True)
        mapping_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "worktrees": {
                        "demo-branch": {
                            "path": str(tmp_path),
                            "project": "avr-wt-demo-branch",
                            "instance": "avibe-wt-demo-branch",
                            "host_port": 15200,
                            "updated_at": "2026-08-01T00:00:00+00:00",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRunner)
    monkeypatch.setattr(incus_regression.subprocess, "run", run_command)
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {})
    for name in (
        "require_runtime_seed_env",
        "guard_paired_master_reset",
        "ensure_project_and_instance",
        "stop_service_for_update",
        "write_runtime_env",
        "migrate_legacy_backend_runtimes",
        "sync_source",
        "invalidate_fingerprints",
        "update_dependencies_and_build",
        "run_prepare_state",
        "normalize_runtime_config",
        "write_metadata",
        "prepare_show_runtime",
        "restart_and_verify",
    ):
        monkeypatch.setattr(incus_regression, name, record(name))

    args = argparse.Namespace(
        target="worktree",
        slug="demo-branch",
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    with pytest.raises(failure_type) as raised:
        incus_regression.cmd_up(args)

    assert raised.value is original_error
    assert fail_at in calls
    recovery_calls = calls.count("restart_service_after_failed_update")
    assert recovery_calls == int("stop_service_for_update" in calls)
    if fail_at == "health_check":
        assert service_commands.count(f"systemctl restart {incus_regression.SERVICE_NAME}") == 1
        assert sum("/health" in script for script in service_commands) == 1
    if incus_regression.fcntl is not None:
        assert recovery_locks == [True] * recovery_calls
    assert not incus_regression.target_run_in_flight(tmp_path, None, "avr-wt-demo-branch")
    stderr = capsys.readouterr().err
    if recovery_calls and recovery_error is not None:
        assert "Could not restart avibe-regression.service after failed update" in stderr
        assert recovery_error.__name__ in stderr
    else:
        assert not stderr
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))["worktrees"]
    assert ("demo-branch" in mapping) == holds_project
    if holds_project:
        # A project the daemon holds keeps its port, and `--host-port`-free
        # reuse of a recorded one is the reason the row exists at all.
        assert mapping["demo-branch"]["host_port"] == 15200


def test_up_leaves_behind_a_row_another_run_has_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `up` must not release a row a concurrent `up` now owns.

    `reserve` merges over whatever row it finds, so two `up` commands on one slug
    are ordered by the mapping lock and not excluded by it: the second takes the
    row while the first is still between its reserve and its failure. Everything
    else here says release -- the daemon reports nothing, so nothing binds the
    port, and the first run is the one that brought the row into existence. Only
    the claim recorded in the row says otherwise, which is why it is the row and
    not the run that gets asked.
    """
    claims = []

    class NewRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing()

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def take_over(*call_args, **kwargs):
        other = incus_regression.resolve_target(args, tmp_path, dry_run=False)
        claims.append(incus_regression.WorktreeMetadata(tmp_path, None).reserve(other).claim)
        raise RuntimeError("taken over")

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRunner)
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", take_over)

    args = argparse.Namespace(
        target="worktree",
        slug="demo-branch",
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    with pytest.raises(RuntimeError):
        incus_regression.cmd_up(args)

    mapping = json.loads(
        (tmp_path / ".runtime" / "incus-regression" / "worktrees.json").read_text(encoding="utf-8")
    )["worktrees"]
    assert mapping["demo-branch"]["claim"] == claims[0]
    assert mapping["demo-branch"]["host_port"] == 15200


def test_no_end_of_a_reservation_writes_over_a_row_another_run_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write a reservation performs stops at a row that is not its own.

    Enumerated from the class rather than listed, because listing is how this got
    in: the comparison was added to the release while completion kept writing
    unconditionally, so an `up` that finished first replaced a newer run's row --
    and with it that run's port and its claim, leaving the port allocated to a
    row that no longer records it. A write added to the class later is covered
    here without editing this test, and one needing an argument this test cannot
    supply fails it instead of being skipped in silence.
    """
    import inspect

    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    mapping_path = tmp_path / ".runtime" / "incus-regression" / "worktrees.json"
    mapping_path.parent.mkdir(parents=True)

    def target_on(port: int) -> incus_regression.RegressionTarget:
        return incus_regression.RegressionTarget(
            target="worktree",
            slug="demo",
            project="avr-wt-demo",
            instance="avibe-wt-demo",
            host_port=port,
            ui_host="127.0.0.1",
            ui_port=5123,
        )

    class NewRunner:
        # The daemon holds nothing, so nothing but the claim can stop a write.
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing()

    mine = incus_regression.WorktreeMetadata(tmp_path, None).reserve(target_on(15200))
    theirs = incus_regression.WorktreeMetadata(tmp_path, None).reserve(target_on(15201))
    assert mine.claim and theirs.claim and mine.claim != theirs.claim

    writes = sorted(
        name
        for name, value in vars(incus_regression.WorktreeReservation).items()
        if callable(value) and not name.startswith("_")
    )
    assert writes, "the reservation performs no writes, so this test proves nothing"
    for name in writes:
        method = getattr(mine, name)
        parameters = set(inspect.signature(method).parameters)
        unknown = parameters - {"runner"}
        assert not unknown, f"{name} takes {sorted(unknown)}: teach this test how to call it"
        method(**({"runner": NewRunner()} if "runner" in parameters else {}))

    rows = json.loads(mapping_path.read_text(encoding="utf-8"))["worktrees"]
    assert rows["demo"]["claim"] == theirs.claim
    assert rows["demo"]["host_port"] == 15201


def test_a_reserved_row_still_reports_the_environment_that_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taking a slug over does not restate what is built there.

    `reserve` merges into whatever row it finds, so every field it writes lands
    beside the previous run's. Build identity is therefore written only by
    `complete`, the end of the reservation that has one: a reservation that wrote
    its own branch produced a row naming the new branch next to the old commit --
    an environment that never existed -- and the report that row feeds is what an
    operator reads before deciding whether the environment is still wanted.
    """
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "branch_name", lambda repo_root: "fix/installed")
    monkeypatch.setattr(incus_regression, "commit_sha", lambda repo_root: "aaaaaaaaaaaa")
    (tmp_path / ".runtime" / "incus-regression").mkdir(parents=True)
    target = incus_regression.RegressionTarget(
        target="worktree",
        slug="demo",
        project="avr-wt-demo",
        instance="avibe-wt-demo",
        host_port=15300,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    metadata = incus_regression.WorktreeMetadata(tmp_path, None)
    built = metadata.reserve(target)
    metadata.complete(target, built.claim)

    monkeypatch.setattr(incus_regression, "branch_name", lambda repo_root: "fix/arriving")
    taking_over = metadata.reserve(target)

    row = metadata.rows()["demo"]
    assert row["claim"] == taking_over.claim
    assert row["commit"] == "aaaaaaaaaaaa"
    described = incus_regression.describe_worktree_entry(row)
    assert "fix/installed" in described
    assert "fix/arriving" not in described


def test_an_empty_remote_names_the_local_daemon_for_every_command() -> None:
    """`--remote ""` is this machine's daemon everywhere, not two authorities at once.

    `remote_ref` has always read an empty name as local, while the metadata
    accessor read the argument itself and called it another daemon, so an
    unexpanded `--remote "$INCUS_REMOTE"` created the environment here and
    recorded nothing about it: the host port allocated with no row naming it.
    Either reader could have been taught the other's rule, which is why the value
    is normalized once where it is defined, and why the property is asserted over
    every command discovered from the parser rather than over a list written here.
    """
    parser = incus_regression.build_parser()
    subcommands = parser._subparsers._group_actions[0].choices
    checked = [
        name
        for name, sub in subcommands.items()
        if any("--remote" in action.option_strings for action in sub._actions)
    ]
    assert checked, "no subcommand accepts --remote"

    for name in checked:
        for spelling in ("", "   "):
            assert parser.parse_args([name, "--remote", spelling]).remote is None, name
        assert parser.parse_args([name, "--remote", " lab "]).remote == "lab", name

    # The consequence, at the reader that used to disagree with `remote_ref`.
    empty = parser.parse_args(["up", "--remote", ""])
    assert incus_regression.WorktreeMetadata(Path("/nonexistent"), empty.remote).owned is True


def test_up_releases_its_reservation_when_the_run_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C is how an `up` is abandoned in practice, and a KeyboardInterrupt is
    # not an Exception -- catching only that class would leave exactly the row
    # this release exists for.
    class NewRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing()

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    monkeypatch.setattr(incus_regression, "load_env_file", lambda repo_root, env_file: None)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "ensure_host_port_available", lambda host, port: None)
    monkeypatch.setattr(incus_regression, "Runner", NewRunner)
    monkeypatch.setattr(incus_regression, "require_runtime_seed_env", interrupt)

    args = argparse.Namespace(
        target="worktree",
        slug="demo-branch",
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    with pytest.raises(KeyboardInterrupt):
        incus_regression.cmd_up(args)

    mapping = json.loads(
        (tmp_path / ".runtime" / "incus-regression" / "worktrees.json").read_text(encoding="utf-8")
    )["worktrees"]
    assert mapping == {}


def test_normalize_runtime_config_updates_preserved_backend_paths_host_and_port() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=6123,
    )

    incus_regression.normalize_runtime_config(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "sudo -H -u avibe" in joined
    assert (
        "/opt/avibe/venv/bin/python scripts/prepare_regression.py "
        "--normalize-config /home/avibe/.avibe/config/config.json"
    ) in joined
    assert "ui.get(\"setup_host\") != '127.0.0.1'" in joined
    assert 'ui.get("setup_port") != 6123' in joined
    assert 'ui["setup_port"] = 6123' in joined


def test_stop_service_for_update_ignores_missing_service() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, *, check=True, **kwargs):
            commands.append((command, check))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.stop_service_for_update(RecordingRunner(), target, remote=None)

    joined = " ".join(commands[0][0])
    assert "systemctl stop avibe-regression.service || true" in joined
    assert commands[0][1] is False


def test_migrate_legacy_backend_runtimes_uses_user_owned_layout() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.migrate_legacy_backend_runtimes(RecordingRunner(), target, remote=None)

    joined = " ".join(commands[0])
    assert "sudo -H -u avibe" in joined
    assert '[ ! -x "$user_bin/claude" ]' in joined
    assert '[ ! -x "$user_bin/codex" ]' in joined
    assert '[ ! -x "$user_bin/opencode" ]' in joined
    assert 'npm config set prefix "$npm_prefix" --location=user' in joined
    assert 'npm install --global --prefix "$npm_prefix" "${npm_packages[@]}"' in joined
    assert "HOME=/home/avibe bash -s -- --no-modify-path" in joined
    assert "/home/avibe/.npm-global/bin/claude" not in joined
    assert "/home/avibe/.npm-global/bin/codex" not in joined


def test_update_builds_ui_before_editable_install() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=True,
        force_ui=False,
        remote=None,
    )

    install_index = next(i for i, command in enumerate(commands) if "pip install -e ." in command)
    build_index = next(i for i, command in enumerate(commands) if "npm run build" in command)
    assert build_index < install_index


@pytest.mark.parametrize("venv_exists", [False, True])
def test_python_environment_is_created_by_its_owner_and_reinstalled_only_when_needed(venv_exists: bool) -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            result = 0 if venv_exists else 1
            return subprocess.CompletedProcess(command, result if "pyvenv.cfg" in commands[-1] else 0)

    target = incus_regression.RegressionTarget("master", "master", "avr-master", "avibe-master", 15130, "127.0.0.1", 5123)
    fingerprints = {"python": "p", "ui_deps": "d", "ui_source": "s"}
    incus_regression.update_dependencies_and_build(
        RecordingRunner(), target, previous_fingerprints=fingerprints, next_fingerprints=fingerprints,
        force_deps=False, build_ui=True, force_ui=False, remote=None,
    )

    creation = [command for command in commands if "python3 -m venv" in command]
    assert len(creation) == (0 if venv_exists else 1)
    assert all("sudo -H -u avibe" in command for command in creation)
    assert any("pip install -e ." in command for command in commands) is not venv_exists
    assert all("chown" not in command for command in commands)


def test_python_environment_creation_failure_stops_deployment() -> None:
    commands = []

    class FailingRunner:
        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if "pyvenv.cfg" in joined:
                return subprocess.CompletedProcess(command, 1)
            assert "python3 -m venv" in joined
            assert "|| true" not in joined
            raise subprocess.CalledProcessError(1, command)

    target = incus_regression.RegressionTarget("master", "master", "avr-master", "avibe-master", 15130, "127.0.0.1", 5123)
    with pytest.raises(subprocess.CalledProcessError):
        incus_regression.update_dependencies_and_build(
            FailingRunner(), target, previous_fingerprints={}, next_fingerprints={},
            force_deps=False, build_ui=True, force_ui=False, remote=None,
        )
    assert len(commands) == 2


def test_force_ui_rebuilds_with_realtime_enabled_by_default() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=True,
        force_ui=True,
        remote=None,
    )

    joined = "\n".join(commands)
    assert "cd ui && npm ci" in joined
    assert "cd ui && npm run build" in joined
    assert "pip install -e ." not in joined


def run_ui_update(
    *,
    present: set[str],
    force_ui: bool = False,
    previous_fingerprints: dict | None = None,
    next_fingerprints: dict | None = None,
) -> str:
    """Drive update_dependencies_and_build with a chosen instance state.

    ``present`` names the artifacts a sync left behind: ``node_modules`` and/or
    ``dist``. Every other command succeeds. The fingerprints default to a pair
    that matches, so a caller that cares only about instance state gets the
    "nothing changed" case.
    """
    commands: list[str] = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if "ui/node_modules/.package-lock.json" in joined:
                return subprocess.CompletedProcess(command, 0 if "node_modules" in present else 1)
            if "test -d ui/dist" in joined:
                return subprocess.CompletedProcess(command, 0 if "dist" in present else 1)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    fingerprints = {"python": "p", "ui_deps": "d", "ui_source": "s"}
    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints=dict(previous_fingerprints if previous_fingerprints is not None else fingerprints),
        next_fingerprints=dict(next_fingerprints if next_fingerprints is not None else fingerprints),
        force_deps=False,
        build_ui=True,
        force_ui=force_ui,
        remote=None,
    )
    return "\n".join(commands)


def test_unchanged_ui_reuses_the_dependency_tree_and_bundle_a_sync_kept() -> None:
    joined = run_ui_update(present={"node_modules", "dist"})

    assert "cd ui && npm ci" not in joined
    assert "cd ui && npm run build" not in joined


def test_npm_ci_runs_when_the_dependency_tree_is_absent_however_the_fingerprint_reads() -> None:
    """``npm run build`` must never be asked to run without its dependencies.

    An unchanged ``ui_deps`` fingerprint describes a lockfile, not the tree
    installed from it; ``--clean`` and a fresh instance both leave the second
    missing while the first still matches.
    """
    joined = run_ui_update(present={"dist"})

    assert "cd ui && npm ci" in joined
    assert joined.index("cd ui && npm ci") < joined.index("cd ui && npm run build")


def test_missing_ui_dist_overrides_no_build_ui_before_editable_install() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            if "test -d ui/dist" in commands[-1]:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=False,
        force_ui=False,
        remote=None,
    )

    joined = "\n".join(commands)
    install_index = next(i for i, command in enumerate(commands) if "pip install -e ." in command)
    build_index = next(i for i, command in enumerate(commands) if "npm run build" in command)
    assert "test -d ui/dist && test -f ui/dist/index.html" in joined
    assert "cd ui && npm ci" in joined
    assert build_index < install_index


def reconcile_update(*, build_ui: bool, present: set[str]) -> set[str]:
    """The keys ``update_dependencies_and_build`` reports it reconciled."""

    class RecordingRunner:
        def run(self, command, **kwargs):
            joined = " ".join(command)
            if "ui/node_modules/.package-lock.json" in joined:
                return subprocess.CompletedProcess(command, 0 if "node_modules" in present else 1)
            if "test -d ui/dist" in joined:
                return subprocess.CompletedProcess(command, 0 if "dist" in present else 1)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    return incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={"python": "p", "ui_deps": "old", "ui_source": "old"},
        next_fingerprints={"python": "p", "ui_deps": "new", "ui_source": "new"},
        force_deps=False,
        build_ui=build_ui,
        force_ui=False,
        remote=None,
    )


def test_a_normal_update_reconciles_every_fingerprint_it_records(tmp_path: Path) -> None:
    """Seeded from the real key set, so a fingerprint added later is covered.

    Listing the keys here instead would pass forever while the new one silently
    went unreconciled.
    """
    write_ui_builder_stage(tmp_path)
    (tmp_path / "ui").mkdir()
    every_key = set(incus_regression.compute_fingerprints(tmp_path))

    reconciled = reconcile_update(build_ui=True, present={"node_modules", "dist"})

    # ``show_runtime`` belongs to prepare_show_runtime, which cmd_up runs
    # unconditionally; everything else is this function's to reconcile.
    assert reconciled | {"show_runtime"} == every_key


def test_no_build_ui_does_not_claim_the_ui_artifacts_it_never_touched() -> None:
    reconciled = reconcile_update(build_ui=False, present={"node_modules", "dist"})

    assert reconciled == {"python"}


def test_no_build_ui_still_claims_a_ui_it_had_to_build_anyway() -> None:
    """A missing dist overrides --no-build-ui, and then the record is honest."""
    reconciled = reconcile_update(build_ui=False, present={"node_modules"})

    assert reconciled == {"python", "ui_deps", "ui_source"}


def test_an_unreconciled_fingerprint_keeps_the_value_that_described_the_artifact() -> None:
    """The recorded fingerprint describes the artifact, not the synced source.

    Blessing the new source here is what would let the next update skip
    ``npm ci`` against a dependency tree installed from a different lockfile.
    """
    recorded = incus_regression.reconciled_fingerprints(
        {"python": "p1", "ui_deps": "d1", "ui_source": "s1"},
        {"python": "p2", "ui_deps": "d2", "ui_source": "s2"},
        {"python"},
    )

    assert recorded == {"python": "p2", "ui_deps": "d1", "ui_source": "s1"}


def test_an_unreconciled_fingerprint_with_no_history_stays_absent() -> None:
    """Absent reads as "rebuild", which is the safe answer for a first run."""
    recorded = incus_regression.reconciled_fingerprints(
        {},
        {"python": "p", "ui_deps": "d", "ui_source": "s"},
        {"python"},
    )

    assert recorded == {"python": "p"}


def test_a_no_build_ui_update_leaves_the_next_update_rebuilding_the_ui() -> None:
    """The whole point, end to end across two updates.

    A sync now keeps ``ui/node_modules``, so a stale tree survives a
    ``--no-build-ui`` update. Only the fingerprint the first update recorded
    decides whether the second one notices.
    """
    changed_lockfile = {"python": "p", "ui_deps": "new", "ui_source": "new"}
    first = reconcile_update(build_ui=False, present={"node_modules", "dist"})
    carried = incus_regression.reconciled_fingerprints(
        {"python": "p", "ui_deps": "old", "ui_source": "old"},
        changed_lockfile,
        first,
    )

    joined = run_ui_update(
        present={"node_modules", "dist"},
        previous_fingerprints=carried,
        next_fingerprints=changed_lockfile,
    )

    assert "cd ui && npm ci" in joined
    assert "cd ui && npm run build" in joined


def test_invalidating_the_record_reads_back_as_rebuild_everything(tmp_path: Path) -> None:
    """Executed rather than pattern-matched, so it proves the file really empties.

    ``read_existing_fingerprints`` turns whatever is on disk into the previous
    values every skip decision compares against, and "no keys" is the only
    shape that reads as "rebuild everything".
    """
    metadata_dir = tmp_path / "metadata"
    record = metadata_dir / "fingerprints.json"
    metadata_dir.mkdir()
    record.write_text(json.dumps({"python": "p", "ui_deps": "d", "ui_source": "s"}), encoding="utf-8")

    commands: list[list[str]] = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.invalidate_fingerprints(RecordingRunner(), target, remote=None)

    script = (
        commands[0][-1]
        .replace(incus_regression.FINGERPRINT_PATH, str(record))
        .replace(incus_regression.METADATA_DIR, str(metadata_dir))
    )
    subprocess.run(["bash", "-lc", script], check=True)

    assert json.loads(record.read_text(encoding="utf-8")) == {}


def test_a_run_that_dies_mid_build_leaves_nothing_licensing_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of what a recorded fingerprint means.

    Recording only what was rebuilt is not enough on its own: a rebuild also
    destroys the artifact the previous record described, so a run that dies
    part way through leaves a claim about something that no longer exists. If
    the next update syncs the same inputs -- a rollback, or a rerun after
    fixing the environment rather than the source -- that claim licenses
    skipping the rebuild that just failed.
    """
    calls = []

    class ExistingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        names = daemon_listing(*MASTER_NAMES)

        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="{}")

    def record(name):
        def wrapper(*args, **kwargs):
            calls.append(name)

        return wrapper

    def die_mid_build(*args, **kwargs):
        calls.append("update_dependencies_and_build")
        raise RuntimeError("npm run build failed")

    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "require_incus", lambda: None)
    monkeypatch.setattr(incus_regression, "Runner", ExistingRunner)
    monkeypatch.setattr(incus_regression, "ensure_project_and_instance", record("ensure_project_and_instance"))
    monkeypatch.setattr(incus_regression, "stop_service_for_update", record("stop_service_for_update"))
    monkeypatch.setattr(incus_regression, "write_runtime_env", record("write_runtime_env"))
    monkeypatch.setattr(incus_regression, "migrate_legacy_backend_runtimes", record("migrate_legacy_backend_runtimes"))
    monkeypatch.setattr(incus_regression, "should_seed_state", lambda *args, **kwargs: False)
    monkeypatch.setattr(incus_regression, "sync_source", record("sync_source"))
    monkeypatch.setattr(incus_regression, "compute_fingerprints", lambda repo_root: {"python": "new"})
    monkeypatch.setattr(incus_regression, "read_existing_fingerprints", lambda *args, **kwargs: {"python": "old"})
    monkeypatch.setattr(incus_regression, "invalidate_fingerprints", record("invalidate_fingerprints"))
    monkeypatch.setattr(incus_regression, "update_dependencies_and_build", die_mid_build)
    monkeypatch.setattr(incus_regression, "write_metadata", record("write_metadata"))

    args = argparse.Namespace(
        target="master",
        slug=None,
        host_port=None,
        ui_host="127.0.0.1",
        ui_port=5123,
        worktree_port_start=15200,
        worktree_port_end=15399,
        env_file=None,
        dry_run=False,
        image="avibe-regression-base-current",
        storage_pool="default",
        network="incusbr0",
        cpus="2",
        memory="4GiB",
        disk="20GiB",
        processes="4096",
        remote=None,
        clean=False,
        force_deps=False,
        no_build_ui=True,
        force_ui=False,
        reset_mode="none",
    )

    with pytest.raises(RuntimeError):
        incus_regression.cmd_up(args)

    assert calls.index("invalidate_fingerprints") < calls.index("update_dependencies_and_build")
    assert "write_metadata" not in calls


def test_missing_ui_dist_rebuilds_even_when_python_is_unchanged() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            if "test -d ui/dist" in commands[-1]:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.update_dependencies_and_build(
        RecordingRunner(),
        target,
        previous_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        next_fingerprints={"python": "p", "ui_deps": "d", "ui_source": "s"},
        force_deps=False,
        build_ui=False,
        force_ui=False,
        remote=None,
    )

    joined = "\n".join(commands)
    assert "test -d ui/dist && test -f ui/dist/index.html" in joined
    assert "cd ui && npm run build" in joined
    assert "pip install -e ." not in joined


@pytest.mark.parametrize("invalid_timeout", ["-1", "0", "not-an-integer", "0.5"])
@pytest.mark.parametrize("from_env_file", [False, True])
def test_up_rejects_invalid_build_timeout_before_incus_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_timeout: str, from_env_file: bool
) -> None:
    name = "REGRESSION_SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS"
    monkeypatch.setenv(name, invalid_timeout)
    monkeypatch.setattr(incus_regression, "current_repo_root", lambda: tmp_path)
    monkeypatch.setattr(incus_regression, "git_common_root", lambda repo_root: repo_root)
    if from_env_file:
        monkeypatch.delenv(name)
        (tmp_path / incus_regression.ENV_FILE_NAME).write_text(f"{name}={invalid_timeout}\n", encoding="utf-8")

    def unexpected_incus_access(*args, **kwargs):
        pytest.fail("Invalid configuration must fail before Incus access")

    monkeypatch.setattr(incus_regression, "require_incus", unexpected_incus_access)
    monkeypatch.setattr(incus_regression.subprocess, "run", unexpected_incus_access)
    args = incus_regression.build_parser().parse_args(["up", "--target", "master"])

    with pytest.raises(incus_regression.RegressionError, match=f"{name} must be .*integer"):
        incus_regression.cmd_up(args)

    assert not (tmp_path / ".runtime").exists()


@pytest.mark.parametrize(
    ("timeout_env", "expected_timeout"),
    [(None, incus_regression.SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS), ("", 300), ("1", 1), (" 900 ", 900)],
)
def test_prepare_show_runtime_builds_archive_and_retries_from_fresh_install(
    monkeypatch: pytest.MonkeyPatch, timeout_env: str | None, expected_timeout: int
) -> None:
    commands = []
    build_timeouts = []

    class RecordingRunner:
        def __init__(self) -> None:
            self.prepare_attempts = 0

        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if "git clone --depth 1" in joined:
                build_timeouts.append(kwargs.get("timeout"))
            if "vibe runtime prepare --strict" in joined:
                self.prepare_attempts += 1
                return subprocess.CompletedProcess(command, 1 if self.prepare_attempts == 1 else 0)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )
    monkeypatch.delenv("REGRESSION_SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS", raising=False)
    if timeout_env is not None:
        monkeypatch.setenv("REGRESSION_SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS", timeout_env)

    incus_regression.prepare_show_runtime(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "git clone --depth 1 https://github.com/avibe-bot/vibe-show-runtime.git" in joined
    assert "npm run bundle:vibe-remote" in joined
    assert 'install -D -m 0644 "$1" "$VIBE_SHOW_RUNTIME_ARCHIVE_PATH"' in joined
    assert build_timeouts == [expected_timeout]
    build_command = next(command for command in commands if "git clone --depth 1" in command)
    assert build_command.index("VIBE_SHOW_RUNTIME_ARCHIVE_PATH is required") < build_command.index("git clone")
    assert joined.count("vibe runtime prepare --strict") == 2
    assert "rm -rf ~/.avibe/runtime/show-runtime/prebuilt/current" in joined
    assert "vibe runtime status --json" in joined


def test_prepare_show_runtime_retry_keeps_the_npm_cache() -> None:
    commands = []

    class RecordingRunner:
        def __init__(self) -> None:
            self.prepare_attempts = 0

        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if "vibe runtime prepare --strict" in joined:
                self.prepare_attempts += 1
                return subprocess.CompletedProcess(command, 1 if self.prepare_attempts == 1 else 0)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.prepare_show_runtime(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "_cacache" not in joined
    assert "npm cache clean" not in joined


@pytest.mark.parametrize("legacy_source", ["github", "github-source", "GitHub-Source"])
def test_retired_runtime_source_is_recognized_by_product_and_regression(
    legacy_source: str,
) -> None:
    from core.show_runtime import _normalize_runtime_source

    assert incus_regression.regression_show_runtime_source(legacy_source) == "archive"
    assert _normalize_runtime_source(legacy_source) == "manifest-cache"

    commands = []

    class RecordingRunner:
        def __init__(self) -> None:
            self.prepare_attempts = 0

        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if 'printf "%s" "${VIBE_SHOW_RUNTIME_SOURCE:-}"' in joined:
                return subprocess.CompletedProcess(command, 0, stdout=legacy_source)
            if "vibe runtime prepare --strict" in joined:
                self.prepare_attempts += 1
                return subprocess.CompletedProcess(command, 1 if self.prepare_attempts == 1 else 0)
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.prepare_show_runtime(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "cat > /etc/avibe-regression.env" not in joined
    assert "git clone --depth 1 https://github.com/avibe-bot/vibe-show-runtime.git" in joined
    assert joined.count("VIBE_SHOW_RUNTIME_SOURCE=archive") == 4
    archive_path = f"{incus_regression.SERVICE_HOME}/.cache/avibe-regression/vibe-show-runtime-node.tgz"
    assert joined.count(f"VIBE_SHOW_RUNTIME_ARCHIVE_PATH={archive_path}") == 4
    assert f"VIBE_SHOW_RUNTIME_SOURCE={legacy_source}" not in joined


def test_restart_waits_for_service_and_status_running() -> None:
    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            commands.append(" ".join(command))
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.restart_and_verify(RecordingRunner(), target, remote=None)

    joined = "\n".join(commands)
    assert "systemctl is-active --quiet avibe-regression.service" in joined
    assert "http://127.0.0.1:5123/status" in joined
    assert "'\"state\":\"running\"'" in joined
