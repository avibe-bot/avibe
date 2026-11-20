"""Device presentation and grouping hints. No field here confers authority."""

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


def describe(headers: Mapping[str, str]) -> dict:
    """The OAuth navigation only supplies the user agent."""
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
    kind = ("iphone_app" if native[1] == "iOS" else "desktop_app") if native else "browser"
    label = {"iphone_app": "Avibe · iPhone", "desktop_app": "Avibe Desktop"}.get(kind) or (
        f"{browser} · {system}" if system else browser
    )
    return dict(kind=kind, display_name=label, app_version=native[2] if native else None, system_version=None)


def _field(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not value.isprintable():
        raise ValueError("invalid_device_description")
    return value.strip()


def self_description(body: object) -> dict:
    fields = {"kind", "display_name", "installation_id", "aliases", "app_version", "system_version"}
    if (
        not isinstance(body, dict)
        or set(body) != fields
        or body["kind"] not in ("iphone_app", "desktop_app", "home_screen_app")
    ):
        raise ValueError("invalid_device_description")
    result = {"kind": body["kind"], "health_alias_hash": None, "schedule_alias": None}
    for name, limit in [("display_name", 120), ("installation_id", 128), ("app_version", 64), ("system_version", 64)]:
        result[name] = _field(body[name], limit)
    aliases = body["aliases"]
    if not isinstance(aliases, list) or len(aliases) > 16:
        raise ValueError("invalid_device_aliases")
    seen = set()
    for alias in aliases:
        if not isinstance(alias, dict) or set(alias) != {"source", "id"} or not isinstance(alias["source"], str):
            raise ValueError("invalid_device_aliases")
        source = alias["source"]
        if source not in {"health", "schedule"}:
            continue
        if source in seen:
            raise ValueError("invalid_device_aliases")
        seen.add(source)
        value = _field(alias["id"], 128)
        if source == "health":
            result["health_alias_hash"] = hashlib.sha256(value.encode()).hexdigest()
        else:
            result["schedule_alias"] = value
    return result
