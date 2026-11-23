"""Explicit, short-lived delivery of this device's existing credential."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlparse

from .pairing import Pairing


def websocket_address(value: str) -> tuple[str, int]:
    """The local listener is plaintext, on one concrete private IP address."""
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "?" in value or "#" in value:
        raise ValueError("invalid_device_url")
    url = urlparse(value)
    if (url.scheme != "ws" or not url.hostname or url.path != "/device/ws"
            or url.username is not None or url.password is not None or url.query or url.fragment):
        raise ValueError("invalid_device_url")
    address = ipaddress.ip_address(url.hostname)
    port = url.port if url.port is not None else 80
    if (address.version != 4 or not (address.is_private or address.is_loopback)
            or address.is_unspecified or not 1 <= port <= 65535):
        raise ValueError("invalid_device_url")
    return str(address), port


class ProvisionWindow:
    def __init__(self, pairing: Pairing, path: Path, *, bind: str, port: int, seconds: int = 120,
                 generation: int | None = None):
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
        if (websocket_address(websocket["url"]) != (str(ipaddress.ip_address(bind)), port)
                or type(websocket["version"]) is not int or websocket["version"] != 2):
            raise ValueError("provision_url_must_match_listener")
        self.pairing = pairing
        snapshot = pairing.read()
        self.device_id = snapshot.get("device_id")
        self.generation = snapshot.get("generation", 0) if generation is None else generation
        if not pairing.accepts(self.device_id, "Bearer " + websocket["token"], generation=self.generation):
            raise ValueError("provision_pairing_mismatch")
        self.document = document
        self.deadline = time.monotonic() + seconds

    def response(self, device_id: str) -> dict | None:
        if self.document is None:
            return None
        token = self.document["websocket"]["token"]
        if time.monotonic() >= self.deadline or not self.pairing.accepts(self.device_id, "Bearer " + token, generation=self.generation):
            self.close()
            return None
        return self.document if device_id == self.device_id else None

    def close(self) -> None:
        self.document = None
