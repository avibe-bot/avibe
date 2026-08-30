"""Closed-loop contract evidence for Memory Runtime installation."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

import yaml

from core import managed_runtime
from core.memory import artifact as memory_artifact
from core.memory.artifact import MemoryArtifactManager
from core.memory.artifact_contract import ColdArtifactAdmissionResult
from tests.ui_server_test_helpers import csrf_headers, save_config
from vibe import api
from vibe.ui_server import app


def _manifest_shaped_linux_arm64_archive(tmp_path: Path) -> Path:
    binary = b"manifest-shaped-linux-arm64-python"
    archive = tmp_path / "memory-runtime-1.2.3-linux-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("bin/python")
        member.mode = 0o755
        member.size = len(binary)
        bundle.addfile(member, io.BytesIO(binary))
    manifest = {
        "schema_version": 1,
        "everos_version": "1.2.3",
        "python_version": "3.12.12",
        "lock_sha256": "e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe",
        "lock_id": "uv-lock-sha256:e6acc17e4c0969563d380326e90134965af0822259bb4a9adb4d54433e9737fe",
        "uv_version": "0.9.18",
        "source": "scenario",
        "release_state": "published",
        "provider_root_format": "everos-1.2.3",
        "compatible_provider_root_formats": [],
        "archives": {
            "linux-arm64": {
                "name": archive.name,
                "url": archive.as_uri(),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary).hexdigest(),
                "size": archive.stat().st_size,
                "bin_path": "bin/python",
            }
        },
    }
    manifest_path = tmp_path / "memory-runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_released_wheel_and_archive_acceptance_remains_explicit_residual() -> None:
    """Scenario: MEMORY-RUNTIME-INSTALL-001 evidence boundary."""

    catalog = yaml.safe_load(
        Path("tests/scenarios/memory_runtime_install/catalog.yaml").read_text(
            encoding="utf-8"
        )
    )
    scenario = next(
        row for row in catalog["scenarios"] if row["id"] == "MEMORY-RUNTIME-INSTALL-001"
    )

    assert scenario["status"] == "partial"
    assert "normal wheel" in catalog["manual_evidence"][0]
    assert "real published manifest and archive" in catalog["manual_evidence"][0]


def test_disabled_memory_installs_manifest_shaped_linux_arm64_artifact_through_web_job(
    tmp_path: Path,
    monkeypatch,
    request,
) -> None:
    """Scenario: MEMORY-RUNTIME-INSTALL-001."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    config = save_config(tmp_path)
    assert config.memory.enabled is False
    assert config.memory.legacy_needs_repair is False
    assert config.memory.custom_processing_complete() is False
    manifest_path = _manifest_shaped_linux_arm64_archive(tmp_path)
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "linux-arm64")
    monkeypatch.setattr(memory_artifact, "runtime_platform_tag", lambda: "linux-arm64")
    admitted: list[Path] = []

    def admit(binary: Path) -> ColdArtifactAdmissionResult:
        admitted.append(binary)
        return ColdArtifactAdmissionResult(ok=True, reason=None, duration_ms=41_000)

    monkeypatch.setattr(memory_artifact, "run_cold_artifact_admission", admit)
    manager = MemoryArtifactManager(
        runtime_dir=tmp_path / "avibe-home" / "runtime" / "memory",
        manifest_path=manifest_path,
        provider_root=tmp_path / "avibe-home" / "memory" / "everos-root",
    )
    monkeypatch.setattr(manager, "_admit_error_scrubbers", lambda _binary: None)
    monkeypatch.setattr(manager, "_binary_matches_manifest", lambda _binary, _manifest: True)

    def install() -> dict:
        result = manager.ensure(force=True)
        reason = result.get("reason") if isinstance(result.get("reason"), str) else None
        return {
            "ok": bool(result.get("ok")),
            "message": reason or "installed",
            "output": None,
            "reason": reason,
            "download_error": result.get("download_error"),
        }

    def dependencies() -> dict:
        status = manager.status()
        return {
            "ok": True,
            "deps": [
                {
                    "id": "memory-runtime",
                    "kind": "runtime",
                    "required": False,
                    "installed": bool(status.get("installed")),
                    "version": status.get("version"),
                    "latest_version": status.get("selected_version"),
                    "has_update": False,
                    "status": "ready" if status.get("installed") else status.get("status"),
                    "reason": status.get("reason"),
                    "download_error": status.get("download_error"),
                }
            ],
        }

    monkeypatch.setattr(api, "_prepare_memory_runtime_job", install)
    monkeypatch.setattr(api, "dependencies_status", dependencies)
    with api._AGENT_INSTALL_JOB_LOCK:
        api._AGENT_INSTALL_JOBS.clear()
        api._AGENT_INSTALL_LATEST_BY_BACKEND.clear()
    request.addfinalizer(_clear_install_jobs)

    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    before = client.get(
        "/api/dependencies",
        headers=headers,
        base_url="http://127.0.0.1:15131",
    ).get_json()["deps"][0]
    assert before["required"] is False
    assert before["installed"] is False

    started = client.post(
        "/api/dependencies/memory-runtime/install",
        headers=headers,
        base_url="http://127.0.0.1:15131",
    ).get_json()
    deadline = time.monotonic() + 2
    finished = started
    while finished.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
        finished = client.get(
            f"/api/dependencies/memory-runtime/install/{started['job_id']}",
            headers=headers,
            base_url="http://127.0.0.1:15131",
        ).get_json()

    assert finished["status"] == "succeeded"
    assert finished["ok"] is True
    assert len(admitted) == 1
    assert admitted[0].parent.parent.name.startswith("install-")
    after = client.get(
        "/api/dependencies",
        headers=headers,
        base_url="http://127.0.0.1:15131",
    ).get_json()["deps"][0]
    assert after["required"] is False
    assert after["status"] == "ready"
    assert after["installed"] is True
    assert after["version"] == "1.2.3"


def _clear_install_jobs() -> None:
    with api._AGENT_INSTALL_JOB_LOCK:
        api._AGENT_INSTALL_JOBS.clear()
        api._AGENT_INSTALL_LATEST_BY_BACKEND.clear()
