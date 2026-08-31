"""Shared cold-admission contract for released Memory Runtime artifacts."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from avibe_memory.modality import pinned_modality_contract_script


EVEROS_VERSION = "1.2.3"
EMBEDDED_PYTHON_VERSION = "3.12.12"
COLD_ARTIFACT_ADMISSION_TIMEOUT_SECONDS = 120
COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON = "memory_runtime_preparation_import_timeout"
COLD_ARTIFACT_ADMISSION_FAILURE_REASON = "memory_runtime_preparation_import_failed"

_COLD_ARTIFACT_ADMISSION_SCRIPT = (
    "from importlib.metadata import version\n"
    "import platform\n"
    "import everos\n"
    "import uvicorn\n"
    "from everos.entrypoints.api.app import create_app\n"
    "import everos.entrypoints.cli.main\n"
    "import everos.memory.cascade\n"
    f"assert version('everos') == '{EVEROS_VERSION}'\n"
    f"assert platform.python_version() == '{EMBEDDED_PYTHON_VERSION}'\n"
    "assert everos is not None and uvicorn is not None\n"
    "assert callable(create_app)\n"
    + pinned_modality_contract_script()
    + "print(version('everos'))\n"
    "print(platform.python_version())\n"
)


@dataclass(frozen=True)
class ColdArtifactAdmissionResult:
    """One bounded observation of the released artifact's cold import surface."""

    ok: bool
    reason: str | None
    duration_ms: int


def run_cold_artifact_admission(
    binary: Path,
    *,
    timeout_seconds: int = COLD_ARTIFACT_ADMISSION_TIMEOUT_SECONDS,
) -> ColdArtifactAdmissionResult:
    """Run the exact cold import gate shared by release verification and install."""

    started_at = time.monotonic()
    try:
        result = subprocess.run(
            [str(binary), "-I", "-c", _COLD_ARTIFACT_ADMISSION_SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=binary.parent.parent,
            **_isolated_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return ColdArtifactAdmissionResult(
            ok=False,
            reason=COLD_ARTIFACT_ADMISSION_TIMEOUT_REASON,
            duration_ms=_duration_ms(started_at),
        )
    except (OSError, subprocess.SubprocessError):
        return ColdArtifactAdmissionResult(
            ok=False,
            reason=COLD_ARTIFACT_ADMISSION_FAILURE_REASON,
            duration_ms=_duration_ms(started_at),
        )

    expected_output = [EVEROS_VERSION, EMBEDDED_PYTHON_VERSION]
    return ColdArtifactAdmissionResult(
        ok=result.returncode == 0 and result.stdout.splitlines() == expected_output,
        reason=(
            None
            if result.returncode == 0 and result.stdout.splitlines() == expected_output
            else COLD_ARTIFACT_ADMISSION_FAILURE_REASON
        ),
        duration_ms=_duration_ms(started_at),
    )


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _isolated_subprocess_kwargs() -> dict[str, object]:
    """Keep the release-builder import path free of installed dependencies."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
