"""Avibe-owned Skill discovery, catalog rendering, loading, and built-ins."""

from __future__ import annotations

import errno
import hashlib
import html
import json
import logging
import os
import re
import shutil
import stat
import struct
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import yaml

from config import paths
from core.prompt_registry import RenderedPromptBlock, join_prompt_blocks, render_prompt, render_prompt_block

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


logger = logging.getLogger(__name__)

SKILL_WORKING_DIR_ENV = "AVIBE_SKILL_WORKING_DIR"
SKILL_PROJECT_BASE_ENV = "AVIBE_SKILL_PROJECT_BASE"
BUILTIN_SKILLS_SNAPSHOT_ENV = "AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID"
BUILTIN_SKILLS_ROOT_ENV = "AVIBE_BUILTIN_SKILLS_ROOT"
SKILL_HOME_ENV = "AVIBE_SKILL_HOME"
SKILL_CODEX_HOME_ENV = "AVIBE_SKILL_CODEX_HOME"
SKILL_CLAUDE_HOME_ENV = "AVIBE_SKILL_CLAUDE_HOME"
SKILL_CLAUDE_CLI_PATH_ENV = "AVIBE_SKILL_CLAUDE_CLI_PATH"
SKILL_XDG_CONFIG_HOME_ENV = "AVIBE_SKILL_XDG_CONFIG_HOME"

CATALOG_PAGE_SIZE = 25
CATALOG_PAGE_MAX_BYTES = 16 * 1024
CATALOG_DESCRIPTION_MAX_CHARS = 1024
FRONTMATTER_MAX_BYTES = 64 * 1024
SKILL_BODY_MAX_BYTES = 256 * 1024
DISCOVERY_ROOT_MAX_CHILDREN = 1024
DISCOVERY_CLASS_MAX_CANDIDATES = 1024
DISCOVERY_CLASS_MAX_FRONTMATTER_BYTES = 8 * 1024 * 1024
DISCOVERY_CLASS_MAX_CHILDREN = 4096
PROJECT_ROOT_MAX_DIRECTORIES = 128
BUILTIN_TREE_MAX_ENTRIES = 4096
BUILTIN_TREE_MAX_BYTES = 32 * 1024 * 1024
CLAUDE_PLUGIN_LIST_MAX_BYTES = 1024 * 1024
CLAUDE_PLUGIN_LIST_MAX_ENTRIES = 256
CLAUDE_PLUGIN_LIST_TIMEOUT_SECONDS = 1

_SNAPSHOT_DOMAIN = b"avibe-builtin-snapshot-v1\0"
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOP_LEVEL_FIELD_RE = re.compile(r"^[^ \t#][^:]*:")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?[1-9]?$|^[|>][1-9]?[+-]?$")
_CATALOG_FIELDS = frozenset({"name", "description", "disable-model-invocation"})
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_CHARS = frozenset('<>:"\\|?*')
_GENERATED_BYTECODE_SUFFIXES = (".pyc", ".pyo")
_PROJECT_FAMILIES = (
    (Path(".agents/skills"), 1),
    (Path(".codex/skills"), 2),
    (Path(".claude/skills"), 3),
    (Path(".opencode/skills"), 4),
)
@dataclass(frozen=True)
class ManagedSkill:
    name: str
    description: str
    directory: Path
    priority: tuple[object, ...]
    body: str | None = None
    directory_identity: tuple[int, int] | None = None
    source_directory: Path | None = None
    source_directory_identity: tuple[int, int] | None = None
    frontmatter_bytes: int = 0
    disable_model_invocation: bool = False


@dataclass
class _DiscoveryBudget:
    candidates: int = 0
    frontmatter_bytes: int = 0
    direct_children: int = 0

    @property
    def exhausted(self) -> bool:
        return (
            self.candidates >= DISCOVERY_CLASS_MAX_CANDIDATES
            or self.frontmatter_bytes >= DISCOVERY_CLASS_MAX_FRONTMATTER_BYTES
            or self.direct_children >= DISCOVERY_CLASS_MAX_CHILDREN
        )

    @property
    def parse_exhausted(self) -> bool:
        return (
            self.candidates >= DISCOVERY_CLASS_MAX_CANDIDATES
            or self.frontmatter_bytes >= DISCOVERY_CLASS_MAX_FRONTMATTER_BYTES
        )

    @property
    def remaining_frontmatter(self) -> int:
        return max(0, DISCOVERY_CLASS_MAX_FRONTMATTER_BYTES - self.frontmatter_bytes)

    @property
    def remaining_children(self) -> int:
        return max(0, DISCOVERY_CLASS_MAX_CHILDREN - self.direct_children)


@dataclass(frozen=True)
class _SnapshotEntry:
    path: Path
    relative: str
    relative_bytes: bytes
    is_directory: bool


def _strip_yaml_comment(value: str) -> str:
    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if double_quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                double_quoted = False
        elif single_quoted:
            if char == "'" and index + 1 < len(value) and value[index + 1] == "'":
                index += 1
            elif char == "'":
                single_quoted = False
        elif char == "'":
            single_quoted = True
        elif char == '"':
            double_quoted = True
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
        index += 1
    return value.rstrip()


def _decode_scalar(value: str) -> str:
    value = value.strip()
    try:
        node = yaml.compose(value, Loader=yaml.BaseLoader)
    except Exception:
        node = None
    if isinstance(node, yaml.ScalarNode):
        return node.value.strip()
    return value.strip()


def _quoted_scalar_is_closed(value: str) -> bool:
    """Return whether a leading YAML quote has a matching closing quote."""

    if not value or value[0] not in {"'", '"'}:
        return True
    quote = value[0]
    escaped = False
    index = 1
    while index < len(value):
        char = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                return True
        elif char == quote:
            if index + 1 < len(value) and value[index + 1] == quote:
                index += 1
            else:
                return True
        index += 1
    return False


def _normalize_description(value: str) -> str:
    without_controls = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    return " ".join(without_controls.split())


class _BoundedBaseLoader(yaml.BaseLoader):
    """Compose bounded YAML nodes without constructing optional typed values."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._composed_nodes = 0
        self._aliases = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            self._aliases += 1
            if self._aliases > 128:
                raise yaml.YAMLError("frontmatter contains too many YAML aliases")
        else:
            self._composed_nodes += 1
            if self._composed_nodes > 1024:
                raise yaml.YAMLError("frontmatter contains too many YAML nodes")
        return super().compose_node(parent, index)


def _normalize_frontmatter_value(field: str, value: str) -> str:
    return _normalize_description(value) if field == "description" else value.strip()


def _structured_frontmatter_fields(frontmatter: str) -> dict[str, str]:
    """Read only the root scalar fields needed by the managed catalog."""

    try:
        node = yaml.compose(frontmatter, Loader=_BoundedBaseLoader)
    except Exception:
        return {}
    if not isinstance(node, yaml.MappingNode):
        return {}

    fields: dict[str, str] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
            continue
        field = key_node.value.strip()
        if field not in _CATALOG_FIELDS or field in fields:
            continue
        value = _normalize_frontmatter_value(field, value_node.value)
        if value:
            fields[field] = value
    return fields


def _top_level_catalog_field(line: str) -> tuple[str, str] | None:
    """Split one top-level mapping line and decode its scalar key."""

    if not line or line[0].isspace() or line[0] == "#":
        return None
    single_quoted = False
    double_quoted = False
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if double_quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                double_quoted = False
        elif single_quoted:
            if char == "'" and index + 1 < len(line) and line[index + 1] == "'":
                index += 1
            elif char == "'":
                single_quoted = False
        elif char == "'":
            single_quoted = True
        elif char == '"':
            double_quoted = True
        elif char == ":":
            field = _decode_scalar(line[:index])
            if field in _CATALOG_FIELDS:
                return field, line[index + 1 :]
            return None
        index += 1
    return None


def _frontmatter_fields(lines: Sequence[str]) -> dict[str, str]:
    fields = _structured_frontmatter_fields("".join(lines))
    if "name" in fields and "description" in fields:
        return fields

    index = 0
    while index < len(lines):
        matched = _top_level_catalog_field(lines[index].rstrip("\r\n"))
        if matched is None:
            index += 1
            continue

        field, raw_value = matched
        if field in fields:
            index += 1
            continue

        value = _strip_yaml_comment(raw_value.strip())
        if _BLOCK_SCALAR_RE.match(value):
            continuation: list[str] = []
            index += 1
            while index < len(lines):
                line = lines[index].rstrip("\r\n")
                if line and not line[0].isspace() and _TOP_LEVEL_FIELD_RE.match(line):
                    break
                continuation.append(line.strip())
                index += 1
            value = " ".join(part for part in continuation if part)
        elif not value:
            continuation = []
            index += 1
            while index < len(lines):
                line = lines[index].rstrip("\r\n")
                if line and not line[0].isspace():
                    if line.startswith("#"):
                        index += 1
                        continue
                    break
                continuation.append(_strip_yaml_comment(line.strip()))
                index += 1
            value = " ".join(part for part in continuation if part)
        elif value[0] in {"'", '"'} and not _quoted_scalar_is_closed(value):
            continuation = [value]
            index += 1
            while index < len(lines):
                line = lines[index].rstrip("\r\n")
                if line and not line[0].isspace():
                    break
                continuation.append(line.strip())
                index += 1
                combined = " ".join(part for part in continuation if part)
                if _quoted_scalar_is_closed(combined):
                    break
            value = _strip_yaml_comment(" ".join(part for part in continuation if part))
        elif field == "description":
            continuation = [value]
            index += 1
            while index < len(lines):
                line = lines[index].rstrip("\r\n")
                if line and not line[0].isspace():
                    break
                continuation.append(_strip_yaml_comment(line.strip()))
                index += 1
            value = " ".join(part for part in continuation if part)
        else:
            index += 1

        decoded = _normalize_frontmatter_value(field, _decode_scalar(value))
        if decoded:
            fields[field] = decoded

    return fields


def _body_has_terminal_controls(value: str) -> bool:
    return any(
        unicodedata.category(char) == "Cc" and char not in {"\t", "\n", "\r"}
        for char in value
    )


def _stat_token(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _path_is_utf8(path: Path) -> bool:
    try:
        os.fspath(path).encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _regular_open_flags() -> int:
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    return flags


def _open_regular_file(path: str | Path, *, dir_fd: int | None = None) -> tuple[int, os.stat_result]:
    raw_path = os.fspath(path)
    follow_guard = int(getattr(os, "O_NOFOLLOW", 0))
    before_path: os.stat_result | None = None
    if not follow_guard:
        try:
            before_path = os.stat(raw_path, dir_fd=dir_fd, follow_symlinks=False)
        except (OSError, TypeError, NotImplementedError):
            before_path = None
        if before_path is not None and not stat.S_ISREG(before_path.st_mode):
            raise OSError(errno.EINVAL, "Skill file is not a regular file", raw_path)

    try:
        fd = os.open(raw_path, _regular_open_flags(), dir_fd=dir_fd)
    except (TypeError, NotImplementedError):
        if dir_fd is not None:
            raise
        fd = os.open(raw_path, _regular_open_flags())
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "Skill file is not a regular file", raw_path)
        if before_path is not None and _directory_identity(before_path) != _directory_identity(opened):
            raise OSError(errno.ESTALE, "Skill file changed while opening", raw_path)
        return fd, opened
    except BaseException:
        os.close(fd)
        raise


def _frontmatter_bounds(data: bytes, *, complete: bool) -> tuple[int, int, int] | None:
    start = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    opening_end = data.find(b"\n", start)
    if opening_end < 0:
        return None
    if data[start:opening_end].rstrip(b"\r") != b"---":
        return None

    position = opening_end + 1
    while position <= len(data):
        line_end = data.find(b"\n", position)
        if line_end < 0:
            if not complete:
                return None
            line_end = len(data)
            next_position = line_end
        else:
            next_position = line_end + 1
        if data[position:line_end].rstrip(b"\r") == b"---":
            return opening_end + 1, position, next_position
        if line_end >= len(data):
            return None
        position = next_position
    return None


def _read_prefix(fd: int, *, limit: int, file_size: int) -> bytes:
    data = bytearray()
    while len(data) < limit:
        chunk = os.read(fd, min(4096, limit - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if _frontmatter_bounds(bytes(data), complete=len(data) >= file_size) is not None:
            break
    return bytes(data)


def _read_all(fd: int, *, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(64 * 1024, limit + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise ValueError("Skill file exceeds the load limit")


def _read_skill_path(
    skill_file: Path,
    *,
    priority: tuple[object, ...],
    include_body: bool,
    frontmatter_limit: int = FRONTMATTER_MAX_BYTES,
    dir_fd: int | None = None,
    source_directory: Path | None = None,
    source_directory_identity: tuple[int, int] | None = None,
    expected_directory_identity: tuple[int, int] | None = None,
) -> tuple[ManagedSkill | None, int]:
    fd: int | None = None
    try:
        fd, before = _open_regular_file(
            "SKILL.md" if dir_fd is not None else skill_file,
            dir_fd=dir_fd,
        )
        if before.st_size > FRONTMATTER_MAX_BYTES + SKILL_BODY_MAX_BYTES:
            return None, 0
        if include_body:
            data = _read_all(fd, limit=FRONTMATTER_MAX_BYTES + SKILL_BODY_MAX_BYTES)
        else:
            data = _read_prefix(
                fd,
                limit=min(FRONTMATTER_MAX_BYTES, max(0, frontmatter_limit)),
                file_size=int(before.st_size),
            )
        after = os.fstat(fd)
        if _stat_token(before) != _stat_token(after):
            return None, len(data)
        try:
            path_after = os.stat(
                "SKILL.md" if dir_fd is not None else skill_file,
                dir_fd=dir_fd,
                follow_symlinks=False,
            )
        except (OSError, TypeError, NotImplementedError):
            return None, len(data)
        if (
            not stat.S_ISREG(path_after.st_mode)
            or _directory_identity(path_after) != _directory_identity(before)
        ):
            return None, len(data)
    except (OSError, ValueError):
        return None, 0
    finally:
        if fd is not None:
            os.close(fd)

    bounds = _frontmatter_bounds(data, complete=include_body or len(data) >= before.st_size)
    if bounds is None:
        return None, min(len(data), frontmatter_limit)
    frontmatter_start, frontmatter_end, body_start = bounds
    if body_start > FRONTMATTER_MAX_BYTES or body_start > frontmatter_limit:
        return None, min(body_start, frontmatter_limit)
    if before.st_size - body_start > SKILL_BODY_MAX_BYTES:
        return None, body_start

    frontmatter = data[frontmatter_start:frontmatter_end].decode("utf-8", errors="replace")
    fields = _frontmatter_fields(frontmatter.splitlines(keepends=True))
    name = fields.get("name", "")
    description = fields.get("description", "")
    disable_model_invocation = fields.get("disable-model-invocation", "").casefold() in {
        "1",
        "on",
        "true",
        "yes",
    }
    if not description or not 1 <= len(name) <= 64 or _PORTABLE_NAME_RE.fullmatch(name) is None:
        return None, body_start
    if len(description) > CATALOG_DESCRIPTION_MAX_CHARS:
        description = description[: CATALOG_DESCRIPTION_MAX_CHARS - 3].rstrip() + "..."

    body: str | None = None
    if include_body:
        try:
            body = data[body_start:].decode("utf-8")
        except UnicodeDecodeError:
            return None, body_start

    directory = _absolute_path(skill_file.parent)
    if not _path_is_utf8(directory):
        return None, body_start
    source = _absolute_path(source_directory or directory)
    try:
        directory_stat = directory.stat(follow_symlinks=False)
    except OSError:
        return None, body_start
    if not stat.S_ISDIR(directory_stat.st_mode):
        return None, body_start
    directory_identity = _directory_identity(directory_stat)
    if expected_directory_identity is not None and directory_identity != expected_directory_identity:
        return None, body_start
    if not _source_directory_still_matches(
        source,
        directory,
        source_directory_identity or directory_identity,
    ):
        return None, body_start

    return (
        ManagedSkill(
            name=name,
            description=description,
            directory=directory,
            priority=priority,
            body=body,
            directory_identity=directory_identity,
            source_directory=source,
            source_directory_identity=source_directory_identity or directory_identity,
            frontmatter_bytes=body_start,
            disable_model_invocation=disable_model_invocation,
        ),
        body_start,
    )


def parse_skill_file(
    skill_file: Path,
    *,
    priority: tuple[object, ...],
    include_body: bool = True,
) -> ManagedSkill | None:
    """Parse one Skill through the same bounded regular-file path as discovery."""

    normalized_priority = tuple(priority)
    if len(normalized_priority) == 3:
        normalized_priority = (*normalized_priority, str(_absolute_path(skill_file.parent)))
    skill, _ = _read_skill_path(
        skill_file,
        priority=normalized_priority,
        include_body=include_body,
    )
    return skill


def _root_children(
    root: Path,
    *,
    ignored_names: frozenset[str] = frozenset(),
    budget: _DiscoveryBudget | None = None,
) -> list[tuple[str, Path, os.stat_result]] | None:
    children: list[tuple[str, Path, os.stat_result]] = []
    limit = DISCOVERY_ROOT_MAX_CHILDREN
    if budget is not None:
        if budget.remaining_children == 0:
            return None
        limit = min(limit, budget.remaining_children)
    enumerated = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                enumerated += 1
                if enumerated > limit:
                    if budget is not None:
                        budget.direct_children += enumerated
                    return None
                if entry.name in ignored_names:
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                children.append((entry.name, Path(entry.path), entry_stat))
    except OSError:
        return []
    if budget is not None:
        budget.direct_children += enumerated
    children.sort(key=lambda item: item[0])
    return children


def _resolved_candidate_directory(
    source: Path,
    source_stat: os.stat_result,
) -> tuple[Path, tuple[int, int], tuple[int, int]] | None:
    source_identity = _directory_identity(source_stat)
    if stat.S_ISDIR(source_stat.st_mode):
        return _absolute_path(source), source_identity, source_identity
    if not stat.S_ISLNK(source_stat.st_mode):
        return None
    try:
        target = source.resolve(strict=True)
        target_stat = target.stat(follow_symlinks=False)
        after = source.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return None
    if (
        not stat.S_ISDIR(target_stat.st_mode)
        or not stat.S_ISLNK(after.st_mode)
        or _directory_identity(after) != source_identity
    ):
        return None
    return _absolute_path(target), source_identity, _directory_identity(target_stat)


def _scan_root(
    root: Path,
    *,
    priority: tuple[int, int, int],
    budget: _DiscoveryBudget,
    seen_directory_identities: set[tuple[int, int]] | None = None,
    ignored_names: frozenset[str] = frozenset(),
) -> list[ManagedSkill]:
    children = _root_children(root, ignored_names=ignored_names, budget=budget)
    if children is None:
        logger.info("Omitting oversized Skill root: %s", root)
        return []

    skills: list[ManagedSkill] = []
    for _, child, child_stat in children:
        if budget.parse_exhausted:
            break
        resolved_directory = _resolved_candidate_directory(child, child_stat)
        if resolved_directory is None:
            continue
        directory, source_identity, directory_identity = resolved_directory
        if not _path_is_utf8(directory):
            continue
        if seen_directory_identities is not None:
            if directory_identity in seen_directory_identities:
                continue
            seen_directory_identities.add(directory_identity)
        budget.candidates += 1
        skill_file = directory / "SKILL.md"
        path_priority: tuple[object, ...] = (*priority, str(_absolute_path(child)))
        skill, consumed = _read_skill_path(
            skill_file,
            priority=path_priority,
            include_body=False,
            frontmatter_limit=min(FRONTMATTER_MAX_BYTES, budget.remaining_frontmatter),
            source_directory=child,
            source_directory_identity=source_identity,
            expected_directory_identity=directory_identity,
        )
        budget.frontmatter_bytes += consumed
        if skill is not None:
            skills.append(skill)
    return skills


def _project_directories(
    cwd: Path,
    *,
    project_base: str | Path | None = None,
) -> list[Path]:
    current = cwd.expanduser().resolve()
    if not current.is_dir():
        return []
    boundary = _project_base_for_working_directory(current, project_base)
    directories: list[Path] = []
    directory = current
    for _ in range(PROJECT_ROOT_MAX_DIRECTORIES):
        directories.append(directory)
        if directory == boundary or (boundary is None and (directory / ".git").exists()):
            return directories
        if directory.parent == directory:
            break
        directory = directory.parent
    return [current]


def _project_base_for_working_directory(
    working_directory: Path,
    project_base: str | Path | None,
) -> Path | None:
    if project_base is None:
        return None
    raw = Path(project_base).expanduser()
    if not raw.is_absolute():
        return None
    resolved = raw.resolve()
    try:
        relative = working_directory.relative_to(resolved)
    except ValueError:
        return None
    if len(relative.parts) >= PROJECT_ROOT_MAX_DIRECTORIES:
        return None
    return resolved


def managed_skill_project_base(context: Any) -> str | None:
    """Return the project base bound to a resolved Avibe run target."""

    payload = getattr(context, "platform_specific", None) or {}
    target = payload.get("agent_run_target") if isinstance(payload, dict) else None
    value = target.get("project_base") if isinstance(target, dict) else None
    return str(value) if isinstance(value, str) and value else None


def managed_skill_claude_cli_path(config: Any) -> str | None:
    """Return the Claude executable selected by the live runtime config."""

    claude = getattr(config, "claude", None)
    if claude is None:
        claude = getattr(getattr(config, "agents", None), "claude", None)
    value = getattr(claude, "cli_path", None)
    normalized = str(value).strip() if value is not None else ""
    return os.path.expanduser(normalized) if normalized else None


def _working_directory(cwd: str | Path | None) -> Path:
    if cwd is not None:
        return Path(cwd).expanduser().resolve()
    bound = os.environ.get(SKILL_WORKING_DIR_ENV)
    return Path(bound or Path.cwd()).expanduser().resolve()


def _selected_builtin_root(
    avibe_home: Path,
    *,
    snapshot_id: str | None = None,
    snapshot_root: str | Path | None = None,
) -> Path | None:
    selected = snapshot_id if snapshot_id is not None else os.environ.get(BUILTIN_SKILLS_SNAPSHOT_ENV)
    bound_root = snapshot_root if snapshot_root is not None else os.environ.get(BUILTIN_SKILLS_ROOT_ENV)
    if bound_root:
        raw_root = Path(bound_root).expanduser()
        if not raw_root.is_absolute():
            return None
        root = _absolute_path(raw_root)
        if selected is None:
            selected = root.name
        if selected and root.name != selected:
            return None
    else:
        root = None
    if selected is None:
        try:
            selected = publish_builtin_skills(destination_root=avibe_home / "builtin-skills")
        except Exception:
            logger.warning("Failed to publish Avibe built-in Skills", exc_info=True)
            return None
    if _SNAPSHOT_ID_RE.fullmatch(selected or "") is None:
        return None
    if root is None:
        root = avibe_home / "builtin-skills" / selected
    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError:
        return None
    return root if stat.S_ISDIR(root_stat.st_mode) else None


def _claude_plugin_skill_roots(
    working_directory: Path,
    claude_home: Path,
    claude_cli_path: str | None,
) -> list[Path]:
    registry = claude_home / "plugins" / "installed_plugins.json"
    try:
        if not registry.is_file():
            return []
    except OSError:
        return []

    command = (
        claude_cli_path
        or os.environ.get(SKILL_CLAUDE_CLI_PATH_ENV)
        or os.environ.get("CLAUDE_CLI_PATH")
        or shutil.which("claude")
    )
    if not command:
        return []
    environment = dict(os.environ)
    environment["CLAUDE_CONFIG_DIR"] = str(claude_home)
    output = _bounded_subprocess_stdout(
        [command, "plugin", "list", "--json"],
        cwd=working_directory,
        env=environment,
        timeout=CLAUDE_PLUGIN_LIST_TIMEOUT_SECONDS,
        max_bytes=CLAUDE_PLUGIN_LIST_MAX_BYTES,
    )
    if output is None:
        return []
    try:
        payload = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list) or len(payload) > CLAUDE_PLUGIN_LIST_MAX_ENTRIES:
        return []

    roots: list[tuple[str, Path]] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        plugin_id = item.get("id")
        install_path = item.get("installPath")
        if not isinstance(plugin_id, str) or not isinstance(install_path, str):
            continue
        raw_path = Path(install_path).expanduser()
        if not raw_path.is_absolute():
            continue
        root = _absolute_path(raw_path) / "skills"
        if not _path_is_utf8(root):
            continue
        roots.append((plugin_id, root))
    return [root for _, root in sorted(roots, key=lambda item: (item[0], str(item[1])))]


def _bounded_subprocess_stdout(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> bytes | None:
    """Capture stdout while bounding combined stdout and stderr in memory."""

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        logger.info("Unable to start bounded subprocess", exc_info=True)
        return None

    stdout = bytearray()
    total_bytes = 0
    lock = threading.Lock()
    limit_exceeded = threading.Event()

    def drain(stream, *, collect: bool) -> None:
        nonlocal total_bytes
        try:
            while chunk := stream.read(64 * 1024):
                with lock:
                    total_bytes += len(chunk)
                    if collect and len(stdout) <= max_bytes:
                        remaining = max_bytes + 1 - len(stdout)
                        stdout.extend(chunk[:remaining])
                    if total_bytes > max_bytes:
                        limit_exceeded.set()
        except (OSError, ValueError):
            return

    assert process.stdout is not None
    assert process.stderr is not None
    streams = (process.stdout, process.stderr)
    threads = (
        threading.Thread(target=drain, args=(process.stdout,), kwargs={"collect": True}, daemon=True),
        threading.Thread(target=drain, args=(process.stderr,), kwargs={"collect": False}, daemon=True),
    )
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        if limit_exceeded.wait(min(0.02, remaining)):
            break

    if (timed_out or limit_exceeded.is_set()) and process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        for stream in streams:
            stream.close()
        return None
    if timed_out or limit_exceeded.is_set() or process.returncode != 0:
        return None
    return bytes(stdout)


def resolve_skills(
    cwd: str | Path | None = None,
    *,
    project_base: str | Path | None = None,
    home: str | Path | None = None,
    avibe_home: str | Path | None = None,
    codex_home: str | Path | None = None,
    claude_home: str | Path | None = None,
    claude_cli_path: str | None = None,
    xdg_config_home: str | Path | None = None,
    builtin_snapshot_id: str | None = None,
    builtin_snapshot_root: str | Path | None = None,
) -> list[ManagedSkill]:
    """Resolve the live Skill catalog using Avibe's v1 precedence rules."""

    resolved_home = (
        Path(home).expanduser().resolve()
        if home is not None
        else Path(os.environ.get(SKILL_HOME_ENV) or Path.home()).expanduser().resolve()
    )
    resolved_avibe_home = (
        Path(avibe_home).expanduser().resolve()
        if avibe_home is not None
        else paths.get_vibe_remote_dir().expanduser().resolve()
    )
    resolved_codex_home = (
        Path(codex_home).expanduser().resolve()
        if codex_home is not None
        else Path(
            os.environ.get(SKILL_CODEX_HOME_ENV)
            or os.environ.get("CODEX_HOME")
            or resolved_home / ".codex"
        )
        .expanduser()
        .resolve()
    )
    if claude_home is not None:
        resolved_claude_home = Path(claude_home).expanduser().resolve()
    else:
        bound_claude_home = os.environ.get(SKILL_CLAUDE_HOME_ENV)
        if bound_claude_home:
            resolved_claude_home = Path(bound_claude_home).expanduser().resolve()
        else:
            from vibe.claude_config import get_claude_home

            resolved_claude_home = get_claude_home(resolved_home if home is not None else None).expanduser().resolve()
    resolved_xdg_home = (
        Path(xdg_config_home).expanduser().resolve()
        if xdg_config_home is not None
        else Path(
            os.environ.get(SKILL_XDG_CONFIG_HOME_ENV)
            or os.environ.get("XDG_CONFIG_HOME")
            or resolved_home / ".config"
        )
        .expanduser()
        .resolve()
    )

    candidates: list[ManagedSkill] = []
    builtin_budget = _DiscoveryBudget()
    builtin_root = _selected_builtin_root(
        resolved_avibe_home,
        snapshot_id=builtin_snapshot_id,
        snapshot_root=builtin_snapshot_root,
    )
    if builtin_root is not None:
        candidates.extend(_scan_root(builtin_root, priority=(0, 0, 0), budget=builtin_budget))

    compatibility_budget = _DiscoveryBudget()
    compatibility_directory_identities: set[tuple[int, int]] = set()
    working_directory = _working_directory(cwd)
    selected_project_base = (
        project_base
        if project_base is not None
        else os.environ.get(SKILL_PROJECT_BASE_ENV)
    )
    for depth, directory in enumerate(
        _project_directories(
            working_directory,
            project_base=selected_project_base,
        )
    ):
        for relative_root, family_rank in _PROJECT_FAMILIES:
            if compatibility_budget.exhausted:
                break
            candidates.extend(
                _scan_root(
                    directory / relative_root,
                    priority=(1, depth, family_rank),
                    budget=compatibility_budget,
                    seen_directory_identities=compatibility_directory_identities,
                )
            )
        if compatibility_budget.exhausted:
            break

    global_user_roots = (
        (resolved_home / ".agents" / "skills", 1, frozenset()),
        (resolved_codex_home / "skills", 2, frozenset({".system"})),
        (resolved_claude_home / "skills", 3, frozenset()),
        (resolved_xdg_home / "opencode" / "skills", 4, frozenset()),
    )
    for root, family_rank, ignored_names in global_user_roots:
        if compatibility_budget.exhausted:
            break
        candidates.extend(
            _scan_root(
                root,
                priority=(2, 0, family_rank),
                budget=compatibility_budget,
                seen_directory_identities=compatibility_directory_identities,
                ignored_names=ignored_names,
            )
        )

    for root in _claude_plugin_skill_roots(
        working_directory,
        resolved_claude_home,
        claude_cli_path,
    ):
        if compatibility_budget.exhausted:
            break
        candidates.extend(
            _scan_root(
                root,
                priority=(2, 0, 5),
                budget=compatibility_budget,
                seen_directory_identities=compatibility_directory_identities,
            )
        )

    if not compatibility_budget.exhausted:
        candidates.extend(
            _scan_root(
                resolved_codex_home / "skills" / ".system",
                priority=(2, 0, 6),
                budget=compatibility_budget,
                seen_directory_identities=compatibility_directory_identities,
            )
        )

    winners: dict[str, ManagedSkill] = {}
    for candidate in candidates:
        current = winners.get(candidate.name)
        if current is None or candidate.priority < current.priority:
            winners[candidate.name] = candidate
    return sorted(winners.values(), key=lambda skill: skill.name)


def _catalog_pages(skills: Sequence[ManagedSkill]) -> list[list[ManagedSkill]]:
    pages: list[list[ManagedSkill]] = []
    current: list[ManagedSkill] = []
    current_bytes = 0
    for skill in sorted(
        skills,
        key=lambda item: (
            item.name.casefold(),
            item.name,
            item.description,
            str(item.directory),
        ),
    ):
        if skill.disable_model_invocation:
            continue
        row = f"- {skill.name}: {skill.description}"
        row_bytes = len(row.encode("utf-8")) + (1 if current else 0)
        if current and (len(current) >= CATALOG_PAGE_SIZE or current_bytes + row_bytes > CATALOG_PAGE_MAX_BYTES):
            pages.append(current)
            current = []
            current_bytes = 0
            row_bytes = len(row.encode("utf-8"))
        current.append(skill)
        current_bytes += row_bytes
    if current:
        pages.append(current)
    return pages


def _page(skills: Sequence[ManagedSkill], page: int) -> tuple[Sequence[ManagedSkill], int | None]:
    if isinstance(page, bool) or page < 1:
        raise ValueError("page must be a positive integer")
    pages = _catalog_pages(skills)
    if page > len(pages):
        return (), None
    return pages[page - 1], page + 1 if page < len(pages) else None


def render_skill_list(
    skills: Sequence[ManagedSkill],
    *,
    page: int = 1,
    more_notice: str | None = None,
) -> str:
    entries, next_page = _page(skills, page)
    lines = [f"- {skill.name}: {skill.description}" for skill in entries]
    if next_page is not None:
        lines.append(
            more_notice or render_prompt("skills-more-notice", next_page=next_page)
        )
    return "\n".join(lines)


def render_skill_catalog_prompt(skills: Sequence[ManagedSkill]) -> str:
    return join_prompt_blocks(render_skill_catalog_blocks(skills))


def render_skill_catalog_blocks(skills: Sequence[ManagedSkill]) -> list[RenderedPromptBlock]:
    rows = render_skill_list(skills, page=1)
    if not rows:
        if any(skill.disable_model_invocation for skill in skills):
            return [render_prompt_block("skills-manual-prompt")]
        return []
    _, next_page = _page(skills, 1)
    blocks = [render_prompt_block("skills-prompt")]
    if next_page is not None:
        blocks.append(render_prompt_block("skills-pagination-prompt"))
    blocks.append(render_prompt_block("skills-catalog", skill_rows=rows))
    return blocks


def render_skill_content(skill: ManagedSkill) -> str:
    if skill.body is None:
        raise ValueError("Skill body was not loaded")
    name = html.escape(skill.name, quote=True)
    directory = html.escape(str(skill.directory), quote=True)
    directory = "".join(
        f"&#x{ord(char):X};" if unicodedata.category(char).startswith("C") else char for char in directory
    )
    return f'<skill_content name="{name}" directory="{directory}">\n{skill.body}</skill_content>'


def _open_selected_directory(skill: ManagedSkill) -> tuple[int | None, tuple[int, int]]:
    if not _source_directory_still_matches(
        skill.source_directory or skill.directory,
        skill.directory,
        skill.source_directory_identity or skill.directory_identity,
    ):
        raise OSError(errno.ESTALE, "Selected Skill source directory changed")
    try:
        before = skill.directory.stat(follow_symlinks=False)
    except OSError as exc:
        raise OSError(errno.ESTALE, "Selected Skill directory disappeared") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise OSError(errno.ENOTDIR, "Selected Skill path is not a directory")
    identity = _directory_identity(before)
    if skill.directory_identity is not None and identity != skill.directory_identity:
        raise OSError(errno.ESTALE, "Selected Skill directory changed")

    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW", "O_DIRECTORY"):
        flags |= int(getattr(os, name, 0))
    try:
        fd = os.open(skill.directory, flags)
    except OSError:
        if os.name != "nt":
            raise
        return None, identity
    opened = os.fstat(fd)
    if not stat.S_ISDIR(opened.st_mode) or _directory_identity(opened) != identity:
        os.close(fd)
        raise OSError(errno.ESTALE, "Selected Skill directory changed while opening")
    return fd, identity


def _directory_path_still_matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and _directory_identity(current) == identity


def _source_directory_still_matches(
    source: Path,
    target: Path,
    identity: tuple[int, int] | None,
) -> bool:
    try:
        before = source.stat(follow_symlinks=False)
        if identity is not None and _directory_identity(before) != identity:
            return False
        if source == target:
            return stat.S_ISDIR(before.st_mode)
        if not stat.S_ISLNK(before.st_mode):
            return False
        resolved = source.resolve(strict=True)
        after = source.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return False
    return (
        resolved == target and stat.S_ISLNK(after.st_mode) and _directory_identity(after) == _directory_identity(before)
    )


def load_skill(
    name: str,
    cwd: str | Path | None = None,
    *,
    resolved_skills: Sequence[ManagedSkill] | None = None,
) -> ManagedSkill | None:
    if _PORTABLE_NAME_RE.fullmatch(name) is None or not 1 <= len(name) <= 64:
        return None
    catalog = resolve_skills(cwd) if resolved_skills is None else resolved_skills
    winner = next((skill for skill in catalog if skill.name == name), None)
    if winner is None:
        return None

    directory_fd: int | None = None
    try:
        directory_fd, identity = _open_selected_directory(winner)
        loaded, _ = _read_skill_path(
            winner.directory / "SKILL.md",
            priority=winner.priority,
            include_body=True,
            dir_fd=directory_fd,
        )
        if (
            loaded is None
            or loaded.name != name
            or loaded.body is None
            or _body_has_terminal_controls(loaded.body)
        ):
            return None
        if not _directory_path_still_matches(winner.directory, identity) or not _source_directory_still_matches(
            winner.source_directory or winner.directory,
            winner.directory,
            winner.source_directory_identity or identity,
        ):
            return None
        return ManagedSkill(
            name=loaded.name,
            description=loaded.description,
            directory=winner.directory,
            priority=winner.priority,
            body=loaded.body,
            directory_identity=identity,
            source_directory=winner.source_directory,
            source_directory_identity=winner.source_directory_identity,
            frontmatter_bytes=loaded.frontmatter_bytes,
            disable_model_invocation=loaded.disable_model_invocation,
        )
    except OSError:
        return None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def builtin_skills_source() -> Path:
    checkout_root = Path(__file__).resolve().parents[1]
    checkout_source = checkout_root / "skills"
    pyproject = checkout_root / "pyproject.toml"
    pyproject_fd: int | None = None
    try:
        pyproject_fd, _ = _open_regular_file(pyproject)
        project_header = _read_all(pyproject_fd, limit=64 * 1024)
        project_metadata = tomllib.loads(project_header.decode("utf-8"))
        project_name = str(project_metadata.get("project", {}).get("name") or "")
    except (OSError, UnicodeDecodeError, ValueError):
        project_name = ""
    finally:
        if pyproject_fd is not None:
            os.close(pyproject_fd)
    if checkout_source.is_dir() and (checkout_root / "vibe" / "__init__.py").is_file() and project_name == "avibe-os":
        return checkout_source

    import vibe

    packaged_source = Path(vibe.__file__).resolve().parent / "builtin_skills_source"
    if packaged_source.is_dir():
        return packaged_source
    raise RuntimeError("Avibe built-in Skills are missing from this installation")


def _validate_portable_component(component: str) -> None:
    if not component or component in {".", ".."}:
        raise RuntimeError("Built-in Skill paths must be relative")
    if unicodedata.normalize("NFC", component) != component:
        raise RuntimeError(f"Built-in Skill path is not NFC: {component!r}")
    if component[-1] in {".", " "}:
        raise RuntimeError(f"Built-in Skill path has a trailing dot or space: {component!r}")
    if any(ord(char) < 32 or char in _WINDOWS_INVALID_CHARS or char == "\x00" for char in component):
        raise RuntimeError(f"Built-in Skill path is not portable: {component!r}")
    basename = component.split(".", 1)[0].casefold()
    if basename in _WINDOWS_RESERVED:
        raise RuntimeError(f"Built-in Skill path uses a Windows-reserved name: {component!r}")


def _snapshot_entries(root: Path) -> list[_SnapshotEntry]:
    entries: list[_SnapshotEntry] = []
    aliases: set[str] = set()
    file_bytes = 0
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]

    while pending:
        directory, relative_parts = pending.pop()
        try:
            with os.scandir(directory) as scanned:
                children = sorted(scanned, key=lambda item: item.name)
        except OSError as exc:
            raise RuntimeError(f"Cannot read built-in Skill source: {directory}") from exc
        accepted_children = 0
        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for child in children:
            # Installers may compile Skill scripts in place; bytecode is not part of the bundled tree.
            if child.name == "__pycache__" or child.name.endswith(_GENERATED_BYTECODE_SUFFIXES):
                continue
            accepted_children += 1
            _validate_portable_component(child.name)
            child_parts = (*relative_parts, child.name)
            relative = "/".join(child_parts)
            relative_bytes = relative.encode("utf-8")
            alias = relative.casefold()
            if alias in aliases:
                raise RuntimeError(f"Built-in Skill paths collide case-insensitively: {relative}")
            aliases.add(alias)
            if len(entries) >= BUILTIN_TREE_MAX_ENTRIES:
                raise RuntimeError(
                    f"Built-in Skill tree exceeds {BUILTIN_TREE_MAX_ENTRIES:,} entries"
                )
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"Cannot inspect built-in Skill path: {relative}") from exc
            file_attributes = int(getattr(child_stat, "st_file_attributes", 0))
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if reparse_flag and file_attributes & reparse_flag:
                raise RuntimeError(f"Built-in Skill path is a reparse point: {relative}")
            if stat.S_ISDIR(child_stat.st_mode):
                entries.append(_SnapshotEntry(Path(child.path), relative, relative_bytes, True))
                child_directories.append((Path(child.path), child_parts))
            elif stat.S_ISREG(child_stat.st_mode):
                file_bytes += int(child_stat.st_size)
                if file_bytes > BUILTIN_TREE_MAX_BYTES:
                    raise RuntimeError(
                        f"Built-in Skill tree exceeds {BUILTIN_TREE_MAX_BYTES:,} bytes"
                    )
                entries.append(_SnapshotEntry(Path(child.path), relative, relative_bytes, False))
            else:
                raise RuntimeError(f"Built-in Skill path is not a directory or regular file: {relative}")
        if relative_parts and accepted_children == 0:
            raise RuntimeError(f"Built-in Skill directories must not be empty: {'/'.join(relative_parts)}")
        pending.extend(reversed(child_directories))

    entries.sort(key=lambda entry: entry.relative_bytes)
    return entries


def _update_digest_with_file(
    digest: object,
    entry: _SnapshotEntry,
    *,
    remaining_bytes: int,
) -> int:
    fd: int | None = None
    try:
        fd, before = _open_regular_file(entry.path)
        file_size = int(before.st_size)
        if file_size > remaining_bytes:
            raise RuntimeError(
                f"Built-in Skill tree exceeds {BUILTIN_TREE_MAX_BYTES:,} bytes"
            )
        digest.update(struct.pack(">Q", file_size))
        remaining = file_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError(f"Built-in Skill file changed while hashing: {entry.relative}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise RuntimeError(f"Built-in Skill file grew while hashing: {entry.relative}")
        after = os.fstat(fd)
        if _stat_token(before) != _stat_token(after):
            raise RuntimeError(f"Built-in Skill file changed while hashing: {entry.relative}")
        executable = int(before.st_mode & 0o111) if os.name != "nt" else 0
        digest.update(bytes((executable,)))
        return file_size
    finally:
        if fd is not None:
            os.close(fd)


def snapshot_tree_digest(root: str | Path) -> str:
    """Return the frozen snapshot-v1 identifier for a validated tree."""

    source = Path(root).resolve()
    if not source.is_dir():
        raise RuntimeError(f"Built-in Skills source does not exist: {source}")
    digest = hashlib.sha256(_SNAPSHOT_DOMAIN)
    consumed_bytes = 0
    for entry in _snapshot_entries(source):
        digest.update(b"d" if entry.is_directory else b"f")
        digest.update(struct.pack(">Q", len(entry.relative_bytes)))
        digest.update(entry.relative_bytes)
        if not entry.is_directory:
            consumed_bytes += _update_digest_with_file(
                digest,
                entry,
                remaining_bytes=BUILTIN_TREE_MAX_BYTES - consumed_bytes,
            )
    return digest.hexdigest()


def _copy_snapshot_entries(entries: Sequence[_SnapshotEntry], destination: Path) -> None:
    """Copy one pre-enumerated tree while charging bytes actually opened."""

    consumed_bytes = 0
    for entry in entries:
        target = destination.joinpath(*entry.relative.split("/"))
        if entry.is_directory:
            try:
                source_stat = entry.path.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot inspect built-in Skill path: {entry.relative}"
                ) from exc
            if not stat.S_ISDIR(source_stat.st_mode):
                raise RuntimeError(
                    f"Built-in Skill directory changed during publication: {entry.relative}"
                )
            target.mkdir(mode=0o700)
            continue

        source_fd: int | None = None
        target_fd: int | None = None
        try:
            source_fd, before = _open_regular_file(entry.path)
            file_size = int(before.st_size)
            if file_size > BUILTIN_TREE_MAX_BYTES - consumed_bytes:
                raise RuntimeError(
                    f"Built-in Skill tree exceeds {BUILTIN_TREE_MAX_BYTES:,} bytes"
                )
            consumed_bytes += file_size
            target_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            target_fd = os.open(target, target_flags, 0o600)
            remaining = file_size
            while remaining:
                chunk = os.read(source_fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError(
                        f"Built-in Skill file changed during publication: {entry.relative}"
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
                remaining -= len(chunk)
            if os.read(source_fd, 1):
                raise RuntimeError(
                    f"Built-in Skill file grew during publication: {entry.relative}"
                )
            after = os.fstat(source_fd)
            if _stat_token(before) != _stat_token(after):
                raise RuntimeError(
                    f"Built-in Skill file changed during publication: {entry.relative}"
                )
            if os.name != "nt" and callable(getattr(os, "fchmod", None)):
                os.fchmod(target_fd, 0o644 | int(before.st_mode & 0o111))
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if source_fd is not None:
                os.close(source_fd)


def _validate_builtin_catalog(root: Path) -> None:
    children = _root_children(root)
    if children is None:
        raise RuntimeError("Avibe packages at most 1,024 built-in Skills")
    frontmatter_bytes = 0
    declared_names: set[str] = set()
    for name, child, child_stat in children:
        if not stat.S_ISDIR(child_stat.st_mode):
            raise RuntimeError(f"Built-in Skill root entry is not a directory: {name}")
        skill, consumed = _read_skill_path(
            child / "SKILL.md",
            priority=(0, 0, 0, str(_absolute_path(child))),
            include_body=True,
        )
        if skill is None:
            raise RuntimeError(f"Built-in Skill is invalid: {name}")
        if skill.name in declared_names:
            raise RuntimeError(f"Built-in Skills must have unique declared names: {skill.name}")
        declared_names.add(skill.name)
        frontmatter_bytes += consumed
        if frontmatter_bytes > DISCOVERY_CLASS_MAX_FRONTMATTER_BYTES:
            raise RuntimeError("Built-in Skill frontmatter exceeds the 8 MiB catalog budget")


def _remove_snapshot_staging(path: Path) -> None:
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect built-in Skill staging path: {path}") from exc
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    file_attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISDIR(value.st_mode) and not (reparse_flag and file_attributes & reparse_flag):
        shutil.rmtree(path)
    elif stat.S_ISDIR(value.st_mode):
        path.rmdir()
    else:
        path.unlink()


def publish_builtin_skills(
    *,
    source_root: str | Path | None = None,
    destination_root: str | Path | None = None,
) -> str:
    """Publish this artifact's built-ins to one content-addressed snapshot."""

    source = Path(source_root).resolve() if source_root is not None else builtin_skills_source()
    umbrella = (
        Path(destination_root).expanduser().resolve()
        if destination_root is not None
        else (paths.get_vibe_remote_dir() / "builtin-skills").expanduser().resolve()
    )
    _snapshot_entries(source)
    _validate_builtin_catalog(source)
    snapshot_id = snapshot_tree_digest(source)
    destination = umbrella / snapshot_id

    from storage.lock import MigrationFileLock

    umbrella.mkdir(parents=True, exist_ok=True)
    lock_path = umbrella / f".publish-{snapshot_id}.lock"
    with MigrationFileLock(lock_path, timeout_seconds=None):
        staging = umbrella / f".snapshot-{snapshot_id}.staging"
        _remove_snapshot_staging(staging)
        if os.path.lexists(destination):
            try:
                destination_stat = destination.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"Built-in Skill snapshot is unavailable: {destination}") from exc
            if not stat.S_ISDIR(destination_stat.st_mode):
                raise RuntimeError(f"Built-in Skill snapshot is not a directory: {destination}")
            try:
                with os.scandir(destination):
                    pass
            except OSError as exc:
                raise RuntimeError(f"Built-in Skill snapshot is unreadable: {destination}") from exc
            return snapshot_id
        staging.mkdir(mode=0o700)
        try:
            _copy_snapshot_entries(_snapshot_entries(source), staging)
            _validate_builtin_catalog(staging)
            if snapshot_tree_digest(staging) != snapshot_id:
                raise RuntimeError("Built-in Skill source changed during publication")
            try:
                os.rename(staging, destination)
            except OSError as exc:
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
        finally:
            if os.path.lexists(staging):
                _remove_snapshot_staging(staging)
    return snapshot_id


def prepare_builtin_skills() -> str:
    """Publish and bind the running artifact's built-in snapshot."""

    snapshot_id = publish_builtin_skills()
    os.environ[BUILTIN_SKILLS_SNAPSHOT_ENV] = snapshot_id
    os.environ[BUILTIN_SKILLS_ROOT_ENV] = str(
        (paths.get_vibe_remote_dir() / "builtin-skills" / snapshot_id).expanduser().resolve()
    )
    return snapshot_id


def managed_skill_environment(
    working_directory: str | Path | None,
    *,
    project_base: str | Path | None = None,
    builtin_snapshot_id: str | None = None,
    builtin_snapshot_root: str | Path | None = None,
    claude_cli_path: str | None = None,
) -> dict[str, str]:
    """Return the per-backend shell bindings consumed by ``vibe skill``."""

    env: dict[str, str] = {}
    if working_directory is not None:
        resolved_working_directory = Path(working_directory).expanduser().resolve()
        env[SKILL_WORKING_DIR_ENV] = str(resolved_working_directory)
        resolved_project_base = _project_base_for_working_directory(
            resolved_working_directory,
            project_base,
        )
        if resolved_project_base is not None:
            env[SKILL_PROJECT_BASE_ENV] = str(resolved_project_base)
    resolved_home = Path.home().resolve()
    resolved_codex_home = Path(os.environ.get("CODEX_HOME") or resolved_home / ".codex").expanduser().resolve()
    from vibe.claude_config import get_claude_home

    resolved_claude_home = get_claude_home().expanduser().resolve()
    resolved_xdg_home = Path(os.environ.get("XDG_CONFIG_HOME") or resolved_home / ".config").expanduser().resolve()
    env.update(
        {
            SKILL_HOME_ENV: str(resolved_home),
            SKILL_CODEX_HOME_ENV: str(resolved_codex_home),
            SKILL_CLAUDE_HOME_ENV: str(resolved_claude_home),
            SKILL_XDG_CONFIG_HOME_ENV: str(resolved_xdg_home),
        }
    )
    normalized_claude_cli_path = str(claude_cli_path or "").strip()
    if normalized_claude_cli_path:
        env[SKILL_CLAUDE_CLI_PATH_ENV] = os.path.expanduser(
            normalized_claude_cli_path
        )
    explicit_builtin_snapshot = builtin_snapshot_id is not None or builtin_snapshot_root is not None
    snapshot_id = (
        builtin_snapshot_id
        if explicit_builtin_snapshot
        else os.environ.get(BUILTIN_SKILLS_SNAPSHOT_ENV, "")
    )
    if isinstance(snapshot_id, str) and _SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        snapshot_root = (
            builtin_snapshot_root
            if explicit_builtin_snapshot
            else os.environ.get(BUILTIN_SKILLS_ROOT_ENV)
        )
        if snapshot_root:
            root = Path(snapshot_root).expanduser()
            if root.is_absolute() and root.name == snapshot_id:
                env[BUILTIN_SKILLS_SNAPSHOT_ENV] = snapshot_id
                env[BUILTIN_SKILLS_ROOT_ENV] = str(_absolute_path(root))
        elif not explicit_builtin_snapshot:
            env[BUILTIN_SKILLS_SNAPSHOT_ENV] = snapshot_id
            env[BUILTIN_SKILLS_ROOT_ENV] = str(
                (paths.get_vibe_remote_dir() / "builtin-skills" / snapshot_id)
                .expanduser()
                .resolve()
            )
    return env
