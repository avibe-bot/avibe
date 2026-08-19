import io
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from core.memory.modality import (
    OFFICE_ATTACHMENT_EXTENSIONS,
    PINNED_UPSTREAM_EXCLUDED_EXTENSIONS,
    SUPPORTED_ATTACHMENT_EXTENSIONS,
    classify_pinned_attachment,
    office_conversion_available,
    pinned_modality_contract_matches,
    pinned_modality_contract_script,
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


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1office"
_RTF_MAGIC = b"{\\rtf office"


def _office_zip(*entries: str, mimetype: str | None = None) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for entry in entries:
            archive.writestr(entry, mimetype if entry == "mimetype" else b"content")
    return payload.getvalue()


def test_classifier_keeps_svg_out_of_the_live_allowlist(tmp_path: Path) -> None:
    path = _write_private(
        tmp_path / "logo.svg",
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    )

    assert "svg" in PINNED_UPSTREAM_EXCLUDED_EXTENSIONS
    assert classify_pinned_attachment("logo.svg", "image/svg+xml", path) is None


def test_office_probe_uses_sidecar_path_and_macos_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_which(name: str, path: str | None = None) -> str | None:
        seen["name"] = name
        seen["path"] = path
        return None

    monkeypatch.setattr("core.memory.modality.shutil.which", fake_which)
    monkeypatch.setattr(
        "core.memory.modality._MACOS_SOFFICE",
        tmp_path / "missing-soffice",
    )
    assert office_conversion_available() is False
    assert seen == {"name": "soffice", "path": "/usr/bin:/bin"}

    fallback = _write_private(tmp_path / "soffice", b"")
    monkeypatch.setattr("core.memory.modality._MACOS_SOFFICE", fallback)
    assert office_conversion_available() is False

    fallback.chmod(0o700)
    assert office_conversion_available() is True


def test_classifier_skips_office_without_soffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: False)
    path = _write_private(
        tmp_path / "report.docx",
        _office_zip("[Content_Types].xml", "word/document.xml"),
    )

    assert classify_pinned_attachment(
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        path,
    ) is None


def test_classifier_accepts_office_when_soffice_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    path = _write_private(
        tmp_path / "report.xlsx",
        _office_zip("[Content_Types].xml", "xl/workbook.xml"),
    )

    assert classify_pinned_attachment(
        "report.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        path,
    ) == ("doc", "xlsx")


def test_classifier_rejects_office_bytes_that_are_not_convertible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    path = _write_private(tmp_path / "report.docx", b"not an office container")

    assert classify_pinned_attachment(
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        path,
    ) is None


def test_classifier_rejects_an_ordinary_zip_renamed_as_office(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    path = _write_private(tmp_path / "report.docx", _office_zip("notes.txt"))

    assert classify_pinned_attachment(
        "report.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        path,
    ) is None


@pytest.mark.parametrize(
    ("filename", "mimetype"),
    [
        ("report.pages", "application/vnd.apple.pages"),
        ("slides.key", "application/vnd.apple.keynote"),
        ("budget.numbers", "application/vnd.apple.numbers"),
    ],
)
def test_classifier_accepts_registered_iwork_mime_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    mimetype: str,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    path = _write_private(tmp_path / filename, _office_zip("Index/Document.iwa"))

    assert classify_pinned_attachment(filename, mimetype, path) == (
        "doc",
        Path(filename).suffix.lstrip("."),
    )


def test_classifier_requires_the_mime_for_the_same_office_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    path = _write_private(
        tmp_path / "report.docx",
        _office_zip("[Content_Types].xml", "word/document.xml"),
    )

    assert classify_pinned_attachment(
        "report.docx",
        "application/vnd.ms-excel",
        path,
    ) is None


def test_classifier_requires_the_registered_odf_package_mimetype(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    valid = _write_private(
        tmp_path / "notes.odt",
        _office_zip(
            "mimetype",
            "content.xml",
            mimetype="application/vnd.oasis.opendocument.text",
        ),
    )
    wrong = _write_private(
        tmp_path / "sheet.odt",
        _office_zip(
            "mimetype",
            "content.xml",
            mimetype="application/vnd.oasis.opendocument.spreadsheet",
        ),
    )

    assert classify_pinned_attachment(
        "notes.odt",
        "application/vnd.oasis.opendocument.text",
        valid,
    ) == ("doc", "odt")
    assert classify_pinned_attachment(
        "sheet.odt",
        "application/vnd.oasis.opendocument.text",
        wrong,
    ) is None


@pytest.mark.parametrize(
    ("filename", "mimetype", "payload"),
    [
        (
            "legacy.doc",
            "application/msword",
            _OLE_MAGIC,
        ),
        (
            "notes.rtf",
            "application/rtf",
            _RTF_MAGIC,
        ),
    ],
)
def test_classifier_accepts_closed_office_containers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    mimetype: str,
    payload: bytes,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    path = _write_private(tmp_path / filename, payload)

    assert classify_pinned_attachment(filename, mimetype, path) == (
        "doc",
        Path(filename).suffix.lstrip("."),
    )
    assert Path(filename).suffix.lstrip(".") in OFFICE_ATTACHMENT_EXTENSIONS
