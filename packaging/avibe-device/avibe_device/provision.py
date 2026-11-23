"""Explicit, short-lived delivery of this device's existing credential."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlparse

from .pairing import Pairing


class ProvisionWindow:
    def __init__(self, pairing: Pairing, path: Path, *, bind: str, port: int, seconds: int = 120):
        if not 1 <= seconds <= 300:
            raise ValueError("invalid_provision_window")
        # Read only an explicitly selected owner-private regular file. No
        # persistent plaintext token is added to the pairing store.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 4096):
                raise ValueError("invalid_provision_file")
            document = json.load(source)
        if not isinstance(document, dict) or set(document) != {"websocket"}:
            raise ValueError("invalid_provision_file")
        websocket = document["websocket"]
        if not isinstance(websocket, dict) or set(websocket) != {"url", "token", "version"}:
            raise ValueError("invalid_provision_file")
        if not isinstance(websocket["url"], str) or not isinstance(websocket["token"], str):
            raise ValueError("invalid_provision_file")
        url = urlparse(websocket["url"])
        if (url.scheme != "ws" or url.hostname != bind or (url.port or 80) != port
                or url.path != "/device/ws" or url.username is not None or url.password is not None
                or url.query or url.fragment or type(websocket["version"]) is not int or websocket["version"] != 2):
            raise ValueError("provision_url_must_match_listener")
        self.pairing = pairing
        self.device_id = pairing.read().get("device_id")
        if not pairing.accepts(self.device_id, "Bearer " + websocket["token"]):
            raise ValueError("provision_pairing_mismatch")
        self.document = document
        self.deadline = time.monotonic() + seconds

    def response(self, device_id: str) -> dict | None:
        if self.document is None:
            return None
        token = self.document["websocket"]["token"]
        if time.monotonic() >= self.deadline or not self.pairing.accepts(self.device_id, "Bearer " + token):
            self.close()
            return None
        return self.document if device_id == self.device_id else None

    def close(self) -> None:
        self.document = None
