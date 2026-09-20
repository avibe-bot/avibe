from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from config.atomic_io import write_atomic
from config.v2_config import normalize_model_hub_base_url
from vibe.model_hub_runtime.api_key_vendors import (
    official_api_key_base_url,
    validate_api_key_auth_scheme,
)


logger = logging.getLogger(__name__)

_CREDENTIAL_REF_RE = re.compile(r"^cred_[A-Za-z0-9_-]{6,128}$")
_SOURCE_ID_RE = re.compile(r"^src_[a-z0-9]{8,}$")
_PROTOCOLS = {"anthropic", "openai_responses", "openai_chat"}


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
    model_reasoning_efforts: tuple[tuple[str, tuple[str, ...]], ...] = ()
    route_model_ids: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SourceRecord:
        try:
            raw_reasoning_efforts = payload["model_reasoning_efforts"]
        except KeyError as exc:
            raise EngineStateError("invalid engine source reasoning state") from exc
        if not isinstance(raw_reasoning_efforts, list):
            raise EngineStateError("invalid engine source reasoning state")
        raw_model_ids = payload.get("model_ids", [])
        if not isinstance(raw_model_ids, list):
            raise EngineStateError("invalid engine source state")
        model_ids = tuple(str(model) for model in raw_model_ids)
        route_model_ids = payload.get("route_model_ids", [])
        if not isinstance(route_model_ids, list) or any(
            not isinstance(model, str) or not model or model != model.strip()
            for model in route_model_ids
        ) or len(set(route_model_ids)) != len(route_model_ids):
            raise EngineStateError("invalid engine route model state")
        parsed_reasoning_efforts: list[tuple[str, tuple[str, ...]]] = []
        seen_reasoning_models: set[str] = set()
        for item in raw_reasoning_efforts:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or item[0] not in model_ids
                or not isinstance(item[1], list)
                or any(not isinstance(effort, str) or not effort for effort in item[1])
                or len(set(item[1])) != len(item[1])
                or item[0] in seen_reasoning_models
            ):
                raise EngineStateError("invalid engine source reasoning state")
            seen_reasoning_models.add(item[0])
            parsed_reasoning_efforts.append((item[0], tuple(item[1])))
        return cls(
            source_id=str(payload["source_id"]),
            vendor=str(payload["vendor"]),
            protocol=str(payload["protocol"]),
            base_url=str(payload["base_url"]) if payload.get("base_url") else None,
            credential_ref=str(payload["credential_ref"]),
            allowed_origins=tuple(str(item) for item in payload.get("allowed_origins", [])),
            model_ids=model_ids,
            prefix=str(payload["prefix"]),
            model_reasoning_efforts=tuple(parsed_reasoning_efforts),
            route_model_ids=tuple(route_model_ids),
        )


class EngineStateStore:
    """Restricted local state for engine-only keys and upstream credentials."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()

    @property
    def auth_dir(self) -> Path:
        return self.root / "auth"

    @property
    def oauth_staging_dir(self) -> Path:
        return self.root / "oauth-staging"

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
            try:
                payload = self._read_json(self.root / "sources.json") or {}
                raw_sources = payload.get("sources", [])
                if not isinstance(raw_sources, list):
                    raise EngineStateError("invalid engine source state")
                return [
                    SourceRecord.from_payload(item)
                    for item in raw_sources
                    if isinstance(item, dict)
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise EngineStateError(f"invalid engine source state: {exc}") from exc

    def get_source(self, source_id: str) -> SourceRecord | None:
        return next((source for source in self.list_sources() if source.source_id == source_id), None)

    def validate_source_id(self, source_id: str) -> None:
        _validated_source_id(source_id)

    def store_api_key(
        self,
        value: str,
        *,
        vendor: str = "custom",
        protocol: str = "openai_chat",
        base_url: str | None = None,
        auth_scheme: str | None = None,
        on_reserved: Callable[[str], None] | None = None,
    ) -> str:
        if not isinstance(value, str) or not value:
            raise EngineStateError("credential is empty")
        normalized_vendor = vendor.strip().lower()
        if not normalized_vendor:
            raise EngineStateError("credential vendor is empty")
        if protocol not in _PROTOCOLS:
            raise EngineStateError("unsupported source protocol")
        try:
            validate_api_key_auth_scheme(
                normalized_vendor, protocol, base_url, value, auth_scheme,
            )
        except ValueError:
            raise EngineStateError("unsupported API key authentication scheme") from None
        normalized_base_url = _validated_base_url(base_url)
        with self._lock:
            credential_ref = f"cred_{secrets.token_hex(16)}"
            credential_path, credential_tmp, stage_path, stage_tmp = (
                self._reserve_credential_namespace(credential_ref)
            )
            try:
                if on_reserved is not None:
                    on_reserved(credential_ref)
                self._write_reserved_json(
                    credential_path,
                    {
                        "kind": "api_key",
                        "vendor": normalized_vendor,
                        "protocol": protocol,
                        "base_url": normalized_base_url,
                        "value": value,
                        **({"auth_scheme": auth_scheme} if auth_scheme is not None else {}),
                    },
                    temporary_path=credential_tmp,
                    credential_ref=credential_ref,
                )
                self._remove_private_file_if_present(stage_path)
                self._remove_private_file_if_present(stage_tmp)
            except BaseException:
                self._cleanup_private_paths(
                    (credential_path, credential_tmp, stage_path, stage_tmp)
                )
                raise
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

    def stage_oauth_credential(
        self,
        source_id: str,
        vendor: str,
        auth_name: str,
        payload: dict[str, Any],
        on_reserved: Callable[[str], None] | None = None,
    ) -> str:
        """Persist an OAuth grant outside the engine's watched auth directory."""

        _validated_source_id(source_id)
        normalized_vendor = vendor.strip().lower()
        normalized_auth_name = _validated_oauth_auth_name(auth_name)
        if not normalized_vendor:
            raise EngineStateError("OAuth credential binding is incomplete")
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise EngineStateError("invalid OAuth auth payload")
        with self._lock:
            self._ensure_private_dir(self.root)
            self._ensure_private_dir(self.oauth_staging_dir)
            credential_ref = f"cred_{secrets.token_hex(16)}"
            prefix = f"avibe-{secrets.token_hex(12)}"
            staged_payload = {
                **payload,
                # CPA's file synthesizer reads the prefix from the auth JSON.
                # It must be part of the fully-written staged bytes before the
                # file can become visible in the watched auth directory.
                "prefix": prefix,
            }
            expected_provider = _oauth_provider_for_vendor(normalized_vendor)
            if str(staged_payload.get("type") or "").strip().lower() != expected_provider:
                raise EngineStateError("OAuth auth payload provider does not match vendor")
            credential_path, credential_tmp, stage_path, stage_tmp = (
                self._reserve_credential_namespace(credential_ref)
            )
            try:
                if on_reserved is not None:
                    on_reserved(credential_ref)
                self._write_reserved_json(
                    stage_path,
                    staged_payload,
                    temporary_path=stage_tmp,
                    credential_ref=credential_ref,
                )
                stage_revision = self._file_revision(stage_path)
                self._write_reserved_json(
                    credential_path,
                    {
                        "kind": "oauth",
                        "source_id": source_id,
                        "vendor": normalized_vendor,
                        "auth_name": normalized_auth_name,
                        "prefix": prefix,
                        "activation_state": "staged",
                        "engine_published": False,
                        "prefix_published": False,
                        "stage_revision": stage_revision,
                        "published_revision": None,
                        "published_identity": None,
                    },
                    temporary_path=credential_tmp,
                    credential_ref=credential_ref,
                )
            except BaseException as exc:
                self._cleanup_private_paths(
                    (credential_path, credential_tmp, stage_path, stage_tmp)
                )
                raise EngineStateError("unable to stage OAuth credential") from exc
            return credential_ref

    def write_oauth_auth_file(self, auth_name: str, payload: dict[str, Any]) -> None:
        """Write an imported OAuth grant into the engine auth directory."""
        normalized = _validated_oauth_auth_name(auth_name)
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise EngineStateError("invalid OAuth auth payload")
        with self._lock:
            self.audit_auth_permissions(enforce=True)
            self._secure_write_json(self.auth_dir / normalized, payload)

    def activate_oauth_auth_file(
        self,
        credential_ref: str,
    ) -> tuple[str, dict[str, Any], str, bool]:
        """Publish one staged grant without replacing an existing live file.

        The watched path is the durable owner after publication. CPA may rotate
        the access and refresh tokens in that same file at any time, so retries
        validate only the opaque file identity (name, provider, prefix), never
        the bytes or a token hash captured before publication.
        """

        with self._lock:
            metadata = self.credential_metadata(credential_ref)
            if metadata.get("kind") != "oauth":
                raise EngineStateError("OAuth credential is unavailable")
            auth_name = _validated_oauth_auth_name(str(metadata.get("auth_name") or ""))
            target = self.auth_dir / auth_name
            self.audit_auth_permissions(enforce=True)

            # A live watched file always wins over private staging. This is the
            # crash/refresh recovery rule: never re-publish stale R0 bytes after
            # CPA has already observed or rotated the file.
            if target.exists():
                payload = self._decode_oauth_payload(target)
                identity = self._assert_oauth_auth_file_owned_locked(metadata, target, payload)
                updated = {
                    **metadata,
                    "activation_state": "active",
                    "prefix_published": True,
                    "published_identity": identity,
                }
                self._secure_write_json(self._credential_path(credential_ref), updated)
                self._remove_oauth_stage_if_present(credential_ref)
                return auth_name, payload, identity["prefix"], metadata.get("activation_state") == "active"

            stage_path = self._oauth_stage_path(credential_ref)
            staged_bytes = self._read_private_bytes(
                stage_path,
                "OAuth staging file is unsafe",
            )
            stage_revision = metadata.get("stage_revision")
            actual_stage_revision = hashlib.sha256(staged_bytes).hexdigest()
            if isinstance(stage_revision, str) and actual_stage_revision != stage_revision:
                raise EngineStateError("OAuth staging record is inconsistent")
            staged_payload = self._decode_oauth_payload(stage_path)
            self._assert_oauth_payload_identity(metadata, auth_name, staged_payload)
            try:
                self._publish_staged_no_replace(stage_path, target)
            except FileExistsError:
                # The watcher or another retry won the publication race. Read
                # that winner and bind to it; do not upload stale staged bytes.
                if not target.exists():
                    raise EngineStateError("OAuth auth file publication is inconclusive") from None
                payload = self._decode_oauth_payload(target)
                identity = self._assert_oauth_auth_file_owned_locked(metadata, target, payload)
            else:
                payload = self._decode_oauth_payload(target)
                identity = self._assert_oauth_auth_file_owned_locked(metadata, target, payload)

            updated = {
                **metadata,
                "activation_state": "active",
                "prefix_published": True,
                "published_identity": identity,
            }
            self._secure_write_json(self._credential_path(credential_ref), updated)
            self._remove_oauth_stage_if_present(credential_ref)
            return auth_name, payload, identity["prefix"], False

    def mark_oauth_engine_published(self, credential_ref: str) -> None:
        with self._lock:
            metadata = self.credential_metadata(credential_ref)
            if metadata.get("kind") != "oauth":
                raise EngineStateError("OAuth credential is unavailable")
            auth_name = _validated_oauth_auth_name(str(metadata.get("auth_name") or ""))
            identity = self._assert_oauth_auth_file_owned_locked(
                metadata,
                self.auth_dir / auth_name,
            )
            self._secure_write_json(
                self._credential_path(credential_ref),
                {
                    **metadata,
                    "activation_state": "active",
                    "engine_published": True,
                    "prefix_published": True,
                    "published_identity": identity,
                },
            )

    def assert_oauth_auth_file_unchanged(self, credential_ref: str) -> None:
        """Backward-compatible name for the post-publication ownership check."""

        with self._lock:
            metadata = self.credential_metadata(credential_ref)
            if metadata.get("kind") != "oauth":
                raise EngineStateError("OAuth credential is unavailable")
            auth_name = _validated_oauth_auth_name(str(metadata.get("auth_name") or ""))
            self._assert_oauth_auth_file_owned_locked(metadata, self.auth_dir / auth_name)

    def mark_oauth_prefix_published(self, credential_ref: str) -> None:
        with self._lock:
            metadata = self.credential_metadata(credential_ref)
            if metadata.get("kind") != "oauth":
                raise EngineStateError("OAuth credential is unavailable")
            auth_name = _validated_oauth_auth_name(str(metadata.get("auth_name") or ""))
            identity = self._assert_oauth_auth_file_owned_locked(
                metadata,
                self.auth_dir / auth_name,
            )
            self._secure_write_json(
                self._credential_path(credential_ref),
                {
                    **metadata,
                    "activation_state": "active",
                    "prefix_published": True,
                    "published_identity": identity,
                },
            )

    def matches_api_key_credential(
        self,
        credential_ref: str,
        vendor: str,
        protocol: str,
        secret: str,
        base_url: str | None,
        *,
        auth_scheme: str | None = None,
    ) -> bool:
        """Compare transient native material with an engine-owned API key."""

        metadata = self.credential_metadata(credential_ref)
        if metadata.get("kind") != "api_key":
            return False
        try:
            validate_api_key_auth_scheme(vendor, protocol, base_url, secret, auth_scheme)
        except ValueError:
            raise EngineStateError("unsupported API key authentication scheme") from None
        normalized_base_url = _validated_base_url(base_url)
        if (
            metadata.get("vendor") != vendor.strip().lower()
            or metadata.get("protocol") != protocol
            or metadata.get("base_url") != normalized_base_url
            or metadata.get("auth_scheme") != auth_scheme
        ):
            return False
        if not isinstance(secret, str):
            return False
        stored = self.read_api_key(credential_ref)
        return hmac.compare_digest(
            stored.encode("utf-8"),
            secret.encode("utf-8"),
        )

    def sync_sources(self, bindings: Sequence[Any]) -> list[SourceRecord]:
        """Atomically replace the engine projection using opaque credential refs."""
        with self._lock:
            path = self.root / "sources.json"
            try:
                existing = {source.source_id: source for source in self.list_sources()}
            except EngineStateError as exc:
                self._discard_invalid_state_file(path, exc)
                existing = {}
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
                _validate_source_target(
                    vendor,
                    protocol,
                    base_url,
                    credential_kind=credential["kind"],
                )
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
                if any(not model for model in model_ids):
                    raise EngineStateError("model id cannot be empty")
                route_model_ids = tuple(binding.route_model_ids)
                if any(not isinstance(model, str) or not model or model != model.strip() for model in route_model_ids):
                    raise EngineStateError("invalid route model id")
                reasoning_by_model: dict[str, tuple[str, ...]] = {}
                for model_id, efforts in binding.model_reasoning_efforts:
                    normalized_model_id = str(model_id).strip()
                    if not normalized_model_id or normalized_model_id not in model_ids:
                        raise EngineStateError("reasoning model id is not registered")
                    if normalized_model_id in reasoning_by_model:
                        raise EngineStateError("duplicate reasoning model id")
                    normalized_efforts = tuple(
                        dict.fromkeys(str(effort).strip() for effort in efforts)
                    )
                    if any(not effort for effort in normalized_efforts):
                        raise EngineStateError("reasoning effort cannot be empty")
                    reasoning_by_model[normalized_model_id] = normalized_efforts
                records.append(
                    SourceRecord(
                        source_id=source_id,
                        vendor=vendor,
                        protocol=protocol,
                        base_url=base_url,
                        credential_ref=credential_ref,
                        allowed_origins=allowed_origins,
                        model_ids=model_ids,
                        route_model_ids=tuple(sorted(set(route_model_ids))),
                        prefix=(
                            str(credential["prefix"])
                            if credential.get("prefix")
                            else previous.prefix
                            if previous
                            else f"avibe-{secrets.token_hex(12)}"
                        ),
                        model_reasoning_efforts=tuple(reasoning_by_model.items()),
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
            retained_reasoning = tuple(
                (model_id, efforts)
                for model_id, efforts in current.model_reasoning_efforts
                if model_id in models
            )
            updated_record = SourceRecord(
                **{
                    **asdict(current),
                    "model_ids": models,
                    "model_reasoning_efforts": retained_reasoning,
                }
            )
            self._write_sources([updated_record if source.source_id == source_id else source for source in sources])
            return updated_record

    def credential_metadata(self, credential_ref: str) -> dict[str, Any]:
        payload = self.credential_metadata_if_present(credential_ref)
        if payload is None:
            raise EngineStateError("credential is unavailable")
        return payload

    def credential_metadata_if_present(
        self,
        credential_ref: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            path = self._credential_path(credential_ref)
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise EngineStateError("credential permissions are unsafe")
            payload = self._read_json(path)
            kind = payload.get("kind") if payload else None
            if kind == "reservation" and payload.get("credential_ref") == credential_ref:
                return None
            if kind not in {"api_key", "oauth"}:
                raise EngineStateError("credential is unavailable")
            if kind == "api_key":
                try:
                    validate_api_key_auth_scheme(
                        payload.get("vendor"), payload.get("protocol"), payload.get("base_url"),
                        payload.get("value"), payload.get("auth_scheme"),
                    )
                    if payload.get("auth_scheme") is not None and (
                        payload.get("protocol") != "anthropic"
                        or not isinstance(payload.get("value"), str)
                        or not payload["value"]
                    ):
                        raise ValueError
                except ValueError:
                    raise EngineStateError("unsupported API key authentication scheme") from None
            elif payload.get("auth_scheme") is not None:
                raise EngineStateError("unsupported API key authentication scheme")
            return payload

    def has_current_source_credential(
        self,
        credential_ref: str,
        *,
        source_id: str,
        kind: str,
        vendor: str,
        protocol: str,
        base_url: str | None,
    ) -> bool:
        """Observe a Source's current credential binding without repair or secrets.

        Unlike credential_metadata(), this read never creates directories or
        fixes permissions. It proves local ownership/existence, not upstream
        authentication, refreshability, or model entitlement. OAuth auth files
        remain entirely engine-owned and are not opened by this observation.
        """
        try:
            if not isinstance(credential_ref, str) or _CREDENTIAL_REF_RE.fullmatch(credential_ref) is None:
                return False
            _validated_source_id(source_id)
            credential_kind = {"api_key": "api_key", "subscription": "oauth"}.get(kind)
            if credential_kind is None or not isinstance(vendor, str) or protocol not in _PROTOCOLS:
                return False
            normalized_vendor = vendor.strip().lower()
            normalized_base_url = _validated_base_url(base_url)
            _validate_source_target(
                normalized_vendor, protocol, normalized_base_url, credential_kind=credential_kind,
            )
            credentials_dir = self.root / "credentials"
            for directory in (self.root, credentials_dir):
                mode = directory.lstat().st_mode
                if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
                    return False
            payload = json.loads(self._read_private_bytes(
                credentials_dir / f"{credential_ref}.json",
                "credential path is unsafe",
            ))
            if not isinstance(payload, dict) or (
                payload.get("kind") != credential_kind or payload.get("vendor") != normalized_vendor
            ):
                return False
            if credential_kind == "api_key":
                validate_api_key_auth_scheme(
                    normalized_vendor, protocol, normalized_base_url,
                    payload.get("value"), payload.get("auth_scheme"),
                )
                return (
                    payload.get("protocol") == protocol
                    and payload.get("base_url") == normalized_base_url
                    and isinstance(payload.get("value"), str)
                    and bool(payload["value"].strip())
                )
            auth_name = payload.get("auth_name")
            return (
                normalized_base_url is None
                and payload.get("source_id") == source_id
                and payload.get("activation_state") in {None, "active"}
                and isinstance(auth_name, str)
                and bool(_validated_oauth_auth_name(auth_name))
            )
        except (OSError, ValueError, TypeError, EngineStateError):
            return False

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
            path, temporary_path, stage_path, stage_temporary_path = (
                self._credential_namespace_paths(credential_ref)
            )
            self.credential_metadata_if_present(credential_ref)
            self._remove_private_file_if_present(stage_path)
            self._remove_private_file_if_present(stage_temporary_path)
            self._remove_private_file_if_present(temporary_path)
            self._remove_private_file_if_present(path)

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
        normalized = _validated_oauth_auth_name(auth_name)
        with self._lock:
            self.audit_auth_permissions(enforce=True)
            self._remove_private_file_if_present(self.auth_dir / normalized)

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

    def _credential_namespace_paths(
        self,
        credential_ref: str,
    ) -> tuple[Path, Path, Path, Path]:
        credential_path = self._credential_path(credential_ref)
        stage_path = self._oauth_stage_path(credential_ref)
        return (
            credential_path,
            credential_path.with_name(f".{credential_path.name}.tmp"),
            stage_path,
            stage_path.with_name(f".{stage_path.name}.tmp"),
        )

    def _reserve_credential_namespace(
        self,
        credential_ref: str,
    ) -> tuple[Path, Path, Path, Path]:
        paths = self._credential_namespace_paths(credential_ref)
        reserved: list[Path] = []
        try:
            for path in paths:
                self._reserve_path(path, credential_ref)
                reserved.append(path)
        except BaseException:
            try:
                self._cleanup_private_paths(tuple(reserved))
            except BaseException:
                logger.warning(
                    "Unable to discard provisional credential reservations for %s",
                    credential_ref,
                    exc_info=True,
                )
            raise
        return paths

    def _oauth_stage_path(self, credential_ref: str) -> Path:
        if _CREDENTIAL_REF_RE.fullmatch(credential_ref) is None:
            raise EngineStateError("invalid credential reference")
        self._ensure_private_dir(self.oauth_staging_dir)
        return self.oauth_staging_dir / f"{credential_ref}.json"

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
                        "route_model_ids": list(source.route_model_ids),
                        "model_reasoning_efforts": [
                            [model_id, list(efforts)]
                            for model_id, efforts in source.model_reasoning_efforts
                        ],
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
    def _discard_invalid_state_file(path: Path, error: EngineStateError) -> None:
        invalid_path = path.with_name(f"{path.name}.invalid")
        try:
            path.replace(invalid_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EngineStateError(
                f"unable to discard invalid engine state file: {path.name}"
            ) from exc
        logger.warning("Discarded invalid engine state file %s: %s", path.name, error)

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
        # The file is 0600 by ``write_atomic``; the directory is this store's own
        # concern, since it holds the engine's secrets alongside its state.
        cls._ensure_private_dir(path.parent)
        write_atomic(path, json.dumps(payload, sort_keys=True) + "\n")

    @classmethod
    def _reserve_path(cls, path: Path, credential_ref: str) -> None:
        """Create a no-secret, no-replace reservation for one exact path."""

        cls._ensure_private_dir(path.parent)
        payload = json.dumps(
            {"kind": "reservation", "credential_ref": credential_ref},
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise EngineStateError("credential reference collision") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            cls._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @classmethod
    def _write_reserved_json(
        cls,
        path: Path,
        payload: dict[str, Any],
        *,
        temporary_path: Path,
        credential_ref: str,
    ) -> None:
        """Write complete bytes to a deterministic per-ref temp before rename."""

        cls._ensure_private_dir(path.parent)
        serialized = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
        reservation = {"credential_ref": credential_ref, "kind": "reservation"}
        if cls._read_json(path) != reservation or cls._read_json(temporary_path) != reservation:
            raise EngineStateError("credential reference collision")
        descriptor = -1
        try:
            descriptor = os.open(temporary_path, os.O_WRONLY | os.O_TRUNC)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            cls._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @classmethod
    def _remove_private_file_if_present(cls, path: Path) -> bool:
        try:
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                # A prior unlink may have succeeded before its directory fsync
                # failed. Retrying an absent path must still establish the
                # directory durability boundary before cleanup can converge.
                cls._fsync_directory(path.parent)
                return False
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise EngineStateError("engine state path is unsafe")
            path.unlink()
            cls._fsync_directory(path.parent)
            return True
        except EngineStateError:
            raise
        except OSError as exc:
            raise EngineStateError("unable to remove engine state file") from exc

    @classmethod
    def _cleanup_private_paths(cls, paths: Sequence[Path]) -> None:
        first_error: BaseException | None = None
        for path in paths:
            try:
                cls._remove_private_file_if_present(path)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _file_revision(path: Path) -> str:
        return hashlib.sha256(
            EngineStateStore._read_private_bytes(
                path,
                "engine auth credential path is unsafe",
            )
        ).hexdigest()

    @staticmethod
    def _read_private_bytes(path: Path, message: str) -> bytes:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as exc:
            raise EngineStateError("OAuth credential is unavailable") from exc
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise EngineStateError(message)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise EngineStateError("OAuth credential is unavailable") from exc

    @staticmethod
    def _decode_oauth_payload(path: Path) -> dict[str, Any]:
        payload = EngineStateStore._read_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise EngineStateError("OAuth credential is unavailable")
        return payload

    @staticmethod
    def _publish_staged_no_replace(stage_path: Path, target: Path) -> None:
        """Atomically expose a complete staged file without replacing a target.

        ``link`` adds the fully-written staging inode to the watched directory
        as one directory-entry operation. It is intentionally followed by
        directory fsyncs and only then removes the private staging name.
        """

        try:
            os.link(stage_path, target, follow_symlinks=False)
        except FileExistsError:
            raise
        except OSError as exc:
            raise EngineStateError("unable to publish OAuth auth file") from exc
        try:
            EngineStateStore._fsync_directory(target.parent)
            stage_path.unlink()
            EngineStateStore._fsync_directory(stage_path.parent)
        except OSError as exc:
            raise EngineStateError("unable to finalize OAuth staging") from exc

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0)
        if not flags:
            return
        descriptor = os.open(path, flags | os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _assert_oauth_auth_file_owned_locked(
        self,
        metadata: dict[str, Any],
        path: Path,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        auth_name = _validated_oauth_auth_name(str(metadata.get("auth_name") or ""))
        current = payload if payload is not None else self._decode_oauth_payload(path)
        return self._assert_oauth_payload_identity(metadata, auth_name, current)

    @staticmethod
    def _assert_oauth_payload_identity(
        metadata: dict[str, Any],
        auth_name: str,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        expected_provider = _oauth_provider_for_vendor(str(metadata.get("vendor") or ""))
        actual_provider = str(payload.get("type") or "").strip().lower()
        expected_prefix = str(metadata.get("prefix") or "").strip()
        actual_prefix = str(payload.get("prefix") or "").strip().strip("/")
        if (
            actual_provider != expected_provider
            or not expected_prefix
            or actual_prefix != expected_prefix
        ):
            raise EngineStateError("OAuth auth file ownership is unavailable")
        return {
            "auth_name": auth_name,
            "provider": expected_provider,
            "prefix": expected_prefix,
        }

    def _remove_oauth_stage_if_present(self, credential_ref: str) -> None:
        self._remove_private_file_if_present(self._oauth_stage_path(credential_ref))

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


def _validated_oauth_auth_name(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or Path(normalized).name != normalized
        or not normalized.lower().endswith(".json")
    ):
        raise EngineStateError("invalid OAuth auth file name")
    return normalized


def _oauth_provider_for_vendor(vendor: str) -> str:
    normalized = vendor.strip().lower()
    provider = {
        "anthropic": "claude",
        "claude": "claude",
        "openai": "codex",
        "codex": "codex",
    }.get(normalized)
    if provider is None:
        raise EngineStateError("native OAuth vendor is unsupported")
    return provider


def _safe_identifier(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return cleaned or "unknown"


def _validated_source_id(value: str) -> str:
    if _SOURCE_ID_RE.fullmatch(value) is None:
        raise EngineStateError("invalid source id")
    return value


def _validated_base_url(value: str | None) -> str | None:
    try:
        return normalize_model_hub_base_url(value)
    except (TypeError, ValueError):
        raise EngineStateError("invalid source base URL")


# `_append_source` raises these same three strings when it reaches a Source whose
# upstream it cannot resolve. The check below is the earlier refusal of that one
# condition, so it answers with the message the renderer would have.
_MISSING_BASE_URL_ERRORS = {
    "anthropic": "Anthropic-compatible source requires a base URL",
    "openai_responses": "Responses API source requires a base URL",
    "openai_chat": "OpenAI-compatible source requires a base URL",
}


def _validate_source_target(
    vendor: str,
    protocol: str,
    base_url: str | None,
    *,
    credential_kind: str,
) -> None:
    """Reject a Source whose upstream this runtime cannot resolve.

    This asks exactly what `_append_source` asks when it renders the YAML: an
    api-key Source is reachable over its own ``base_url``, or over the official
    one the shipped catalog holds for its vendor, and over nothing else.

    It deliberately does not compare the Source's protocol against that vendor's
    catalog pin. Which protocols a vendor may be added as belongs to the create
    path's proof ladder — a catalog pin, a client declaration on `custom`, or a
    protocol-shaped response — and that decision was made when the Source was
    saved. Re-deciding it here would judge a stored Source by a pin that can
    change under it: a vendor repinned between releases would retroactively
    invalidate the Sources its own earlier pin admitted. Since `sync_sources`
    replaces the whole projection atomically, that verdict is not private to the
    Source it falls on — it would take every other Source down with it.

    An engine-held credential has no upstream to resolve at all: the auth file
    stays inside the engine, `_append_source` returns before rendering it, and
    `sync_sources` already requires ``base_url is None``. The requirement does
    not reach it.
    """

    if credential_kind != "api_key":
        return
    if base_url is not None:
        return
    if official_api_key_base_url(vendor) is not None:
        return
    raise EngineStateError(_MISSING_BASE_URL_ERRORS[protocol])
