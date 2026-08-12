import sys
from types import ModuleType

import pytest

from core.memory.modality import (
    PINNED_UPSTREAM_EXCLUDED_EXTENSIONS,
    SUPPORTED_ATTACHMENT_EXTENSIONS,
    pinned_modality_contract_matches,
    pinned_modality_contract_script,
)


def test_pinned_modality_contract_allows_exact_upstream_set_minus_exclusions() -> None:
    upstream = SUPPORTED_ATTACHMENT_EXTENSIONS | PINNED_UPSTREAM_EXCLUDED_EXTENSIONS

    assert pinned_modality_contract_matches(upstream) is True
    assert pinned_modality_contract_matches(upstream | {"video"}) is False
    assert pinned_modality_contract_matches(["txt"]) is False
    assert pinned_modality_contract_matches(frozenset({"txt", 1})) is False


def test_pinned_modality_admission_script_is_derived_from_static_policy() -> None:
    script = pinned_modality_contract_script()

    assert "from everalgo.types.modality import SUPPORTED_EXTENSIONS" in script
    assert "isinstance(SUPPORTED_EXTENSIONS, (set, frozenset))" in script
    assert "frozenset(SUPPORTED_EXTENSIONS)" in script
    assert repr(SUPPORTED_ATTACHMENT_EXTENSIONS) in script
    assert repr(PINNED_UPSTREAM_EXCLUDED_EXTENSIONS) in script


@pytest.mark.parametrize("container", [set, frozenset], ids=["set", "frozenset"])
def test_pinned_modality_admission_script_accepts_supported_upstream_containers(
    monkeypatch: pytest.MonkeyPatch,
    container,
) -> None:
    everalgo = ModuleType("everalgo")
    everalgo_types = ModuleType("everalgo.types")
    modality = ModuleType("everalgo.types.modality")
    modality.SUPPORTED_EXTENSIONS = container(
        SUPPORTED_ATTACHMENT_EXTENSIONS | PINNED_UPSTREAM_EXCLUDED_EXTENSIONS
    )
    everalgo.types = everalgo_types
    everalgo_types.modality = modality
    monkeypatch.setitem(sys.modules, "everalgo", everalgo)
    monkeypatch.setitem(sys.modules, "everalgo.types", everalgo_types)
    monkeypatch.setitem(sys.modules, "everalgo.types.modality", modality)

    exec(pinned_modality_contract_script(), {})
