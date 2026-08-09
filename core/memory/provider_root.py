"""Owned filesystem policy for the EverOS provider root."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    remove_confined_path,
)


ROOT_SENTINEL_FILENAME = ".avibe-memory-root.json"
ROOT_SENTINEL_SCHEMA_VERSION = 1
ROOT_PROVIDER_ID = "everos"
MAX_ROOT_SENTINEL_BYTES = 4 * 1024
PROVIDER_ROOT_CONTROL_FILES = frozenset(
    {ROOT_SENTINEL_FILENAME, "everos.toml", "ome.toml"}
)


class ProviderRootMeta(Protocol):
    """Persisted identity needed to bind a root to one Memory store."""

    provider_root_id: str


@dataclass(frozen=True)
class ProviderRootMetadata:
    """Artifact metadata governing one provider-root format."""

    provider_root_format: str
    compatible_provider_root_formats: frozenset[str]
    artifact_fingerprint: str


@dataclass(frozen=True)
class ProviderRootState:
    """A bounded, non-secret snapshot used before an artifact cutover."""

    exists: bool
    provider_root_format: str | None = None
    empty: bool = False


class ProviderRootError(RuntimeError):
    """The provider root could not be proven safe for the requested operation."""


@dataclass(frozen=True)
class ProviderRootRollback:
    """Restore the sentinel metadata replaced by an empty-format activation."""

    _root: ProviderRoot
    _meta: ProviderRootMeta
    _metadata: ProviderRootMetadata

    def rollback(self) -> None:
        self._root._write_sentinel(self._meta, self._metadata)
        self._root._verify(
            self._meta,
            self._metadata,
            require_empty=True,
        )


class ProviderRoot:
    """Own all synchronous security, format, and transition policy for one root."""

    def __init__(self, path: Path | str, *, effective_home: Path | str) -> None:
        self.path = Path(path)
        self._effective_home = Path(effective_home)

    def inspect(self, candidate_metadata: ProviderRootMetadata) -> ProviderRootState:
        """Inspect compatibility without requiring the store-owned root id."""

        self._ensure_chain_safe()
        try:
            root_info = self.path.lstat()
        except FileNotFoundError:
            return ProviderRootState(exists=False)
        except OSError as error:
            raise ProviderRootError("memory provider root cannot be inspected") from error
        self._require_directory(root_info, "memory provider root", private=True)
        sentinel = self._read_sentinel()
        provider_root_format = sentinel["provider_root_format"]
        empty = self._is_empty()
        if (
            not empty
            and provider_root_format
            not in candidate_metadata.compatible_provider_root_formats
        ):
            raise ProviderRootError("memory provider root format is incompatible")
        return ProviderRootState(
            exists=True,
            provider_root_format=provider_root_format,
            empty=empty,
        )

    def ensure(
        self,
        meta: ProviderRootMeta,
        active_metadata: ProviderRootMetadata,
    ) -> None:
        """Create the first sentinel-owned root or verify an existing root."""

        self._ensure_chain_safe()
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise ProviderRootError(
                "memory provider root parent cannot be created"
            ) from error
        self._ensure_chain_safe()
        self._require_directory(
            self._lstat(parent, "memory provider root parent"),
            "memory provider root parent",
            private=True,
        )
        try:
            root_info = self.path.lstat()
        except FileNotFoundError:
            try:
                self.path.mkdir(mode=0o700)
                root_info = self.path.lstat()
            except OSError as error:
                raise ProviderRootError(
                    "memory provider root cannot be created"
                ) from error
        self._require_directory(root_info, "memory provider root", private=True)
        sentinel_path = self.path / ROOT_SENTINEL_FILENAME
        try:
            sentinel_path.lstat()
        except FileNotFoundError:
            if not self._directory_is_empty():
                raise ProviderRootError("memory provider root is not empty")
            self._write_sentinel(meta, active_metadata)
            self._verify(meta, active_metadata, require_empty=True)
            return
        except OSError as error:
            raise ProviderRootError(
                "memory provider root sentinel is unavailable"
            ) from error
        self._verify(meta, active_metadata, require_empty=False)

    def activate_empty_format(
        self,
        meta: ProviderRootMeta,
        candidate_metadata: ProviderRootMetadata,
    ) -> ProviderRootRollback | None:
        """Rewrite an owned empty root for a candidate and return its rollback."""

        try:
            self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ProviderRootError("memory provider root is unavailable") from error
        self._verify(
            meta,
            candidate_metadata,
            require_empty=True,
            allow_format_mismatch=True,
        )
        sentinel = self._read_sentinel()
        current_format = sentinel["provider_root_format"]
        if current_format == candidate_metadata.provider_root_format:
            return None
        previous_metadata = ProviderRootMetadata(
            provider_root_format=current_format,
            compatible_provider_root_formats=frozenset({current_format}),
            artifact_fingerprint=sentinel["created_by_artifact_fingerprint"],
        )
        rollback = ProviderRootRollback(self, meta, previous_metadata)
        try:
            self._write_sentinel(meta, candidate_metadata)
            self._verify(meta, candidate_metadata, require_empty=True)
        except Exception:
            rollback.rollback()
            raise
        return rollback

    def recreate_empty(
        self,
        meta: ProviderRootMeta,
        active_metadata: ProviderRootMetadata,
    ) -> None:
        """Remove provider children safely while preserving the owned root itself."""

        self._verify(meta, active_metadata, require_empty=False)
        try:
            with os.scandir(self.path) as entries:
                children = [
                    Path(entry.path)
                    for entry in entries
                    if entry.name != ROOT_SENTINEL_FILENAME
                ]
        except OSError as error:
            raise ProviderRootError("memory provider root cannot be read") from error
        for child in children:
            try:
                remove_confined_path(self._effective_home, child)
            except (ConfinedFilesystemError, OSError, ValueError) as error:
                raise ProviderRootError(
                    "memory provider root child could not be removed"
                ) from error
        self._write_sentinel(meta, active_metadata)
        self._verify(meta, active_metadata, require_empty=True)

    def has_data(self) -> bool:
        """Return whether the safe root has entries beyond generated control files."""

        self._ensure_chain_safe()
        try:
            root_info = self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ProviderRootError("memory provider root cannot be inspected") from error
        self._require_directory(root_info, "memory provider root", private=True)
        return not self._is_empty()

    def _verify(
        self,
        meta: ProviderRootMeta,
        metadata: ProviderRootMetadata,
        *,
        require_empty: bool,
        allow_format_mismatch: bool = False,
    ) -> None:
        self._ensure_chain_safe()
        root_info = self._lstat(self.path, "memory provider root")
        self._require_directory(root_info, "memory provider root", private=True)
        sentinel = self._read_sentinel()
        if sentinel["provider_root_id"] != meta.provider_root_id:
            raise ProviderRootError("memory provider root id does not match")
        if (
            not allow_format_mismatch
            and sentinel["provider_root_format"]
            not in metadata.compatible_provider_root_formats
        ):
            raise ProviderRootError("memory provider root format does not match")
        if require_empty and not self._is_empty():
            raise ProviderRootError("memory provider root still contains data")

    def _read_sentinel(self) -> dict[str, str | int]:
        path = self.path / ROOT_SENTINEL_FILENAME
        expected = self._lstat(path, "memory provider root sentinel")
        if (
            stat.S_ISLNK(expected.st_mode)
            or not stat.S_ISREG(expected.st_mode)
        ):
            raise ProviderRootError("memory provider root sentinel is unsafe")
        self._require_owner(expected, "memory provider root sentinel")
        if (
            stat.S_IMODE(expected.st_mode) != 0o600
            or expected.st_size > MAX_ROOT_SENTINEL_BYTES
        ):
            raise ProviderRootError("memory provider root sentinel is invalid")

        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or actual.st_dev != expected.st_dev
                or actual.st_ino != expected.st_ino
                or stat.S_IMODE(actual.st_mode) != 0o600
                or actual.st_size > MAX_ROOT_SENTINEL_BYTES
            ):
                raise ProviderRootError(
                    "memory provider root sentinel is invalid"
                )
            self._require_owner(actual, "memory provider root sentinel")
            payload = os.read(descriptor, MAX_ROOT_SENTINEL_BYTES + 1)
        except OSError as error:
            raise ProviderRootError(
                "memory provider root sentinel is invalid"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if len(payload) > MAX_ROOT_SENTINEL_BYTES:
            raise ProviderRootError("memory provider root sentinel is invalid")
        try:
            sentinel = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ProviderRootError(
                "memory provider root sentinel is invalid"
            ) from error
        expected_keys = {
            "schema_version",
            "provider_root_id",
            "provider_id",
            "provider_root_format",
            "created_by_artifact_fingerprint",
        }
        if (
            not isinstance(sentinel, dict)
            or set(sentinel) != expected_keys
            or type(sentinel.get("schema_version")) is not int
            or sentinel.get("schema_version") != ROOT_SENTINEL_SCHEMA_VERSION
            or sentinel.get("provider_id") != ROOT_PROVIDER_ID
            or not _is_metadata_value(sentinel.get("provider_root_id"))
            or not _is_metadata_value(sentinel.get("provider_root_format"))
            or not _is_metadata_value(
                sentinel.get("created_by_artifact_fingerprint")
            )
        ):
            raise ProviderRootError("memory provider root sentinel is invalid")
        return sentinel

    def _write_sentinel(
        self,
        meta: ProviderRootMeta,
        metadata: ProviderRootMetadata,
    ) -> None:
        if not (
            _is_metadata_value(meta.provider_root_id)
            and _is_metadata_value(metadata.provider_root_format)
            and _is_metadata_value(metadata.artifact_fingerprint)
        ):
            raise ProviderRootError("memory provider root metadata is invalid")
        payload = json.dumps(
            {
                "schema_version": ROOT_SENTINEL_SCHEMA_VERSION,
                "provider_root_id": meta.provider_root_id,
                "provider_id": ROOT_PROVIDER_ID,
                "provider_root_format": metadata.provider_root_format,
                "created_by_artifact_fingerprint": metadata.artifact_fingerprint,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        temporary = self.path / (
            f".{ROOT_SENTINEL_FILENAME}.{secrets.token_hex(8)}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.path / ROOT_SENTINEL_FILENAME)
            sentinel_info = self._lstat(
                self.path / ROOT_SENTINEL_FILENAME,
                "memory provider root sentinel",
            )
            self._require_regular(
                sentinel_info,
                "memory provider root sentinel",
                private=True,
            )
        except OSError as error:
            raise ProviderRootError(
                "memory provider root sentinel could not be written"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _directory_is_empty(self) -> bool:
        try:
            with os.scandir(self.path) as entries:
                return not any(True for _entry in entries)
        except OSError as error:
            raise ProviderRootError("memory provider root cannot be read") from error

    def _is_empty(self) -> bool:
        try:
            with os.scandir(self.path) as entries:
                return all(
                    entry.name in PROVIDER_ROOT_CONTROL_FILES for entry in entries
                )
        except OSError as error:
            raise ProviderRootError("memory provider root cannot be inspected") from error

    def _ensure_chain_safe(self) -> None:
        home = Path(os.path.abspath(os.fspath(self._effective_home)))
        current = Path(os.path.abspath(os.fspath(self.path)))
        while True:
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise ProviderRootError(
                    "memory provider root chain is unavailable"
                ) from error
            else:
                if stat.S_ISLNK(info.st_mode):
                    raise ProviderRootError(
                        "memory provider root chain contains a symlink"
                    )
            if current == current.parent or current == home:
                break
            current = current.parent

    @staticmethod
    def _lstat(path: Path, label: str) -> os.stat_result:
        try:
            return os.lstat(path)
        except OSError as error:
            raise ProviderRootError(f"{label} is unavailable") from error

    @classmethod
    def _require_directory(
        cls,
        info: os.stat_result,
        label: str,
        *,
        private: bool,
    ) -> None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProviderRootError(f"{label} is unsafe")
        cls._require_owner(info, label)
        if private and stat.S_IMODE(info.st_mode) != 0o700:
            raise ProviderRootError(f"{label} mode mismatch")

    @classmethod
    def _require_regular(
        cls,
        info: os.stat_result,
        label: str,
        *,
        private: bool,
    ) -> None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProviderRootError(f"{label} is unsafe")
        cls._require_owner(info, label)
        if private and stat.S_IMODE(info.st_mode) != 0o600:
            raise ProviderRootError(f"{label} is invalid")

    @staticmethod
    def _require_owner(info: os.stat_result, label: str) -> None:
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and info.st_uid != getuid():
            raise ProviderRootError(f"{label} owner mismatch")


def _is_metadata_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= 128
        and all(
            character.isascii()
            and (character.isalnum() or character in {".", "-", "_"})
            for character in value
        )
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        result = os.write(descriptor, payload[written:])
        if result <= 0:
            raise OSError("provider root sentinel write failed")
        written += result
