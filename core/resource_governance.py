"""Best-effort Linux cgroup v2 resource governance for agent workloads."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import paths as config_paths

logger = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_GROUP_NAME = "avibe-agents"
DEFAULT_RUNTIME_GROUP_NAME = "avibe-runtime"
DEFAULT_AGENT_CPU_WEIGHT = 50
DEFAULT_AGENT_IO_WEIGHT = 50
DEFAULT_AGENT_PIDS_MAX = 512
DEFAULT_AGENT_OOM_SCORE_ADJ = 500
MIN_AGENT_MEMORY_MAX_BYTES = 512 * 1024 * 1024
MIB = 1024 * 1024
AGENT_CONTROLLERS = ("memory", "cpu", "io", "pids")
CONTROLLER_GOVERNOR_ATTR = "_avibe_controller_owned_resource_governor"
AVIBE_RUNTIME_CWD_CMD_MARKERS = (
    "incus_regression_supervisor.py",
    "from vibe.ui_server import run_ui_server",
)


@dataclass(frozen=True)
class AgentResourceLimits:
    memory_high: int | None
    memory_max: int | None
    cpu_weight: int = DEFAULT_AGENT_CPU_WEIGHT
    io_weight: int = DEFAULT_AGENT_IO_WEIGHT
    pids_max: int = DEFAULT_AGENT_PIDS_MAX
    oom_score_adj: int = DEFAULT_AGENT_OOM_SCORE_ADJ


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _parse_memory_value(value: str | None) -> int | None:
    if not value or value == "max":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _parse_pid(value: str | None) -> int | None:
    if not value:
        return None
    try:
        pid = int(value.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def _round_down_mib(value: int) -> int:
    return max(MIB, (int(value) // MIB) * MIB)


def detect_cgroup_root() -> Path | None:
    if os.name != "posix" or not CGROUP_ROOT.exists():
        return None
    if not (CGROUP_ROOT / "cgroup.controllers").exists():
        return None
    return CGROUP_ROOT


def current_cgroup_path(root: Path | None = None) -> Path | None:
    root = root or detect_cgroup_root()
    if root is None:
        return None
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            rel = parts[2].strip("/")
            return root / rel if rel else root
    return None


def tenant_memory_limit_bytes(cgroup: Path | None = None, root: Path | None = None) -> int | None:
    root = root or detect_cgroup_root()
    if root is None:
        return None
    cursor = cgroup or current_cgroup_path(root)
    if cursor is None:
        return None

    root = root.resolve()
    try:
        cursor = cursor.resolve()
    except OSError:
        return None

    while True:
        limit = _parse_memory_value(_read_text(cursor / "memory.max"))
        if limit is not None:
            return limit
        if cursor == root:
            break
        cursor = cursor.parent

    meminfo = _read_text(Path("/proc/meminfo"))
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1]) * 1024
                    except ValueError:
                        return None
    return None


def derive_agent_limits(
    tenant_memory_bytes: int | None,
    config: dict[str, Any] | None = None,
) -> AgentResourceLimits:
    config = config or {}

    def _int_config(name: str, default: int) -> int:
        value = config.get(name)
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    cpu_weight = max(1, min(10_000, _int_config("agent_cpu_weight", DEFAULT_AGENT_CPU_WEIGHT)))
    io_weight = max(1, min(10_000, _int_config("agent_io_weight", DEFAULT_AGENT_IO_WEIGHT)))
    pids_max = max(32, _int_config("agent_pids_max", DEFAULT_AGENT_PIDS_MAX))
    oom_score_adj = max(-1000, min(1000, _int_config("agent_oom_score_adj", DEFAULT_AGENT_OOM_SCORE_ADJ)))

    explicit_max = _int_config("agent_memory_max_bytes", 0)
    explicit_high = _int_config("agent_memory_high_bytes", 0)
    if explicit_max > 0:
        memory_max = _round_down_mib(explicit_max)
        memory_high = _round_down_mib(explicit_high) if explicit_high > 0 else _round_down_mib(memory_max * 85 // 100)
        return AgentResourceLimits(
            memory_high=min(memory_high, memory_max),
            memory_max=memory_max,
            cpu_weight=cpu_weight,
            io_weight=io_weight,
            pids_max=pids_max,
            oom_score_adj=oom_score_adj,
        )

    if tenant_memory_bytes is None or tenant_memory_bytes < 1024 * MIB:
        return AgentResourceLimits(
            memory_high=None,
            memory_max=None,
            cpu_weight=cpu_weight,
            io_weight=io_weight,
            pids_max=pids_max,
            oom_score_adj=oom_score_adj,
        )

    avibe_reserve = max(384 * MIB, min(1024 * MIB, tenant_memory_bytes * 20 // 100))
    system_headroom = max(256 * MIB, tenant_memory_bytes * 12 // 100)
    memory_max = _round_down_mib(tenant_memory_bytes - avibe_reserve - system_headroom)
    if memory_max < MIN_AGENT_MEMORY_MAX_BYTES:
        memory_max = MIN_AGENT_MEMORY_MAX_BYTES
    # Keep a soft throttle below the hard cap so the agent domain slows before
    # it hits OOM, but keep enough burst room for model/tool startup spikes.
    memory_high = _round_down_mib(memory_max * 85 // 100)
    return AgentResourceLimits(
        memory_high=memory_high,
        memory_max=memory_max,
        cpu_weight=cpu_weight,
        io_weight=io_weight,
        pids_max=pids_max,
        oom_score_adj=oom_score_adj,
    )


def _format_cgroup_memory(value: int | None) -> str:
    return "max" if value is None else str(int(value))


def _write_cgroup_value(path: Path, value: str) -> None:
    path.write_text(f"{value}\n", encoding="utf-8")


def _cgroup_member_pids(cgroup: Path) -> list[int]:
    text = _read_text(cgroup / "cgroup.procs")
    if not text:
        return []
    pids: list[int] = []
    for raw_pid in text.split():
        try:
            pids.append(int(raw_pid))
        except ValueError:
            continue
    return pids


def _runtime_process_tree_pids(pid: int | None = None) -> set[int]:
    root_pid = pid or os.getpid()
    return {root_pid, *_descendant_pids(root_pid)}


def _pid_cmdline(pid: int) -> str | None:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return data.replace(b"\0", b" ").decode(errors="replace").strip()


def _first_cmd_arg(cmdline: str) -> str:
    parts = cmdline.split(maxsplit=1)
    return parts[0] if parts else ""


def _is_cloudflared_cmd(cmdline: str) -> bool:
    executable = Path(_first_cmd_arg(cmdline)).name.lower()
    return executable in {"cloudflared", "cloudflared.exe"}


def _pid_uid(pid: int) -> int | None:
    status = _read_text(Path(f"/proc/{pid}/status"))
    if not status:
        return None
    for line in status.splitlines():
        if not line.startswith("Uid:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def _pid_cwd(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return None


def _current_runtime_cwd() -> Path | None:
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def _path_variants(path: Path) -> set[str]:
    variants = {str(path)}
    try:
        variants.add(str(path.resolve()))
    except OSError:
        pass
    return variants


def _is_under_runtime_dir(cmdline: str) -> bool:
    for runtime_dir in _path_variants(config_paths.get_runtime_dir()):
        if f"{runtime_dir}/" in cmdline:
            return True
    return False


def _is_managed_cloudflared(pid: int, cmdline: str) -> bool:
    if not _is_cloudflared_cmd(cmdline):
        return False
    expected_pid = _parse_pid(_read_text(config_paths.get_runtime_remote_access_pid_path()))
    if expected_pid == pid:
        return True
    for home_dir in _path_variants(config_paths.get_vibe_remote_dir()):
        managed_binary = f"{home_dir}/bin/{_first_cmd_arg(cmdline).split('/')[-1]}"
        if cmdline == managed_binary or cmdline.startswith(f"{managed_binary} "):
            return True
    return False


def _is_known_avibe_runtime_member(pid: int, *, uid: int | None = None) -> bool:
    expected_uid = os.geteuid() if uid is None else uid
    pid_uid = _pid_uid(pid)
    if pid_uid != expected_uid:
        return False
    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return False
    if _is_under_runtime_dir(cmdline):
        return True
    if _is_managed_cloudflared(pid, cmdline):
        return True

    runtime_cwd = _current_runtime_cwd()
    pid_cwd = _pid_cwd(pid)
    if runtime_cwd is None or pid_cwd != runtime_cwd:
        return False
    if f"{runtime_cwd}/main.py" in cmdline:
        return True
    return any(marker in cmdline for marker in AVIBE_RUNTIME_CWD_CMD_MARKERS)


def _known_avibe_runtime_sibling_pids(pids: list[int]) -> set[int]:
    current_uid = os.geteuid()
    return {pid for pid in pids if _is_known_avibe_runtime_member(pid, uid=current_uid)}


def _descendant_pids(pid: int) -> list[int]:
    pending = [pid]
    descendants: list[int] = []
    seen = {pid}
    while pending:
        current = pending.pop()
        try:
            task_dirs = list(Path(f"/proc/{current}/task").iterdir())
        except OSError:
            task_dirs = [Path(f"/proc/{current}/task/{current}")]
        for task_dir in task_dirs:
            children = _read_text(task_dir / "children")
            if not children:
                continue
            for raw_child in children.split():
                try:
                    child = int(raw_child)
                except ValueError:
                    continue
                if child in seen:
                    continue
                seen.add(child)
                descendants.append(child)
                pending.append(child)
    return descendants


class AgentResourceGovernor:
    """Move backend runtime roots into one shared constrained cgroup."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        root: Path | None = None,
        base_cgroup: Path | None = None,
    ) -> None:
        self.config = config or {}
        self.root = root
        self.base_cgroup = base_cgroup
        self._base: Path | None = None
        self._group: Path | None = None
        self._limits: AgentResourceLimits | None = None
        self._disabled_reason: str | None = None

    @property
    def mode(self) -> str:
        value = str(self.config.get("mode") or "auto").strip().lower()
        return value if value in {"auto", "enabled", "disabled"} else "auto"

    @property
    def group_path(self) -> Path | None:
        return self._group

    @property
    def limits(self) -> AgentResourceLimits | None:
        return self._limits

    def update_config(self, config: dict[str, Any] | None) -> None:
        self.config = config or {}
        self._base = None
        self._group = None
        self._limits = None
        self._disabled_reason = None

    def apply_to_pid(self, pid: int | None, *, label: str = "agent") -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        descendant_pids = _descendant_pids(pid)
        known_agent_pids = {pid, *descendant_pids}
        group = self._ensure_group(known_agent_pids=known_agent_pids)
        if group is None:
            return False
        moved = self._move_pid(group, pid, label=label)
        for child_pid in descendant_pids:
            self._move_pid(group, child_pid, label=f"{label} child", warn=False)
        return moved

    def _move_pid(self, group: Path, pid: int, *, label: str, warn: bool = True) -> bool:
        try:
            _write_cgroup_value(group / "cgroup.procs", str(pid))
            self._apply_oom_score_adj(pid)
            return True
        except OSError as exc:
            log = logger.warning if warn else logger.debug
            log("Agent resource governance could not move %s pid=%s into cgroup: %s", label, pid, exc)
            return False

    def _ensure_group(self, *, known_agent_pids: set[int] | None = None) -> Path | None:
        if self._group is not None:
            return self._group
        if self.mode == "disabled":
            self._disabled_reason = "disabled"
            return None
        root = self.root or detect_cgroup_root()
        if root is None:
            self._disabled_reason = "no-cgroup-v2"
            return None
        base = self._base_cgroup(root)
        if base is None:
            self._disabled_reason = "unknown-current-cgroup"
            return None
        self._base = base
        group_name = str(self.config.get("agent_group_name") or DEFAULT_GROUP_NAME).strip() or DEFAULT_GROUP_NAME
        runtime_group_name = (
            str(self.config.get("runtime_group_name") or DEFAULT_RUNTIME_GROUP_NAME).strip()
            or DEFAULT_RUNTIME_GROUP_NAME
        )
        if runtime_group_name == group_name:
            self._disabled_reason = "runtime-and-agent-cgroup-collide"
            return None
        group = base / group_name
        runtime_group = base / runtime_group_name
        try:
            self._prepare_base_cgroup(base, runtime_group, group, root, known_agent_pids=known_agent_pids)
            self._enable_subtree_controllers(base)
            group.mkdir(exist_ok=True)
            limits = derive_agent_limits(tenant_memory_limit_bytes(base, root), self.config)
            self._configure_group(group, limits)
        except OSError as exc:
            self._disabled_reason = str(exc)
            if self.mode == "enabled":
                logger.warning("Agent resource governance requested but cgroup setup failed: %s", exc)
            else:
                logger.info("Agent resource governance unavailable: %s", exc)
            return None
        self._group = group
        self._limits = limits
        logger.info(
            "Agent resource governance enabled group=%s memory_high=%s memory_max=%s cpu_weight=%s io_weight=%s pids_max=%s",
            group,
            limits.memory_high,
            limits.memory_max,
            limits.cpu_weight,
            limits.io_weight,
            limits.pids_max,
        )
        return group

    def _base_cgroup(self, root: Path) -> Path | None:
        runtime_group_name = (
            str(self.config.get("runtime_group_name") or DEFAULT_RUNTIME_GROUP_NAME).strip()
            or DEFAULT_RUNTIME_GROUP_NAME
        )
        if self.base_cgroup is not None:
            return self._parent_if_runtime_leaf(self.base_cgroup, runtime_group_name)
        if self._base is not None:
            return self._base
        current = current_cgroup_path(root)
        if current is None:
            return None
        return self._parent_if_runtime_leaf(current, runtime_group_name)

    def _parent_if_runtime_leaf(self, cgroup: Path, runtime_group_name: str) -> Path:
        if cgroup.name == runtime_group_name and cgroup.parent != cgroup:
            return cgroup.parent
        return cgroup

    def _prepare_base_cgroup(
        self,
        base: Path,
        runtime_group: Path,
        agent_group: Path,
        root: Path,
        *,
        known_agent_pids: set[int] | None = None,
    ) -> None:
        try:
            is_root = base.resolve() == root.resolve()
        except OSError:
            is_root = False
        if is_root:
            return
        # cgroup v2 rejects enabling domain controllers in a non-root cgroup
        # that still has member processes. Keep Avibe in a runtime leaf and
        # create the agent cgroup as a sibling under the now-empty parent.
        runtime_group.mkdir(exist_ok=True)
        if not (runtime_group / "cgroup.procs").exists():
            raise OSError(f"runtime cgroup.procs is unavailable in {runtime_group}")
        self._move_base_processes_to_runtime_leaf(
            base,
            runtime_group,
            agent_group,
            known_agent_pids=known_agent_pids,
        )

    def _move_base_processes_to_runtime_leaf(
        self,
        base: Path,
        runtime_group: Path,
        agent_group: Path,
        *,
        known_agent_pids: set[int] | None = None,
    ) -> None:
        known_agent_pids = known_agent_pids or set()
        agent_group_ready = False
        for _ in range(3):
            pids = [pid for pid in _cgroup_member_pids(base) if pid > 0]
            if not pids:
                return
            runtime_pids = _runtime_process_tree_pids()
            runtime_sibling_pids = _known_avibe_runtime_sibling_pids(pids)
            managed_pids = runtime_pids | runtime_sibling_pids | known_agent_pids
            foreign_pids = [pid for pid in pids if pid not in managed_pids]
            if foreign_pids:
                raise OSError(f"runtime cgroup has non-Avibe member pids: {base}")
            moved = False
            for pid in pids:
                target_group = runtime_group
                target_label = "Avibe runtime"
                if pid in known_agent_pids:
                    if not agent_group_ready:
                        agent_group.mkdir(exist_ok=True)
                        if not (agent_group / "cgroup.procs").exists():
                            raise OSError(f"agent cgroup.procs is unavailable in {agent_group}")
                        agent_group_ready = True
                    target_group = agent_group
                    target_label = "known agent"
                try:
                    _write_cgroup_value(target_group / "cgroup.procs", str(pid))
                    moved = True
                except OSError as exc:
                    logger.debug(
                        "Failed to move %s pid=%s into cgroup %s: %s",
                        target_label,
                        pid,
                        target_group,
                        exc,
                    )
            if not moved:
                break
        remaining = _cgroup_member_pids(base)
        if remaining:
            raise OSError(f"runtime cgroup still has member pids after migration: {base}")

    def _enable_subtree_controllers(self, base: Path) -> None:
        available = set((_read_text(base / "cgroup.controllers") or "").split())
        requested = [controller for controller in AGENT_CONTROLLERS if controller in available]
        if not requested or not (base / "cgroup.subtree_control").exists():
            return
        try:
            _write_cgroup_value(base / "cgroup.subtree_control", " ".join(f"+{controller}" for controller in requested))
        except OSError as exc:
            raise OSError(f"failed to enable delegated cgroup controllers under {base}: {exc}") from exc

    def _configure_group(self, group: Path, limits: AgentResourceLimits) -> None:
        if not (group / "cgroup.procs").exists():
            raise OSError(f"cgroup.procs is unavailable in {group}")
        if limits.memory_max is not None:
            missing_memory_files = [
                str(path.name) for path in (group / "memory.high", group / "memory.max") if not path.exists()
            ]
            if missing_memory_files:
                raise OSError(f"memory controller unavailable in {group}: missing {', '.join(missing_memory_files)}")
        if (group / "memory.high").exists():
            _write_cgroup_value(group / "memory.high", _format_cgroup_memory(limits.memory_high))
        if (group / "memory.max").exists():
            _write_cgroup_value(group / "memory.max", _format_cgroup_memory(limits.memory_max))
        if (group / "memory.oom.group").exists():
            _write_cgroup_value(group / "memory.oom.group", "1")
        if (group / "cpu.weight").exists():
            _write_cgroup_value(group / "cpu.weight", str(limits.cpu_weight))
        if (group / "io.weight").exists():
            _write_cgroup_value(group / "io.weight", f"default {limits.io_weight}")
        if (group / "pids.max").exists():
            _write_cgroup_value(group / "pids.max", str(limits.pids_max))

    def _apply_oom_score_adj(self, pid: int) -> None:
        limits = self._limits
        if limits is None:
            return
        try:
            Path(f"/proc/{pid}/oom_score_adj").write_text(f"{limits.oom_score_adj}\n", encoding="utf-8")
        except OSError:
            logger.debug("Failed to apply oom_score_adj to agent pid=%s", pid, exc_info=True)


def config_from_controller(controller: Any) -> dict[str, Any]:
    return config_from_runtime(getattr(controller, "config", None))


def config_from_runtime(config: Any) -> dict[str, Any]:
    runtime_config = getattr(config, "resource_governance", None)
    if isinstance(runtime_config, dict):
        return runtime_config
    nested_runtime = getattr(config, "runtime", None)
    nested_config = getattr(nested_runtime, "resource_governance", None)
    if isinstance(nested_config, dict):
        return nested_config
    if isinstance(config, dict):
        direct = config.get("resource_governance")
        if isinstance(direct, dict):
            return direct
        runtime = config.get("runtime")
        if isinstance(runtime, dict):
            nested = runtime.get("resource_governance")
            if isinstance(nested, dict):
                return nested
    return {"mode": "auto"}


def mark_controller_resource_governor(governor: Any) -> None:
    setattr(governor, CONTROLLER_GOVERNOR_ATTR, True)


def is_controller_resource_governor(governor: Any) -> bool:
    return bool(getattr(governor, CONTROLLER_GOVERNOR_ATTR, False))


def governor_from_controller(controller: Any) -> AgentResourceGovernor:
    existing = getattr(controller, "_agent_resource_governor", None)
    if isinstance(existing, AgentResourceGovernor):
        mark_controller_resource_governor(existing)
        return existing
    governor = AgentResourceGovernor(config_from_controller(controller))
    mark_controller_resource_governor(governor)
    setattr(controller, "_agent_resource_governor", governor)
    return governor
