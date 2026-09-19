"""Private write-ahead state for native credential ownership transitions.

This document is never an API payload. Native snapshots live only until the
reversible phase ends; the completed receipt contains opaque IDs, not secrets.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.atomic_io import write_atomic
from config.v2_config import ModelHubConfig


class TakeoverStateError(RuntimeError):
    """Sanitized failure: never include a credential or native file contents."""


def _read_regular(path: Path) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise TakeoverStateError("native configuration is not a regular file")
    return path.read_bytes()


def _encoded(content: bytes | None) -> str | None:
    return base64.b64encode(content).decode("ascii") if content is not None else None


def _decoded(content: object) -> bytes | None:
    if content is None:
        return None
    if not isinstance(content, str):
        raise TakeoverStateError("invalid takeover snapshot")
    try:
        return base64.b64decode(content, validate=True)
    except ValueError:
        raise TakeoverStateError("invalid takeover snapshot") from None


@dataclass(frozen=True, repr=False)
class NativeFileEdit:
    path: Path
    before: bytes | None
    after: bytes | None
    mode: int = 0o600

    @classmethod
    def plan(cls, path: Path, after: bytes | None) -> NativeFileEdit:
        before = _read_regular(path)
        mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600
        return cls(path.absolute(), before, after, mode)

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "before": _encoded(self.before),
            "after": _encoded(self.after),
            "mode": self.mode,
        }

    @classmethod
    def from_payload(cls, payload: object) -> NativeFileEdit:
        if not isinstance(payload, dict) or set(payload) != {"path", "before", "after", "mode"}:
            raise TakeoverStateError("invalid takeover snapshot")
        path = payload["path"]
        mode = payload["mode"]
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o777
        ):
            raise TakeoverStateError("invalid takeover snapshot")
        return cls(Path(path), _decoded(payload["before"]), _decoded(payload["after"]), mode)

    def check(self, *, applied: bool = False) -> None:
        expected = self.after if applied else self.before
        if _read_regular(self.path) != expected:
            raise TakeoverStateError("native configuration changed")

    def apply(self, *, reverse: bool = False) -> None:
        """Compare before writing; replay accepts only either recorded state."""
        source, target = (self.after, self.before) if reverse else (self.before, self.after)
        actual = _read_regular(self.path)
        if actual == target:
            return
        if actual != source:
            raise TakeoverStateError("native configuration changed")
        if target is None:
            self.path.unlink()
            # The absence is part of the ownership decision, not best effort.
            if _read_regular(self.path) is not None:
                raise TakeoverStateError("native cleanup did not persist")
            _fsync_directory(self.path.parent)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Authentication-bearing files must never become more permissive.
        write_atomic(self.path, target)
        _fsync_directory(self.path.parent)
        if _read_regular(self.path) != target:
            raise TakeoverStateError("native configuration did not persist")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class NativeTakeoverJournal:
    """One controller-owned transaction, plus the last idempotency receipt."""

    def __init__(self, path: Path):
        self.path = path

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        mode = self.path.parent.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise TakeoverStateError("unsafe takeover directory")

    def load(self) -> dict[str, Any] | None:
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise TakeoverStateError("unsafe takeover journal")
        try:
            payload = json.loads(self.path.read_bytes())
        except (ValueError, UnicodeError):
            raise TakeoverStateError("invalid takeover journal") from None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("phase") not in {
                "prepared", "withdrawn", "exposed", "complete", "reverting"
            }
            or not isinstance(payload.get("items"), list)
            or not isinstance(payload.get("backends"), list)
            or not payload["backends"]
            or any(backend not in {"claude", "codex", "opencode"} for backend in payload["backends"])
            or len(set(payload["backends"])) != len(payload["backends"])
            or not isinstance(payload.get("source_ids"), list)
            or any(not isinstance(value, str) or not value for value in payload["source_ids"])
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or item.get("backend") not in payload["backends"]
                for item in payload["items"]
            )
        ):
            raise TakeoverStateError("invalid takeover journal")
        if payload["phase"] != "complete":
            try:
                ModelHubConfig.from_payload(payload["previous"])
                ModelHubConfig.from_payload(payload["updated"])
            except (KeyError, TypeError, ValueError):
                raise TakeoverStateError("invalid takeover configuration") from None
            if (
                not isinstance(payload.get("files"), list)
                or not isinstance(payload.get("credentials"), list)
                or any(
                    not isinstance(value, dict)
                    or value.get("kind") not in {"oauth", "api_key"}
                    or not isinstance(value.get("source_id"), str)
                    or not isinstance(value.get("credential_ref"), str)
                    for value in payload["credentials"]
                )
                or not isinstance(payload.get("keychain", []), list)
                or not isinstance(payload.get("native_before", {}), dict)
                or not isinstance(payload.get("native_after", {}), dict)
            ):
                raise TakeoverStateError("invalid takeover journal")
            edits = [NativeFileEdit.from_payload(value) for value in payload["files"]]
            if len({edit.path for edit in edits}) != len(edits):
                raise TakeoverStateError("duplicate takeover path")
            for edit in payload.get("keychain", []):
                if (
                    not isinstance(edit, dict)
                    or edit.get("version") != 1
                    or edit.get("backend") not in payload["backends"]
                    or not isinstance(edit.get("operations"), list)
                    or not edit["operations"]
                    or any(
                        not isinstance(operation, dict)
                        or operation.get("kind") != "keychain"
                        or not isinstance(operation.get("service"), str)
                        or not isinstance(operation.get("account"), str)
                        or not isinstance(operation.get("before"), dict)
                        or not isinstance(operation.get("after"), dict)
                        for operation in edit["operations"]
                    )
                ):
                    raise TakeoverStateError("invalid takeover credential store")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        self._prepare()
        if self.path.exists():
            self.load()  # Refuse unsafe/corrupt state, never silently overwrite.
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        write_atomic(self.path, content)
        _fsync_directory(self.path.parent)
        if self.path.read_bytes() != content:
            raise TakeoverStateError("takeover journal did not persist")

    def forget(self) -> None:
        if not self.path.exists():
            return
        self.load()
        self.path.unlink()
        _fsync_directory(self.path.parent)

    def complete(self, record: dict[str, Any]) -> None:
        receipt = NativeTakeoverJournal(self.path.with_name("last-completed.json"))
        receipt.save({
            "version": 1,
            "phase": "complete",
            "items": record["items"],
            "backends": record["backends"],
            "source_ids": record["source_ids"],
            "source_credentials": {
                source["id"]: source["credential_ref"]
                for source in record["updated"]["sources"]
                if source["id"] in record["source_ids"]
            },
        })
        self.forget()

    def completed(self) -> dict[str, Any] | None:
        return NativeTakeoverJournal(self.path.with_name("last-completed.json")).load()
