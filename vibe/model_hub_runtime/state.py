from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse


_CREDENTIAL_REF_RE = re.compile(r"^cred_[A-Za-z0-9_-]{6,128}$")
_SOURCE_ID_RE = re.compile(r"^src_[a-z0-9]{8,}$")
_PROTOCOLS = {"anthropic", "openai_responses", "openai_chat", "openai_compatible"}


class EngineStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSecrets:
    management_key: str
    gateway_token: str


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    vendor: str
    protocol: str
    base_url: str | None
    credential_ref: str
    allowed_origins: tuple[str, ...]
    model_ids: tuple[str, ...]
    prefix: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SourceRecord:
        return cls(
            source_id=str(payload["source_id"]),
            vendor=str(payload["vendor"]),
            protocol=str(payload["protocol"]),
            base_url=str(payload["base_url"]) if payload.get("base_url") else None,
            credential_ref=str(payload["credential_ref"]),
            allowed_origins=tuple(str(item) for item in payload.get("allowed_origins", [])),
            model_ids=tuple(str(model) for model in payload.get("model_ids", [])),
            prefix=str(payload["prefix"]),
        )


class EngineStateStore:
    """Restricted local state for engine-only keys and upstream credentials."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()

    @property
    def auth_dir(self) -> Path:
        return self.root / "auth"

    def prepare_instance(self, install_id: str, *, rotate: bool = False) -> tuple[Path, RuntimeSecrets]:
        with self._lock:
            self._ensure_private_dir(self.root)
            self.audit_auth_permissions()
            instance_dir = self.root / "instances" / _safe_identifier(install_id)
            self._ensure_private_dir(instance_dir)
            self._remove_obsolete_instances(instance_dir)
            secrets_path = instance_dir / "runtime-secrets.json"
            if not rotate:
                self._assert_private_file(secrets_path, "runtime secret permissions are unsafe")
                existing = self._read_json(secrets_path)
                if existing:
                    management_key = existing.get("management_key")
                    gateway_token = existing.get("gateway_token")
                    if isinstance(management_key, str) and isinstance(gateway_token, str):
                        return instance_dir, RuntimeSecrets(management_key, gateway_token)
            generated = RuntimeSecrets(
                management_key=secrets.token_urlsafe(48),
                gateway_token=secrets.token_urlsafe(48),
            )
            self._secure_write_json(secrets_path, asdict(generated))
            return instance_dir, generated

    def list_sources(self) -> list[SourceRecord]:
        with self._lock:
            payload = self._read_json(self.root / "sources.json") or {}
            raw_sources = payload.get("sources", [])
            if not isinstance(raw_sources, list):
                raise EngineStateError("invalid engine source state")
            return [SourceRecord.from_payload(item) for item in raw_sources if isinstance(item, dict)]

    def get_source(self, source_id: str) -> SourceRecord | None:
        return next((source for source in self.list_sources() if source.source_id == source_id), None)

    def validate_source_id(self, source_id: str) -> None:
        _validated_source_id(source_id)

    def store_api_key(
        self,
        value: str,
        *,
        vendor: str = "custom",
        protocol: str = "openai_compatible",
        base_url: str | None = None,
    ) -> str:
        if not isinstance(value, str) or not value:
            raise EngineStateError("credential is empty")
        normalized_vendor = vendor.strip().lower()
        if not normalized_vendor:
            raise EngineStateError("credential vendor is empty")
        if protocol not in _PROTOCOLS:
            raise EngineStateError("unsupported source protocol")
        normalized_base_url = _validated_base_url(base_url)
        with self._lock:
            credential_ref = f"cred_{secrets.token_hex(16)}"
            self._secure_write_json(
                self._credential_path(credential_ref),
                {
                    "kind": "api_key",
                    "vendor": normalized_vendor,
                    "protocol": protocol,
                    "base_url": normalized_base_url,
                    "value": value,
                },
            )
            return credential_ref

    def bind_oauth_credential(self, source_id: str, vendor: str, auth_name: str) -> str:
        _validated_source_id(source_id)
        if not vendor.strip() or not auth_name.strip():
            raise EngineStateError("OAuth credential binding is incomplete")
        normalized_vendor = vendor.strip().lower()
        normalized_auth_name = auth_name.strip()
        with self._lock:
            matches = [
                (credential_ref, payload)
                for credential_ref, payload in self._oauth_credentials()
                if payload.get("auth_name") == normalized_auth_name
            ]
            if matches:
                if len(matches) != 1:
                    raise EngineStateError("OAuth auth record binding is ambiguous")
                credential_ref, payload = matches[0]
                if payload.get("source_id") != source_id or payload.get("vendor") != normalized_vendor:
                    raise EngineStateError("OAuth auth record is already bound to another source")
                return credential_ref
            credential_ref = f"cred_{secrets.token_hex(16)}"
            prefix = f"avibe-{secrets.token_hex(12)}"
            self._secure_write_json(
                self._credential_path(credential_ref),
                {
                    "kind": "oauth",
                    "source_id": source_id,
                    "vendor": normalized_vendor,
                    "auth_name": normalized_auth_name,
                    "prefix": prefix,
                },
            )
            return credential_ref

    def sync_sources(self, bindings: Sequence[Any]) -> list[SourceRecord]:
        """Atomically replace the engine projection using opaque credential refs."""
        with self._lock:
            existing = {source.source_id: source for source in self.list_sources()}
            records: list[SourceRecord] = []
            seen: set[str] = set()
            for binding in bindings:
                source_id = _validated_source_id(str(binding.source_id))
                if source_id in seen:
                    raise EngineStateError("duplicate source binding")
                seen.add(source_id)
                credential_ref = str(binding.credential_ref)
                credential = self.credential_metadata(credential_ref)
                protocol = str(binding.protocol)
                if protocol not in _PROTOCOLS:
                    raise EngineStateError("unsupported source protocol")
                vendor = str(binding.vendor).strip().lower()
                base_url = _validated_base_url(binding.base_url)
                _validate_source_target(vendor, protocol, base_url)
                if credential["kind"] == "api_key":
                    if (
                        credential.get("vendor") != vendor
                        or credential.get("protocol") != protocol
                        or credential.get("base_url") != base_url
                    ):
                        raise EngineStateError("credential does not match source binding")
                elif (
                    credential.get("source_id") != source_id
                    or credential.get("vendor") != vendor
                    or base_url is not None
                ):
                    raise EngineStateError("OAuth credential does not match source binding")
                allowed_origins = tuple(dict.fromkeys(str(origin).strip() for origin in binding.allowed_origins))
                if any(not origin for origin in allowed_origins):
                    raise EngineStateError("allowed origin cannot be empty")
                if credential["kind"] == "oauth" and not allowed_origins:
                    raise EngineStateError("OAuth source requires at least one allowed origin")
                previous = existing.get(source_id)
                model_ids = tuple(dict.fromkeys(str(model).strip() for model in binding.model_ids))
                if not model_ids:
                    raise EngineStateError("source requires at least one model id")
                if any(not model for model in model_ids):
                    raise EngineStateError("model id cannot be empty")
                records.append(
                    SourceRecord(
                        source_id=source_id,
                        vendor=vendor,
                        protocol=protocol,
                        base_url=base_url,
                        credential_ref=credential_ref,
                        allowed_origins=allowed_origins,
                        model_ids=model_ids,
                        prefix=(
                            str(credential["prefix"])
                            if credential.get("prefix")
                            else previous.prefix
                            if previous
                            else f"avibe-{secrets.token_hex(12)}"
                        ),
                    )
                )
            self._write_sources(records)
            return records

    def replace_sources(self, sources: Sequence[SourceRecord]) -> None:
        """Restore a previously validated source projection."""
        with self._lock:
            self._write_sources(sources)

    def set_models(self, source_id: str, model_ids: Sequence[str]) -> SourceRecord:
        with self._lock:
            sources = self.list_sources()
            current = next((source for source in sources if source.source_id == source_id), None)
            if current is None:
                raise EngineStateError("source is not registered")
            models = tuple(dict.fromkeys(str(model).strip() for model in model_ids))
            if not models:
                raise EngineStateError("source requires at least one model id")
            if any(not model for model in models):
                raise EngineStateError("model id cannot be empty")
            updated_record = SourceRecord(**{**asdict(current), "model_ids": models})
            self._write_sources([updated_record if source.source_id == source_id else source for source in sources])
            return updated_record

    def credential_metadata(self, credential_ref: str) -> dict[str, Any]:
        path = self._credential_path(credential_ref)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            mode = 0
        if mode and (not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600):
            raise EngineStateError("credential permissions are unsafe")
        payload = self._read_json(path)
        kind = payload.get("kind") if payload else None
        if kind not in {"api_key", "oauth"}:
            raise EngineStateError("credential is unavailable")
        return payload

    def validate_api_key_target(
        self,
        credential_ref: str,
        *,
        vendor: str,
        protocol: str,
        base_url: str | None,
    ) -> str | None:
        metadata = self.credential_metadata(credential_ref)
        normalized_base_url = _validated_base_url(base_url)
        if (
            metadata["kind"] != "api_key"
            or metadata.get("vendor") != vendor.strip().lower()
            or metadata.get("protocol") != protocol
            or metadata.get("base_url") != normalized_base_url
        ):
            raise EngineStateError("credential does not match discovery target")
        return normalized_base_url

    def assert_credential_unbound(self, credential_ref: str) -> None:
        if any(source.credential_ref == credential_ref for source in self.list_sources()):
            raise EngineStateError("credential is still bound to a source")

    def revoke_credential(self, credential_ref: str) -> None:
        with self._lock:
            self.assert_credential_unbound(credential_ref)
            path = self._credential_path(credential_ref)
            if not path.exists():
                return
            self.credential_metadata(credential_ref)
            path.unlink()

    def clear_runtime_configs(self) -> None:
        """Remove persisted engine configs after any credential is revoked."""
        with self._lock:
            self._ensure_private_dir(self.root)
            instances_dir = self.root / "instances"
            if not instances_dir.exists():
                return
            self._ensure_private_dir(instances_dir)
            for instance_dir in instances_dir.iterdir():
                mode = instance_dir.lstat().st_mode
                if not stat.S_ISDIR(mode):
                    raise EngineStateError("engine instance directory is unsafe")
                config_path = instance_dir / "config.yaml"
                try:
                    config_mode = config_path.lstat().st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(config_mode):
                    raise EngineStateError("engine config path is unsafe")
                config_path.unlink()

    def read_api_key(self, credential_ref: str) -> str:
        payload = self.credential_metadata(credential_ref)
        value = payload.get("value") if payload.get("kind") == "api_key" else None
        if not isinstance(value, str) or not value:
            raise EngineStateError("API key credential is unavailable")
        return value

    def oauth_auth_name(self, credential_ref: str) -> str | None:
        payload = self.credential_metadata(credential_ref)
        value = payload.get("auth_name") if payload.get("kind") == "oauth" else None
        return str(value) if value else None

    def oauth_credential_ref(self, auth_name: str) -> str | None:
        normalized = auth_name.strip()
        with self._lock:
            matches = [
                credential_ref
                for credential_ref, payload in self._oauth_credentials()
                if payload.get("auth_name") == normalized
            ]
        if len(matches) > 1:
            raise EngineStateError("OAuth auth record binding is ambiguous")
        return matches[0] if matches else None

    def delete_oauth_auth_file(self, auth_name: str) -> None:
        """Delete one managed OAuth file without requiring a running engine."""
        normalized = auth_name.strip()
        if (
            not normalized
            or "\x00" in normalized
            or "\\" in normalized
            or Path(normalized).name != normalized
            or not normalized.lower().endswith(".json")
        ):
            raise EngineStateError("invalid OAuth auth file name")
        with self._lock:
            self.audit_auth_permissions(enforce=True)
            path = self.auth_dir / normalized
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                return
            if not stat.S_ISREG(mode):
                raise EngineStateError("engine auth credential path is unsafe")
            path.unlink()

    def audit_auth_permissions(self, *, enforce: bool = False) -> None:
        self._ensure_private_dir(self.root)
        if not self.auth_dir.exists():
            self.auth_dir.mkdir(parents=True, mode=0o700)
        auth_mode = self.auth_dir.lstat().st_mode
        if not stat.S_ISDIR(auth_mode):
            raise EngineStateError("engine auth directory is unsafe")
        if stat.S_IMODE(self.auth_dir.stat().st_mode) != 0o700:
            raise EngineStateError("engine auth directory permissions are unsafe")
        for entry in self.auth_dir.iterdir():
            mode = entry.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise EngineStateError("engine auth directory contains an unsafe entry")
            if stat.S_IMODE(mode) == 0o600:
                continue
            if not enforce:
                raise EngineStateError("engine auth credential permissions are unsafe")
            entry.chmod(0o600)

    def _credential_path(self, credential_ref: str) -> Path:
        if _CREDENTIAL_REF_RE.fullmatch(credential_ref) is None:
            raise EngineStateError("invalid credential reference")
        self._ensure_private_dir(self.root)
        credentials_dir = self.root / "credentials"
        self._ensure_private_dir(credentials_dir)
        return credentials_dir / f"{credential_ref}.json"

    def _oauth_credentials(self) -> list[tuple[str, dict[str, Any]]]:
        self._ensure_private_dir(self.root)
        credentials_dir = self.root / "credentials"
        self._ensure_private_dir(credentials_dir)
        result: list[tuple[str, dict[str, Any]]] = []
        for path in credentials_dir.glob("cred_*.json"):
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise EngineStateError("credential permissions are unsafe")
            credential_ref = path.stem
            if path.suffix != ".json" or _CREDENTIAL_REF_RE.fullmatch(credential_ref) is None:
                raise EngineStateError("credential state contains an unsafe entry")
            payload = self._read_json(path)
            if payload and payload.get("kind") == "oauth":
                result.append((credential_ref, payload))
        return result

    def _write_sources(self, sources: Sequence[SourceRecord]) -> None:
        self._secure_write_json(
            self.root / "sources.json",
            {
                "sources": [
                    {
                        **asdict(source),
                        "allowed_origins": list(source.allowed_origins),
                        "model_ids": list(source.model_ids),
                    }
                    for source in sources
                ]
            },
        )

    def _remove_obsolete_instances(self, active_instance: Path) -> None:
        instances_dir = active_instance.parent
        self._ensure_private_dir(instances_dir)
        for instance_dir in instances_dir.iterdir():
            if instance_dir == active_instance:
                continue
            mode = instance_dir.lstat().st_mode
            if not stat.S_ISDIR(mode):
                raise EngineStateError("engine instance directory is unsafe")
            shutil.rmtree(instance_dir)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise EngineStateError(f"invalid engine state file: {path.name}") from exc
        if not isinstance(payload, dict):
            raise EngineStateError(f"invalid engine state file: {path.name}")
        return payload

    @classmethod
    def _secure_write_json(cls, path: Path, payload: dict[str, Any]) -> None:
        cls._ensure_private_dir(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _ensure_private_dir(path: Path) -> None:
        if path.exists():
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise EngineStateError("engine state path is unsafe")
        else:
            path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)

    @staticmethod
    def _assert_private_file(path: Path, message: str) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise EngineStateError(message)


def _safe_identifier(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return cleaned or "unknown"


def _validated_source_id(value: str) -> str:
    if _SOURCE_ID_RE.fullmatch(value) is None:
        raise EngineStateError("invalid source id")
    return value


def _validated_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise EngineStateError("invalid source base URL")
    return normalized


def _validate_source_target(vendor: str, protocol: str, base_url: str | None) -> None:
    if protocol == "anthropic" and base_url is None and vendor != "anthropic":
        raise EngineStateError("Anthropic-compatible source requires a base URL")
    if protocol == "openai_responses" and base_url is None and vendor not in {"openai", "codex"}:
        raise EngineStateError("Responses API source requires a base URL")
    if protocol in {"openai_chat", "openai_compatible"} and base_url is None and vendor != "openai":
        raise EngineStateError("OpenAI-compatible source requires a base URL")
