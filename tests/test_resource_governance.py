from pathlib import Path
from types import SimpleNamespace

import pytest

from config import paths
from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
from core.resource_governance import (
    MIB,
    AgentResourceGovernor,
    _is_known_avibe_runtime_member,
    config_from_controller,
    derive_agent_limits,
    is_controller_resource_governor,
    governor_from_controller,
    tenant_memory_limit_bytes,
)


def test_derive_agent_limits_uses_single_aggregate_budget() -> None:
    limits = derive_agent_limits(4 * 1024 * MIB)

    assert limits.memory_max == 2785 * MIB
    assert limits.memory_high == 2367 * MIB
    assert limits.cpu_weight == 50
    assert limits.io_weight == 50
    assert limits.pids_max == 512


def test_derive_agent_limits_honors_explicit_bytes() -> None:
    limits = derive_agent_limits(
        8 * 1024 * MIB,
        {
            "agent_memory_max_bytes": 1536 * MIB,
            "agent_memory_high_bytes": 1200 * MIB,
            "agent_cpu_weight": 250,
            "agent_io_weight": 200,
            "agent_pids_max": 1024,
            "agent_oom_score_adj": 650,
        },
    )

    assert limits.memory_max == 1536 * MIB
    assert limits.memory_high == 1200 * MIB
    assert limits.cpu_weight == 250
    assert limits.io_weight == 200
    assert limits.pids_max == 1024
    assert limits.oom_score_adj == 650


def test_tenant_memory_limit_walks_to_parent_cap(tmp_path: Path) -> None:
    root = tmp_path / "cgroup"
    child = root / "service" / "worker"
    child.mkdir(parents=True)
    (root / "memory.max").write_text("max\n", encoding="utf-8")
    (root / "service").mkdir(exist_ok=True)
    (root / "service" / "memory.max").write_text(str(2 * 1024 * MIB), encoding="utf-8")
    (child / "memory.max").write_text("max\n", encoding="utf-8")

    assert tenant_memory_limit_bytes(child, root) == 2 * 1024 * MIB


def test_config_from_controller_reads_v2_runtime_resource_governance() -> None:
    v2_config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(),
        agents=AgentsConfig(),
        runtime=RuntimeConfig(
            default_cwd=".",
            resource_governance={
                "mode": "disabled",
                "agent_group_name": "custom-agents",
            },
        ),
    )
    controller = SimpleNamespace(config=v2_config)

    assert config_from_controller(controller) == {
        "mode": "disabled",
        "agent_group_name": "custom-agents",
    }


def test_governor_from_controller_marks_long_lived_governor() -> None:
    controller = SimpleNamespace(config=SimpleNamespace(resource_governance={"mode": "auto"}))

    governor = governor_from_controller(controller)

    assert is_controller_resource_governor(governor) is True
    assert governor_from_controller(controller) is governor


def test_known_runtime_member_accepts_current_source_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    monkeypatch.setattr("core.resource_governance._pid_uid", lambda pid: 1000)
    monkeypatch.setattr("core.resource_governance._pid_cwd", lambda pid: source)
    monkeypatch.setattr("core.resource_governance._current_runtime_cwd", lambda: source)
    monkeypatch.setattr(
        "core.resource_governance._pid_cmdline",
        lambda pid: f"/opt/avibe/venv/bin/python {source}/main.py",
    )

    assert _is_known_avibe_runtime_member(1234, uid=1000) is True


def test_known_runtime_member_rejects_unrelated_main_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "avibe" / "source"
    other = tmp_path / "other"
    source.mkdir(parents=True)
    other.mkdir()

    monkeypatch.setattr("core.resource_governance._pid_uid", lambda pid: 1000)
    monkeypatch.setattr("core.resource_governance._pid_cwd", lambda pid: other)
    monkeypatch.setattr("core.resource_governance._current_runtime_cwd", lambda: source)
    monkeypatch.setattr(
        "core.resource_governance._pid_cmdline",
        lambda pid: f"/usr/bin/python {other}/main.py",
    )

    assert _is_known_avibe_runtime_member(1234, uid=1000) is False


def test_known_runtime_member_accepts_scoped_package_service_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_service_main = Path(__file__).resolve().parents[1] / "vibe" / "service_main.py"

    monkeypatch.setattr("core.resource_governance._pid_uid", lambda pid: 1000)
    monkeypatch.setattr("core.resource_governance._pid_cwd", lambda pid: None)
    monkeypatch.setattr("core.resource_governance._current_runtime_cwd", lambda: None)
    monkeypatch.setattr(
        "core.resource_governance._pid_cmdline",
        lambda pid: (
            "systemd-run --user --scope -q -p Delegate=yes -- "
            f"/opt/avibe/venv/bin/python {package_service_main}"
        ),
    )

    assert _is_known_avibe_runtime_member(1234, uid=1000) is True


def test_known_runtime_member_accepts_custom_avibe_runtime_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    avibe_home = tmp_path / "data" / "avibe"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))

    monkeypatch.setattr("core.resource_governance._pid_uid", lambda pid: 1000)
    monkeypatch.setattr("core.resource_governance._pid_cwd", lambda pid: None)
    monkeypatch.setattr("core.resource_governance._current_runtime_cwd", lambda: None)
    monkeypatch.setattr(
        "core.resource_governance._pid_cmdline",
        lambda pid: f"/usr/bin/node {avibe_home}/runtime/show-runtime/source/main/packages/runtime/dist/cli.js",
    )

    assert _is_known_avibe_runtime_member(1234, uid=1000) is True


def test_known_runtime_member_accepts_custom_cloudflared_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    avibe_home = tmp_path / "custom-home"
    monkeypatch.setenv("AVIBE_HOME", str(avibe_home))
    paths.get_runtime_dir().mkdir(parents=True)
    paths.get_runtime_remote_access_pid_path().write_text("4321\n", encoding="utf-8")

    monkeypatch.setattr("core.resource_governance._pid_uid", lambda pid: 1000)
    monkeypatch.setattr("core.resource_governance._pid_cwd", lambda pid: None)
    monkeypatch.setattr("core.resource_governance._current_runtime_cwd", lambda: None)
    monkeypatch.setattr(
        "core.resource_governance._pid_cmdline",
        lambda pid: "/opt/cloudflare/cloudflared tunnel --no-autoupdate run",
    )

    assert _is_known_avibe_runtime_member(4321, uid=1000) is True


def test_known_runtime_member_rejects_unowned_cloudflared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("core.resource_governance._pid_uid", lambda pid: 1000)
    monkeypatch.setattr("core.resource_governance._pid_cwd", lambda pid: None)
    monkeypatch.setattr("core.resource_governance._current_runtime_cwd", lambda: None)
    monkeypatch.setattr(
        "core.resource_governance._pid_cmdline",
        lambda pid: "/opt/cloudflare/cloudflared tunnel --no-autoupdate run",
    )

    assert _is_known_avibe_runtime_member(4321, uid=1000) is False


def test_governor_disabled_mode_does_not_create_group(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    governor = AgentResourceGovernor({"mode": "disabled"}, root=tmp_path, base_cgroup=base)

    assert governor.apply_to_pid(123, label="test") is False
    assert not (base / "avibe-agents").exists()


def test_governor_update_config_resets_cached_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(512 * MIB), encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir

    def mkdir_with_controller_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            for name in ("cpu.weight", "io.weight", "pids.max", "cgroup.procs"):
                (group / name).write_text("", encoding="utf-8")
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_controller_files)

    assert governor.apply_to_pid(4321, label="test") is True
    assert governor.group_path == group

    governor.update_config({"mode": "disabled"})

    assert governor.group_path is None
    assert governor.apply_to_pid(4322, label="test") is False


def test_governor_configures_group_and_moves_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (root / "memory.max").write_text("max\n", encoding="utf-8")
    (base / "memory.max").write_text(str(2 * 1024 * MIB), encoding="utf-8")
    (base / "cgroup.controllers").write_text("memory cpu io pids\n", encoding="utf-8")
    (base / "cgroup.subtree_control").write_text("", encoding="utf-8")
    (base / "cgroup.procs").write_text("1001\n1002\n", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir
    runtime_writes: list[str] = []

    def mkdir_with_controller_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            for name in (
                "memory.high",
                "memory.max",
                "memory.oom.group",
                "cpu.weight",
                "io.weight",
                "pids.max",
                "cgroup.procs",
            ):
                (group / name).write_text("", encoding="utf-8")
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_controller_files)

    def fake_write_cgroup_value(path: Path, value: str) -> None:
        if path == runtime_group / "cgroup.procs":
            runtime_writes.append(value)
            if value in {"1001", "1002"}:
                remaining = "1002\n" if value == "1001" else ""
                (base / "cgroup.procs").write_text(remaining, encoding="utf-8")
            return
        path.write_text(f"{value}\n", encoding="utf-8")

    monkeypatch.setattr("core.resource_governance._write_cgroup_value", fake_write_cgroup_value)
    monkeypatch.setattr("core.resource_governance._runtime_process_tree_pids", lambda pid=None: {1001, 1002})

    assert governor.apply_to_pid(4321, label="test") is True

    assert runtime_writes == ["1001", "1002"]
    assert (base / "cgroup.subtree_control").read_text(encoding="utf-8") == "+memory +cpu +io +pids\n"
    assert (group / "cgroup.procs").read_text(encoding="utf-8") == "4321\n"
    assert (group / "memory.max").read_text(encoding="utf-8").strip() == str(1382 * MIB)
    assert (group / "memory.high").read_text(encoding="utf-8").strip() == str(1174 * MIB)
    assert (group / "memory.oom.group").read_text(encoding="utf-8").strip() == "1"
    assert (group / "cpu.weight").read_text(encoding="utf-8").strip() == "50"
    assert (group / "io.weight").read_text(encoding="utf-8").strip() == "default 50"
    assert (group / "pids.max").read_text(encoding="utf-8").strip() == "512"


def test_governor_falls_back_when_memory_controller_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(2 * 1024 * MIB), encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "auto"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir

    def mkdir_with_minimal_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            (group / "cgroup.procs").write_text("", encoding="utf-8")
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_minimal_files)

    assert governor.apply_to_pid(4321, label="test") is False
    assert governor.group_path is None


def test_governor_falls_back_when_runtime_leaf_cannot_be_created(tmp_path: Path) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(2 * 1024 * MIB), encoding="utf-8")
    (base / "cgroup.controllers").write_text("memory cpu io pids\n", encoding="utf-8")
    (base / "cgroup.subtree_control").write_text("", encoding="utf-8")
    (base / "cgroup.procs").write_text("1001\n", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "auto"}, root=root, base_cgroup=base)

    assert governor.apply_to_pid(4321, label="test") is False
    assert governor.group_path is None


def test_governor_falls_back_when_base_has_foreign_member_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(512 * MIB), encoding="utf-8")
    (base / "cgroup.procs").write_text("1001\n2002\n", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "auto"}, root=root, base_cgroup=base)
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir
    writes: list[str] = []

    def mkdir_with_runtime_file(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_runtime_file)
    monkeypatch.setattr("core.resource_governance._runtime_process_tree_pids", lambda pid=None: {1001})
    monkeypatch.setattr(
        "core.resource_governance._write_cgroup_value",
        lambda path, value: writes.append(value),
    )

    assert governor.apply_to_pid(4321, label="test") is False
    assert governor.group_path is None
    assert writes == []


def test_governor_allows_known_runtime_sibling_pids_during_base_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(512 * MIB), encoding="utf-8")
    (base / "cgroup.procs").write_text("1001\n1002\n5001\n", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir
    runtime_writes: list[str] = []
    agent_writes: list[str] = []

    def mkdir_with_cgroup_procs(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            (group / "cgroup.procs").write_text("", encoding="utf-8")
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    def fake_write_cgroup_value(path: Path, value: str) -> None:
        if path == runtime_group / "cgroup.procs":
            runtime_writes.append(value)
        elif path == group / "cgroup.procs":
            agent_writes.append(value)
        else:
            path.write_text(f"{value}\n", encoding="utf-8")
            return
        remaining = [pid for pid in _base_members() if pid != value]
        (base / "cgroup.procs").write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")

    def _base_members() -> list[str]:
        text = (base / "cgroup.procs").read_text(encoding="utf-8")
        return [pid for pid in text.split() if pid]

    monkeypatch.setattr(Path, "mkdir", mkdir_with_cgroup_procs)
    monkeypatch.setattr("core.resource_governance._runtime_process_tree_pids", lambda pid=None: {1001})
    monkeypatch.setattr("core.resource_governance._known_avibe_runtime_sibling_pids", lambda pids: {1002})
    monkeypatch.setattr("core.resource_governance._write_cgroup_value", fake_write_cgroup_value)

    assert governor.apply_to_pid(5001, label="test") is True
    assert runtime_writes == ["1001", "1002"]
    assert agent_writes == ["5001", "5001"]
    assert governor.group_path == group


def test_governor_allows_known_agent_pids_during_base_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(512 * MIB), encoding="utf-8")
    (base / "cgroup.procs").write_text("1001\n5001\n5002\n", encoding="utf-8")
    (base / "cgroup.controllers").write_text("cpu io pids\n", encoding="utf-8")
    (base / "cgroup.subtree_control").write_text("", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir
    runtime_writes: list[str] = []
    agent_writes: list[str] = []

    def mkdir_with_controller_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            for name in ("cpu.weight", "io.weight", "pids.max", "cgroup.procs"):
                (group / name).write_text("", encoding="utf-8")
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    def fake_write_cgroup_value(path: Path, value: str) -> None:
        if path == runtime_group / "cgroup.procs":
            runtime_writes.append(value)
        elif path == group / "cgroup.procs":
            agent_writes.append(value)
        else:
            path.write_text(f"{value}\n", encoding="utf-8")
            return
        remaining = [pid for pid in _base_members() if pid != value]
        (base / "cgroup.procs").write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")

    def _base_members() -> list[str]:
        text = (base / "cgroup.procs").read_text(encoding="utf-8")
        return [pid for pid in text.split() if pid]

    monkeypatch.setattr(Path, "mkdir", mkdir_with_controller_files)
    monkeypatch.setattr("core.resource_governance._runtime_process_tree_pids", lambda pid=None: {1001})
    monkeypatch.setattr("core.resource_governance._descendant_pids", lambda pid: [5002])
    monkeypatch.setattr("core.resource_governance._write_cgroup_value", fake_write_cgroup_value)

    assert governor.apply_to_pid(5001, label="test") is True
    assert runtime_writes == ["1001"]
    assert agent_writes == ["5001", "5002", "5001", "5002"]
    assert governor.group_path == group


def test_governor_falls_back_when_subtree_control_enable_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(2 * 1024 * MIB), encoding="utf-8")
    (base / "cgroup.controllers").write_text("memory cpu io pids\n", encoding="utf-8")
    (base / "cgroup.subtree_control").write_text("", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "auto"}, root=root, base_cgroup=base)
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir

    def mkdir_with_runtime_file(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    def fake_write_cgroup_value(path: Path, value: str) -> None:
        if path == base / "cgroup.subtree_control":
            raise OSError("busy")
        path.write_text(f"{value}\n", encoding="utf-8")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_runtime_file)
    monkeypatch.setattr("core.resource_governance._write_cgroup_value", fake_write_cgroup_value)

    assert governor.apply_to_pid(4321, label="test") is False
    assert governor.group_path is None
    assert not (base / "avibe-agents").exists()


def test_governor_uses_parent_when_current_cgroup_is_runtime_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    runtime_group = base / "avibe-runtime"
    runtime_group.mkdir(parents=True)
    (base / "memory.max").write_text(str(512 * MIB), encoding="utf-8")
    (base / "cgroup.controllers").write_text("cpu io pids\n", encoding="utf-8")
    (base / "cgroup.subtree_control").write_text("", encoding="utf-8")
    (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=runtime_group)
    group = base / "avibe-agents"
    original_mkdir = Path.mkdir

    def mkdir_with_controller_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            for name in ("cpu.weight", "io.weight", "pids.max", "cgroup.procs"):
                (group / name).write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_controller_files)

    assert governor.apply_to_pid(4321, label="test") is True
    assert governor.group_path == group
    assert not (runtime_group / "avibe-agents").exists()
    assert (base / "cgroup.subtree_control").read_text(encoding="utf-8") == "+cpu +io +pids\n"


def test_governor_moves_existing_descendant_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cgroup"
    base = root / "service"
    base.mkdir(parents=True)
    (base / "memory.max").write_text(str(512 * MIB), encoding="utf-8")

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    runtime_group = base / "avibe-runtime"
    original_mkdir = Path.mkdir
    writes: list[str] = []

    def mkdir_with_controller_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            for name in ("cpu.weight", "io.weight", "pids.max", "cgroup.procs"):
                (group / name).write_text("", encoding="utf-8")
        if path == runtime_group:
            (runtime_group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    def fake_write_cgroup_value(path: Path, value: str) -> None:
        if path == group / "cgroup.procs":
            writes.append(value)
            return
        path.write_text(f"{value}\n", encoding="utf-8")

    monkeypatch.setattr(Path, "mkdir", mkdir_with_controller_files)
    monkeypatch.setattr("core.resource_governance._descendant_pids", lambda pid: [5002, 5003])
    monkeypatch.setattr("core.resource_governance._write_cgroup_value", fake_write_cgroup_value)

    assert governor.apply_to_pid(5001, label="test") is True
    assert writes == ["5001", "5002", "5003"]
