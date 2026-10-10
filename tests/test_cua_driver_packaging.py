from __future__ import annotations

from pathlib import Path
from runpy import run_path


_SCRIPT = run_path(
    str(Path(__file__).parents[1] / "desktop/scripts/prepare-cua-driver.py")
)


def test_managed_policy_matches_the_pinned_28_tool_snapshot() -> None:
    """Release preparation rejects policy drift before any driver is packaged."""

    _SCRIPT["verify_managed_policy_surface"]()
