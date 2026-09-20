"""Private write-ahead state for native credential ownership transitions.

This document is never an API payload. Native snapshots are retained for
compare-before-write recovery until custody settles, never for rollback after
CPA exposure. The completed receipt contains opaque IDs, not secrets.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
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
    before_mode: int | None = None

    @classmethod
    def plan(cls, path: Path, after: bytes | None) -> NativeFileEdit:
        before = _read_regular(path)
        mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600
        return cls(path.absolute(), before, after, mode, mode if before is not None and before != after else None)

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "before": _encoded(self.before),
            "after": _encoded(self.after),
            "mode": self.mode,
            **({"before_mode": self.before_mode} if self.before_mode is not None else {}),
        }

    @classmethod
    def from_payload(cls, payload: object) -> NativeFileEdit:
        required = {"path", "before", "after", "mode"}
        if (
            not isinstance(payload, dict) or not required <= set(payload)
            or set(payload) - required - {"before_mode"}
        ):
            raise TakeoverStateError("invalid takeover snapshot")
        path = payload["path"]
        mode = payload["mode"]
        before_mode = payload.get("before_mode")
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o777
            or (
                before_mode is not None and (
                    not isinstance(before_mode, int) or isinstance(before_mode, bool)
                    or not 0 <= before_mode <= 0o777
                )
            )
        ):
            raise TakeoverStateError("invalid takeover snapshot")
        return cls(Path(path), _decoded(payload["before"]), _decoded(payload["after"]), mode, before_mode)

    def check(self, *, applied: bool = False) -> None:
        expected = self.after if applied else self.before
        mode = (self.mode if applied else self.before_mode) if self.before != self.after else None
        self._verify(expected, mode)

    def apply(self, *, reverse: bool = False) -> None:
        """Compare before writing; replay accepts only either recorded state."""
        source, target = (self.after, self.before) if reverse else (self.before, self.after)
        actual = _read_regular(self.path)
        if actual == target:
            if self.before != self.after:
                if target is not None:
                    # Bytes and mode were published together. Replay may complete
                    # durability, but never chmod someone else's replacement.
                    self._verify(target, self.before_mode if reverse else self.mode, sync=True)
                _fsync_directory(self.path.parent)
            return
        if actual != source:
            raise TakeoverStateError("native configuration changed")
        source_mode = self.mode if reverse else self.before_mode
        self._verify(source, source_mode)
        if target is None:
            self.path.unlink()
            # The absence is part of the ownership decision, not best effort.
            if _read_regular(self.path) is not None:
                raise TakeoverStateError("native cleanup did not persist")
            _fsync_directory(self.path.parent)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unlike agent-owned state (write_atomic, always 0600), this transaction
        # edits user files. Publish captured permissions WITH the target bytes;
        # never widen a path after publication. Recheck consent before swapping.
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        unpublished: str | None = temporary
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(target)
                handle.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), self.mode)
                else:  # pragma: no cover - Windows permission fallback
                    os.chmod(temporary, self.mode)
                os.fsync(handle.fileno())
            self._verify(source, source_mode)
            os.replace(temporary, self.path)
            unpublished = None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if unpublished is not None:
                Path(unpublished).unlink(missing_ok=True)
        _fsync_directory(self.path.parent)
        self._verify(target, self.mode)

    def _verify(self, expected: bytes | None, mode: int | None, *, sync: bool = False) -> None:
        if expected is None:
            if _read_regular(self.path) is not None:
                raise TakeoverStateError("native configuration changed")
            return
        descriptor = os.open(
            self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):
                raise TakeoverStateError("native configuration is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                content = handle.read()
            current = os.fstat(descriptor)
            entry = self.path.lstat()
            if (
                content != expected
                or (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino)
                or (mode is not None and stat.S_IMODE(current.st_mode) != mode)
            ):
                raise TakeoverStateError("native configuration changed")
            if sync:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)


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
            or (
                "inventory_ids" in payload
                and (
                    not isinstance(payload["inventory_ids"], list)
                    or len(payload["inventory_ids"]) != len(payload["items"])
                    or any(not isinstance(value, str) or not value for value in payload["inventory_ids"])
                )
            )
            or not isinstance(payload.get("clean_native_stores", {}), dict)
            or any(
                backend not in {"claude", "codex"}
                or not isinstance(revision, str) or not revision
                for backend, revision in payload.get("clean_native_stores", {}).items()
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
            validated = payload.get("validated_source_ids", [])
            oauth_ids = {
                credential["source_id"] for credential in payload["credentials"]
                if credential["kind"] == "oauth"
            }
            if (
                not isinstance(validated, list)
                or any(not isinstance(value, str) or value not in oauth_ids for value in validated)
                or len(set(validated)) != len(validated)
            ):
                raise TakeoverStateError("invalid takeover validation evidence")
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
            terminal = payload.get("terminal")
            if terminal is not None:
                if (
                    payload["phase"] != "exposed"
                    or not isinstance(terminal, dict)
                    or set(terminal) - {"invalid_source_ids", "config", "reason"}
                    or terminal.get("reason", "rejected") not in {"rejected", "reauth_requested"}
                    or not isinstance(terminal["invalid_source_ids"], list)
                    or not terminal["invalid_source_ids"]
                    or any(
                        not isinstance(value, str) or value not in payload["source_ids"]
                        for value in terminal["invalid_source_ids"]
                    )
                ):
                    raise TakeoverStateError("invalid takeover terminal decision")
                try:
                    expected = ModelHubConfig.from_payload(payload["updated"]).to_payload()
                    actual = ModelHubConfig.from_payload(terminal["config"]).to_payload()
                except (KeyError, TypeError, ValueError):
                    raise TakeoverStateError("invalid takeover terminal configuration") from None
                for source in expected["sources"]:
                    if source["id"] in terminal["invalid_source_ids"]:
                        from config.v2_config import ModelHubSourceStateConfig

                        source["state"] = ModelHubSourceStateConfig(
                            status="needs_action",
                            detail_key="models.source.needs_action.oauth_expired",
                        ).to_payload()
                if actual != expected:
                    raise TakeoverStateError("invalid takeover terminal configuration")
        elif payload.get("outcome", "success") not in {"success", "needs_auth", "reauth_requested"}:
            raise TakeoverStateError("invalid takeover receipt")
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
        clean_stores = {
            backend: revision
            for backend, revision in (receipt.load() or {}).get("clean_native_stores", {}).items()
            if backend not in record["backends"]
        }
        clean_stores.update(record.get("clean_native_stores", {}))
        receipt.save({
            "version": 1,
            "phase": "complete",
            "items": record["items"],
            **({"inventory_ids": record["inventory_ids"]} if "inventory_ids" in record else {}),
            "backends": record["backends"],
            "source_ids": record["source_ids"],
            "outcome": (
                "reauth_requested"
                if (record.get("terminal") or {}).get("reason") == "reauth_requested"
                else "needs_auth" if record.get("terminal") else "success"
            ),
            "source_credentials": {
                source["id"]: source["credential_ref"]
                for source in record["updated"]["sources"]
                if source["id"] in record["source_ids"]
            },
            "clean_native_stores": clean_stores,
        })
        self.forget()

    def completed(self) -> dict[str, Any] | None:
        return NativeTakeoverJournal(self.path.with_name("last-completed.json")).load()
