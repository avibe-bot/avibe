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
    assert "isinstance(SUPPORTED_EXTENSIONS, frozenset)" in script
    assert repr(SUPPORTED_ATTACHMENT_EXTENSIONS) in script
    assert repr(PINNED_UPSTREAM_EXCLUDED_EXTENSIONS) in script
