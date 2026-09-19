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
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

__all__ = [
    "NativeOAuthError",
    "NativeOAuthPermissionError",
    "NativeOAuthRevisionError",
    "NativeOAuthSnapshot",
    "apply_keychain_edit",
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
_SAFE_CLAUDE_ACCOUNT = re.compile(r"^[A-Za-z0-9._-]+$")
_SECURITY_TOOL = "/usr/bin/security"


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
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _KeychainRead:
    value: str
    revision: str


class _KeychainStore(Protocol):
    def metadata(self, service: str, account: str) -> _KeychainMetadata:
        ...

    def read(self, service: str, account: str) -> _KeychainRead:
        ...

    def write(self, service: str, account: str, value: str) -> None:
        ...

    def delete(self, service: str, account: str) -> None:
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


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        try:
            import tomllib  # type: ignore[attr-defined]
        except ImportError:  # pragma: no cover - Python 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


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


def _claude_oauth_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    nested = payload.get("claudeAiOauth")
    if isinstance(nested, dict) and _has_nonempty_string(nested, _CLAUDE_OAUTH_KEYS):
        return nested
    if _has_nonempty_string(payload, _CLAUDE_OAUTH_KEYS):
        return dict(payload)
    return None


def _remove_codex_oauth(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("tokens", None)
    result.pop("last_refresh", None)
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
) -> dict[str, Any]:
    return {
        "kind": "keychain",
        "service": service,
        "account": account,
        "before": before,
        "after": after,
        "store_scope": "fixture" if isolated else "native",
    }


def _codex_home(home: Path | None) -> Path:
    if home is not None:
        return Path(home).expanduser().resolve() / ".codex"
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


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
            suffix = "" if not secure_env else f"-{_sha256(str(secure_root).encode('utf-8'))[:8]}"
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
    config = _read_toml(codex_home / "config.toml")
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


def _run_security(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``security`` without ever putting a credential in argv."""

    try:
        return subprocess.run(
            [_SECURITY_TOOL, *args],
            input=input_text,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL if input_text is None else None,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _KeychainUnavailable("native keychain is unavailable") from exc


def _security_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return any(
        marker in output
        for marker in (
            "could not be found",
            "item not found",
            "errsecitemnotfound",
            "no matching",
        )
    )


def _security_attributes(stdout: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key in {"acct", "svce", "cdat", "mdat", "uuid", "type"}:
            attributes[key] = value
    return attributes


class _SecurityKeychainStore:
    def metadata(self, service: str, account: str) -> _KeychainMetadata:
        if sys.platform != "darwin":
            return _KeychainMetadata("not_found", "unavailable")
        result = _run_security(["find-generic-password", "-a", account, "-s", service])
        if result.returncode == 0:
            attributes = _security_attributes(result.stdout or "")
            native_revision = "|".join(
                attributes.get(key, "")
                for key in ("uuid", "mdat", "cdat", "acct", "svce")
            )
            return _KeychainMetadata("found", native_revision or "present", attributes)
        if _security_not_found(result):
            return _KeychainMetadata("not_found", "absent")
        return _KeychainMetadata("permission_needed", "permission")

    def read(self, service: str, account: str) -> _KeychainRead:
        metadata = self.metadata(service, account)
        if metadata.state == "not_found":
            raise _KeychainNotFound("native keychain item was not found")
        if metadata.state != "found":
            raise _KeychainDenied("native keychain read was denied")
        result = _run_security(["find-generic-password", "-a", account, "-s", service, "-w"])
        if result.returncode != 0:
            if _security_not_found(result):
                raise _KeychainNotFound("native keychain item was not found")
            raise _KeychainDenied("native keychain read was denied")
        value = (result.stdout or "").rstrip("\r\n")
        if not value:
            raise _KeychainDenied("native keychain value was empty")
        return _KeychainRead(value=value, revision=metadata.revision)

    def write(self, service: str, account: str, value: str) -> None:
        result = _run_security(
            ["add-generic-password", "-U", "-a", account, "-s", service, "-w"],
            input_text=value,
        )
        if result.returncode != 0:
            raise _KeychainDenied("native keychain write was denied")

    def delete(self, service: str, account: str) -> None:
        result = _run_security(["delete-generic-password", "-a", account, "-s", service])
        if result.returncode != 0 and not _security_not_found(result):
            raise _KeychainDenied("native keychain delete was denied")


class _FixtureKeychainStore:
    """A no-op store used whenever a caller supplies a fixture home."""

    def metadata(self, service: str, account: str) -> _KeychainMetadata:
        return _KeychainMetadata("not_found", "fixture-absent")

    def read(self, service: str, account: str) -> _KeychainRead:
        raise _KeychainNotFound("fixture keychain item was not found")

    def write(self, service: str, account: str, value: str) -> None:
        raise _KeychainUnavailable("fixture keychain is not writable")

    def delete(self, service: str, account: str) -> None:
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
        read = store.read(service, account)
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
        supported = _codex_has_oauth(payload)
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
    if not _codex_has_oauth(payload):
        return None
    if not allow_secret:
        return NativeOAuthSnapshot(
            backend="codex",
            revision=f"codex:file:{_sha256((raw or '').encode('utf-8'))}",
            payload=payload,
            exportable=True,
        )
    try:
        before = _json_file_state(auth_path)
    except NativeOAuthPermissionError:
        return _placeholder("codex", revision, "permission_needed", store="file")
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


def _read_claude_file(
    credentials_path: Path,
    *,
    allow_secret: bool,
) -> NativeOAuthSnapshot | None:
    revision = _path_revision("claude", credentials_path)
    status, payload, raw = _read_json_file(credentials_path)
    if status == "missing":
        return None
    if status in {"permission_needed", "invalid"} or payload is None:
        return _placeholder("claude", revision, status, store="file")
    if _claude_oauth_payload(payload) is None:
        return None
    if not allow_secret:
        file_revision = f"claude:file:{_sha256((raw or '').encode('utf-8'))}"
        return NativeOAuthSnapshot(
            backend="claude",
            revision=file_revision,
            payload=payload,
            exportable=True,
        )
    try:
        before = _json_file_state(credentials_path)
    except NativeOAuthPermissionError:
        return _placeholder("claude", revision, "permission_needed", store="file")
    after_payload = _remove_claude_oauth(payload)
    after = _state_for_json_payload(after_payload, raw or "")
    operation = _file_operation(credentials_path, before, after)
    file_revision = f"claude:file:{_sha256((raw or '').encode('utf-8'))}"
    return NativeOAuthSnapshot(
        backend="claude",
        revision=file_revision,
        payload=payload,
        exportable=True,
        keychain_edit=_edit("claude", file_revision, [operation]),
    )


def _read_codex(home: Path | None, *, allow_secret: bool) -> NativeOAuthSnapshot | None:
    codex_home = _codex_home(home)
    store_mode, unsupported = _codex_store_config(codex_home)
    auth_path = codex_home / "auth.json"
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
    secure_root, service, account = _claude_locator(home)
    credentials_path = secure_root / ".credentials.json"
    store, isolated = _keychain_for(home)
    keychain_snapshot = _read_keychain_snapshot(
        "claude",
        service,
        account,
        store=store,
        isolated=isolated,
        allow_secret=allow_secret,
    )
    if keychain_snapshot is not None:
        if keychain_snapshot.exportable and allow_secret and keychain_snapshot.keychain_edit:
            try:
                file_status, file_payload, file_raw = _read_json_file(credentials_path)
            except OSError:
                file_status, file_payload, file_raw = "permission_needed", None, None
            if file_status == "permission_needed":
                return _placeholder(
                    "claude",
                    keychain_snapshot.revision,
                    "permission_needed",
                    store="file",
                )
            if file_status not in {"missing", "invalid"} and file_payload is not None:
                if _claude_oauth_payload(file_payload) is not None:
                    try:
                        file_before = _json_file_state(credentials_path)
                    except NativeOAuthPermissionError:
                        return _placeholder(
                            "claude",
                            keychain_snapshot.revision,
                            "permission_needed",
                            store="file",
                        )
                    file_after_payload = _remove_claude_oauth(file_payload)
                    operations = list(keychain_snapshot.keychain_edit["operations"])
                    operations.append(
                        _file_operation(
                            credentials_path,
                            file_before,
                            _state_for_json_payload(file_after_payload, file_raw or ""),
                        )
                    )
                    return NativeOAuthSnapshot(
                        backend="claude",
                        revision=keychain_snapshot.revision,
                        payload=keychain_snapshot.payload,
                        exportable=True,
                        keychain_edit=_edit(
                            "claude",
                            keychain_snapshot.revision,
                            operations,
                        ),
                    )
        if keychain_snapshot.payload and keychain_snapshot.payload.get("status") == "metadata_only":
            return keychain_snapshot
        return keychain_snapshot

    return _read_claude_file(credentials_path, allow_secret=allow_secret)


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


def _live_keychain_state(store: _KeychainStore, operation: Mapping[str, Any]) -> dict[str, Any]:
    service = operation.get("service")
    account = operation.get("account")
    if not isinstance(service, str) or not isinstance(account, str):
        raise ValueError("invalid keychain edit locator")
    try:
        read = store.read(service, account)
    except _KeychainNotFound:
        return {"exists": False}
    except (_KeychainDenied, _KeychainUnavailable) as exc:
        raise NativeOAuthPermissionError("native keychain could not be read") from exc
    return {"exists": True, "value": read.value, "revision": read.revision}


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.avibe-tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise NativeOAuthPermissionError("native credential file could not be written") from exc


def _apply_keychain_state(
    store: _KeychainStore,
    operation: Mapping[str, Any],
    desired: Mapping[str, Any],
) -> None:
    service = operation.get("service")
    account = operation.get("account")
    if not isinstance(service, str) or not isinstance(account, str):
        raise ValueError("invalid keychain edit locator")
    exists, value = _state_value(desired)
    try:
        if exists:
            if not isinstance(value, str):
                raise ValueError("invalid keychain edit state")
            store.write(service, account, value)
        else:
            store.delete(service, account)
    except (_KeychainDenied, _KeychainUnavailable) as exc:
        raise NativeOAuthPermissionError("native keychain mutation was denied") from exc


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
    planned: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool, bool]] = []

    for operation in operations:
        kind = operation.get("kind")
        if kind == "keychain":
            if operation.get("store_scope") == "fixture" and keychain_store is _DEFAULT_KEYCHAIN_STORE:
                raise NativeOAuthPermissionError("fixture keychain is not available")
            live = _live_keychain_state(keychain_store, operation)
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
        if _state_matches(live, desired):
            planned.append((operation, desired, expected, is_keychain, False))
            continue
        if not _state_matches(live, expected):
            raise NativeOAuthRevisionError("native OAuth store changed")
        planned.append((operation, desired, expected, is_keychain, True))

    applied: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
    try:
        for operation, desired, original, is_keychain, should_apply in planned:
            if not should_apply:
                continue
            if is_keychain:
                _apply_keychain_state(keychain_store, operation, desired)
            else:
                _apply_file_state(operation, desired)
            applied.append((operation, original, is_keychain))
    except (NativeOAuthError, OSError, ValueError) as exc:
        for operation, original, is_keychain in reversed(applied):
            try:
                if is_keychain:
                    _apply_keychain_state(keychain_store, operation, original)
                else:
                    _apply_file_state(operation, original)
            except (NativeOAuthError, OSError, ValueError):
                pass
        if isinstance(exc, NativeOAuthError):
            raise
        raise NativeOAuthError("native OAuth edit could not be applied") from exc
