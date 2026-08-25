from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.memory_loader import load_memory_runtime
from vibe.memory_contract import (
    MemoryPluginIncompatibleError,
    MemoryPluginUnavailableError,
)


def _config(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled)


def test_disabled_loader_never_imports_optional_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    importer = Mock(side_effect=AssertionError("disabled loader imported Memory"))
    monkeypatch.setattr(core.memory_loader.importlib, "import_module", importer)

    assert load_memory_runtime(_config(False)) is None
    importer.assert_not_called()


def test_loader_maps_missing_implementation_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(side_effect=ModuleNotFoundError("optional package missing")),
    )

    with pytest.raises(MemoryPluginUnavailableError):
        load_memory_runtime(_config())


def test_loader_rejects_protocol_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=SimpleNamespace(MEMORY_RUNTIME_PROTOCOL_VERSION=99)),
    )

    with pytest.raises(MemoryPluginIncompatibleError):
        load_memory_runtime(_config())


def test_loader_maps_constructor_failure_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    factory = Mock(side_effect=RuntimeError("constructor failed"))
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(
            return_value=SimpleNamespace(
                MEMORY_RUNTIME_PROTOCOL_VERSION=1,
                create_memory_runtime=factory,
            )
        ),
    )

    with pytest.raises(MemoryPluginUnavailableError):
        load_memory_runtime(_config())


def test_loader_constructs_fixed_protocol_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    async def close() -> None:
        return None

    runtime = SimpleNamespace(module=object(), available=True, close=close)
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(
            return_value=SimpleNamespace(
                MEMORY_RUNTIME_PROTOCOL_VERSION=1,
                create_memory_runtime=factory,
            )
        ),
    )

    assert load_memory_runtime(_config(), marker="tested") is runtime
    factory.assert_called_once_with(_config(), marker="tested")


def test_disabled_loader_allows_explicit_maintenance_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.memory_loader

    async def close() -> None:
        return None

    runtime = SimpleNamespace(module=object(), available=True, close=close)
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(
            return_value=SimpleNamespace(
                MEMORY_RUNTIME_PROTOCOL_VERSION=1,
                create_memory_runtime=factory,
            )
        ),
    )

    config = _config(False)
    assert load_memory_runtime(config, allow_disabled=True) is runtime
    factory.assert_called_once_with(config)


def test_loader_rejects_incomplete_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.memory_loader

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(
            return_value=SimpleNamespace(
                MEMORY_RUNTIME_PROTOCOL_VERSION=1,
                create_memory_runtime=Mock(return_value=SimpleNamespace()),
            )
        ),
    )

    with pytest.raises(MemoryPluginIncompatibleError):
        load_memory_runtime(_config())


def test_loader_maps_runtime_contract_probe_failure_to_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.memory_loader

    class _BrokenRuntime:
        @property
        def module(self):
            raise RuntimeError("module probe failed")

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(
            return_value=SimpleNamespace(
                MEMORY_RUNTIME_PROTOCOL_VERSION=1,
                create_memory_runtime=Mock(return_value=_BrokenRuntime()),
            )
        ),
    )

    with pytest.raises(MemoryPluginIncompatibleError):
        load_memory_runtime(_config())
