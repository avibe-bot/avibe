import sys
from pathlib import Path
from types import ModuleType

import pytest

from avibe_memory.modality import (
    PINNED_UPSTREAM_EXCLUDED_EXTENSIONS,
    SUPPORTED_ATTACHMENT_EXTENSIONS,
    classify_pinned_attachment,
    pinned_modality_contract_matches,
    pinned_modality_contract_script,
)


EXTERNALLY_CONVERTED_EXTENSIONS = frozenset(
    {
        "docx",
        "pptx",
        "xlsx",
        "doc",
        "ppt",
        "xls",
        "pages",
        "key",
        "numbers",
        "odt",
        "ods",
        "odp",
        "rtf",
    }
)


def _write_private(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def test_classifier_accepts_mpeg_2_5_mp3_frame_sync(tmp_path: Path) -> None:
    path = _write_private(tmp_path / "voice.mp3", b"\xff\xe3\x18\x00payload")

    assert classify_pinned_attachment("voice.mp3", "audio/mpeg", path) == (
        "audio",
        "mp3",
    )


def test_classifier_requires_an_audio_brand_for_m4a(tmp_path: Path) -> None:
    video = _write_private(
        tmp_path / "clip.m4a",
        b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x0cfreeM4A ",
    )
    audio = _write_private(
        tmp_path / "voice.m4a",
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00M4A mp42",
    )

    assert classify_pinned_attachment("clip.m4a", "audio/mp4", video) is None
    assert classify_pinned_attachment("voice.m4a", "audio/mp4", audio) == (
        "audio",
        "m4a",
    )


def test_classifier_accepts_utf8_sample_with_incomplete_trailing_codepoint(
    tmp_path: Path,
) -> None:
    path = _write_private(tmp_path / "notes.txt", b"a" * (64 * 1024 - 1) + "你".encode())

    assert classify_pinned_attachment("notes.txt", "text/plain", path) == (
        "doc",
        "txt",
    )


def test_classifier_still_rejects_invalid_utf8_inside_sample(tmp_path: Path) -> None:
    path = _write_private(tmp_path / "notes.txt", b"valid\xffinvalid")

    assert classify_pinned_attachment("notes.txt", "text/plain", path) is None


@pytest.mark.parametrize("tail", [b"\x00", b"\xff"], ids=["nul", "invalid-utf8"])
def test_classifier_validates_complete_text_file(tmp_path: Path, tail: bytes) -> None:
    path = _write_private(tmp_path / "notes.txt", b"a" * 4096 + tail)

    assert classify_pinned_attachment("notes.txt", "text/plain", path) is None


def test_classifier_normalizes_missing_mime_to_octet_stream(tmp_path: Path) -> None:
    path = _write_private(tmp_path / "notes.txt", b"valid text")

    assert classify_pinned_attachment("notes.txt", None, path) == ("doc", "txt")


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


def test_classifier_excludes_every_externally_converted_format(tmp_path: Path) -> None:
    assert EXTERNALLY_CONVERTED_EXTENSIONS <= PINNED_UPSTREAM_EXCLUDED_EXTENSIONS
    assert EXTERNALLY_CONVERTED_EXTENSIONS.isdisjoint(SUPPORTED_ATTACHMENT_EXTENSIONS)

    for extension in EXTERNALLY_CONVERTED_EXTENSIONS:
        path = _write_private(tmp_path / f"attachment.{extension}", b"payload")
        assert (
            classify_pinned_attachment(path.name, "application/octet-stream", path)
            is None
        )


def test_classifier_keeps_svg_out_of_the_live_allowlist(tmp_path: Path) -> None:
    path = _write_private(
        tmp_path / "logo.svg",
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    )

    assert "svg" in PINNED_UPSTREAM_EXCLUDED_EXTENSIONS
    assert classify_pinned_attachment("logo.svg", "image/svg+xml", path) is None
