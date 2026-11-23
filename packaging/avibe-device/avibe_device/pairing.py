"""One revocable device, in plugin-owned state, independent from host config."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import re
from pathlib import Path


class Pairing:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "device.json"

    @contextmanager
    def _locked(self):
        import fcntl

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / "state.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._recover()
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict):
                raise ValueError("invalid_pairing")
            return value
        except FileNotFoundError:
            return {"enabled": False, "generation": 0}

    @staticmethod
    def _sync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, value: dict) -> None:
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(value, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
            self._sync_directory(self.root)
        finally:
            Path(name).unlink(missing_ok=True)

    def _recover(self) -> None:
        """Under the state lock, abort unpublished pairs or finish committed cleanup."""
        row = self._read()
        pending = row.get("pending_provision")
        if pending:
            stage, artifact = Path(pending["stage"]), Path(pending["artifact"])
            # The staging hard link identifies exactly the artifact this attempt
            # published. Never remove a pre-existing or replaced destination.
            try:
                ours = stage.samefile(artifact)
            except FileNotFoundError:
                ours = False
            if ours:
                artifact.unlink()
            had_stage = stage.exists()
            stage.unlink(missing_ok=True)
            if ours or had_stage:
                self._sync_directory(artifact.parent)
            self._write({k: v for k, v in row.items() if k != "pending_provision"})
        elif row.get("provision_stage"):
            stage = Path(row["provision_stage"])
            stage.unlink(missing_ok=True)
            self._sync_directory(stage.parent)
            self._write({k: v for k, v in row.items() if k != "provision_stage"})

    def read(self) -> dict:
        with self._locked():
            return self._read()

    def pair(self, device_id: str, *, name: str = "Vibey", provision_file: Path | None = None,
             websocket_url: str | None = None) -> str:
        with self._locked():
            row = self._read()
            artifact = provision_file.absolute() if provision_file is not None else None
            if row.get("device_id"):
                # A failure after the atomic state commit is already a complete
                # pair. Retrying the same command succeeds without rotating it.
                if (artifact is not None and row.get("provision_file") == str(artifact)
                        and row["device_id"] == device_id and row.get("name") == name):
                    document = json.loads(artifact.read_text())
                    ws = document.get("websocket", {}) if isinstance(document, dict) else {}
                    if not isinstance(ws, dict):
                        raise ValueError("invalid_provision_file")
                    token = ws.get("token", "")
                    if (ws.get("url") == websocket_url and ws.get("version") == 2
                            and isinstance(token, str) and self._matches(row, device_id, "Bearer " + token)):
                        return token
                raise ValueError("revoke_existing_device_first")
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", device_id) or not name.strip() or len(name) > 96:
                raise ValueError("invalid_device_id")
            token = secrets.token_urlsafe(32)
            paired = {"enabled": False, "device_id": device_id, "name": name,
                      "generation": row.get("generation", 0),
                      "digest": hashlib.sha256(token.encode()).hexdigest()}
            if artifact is None:
                self._write(paired)
                return token
            if os.path.lexists(artifact):
                raise FileExistsError("provision_file_exists")
            stage = artifact.parent / (".avibe-provision-" + secrets.token_hex(16))
            # Persist the abort record before creating either token-bearing file.
            self._write({**row, "pending_provision": {"stage": str(stage), "artifact": str(artifact)}})
            try:
                fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump({"websocket": {"url": websocket_url, "token": token, "version": 2}}, output)
                    output.flush()
                    os.fsync(output.fileno())
                # link is an atomic no-overwrite publication (O_EXCL semantics).
                # Keep the staging link until state commits, for crash recovery.
                os.link(stage, artifact)
                self._sync_directory(artifact.parent)
                self._write({**paired, "provision_file": str(artifact), "provision_stage": str(stage)})
            finally:
                self._recover()
            return token

    @staticmethod
    def _matches(row: dict, device_id: str, authorization: str) -> bool:
        if row.get("device_id") != device_id or not authorization.startswith("Bearer "):
            return False
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        return hmac.compare_digest(digest, row.get("digest", ""))

    def active(self, generation: int) -> bool:
        row = self.read()
        return bool(row.get("enabled") and row.get("generation", 0) == generation)

    def accepts(self, device_id: str, authorization: str, *, generation: int | None = None) -> bool:
        row = self.read()
        return bool(row.get("enabled") and (generation is None or row.get("generation", 0) == generation)
                    and self._matches(row, device_id, authorization))

    def revoke(self) -> None:
        with self._locked():
            row = self._read()
            self._write({"enabled": False, "generation": row.get("generation", 0) + 1})

    def enable(self, enabled: bool) -> None:
        with self._locked():
            row = self._read()
            if enabled and not row.get("device_id"):
                raise ValueError("pair_device_first")
            self._write({**row, "enabled": enabled,
                         "generation": row.get("generation", 0) + (not enabled)})

    def status(self) -> dict:
        row = self.read()
        return {"enabled": row.get("enabled", False), "paired": bool(row.get("device_id")),
                "device_id": row.get("device_id"), "name": row.get("name", "Vibey"),
                "running": self.running()}

    def running(self) -> bool:
        if os.name == "nt":
            return False
        import fcntl

        try:
            with (self.root / "serve.lock").open("rb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        return False
