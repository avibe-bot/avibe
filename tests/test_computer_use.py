from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.computer_use import (
    COMPUTER_USE_STATE_DIR_ENV,
    COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS,
    COMPUTER_USE_TOOL_SNAPSHOT_SHA256,
    COMPUTER_USE_TOOL_NAMES,
    SnapshotVerifier,
    desktop_computer_use_dir,
    desktop_computer_use_shell_is_live,
    effective_computer_use_status,
)


def _write_state(
    directory: Path,
    *,
    enabled: bool = True,
    state: str = "ready",
    reason: str | None = None,
    snapshot_content: bytes = b'{"tools":[]}\n',
    snapshot_sha256: str | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / "tools.json"
    snapshot.write_bytes(snapshot_content)
    digest = snapshot_sha256 or hashlib.sha256(snapshot_content).hexdigest()
    payload = {
        "schema_version": 1,
        "enabled": enabled,
        "state": state,
        "reason": reason,
        "shell_pid": 123,
        "instance_id": "shell-a",
        "generation": 4,
        "driver_version": "0.31.0",
        "tool_snapshot": {"path": str(snapshot), "sha256": digest},
    }
    if state == "ready":
        payload.update(
            {
                "socket_path": str(directory / "driver.sock"),
                "proxy_executable": str(directory / "cua-driver"),
                "host_bundle_id": "bot.avibe.desktop.acceptance",
            }
        )
    state_path = directory / "computer-use.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path, snapshot


def test_tool_contract_has_exactly_the_approved_29_names() -> None:
    """Protect the reviewed surface from silently widening with upstream."""

    assert len(COMPUTER_USE_TOOL_NAMES) == 29
    assert len(set(COMPUTER_USE_TOOL_NAMES)) == 29
    assert "bring_to_front" in COMPUTER_USE_TOOL_NAMES
    assert {
        "check_permissions",
        "move_cursor",
        "invoke_menu",
    }.isdisjoint(COMPUTER_USE_TOOL_NAMES)


def test_tool_snapshot_digest_matches_core_and_manifest_pins() -> None:
    """Every consumer must approve the exact shipped snapshot bytes."""

    snapshot_path = Path("desktop/cua-driver/tools-v0.31.0.json")
    snapshot_bytes = snapshot_path.read_bytes()
    actual = hashlib.sha256(snapshot_bytes).hexdigest()
    manifest = json.loads(
        Path("desktop/cua-driver/sources.json").read_text(encoding="utf-8")
    )

    assert actual == COMPUTER_USE_TOOL_SNAPSHOT_SHA256
    assert actual == manifest["driver"]["tool_snapshot_sha256"]
    assert "1614e37e369df122b2bb74be4d1c790f304c9325d3b57b301deb62f8c11e8fd6" in COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS
    assert "37b99238c5f59a64ddece25467aa51148ac6267baa928cbb5d3404196d9d4c71" in COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS
    assert "0af1e40dbf8d48303c25d969fa54b54ccff5d0c36f2f7966b4f04964c13e5ba0" in COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS
    assert "edf496838092b2fa21807724b30308c4e9a90a404c9a9b7783a299b71e427144" in COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS
    assert COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS[actual] == frozenset(COMPUTER_USE_TOOL_NAMES)

    mutated = bytearray(snapshot_bytes)
    mutated[0] ^= 1
    assert hashlib.sha256(mutated).hexdigest() != COMPUTER_USE_TOOL_SNAPSHOT_SHA256


def test_desktop_state_path_ignores_avibe_home(tmp_path: Path) -> None:
    """A shell-adopted Runtime must find D even under a different AVIBE_HOME."""

    env = {"AVIBE_HOME": str(tmp_path / "runtime-home")}
    assert desktop_computer_use_dir(
        environ=env,
        home=tmp_path / "user",
        platform="darwin",
    ) == tmp_path / "user" / "Library" / "Application Support" / "bot.avibe.desktop"
    env[COMPUTER_USE_STATE_DIR_ENV] = str(tmp_path / "isolated-desktop")
    assert desktop_computer_use_dir(
        environ=env,
        home=tmp_path / "user",
        platform="darwin",
    ) == tmp_path / "isolated-desktop"


@pytest.mark.parametrize(
    ("arrange", "lock_held", "expected"),
    [
        ("missing", True, True),
        ("invalid", True, True),
        ("stopped", True, False),
        ("off", True, True),
        ("ready", False, False),
        ("ready", True, True),
    ],
)
def test_shell_support_follows_live_native_lock_across_optional_state_shapes(
    tmp_path: Path,
    arrange: str,
    lock_held: bool,
    expected: bool,
) -> None:
    """Support follows shell ownership even while the optional feature is off."""

    state_path = tmp_path / "computer-use.json"
    if arrange == "invalid":
        state_path.write_text("{", encoding="utf-8")
    elif arrange != "missing":
        _write_state(
            tmp_path,
            enabled=arrange != "off",
            state="off" if arrange == "off" else arrange,
        )

    assert (
        desktop_computer_use_shell_is_live(
            state_path=state_path,
            shell_lock_held=lambda _path: lock_held,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        ("missing", ("off", "never_enabled")),
        ("malformed", ("unavailable", "invalid_state_file")),
        ("newer", ("unavailable", "invalid_state_file")),
        ("off", ("off", "toggle_off")),
        ("snapshot_invalid", ("unavailable", "snapshot_invalid")),
        ("shell_missing", ("unavailable", "shell_not_running")),
        ("stopped", ("unavailable", "shell_not_running")),
        ("needs_permission", ("needs_permission", "screen_recording")),
        ("starting", ("starting", None)),
        ("error", ("error", "endpoint_busy")),
        ("needs_runtime", ("needs_runtime", "runtime_unavailable")),
        ("daemon_unreachable", ("unavailable", "daemon_unreachable")),
        ("ready", ("ready", None)),
    ],
)
def test_effective_status_table_is_total(
    tmp_path: Path,
    arrange: str,
    expected: tuple[str, str | None],
) -> None:
    """Each approved first-match row returns one stable status and reason."""

    state_path = tmp_path / "computer-use.json"
    if arrange == "malformed":
        state_path.write_text("{", encoding="utf-8")
    elif arrange == "newer":
        state_path.write_text('{"schema_version":2}', encoding="utf-8")
    elif arrange != "missing":
        state_name = arrange
        reason = None
        enabled = True
        snapshot_sha = None
        if arrange == "off":
            enabled = False
            state_name = "off"
        elif arrange == "snapshot_invalid":
            state_name = "ready"
            snapshot_sha = "0" * 64
        elif arrange == "shell_missing":
            state_name = "ready"
        elif arrange == "needs_permission":
            reason = "screen_recording"
        elif arrange == "error":
            reason = "endpoint_busy"
        elif arrange == "needs_runtime":
            reason = "runtime_unavailable"
        elif arrange == "daemon_unreachable":
            state_name = "ready"
        _write_state(
            tmp_path,
            enabled=enabled,
            state=state_name,
            reason=reason,
            snapshot_sha256=snapshot_sha,
        )

    lock_held = arrange not in {"shell_missing", "missing"}
    socket_ready = arrange == "ready"
    status = effective_computer_use_status(
        state_path=state_path,
        shell_lock_held=lambda _path: lock_held,
        socket_accepts=lambda _path: socket_ready,
        verifier=SnapshotVerifier(),
    )
    assert (status.status, status.reason) == expected
