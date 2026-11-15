"""Source-owned capability registry for the COMPLETE packaged Vault sandbox pin.

No environment, database, API, version guess or network response can add support.
Resource fetching/integrity and ready/handshake checks remain browser-owned.
Adding a reviewed build here is a separate exact-pin release change.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

_TYPES = ("p2pkh", "p2wpkh", "p2tr")
_PIN_KEYS = {"schema", "algorithm", "sandboxOrigin", "sandboxVersion", "buildHash", "resources", "manifestHash"}
_SUPPORT_KEYS = {"ordinary_input_types", "hd_input_types", "ecdsa_return_types"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SRI = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")

# fc013b7 is the currently packaged ordinary-only legacy signing cohort. It
# implements protected Taproot, not protected HD or the new ECDSA input kinds.
# The full pin digest was computed using the existing lossless C codec; changing
# ANY resource, version, origin, build or manifest is a different registry key.
CAPABILITIES_BY_PIN_ID = MappingProxyType({
    "7f320bc014c0a2ee7d65f9c48c8b91b7e0c0fcc0effbfb3a36d3a3c30fa7c08b": MappingProxyType({
        "ordinary_input_types": ("p2tr",),
        "hd_input_types": (),
        "ecdsa_return_types": (),
    }),
})


@dataclass(frozen=True)
class PinSupport:
    pin_id: str
    build_hash: str
    manifest_hash: str
    ordinary_input_types: tuple[str, ...]
    hd_input_types: tuple[str, ...]
    ecdsa_return_types: tuple[str, ...]

    @property
    def cohort(self) -> dict[str, str]:
        return {"pin_id": self.pin_id, "buildHash": self.build_hash, "manifestHash": self.manifest_hash}


def _sri(value: Any) -> bool:
    if type(value) is not str or _SRI.fullmatch(value) is None:
        return False
    try:
        raw = base64.b64decode(value[7:], validate=True)
    except ValueError:
        return False
    return len(raw) == 32 and base64.b64encode(raw).decode("ascii") == value[7:]


def _valid_pin(pin: Any) -> bool:
    if type(pin) is not dict or set(pin) != _PIN_KEYS:
        return False
    if type(pin["schema"]) is not int or pin["schema"] != 1 or pin["algorithm"] != "sha256":
        return False
    if pin["sandboxOrigin"] != "https://sandbox.avibe.bot" or type(pin["sandboxVersion"]) is not str or not 1 <= len(pin["sandboxVersion"]) <= 80:
        return False
    build = pin["buildHash"]
    if type(build) is not str or _HEX64.fullmatch(build) is None or not _sri(pin["manifestHash"]):
        return False
    resources = pin["resources"]
    prefix = f"/v/{build}/"
    return (
        type(resources) is dict and 1 <= len(resources) <= 128 and prefix + "index.html" in resources
        and all(type(path) is str and path.startswith(prefix) and _sri(sri) for path, sri in resources.items())
    )


def _valid_support(value: Any) -> bool:
    if not hasattr(value, "keys") or set(value) != _SUPPORT_KEYS:
        return False
    for key in _SUPPORT_KEYS:
        types = value[key]
        if type(types) is not tuple or types != tuple(t for t in _TYPES if t in types):
            return False
    for key in ("ordinary_input_types", "hd_input_types"):
        if any(t in value[key] for t in ("p2pkh", "p2wpkh")) and (
            value[key] != _TYPES or value["ecdsa_return_types"] != _TYPES
        ):
            return False
    return True


def load_packaged_pin() -> dict | None:
    # One bundled package member, no configurable path or fallback.
    path = Path(__file__).parent / "data" / "vault_sandbox_release.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            return None
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return None


def support_for_pin(pin: Any) -> PinSupport | None:
    if not _valid_pin(pin):
        return None
    # Local import avoids making policy target derivation a module-import cycle.
    from storage import vault_policy

    try:
        pin_id = hashlib.sha256(vault_policy.canonical_json(pin).encode("utf-8")).hexdigest()
    except vault_policy.PolicyContextError:
        return None
    value = CAPABILITIES_BY_PIN_ID.get(pin_id)
    if not _valid_support(value):
        return None
    return PinSupport(pin_id, pin["buildHash"], pin["manifestHash"], **value)


def current_support() -> PinSupport | None:
    return support_for_pin(load_packaged_pin())
