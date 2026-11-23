"""Keep private mobile files outside browser file, media and Show capabilities."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote

from config import paths


_PROC_DESCRIPTOR = re.compile(
    r"^/proc/(?:self|thread-self|[0-9]+)/(?:task/[0-9]+/)?(?:fd|map_files)(?:/|$)"
)


def _descriptor_namespace(path: str) -> bool:
    # macOS devfs aliases retain their spelling through realpath on a
    # case-insensitive root volume.
    path = os.path.normpath(path).casefold()
    return (
        path == "/dev/fd" or path.startswith("/dev/fd/")
        or path in {"/dev/stdin", "/dev/stdout", "/dev/stderr"}
        or bool(_PROC_DESCRIPTOR.match(path))
    )


def _descriptor_alias(raw: str) -> bool:
    """Check namespace spellings before realpath can erase a procfs alias."""
    pending = Path(os.path.abspath(os.path.expanduser(raw)))
    seen: set[Path] = set()
    for _ in range(40):
        if _descriptor_namespace(str(pending)):
            return True
        if pending in seen:
            return False  # Ordinary cyclic links retain their existing entry semantics.
        seen.add(pending)
        parts = pending.parts
        prefix = Path(parts[0])
        for index, part in enumerate(parts[1:], start=1):
            prefix /= part
            if prefix.is_symlink():
                destination = Path(os.readlink(prefix))
                if not destination.is_absolute():
                    destination = prefix.parent / destination
                pending = Path(os.path.abspath(destination.joinpath(*parts[index + 1:])))
                break
        else:
            return False
    # An unresolvable chain cannot establish a safe namespace.
    return True


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError as error:
        if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            return None
        raise
    return info.st_dev, info.st_ino


class MobileStorageBoundary:
    """One operation's private roots and file identities, without reading values.

    File identity also covers macOS firmlinks, /.vol and ordinary hard links.
    Collection callers reuse this object instead of resolving roots per entry.
    """

    def __init__(self):
        self.roots = tuple(dict.fromkeys(
            Path(os.path.realpath(root)) for root in (
                paths.get_state_dir() / "mobile",
                Path.home() / ".avibe" / "state" / "mobile",
                Path.home() / ".vibe_remote" / "state" / "mobile",
            )
        ))
        self._root_names = tuple(str(root).rstrip("/\\").casefold() for root in self.roots)
        self._root_ids: set[tuple[int, int]] = set()
        self._root_anchors: list[tuple[tuple[int, int], tuple[str, ...]]] = []
        self._nodes: set[tuple[int, int]] | None = None
        self._ancestors: set[tuple[int, int]] | None = None
        self._unavailable = False
        try:
            for root in self.roots:
                # A missing root still has a physical parent and intended suffix.
                # This reserves its aliases without creating files while browsing.
                suffix: tuple[str, ...] = ()
                for candidate in (root, *root.parents):
                    identity = _identity(candidate)
                    if identity is not None:
                        self._root_anchors.append((identity, suffix))
                        if not suffix:
                            self._root_ids.add(identity)
                        break
                    if candidate.is_symlink():
                        raise OSError(errno.ELOOP, "Unverifiable private root")
                    suffix = (candidate.name.casefold(), *suffix)
                else:
                    raise OSError(errno.ENOENT, "Unverifiable private root")
        except (OSError, RuntimeError, ValueError):
            self._unavailable = True

    def _private_nodes(self) -> set[tuple[int, int]]:
        if self._nodes is None:
            try:
                nodes = set(self._root_ids)
                pending = [root for root in self.roots if _identity(root) is not None]
                visited = set()
                while pending:
                    directory = pending.pop()
                    identity = _identity(directory)
                    if identity is None or identity in visited:
                        continue
                    visited.add(identity)
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            try:
                                # DirEntry.stat reports zero file IDs on Windows.
                                info = os.stat(entry.path, follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            nodes.add((info.st_dev, info.st_ino))
                            if stat.S_ISDIR(info.st_mode):
                                pending.append(Path(entry.path))
                self._nodes = nodes
            except (OSError, RuntimeError, ValueError):
                # This snapshot protects every path in the current operation.
                self._unavailable = True
                raise
        return self._nodes

    def _private_ancestors(self) -> set[tuple[int, int]]:
        if self._ancestors is None:
            try:
                self._ancestors = {
                    identity for root in self.roots for parent in root.parents
                    if (identity := _identity(parent))
                }
            except (OSError, RuntimeError, ValueError):
                self._unavailable = True
                raise
        return self._ancestors

    def _matches(self, raw: str, include_ancestors: bool) -> bool:
        if _descriptor_alias(raw):
            return True
        # Unlike Path.resolve on older Python, realpath preserves an unreachable
        # cyclic entry so metadata/removal can still operate on the link itself.
        target = Path(os.path.realpath(os.path.expanduser(raw)))
        if _descriptor_namespace(str(target)):
            return True
        name = str(target).rstrip("/\\").casefold()
        for private in self._root_names:
            if name == private or name.startswith(private + os.sep):
                return True
            if include_ancestors and (not name or private.startswith(name + os.sep)):
                return True
        suffix: tuple[str, ...] = ()
        for candidate in (target, *target.parents):
            identity = _identity(candidate)
            if identity is not None:
                for anchor, private_suffix in self._root_anchors:
                    if identity == anchor and (
                        suffix[:len(private_suffix)] == private_suffix
                        or (include_ancestors and private_suffix[:len(suffix)] == suffix)
                    ):
                        return True
                if identity in self._private_nodes():
                    return True
                if candidate == target and include_ancestors and identity in self._private_ancestors():
                    return True
            suffix = (candidate.name.casefold(), *suffix)
        return False

    def is_private_stat(self, info: os.stat_result) -> bool:
        """Validate an already-open object against this operation's snapshot."""
        if self._unavailable:
            return True
        try:
            return (info.st_dev, info.st_ino) in self._private_nodes()
        except (OSError, RuntimeError, ValueError):
            return True

    def is_private_path(self, value: str | Path, *, include_ancestors: bool = False) -> bool:
        if self._unavailable:
            return True
        raw = os.fspath(value)
        try:
            # Check the literal spelling too: a real HOME can contain '%'.
            for _ in range(9):
                if self._matches(raw, include_ancestors):
                    return True
                decoded = unquote(raw)
                if decoded == raw:
                    break
                raw = decoded
        except (OSError, RuntimeError, ValueError):
            # A target/ancestor error refuses this entry. Snapshot builders above
            # alone invalidate the shared boundary when private state is unknown.
            return True
        return False


def is_mobile_private_path(value: str | Path, *, include_ancestors: bool = False) -> bool:
    return MobileStorageBoundary().is_private_path(value, include_ancestors=include_ancestors)
