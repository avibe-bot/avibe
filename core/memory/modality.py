"""Attachment extensions the pinned EverOS runtime can actually parse.

The EverOS parser dispatches on a closed extension table and answers any
extension outside it with HTTP 415 (``UnsupportedModalityError``). Forwarding
an unparseable upload therefore produces a deterministic rejection that no
amount of retrying can clear, so the capture boundary filters uploads here
instead of discovering the limit at the provider.

This mirrors ``everalgo.types.modality`` in the packaged EverOS runtime
runtime and must stay in sync with it. Two modality groups from that table are
deliberately excluded because the text-only build lacks their integrations:

- ``DOCUMENT`` (docx / xlsx / pptx / ODF / iWork / rtf) needs LibreOffice
- ``svg`` needs the cairosvg integration

Kept dependency-light on purpose: ``core.memory.sidecar`` imports this from the
runtime child process, which runs with a minimal environment.
"""

from __future__ import annotations

from pathlib import Path

from core.memory.types import MemoryContentKind


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
    }
)

# These pinned upstream formats need unavailable local integrations.  They are
# deliberately a static Avibe policy, not a provider import in request handling.
PINNED_UPSTREAM_EXCLUDED_EXTENSIONS: frozenset[str] = frozenset(
    {
        "svg",
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

_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"})
_AUDIO_EXTENSIONS = frozenset({"mp3", "wav", "m4a", "amr", "aiff", "aac", "ogg", "flac"})
_TEXT_EXTENSIONS = frozenset({"txt", "md", "vtt", "csv", "tsv"})


def classify_pinned_attachment(
    name: str,
    mimetype: str,
    path: Path,
) -> tuple[MemoryContentKind, str] | None:
    """Classify one acquired file only when extension, MIME, and bytes agree."""

    extension = path.suffix.lstrip(".").lower()
    if extension not in SUPPORTED_ATTACHMENT_EXTENSIONS:
        return None
    if Path(name).suffix.lstrip(".").lower() != extension:
        return None
    normalized_mime = mimetype.lower().split(";", 1)[0].strip()
    try:
        with path.open("rb") as file_obj:
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
    if b"\x00" in sample:
        return None
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
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


def _extension_aliases_match(expected: str, detected: str) -> bool:
    aliases = ({"jpg", "jpeg"}, {"tif", "tiff"}, {"m4a", "mp4"})
    return expected == detected or any({expected, detected} <= group for group in aliases)


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
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF0 == 0xF0:
        return "aac" if data[1] & 0x06 == 0 else "mp3"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "m4a"
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
