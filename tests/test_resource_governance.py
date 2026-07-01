from pathlib import Path

import pytest

from core.resource_governance import (
    MIB,
    AgentResourceGovernor,
    derive_agent_limits,
    tenant_memory_limit_bytes,
)


def test_derive_agent_limits_uses_single_aggregate_budget() -> None:
    limits = derive_agent_limits(4 * 1024 * MIB)

    assert limits.memory_max == 2785 * MIB
    assert limits.memory_high == 2367 * MIB
    assert limits.cpu_weight == 150
    assert limits.io_weight == 100
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

    governor = AgentResourceGovernor({"mode": "enabled"}, root=root, base_cgroup=base)
    group = base / "avibe-agents"
    original_mkdir = Path.mkdir

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
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_controller_files)

    assert governor.apply_to_pid(4321, label="test") is True

    assert (base / "cgroup.subtree_control").read_text(encoding="utf-8") == "+memory +cpu +io +pids\n"
    assert (group / "cgroup.procs").read_text(encoding="utf-8") == "4321\n"
    assert (group / "memory.max").read_text(encoding="utf-8").strip() == str(1382 * MIB)
    assert (group / "memory.high").read_text(encoding="utf-8").strip() == str(1174 * MIB)
    assert (group / "memory.oom.group").read_text(encoding="utf-8").strip() == "1"
    assert (group / "cpu.weight").read_text(encoding="utf-8").strip() == "150"
    assert (group / "io.weight").read_text(encoding="utf-8").strip() == "default 100"
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
    original_mkdir = Path.mkdir

    def mkdir_with_minimal_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            (group / "cgroup.procs").write_text("", encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_with_minimal_files)

    assert governor.apply_to_pid(4321, label="test") is False
    assert governor.group_path is None


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
    original_mkdir = Path.mkdir
    writes: list[str] = []

    def mkdir_with_controller_files(path: Path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == group:
            for name in ("cpu.weight", "io.weight", "pids.max", "cgroup.procs"):
                (group / name).write_text("", encoding="utf-8")
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
