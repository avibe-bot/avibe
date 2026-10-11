from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.computer_use import (
    COMPUTER_USE_STATE_DIR_ENV,
    COMPUTER_USE_TOOL_SNAPSHOT_SHA256,
    COMPUTER_USE_TOOL_NAMES,
    ComputerUseConfigFingerprint,
    ComputerUseConfigReconciler,
    SnapshotVerifier,
    computer_use_config_fingerprint,
    desktop_computer_use_dir,
    desktop_computer_use_shell_is_live,
    effective_computer_use_status,
    managed_mcp_server_spec,
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


def test_tool_contract_has_exactly_the_approved_28_names() -> None:
    """Protect the reviewed v1 surface from silently widening with upstream."""

    assert len(COMPUTER_USE_TOOL_NAMES) == 28
    assert len(set(COMPUTER_USE_TOOL_NAMES)) == 28
    assert {
        "check_permissions",
        "bring_to_front",
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


def test_managed_spec_exists_only_for_enabled_verified_snapshot(tmp_path: Path) -> None:
    """Broken optional assets must disable injection without breaking agents."""

    state_path, snapshot = _write_state(tmp_path)
    spec = managed_mcp_server_spec(
        state_path=state_path,
        python_executable="/test/python",
        verifier=SnapshotVerifier(),
    )
    assert spec is not None
    assert spec.name == "avibe_computer"
    assert spec.command == "/test/python"
    assert spec.args[0] == "-I"
    assert spec.args[1].endswith("/core/computer_server.py")
    assert spec.env == {COMPUTER_USE_STATE_DIR_ENV: str(tmp_path)}

    snapshot.write_text("corrupt", encoding="utf-8")
    assert (
        managed_mcp_server_spec(
            state_path=state_path,
            verifier=SnapshotVerifier(),
        )
        is None
    )

    _write_state(tmp_path, enabled=False)
    assert (
        managed_mcp_server_spec(
            state_path=state_path,
            verifier=SnapshotVerifier(),
        )
        is None
    )


def test_config_fingerprint_excludes_daemon_availability(tmp_path: Path) -> None:
    """A daemon state change must not restart any backend process."""

    state_path, _snapshot = _write_state(tmp_path, state="starting")
    verifier = SnapshotVerifier()
    before = computer_use_config_fingerprint(state_path=state_path, verifier=verifier)
    _write_state(tmp_path, state="ready")
    after = computer_use_config_fingerprint(state_path=state_path, verifier=verifier)
    assert before == after == ComputerUseConfigFingerprint(True, before.expected_snapshot_sha256, True)


@pytest.mark.asyncio
async def test_reconciler_renews_only_after_the_final_config_triple_changes(
    tmp_path: Path,
) -> None:
    """Polling compares final config, including snapshot deletion/restoration."""

    state_path, snapshot = _write_state(tmp_path)
    renewed: list[str] = []

    async def renew(backend: str) -> None:
        renewed.append(backend)

    reconciler = ComputerUseConfigReconciler(
        renew,
        lambda: ["claude", "codex", "opencode"],
        state_path=state_path,
        verifier=SnapshotVerifier(),
    )

    _write_state(tmp_path, state="starting")
    assert await reconciler.check_once() is False
    assert renewed == []

    snapshot.unlink()
    assert await reconciler.check_once() is True
    assert renewed == ["claude", "codex", "opencode"]

    renewed.clear()
    snapshot.write_bytes(b'{"tools":[]}\n')
    assert await reconciler.check_once() is True
    assert renewed == ["claude", "codex", "opencode"]

    renewed.clear()
    _write_state(tmp_path, enabled=False)
    _write_state(tmp_path, enabled=True)
    assert await reconciler.check_once() is False
    assert renewed == []


@pytest.mark.asyncio
async def test_reconciler_confirms_each_consumer_and_retries_one_failure(
    tmp_path: Path,
) -> None:
    """One failed backend must not strand itself or consumers after it."""

    state_path, _snapshot = _write_state(tmp_path, enabled=False)
    attempts: list[str] = []
    failed_once = False

    async def renew(backend: str) -> None:
        nonlocal failed_once
        attempts.append(backend)
        if backend == "claude" and not failed_once:
            failed_once = True
            raise RuntimeError("transient")

    reconciler = ComputerUseConfigReconciler(
        renew,
        lambda: ["claude", "codex", "opencode"],
        state_path=state_path,
        verifier=SnapshotVerifier(),
    )
    _write_state(tmp_path, enabled=True)

    assert await reconciler.check_once() is True
    assert attempts == ["claude", "codex", "opencode"]

    attempts.clear()
    assert await reconciler.check_once() is True
    assert attempts == ["claude"]

    attempts.clear()
    assert await reconciler.check_once() is False
    assert attempts == []
