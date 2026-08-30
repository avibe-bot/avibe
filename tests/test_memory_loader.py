from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.memory_loader import load_memory_runtime, probe_memory_runtime_entrypoint
from vibe.memory_contract import (
    MemoryPluginIncompatibleError,
    MemoryPluginUnavailableError,
    MemoryRuntimeBusyError,
)


def _config(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled)


def _implementation(factory: object) -> SimpleNamespace:
    return SimpleNamespace(
        MEMORY_RUNTIME_PROTOCOL_VERSION=1,
        MEMORY_RUNTIME_LIFECYCLE_CONTRACT=1,
        create_memory_runtime=factory,
    )


def test_disabled_loader_never_imports_optional_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    importer = Mock(side_effect=AssertionError("disabled loader imported Memory"))
    monkeypatch.setattr(core.memory_loader.importlib, "import_module", importer)

    assert load_memory_runtime(_config(False)) is None
    importer.assert_not_called()


@pytest.mark.parametrize("probe_only", (False, True))
def test_loader_and_probe_map_missing_implementation_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    probe_only: bool,
) -> None:
    import core.memory_loader

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(side_effect=ModuleNotFoundError("optional package missing")),
    )

    with pytest.raises(MemoryPluginUnavailableError):
        if probe_only:
            probe_memory_runtime_entrypoint()
        else:
            load_memory_runtime(_config())


@pytest.mark.parametrize(
    ("attribute", "value"),
    (
        ("MEMORY_RUNTIME_PROTOCOL_VERSION", 99),
        ("MEMORY_RUNTIME_LIFECYCLE_CONTRACT", None),
        ("MEMORY_RUNTIME_LIFECYCLE_CONTRACT", 99),
    ),
)
def test_entrypoint_probe_rejects_legacy_or_incompatible_contract(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: int | None,
) -> None:
    import core.memory_loader

    factory = Mock()
    implementation = _implementation(factory)
    if value is None:
        delattr(implementation, attribute)
    else:
        setattr(implementation, attribute, value)
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=implementation),
    )

    with pytest.raises(MemoryPluginIncompatibleError):
        probe_memory_runtime_entrypoint()
    factory.assert_not_called()


@pytest.mark.parametrize("factory", (None, object()))
def test_entrypoint_probe_rejects_missing_or_noncallable_factory(
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
) -> None:
    import core.memory_loader

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_implementation(factory)),
    )

    with pytest.raises(MemoryPluginUnavailableError):
        probe_memory_runtime_entrypoint()


def test_entrypoint_probe_validates_contract_without_constructing_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.memory_loader

    factory = Mock(side_effect=AssertionError("the contract probe constructed Memory"))
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_implementation(factory)),
    )

    assert probe_memory_runtime_entrypoint() is None
    factory.assert_not_called()


@pytest.mark.parametrize(
    "attribute",
    (
        "MEMORY_RUNTIME_PROTOCOL_VERSION",
        "MEMORY_RUNTIME_LIFECYCLE_CONTRACT",
        "create_memory_runtime",
    ),
)
def test_loader_translates_implementation_attribute_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
) -> None:
    import core.memory_loader

    class _BrokenImplementation:
        def __getattr__(self, name: str) -> object:
            if name == attribute:
                raise RuntimeError("broken implementation attribute")
            return None

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_BrokenImplementation()),
    )

    with pytest.raises(MemoryPluginUnavailableError):
        load_memory_runtime(_config())


def test_loader_maps_constructor_failure_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    factory = Mock(side_effect=RuntimeError("constructor failed"))
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_implementation(factory)),
    )

    with pytest.raises(MemoryPluginUnavailableError):
        load_memory_runtime(_config())


def test_loader_preserves_runtime_root_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.memory_loader

    busy = MemoryRuntimeBusyError("provider root busy")
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_implementation(Mock(side_effect=busy))),
    )

    with pytest.raises(MemoryRuntimeBusyError) as raised:
        load_memory_runtime(_config())

    assert raised.value is busy


@pytest.mark.parametrize(
    ("config", "loader_kwargs", "factory_kwargs"),
    (
        (_config(), {"marker": "tested"}, {"marker": "tested"}),
        (_config(False), {"allow_disabled": True}, {}),
    ),
)
def test_loader_constructs_declared_runtime(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    loader_kwargs: dict[str, object],
    factory_kwargs: dict[str, object],
) -> None:
    import core.memory_loader

    async def close() -> None:
        return None

    runtime = SimpleNamespace(module=object(), available=True, close=close)
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_implementation(factory)),
    )

    assert load_memory_runtime(config, **loader_kwargs) is runtime
    factory.assert_called_once_with(config, **factory_kwargs)


def test_loader_rejects_incomplete_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.memory_loader

    monkeypatch.setattr(
        core.memory_loader.importlib,
        "import_module",
        Mock(return_value=_implementation(Mock(return_value=SimpleNamespace()))),
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
        Mock(return_value=_implementation(Mock(return_value=_BrokenRuntime()))),
    )

    with pytest.raises(MemoryPluginIncompatibleError):
        load_memory_runtime(_config())
