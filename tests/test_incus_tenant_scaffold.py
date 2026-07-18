import importlib.util
import argparse
import errno
import socket
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "incus_tenant.py"
SPEC = importlib.util.spec_from_file_location("incus_tenant", SCRIPT_PATH)
incus_tenant = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = incus_tenant
SPEC.loader.exec_module(incus_tenant)


def tenant_spec(**overrides):
    payload = {
        "tenant": "demo-01",
        "instance_type": "container",
        "image": incus_tenant.DEFAULT_IMAGE,
        "cpus": "2",
        "memory": "4GiB",
        "disk": "30GiB",
        "processes": "4096",
        "storage_pool": "default",
        "network": "incusbr0",
        "ui_port": 5123,
        "ui_host": "127.0.0.1",
        "ui_host_port": 15123,
        "backend": "codex",
        "install_package_spec": None,
    }
    payload.update(overrides)
    return incus_tenant.TenantSpec(**payload)


def test_project_name_validates_slug():
    assert incus_tenant.project_name("demo-01") == "vr-demo-01"
    with pytest.raises(incus_tenant.TenantError):
        incus_tenant.project_name("Demo")
    with pytest.raises(incus_tenant.TenantError):
        incus_tenant.project_name("a")


def test_cloud_init_configures_vibe_user_service_and_ui():
    data = incus_tenant.cloud_init_user_data(tenant_spec())

    assert "#cloud-config" in data
    assert "name: vibey" in data
    assert "User=vibey" in data
    assert '"default_cwd": "/home/vibey/work"' in data
    assert "ExecStart=/bin/bash -lc 'vibe'" in data
    assert '"setup_host": "0.0.0.0"' in data
    assert '"setup_port": 5123' in data
    assert '"default_backend"' not in data
    assert '"vibe agent default codex"' in data
    assert "https://avibe.bot/install.sh" in data
    # The installer runs as vibey, so cloud-init must own the whole home (not just
    # work/ + .vibe_remote) and write the root-created config as root before chown.
    assert '"/home/vibey"]' in data
    assert "owner: root:root" in data


def test_profile_yaml_sets_resources_and_devices():
    data = incus_tenant.profile_yaml(tenant_spec(cpus="4", memory="8GiB", disk="80GiB"))

    assert 'limits.cpu: "4"' in data
    assert 'limits.memory: "8GiB"' in data
    assert "size: 80GiB" in data
    assert "network: incusbr0" in data
    assert "pool: default" in data


def test_project_create_config_uses_vm_limit_for_vm():
    config = incus_tenant.project_create_config(tenant_spec(instance_type="vm"))

    assert "limits.instances=1" in config
    assert "limits.virtual-machines=1" in config
    assert "limits.containers=1" not in config
    assert "user.vibe_remote.ui_host=127.0.0.1" in config
    assert "restricted=true" in config
    assert "restricted.devices.proxy=allow" in config


def test_project_create_config_omits_invalid_disk_pool_key():
    # `limits.disk.pool.<pool>` is not a valid Incus 6.0 project config key and
    # makes `incus project create` fail; root disk size lives on the profile.
    config = incus_tenant.project_create_config(tenant_spec(disk="50GiB"))

    assert not any(item.startswith("limits.disk.pool") for item in config)


def test_instance_name_defaults_to_per_tenant_slug(monkeypatch):
    # Per-tenant instance names avoid DNS collisions on the shared incus bridge;
    # with no incus binary present it falls back to the prefixed slug.
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: None)

    assert incus_tenant.instance_name("demo-01") == "vibe-demo-01"


def _fake_incus_query(*, override="", legacy_exists=False):
    def run(command):
        joined = " ".join(command)
        if "user.vibe_remote.instance_name" in joined:
            return subprocess.CompletedProcess(command, 0 if override else 1, override, "")
        if "info" in command:
            return subprocess.CompletedProcess(command, 0 if legacy_exists else 1, "", "")
        return subprocess.CompletedProcess(command, 1, "", "")

    return run


def test_instance_name_prefers_project_override(monkeypatch):
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: "/usr/bin/incus")
    monkeypatch.setattr(incus_tenant, "_incus_query", _fake_incus_query(override="renamed-demo"))

    assert incus_tenant.instance_name("demo-01") == "renamed-demo"


def test_instance_name_falls_back_to_legacy_vibe(monkeypatch):
    # Tenants created before per-tenant names have a "vibe" instance and no
    # override; management commands must still target it instead of vibe-<tenant>.
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: "/usr/bin/incus")
    monkeypatch.setattr(incus_tenant, "_incus_query", _fake_incus_query(legacy_exists=True))

    assert incus_tenant.instance_name("demo-01") == "vibe"


def test_instance_name_defaults_when_no_legacy_instance(monkeypatch):
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: "/usr/bin/incus")
    monkeypatch.setattr(incus_tenant, "_incus_query", _fake_incus_query(legacy_exists=False))

    assert incus_tenant.instance_name("demo-01") == "vibe-demo-01"


def test_instance_name_dry_run_never_touches_incus(monkeypatch):
    # Dry-run planning must not depend on the incus daemon/permissions.
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: "/usr/bin/incus")

    def explode(command):
        raise AssertionError(f"dry-run must not call incus: {command}")

    monkeypatch.setattr(incus_tenant, "_incus_query", explode)

    assert incus_tenant.instance_name("demo-01", dry_run=True) == "vibe-demo-01"


def test_parser_accepts_create_dry_run():
    parser = incus_tenant.build_parser()
    args = parser.parse_args(
        [
            "create",
            "demo-01",
            "--cpus",
            "4",
            "--memory",
            "8GiB",
            "--disk",
            "80GiB",
            "--backend",
            "codex",
            "--ui-host-port",
            "15123",
            "--dry-run",
        ]
    )

    assert args.command == "create"
    assert args.tenant == "demo-01"
    assert args.cpus == "4"
    assert args.ui_host == "127.0.0.1"
    assert args.backend == "codex"
    assert args.dry_run is True


def test_exec_dry_run_strips_command_separator(capsys):
    exit_code = incus_tenant.main(["exec", "--dry-run", "demo-01", "--", "pwd"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "--user 1000" not in output
    assert "sudo -H -u vibey" in output
    assert "exec \"$@\"' bash pwd" in output


def test_shell_dry_run_uses_tenant_username(capsys):
    exit_code = incus_tenant.main(["shell", "--dry-run", "demo-01"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "--user 1000" not in output
    assert "sudo -H -u vibey" in output
    assert "exec bash -l" in output


def test_parser_keeps_exec_separator_for_handler():
    parser = incus_tenant.build_parser()
    args = parser.parse_args(["exec", "--dry-run", "demo-01", "--", "pwd"])

    assert args.command == ["pwd"]
    assert args.dry_run is True


def test_create_dry_run_binds_proxy_to_loopback(capsys):
    exit_code = incus_tenant.main(["create", "demo-01", "--ui-host-port", "15123", "--dry-run"])

    assert exit_code == 0
    assert "listen=tcp:127.0.0.1:15123" in capsys.readouterr().out


def test_create_vm_ui_proxy_uses_nat_mode(capsys):
    exit_code = incus_tenant.main(["create", "demo-01", "--type", "vm", "--ui-host-port", "15123", "--dry-run"])

    assert exit_code == 0
    assert "connect=tcp:127.0.0.1:5123 nat=true" in capsys.readouterr().out


def test_create_formats_ipv6_ui_proxy_endpoint(capsys):
    exit_code = incus_tenant.main(
        ["create", "demo-01", "--ui-host", "::1", "--ui-host-port", "15123", "--dry-run"]
    )

    assert exit_code == 0
    assert "listen=tcp:[::1]:15123" in capsys.readouterr().out


def test_privileged_port_preflight_does_not_block_on_permission(monkeypatch, capsys):
    class PermissionDeniedSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def setsockopt(self, *args):
            return None

        def bind(self, address):
            raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: PermissionDeniedSocket())

    incus_tenant.ensure_host_port_available("127.0.0.1", 80)
    assert "cannot preflight privileged host port" in capsys.readouterr().err


def test_port_preflight_still_rejects_busy_ports(monkeypatch):
    class BusySocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def setsockopt(self, *args):
            return None

        def bind(self, address):
            raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: BusySocket())

    with pytest.raises(incus_tenant.TenantError):
        incus_tenant.ensure_host_port_available("127.0.0.1", 15123)


def test_port_preflight_uses_ipv6_socket(monkeypatch):
    families = []
    addresses = []

    class IPv6Socket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def setsockopt(self, *args):
            return None

        def bind(self, address):
            addresses.append(address)

    def fake_socket(family, sock_type):
        families.append((family, sock_type))
        return IPv6Socket()

    monkeypatch.setattr(socket, "socket", fake_socket)

    incus_tenant.ensure_host_port_available("::1", 15123)

    assert families == [(socket.AF_INET6, socket.SOCK_STREAM)]
    assert addresses == [("::1", 15123)]


def test_create_refuses_existing_project_before_writes():
    class ExistingProjectRunner:
        def __init__(self):
            self.commands = []

        def exists(self, command):
            self.commands.append(command)
            return command == ["incus", "project", "show", "vr-demo-01"]

    runner = ExistingProjectRunner()

    with pytest.raises(incus_tenant.TenantError):
        incus_tenant.ensure_create_target_available(runner, tenant_spec())

    assert runner.commands == [["incus", "project", "show", "vr-demo-01"]]


def test_doctor_returns_nonzero_for_failed_readiness_check(monkeypatch):
    class FailingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, capture=False, input_text=None):
            returncode = 1 if command == ["incus", "info"] else 0
            return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(incus_tenant, "Runner", FailingRunner)

    assert incus_tenant.cmd_doctor(argparse.Namespace(dry_run=True)) == 1


def test_status_returns_nonzero_for_failed_probe(monkeypatch):
    class FailingStatusRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, capture=False, input_text=None):
            returncode = 1 if "vibe-tenant-info" in command else 0
            return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(incus_tenant, "Runner", FailingStatusRunner)

    assert incus_tenant.cmd_status(argparse.Namespace(tenant="demo-01", dry_run=True)) == 1


def test_list_returns_incus_project_list_exit_code(monkeypatch):
    class FailingListRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, capture=False, input_text=None):
            assert command == ["incus", "project", "list"]
            return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(incus_tenant, "Runner", FailingListRunner)

    assert incus_tenant.cmd_list(argparse.Namespace(dry_run=True)) == 7


def test_wait_ready_checks_tenant_service_active(monkeypatch):
    commands = []

    class RecordingRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def run(self, command, *, check=True, capture=False, input_text=None):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(incus_tenant, "Runner", RecordingRunner)
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: None)

    assert incus_tenant.cmd_wait_ready(argparse.Namespace(tenant="demo-01", dry_run=True)) == 0
    assert [
        "incus",
        "--project",
        "vr-demo-01",
        "exec",
        "vibe-demo-01",
        "--",
        "systemctl",
        "is-active",
        "--quiet",
        "vibe-remote.service",
    ] in commands


def test_delete_returns_error_when_project_missing(monkeypatch):
    class MissingProjectRunner:
        def __init__(self, *, dry_run=False):
            self.dry_run = dry_run

        def exists(self, command):
            return False

    monkeypatch.setattr(incus_tenant, "Runner", MissingProjectRunner)
    monkeypatch.setattr(incus_tenant, "require_incus", lambda: None)

    with pytest.raises(incus_tenant.TenantError):
        incus_tenant.cmd_delete(argparse.Namespace(tenant="demo-01", yes=True, dry_run=False))


def test_delete_dry_run_prints_cleanup_plan(monkeypatch, capsys):
    monkeypatch.setattr(incus_tenant.shutil, "which", lambda name: None)
    exit_code = incus_tenant.main(["delete", "--dry-run", "-y", "demo-01"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "incus --project vr-demo-01 delete vibe-demo-01 --force" in output
    assert "incus project delete vr-demo-01" in output
