#!/usr/bin/env python3
"""Manage local Incus tenants for Vibe Remote.

This script intentionally uses only the Python standard library so it can run on
a fresh Incus host before the Vibe Remote Python package is installed.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROJECT_PREFIX = "vr-"
INSTANCE_NAME_PREFIX = "vibe-"
LEGACY_INSTANCE_NAME = "vibe"
TENANT_USER = "vibey"
TENANT_HOME = f"/home/{TENANT_USER}"
TENANT_WORKDIR = f"{TENANT_HOME}/work"
DEFAULT_IMAGE = "images:ubuntu/24.04/cloud"
DEFAULT_STORAGE_POOL = "default"
DEFAULT_NETWORK = "incusbr0"
DEFAULT_UI_PORT = 5123
TENANT_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")


class TenantError(RuntimeError):
    """A user-correctable scaffold error."""


@dataclass(frozen=True)
class TenantSpec:
    tenant: str
    instance_type: str
    image: str
    cpus: str
    memory: str
    disk: str
    processes: str
    storage_pool: str
    network: str
    ui_port: int
    ui_host: str
    ui_host_port: int | None
    backend: str
    install_package_spec: str | None

    @property
    def project(self) -> str:
        return project_name(self.tenant)


class Runner:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        print("+ " + shlex.join(command))
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.run(
            list(command),
            input=input_text,
            text=True,
            check=check,
            capture_output=capture,
        )

    def exists(self, command: Sequence[str]) -> bool:
        if self.dry_run:
            return False
        result = subprocess.run(list(command), text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0


def default_instance_name(tenant: str) -> str:
    """The per-tenant instance name used for newly created tenants."""
    validate_tenant(tenant)
    return f"{INSTANCE_NAME_PREFIX}{tenant}"


def _incus_query(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def instance_name(tenant: str, *, dry_run: bool = False) -> str:
    """Resolve the incus instance name for managing an existing tenant.

    Resolution order: an explicit ``user.vibe_remote.instance_name`` project
    override, then the legacy shared ``vibe`` instance (tenants created before
    per-tenant names had no override), then the per-tenant default. Dry runs stay
    purely local and never touch the incus daemon.
    """
    default = default_instance_name(tenant)
    if dry_run or shutil.which("incus") is None:
        return default
    project = project_name(tenant)
    override = _incus_query(incus("project", "get", project, "user.vibe_remote.instance_name"))
    configured = override.stdout.strip() if override.returncode == 0 else ""
    if configured:
        return configured
    if _incus_query(incus("info", LEGACY_INSTANCE_NAME, project=project)).returncode == 0:
        return LEGACY_INSTANCE_NAME
    return default


def project_name(tenant: str) -> str:
    validate_tenant(tenant)
    return f"{PROJECT_PREFIX}{tenant}"


def validate_tenant(tenant: str) -> None:
    if not TENANT_RE.match(tenant):
        raise TenantError(
            "Tenant must be 3-40 chars, lowercase, and contain only letters, numbers, and single hyphens."
        )


def incus(*args: str, project: str | None = None) -> list[str]:
    command = ["incus"]
    if project:
        command.extend(["--project", project])
    command.extend(args)
    return command


def require_incus() -> None:
    if shutil.which("incus") is None:
        raise TenantError("The Incus CLI was not found. Install Incus and make sure `incus` is in PATH.")


def unbracket_host(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def is_ipv6_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(unbracket_host(host)).version == 6
    except ValueError:
        return False


def tcp_endpoint(host: str, port: int) -> str:
    if is_ipv6_host(host):
        return f"tcp:[{unbracket_host(host)}]:{port}"
    return f"tcp:{host}:{port}"


def ensure_host_port_available(host: str, port: int) -> None:
    bind_host = unbracket_host(host)
    family = socket.AF_INET6 if is_ipv6_host(host) else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, port))
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                print(
                    f"Warning: cannot preflight privileged host port {host}:{port} as this user; "
                    "continuing so the Incus daemon can validate the proxy device.",
                    file=sys.stderr,
                )
                return
            raise TenantError(f"Host port {host}:{port} is not available: {exc}") from exc


def yaml_block(value: str, indent: int = 6) -> str:
    prefix = " " * indent
    return "\n".join(prefix + line if line else prefix for line in value.splitlines())


def default_config(spec: TenantSpec) -> dict:
    enabled = {spec.backend, "opencode", "claude", "codex"}
    return {
        "platform": "slack",
        "platforms": {"enabled": ["slack"], "primary": "slack"},
        "mode": "self_host",
        "version": "v2",
        "slack": {"bot_token": "", "app_token": "", "signing_secret": "", "require_mention": False},
        "discord": None,
        "telegram": None,
        "lark": None,
        "wechat": None,
        "runtime": {"default_cwd": TENANT_WORKDIR, "log_level": "INFO"},
        "agents": {
            "opencode": {"enabled": "opencode" in enabled, "cli_path": "opencode", "error_retry_limit": 1},
            "claude": {"enabled": "claude" in enabled, "cli_path": "claude"},
            "codex": {"enabled": "codex" in enabled, "cli_path": "codex"},
        },
        "gateway": None,
        "ui": {"setup_host": "0.0.0.0", "setup_port": spec.ui_port, "open_browser": False},
        "remote_access": {"provider": "vibe_cloud", "vibe_cloud": {}},
        "update": {},
        "ack_mode": "typing",
        "show_duration": False,
        "include_time_info": True,
        "include_user_info": True,
        "reply_enhancements": True,
        "language": "en",
    }


def cloud_init_user_data(spec: TenantSpec) -> str:
    config_json = json.dumps(default_config(spec), indent=2)
    install_prefix = ""
    if spec.install_package_spec:
        install_prefix = f"export AVIBE_INSTALL_PACKAGE_SPEC={shlex.quote(spec.install_package_spec)}; "
    install_command = install_prefix + "curl -fsSL https://avibe.bot/install.sh | bash"
    seed_default_agent_command = f"vibe agent default {shlex.quote(spec.backend)}"
    runcmd = [
        ["mkdir", "-p", TENANT_WORKDIR, f"{TENANT_HOME}/.vibe_remote/config"],
        ["chown", "-R", f"{TENANT_USER}:{TENANT_USER}", TENANT_HOME],
        ["su", "-", TENANT_USER, "-c", install_command],
        ["su", "-", TENANT_USER, "-c", seed_default_agent_command],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "vibe-remote.service"],
    ]
    runcmd_yaml = "\n".join(f"  - {json.dumps(item)}" for item in runcmd)
    service = textwrap.dedent(
        f"""\
        [Unit]
        Description=Vibe Remote tenant service
        Wants=network-online.target
        After=network-online.target

        [Service]
        Type=oneshot
        User={TENANT_USER}
        Group={TENANT_USER}
        Environment=HOME={TENANT_HOME}
        Environment=SSH_CONNECTION=incus-cloud-init
        Environment=PATH={TENANT_HOME}/.local/bin:{TENANT_HOME}/.cargo/bin:/usr/local/bin:/usr/bin:/bin
        ExecStart=/bin/bash -lc 'vibe'
        ExecStop=/bin/bash -lc 'vibe stop'
        RemainAfterExit=yes
        TimeoutStartSec=300
        TimeoutStopSec=60

        [Install]
        WantedBy=multi-user.target
        """
    ).rstrip()
    tenant_info = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        echo "tenant={spec.tenant}"
        echo "project={spec.project}"
        echo "instance={default_instance_name(spec.tenant)}"
        echo "ui=http://127.0.0.1:{spec.ui_port}"
        echo "workdir={TENANT_WORKDIR}"
        """
    ).rstrip()
    lines = [
        "#cloud-config",
        "package_update: true",
        "packages:",
        "  - bash",
        "  - ca-certificates",
        "  - curl",
        "  - git",
        "  - build-essential",
        "  - python3",
        "  - python3-pip",
        "  - python3-venv",
        "  - sudo",
        "users:",
        f"  - name: {TENANT_USER}",
        "    groups: sudo",
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    lock_passwd: true",
        "write_files:",
        f"  - path: {TENANT_HOME}/.vibe_remote/config/config.json",
        "    owner: root:root",
        "    permissions: '0600'",
        "    content: |",
        yaml_block(config_json),
        "  - path: /etc/systemd/system/vibe-remote.service",
        "    owner: root:root",
        "    permissions: '0644'",
        "    content: |",
        yaml_block(service),
        "  - path: /usr/local/bin/vibe-tenant-info",
        "    owner: root:root",
        "    permissions: '0755'",
        "    content: |",
        yaml_block(tenant_info),
        "runcmd:",
        runcmd_yaml,
        f'final_message: "Vibe Remote tenant {spec.tenant} is ready. Run vibe-tenant-info for details."',
    ]
    return "\n".join(lines)


def profile_yaml(spec: TenantSpec) -> str:
    return textwrap.dedent(
        f"""\
        config:
          limits.cpu: "{spec.cpus}"
          limits.memory: "{spec.memory}"
          limits.processes: "{spec.processes}"
        description: Vibe Remote tenant profile for {spec.tenant}
        devices:
          eth0:
            name: eth0
            network: {spec.network}
            type: nic
          root:
            path: /
            pool: {spec.storage_pool}
            size: {spec.disk}
            type: disk
        name: default
        """
    )


def project_create_config(spec: TenantSpec) -> list[str]:
    instance_limit_key = "limits.virtual-machines" if spec.instance_type == "vm" else "limits.containers"
    return [
        "features.images=false",
        "features.profiles=true",
        "features.storage.volumes=true",
        "restricted=true",
        "restricted.devices.proxy=allow",
        "limits.instances=1",
        f"{instance_limit_key}=1",
        f"limits.cpu={spec.cpus}",
        f"limits.memory={spec.memory}",
        f"limits.processes={spec.processes}",
        f"user.vibe_remote.tenant={spec.tenant}",
        f"user.vibe_remote.instance_type={spec.instance_type}",
        f"user.vibe_remote.ui_host={spec.ui_host}",
        f"user.vibe_remote.ui_host_port={spec.ui_host_port or ''}",
    ]


def maybe_require_incus(args: argparse.Namespace) -> None:
    if not getattr(args, "dry_run", False):
        require_incus()


def cmd_doctor(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    runner = Runner(dry_run=args.dry_run)
    checks = [
        ("version", incus("version")),
        ("daemon info", incus("info")),
        ("storage pools", incus("storage", "list")),
        ("networks", incus("network", "list")),
        ("default profile", incus("profile", "show", "default")),
    ]
    failed: list[str] = []
    for name, command in checks:
        result = runner.run(command, check=False)
        if result.returncode != 0:
            failed.append(name)
    if failed:
        print("Failed checks: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def cmd_init_host(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    runner = Runner(dry_run=args.dry_run)
    if args.minimal:
        runner.run(incus("admin", "init", "--minimal"))
    return cmd_doctor(args)


def create_project(runner: Runner, spec: TenantSpec) -> None:
    command = incus("project", "create", spec.project)
    for item in project_create_config(spec):
        command.extend(["--config", item])
    runner.run(command)


def ensure_create_target_available(runner: Runner, spec: TenantSpec) -> None:
    if runner.exists(incus("project", "show", spec.project)):
        raise TenantError(
            f"Tenant project {spec.project} already exists. Use status/delete, or add a dedicated update command."
        )


def configure_profile(runner: Runner, spec: TenantSpec) -> None:
    runner.run(incus("profile", "edit", "default", project=spec.project), input_text=profile_yaml(spec))


def proxy_device_args(spec: TenantSpec) -> list[str]:
    args = [
        "config",
        "device",
        "add",
        default_instance_name(spec.tenant),
        "ui",
        "proxy",
        f"listen={tcp_endpoint(spec.ui_host, spec.ui_host_port)}",
        f"connect=tcp:127.0.0.1:{spec.ui_port}",
    ]
    if spec.instance_type == "vm":
        args.append("nat=true")
    return args


def create_instance(runner: Runner, spec: TenantSpec) -> None:
    name = default_instance_name(spec.tenant)
    if runner.exists(incus("info", name, project=spec.project)):
        raise TenantError(f"Tenant instance already exists in project {spec.project}.")
    command = incus(
        "init",
        spec.image,
        name,
        "--profile",
        "default",
        "--config",
        f"cloud-init.user-data={cloud_init_user_data(spec)}",
        project=spec.project,
    )
    if spec.instance_type == "vm":
        command.append("--vm")
    runner.run(command)
    if spec.ui_host_port:
        runner.run(incus(*proxy_device_args(spec), project=spec.project))
    runner.run(incus("start", name, project=spec.project))


def cmd_create(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    spec = TenantSpec(
        tenant=args.tenant,
        instance_type=args.type,
        image=args.image,
        cpus=args.cpus,
        memory=args.memory,
        disk=args.disk,
        processes=args.processes,
        storage_pool=args.storage_pool,
        network=args.network,
        ui_port=args.ui_port,
        ui_host=args.ui_host,
        ui_host_port=args.ui_host_port,
        backend=args.backend,
        install_package_spec=args.install_package_spec,
    )
    if spec.ui_host_port and not args.dry_run:
        ensure_host_port_available(spec.ui_host, spec.ui_host_port)
    runner = Runner(dry_run=args.dry_run)
    ensure_create_target_available(runner, spec)
    create_project(runner, spec)
    configure_profile(runner, spec)
    create_instance(runner, spec)
    print("")
    print(f"Tenant created: {spec.tenant}")
    print(f"Project: {spec.project}")
    print(f"Instance: {default_instance_name(spec.tenant)}")
    print(f"Wait for setup: python3 scripts/incus_tenant.py wait-ready {spec.tenant}")
    if spec.ui_host_port:
        print(f"Web UI: http://{spec.ui_host}:{spec.ui_host_port}")
    else:
        print(f"Web UI: incus --project {spec.project} exec {default_instance_name(spec.tenant)} -- vibe remote")
    return 0


def cmd_wait_ready(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    project = project_name(args.tenant)
    runner = Runner(dry_run=args.dry_run)
    name = instance_name(args.tenant, dry_run=args.dry_run)
    runner.run(incus("exec", name, "--", "cloud-init", "status", "--wait", project=project))
    runner.run(
        incus("exec", name, "--", "systemctl", "is-active", "--quiet", "vibe-remote.service", project=project)
    )
    runner.run(incus("exec", name, "--", "systemctl", "status", "vibe-remote", "--no-pager", project=project), check=False)
    return 0


def cmd_lifecycle(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    project = project_name(args.tenant)
    runner = Runner(dry_run=args.dry_run)
    runner.run(incus(args.action, instance_name(args.tenant, dry_run=args.dry_run), project=project))
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    project = project_name(args.tenant)
    runner = Runner(dry_run=args.dry_run)
    runner.run(incus("restart", instance_name(args.tenant, dry_run=args.dry_run), project=project))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    project = project_name(args.tenant)
    runner = Runner(dry_run=args.dry_run)
    inst = instance_name(args.tenant, dry_run=args.dry_run)
    checks = [
        ("instance list", incus("list", project=project)),
        ("tenant info", incus("exec", inst, "--", "vibe-tenant-info", project=project)),
        ("vibe status", incus("exec", inst, "--", *tenant_user_bash("vibe status"), project=project)),
    ]
    failed: list[str] = []
    for name, command in checks:
        result = runner.run(command, check=False)
        if result.returncode != 0:
            failed.append(name)
    if failed:
        print("Failed status checks: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def tenant_user_bash(command: str, *args: str) -> list[str]:
    return [
        "sudo",
        "-H",
        "-u",
        TENANT_USER,
        "--",
        "bash",
        "-lc",
        f"cd {shlex.quote(TENANT_WORKDIR)} && {command}",
        "bash",
        *args,
    ]


def cmd_shell(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    project = project_name(args.tenant)
    runner = Runner(dry_run=args.dry_run)
    runner.run(incus("exec", instance_name(args.tenant, dry_run=args.dry_run), "--", *tenant_user_bash("exec bash -l"), project=project))
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise TenantError("Pass a command after `--`.")
    project = project_name(args.tenant)
    runner = Runner(dry_run=args.dry_run)
    runner.run(
        incus(
            "exec",
            instance_name(args.tenant, dry_run=args.dry_run),
            "--",
            *tenant_user_bash('exec "$@"', *command),
            project=project,
        )
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    runner = Runner(dry_run=args.dry_run)
    result = runner.run(incus("project", "list"), check=False)
    print("")
    print(f"Vibe Remote tenant projects use the `{PROJECT_PREFIX}` prefix.")
    if result.returncode != 0:
        return result.returncode
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    maybe_require_incus(args)
    project = project_name(args.tenant)
    if not args.yes and not args.dry_run:
        confirmation = input(f"Delete tenant {args.tenant} ({project}) and all its data? Type the tenant name: ")
        if confirmation != args.tenant:
            raise TenantError("Confirmation did not match; aborting.")
    runner = Runner(dry_run=args.dry_run)
    if args.dry_run:
        runner.run(incus("delete", instance_name(args.tenant, dry_run=args.dry_run), "--force", project=project))
        runner.run(incus("project", "delete", project))
        return 0
    if not runner.exists(incus("project", "show", project)):
        raise TenantError(f"Tenant project {project} does not exist.")
    name = instance_name(args.tenant, dry_run=args.dry_run)
    if runner.exists(incus("info", name, project=project)):
        runner.run(incus("delete", name, "--force", project=project))
    else:
        print(f"Tenant instance {name} is already absent in project {project}.", file=sys.stderr)
    runner.run(incus("project", "delete", project))
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print Incus commands without running them.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and operate Vibe Remote tenant instances on a local Incus host.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check Incus host readiness.")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    init_host = subparsers.add_parser("init-host", help="Optionally initialize Incus, then check readiness.")
    init_host.add_argument("--minimal", action="store_true", help="Run `incus admin init --minimal` before checks.")
    add_common(init_host)
    init_host.set_defaults(func=cmd_init_host)

    create = subparsers.add_parser("create", help="Create and start a new Vibe Remote tenant.")
    create.add_argument("tenant", help="Tenant slug, for example alice or demo-01.")
    create.add_argument("--type", choices=["container", "vm"], default="container", help="Incus instance type.")
    create.add_argument("--image", default=DEFAULT_IMAGE, help=f"Incus image alias. Default: {DEFAULT_IMAGE}")
    create.add_argument("--cpus", default="2", help="CPU limit, for example 2.")
    create.add_argument("--memory", default="4GiB", help="Memory limit, for example 4GiB.")
    create.add_argument("--disk", default="30GiB", help="Root disk limit, for example 30GiB.")
    create.add_argument("--processes", default="4096", help="Process count limit.")
    create.add_argument("--storage-pool", default=DEFAULT_STORAGE_POOL, help="Incus storage pool.")
    create.add_argument("--network", default=DEFAULT_NETWORK, help="Incus managed bridge network.")
    create.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="Tenant Web UI port inside the instance.")
    create.add_argument(
        "--ui-host",
        default="127.0.0.1",
        help="Host address for the optional Web UI proxy. Use 0.0.0.0 only behind a firewall or reverse proxy.",
    )
    create.add_argument("--ui-host-port", type=int, help="Optional host port proxy for the tenant Web UI.")
    create.add_argument("--backend", choices=["opencode", "claude", "codex"], default="opencode", help="Default agent backend.")
    create.add_argument(
        "--install-package-spec",
        help="Optional package spec passed to the avibe installer, e.g. git+https://github.com/avibe-bot/avibe.git@master.",
    )
    add_common(create)
    create.set_defaults(func=cmd_create)

    wait_ready = subparsers.add_parser("wait-ready", help="Wait until cloud-init finishes inside the tenant.")
    wait_ready.add_argument("tenant")
    add_common(wait_ready)
    wait_ready.set_defaults(func=cmd_wait_ready)

    for name in ("start", "stop"):
        lifecycle = subparsers.add_parser(name, help=f"{name.title()} a tenant instance.")
        lifecycle.add_argument("tenant")
        add_common(lifecycle)
        lifecycle.set_defaults(func=cmd_lifecycle, action=name)

    restart = subparsers.add_parser("restart", help="Restart a tenant instance.")
    restart.add_argument("tenant")
    add_common(restart)
    restart.set_defaults(func=cmd_restart)

    status = subparsers.add_parser("status", help="Show tenant instance and Vibe Remote status.")
    status.add_argument("tenant")
    add_common(status)
    status.set_defaults(func=cmd_status)

    shell = subparsers.add_parser("shell", help="Open a shell as the tenant user.")
    shell.add_argument("tenant")
    add_common(shell)
    shell.set_defaults(func=cmd_shell)

    exec_parser = subparsers.add_parser("exec", help="Run a command as the tenant user.")
    exec_parser.add_argument("tenant")
    exec_parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --.")
    add_common(exec_parser)
    exec_parser.set_defaults(func=cmd_exec)

    list_parser = subparsers.add_parser("list", help="List Incus projects and highlight tenant prefix.")
    add_common(list_parser)
    list_parser.set_defaults(func=cmd_list)

    delete = subparsers.add_parser("delete", help="Delete a tenant project and all data.")
    delete.add_argument("tenant")
    delete.add_argument("-y", "--yes", action="store_true", help="Skip confirmation.")
    add_common(delete)
    delete.set_defaults(func=cmd_delete)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except TenantError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed with exit code {exc.returncode}: {shlex.join(exc.cmd)}", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
