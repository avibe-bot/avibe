"""Attachment extensions the pinned EverOS runtime can actually parse.

The EverOS parser dispatches on a closed extension table and answers any
extension outside it with HTTP 415 (``UnsupportedModalityError``). Forwarding
an unparseable upload therefore produces a deterministic rejection that no
amount of retrying can clear, so the capture boundary filters uploads here
instead of discovering the limit at the provider.

This mirrors ``everalgo.types.modality`` in the packaged EverOS runtime
and must stay in sync with it. Two upstream groups stay out of the live
allowlist for host-capability reasons:

- ``svg`` needs the cairosvg integration, which this runtime does not ship
- video is still unimplemented in the pinned EverOS parser

Office / iWork / ODF / RTF are admitted only when the host can resolve
LibreOffice's ``soffice`` binary. EverOS converts those files to PDF before
the multimodal LLM sees them; sending one without ``soffice`` aborts the
whole ``/add`` batch with ``CAPABILITY_UNAVAILABLE``.

Kept dependency-light on purpose: ``core.memory.sidecar`` imports this from the
runtime child process, which runs with a minimal environment.
"""

from __future__ import annotations

import codecs
import os
import shutil
import zipfile
from pathlib import Path

from core.memory.types import MemoryContentKind


# EverOS's macOS fallback; keep this identical so Avibe and the parser agree.
_MACOS_SOFFICE = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")

OFFICE_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
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

SUPPORTED_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        # DIRECT: plain text, parsed without any model call
        "txt",
        "md",
        "vtt",
        "csv",
        "tsv",
        # PDF
        "pdf",
        # IMAGE (bitmap only; svg needs cairosvg)
        "png",
        "jpg",
        "jpeg",
        "webp",
        "tiff",
        "tif",
        "bmp",
        # AUDIO
        "mp3",
        "wav",
        "m4a",
        "amr",
        "aiff",
        "aac",
        "ogg",
        "flac",
        # HTML
        "html",
        "htm",
        # EMAIL
        "eml",
        *OFFICE_ATTACHMENT_EXTENSIONS,
    }
)

# These pinned upstream formats need unavailable local integrations.  They are
# deliberately a static Avibe policy, not a provider import in request handling.
PINNED_UPSTREAM_EXCLUDED_EXTENSIONS: frozenset[str] = frozenset({"svg"})

_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"})
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "m4a", "amr", "aiff", "aac", "ogg", "flac"})
_TEXT_EXTENSIONS = frozenset({"txt", "md", "vtt", "csv", "tsv"})
_OFFICE_ZIP_MARKERS: dict[str, frozenset[str]] = {
    "docx": frozenset({"[Content_Types].xml", "word/document.xml"}),
    "pptx": frozenset({"[Content_Types].xml", "ppt/presentation.xml"}),
    "xlsx": frozenset({"[Content_Types].xml", "xl/workbook.xml"}),
    "odt": frozenset({"mimetype", "content.xml"}),
    "ods": frozenset({"mimetype", "content.xml"}),
    "odp": frozenset({"mimetype", "content.xml"}),
    # Current iWork packages share the IWA document index; the extension tells
    # LibreOffice which filter to apply.
    "pages": frozenset({"Index/Document.iwa"}),
    "key": frozenset({"Index/Document.iwa"}),
    "numbers": frozenset({"Index/Document.iwa"}),
}
_OFFICE_OLE_EXTENSIONS = frozenset({"doc", "ppt", "xls"})
_OFFICE_RTF_EXTENSIONS = frozenset({"rtf"})
_OFFICE_MIMES_BY_EXTENSION: dict[str, frozenset[str]] = {
    "docx": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    ),
    "pptx": frozenset(
        {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    ),
    "xlsx": frozenset(
        {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
    "doc": frozenset({"application/msword"}),
    "ppt": frozenset({"application/vnd.ms-powerpoint"}),
    "xls": frozenset({"application/vnd.ms-excel"}),
    "pages": frozenset(
        {
            "application/vnd.apple.pages",
            "application/x-iwork-pages-sffpages",
        }
    ),
    "key": frozenset(
        {
            "application/vnd.apple.keynote",
            "application/x-iwork-keynote-sffkey",
        }
    ),
    "numbers": frozenset(
        {
            "application/vnd.apple.numbers",
            "application/x-iwork-numbers-sffnumbers",
        }
    ),
    "odt": frozenset({"application/vnd.oasis.opendocument.text"}),
    "ods": frozenset({"application/vnd.oasis.opendocument.spreadsheet"}),
    "odp": frozenset({"application/vnd.oasis.opendocument.presentation"}),
    "rtf": frozenset({"application/rtf", "text/rtf"}),
}
_ODF_MIME_BY_EXTENSION = {
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_RTF_MAGIC = b"{\\rtf"


def office_conversion_available() -> bool:
    """Return whether the Memory sidecar can resolve LibreOffice.

    The sidecar PATH is ``<runtime>/bin:/usr/bin:/bin``. Probe only those
    locations plus EverOS's macOS App fallback so Avibe never admits an
    Office file the parser child cannot convert.
    """

    return (
        shutil.which("soffice", path="/usr/bin:/bin") is not None
        or (
            _MACOS_SOFFICE.is_file()
            and os.access(_MACOS_SOFFICE, os.X_OK)
        )
    )


def classify_pinned_attachment(
    name: str,
    mimetype: object,
    path: Path,
    *,
    file_fd: int | None = None,
) -> tuple[MemoryContentKind, str] | None:
    """Classify one acquired file only when extension, MIME, and bytes agree."""

    extension = path.suffix.lstrip(".").lower()
    if extension not in SUPPORTED_ATTACHMENT_EXTENSIONS:
        return None
    if Path(name).suffix.lstrip(".").lower() != extension:
        return None
    normalized_mime = (
        mimetype.lower().split(";", 1)[0].strip()
        if isinstance(mimetype, str) and mimetype.strip()
        else "application/octet-stream"
    )
    try:
        with _open_attachment_file(path, file_fd) as file_obj:
            file_obj.seek(0)
            sample = file_obj.read(4096)
    except OSError:
        return None

    if extension in _IMAGE_EXTENSIONS:
        detected = _image_extension(sample)
        if detected is None or not _extension_aliases_match(extension, detected):
            return None
        if normalized_mime not in {"application/octet-stream", "image/unknown"} and not normalized_mime.startswith("image/"):
            return None
        return "image", extension
    if extension in _AUDIO_EXTENSIONS:
        detected = _audio_extension(sample)
        if detected is None or not _extension_aliases_match(extension, detected):
            return None
        if normalized_mime != "application/octet-stream" and not normalized_mime.startswith("audio/"):
            return None
        return "audio", extension
    if extension == "pdf":
        if not sample.startswith(b"%PDF-") or normalized_mime not in {
            "application/pdf",
            "application/octet-stream",
        }:
            return None
        return "pdf", extension
    if extension in OFFICE_ATTACHMENT_EXTENSIONS:
        if not office_conversion_available() or not _office_container_matches(
            extension,
            path,
            file_fd,
            sample,
        ):
            return None
        if (
            normalized_mime != "application/octet-stream"
            and normalized_mime not in _OFFICE_MIMES_BY_EXTENSION[extension]
        ):
            return None
        return "doc", extension
    if not _valid_utf8_text_file(path, file_fd):
        return None
    if normalized_mime != "application/octet-stream" and not (
        normalized_mime.startswith("text/")
        or normalized_mime in {"message/rfc822", "application/csv"}
    ):
        return None
    if extension in {"html", "htm"}:
        return "html", extension
    if extension == "eml":
        return "email", extension
    if extension in _TEXT_EXTENSIONS:
        return "doc", extension
    return None


def _open_attachment_file(path: Path, file_fd: int | None):
    if file_fd is None:
        return path.open("rb")
    return os.fdopen(os.dup(file_fd), "rb")


def _valid_utf8_text_file(path: Path, file_fd: int | None) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with _open_attachment_file(path, file_fd) as file_obj:
            file_obj.seek(0)
            while chunk := file_obj.read(64 * 1024):
                if b"\x00" in chunk:
                    return False
                decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _extension_aliases_match(expected: str, detected: str) -> bool:
    aliases = ({"jpg", "jpeg"}, {"tif", "tiff"}, {"m4a", "mp4"})
    return expected == detected or any({expected, detected} <= group for group in aliases)


def _office_container_matches(
    extension: str,
    path: Path,
    file_fd: int | None,
    sample: bytes,
) -> bool:
    if extension in _OFFICE_ZIP_MARKERS:
        return _office_zip_matches(extension, path, file_fd)
    if extension in _OFFICE_OLE_EXTENSIONS:
        return sample.startswith(_OLE_MAGIC)
    if extension in _OFFICE_RTF_EXTENSIONS:
        return sample.startswith(_RTF_MAGIC)
    return False


def _office_zip_matches(extension: str, path: Path, file_fd: int | None) -> bool:
    try:
        with _open_attachment_file(path, file_fd) as file_obj:
            file_obj.seek(0)
            with zipfile.ZipFile(file_obj) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    return False
                if not _OFFICE_ZIP_MARKERS[extension].issubset(names):
                    return False
                expected_mime = _ODF_MIME_BY_EXTENSION.get(extension)
                if expected_mime is None:
                    return True
                mime_info = archive.getinfo("mimetype")
                if mime_info.file_size > 128:
                    return False
                return archive.read(mime_info) == expected_mime.encode("ascii")
    except (
        KeyError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return False


def _image_extension(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if data.startswith(b"BM"):
        return "bmp"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def _audio_extension(data: bytes) -> str | None:
    if data.startswith(b"ID3"):
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return "aac"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x06) != 0:
        return "mp3"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        box_size = int.from_bytes(data[:4], "big")
        if box_size < 16 or box_size > len(data) or box_size % 4:
            return None
        brands = (
            data[8:12],
            *(data[offset : offset + 4] for offset in range(16, box_size, 4)),
        )
        if any(brand in {b"M4A ", b"M4B "} for brand in brands):
            return "m4a"
        return None
    if data.startswith(b"#!AMR"):
        return "amr"
    if len(data) >= 12 and data.startswith(b"FORM") and data[8:12] in {b"AIFF", b"AIFC"}:
        return "aiff"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    return None


def pinned_modality_contract_matches(upstream_extensions: object) -> bool:
    """Verify the pinned artifact's extension set without changing runtime policy."""

    if not isinstance(upstream_extensions, (set, frozenset)) or not all(
        isinstance(extension, str) for extension in upstream_extensions
    ):
        return False
    return (
        frozenset(upstream_extensions) - PINNED_UPSTREAM_EXCLUDED_EXTENSIONS
        == SUPPORTED_ATTACHMENT_EXTENSIONS
    )


def pinned_modality_contract_script() -> str:
    """Build the artifact-only check from Avibe's static modality policy."""

    expected = repr(frozenset(SUPPORTED_ATTACHMENT_EXTENSIONS))
    exclusions = repr(frozenset(PINNED_UPSTREAM_EXCLUDED_EXTENSIONS))
    return (
        "from everalgo.types.modality import SUPPORTED_EXTENSIONS\n"
        "assert isinstance(SUPPORTED_EXTENSIONS, (set, frozenset))\n"
        "assert all(isinstance(extension, str) for extension in SUPPORTED_EXTENSIONS)\n"
        f"assert frozenset(SUPPORTED_EXTENSIONS) - {exclusions} == {expected}\n"
    )
