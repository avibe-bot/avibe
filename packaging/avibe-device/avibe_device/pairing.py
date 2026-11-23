"""One revocable device, in plugin-owned state, independent from host config."""
from __future__ import annotations

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

    def read(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict):
                raise ValueError("invalid_pairing")
            return value
        except FileNotFoundError:
            return {"enabled": False}

    def write(self, value: dict) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(value, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def pair(self, device_id: str, *, name: str = "Vibey") -> str:
        if self.read().get("device_id"):
            raise ValueError("revoke_existing_device_first")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", device_id) or not name.strip() or len(name) > 96:
            raise ValueError("invalid_device_id")
        token = secrets.token_urlsafe(32)
        self.write({"enabled": False, "device_id": device_id, "name": name,
                    "digest": hashlib.sha256(token.encode()).hexdigest()})
        return token

    def accepts(self, device_id: str, authorization: str) -> bool:
        row = self.read()
        if not row.get("enabled") or row.get("device_id") != device_id:
            return False
        if not authorization.startswith("Bearer "):
            return False
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        return hmac.compare_digest(digest, row.get("digest", ""))

    def revoke(self) -> None:
        self.write({"enabled": False})

    def enable(self, enabled: bool) -> None:
        row = self.read()
        if enabled and not row.get("device_id"):
            raise ValueError("pair_device_first")
        self.write({**row, "enabled": enabled})

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
