"""Read and mutate Codex and Claude Code native OAuth stores.

The migration scanner calls :func:`read_native_oauth` without
``allow_secret``. That path may read fixture files, or safe Keychain
attributes, but it never reads a Keychain value. The caller must perform the
user confirmation and native admission drain before calling the secret-bearing
path.

``keychain_edit`` is deliberately private data. It contains the exact
before/after credential states needed for a compare-and-swap cleanup or
reverse, so callers must keep it in the migration journal and never put it on
the scan response.

Claude selection covers its Keychain and both supported credential filenames
under the same secure root. Newest-layout files take precedence over legacy
files, but every credential-bearing copy must have same-account evidence.
The selection revision binds all file states, including absence. Journal file
operations include unchanged states as guards; callers must retain them when
coalescing edits so a newly appeared fallback login cannot escape cleanup.

Metadata scans use ``kSecUseAuthenticationUIFail`` and temporarily disable the
legacy process-wide Keychain interaction switch. The explicit-consent
``allow_secret`` read and subsequent mutations use
``kSecUseAuthenticationUIAllow`` so the OS can complete its normal approval
flow. Fixture homes never select the real OS Keychain.

Mutations require the caller's cooperative credential lease and external CLI
drain. They recheck immediately, bind the observed item and modification date,
and read back. Apple's SecItem API is not an atomic value-CAS against an
uncooperative writer updating the same item inside its query/update window.
Recovery belongs to the durable controller journal, not this module.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import getpass
import hmac
import hashlib
import json
import math
import os
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

from config.atomic_io import write_atomic
from vibe.codex_config import get_codex_home

__all__ = [
    "NativeOAuthError",
    "NativeOAuthPermissionError",
    "NativeOAuthRevisionError",
    "NativeOAuthSnapshot",
    "apply_keychain_edit",
    "check_keychain_edit",
    "native_credentials_paths",
    "read_native_oauth",
]

_CODEX_STORES = frozenset({"file", "keyring", "auto", "ephemeral"})
_CLAUDE_OAUTH_KEYS = frozenset(
    {
        "access_token",
        "accessToken",
        "refresh_token",
        "refreshToken",
    }
)
_CLAUDE_OAUTH_METADATA_KEYS = frozenset(
    {
        "expiresAt",
        "expires_at",
        "scopes",
        "subscriptionType",
        "rateLimitTier",
        "tokenType",
        "accountUuid",
        "account_uuid",
        "organizationUuid",
        "organization_uuid",
    }
)
_CLAUDE_IDENTITY_KEYS = {
    "accountUuid": "account_uuid",
    "account_uuid": "account_uuid",
    "accountId": "account_id",
    "account_id": "account_id",
    "email": "email",
    "emailAddress": "email",
    "accountEmail": "email",
    "organizationUuid": "organization_uuid",
    "organization_uuid": "organization_uuid",
    "organizationId": "organization_id",
    "organization_id": "organization_id",
}
_SAFE_CLAUDE_ACCOUNT = re.compile(r"^[A-Za-z0-9._-]+$")


class NativeOAuthError(RuntimeError):
    """Base class for native OAuth store failures."""


class NativeOAuthPermissionError(NativeOAuthError):
    """The native store denied a read or mutation."""


class NativeOAuthRevisionError(NativeOAuthError):
    """The live native store no longer matches the migration journal."""


@dataclass(frozen=True)
class NativeOAuthSnapshot:
    """A native OAuth candidate and its stable selection revision."""

    backend: str
    revision: str
    payload: dict[str, Any] | None = field(default=None, repr=False)
    exportable: bool = False
    keychain_edit: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _KeychainMetadata:
    state: str
    revision: str
    attributes: dict[str, Any] = field(default_factory=dict)
    persistent_reference: str | None = None
    modification_date: float | None = None


@dataclass(frozen=True)
class _KeychainRead:
    value: str = field(repr=False)
    revision: str
    persistent_reference: str | None = None
    modification_date: float | None = None


class _KeychainStore(Protocol):
    def metadata(self, service: str, account: str) -> _KeychainMetadata:
        ...

    def read(
        self,
        service: str,
        account: str,
        *,
        expected: _KeychainRead | None = None,
    ) -> _KeychainRead:
        ...

    def write(
        self,
        service: str,
        account: str,
        value: str,
        *,
        expected: _KeychainRead | None,
    ) -> None:
        ...

    def delete(
        self,
        service: str,
        account: str,
        *,
        expected: _KeychainRead,
    ) -> None:
        ...


class _KeychainNotFound(NativeOAuthError):
    pass


class _KeychainUnavailable(NativeOAuthError):
    pass


class _KeychainDenied(NativeOAuthPermissionError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _locator_revision(backend: str, kind: str, locator: str, detail: str = "") -> str:
    material = "\x00".join((backend, kind, locator, detail))
    return f"{backend}:{kind}:{_sha256(material.encode('utf-8'))}"


def _path_revision(backend: str, path: Path) -> str:
    try:
        locator = str(path.resolve())
    except OSError:
        locator = str(path.absolute())
    return _locator_revision(backend, "file", locator)


def _keychain_revision(backend: str, service: str, account: str, native_revision: str) -> str:
    return _locator_revision(backend, "keychain", f"{service}\x00{account}", native_revision)


def _read_toml(path: Path) -> tuple[str, dict[str, Any] | None]:
    try:
        try:
            import tomllib  # type: ignore[attr-defined]
        except ImportError:  # pragma: no cover - Python 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", {}
    except (OSError, UnicodeError):
        return "permission_needed", None
    except (ValueError, TypeError):
        return "invalid", None
    return ("ok", value) if isinstance(value, dict) else ("invalid", None)


def _read_json_file(path: Path) -> tuple[str, dict[str, Any] | None, str | None]:
    """Return ``(status, payload, raw)`` without exposing errors."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None, None
    except (OSError, UnicodeError):
        return "permission_needed", None, None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "invalid", None, raw
    if not isinstance(payload, dict):
        return "invalid", None, raw
    return "ok", payload, raw


def _json_file_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"exists": False}
    except (OSError, UnicodeError) as exc:
        raise NativeOAuthPermissionError("native credential file could not be read") from exc
    return {"exists": True, "raw": raw}


def _json_file_state_from_raw(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {"exists": False}
    return {"exists": True, "raw": raw}


def _json_after_state(payload: dict[str, Any], original_raw: str) -> dict[str, Any]:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if original_raw.endswith("\n"):
        rendered += "\n"
    return {"exists": True, "raw": rendered}


def _placeholder(
    backend: str,
    revision: str,
    status: str,
    *,
    store: str,
    service: str | None = None,
    account: str | None = None,
) -> NativeOAuthSnapshot:
    payload: dict[str, Any] = {"status": status, "store": store}
    if service is not None:
        payload["service"] = service
    if account is not None:
        payload["account"] = account
    return NativeOAuthSnapshot(backend=backend, revision=revision, payload=payload)


def _has_nonempty_string(mapping: Mapping[str, Any], keys: set[str] | frozenset[str]) -> bool:
    return any(isinstance(mapping.get(key), str) and bool(mapping[key].strip()) for key in keys)


def _codex_has_oauth(payload: Mapping[str, Any]) -> bool:
    tokens = payload.get("tokens")
    return isinstance(tokens, dict) and _has_nonempty_string(
        tokens,
        {"access_token", "refresh_token", "id_token"},
    )


def _codex_has_credential(payload: Mapping[str, Any]) -> bool:
    return _codex_has_oauth(payload) or (
        isinstance(payload.get("OPENAI_API_KEY"), str)
        and bool(payload["OPENAI_API_KEY"].strip())
    )


def _claude_oauth_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    nested = payload.get("claudeAiOauth")
    if isinstance(nested, dict) and _has_nonempty_string(nested, _CLAUDE_OAUTH_KEYS):
        return nested
    if _has_nonempty_string(payload, _CLAUDE_OAUTH_KEYS):
        return dict(payload)
    return None


def _claude_identity(payload: Mapping[str, Any]) -> dict[str, set[str]]:
    nested = payload.get("claudeAiOauth")
    sources: list[Mapping[str, Any]] = [payload]
    if isinstance(nested, dict):
        sources.insert(0, nested)
    identity: dict[str, set[str]] = {}
    for source in sources:
        for key, canonical in _CLAUDE_IDENTITY_KEYS.items():
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                identity.setdefault(canonical, set()).add(value.strip())
    return identity


def _claude_same_account(
    primary: Mapping[str, Any],
    fallback: Mapping[str, Any],
) -> bool:
    primary_identity = _claude_identity(primary)
    fallback_identity = _claude_identity(fallback)
    if any(
        len(values) != 1
        for identity in (primary_identity, fallback_identity)
        for values in identity.values()
    ):
        return False
    shared_keys = primary_identity.keys() & fallback_identity.keys()
    if any(primary_identity[key] != fallback_identity[key] for key in shared_keys):
        return False
    # An organization can contain multiple accounts. Its identity can disprove
    # compatibility, but cannot prove that two different grants share an owner.
    if shared_keys & {"account_uuid", "account_id", "email"}:
        return True

    def refresh_grant(payload: Mapping[str, Any]) -> str | None:
        nested = payload.get("claudeAiOauth")
        sources: list[Mapping[str, Any]] = [payload]
        if isinstance(nested, dict):
            sources.insert(0, nested)
        for source in sources:
            for key in ("refresh_token", "refreshToken"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return None

    primary_refresh = refresh_grant(primary)
    fallback_refresh = refresh_grant(fallback)
    return bool(
        primary_refresh
        and fallback_refresh
        and hmac.compare_digest(primary_refresh.encode("utf-8"), fallback_refresh.encode("utf-8"))
    )


def _remove_codex_oauth(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("tokens", None)
    result.pop("last_refresh", None)
    result.pop("OPENAI_API_KEY", None)
    if result.get("auth_mode") == "chatgpt":
        result.pop("auth_mode", None)
    return result


def _remove_claude_oauth(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if isinstance(result.get("claudeAiOauth"), dict):
        result.pop("claudeAiOauth", None)
        return result
    for key in _CLAUDE_OAUTH_KEYS | _CLAUDE_OAUTH_METADATA_KEYS:
        result.pop(key, None)
    return result


def _state_for_json_payload(payload: dict[str, Any], original_raw: str) -> dict[str, Any]:
    if not payload:
        return {"exists": False}
    return _json_after_state(payload, original_raw)


def _edit(
    backend: str,
    revision: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "version": 1,
        "backend": backend,
        "selection_revision": revision,
        "operations": operations,
    }


def _file_operation(
    path: Path,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "file",
        "path": str(path),
        "before": before,
        "after": after,
    }


def _keychain_operation(
    service: str,
    account: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    isolated: bool,
    persistent_reference: str | None = None,
    modification_date: float | None = None,
) -> dict[str, Any]:
    operation = {
        "kind": "keychain",
        "service": service,
        "account": account,
        "before": before,
        "after": after,
        "store_scope": "fixture" if isolated else "native",
    }
    if persistent_reference is not None:
        operation["persistent_reference"] = persistent_reference
    if modification_date is not None:
        operation["modification_date"] = modification_date
    return operation


def _codex_home(home: Path | None) -> Path:
    return get_codex_home(home)


def native_credentials_paths(
    backend: str,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Return all supported native credential files, in precedence order."""

    normalized = str(backend or "").strip().lower()
    if normalized == "codex":
        return (_codex_home(home) / "auth.json",)
    if normalized == "claude":
        secure_root = _claude_locator(home)[0]
        return (
            secure_root / ".credentials.json",
            secure_root / "credentials.json",
        )
    return ()


def _claude_locator(home: Path | None) -> tuple[Path, str, str]:
    """Return ``(secure_root, service, account)`` for Claude Code 2.1.273."""

    if home is not None:
        secure_root = (Path(home).expanduser().resolve() / ".claude").resolve()
        suffix = ""
    else:
        secure_env_defined = "CLAUDE_SECURESTORAGE_CONFIG_DIR" in os.environ
        secure_env = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
        if secure_env_defined:
            secure_root = (
                Path(secure_env).expanduser().resolve()
                if secure_env
                else (Path.home() / ".claude").resolve()
            )
            suffix = (
                ""
                if not secure_env or "CLAUDE_CONFIG_DIR" not in os.environ
                else f"-{_sha256(str(secure_root).encode('utf-8'))[:8]}"
            )
        else:
            config_defined = "CLAUDE_CONFIG_DIR" in os.environ
            config_env = os.environ.get("CLAUDE_CONFIG_DIR", "")
            secure_root = (
                Path(config_env).expanduser().resolve()
                if config_env
                else (Path.home() / ".claude").resolve()
            )
            suffix = (
                ""
                if not config_defined
                else f"-{_sha256(str(secure_root).encode('utf-8'))[:8]}"
            )

    raw_user = os.environ.get("USER") or ""
    if not raw_user:
        try:
            raw_user = os.userInfo().username  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            try:
                raw_user = getpass.getuser()
            except OSError:
                raw_user = ""
    account = raw_user if _SAFE_CLAUDE_ACCOUNT.fullmatch(raw_user) else "claude-code-user"
    return secure_root, f"Claude Code-credentials{suffix}", account


def _codex_store_config(codex_home: Path) -> tuple[str, str | None]:
    config_status, config = _read_toml(codex_home / "config.toml")
    if config_status in {"permission_needed", "invalid"} or config is None:
        return "unsupported", f"{config_status}_config"
    if _secret_auth_storage_enabled(config):
        return "unsupported", "secret_auth_storage"
    raw_store = config.get("cli_auth_credentials_store", "file")
    if not isinstance(raw_store, str):
        return "unsupported", "invalid_store"
    store = raw_store.strip().lower()
    if store not in _CODEX_STORES:
        return "unsupported", "unknown_store"
    return store, None


def _secret_auth_storage_enabled(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized == "secret_auth_storage":
                if isinstance(child, bool):
                    if child:
                        return True
                elif child is not None and str(child).strip().lower() not in {
                    "",
                    "false",
                    "off",
                    "disabled",
                    "none",
                }:
                    return True
            if normalized in {"auth_keyring_backend", "keyring_backend"} and str(child).strip().lower() in {
                "secret",
                "secrets",
                "managed",
            }:
                return True
            if _secret_auth_storage_enabled(child):
                return True
    return False


_CFIndex = ctypes.c_long
_CFTypeRef = ctypes.c_void_p
_CFStringEncoding = ctypes.c_uint32
_CFDictionaryCallback = ctypes.c_void_p


class _CFDictionaryKeyCallbacks(ctypes.Structure):
    _fields_ = [
        ("version", _CFIndex),
        ("retain", _CFDictionaryCallback),
        ("release", _CFDictionaryCallback),
        ("copy_description", _CFDictionaryCallback),
        ("equal", _CFDictionaryCallback),
        ("hash", _CFDictionaryCallback),
    ]


class _CFDictionaryValueCallbacks(ctypes.Structure):
    _fields_ = [
        ("version", _CFIndex),
        ("retain", _CFDictionaryCallback),
        ("release", _CFDictionaryCallback),
        ("copy_description", _CFDictionaryCallback),
        ("equal", _CFDictionaryCallback),
    ]


class _CFArrayCallbacks(ctypes.Structure):
    _fields_ = _CFDictionaryValueCallbacks._fields_


class _SecurityBindings:
    """SDK-typed bindings; construction loads symbols but calls no Sec* API.

    ABI reference: Apple's CFString.h, CFDictionary.h, CFArray.h, SecItem.h
    and SecKeychain.h. Opaque handles are Python integers (or None for NULL),
    never c_void_p objects used as Python dictionary keys.
    """

    def __init__(self, security: Any, core_foundation: Any) -> None:
        self.security = security
        self.cf = core_foundation
        self.constants = {
            name: ctypes.c_void_p.in_dll(library, name).value
            for library, names in (
                (
                    core_foundation,
                    (
                        "kCFAllocatorDefault",
                        "kCFBooleanFalse",
                        "kCFBooleanTrue",
                    ),
                ),
                (
                    security,
                    (
                        "kSecAttrAccount",
                        "kSecAttrCreationDate",
                        "kSecAttrModificationDate",
                        "kSecAttrService",
                        "kSecClass",
                        "kSecClassGenericPassword",
                        "kSecMatchLimit",
                        "kSecMatchLimitOne",
                        "kSecMatchItemList",
                        "kSecReturnAttributes",
                        "kSecReturnData",
                        "kSecReturnPersistentRef",
                        "kSecUseAuthenticationUI",
                        "kSecUseAuthenticationUIAllow",
                        "kSecUseAuthenticationUIFail",
                        "kSecValuePersistentRef",
                        "kSecValueData",
                    ),
                ),
            )
            for name in names
        }
        self.key_callbacks = _CFDictionaryKeyCallbacks.in_dll(
            core_foundation,
            "kCFTypeDictionaryKeyCallBacks",
        )
        self.value_callbacks = _CFDictionaryValueCallbacks.in_dll(
            core_foundation,
            "kCFTypeDictionaryValueCallBacks",
        )
        self.array_callbacks = _CFArrayCallbacks.in_dll(core_foundation, "kCFTypeArrayCallBacks")
        self._configure()

    def _configure(self) -> None:
        cf = self.cf
        cf.CFRelease.argtypes = [_CFTypeRef]
        cf.CFRelease.restype = None
        cf.CFGetTypeID.argtypes = [_CFTypeRef]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFStringCreateWithBytes.argtypes = [
            _CFTypeRef,
            ctypes.POINTER(ctypes.c_ubyte),
            _CFIndex,
            _CFStringEncoding,
            ctypes.c_ubyte,
        ]
        cf.CFStringCreateWithBytes.restype = _CFTypeRef
        cf.CFStringGetTypeID.argtypes = []
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetLength.argtypes = [_CFTypeRef]
        cf.CFStringGetLength.restype = _CFIndex
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            _CFIndex,
            _CFStringEncoding,
        ]
        cf.CFStringGetMaximumSizeForEncoding.restype = _CFIndex
        cf.CFStringGetCString.argtypes = [
            _CFTypeRef,
            ctypes.c_char_p,
            _CFIndex,
            _CFStringEncoding,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFDataGetTypeID.argtypes = []
        cf.CFDataGetTypeID.restype = ctypes.c_ulong
        cf.CFDataGetLength.argtypes = [_CFTypeRef]
        cf.CFDataGetLength.restype = _CFIndex
        cf.CFDataGetBytePtr.argtypes = [_CFTypeRef]
        cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
        cf.CFDataCreate.argtypes = [
            _CFTypeRef,
            ctypes.POINTER(ctypes.c_ubyte),
            _CFIndex,
        ]
        cf.CFDataCreate.restype = _CFTypeRef
        cf.CFDateGetTypeID.argtypes = []
        cf.CFDateGetTypeID.restype = ctypes.c_ulong
        cf.CFDateGetAbsoluteTime.argtypes = [_CFTypeRef]
        cf.CFDateGetAbsoluteTime.restype = ctypes.c_double
        cf.CFDateCreate.argtypes = [_CFTypeRef, ctypes.c_double]
        cf.CFDateCreate.restype = _CFTypeRef
        cf.CFNumberGetTypeID.argtypes = []
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetValue.argtypes = [
            _CFTypeRef,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetTypeID.argtypes = []
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetValue.argtypes = [_CFTypeRef]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFDictionaryCreateMutable.argtypes = [
            _CFTypeRef,
            _CFIndex,
            ctypes.POINTER(_CFDictionaryKeyCallbacks),
            ctypes.POINTER(_CFDictionaryValueCallbacks),
        ]
        cf.CFDictionaryCreateMutable.restype = _CFTypeRef
        cf.CFDictionarySetValue.argtypes = [
            _CFTypeRef,
            _CFTypeRef,
            _CFTypeRef,
        ]
        cf.CFDictionarySetValue.restype = None
        cf.CFDictionaryGetValue.argtypes = [_CFTypeRef, _CFTypeRef]
        cf.CFDictionaryGetValue.restype = _CFTypeRef
        cf.CFDictionaryGetTypeID.argtypes = []
        cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayCreate.argtypes = [
            _CFTypeRef, ctypes.POINTER(_CFTypeRef), _CFIndex,
            ctypes.POINTER(_CFArrayCallbacks),
        ]
        cf.CFArrayCreate.restype = _CFTypeRef
        cf.CFArrayGetCount.argtypes = [_CFTypeRef]
        cf.CFArrayGetCount.restype = _CFIndex
        cf.CFArrayGetValueAtIndex.argtypes = [_CFTypeRef, _CFIndex]
        cf.CFArrayGetValueAtIndex.restype = _CFTypeRef
        self.security.SecItemCopyMatching.argtypes = [
            _CFTypeRef,
            ctypes.POINTER(_CFTypeRef),
        ]
        self.security.SecItemCopyMatching.restype = ctypes.c_int32
        self.security.SecItemAdd.argtypes = [_CFTypeRef, ctypes.POINTER(_CFTypeRef)]
        self.security.SecItemAdd.restype = ctypes.c_int32
        self.security.SecItemUpdate.argtypes = [_CFTypeRef, _CFTypeRef]
        self.security.SecItemUpdate.restype = ctypes.c_int32
        self.security.SecItemDelete.argtypes = [_CFTypeRef]
        self.security.SecItemDelete.restype = ctypes.c_int32
        self.security.SecKeychainGetUserInteractionAllowed.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self.security.SecKeychainGetUserInteractionAllowed.restype = ctypes.c_int32
        self.security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        self.security.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32

    def constant(self, name: str) -> int | None:
        return self.constants[name]


_SECURITY_BINDINGS: _SecurityBindings | None = None
_ERR_SEC_SUCCESS = 0
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_DUPLICATE_ITEM = -25299
_UTF8 = 0x08000100
_CF_NUMBER_DOUBLE = 6
_KEYCHAIN_INTERACTION_LOCK = threading.RLock()


def _security_bindings() -> _SecurityBindings:
    global _SECURITY_BINDINGS
    if _SECURITY_BINDINGS is not None:
        return _SECURITY_BINDINGS
    if sys.platform != "darwin":
        raise _KeychainUnavailable("native keychain is unavailable")
    try:
        security_path = ctypes.util.find_library("Security") or (
            "/System/Library/Frameworks/Security.framework/Security"
        )
        foundation_path = ctypes.util.find_library("CoreFoundation") or (
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        security = ctypes.CDLL(security_path)
        core_foundation = ctypes.CDLL(foundation_path)
        _SECURITY_BINDINGS = _SecurityBindings(security, core_foundation)
    except (OSError, AttributeError, KeyError) as exc:
        raise _KeychainUnavailable("native keychain is unavailable") from exc
    return _SECURITY_BINDINGS


def _cf_string(bindings: _SecurityBindings, value: str) -> _CFTypeRef:
    raw = value.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    result = bindings.cf.CFStringCreateWithBytes(
        bindings.constant("kCFAllocatorDefault"),
        buffer,
        len(raw),
        _UTF8,
        False,
    )
    if not result:
        raise _KeychainUnavailable("native keychain string allocation failed")
    return result.value if isinstance(result, ctypes.c_void_p) else result


def _cf_data(bindings: _SecurityBindings, value: str) -> _CFTypeRef:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    result = bindings.cf.CFDataCreate(
        bindings.constant("kCFAllocatorDefault"),
        buffer,
        len(raw),
    )
    if not result:
        raise _KeychainUnavailable("native keychain data allocation failed")
    return result.value if isinstance(result, ctypes.c_void_p) else result


def _cf_dictionary(
    bindings: _SecurityBindings,
    values: Mapping[_CFTypeRef, _CFTypeRef],
) -> _CFTypeRef:
    dictionary = bindings.cf.CFDictionaryCreateMutable(
        bindings.constant("kCFAllocatorDefault"),
        0,
        ctypes.byref(bindings.key_callbacks),
        ctypes.byref(bindings.value_callbacks),
    )
    if not dictionary:
        raise _KeychainUnavailable("native keychain query allocation failed")
    try:
        for key, value in values.items():
            bindings.cf.CFDictionarySetValue(dictionary, key, value)
    except BaseException:
        bindings.cf.CFRelease(dictionary)
        raise
    return dictionary.value if isinstance(dictionary, ctypes.c_void_p) else dictionary


def _release(bindings: _SecurityBindings, *objects: _CFTypeRef) -> None:
    for value in objects:
        if value:
            bindings.cf.CFRelease(value)


def _cf_string_value(bindings: _SecurityBindings, value: _CFTypeRef) -> str:
    if not value or bindings.cf.CFGetTypeID(value) != bindings.cf.CFStringGetTypeID():
        raise _KeychainUnavailable("native keychain returned invalid attributes")
    length = bindings.cf.CFStringGetLength(value)
    maximum = bindings.cf.CFStringGetMaximumSizeForEncoding(length, _UTF8) + 1
    buffer = ctypes.create_string_buffer(maximum)
    if not bindings.cf.CFStringGetCString(value, buffer, maximum, _UTF8):
        raise _KeychainUnavailable("native keychain returned invalid attributes")
    return buffer.value.decode("utf-8")


def _cf_safe_value(bindings: _SecurityBindings, value: _CFTypeRef) -> Any:
    type_id = bindings.cf.CFGetTypeID(value)
    if type_id == bindings.cf.CFStringGetTypeID():
        return _cf_string_value(bindings, value)
    if type_id == bindings.cf.CFDateGetTypeID():
        return bindings.cf.CFDateGetAbsoluteTime(value)
    if type_id == bindings.cf.CFNumberGetTypeID():
        number = ctypes.c_double()
        if bindings.cf.CFNumberGetValue(value, _CF_NUMBER_DOUBLE, ctypes.byref(number)):
            return number.value
    if type_id == bindings.cf.CFBooleanGetTypeID():
        return bool(bindings.cf.CFBooleanGetValue(value))
    if type_id == bindings.cf.CFDataGetTypeID():
        length = bindings.cf.CFDataGetLength(value)
        pointer = bindings.cf.CFDataGetBytePtr(value)
        raw = bytes(pointer[:length]) if pointer and length else b""
        return f"sha256:{_sha256(raw)}"
    return f"cf-type-{type_id}"


def _cf_data_bytes(bindings: _SecurityBindings, value: _CFTypeRef) -> bytes:
    if not value or bindings.cf.CFGetTypeID(value) != bindings.cf.CFDataGetTypeID():
        raise _KeychainUnavailable("native keychain returned invalid data")
    length = bindings.cf.CFDataGetLength(value)
    pointer = bindings.cf.CFDataGetBytePtr(value)
    return bytes(pointer[:length]) if pointer and length else b""


def _persistent_reference_from_cf_data(
    bindings: _SecurityBindings,
    value: _CFTypeRef | None,
) -> str | None:
    if not value:
        return None
    raw = _cf_data_bytes(bindings, value)
    return base64.urlsafe_b64encode(raw).decode("ascii") if raw else None


def _persistent_reference_to_cf_data(
    bindings: _SecurityBindings,
    value: str,
) -> _CFTypeRef:
    try:
        raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError):
        raise ValueError("invalid keychain persistent reference") from None
    if not raw:
        raise ValueError("invalid keychain persistent reference")
    return _cf_data(bindings, raw)


def _keychain_query(
    bindings: _SecurityBindings,
    service: str,
    account: str,
    *,
    return_attributes: bool,
    return_data: bool,
    expected: _KeychainRead | None = None,
    allow_interaction: bool = False,
) -> tuple[_CFTypeRef, list[_CFTypeRef]]:
    owned: list[int] = []
    try:
        owned.append(_cf_string(bindings, service))
        owned.append(_cf_string(bindings, account))
        values = {
            bindings.constant("kSecClass"): bindings.constant("kSecClassGenericPassword"),
            bindings.constant("kSecAttrService"): owned[0],
            bindings.constant("kSecAttrAccount"): owned[1],
            bindings.constant("kSecUseAuthenticationUI"): bindings.constant(
                "kSecUseAuthenticationUIAllow"
                if allow_interaction
                else "kSecUseAuthenticationUIFail"
            ),
        }
        if return_attributes or return_data:
            values[bindings.constant("kSecMatchLimit")] = bindings.constant("kSecMatchLimitOne")
        if return_attributes:
            values[bindings.constant("kSecReturnAttributes")] = bindings.constant("kCFBooleanTrue")
            values[bindings.constant("kSecReturnPersistentRef")] = bindings.constant("kCFBooleanTrue")
        if return_data:
            values[bindings.constant("kSecReturnData")] = bindings.constant("kCFBooleanTrue")
        query = _cf_dictionary(bindings, values)
        try:
            if expected is not None:
                _bind_observed_item(bindings, query, expected)
        except BaseException:
            _release(bindings, query)
            raise
        return query, owned
    except BaseException:
        _release(bindings, *owned)
        raise


def _bind_observed_item(
    bindings: _SecurityBindings,
    query: int,
    observed: _KeychainRead,
) -> None:
    """macOS SecItem.h requires kSecMatchItemList for persistent references."""

    if not observed.persistent_reference or observed.modification_date is None:
        raise _KeychainUnavailable("native keychain mutation requires item metadata")
    owned: list[int] = []
    try:
        reference = _persistent_reference_to_cf_data(bindings, observed.persistent_reference)
        owned.append(reference)
        array = bindings.cf.CFArrayCreate(
            None, (_CFTypeRef * 1)(reference), 1, ctypes.byref(bindings.array_callbacks),
        )
        if not array:
            raise _KeychainUnavailable("native keychain query allocation failed")
        owned.append(array)
        date = bindings.cf.CFDateCreate(None, observed.modification_date)
        if not date:
            raise _KeychainUnavailable("native keychain query allocation failed")
        owned.append(date)
        bindings.cf.CFDictionarySetValue(query, bindings.constant("kSecMatchItemList"), array)
        bindings.cf.CFDictionarySetValue(query, bindings.constant("kSecAttrModificationDate"), date)
    finally:
        _release(bindings, *reversed(owned))


def _metadata_from_result(
    bindings: _SecurityBindings,
    result: _CFTypeRef,
    *,
    service: str,
    account: str,
) -> _KeychainMetadata:
    if bindings.cf.CFGetTypeID(result) != bindings.cf.CFDictionaryGetTypeID():
        raise _KeychainUnavailable("native keychain returned invalid attributes")
    safe_attributes: dict[str, Any] = {}
    for name, key in (
        ("acct", "kSecAttrAccount"),
        ("svce", "kSecAttrService"),
        ("cdat", "kSecAttrCreationDate"),
        ("mdat", "kSecAttrModificationDate"),
    ):
        value = bindings.cf.CFDictionaryGetValue(result, bindings.constant(key))
        if name in {"acct", "svce"}:
            safe_attributes[name] = _cf_string_value(bindings, value)
        elif value and bindings.cf.CFGetTypeID(value) == bindings.cf.CFDateGetTypeID():
            date = bindings.cf.CFDateGetAbsoluteTime(value)
            if not math.isfinite(date):
                raise _KeychainUnavailable("native keychain returned invalid timestamps")
            safe_attributes[name] = date
        else:
            raise _KeychainUnavailable("native keychain returned incomplete timestamps")
    if safe_attributes["acct"] != account or safe_attributes["svce"] != service:
        raise _KeychainUnavailable("native keychain returned a different item")
    persistent_reference = _persistent_reference_from_cf_data(
        bindings,
        bindings.cf.CFDictionaryGetValue(
            result,
            bindings.constant("kSecValuePersistentRef"),
        ),
    )
    if persistent_reference is None:
        raise _KeychainUnavailable("native keychain returned no item reference")
    safe_attributes["pref"] = persistent_reference
    native_revision = _sha256(
        json.dumps(
            safe_attributes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    attributes = {
        key: value
        for key, value in safe_attributes.items()
        if key != "pref"
    }
    attributes.setdefault("acct", account)
    attributes.setdefault("svce", service)
    return _KeychainMetadata(
        "found",
        native_revision,
        attributes,
        persistent_reference,
        safe_attributes["mdat"],
    )


def _keychain_status(status: int, *, action: str) -> NativeOAuthError:
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return _KeychainNotFound("native keychain item was not found")
    if status == _ERR_SEC_DUPLICATE_ITEM:
        return NativeOAuthRevisionError("native keychain item already exists")
    if status in {-25293, -25308, -25291, -25292}:
        return _KeychainDenied(f"native keychain {action} was denied")
    return _KeychainUnavailable(f"native keychain {action} failed")


@contextmanager
def _keychain_call_lock():
    with _KEYCHAIN_INTERACTION_LOCK:
        yield


@contextmanager
def _without_keychain_ui(bindings: _SecurityBindings):
    """Legacy macOS Keychain also needs its documented interaction switch.

    kSecUseAuthenticationUIFail alone is not implemented by every legacy path.
    Serialize our callers and restore the process setting on every exit.
    """

    with _keychain_call_lock():
        previous = ctypes.c_ubyte()
        status = bindings.security.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous))
        if status != _ERR_SEC_SUCCESS:
            raise _keychain_status(status, action="interaction check")
        status = bindings.security.SecKeychainSetUserInteractionAllowed(False)
        if status != _ERR_SEC_SUCCESS:
            raise _keychain_status(status, action="interaction control")
        try:
            yield
        finally:
            status = bindings.security.SecKeychainSetUserInteractionAllowed(previous.value)
            if status != _ERR_SEC_SUCCESS:
                raise _keychain_status(status, action="interaction restore") from None


class _SecurityKeychainStore:
    def _copy_matching(
        self,
        service: str,
        account: str,
        *,
        return_attributes: bool,
        return_data: bool,
        expected: _KeychainRead | None = None,
        allow_interaction: bool = False,
    ) -> tuple[_SecurityBindings, _CFTypeRef]:
        bindings = _security_bindings()
        query, owned = _keychain_query(
            bindings,
            service,
            account,
            return_attributes=return_attributes,
            return_data=return_data,
            expected=expected,
            allow_interaction=allow_interaction,
        )
        result = _CFTypeRef()
        try:
            if allow_interaction:
                with _keychain_call_lock():
                    status = bindings.security.SecItemCopyMatching(
                        query,
                        ctypes.byref(result),
                    )
            else:
                with _without_keychain_ui(bindings):
                    status = bindings.security.SecItemCopyMatching(query, ctypes.byref(result))
        finally:
            _release(bindings, query, *owned)
        if status != _ERR_SEC_SUCCESS:
            _release(bindings, result)
            raise _keychain_status(status, action="read")
        if not result:
            raise _KeychainUnavailable("native keychain returned no result")
        return bindings, result

    def metadata(self, service: str, account: str) -> _KeychainMetadata:
        try:
            bindings, result = self._copy_matching(
                service,
                account,
                return_attributes=True,
                return_data=False,
            )
        except _KeychainNotFound:
            return _KeychainMetadata("not_found", "absent")
        except _KeychainDenied:
            return _KeychainMetadata("permission_needed", "permission")
        except _KeychainUnavailable:
            raise
        try:
            return _metadata_from_result(
                bindings,
                result,
                service=service,
                account=account,
            )
        finally:
            _release(bindings, result)

    def read(
        self,
        service: str,
        account: str,
        *,
        expected: _KeychainRead | None = None,
    ) -> _KeychainRead:
        bindings, result = self._copy_matching(
            service,
            account,
            return_attributes=True,
            return_data=True,
            expected=expected,
            allow_interaction=True,
        )
        try:
            metadata = _metadata_from_result(
                bindings,
                result,
                service=service,
                account=account,
            )
            data = bindings.cf.CFDictionaryGetValue(
                result,
                bindings.constant("kSecValueData"),
            )
            if not data:
                raise _KeychainDenied("native keychain value was unavailable")
            raw = _cf_data_bytes(bindings, data)
            if not raw:
                raise _KeychainDenied("native keychain value was empty")
            try:
                value = raw.decode("utf-8")
            except UnicodeError as exc:
                raise _KeychainDenied("native keychain value was not UTF-8") from exc
            return _KeychainRead(
                value=value,
                revision=metadata.revision,
                persistent_reference=metadata.persistent_reference,
                modification_date=metadata.modification_date,
            )
        finally:
            _release(bindings, result)

    def write(
        self,
        service: str,
        account: str,
        value: str,
        *,
        expected: _KeychainRead | None = None,
    ) -> None:
        bindings = _security_bindings()
        query: _CFTypeRef | None = None
        query_owned: list[int] = []
        data: _CFTypeRef | None = None
        attributes: _CFTypeRef | None = None
        item: _CFTypeRef | None = None
        item_owned: list[int] = []
        try:
            data = _cf_data(bindings, value)
            if expected is None:
                service_ref = _cf_string(bindings, service)
                item_owned.append(service_ref)
                account_ref = _cf_string(bindings, account)
                item_owned.append(account_ref)
                item = _cf_dictionary(
                    bindings,
                    {
                        bindings.constant("kSecClass"): bindings.constant(
                            "kSecClassGenericPassword"
                        ),
                        bindings.constant("kSecAttrService"): service_ref,
                        bindings.constant("kSecAttrAccount"): account_ref,
                        bindings.constant("kSecValueData"): data,
                    },
                )
                with _keychain_call_lock():
                    status = bindings.security.SecItemAdd(item, None)
            else:
                query, query_owned = _keychain_query(
                    bindings,
                    service,
                    account,
                    return_attributes=False,
                    return_data=False,
                    expected=expected,
                    allow_interaction=True,
                )
                attributes = _cf_dictionary(
                    bindings,
                    {bindings.constant("kSecValueData"): data},
                )
                with _keychain_call_lock():
                    status = bindings.security.SecItemUpdate(query, attributes)
            if status != _ERR_SEC_SUCCESS:
                raise _keychain_status(status, action="write")
        finally:
            _release(
                bindings,
                query,
                attributes,
                data,
                item,
                *query_owned,
                *item_owned,
            )

    def delete(
        self,
        service: str,
        account: str,
        *,
        expected: _KeychainRead,
    ) -> None:
        if expected is None:
            raise ValueError("native keychain delete requires an observed item")
        bindings = _security_bindings()
        query, owned = _keychain_query(
            bindings,
            service,
            account,
            return_attributes=False,
            return_data=False,
            expected=expected,
            allow_interaction=True,
        )
        try:
            with _keychain_call_lock():
                status = bindings.security.SecItemDelete(query)
        finally:
            _release(bindings, query, *owned)
        if status not in {_ERR_SEC_SUCCESS, _ERR_SEC_ITEM_NOT_FOUND}:
            raise _keychain_status(status, action="delete")


class _FixtureKeychainStore:
    """A no-op store used whenever a caller supplies a fixture home."""

    def metadata(self, service: str, account: str) -> _KeychainMetadata:
        return _KeychainMetadata("not_found", "fixture-absent")

    def read(
        self,
        service: str,
        account: str,
        *,
        expected: _KeychainRead | None = None,
    ) -> _KeychainRead:
        raise _KeychainNotFound("fixture keychain item was not found")

    def write(
        self,
        service: str,
        account: str,
        value: str,
        *,
        expected: _KeychainRead | None = None,
    ) -> None:
        raise _KeychainUnavailable("fixture keychain is not writable")

    def delete(
        self,
        service: str,
        account: str,
        *,
        expected: _KeychainRead,
    ) -> None:
        raise _KeychainUnavailable("fixture keychain is not writable")


_DEFAULT_KEYCHAIN_STORE: _KeychainStore = _SecurityKeychainStore()
_KEYCHAIN_STORE: _KeychainStore = _DEFAULT_KEYCHAIN_STORE


def _keychain_for(home: Path | None) -> tuple[_KeychainStore, bool]:
    if _KEYCHAIN_STORE is not _DEFAULT_KEYCHAIN_STORE:
        return _KEYCHAIN_STORE, False
    if home is not None:
        return _FixtureKeychainStore(), True
    return _KEYCHAIN_STORE, False


def _read_keychain_snapshot(
    backend: str,
    service: str,
    account: str,
    *,
    store: _KeychainStore,
    isolated: bool,
    allow_secret: bool,
) -> NativeOAuthSnapshot | None:
    fallback_revision = _keychain_revision(backend, service, account, "permission")
    try:
        metadata = store.metadata(service, account)
    except (_KeychainDenied, _KeychainUnavailable):
        return _placeholder(
            backend,
            fallback_revision,
            "permission_needed",
            store="keychain",
            service=service,
            account=account,
        )
    revision = _keychain_revision(backend, service, account, metadata.revision)
    if metadata.state == "not_found":
        return None
    if metadata.state != "found":
        return _placeholder(
            backend,
            revision,
            "permission_needed",
            store="keychain",
            service=service,
            account=account,
        )
    if not allow_secret:
        return NativeOAuthSnapshot(
            backend=backend,
            revision=revision,
            payload={
                "status": "metadata_only",
                "store": "keychain",
                "service": service,
                "account": account,
                "attributes": dict(metadata.attributes),
            },
        )

    try:
        read = store.read(
            service,
            account,
            expected=_KeychainRead(
                value="",
                revision=metadata.revision,
                persistent_reference=metadata.persistent_reference,
                modification_date=metadata.modification_date,
            ),
        )
    except _KeychainNotFound:
        return _placeholder(
            backend,
            revision,
            "changed",
            store="keychain",
            service=service,
            account=account,
        )
    except (_KeychainDenied, _KeychainUnavailable):
        return _placeholder(
            backend,
            revision,
            "permission_needed",
            store="keychain",
            service=service,
            account=account,
        )
    if read.revision != metadata.revision:
        return _placeholder(
            backend,
            _keychain_revision(backend, service, account, read.revision),
            "changed",
            store="keychain",
            service=service,
            account=account,
        )
    try:
        payload = json.loads(read.value)
    except (TypeError, ValueError):
        return _placeholder(
            backend,
            revision,
            "unsupported_credentials",
            store="keychain",
            service=service,
            account=account,
        )
    if not isinstance(payload, dict):
        return _placeholder(
            backend,
            revision,
            "unsupported_credentials",
            store="keychain",
            service=service,
            account=account,
        )
    if backend == "codex":
        supported = _codex_has_credential(payload)
        after_payload = _remove_codex_oauth(payload)
    else:
        supported = _claude_oauth_payload(payload) is not None
        after_payload = _remove_claude_oauth(payload)
    if not supported:
        return None

    after_value = json.dumps(after_payload, ensure_ascii=False, indent=2)
    after_state: dict[str, Any] = (
        {"exists": False} if not after_payload else {"exists": True, "value": after_value}
    )
    operation = _keychain_operation(
        service,
        account,
        {"exists": True, "value": read.value, "revision": metadata.revision},
        after_state,
        isolated=isolated,
        persistent_reference=(
            read.persistent_reference
            if read.persistent_reference is not None
            else metadata.persistent_reference
        ),
        modification_date=(
            read.modification_date
            if read.modification_date is not None
            else metadata.modification_date
        ),
    )
    return NativeOAuthSnapshot(
        backend=backend,
        revision=revision,
        payload=payload,
        exportable=True,
        keychain_edit=_edit(backend, revision, [operation]),
    )


def _read_codex_file(
    auth_path: Path,
    *,
    allow_secret: bool,
) -> NativeOAuthSnapshot | None:
    revision = _path_revision("codex", auth_path)
    status, payload, raw = _read_json_file(auth_path)
    if status == "missing":
        return None
    if status in {"permission_needed", "invalid"} or payload is None:
        return _placeholder("codex", revision, status, store="file")
    if not _codex_has_credential(payload):
        return None
    if not allow_secret:
        return NativeOAuthSnapshot(
            backend="codex",
            revision=f"codex:file:{_sha256((raw or '').encode('utf-8'))}",
            payload=payload,
            exportable=True,
        )
    before = _json_file_state_from_raw(raw)
    after_payload = _remove_codex_oauth(payload)
    after = _state_for_json_payload(after_payload, raw or "")
    operation = _file_operation(auth_path, before, after)
    file_revision = f"codex:file:{_sha256((raw or '').encode('utf-8'))}"
    return NativeOAuthSnapshot(
        backend="codex",
        revision=file_revision,
        payload=payload,
        exportable=True,
        keychain_edit=_edit("codex", file_revision, [operation]),
    )


def _read_claude_files(
    credentials_paths: tuple[Path, ...],
    *,
    allow_secret: bool,
    keychain_snapshot: NativeOAuthSnapshot | None = None,
    service: str | None = None,
    account: str | None = None,
) -> NativeOAuthSnapshot | None:
    # One original read supplies the revision, payload and journal before-image.
    files = [(path, *_read_json_file(path)) for path in credentials_paths]
    revision = _locator_revision(
        "claude",
        "keychain" if keychain_snapshot is not None else "file",
        json.dumps({
            "keychain": keychain_snapshot.revision if keychain_snapshot is not None else None,
            "files": [
                [str(path), status, _sha256(raw.encode("utf-8")) if raw is not None else None]
                for path, status, _, raw in files
            ],
        }, ensure_ascii=True, separators=(",", ":")),
    )
    for _, status, _, _ in files:
        if status in {"permission_needed", "invalid"}:
            # A hidden/invalid fallback cannot be certified free of credentials.
            return _placeholder("claude", revision, status, store="file")

    payloads = [
        payload for _, _, payload, _ in files
        if payload is not None and _claude_oauth_payload(payload) is not None
    ]
    if keychain_snapshot is not None and keychain_snapshot.exportable:
        if keychain_snapshot.payload is not None:
            payloads.insert(0, keychain_snapshot.payload)
    # Compare every pair: a primary lacking identity must not hide contradictory
    # identity evidence between two fallback files sharing its refresh grant.
    if any(
        not _claude_same_account(primary, fallback)
        for index, primary in enumerate(payloads)
        for fallback in payloads[index + 1:]
    ):
        return _placeholder(
            "claude", revision, "conflict",
            store="keychain+file" if keychain_snapshot is not None else "file",
            service=service if keychain_snapshot is not None else None,
            account=account if keychain_snapshot is not None else None,
        )
    if keychain_snapshot is not None and not keychain_snapshot.exportable:
        # Preserve metadata-only/permission semantics, never choose a file over
        # an unread Keychain. File changes invalidate this same consent revision.
        return replace(keychain_snapshot, revision=revision)
    if not payloads:
        return None

    edit = None
    if allow_secret:
        operations = (
            list(keychain_snapshot.keychain_edit["operations"])
            if keychain_snapshot is not None and keychain_snapshot.keychain_edit else []
        )
        for path, _, payload, raw in files:
            before = _json_file_state_from_raw(raw)
            after = (
                _state_for_json_payload(_remove_claude_oauth(payload), raw or "")
                if payload is not None and _claude_oauth_payload(payload) is not None
                else before
            )
            operations.append(_file_operation(path, before, after))
        edit = _edit("claude", revision, operations)
    return NativeOAuthSnapshot(
        backend="claude",
        revision=revision,
        payload=payloads[0],
        exportable=True,
        keychain_edit=edit,
    )


def _read_codex(home: Path | None, *, allow_secret: bool) -> NativeOAuthSnapshot | None:
    codex_home = _codex_home(home)
    store_mode, unsupported = _codex_store_config(codex_home)
    auth_path = native_credentials_paths("codex", home)[0]
    if unsupported is not None:
        revision = _locator_revision("codex", "unsupported", str(codex_home), unsupported)
        return _placeholder("codex", revision, "unsupported_store", store=unsupported)
    if store_mode == "ephemeral":
        return None
    if store_mode == "file":
        return _read_codex_file(auth_path, allow_secret=allow_secret)

    service = "Codex Auth"
    canonical_home = codex_home.resolve()
    account = f"cli|{_sha256(str(canonical_home).encode('utf-8'))[:16]}"
    store, isolated = _keychain_for(home)
    keychain_snapshot = _read_keychain_snapshot(
        "codex",
        service,
        account,
        store=store,
        isolated=isolated,
        allow_secret=allow_secret,
    )
    if store_mode == "keyring":
        return keychain_snapshot
    if keychain_snapshot is not None:
        return keychain_snapshot
    return _read_codex_file(auth_path, allow_secret=allow_secret)


def _read_claude(home: Path | None, *, allow_secret: bool) -> NativeOAuthSnapshot | None:
    credentials_paths = native_credentials_paths("claude", home)
    # Claude uses the file store off macOS. An unavailable Security framework
    # there is not a Keychain permission request. Explicit fixture homes may
    # still exercise a fake macOS store on any test host.
    if home is None and sys.platform != "darwin":
        return _read_claude_files(credentials_paths, allow_secret=allow_secret)
    _, service, account = _claude_locator(home)
    store, isolated = _keychain_for(home)
    keychain_snapshot = _read_keychain_snapshot(
        "claude",
        service,
        account,
        store=store,
        isolated=isolated,
        allow_secret=allow_secret,
    )
    return _read_claude_files(
        credentials_paths, allow_secret=allow_secret, keychain_snapshot=keychain_snapshot,
        service=service, account=account,
    )


def read_native_oauth(
    backend: str,
    *,
    home: Path | None = None,
    allow_secret: bool = False,
) -> NativeOAuthSnapshot | None:
    """Resolve the effective native OAuth store for ``backend``.

    ``home`` is a fixture root. Supplying it disables access to the real OS
    Keychain; tests may replace the module's private ``_KEYCHAIN_STORE`` with
    a fake store when exercising Keychain behavior.
    """

    normalized = str(backend or "").strip().lower()
    if normalized == "codex":
        return _read_codex(home, allow_secret=allow_secret)
    if normalized == "claude":
        return _read_claude(home, allow_secret=allow_secret)
    return None


def _state_value(state: Mapping[str, Any]) -> tuple[bool, str | dict[str, Any] | None]:
    exists = bool(state.get("exists"))
    if not exists:
        return False, None
    if "raw" in state:
        value = state["raw"]
    else:
        value = state.get("value")
    if isinstance(value, (str, dict)):
        return True, value
    return True, None


def _state_matches(live: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    expected_revision = expected.get("revision")
    if expected_revision is not None and live.get("revision") != expected_revision:
        return False
    live_exists, live_value = _state_value(live)
    expected_exists, expected_value = _state_value(expected)
    if live_exists != expected_exists:
        return False
    if not live_exists:
        return True
    if live_value == expected_value:
        return True
    if isinstance(live_value, str) and isinstance(expected_value, dict):
        try:
            return json.loads(live_value) == expected_value
        except (TypeError, ValueError):
            return False
    if isinstance(live_value, dict) and isinstance(expected_value, str):
        try:
            return live_value == json.loads(expected_value)
        except (TypeError, ValueError):
            return False
    return False


def _state_without_revision(state: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(state)
    result.pop("revision", None)
    return result


def _desired_state_matches(
    live: Mapping[str, Any],
    desired: Mapping[str, Any],
    *,
    reverse: bool,
) -> bool:
    if _state_matches(live, desired):
        return True
    return reverse and _state_matches(live, _state_without_revision(desired))


def _live_keychain_state(
    store: _KeychainStore,
    operation: Mapping[str, Any],
    *,
    bind_persistent_reference: bool = True,
) -> dict[str, Any]:
    service = operation.get("service")
    account = operation.get("account")
    if not isinstance(service, str) or not isinstance(account, str):
        raise ValueError("invalid keychain edit locator")
    persistent_reference = (
        operation.get("persistent_reference") if bind_persistent_reference else None
    )
    if persistent_reference is not None and not isinstance(persistent_reference, str):
        raise ValueError("invalid keychain edit persistent reference")
    modification_date = operation.get("modification_date")
    if modification_date is not None and not isinstance(modification_date, (int, float)):
        raise ValueError("invalid keychain edit modification date")
    expected = None
    if bind_persistent_reference and persistent_reference is not None:
        if modification_date is None:
            raise ValueError("native keychain edit is missing modification date")
        expected = _KeychainRead(
            value="",
            revision="",
            persistent_reference=persistent_reference,
            modification_date=float(modification_date),
        )
    try:
        read = store.read(
            service,
            account,
            expected=expected,
        )
    except _KeychainNotFound:
        return {"exists": False}
    except (_KeychainDenied, _KeychainUnavailable) as exc:
        raise NativeOAuthPermissionError("native keychain could not be read") from exc
    return {
        "exists": True,
        "value": read.value,
        "revision": read.revision,
        "persistent_reference": read.persistent_reference,
        "modification_date": read.modification_date,
    }


def _live_keychain_state_for_apply(
    store: _KeychainStore,
    operation: Mapping[str, Any],
    *,
    reverse: bool,
) -> dict[str, Any]:
    if reverse:
        # The cleanup mutation may have changed mdat. Reverse therefore reads
        # by the stable service/account locator, then binds the mutation to
        # the exact item and fresh metadata returned by this read.
        return _live_keychain_state(
            store,
            operation,
            bind_persistent_reference=False,
        )
    live = _live_keychain_state(store, operation)
    if not live.get("exists") and operation.get("persistent_reference"):
        # A successful prior update changes mdat, so its original bound query
        # is intentionally stale on an idempotent retry. Re-read by locator
        # only to distinguish the desired post-state from a replacement item;
        # any mutation below is still bound to the fresh observation.
        locator_live = _live_keychain_state(
            store,
            operation,
            bind_persistent_reference=False,
        )
        if locator_live.get("exists"):
            return locator_live
    return live


def _live_file_state(operation: Mapping[str, Any]) -> dict[str, Any]:
    raw_path = operation.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("invalid file edit locator")
    return _json_file_state(Path(raw_path))


def _apply_file_state(operation: Mapping[str, Any], desired: Mapping[str, Any]) -> None:
    raw_path = operation.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("invalid file edit locator")
    path = Path(raw_path)
    exists, value = _state_value(desired)
    if not exists:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise NativeOAuthPermissionError("native credential file could not be removed") from exc
        return
    if not isinstance(value, str):
        raise ValueError("invalid file edit state")
    try:
        write_atomic(path, value)
    except OSError as exc:
        raise NativeOAuthPermissionError("native credential file could not be written") from exc


def _apply_keychain_state(
    store: _KeychainStore,
    operation: Mapping[str, Any],
    desired: Mapping[str, Any],
    *,
    observed: Mapping[str, Any],
) -> None:
    service = operation.get("service")
    account = operation.get("account")
    if not isinstance(service, str) or not isinstance(account, str):
        raise ValueError("invalid keychain edit locator")
    exists, value = _state_value(desired)
    observed_exists = bool(observed.get("exists"))
    observed_read = None
    if observed_exists:
        persistent_reference = observed.get("persistent_reference")
        modification_date = observed.get("modification_date")
        if not isinstance(persistent_reference, str) or not isinstance(
            modification_date,
            (int, float),
        ):
            raise NativeOAuthRevisionError("native keychain item metadata was incomplete")
        observed_read = _KeychainRead(
            value=str(observed.get("value", "")),
            revision=str(observed.get("revision", "")),
            persistent_reference=persistent_reference,
            modification_date=float(modification_date),
        )
    try:
        if exists:
            if not isinstance(value, str):
                raise ValueError("invalid keychain edit state")
            store.write(
                service,
                account,
                value,
                expected=observed_read,
            )
        else:
            if observed_read is None:
                raise NativeOAuthRevisionError("native keychain item disappeared")
            store.delete(
                service,
                account,
                expected=observed_read,
            )
    except _KeychainNotFound as exc:
        raise NativeOAuthRevisionError("native keychain item changed") from exc
    except (_KeychainDenied, _KeychainUnavailable) as exc:
        raise NativeOAuthPermissionError("native keychain mutation was denied") from exc
    live = _live_keychain_state(
        store,
        operation,
        bind_persistent_reference=False,
    )
    readback_expected = dict(desired)
    readback_expected.pop("revision", None)
    if not _state_matches(live, readback_expected):
        raise NativeOAuthRevisionError("native keychain mutation did not read back")


def _normalize_operations(edit: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_operations = edit.get("operations")
    if isinstance(raw_operations, list):
        operations = raw_operations
    elif all(key in edit for key in ("service", "account", "before", "after")):
        operations = [
            {
                "kind": "keychain",
                "service": edit["service"],
                "account": edit["account"],
                "before": edit["before"],
                "after": edit["after"],
                "store_scope": edit.get("store_scope", "native"),
            }
        ]
    else:
        raise ValueError("invalid native OAuth edit")
    if not operations or not all(isinstance(operation, dict) for operation in operations):
        raise ValueError("invalid native OAuth edit")
    return operations


def _check_edit(
    edit: Mapping[str, Any],
    *,
    target_key: str,
    reverse: bool,
) -> bool:
    operations = _normalize_operations(edit)
    keychain_store = _KEYCHAIN_STORE
    for operation in operations:
        kind = operation.get("kind")
        if kind == "keychain":
            if operation.get("store_scope") == "fixture" and keychain_store is _DEFAULT_KEYCHAIN_STORE:
                raise NativeOAuthPermissionError("fixture keychain is not available")
            live = _live_keychain_state(
                keychain_store,
                operation,
                bind_persistent_reference=(
                    not reverse and target_key == "before"
                ),
            )
        elif kind == "file":
            live = _live_file_state(operation)
        else:
            raise ValueError("invalid native OAuth edit operation")
        expected = operation.get(target_key)
        if not isinstance(expected, dict):
            raise ValueError("invalid native OAuth edit state")
        matches = (
            _desired_state_matches(live, expected, reverse=True)
            if reverse
            else _state_matches(live, expected)
        )
        if not matches:
            raise NativeOAuthRevisionError("native OAuth store changed")
    return True


def check_keychain_edit(
    edit: dict[str, Any],
    *,
    applied: bool = False,
    reverse: bool = False,
) -> bool:
    """Check a journal edit before or after forward/reverse recovery."""

    if not isinstance(edit, dict) or edit.get("version") not in {None, 1}:
        raise ValueError("invalid native OAuth edit")
    if reverse:
        target_key = "before" if applied else "after"
    else:
        target_key = "after" if applied else "before"
    return _check_edit(edit, target_key=target_key, reverse=reverse)


def apply_keychain_edit(edit: dict[str, Any], *, reverse: bool = False) -> None:
    """Compare-and-swap a native cleanup edit, or apply its reverse.

    The live state is read and checked for every operation before any
    operation mutates. A live state that already equals the desired side is
    accepted as an idempotent retry. Any other state is a stale revision and
    aborts without mutation. This function only changes local stores; it
    never performs remote logout or token revocation.
    """

    if not isinstance(edit, dict) or edit.get("version") not in {None, 1}:
        raise ValueError("invalid native OAuth edit")
    operations = _normalize_operations(edit)
    keychain_store = _KEYCHAIN_STORE
    planned: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]] = []

    for operation in operations:
        kind = operation.get("kind")
        if kind == "keychain":
            if operation.get("store_scope") == "fixture" and keychain_store is _DEFAULT_KEYCHAIN_STORE:
                raise NativeOAuthPermissionError("fixture keychain is not available")
            live = _live_keychain_state_for_apply(
                keychain_store,
                operation,
                reverse=reverse,
            )
            is_keychain = True
        elif kind == "file":
            live = _live_file_state(operation)
            is_keychain = False
        else:
            raise ValueError("invalid native OAuth edit operation")
        expected_key = "after" if reverse else "before"
        desired_key = "before" if reverse else "after"
        expected = operation.get(expected_key)
        desired = operation.get(desired_key)
        if not isinstance(expected, dict) or not isinstance(desired, dict):
            raise ValueError("invalid native OAuth edit state")
        if _desired_state_matches(live, desired, reverse=reverse):
            planned.append((operation, desired, expected, is_keychain))
            continue
        matches = (
            _desired_state_matches(live, expected, reverse=True)
            if reverse
            else _state_matches(live, expected)
        )
        if not matches:
            raise NativeOAuthRevisionError("native OAuth store changed")
        planned.append((operation, desired, expected, is_keychain))

    try:
        for operation, desired, expected, is_keychain in planned:
            if is_keychain:
                live = _live_keychain_state_for_apply(
                    keychain_store,
                    operation,
                    reverse=reverse,
                )
            else:
                live = _live_file_state(operation)
            if _desired_state_matches(live, desired, reverse=reverse):
                continue
            matches = (
                _desired_state_matches(live, expected, reverse=True)
                if reverse
                else _state_matches(live, expected)
            )
            if not matches:
                raise NativeOAuthRevisionError("native OAuth store changed")
            if is_keychain:
                _apply_keychain_state(
                    keychain_store,
                    operation,
                    desired,
                    observed=live,
                )
            else:
                _apply_file_state(operation, desired)
                if not _state_matches(_live_file_state(operation), desired):
                    raise NativeOAuthRevisionError("native file mutation did not read back")
    except (NativeOAuthError, OSError, ValueError) as exc:
        if isinstance(exc, NativeOAuthError):
            raise
        raise NativeOAuthError("native OAuth edit could not be applied") from exc
