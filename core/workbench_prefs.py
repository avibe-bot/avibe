"""Global workbench preferences persisted in ``state_meta`` (KV).

Small global toggles the Web UI flips and every session reads — distinct from
the JSON config file (``config/v2_config.py``) and from per-session settings.
Mirrors the Dock store pattern (``core/dock_store.py``) over the generic
``state_meta`` helpers in ``core/chat_discovery.py``.
"""

from __future__ import annotations

from pathlib import Path

from core.chat_discovery import get_state_meta, set_state_meta

# Global toggle for the workbench chat's unified background-work banner.
# Default ON; when off the banner never renders in ANY session. The underlying
# runtime-state data/API is unaffected — this only gates presentation.
BACKGROUND_WORK_BANNER_KEY = "workbench.background_work_banner_enabled"


def get_background_work_banner_enabled(*, db_path: Path | None = None) -> bool:
    """Whether the background-work banner may render. Absent/malformed → ON."""
    raw = get_state_meta(BACKGROUND_WORK_BANNER_KEY, db_path=db_path)
    return raw if isinstance(raw, bool) else True


def set_background_work_banner_enabled(enabled: bool, *, db_path: Path | None = None) -> bool:
    """Persist the banner toggle. Returns the stored boolean."""
    value = bool(enabled)
    set_state_meta(BACKGROUND_WORK_BANNER_KEY, value, db_path=db_path)
    return value
