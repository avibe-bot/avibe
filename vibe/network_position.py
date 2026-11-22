"""The published local binding, not connector health, decides setup-network trust."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from config import paths
from config.v2_config import V2Config, config_recovery_refusal


class BindingState(Enum):
    UNBOUND = "unbound"
    BOUND = "bound"
    UNAVAILABLE = "unavailable"


def _reset_absent() -> bool:
    # AV-13's durable gate outlives its config clear. Only ENOENT means absent,
    # including on recovery from an interrupted reset; never follow a link.
    try:
        (paths.get_state_dir() / "remote-binding-reset.json").lstat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def binding_state(config: V2Config | None) -> BindingState:
    if config is None or config_recovery_refusal(config, "remote_access") or not _reset_absent():
        return BindingState.UNAVAILABLE
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    if cloud is None:
        return BindingState.UNAVAILABLE
    if all((cloud.instance_id.strip(), cloud.instance_secret.strip(), cloud.backend_url.strip())):
        return BindingState.BOUND
    if any((cloud.instance_id, cloud.instance_secret, cloud.tunnel_token, cloud.client_id, cloud.session_secret)):
        return BindingState.UNAVAILABLE
    return BindingState.UNBOUND


@lru_cache(maxsize=1)
def _config_revision(path: Path, revision: tuple[int, ...]) -> V2Config:
    # Used only by read-only admission code. Never return this shared object to
    # a mutator or persist it. Exceptions are not memoized.
    return V2Config.load(path)


def position_snapshot() -> tuple[V2Config | None, BindingState]:
    """One stat per atomic-config revision check, one load per file version.

    The revision is sampled BEFORE the load. Concurrent atomic replacement
    invalidates it on the next call even if publication raced this read. Reset
    intent is checked separately every time, never reused from the cache.
    """
    try:
        path = paths.get_config_path()
        stat = path.stat()
        config = _config_revision(path, (stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size))
        return config, binding_state(config)
    except Exception:
        # An unreadable snapshot is an admission result, also for an idle
        # stream: do not let a reader failure terminate just its watchdog.
        return None, BindingState.UNAVAILABLE
