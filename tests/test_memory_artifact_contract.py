from __future__ import annotations

import subprocess
from pathlib import Path

from core.memory import artifact_contract


def test_cold_artifact_admission_is_bounded_and_warms_the_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binary = tmp_path / "runtime" / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.touch()
    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                f"{artifact_contract.EVEROS_VERSION}\n"
                f"{artifact_contract.EMBEDDED_PYTHON_VERSION}\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(artifact_contract.subprocess, "run", run)

    result = artifact_contract.run_cold_artifact_admission(binary)

    assert result.ok is True
    assert result.reason is None
    command = captured["command"]
    assert command[:3] == [str(binary), "-I", "-c"]
    assert "-B" not in command
    assert "everos.entrypoints.cli.main" in command[3]
    assert "everos.memory.cascade" in command[3]
    assert "SUPPORTED_EXTENSIONS" in command[3]
    assert captured["cwd"] == binary.parent.parent
    assert captured["timeout"] == artifact_contract.COLD_ARTIFACT_ADMISSION_TIMEOUT_SECONDS


def test_cold_artifact_admission_classifies_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binary = tmp_path / "runtime" / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.touch()

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(artifact_contract.subprocess, "run", timeout)

    result = artifact_contract.run_cold_artifact_admission(binary)

    assert result.ok is False
    assert result.reason == artifact_contract.COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON
    assert result.duration_ms >= 0
