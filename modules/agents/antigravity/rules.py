"""Avibe-owned per-session AGENTS.md rendering for Antigravity ``additionalDirectories``."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from config import paths

logger = logging.getLogger(__name__)

#: Antigravity enforces a 24,000-byte limit per rule file and a 20,000-token total budget.
MAX_RULE_FILE_BYTES = 24_000
MAX_RULE_TOTAL_TOKENS = 20_000
#: Conservative UTF-8 byte cap corresponding to the 20,000-token total budget (~4 bytes/token).
MAX_RULE_TOTAL_BYTES = MAX_RULE_TOTAL_TOKENS * 4

_RULE_FILENAME = "AGENTS.md"
_TRUNCATION_MARKER = "\n\n[Avibe rules truncated to stay within Antigravity's 20,000-token rule budget.]\n"


@dataclass(frozen=True)
class RenderedRules:
    """Rendered per-session rule directories suitable for ACP ``additionalDirectories``."""

    directories: tuple[str, ...]
    digest: str
    written: bool


def _session_dir_slug(runtime_key: str) -> str:
    return hashlib.sha256(runtime_key.encode("utf-8")).hexdigest()[:24]


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker_bytes = _TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    budget = max_bytes - len(marker_bytes)
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    # Prefer cutting at a newline boundary so we don't split a markdown directive mid-line.
    last_nl = prefix.rfind("\n")
    if last_nl > 0:
        prefix = prefix[:last_nl]
    return prefix.rstrip() + _TRUNCATION_MARKER


def split_rule_content(
    content: str,
    *,
    max_file_bytes: int = MAX_RULE_FILE_BYTES,
    max_total_bytes: int = MAX_RULE_TOTAL_BYTES,
) -> tuple[str, ...]:
    """Split ``content`` into UTF-8 chunks each ``<= max_file_bytes`` within ``max_total_bytes``."""
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")
    if max_total_bytes <= 0:
        raise ValueError("max_total_bytes must be positive")

    normalized = content.strip()
    if not normalized:
        return ()

    bounded = _truncate_utf8(normalized, max_total_bytes)
    if len(bounded.encode("utf-8")) <= max_file_bytes:
        return (bounded,)

    chunks: list[str] = []
    remaining = bounded
    while remaining:
        raw = remaining.encode("utf-8")
        if len(raw) <= max_file_bytes:
            chunks.append(remaining)
            break

        # Decode the largest valid UTF-8 prefix that fits max_file_bytes.
        candidate = raw[:max_file_bytes].decode("utf-8", errors="ignore")
        split_at = candidate.rfind("\n\n")
        if split_at > 0:
            cut = split_at
            advance = split_at + 2
        else:
            split_at = candidate.rfind("\n")
            if split_at > 0:
                cut = split_at
                advance = split_at + 1
            else:
                cut = len(candidate)
                advance = len(candidate)

        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[advance:].lstrip("\n")

    return tuple(chunks)


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".agents-md-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def render_session_rules(
    runtime_key: str,
    content: str,
    *,
    base_dir: Path | None = None,
    max_file_bytes: int = MAX_RULE_FILE_BYTES,
    max_total_bytes: int = MAX_RULE_TOTAL_BYTES,
) -> RenderedRules:
    """Render Avibe system instructions into per-session ``AGENTS.md`` directories.

    Each returned directory contains one ``AGENTS.md`` file of at most
    ``max_file_bytes`` UTF-8 bytes so Antigravity loads all parts via
    ``additionalDirectories`` without touching user project or global rules.
    Disk writes are skipped completely when the on-disk files already match.
    """
    chunks = split_rule_content(
        content,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    digest_hash = hashlib.sha256()
    for chunk in chunks:
        digest_hash.update(chunk.encode("utf-8"))
        digest_hash.update(b"\x00")
    digest = digest_hash.hexdigest()

    root = (base_dir if base_dir is not None else paths.get_state_dir() / "antigravity-rules") / _session_dir_slug(
        runtime_key
    )
    if not chunks:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        return RenderedRules(directories=(), digest=digest, written=False)

    expected_dirs: list[Path] = []
    expected_part_names: set[str] = set()
    for index in range(len(chunks)):
        part_name = f"part-{index:02d}"
        expected_part_names.add(part_name)
        expected_dirs.append(root / part_name)

    needs_write = False
    if not root.is_dir():
        needs_write = True
    else:
        existing_entries = {entry.name for entry in root.iterdir()}
        if existing_entries != expected_part_names:
            needs_write = True
        else:
            for part_dir, chunk in zip(expected_dirs, chunks, strict=True):
                rule_file = part_dir / _RULE_FILENAME
                if not rule_file.is_file():
                    needs_write = True
                    break
                try:
                    if rule_file.read_text(encoding="utf-8") != chunk:
                        needs_write = True
                        break
                except OSError:
                    needs_write = True
                    break

    if not needs_write:
        return RenderedRules(
            directories=tuple(str(d) for d in expected_dirs),
            digest=digest,
            written=False,
        )

    root.mkdir(parents=True, exist_ok=True)
    for entry in list(root.iterdir()):
        if entry.name not in expected_part_names:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)

    for part_dir, chunk in zip(expected_dirs, chunks, strict=True):
        rule_file = part_dir / _RULE_FILENAME
        _atomic_write_text(rule_file, chunk)

    return RenderedRules(
        directories=tuple(str(d) for d in expected_dirs),
        digest=digest,
        written=True,
    )
