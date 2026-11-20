"""Device metadata reported at sign-in. No field here confers authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Mapping


def binding_key(instance_id: str, secret: str) -> str:
    """A re-pair with different cookie credentials starts a new registry."""
    return hmac.digest(secret.encode(), b"device-registry:v1:" + instance_id.encode(), "sha256").hex()


def device_key(binding: str, subject: str, source: str, identifier: str) -> str:
    value = json.dumps([binding, subject, source, identifier], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def _field(value: str, limit: int) -> str | None:
    if not value or len(value) > limit or not value.isprintable():
        return None
    return value.strip() or None


def describe(headers: Mapping[str, str]) -> dict:
    """Native clients may report metadata on the OAuth callback; UA is a fallback."""
    ua = headers.get("User-Agent", "")[:1024]
    browser = next(
        (
            name
            for marker, name in (
                ("Edg", "Edge"),
                ("OPR/", "Opera"),
                ("FxiOS/", "Firefox"),
                ("Firefox/", "Firefox"),
                ("CriOS/", "Chrome"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if marker in ua
        ),
        "Browser",
    )
    system = next(
        (
            name
            for marker, name in (
                ("iPhone", "iOS"),
                ("iPad", "iPadOS"),
                ("Android", "Android"),
                ("Macintosh", "macOS"),
                ("Windows", "Windows"),
                ("Linux", "Linux"),
            )
            if marker in ua
        ),
        None,
    )
    native = re.search(r"\bAvibe(iOS|Desktop)/([0-9.]+)", ua)
    kind = headers.get("X-Avibe-Device-Kind", "")
    if kind not in {"iphone_app", "desktop_app", "home_screen_app"}:
        kind = ("iphone_app" if native[1] == "iOS" else "desktop_app") if native else "browser"
    label = _field(headers.get("X-Avibe-Device-Name", ""), 120) if kind != "browser" else None
    if not label:
        label = {"iphone_app": "Avibe · iPhone", "desktop_app": "Avibe Desktop"}.get(kind) or (
            f"{browser} · {system}" if system else browser
        )
    return {
        "kind": kind,
        "display_name": label,
        "installation_id": _field(headers.get("X-Avibe-Installation-ID", ""), 128)
        if kind in {"iphone_app", "desktop_app"}
        else None,
        "app_version": _field(headers.get("X-Avibe-App-Version", ""), 64) or (native[2] if native else None),
        "system_version": _field(headers.get("X-Avibe-System-Version", ""), 64),
    }
