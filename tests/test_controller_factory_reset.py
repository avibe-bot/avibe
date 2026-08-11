from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.controller import Controller


class _Runtime:
    def __init__(self, effective_home: Path, *, artifact_admitted: bool = True) -> None:
        self.effective_home = effective_home
        self._artifact_admitted = artifact_admitted
        self.retired = False

    def artifact_admitted(self) -> bool:
        return self._artifact_admitted

    def adopt_recovery_intent(self, _candidate: object) -> None:
        return None

    def retire(self) -> None:
        self.retired = True

    async def close(self) -> None:
        raise RuntimeError("retirement failed")


def _controller(runtime: _Runtime) -> Controller:
    controller = Controller.__new__(Controller)
    controller.memory_runtime = runtime
    return controller


def _create_roots(home: Path) -> None:
    (home / "memory").mkdir()
    (home / "state" / "memory").mkdir(parents=True)


@pytest.mark.asyncio
async def test_factory_reset_artifact_invalid_returns_closed_unchanged_result(
    tmp_path: Path,
) -> None:
    _create_roots(tmp_path)
    result = await Controller._factory_reset_memory_once(_controller(_Runtime(tmp_path, artifact_admitted=False)))

    assert result == {
        "ok": False,
        "error": "memory_factory_reset_failed",
        "result": "failed",
        "reason": "artifact_repair_required",
        "data_deleted": False,
        "data_remaining": True,
        "roots": [
            {"path": "memory", "existed": True, "deleted": False},
            {"path": "state/memory", "existed": True, "deleted": False},
        ],
    }


@pytest.mark.asyncio
async def test_factory_reset_retirement_failure_returns_closed_unchanged_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_roots(tmp_path)
    controller = _controller(_Runtime(tmp_path))
    monkeypatch.setattr(
        controller,
        "_mark_factory_reset_intent",
        lambda: SimpleNamespace(recovery_intent="factory_reset"),
    )

    result = await controller._factory_reset_memory_once()

    assert result["result"] == "failed"
    assert result["reason"] == "runtime_retirement_failed"
    assert result["data_deleted"] is False
    assert result["data_remaining"] is True
    assert result["roots"] == [
        {"path": "memory", "existed": True, "deleted": False},
        {"path": "state/memory", "existed": True, "deleted": False},
    ]
