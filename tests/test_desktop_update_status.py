from pathlib import Path

from core.computer_use import COMPUTER_USE_STATE_DIR_ENV
from vibe.desktop_update_status import desktop_update_status_path


def test_desktop_update_status_path_ignores_avibe_home(tmp_path: Path) -> None:
    """A Runtime the shell adopted finds the shell's status under any AVIBE_HOME."""

    env = {"AVIBE_HOME": str(tmp_path / "runtime-home")}
    assert desktop_update_status_path(
        environ=env,
        home=tmp_path / "user",
        platform="darwin",
    ) == tmp_path / "user" / "Library" / "Application Support" / "bot.avibe.desktop" / "update-status.json"
    env[COMPUTER_USE_STATE_DIR_ENV] = str(tmp_path / "isolated-desktop")
    assert desktop_update_status_path(
        environ=env,
        home=tmp_path / "user",
        platform="darwin",
    ) == tmp_path / "isolated-desktop" / "update-status.json"
