"""Safe conversion of Workbench uploads into Memory capture attachments."""

from __future__ import annotations

from pathlib import Path

from config import paths
from core.memory.types import CaptureAttachment


def workbench_capture_attachments(files: object) -> tuple[CaptureAttachment, ...]:
    if not isinstance(files, list):
        return ()
    converted: list[CaptureAttachment] = []
    for file in files:
        local_path = getattr(file, "local_path", None)
        name = getattr(file, "name", None)
        mimetype = getattr(file, "mimetype", None)
        if not all(isinstance(value, str) and value for value in (local_path, name, mimetype)):
            continue
        try:
            path = Path(local_path).resolve(strict=True)
            path.relative_to((paths.get_attachments_dir() / "avibe").resolve(strict=True))
        except (OSError, ValueError):
            continue
        extension = path.suffix.lstrip(".").lower()
        if not extension.isalnum() or len(extension) > 8:
            continue
        normalized_mime = mimetype.lower().split(";", 1)[0].strip()
        if normalized_mime.startswith("image/"):
            kind = "image"
        elif normalized_mime.startswith("audio/"):
            kind = "audio"
        elif normalized_mime == "application/pdf" or extension == "pdf":
            kind = "pdf"
        elif normalized_mime == "text/html" or extension in {"html", "htm"}:
            kind = "html"
        elif normalized_mime == "message/rfc822" or extension == "eml":
            kind = "email"
        else:
            kind = "doc"
        display_name = Path(name).name
        if len(display_name.encode("utf-8")) > 512:
            display_name = display_name.encode("utf-8")[:512].decode("utf-8", errors="ignore")
        if display_name:
            converted.append(
                CaptureAttachment(
                    kind=kind,
                    name=display_name,
                    uri=path.as_uri(),
                    ext=extension,
                )
            )
    return tuple(converted)
