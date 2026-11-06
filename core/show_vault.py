"""Private transport bootstrap for the Node-only Show Vault adapter.

One snapshot belongs to one child generation, outside all served workspaces.
Python verifies discovery/Windows ACLs; Node rereads the snapshot per call.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

from core.private_fs import PrivatePathError, ensure_private_directory, is_private, windows_security
from vibe import internal_client


class ShowVaultSnapshot:
    def __init__(self, runtime_dir: Path):
        self.root = runtime_dir / "server-control"
        ensure_private_directory(self.root)
        self.directory = self.root / uuid4().hex
        ensure_private_directory(self.directory)
        self.path = self.directory / "endpoint.json"
        self._lock = threading.Lock()
        self._retired = threading.Event()
        self._write(None)

    def environment(self) -> dict[str, str]:
        module = Path(__file__).resolve().parents[1] / "vibe" / "show_server" / "vault.mjs"
        return {"AVIBE_SHOW_SERVER_MODULE": module.as_uri(), "AVIBE_SHOW_CONTROL_SNAPSHOT": str(self.path)}

    def _validate_directory(self, path: Path) -> None:
        info = path.lstat()
        if not is_private(path, info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise PrivatePathError("Show server transport directory is not private")
        if not path.is_dir() or path.is_symlink():
            raise PrivatePathError("Show server transport directory is invalid")

    def refresh(self) -> None:
        # Includes resolution, not just publication: an older slow resolver must
        # never overwrite a later generation. This lock is only waited on off-loop.
        with self._lock:
            try:
                if self._retired.is_set():
                    raise OSError("Show server transport owner retired")
                self._validate_directory(self.root)
                self._validate_directory(self.directory)
                try:
                    endpoint = internal_client.resolve_control_endpoint()
                    payload = internal_client.control_endpoint_payload(endpoint)
                except (internal_client.InternalServerUnavailable, OSError):
                    payload = None
                self._write(payload)
            finally:
                if self._retired.is_set():
                    self._remove()

    def _write(self, endpoint: dict | None) -> None:
        if self._retired.is_set():
            raise OSError("Show server transport owner retired")
        if os.name == "nt":
            fd, temporary = windows_security().create_private_temporary_file(self.path)
        else:
            fd, name = tempfile.mkstemp(prefix=".endpoint-", dir=self.directory)
            temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "endpoint": endpoint}, stream, separators=(",", ":"))
            if self._retired.is_set():
                raise OSError("Show server transport owner retired")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove(self) -> None:
        # At most our one snapshot remains; publication removes its temporary
        # file before releasing the worker lock. Never remove another generation.
        self.path.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except FileNotFoundError:
            pass

    def close(self) -> None:
        self._retired.set()
        # stop() can be reached from the UI loop. Never wait there on a refresh
        # worker. The worker's finally owns cleanup if it is still active.
        if self._lock.acquire(blocking=False):
            try:
                self._remove()
            finally:
                self._lock.release()
