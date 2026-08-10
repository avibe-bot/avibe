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
