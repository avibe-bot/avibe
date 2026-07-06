"""CRUD + resolve + grants + audit over the vault tables.

Data layer for Vaults, sibling to ``storage/messages_service.py`` etc.: functions take
a SQLAlchemy ``Connection`` and never open their own engine. This module owns the
metadata invariants around stored envelopes, approval requests, grants, and audit
rows so future vault behavior lands here rather than in callers.

Secret values and key material never live here. Standard-tier values are sealed by
``avault`` before this layer stores them. Protected-tier values arrive already
encrypted by the browser; this layer only stores the opaque ciphertext + wrap
metadata. Grants persist metadata only; protected delivery material is owned by
the resident avault agent, not by Python.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from storage import vault_crypto
from storage.models import vault_audit, vault_grants, vault_requests, vault_secrets
from storage.vault_addresses import derive_addresses
from storage.vault_crypto import Sealed

logger = logging.getLogger(__name__)

SKILL_TAG_PREFIX = "skill:"
DEFAULT_ENV_GRANT_TTL_SECONDS = 300
DEFAULT_TAG_GRANT_TTL_SECONDS = 900
DEFAULT_GRANT_TTL_SECONDS = {
    "env": DEFAULT_ENV_GRANT_TTL_SECONDS,
    "tag": DEFAULT_TAG_GRANT_TTL_SECONDS,
    "skill": DEFAULT_TAG_GRANT_TTL_SECONDS,
}
GRANT_TTL_OPTIONS_SECONDS = (300, 900, 3600)
GRANT_PURPOSES = {"run", "fetch", "inject"}
SUPPORTED_SIGNATURE_SCHEMES = {
    "ecdsa-secp256k1-recoverable",
    "ecdsa-secp256k1-der",
    "schnorr-secp256k1-bip340",
}
REQUEST_AUDIENCE_AGENT = "agent"
REQUEST_AUDIENCE_UI = "ui"
REQUEST_AUDIENCES = {REQUEST_AUDIENCE_AGENT, REQUEST_AUDIENCE_UI}
PROVISION_SPEC_FORBIDDEN_KEYS = {
    "value",
    "sealed",
    "envelope",
    "blind_box",
    "ciphertext",
    "nonce",
    "wrap_meta",
    "private_key",
    "secret",
}
PROVISION_SPEC_ALLOWED_KEYS = {
    "kind",
    "protection",
    "description",
    "tags",
    "policy",
    "links",
}
PROVISION_SPEC_ALLOWED_POLICY_KEYS = {"allowed_hosts", "auth"}
PROVISION_SPEC_ALLOWED_AUTH_KEYS = {"type", "name"}


@dataclass(frozen=True)
class GrantApproval:
    grant_id: str
    members: list[str]
    session_id: str | None
    source_selector: dict[str, Any]
    purpose: str
    one_shot: bool
    ttl_cap_seconds: int


@dataclass(frozen=True)
class RequestGrantOption:
    grant_id: str
    members: list[str]
    source_selector: dict[str, Any]
    purpose: str
    one_shot: bool
    ttl_cap_seconds: int


@dataclass(frozen=True)
class GrantReadiness:
    active: bool
    persisted_agent_ready: bool
    runtime_agent_ready: bool
    standard_ready: bool
    delivery_ready: bool
    delivery_status: str


@dataclass(frozen=True)
class CardHydrationPolicy:
    audience: str
    include_protected_unlock_material: bool


class VaultServiceError(Exception):
    """Base class for vault data-layer errors."""


class InvalidSecretNameError(VaultServiceError):
    pass


class SecretExistsError(VaultServiceError):
    pass


class SecretNameCaseConflictError(VaultServiceError):
    def __init__(self, name: str, existing_name: str):
        self.name = name
        self.existing_name = existing_name
        super().__init__(f"secret name {name!r} conflicts with existing name {existing_name!r}")


class SecretNotFoundError(VaultServiceError):
    pass


class RequestNotFoundError(VaultServiceError):
    pass


class InvalidRequestError(VaultServiceError):
    pass


class UnsupportedProtectionError(VaultServiceError):
    """A caller attempted the wrong delivery path for a protection tier."""


class KeypairNotValueDeliverableError(VaultServiceError):
    """A signing key was requested through a value-delivery path."""


class InvalidGrantError(VaultServiceError):
    pass


class GrantNotFoundError(VaultServiceError):
    pass


class GrantNotActiveError(VaultServiceError):
    pass


class NotGrantableError(VaultServiceError):
    pass


class VaultAlreadyInitializedError(VaultServiceError):
    pass


class VaultGrantRuntimeCache:
    """Process-local grant delivery readiness tracker.

    This intentionally stores no DEKs, plaintext, or browser-unwrapped key material.
    The resident avault agent owns the actual DEK cache; this cache only remembers
    which persisted grant rows should be deliverable through that agent process.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._members_by_grant: dict[str, set[str]] = {}
        self._expires_at_by_grant: dict[str, datetime] = {}
        self._timers_by_grant: dict[str, threading.Timer] = {}

    def put(self, grant_id: str, members: list[str] | set[str], *, expires_at: str | None = None) -> None:
        expires_at_dt = _parse_iso_datetime(expires_at)
        with self._lock:
            self._drop_locked(grant_id)
            if expires_at_dt is not None and expires_at_dt <= datetime.now(timezone.utc):
                return
            self._members_by_grant[grant_id] = set(members)
            if expires_at_dt is not None:
                self._expires_at_by_grant[grant_id] = expires_at_dt
                delay = max(0.0, (expires_at_dt - datetime.now(timezone.utc)).total_seconds())
                timer = threading.Timer(delay, self.drop, args=(grant_id,))
                timer.daemon = True
                self._timers_by_grant[grant_id] = timer
                timer.start()

    def has(self, grant_id: str, secret_name: str) -> bool:
        with self._lock:
            self._drop_if_expired_locked(grant_id)
            return secret_name in self._members_by_grant.get(grant_id, set())

    def get(self, grant_id: str, secret_name: str) -> str | None:
        """No Python-owned protected key material is available from this cache."""
        with self._lock:
            self._drop_if_expired_locked(grant_id)
            return None

    def covered_names(self, grant_id: str) -> list[str]:
        with self._lock:
            self._drop_if_expired_locked(grant_id)
            return sorted(self._members_by_grant.get(grant_id, set()))

    def drop(self, grant_id: str) -> None:
        with self._lock:
            self._drop_locked(grant_id)

    def clear(self) -> None:
        with self._lock:
            for timer in self._timers_by_grant.values():
                timer.cancel()
            self._members_by_grant.clear()
            self._expires_at_by_grant.clear()
            self._timers_by_grant.clear()

    def _drop_locked(self, grant_id: str) -> None:
        self._members_by_grant.pop(grant_id, None)
        self._expires_at_by_grant.pop(grant_id, None)
        timer = self._timers_by_grant.pop(grant_id, None)
        if timer is not None:
            timer.cancel()

    def _drop_if_expired_locked(self, grant_id: str) -> None:
        expires_at = self._expires_at_by_grant.get(grant_id)
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            self._drop_locked(grant_id)


GRANT_RUNTIME_CACHE = VaultGrantRuntimeCache()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _secret_name_case_key(name: str) -> str:
    # Secret names are ASCII shell identifiers, so SQL lower() and Python lower()
    # have the same case-folding behavior for the enforced domain.
    return name.lower()


def _find_secret_name_case_insensitive(conn: Connection, name: str) -> str | None:
    return conn.execute(
        select(vault_secrets.c.name)
        .where(func.lower(vault_secrets.c.name) == _secret_name_case_key(name))
        .limit(1)
    ).scalar_one_or_none()


def _find_pending_provision_name_case_insensitive(conn: Connection, name: str) -> str | None:
    return conn.execute(
        select(vault_requests.c.secret_name)
        .where(
            vault_requests.c.request_type == "provision",
            vault_requests.c.status == "pending",
            func.lower(vault_requests.c.secret_name) == _secret_name_case_key(name),
        )
        .order_by(vault_requests.c.created_at.desc(), vault_requests.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _preflight_secret_create_name(
    conn: Connection,
    *,
    name: str,
    provision_request_id: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    existing_name = _find_secret_name_case_insensitive(conn, name)
    existing_secret = existing_name == name
    if existing_name is not None and existing_name != name:
        raise SecretNameCaseConflictError(name, existing_name)
    provision_row: dict[str, Any] | None = None
    if provision_request_id:
        _expire_pending_requests(conn)
        provision_row = _load_request_row(conn, provision_request_id)
        if provision_row.get("request_type") != "provision":
            raise InvalidRequestError("secret create must complete a provision request")
        if provision_row.get("secret_name") != name:
            raise InvalidRequestError("provision request secret name does not match")
        if provision_row.get("status") == "expired":
            raise InvalidRequestError("provision request has expired")
        if provision_row.get("status") == "fulfilled" and existing_secret:
            raise SecretExistsError(name)
        if provision_row.get("status") != "pending":
            raise InvalidRequestError("provision request is not pending")
    pending_name = _find_pending_provision_name_case_insensitive(conn, name)
    if pending_name is not None and pending_name != name:
        raise SecretNameCaseConflictError(name, pending_name)
    if existing_secret:
        raise SecretExistsError(name)
    return provision_row, bool(existing_secret)


def preflight_secret_create(
    conn: Connection,
    *,
    name: str,
    provision_request_id: str | None = None,
) -> None:
    """Validate name/request conflicts before a caller performs expensive sealing."""

    if not vault_crypto.is_valid_secret_name(name):
        raise InvalidSecretNameError(name)
    _preflight_secret_create_name(conn, name=name, provision_request_id=provision_request_id)


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _public_meta(raw: str | None) -> dict[str, Any]:
    payload = _loads(raw)
    return payload if isinstance(payload, dict) else {}


def _reject_provision_spec_secret_fields(value: Any, *, path: str = "spec") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in PROVISION_SPEC_FORBIDDEN_KEYS:
                raise VaultServiceError(f"{path}.{key} is not allowed in vault request spec")
            _reject_provision_spec_secret_fields(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_provision_spec_secret_fields(item, path=f"{path}[{index}]")


def _string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise VaultServiceError(f"{field} must be an array of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise VaultServiceError(f"{field} must be an array of strings")
        stripped = item.strip()
        if stripped:
            out.append(stripped)
    return out


def _normalize_tag(value: str, *, field: str = "tag") -> str:
    tag = value.strip()
    if not tag:
        raise VaultServiceError(f"{field} must be a non-empty string")
    if any(ch.isspace() for ch in tag):
        raise VaultServiceError(f"{field} must not contain whitespace")
    return tag


def skill_tag(skill: str) -> str:
    skill_name = _normalize_tag(skill, field="skill")
    if skill_name.startswith(SKILL_TAG_PREFIX):
        return skill_name
    return f"{SKILL_TAG_PREFIX}{skill_name}"


def _normalize_tags(tags: list[str] | None) -> list[str]:
    return list(dict.fromkeys(_normalize_tag(tag) for tag in (tags or []) if isinstance(tag, str) and tag.strip()))


def _row_tags(row: dict[str, Any]) -> list[str]:
    raw = _loads(row.get("tags")) or []
    if not isinstance(raw, list):
        return []
    return [str(tag) for tag in raw if isinstance(tag, str) and tag]


def _normalize_allowed_host(value: str, *, field: str) -> str:
    raw = value.strip().lower()
    if not raw:
        raise VaultServiceError(f"{field} must contain non-empty host strings")
    leading_dot = raw.startswith(".")
    hostish = raw[1:] if leading_dot else raw
    if "://" in hostish:
        host = urlsplit(hostish).hostname or ""
    elif hostish.startswith("[") or "/" in hostish or "?" in hostish or "#" in hostish or hostish.count(":") == 1:
        host = urlsplit(f"//{hostish}").hostname or ""
    else:
        host = hostish
    if not host or any(ch.isspace() for ch in host) or "/" in host or "*" in host:
        raise VaultServiceError(f"{field} entries must be hostnames, URLs, or host:port values")
    return f".{host}" if leading_dot else host


def _allowed_host_list(value: Any, *, field: str) -> list[str]:
    return list(dict.fromkeys(_normalize_allowed_host(item, field=field) for item in _string_list(value, field=field)))


def _optional_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VaultServiceError(f"{field} must be a string")
    stripped = value.strip()
    return stripped or None


def normalize_provision_spec(spec: Any) -> dict[str, Any]:
    """Return non-secret creation hints for a provision request.

    The request spec is agent-provided metadata only. It can pre-fill the browser
    form and propose skill links, but it must never carry plaintext or sealed
    value material.
    """

    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise VaultServiceError("vault request spec must be a JSON object")
    _reject_provision_spec_secret_fields(spec)
    extra_keys = set(spec) - PROVISION_SPEC_ALLOWED_KEYS
    if extra_keys:
        raise VaultServiceError(f"unsupported vault request spec fields: {', '.join(sorted(extra_keys))}")

    normalized: dict[str, Any] = {}
    kind = spec.get("kind")
    if kind is not None:
        if not isinstance(kind, str):
            raise VaultServiceError("spec.kind must be a string")
        kind = kind.strip().lower()
        if kind != "static":
            raise VaultServiceError("spec.kind currently supports only 'static' for provision requests")
        normalized["kind"] = kind

    protection = spec.get("protection")
    if protection is not None:
        if not isinstance(protection, str):
            raise VaultServiceError("spec.protection must be a string")
        protection = protection.strip().lower()
        if protection not in {"standard", "protected"}:
            raise VaultServiceError("spec.protection must be 'standard' or 'protected'")
        normalized["protection"] = protection

    description = _optional_string(spec.get("description"), field="spec.description") if "description" in spec else None
    if description:
        normalized["description"] = description

    tags = _string_list(spec.get("tags"), field="spec.tags") if "tags" in spec else []
    tags = _normalize_tags(tags)

    policy = spec.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            raise VaultServiceError("spec.policy must be an object")
        extra_policy_keys = set(policy) - PROVISION_SPEC_ALLOWED_POLICY_KEYS
        if extra_policy_keys:
            raise VaultServiceError(f"unsupported spec.policy fields: {', '.join(sorted(extra_policy_keys))}")
        normalized_policy: dict[str, Any] = {}
        allowed_hosts = _allowed_host_list(policy.get("allowed_hosts"), field="spec.policy.allowed_hosts") if "allowed_hosts" in policy else []
        if allowed_hosts:
            normalized_policy["allowed_hosts"] = allowed_hosts
        auth = policy.get("auth")
        if auth is not None:
            if not isinstance(auth, dict):
                raise VaultServiceError("spec.policy.auth must be an object")
            extra_auth_keys = set(auth) - PROVISION_SPEC_ALLOWED_AUTH_KEYS
            if extra_auth_keys:
                raise VaultServiceError(f"unsupported spec.policy.auth fields: {', '.join(sorted(extra_auth_keys))}")
            raw_auth_type = auth.get("type") or "bearer"
            if not isinstance(raw_auth_type, str):
                raise VaultServiceError("spec.policy.auth.type must be a string")
            auth_type = raw_auth_type.strip().lower()
            if auth_type not in {"bearer", "header", "query"}:
                raise VaultServiceError("spec.policy.auth.type must be bearer, header, or query")
            normalized_auth: dict[str, Any] = {"type": auth_type}
            auth_name = _optional_string(auth.get("name"), field="spec.policy.auth.name") if "name" in auth else None
            if auth_type in {"header", "query"}:
                if not auth_name:
                    raise VaultServiceError("spec.policy.auth.name is required for header/query auth")
                normalized_auth["name"] = auth_name
            elif auth_name:
                normalized_auth["name"] = auth_name
            normalized_policy["auth"] = normalized_auth
        if normalized_policy:
            normalized["policy"] = normalized_policy

    links = spec.get("links")
    if links is not None:
        if not isinstance(links, dict):
            raise VaultServiceError("spec.links must be an object")
        extra_link_keys = set(links) - {"skills"}
        if extra_link_keys:
            raise VaultServiceError(f"unsupported spec.links fields: {', '.join(sorted(extra_link_keys))}")
        skills = _string_list(links.get("skills"), field="spec.links.skills") if "skills" in links else []
        if skills:
            normalized_skills = list(dict.fromkeys(_normalize_tag(skill, field="spec.links.skills") for skill in skills))
            tags = list(dict.fromkeys([*tags, *(skill_tag(skill) for skill in normalized_skills)]))
            normalized["links"] = {"skills": normalized_skills}

    if tags:
        normalized["tags"] = tags

    return normalized


def _meta_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Masked, value-free metadata for a secret row."""
    public_meta = _public_meta(row.get("public_meta"))
    kind = row.get("kind")
    protection = row.get("protection")
    payload = {
        "name": row["name"],
        "tags": _row_tags(row),
        "kind": kind,
        "protection": protection,
        "signer_kind": row.get("signer_kind"),
        "source": row.get("source"),
        "access_grantable": _secret_agent_access_grantable(row),
        "per_use_sign": _secret_agent_per_use_signable(row),
        "description": public_meta.get("description"),
        # Policy is non-secret (allowed hosts, auth scheme name) — safe to surface.
        "policy": _loads(row.get("policy")) or {},
        "last_used_at": row.get("last_used_at"),
        "use_count": row.get("use_count"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    pubkey_pin = public_meta.get("avault_pubkey_pin")
    if isinstance(pubkey_pin, dict):
        payload["avault_pubkey_pin"] = {
            key: value
            for key, value in pubkey_pin.items()
            if key in {"public_key", "fingerprint", "attested_at", "attestation"}
        }
    signing_public_key = public_meta.get("signing_public_key")
    if isinstance(signing_public_key, dict):
        # Surface derived receive addresses instead of the raw public key: agents and the
        # UI identify a signing key by address, not hex. Derivation is a pure function of the
        # (public) key; a malformed/legacy key degrades to no addresses, never a hard error.
        public_key = signing_public_key.get("public_key")
        if isinstance(public_key, str) and signing_public_key.get("curve") == "secp256k1":
            try:
                payload["signing_addresses"] = derive_addresses(public_key)
            except Exception:
                logger.warning("failed to derive signing addresses for secret %r", row.get("name"))
    return payload


def _row_sealed(row: dict[str, Any]) -> Sealed:
    return Sealed(ciphertext=row["ciphertext"], nonce=row["nonce"], wrap_meta=row["wrap_meta"])


def _protected_unlock_material(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("protection") != "protected":
        return None
    return {
        "name": row["name"],
        "kind": row.get("kind"),
        "envelope": {
            "ciphertext": row["ciphertext"],
            "nonce": row["nonce"],
            "wrap_meta": row["wrap_meta"],
        },
    }


def _member_version(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "secret_id": row.get("id"),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
    }


def _member_version_matches(row: dict[str, Any], version: dict[str, Any]) -> bool:
    return (
        row.get("name") == version.get("name")
        and row.get("id") == version.get("secret_id")
        and row.get("updated_at") == version.get("updated_at")
    )


def _normalize_request_audience(audience: str | None) -> str:
    return audience if audience in REQUEST_AUDIENCES else REQUEST_AUDIENCE_UI


def _request_audience_from_requester(requester: Any) -> str:
    if not isinstance(requester, dict):
        return REQUEST_AUDIENCE_UI
    source = str(requester.get("source") or "").strip()
    if source in {"agent-cli", "cli"} or requester.get("backend") or requester.get("native_session_id"):
        return REQUEST_AUDIENCE_AGENT
    return REQUEST_AUDIENCE_UI


def _card_hydration_policy(audience: str | None) -> CardHydrationPolicy:
    normalized = _normalize_request_audience(audience)
    return CardHydrationPolicy(
        audience=normalized,
        include_protected_unlock_material=normalized == REQUEST_AUDIENCE_UI,
    )


ACTIVE_GRANT_STATES = {"active", "reserved"}
ACTIVE_GRANT_STATUSES = ACTIVE_GRANT_STATES


def _grant_is_active(row: dict[str, Any]) -> bool:
    if row.get("status") not in ACTIVE_GRANT_STATES:
        return False
    expires_at = _parse_iso_datetime(row.get("expires_at"))
    return expires_at is None or expires_at > datetime.now(timezone.utc)


def grant_row_has_resident_agent_ready(row: dict[str, Any]) -> bool:
    if not _grant_is_active(row):
        return False
    try:
        return int(row.get("agent_ready") or 0) == 1
    except (TypeError, ValueError):
        return False


def _grant_members_are_standard_secrets(conn: Connection, members: list[str]) -> bool:
    if not members:
        return False
    rows = conn.execute(
        select(vault_secrets.c.name, vault_secrets.c.protection).where(vault_secrets.c.name.in_(members))
    ).mappings()
    protection_by_name = {str(item["name"]): item.get("protection") for item in rows}
    return set(protection_by_name) == set(members) and all(
        protection == "standard" for protection in protection_by_name.values()
    )


def _grant_readiness(
    conn: Connection,
    row: dict[str, Any],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    members: list[str] | None = None,
) -> GrantReadiness:
    members = _grant_member_names(row) if members is None else members
    active = _grant_is_active(row)
    persisted_agent_ready = active and grant_row_has_resident_agent_ready(row)
    if persisted_agent_ready and members:
        cache.put(str(row["id"]), members, expires_at=row.get("expires_at"))
    runtime_members = cache.covered_names(str(row["id"])) if active else []
    runtime_agent_ready = persisted_agent_ready and bool(members) and set(members).issubset(set(runtime_members))
    standard_ready = active and _grant_members_are_standard_secrets(conn, members)
    delivery_ready = standard_ready or runtime_agent_ready
    delivery_status = (
        "standard_ready"
        if standard_ready
        else ("agent_cache_ready" if runtime_agent_ready else "agent_cache_unverified")
    )
    return GrantReadiness(
        active=active,
        persisted_agent_ready=persisted_agent_ready,
        runtime_agent_ready=runtime_agent_ready,
        standard_ready=standard_ready,
        delivery_ready=delivery_ready,
        delivery_status=delivery_status,
    )


def _grant_row_payload(
    conn: Connection,
    row: dict[str, Any],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    members = _grant_member_names(row)
    readiness = _grant_readiness(conn, row, cache=cache, members=members)
    grant_id = row["id"]
    runtime_members = cache.covered_names(grant_id) if readiness.active else []
    return {
        "id": grant_id,
        "source_selector": _loads(row.get("source_selector")) or {},
        "session_id": row.get("session_id"),
        "purpose": row.get("purpose"),
        "status": row.get("status"),
        "request_id": row.get("request_id"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "revoked_at": row.get("revoked_at"),
        "member_snapshot": members,
        "member_count": len(members),
        "runtime_member_count": len(runtime_members),
        "delivery_ready": readiness.delivery_ready,
        "delivery_status": readiness.delivery_status,
        "one_shot": bool(int(row.get("one_shot") or 0)),
    }


def _clear_grant_agent_ready(conn: Connection, grant_id: str) -> None:
    conn.execute(
        vault_grants.update()
        .where(vault_grants.c.id == grant_id)
        .values(agent_ready=0, agent_ready_at=None)
    )


def _grant_member_names(row: dict[str, Any]) -> list[str]:
    members = _loads(row.get("member_snapshot")) or []
    if not isinstance(members, list):
        return []
    return [str(name) for name in members if isinstance(name, str) and name]


def _grant_payload(conn: Connection, row: dict[str, Any], *, cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE) -> dict[str, Any]:
    return _grant_row_payload(conn, dict(row), cache=cache)


def _unique_grant_release_refs(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    refs: list[dict[str, str]] = []
    for row in rows:
        key = str(row.get("id") or "")
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        refs.append({"grant_id": key})
    return refs


def _hydrate_card_unlock_material(conn: Connection, row: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    card = dict(card)
    secret_name = row.get("secret_name")
    if secret_name:
        secret_row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == secret_name)).mappings().first()
        if secret_row is not None and (material := _protected_unlock_material(dict(secret_row))) is not None:
            card["secret_unlock_material"] = material
    hydrated_options: list[dict[str, Any]] = []
    for option in card.get("grant_options") or []:
        if not isinstance(option, dict):
            continue
        option_payload = dict(option)
        versions = option_payload.get("member_versions") or []
        materials: list[dict[str, Any]] = []
        if isinstance(versions, list):
            names = [item.get("name") for item in versions if isinstance(item, dict) and isinstance(item.get("name"), str)]
            current_rows = {
                current["name"]: dict(current)
                for current in conn.execute(select(vault_secrets).where(vault_secrets.c.name.in_(names))).mappings()
            }
            for version in versions:
                if not isinstance(version, dict):
                    continue
                current = current_rows.get(version.get("name"))
                if current is None or not _member_version_matches(current, version):
                    continue
                material = _protected_unlock_material(current)
                if material is not None:
                    materials.append(material)
        if materials:
            option_payload["unlock_material"] = materials
        hydrated_options.append(option_payload)
    if hydrated_options:
        card["grant_options"] = hydrated_options
    return card


def _request_row_payload(
    row: dict[str, Any],
    *,
    conn: Connection | None = None,
    audience: str | None = REQUEST_AUDIENCE_UI,
) -> dict[str, Any]:
    requester = _loads(row.get("requester"))
    delivery = _loads(row.get("delivery"))
    card = delivery.get("card") if isinstance(delivery, dict) else None
    policy = _card_hydration_policy(audience)
    if (
        policy.include_protected_unlock_material
        and row.get("status") == "pending"
        and conn is not None
        and isinstance(card, dict)
    ):
        card = _hydrate_card_unlock_material(conn, row, card)
    return {
        "id": row["id"],
        "request_type": row["request_type"],
        "secret_name": row.get("secret_name"),
        "requester": requester if isinstance(requester, dict) else requester,
        "delivery": delivery if isinstance(delivery, dict) else delivery,
        "status": row.get("status"),
        "message_id": row.get("message_id"),
        "created_at": row.get("created_at"),
        "decided_at": row.get("decided_at"),
        "expires_at": row.get("expires_at"),
        "card": card if isinstance(card, dict) else None,
    }


def _request_json_payloads(row: dict[str, Any]) -> tuple[Any, Any]:
    return _loads(row.get("requester")), _loads(row.get("delivery"))


def _request_session_id(row: dict[str, Any]) -> str | None:
    requester, delivery = _request_json_payloads(row)
    for payload in (requester, delivery):
        if isinstance(payload, dict) and payload.get("session_id"):
            return str(payload["session_id"])
    card = delivery.get("card") if isinstance(delivery, dict) else None
    if isinstance(card, dict) and card.get("session_id"):
        return str(card["session_id"])
    return None


def _expire_pending_request_rows(
    conn: Connection,
    rows: list[dict[str, Any]],
    *,
    reason: str = "request-expired",
    delivery_extra: dict[str, Any] | None = None,
) -> int:
    now = _now()
    expired = 0
    for row in rows:
        result = conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == row["id"], vault_requests.c.status == "pending")
            .values(status="expired", decided_at=now, callback_status="pending")
        )
        if result.rowcount != 1:
            continue
        delivery = {"request_type": row.get("request_type")}
        if delivery_extra:
            delivery.update(delivery_extra)
        audit(
            conn,
            reason,
            secret_name=row.get("secret_name"),
            delivery=delivery,
            request_id=row["id"],
        )
        expired += 1
    return expired


def _expire_request_if_due(conn: Connection, row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    expires_at = _parse_iso_datetime(row.get("expires_at"))
    if row.get("status") != "pending" or expires_at is None or expires_at > datetime.now(timezone.utc):
        return row, False
    expired = _expire_pending_request_rows(conn, [row])
    updated = conn.execute(select(vault_requests).where(vault_requests.c.id == row["id"])).mappings().one()
    return dict(updated), expired == 1


def _load_request_row(conn: Connection, request_id: str) -> dict[str, Any]:
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
    if row is None:
        raise RequestNotFoundError(request_id)
    row_dict, _ = _expire_request_if_due(conn, dict(row))
    return row_dict


def _load_request_for_transition(
    conn: Connection,
    request_id: str,
    *,
    request_type: str | None,
    allowed_statuses: set[str],
    wrong_type_message: str,
    wrong_status_message: str,
    expired_message: str,
) -> dict[str, Any]:
    row = _load_request_row(conn, request_id)
    if request_type is not None and row.get("request_type") != request_type:
        raise InvalidRequestError(wrong_type_message)
    if row.get("status") == "expired":
        raise InvalidRequestError(expired_message)
    if row.get("status") not in allowed_statuses:
        raise InvalidRequestError(wrong_status_message)
    return row


def _expire_pending_requests(conn: Connection) -> None:
    for row in conn.execute(select(vault_requests).where(vault_requests.c.status == "pending")).mappings():
        _expire_request_if_due(conn, dict(row))


def _payload_session_id(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("session_id"):
        return str(payload["session_id"])
    return None


# --- Auto-resume callbacks (P4) -------------------------------------------------------------
#
# When a request reaches a terminal state its transition also sets ``callback_status="pending"``.
# The daemon sweep (``core.scheduled_tasks``) drains these: for each it resolves a callback plan
# and enqueues exactly one callback turn to the requesting session — the same entry Agent Run /
# watch / scheduled tasks use — then marks the row ``sent`` / ``skipped``. The atomic
# ``WHERE status='pending'`` claim on every transition makes this exactly-once (a re-resolve
# updates zero rows, so ``callback_status`` is never re-armed).


@dataclass(frozen=True)
class PendingRequestCallback:
    """A resolved request's auto-resume plan: wake ``session_id`` with ``message``."""

    request_id: str
    session_id: str
    message: str


def _request_callback_disabled(row: dict[str, Any]) -> bool:
    requester, _ = _request_json_payloads(row)
    return bool(isinstance(requester, dict) and requester.get("callback_disabled"))


def _build_request_callback_message(row: dict[str, Any]) -> str:
    """Agent-facing text delivered to the requesting session when a request resolves."""
    request_type = str(row.get("request_type") or "")
    status = str(row.get("status") or "")
    request_id = str(row.get("id") or "").strip()
    name = str(row.get("secret_name") or "").strip()
    label = f" '{name}'" if name else ""
    subject = {
        "provision": f"vault request for the secret{label}",
        "access": f"vault access request{label}",
        "sign": f"signature request{label}",
    }.get(request_type, f"vault request{label}")

    if status in {"approved", "fulfilled"}:
        if request_type == "provision":
            usage = f" You can use it, e.g. `vibe vault run --env {name} -- <command>`." if name else ""
            return f"The user provided your {subject}; the secret is now available.{usage} Continue the task."
        if request_type == "access":
            return f"The user approved your {subject}; the grant is ready. Continue the task."
        if request_type == "sign":
            # The public signature is the deliverable — the agent needs it to continue. It's stored
            # in the request; retrieving it by id returns immediately (the request is already done).
            retrieve = f" Retrieve the signature with: vibe vault await {request_id}." if request_id else ""
            return f"The user approved and completed your {subject}.{retrieve} Then continue the task."
        return f"The user approved your {subject}. Continue the task."
    if status == "denied":
        return f"The user declined your {subject}. Do not retry — adjust your approach or ask the user how to proceed."
    if status == "failed":
        # 'failed' is a signing error (transient/crypto/browser), NOT a user decision — retry is fine.
        return f"Your {subject} could not be completed due to a signing error (not a user decision). You may retry if it still makes sense."
    if status == "expired":
        return f"Your {subject} expired without a decision. Re-request it if you still need it, or continue without."
    return ""


def resolve_request_callback(row: dict[str, Any]) -> PendingRequestCallback | None:
    """Plan the auto-resume callback for a resolved request, or ``None`` to skip.

    Skipped when the requester opted out (``--no-callback``), the request has no originating
    session, or the terminal state maps to no message.
    """
    request_id = str(row.get("id") or "").strip()
    if not request_id or _request_callback_disabled(row):
        return None
    session_id = _request_session_id(row)
    if not session_id:
        return None
    message = _build_request_callback_message(row)
    if not message.strip():
        return None
    return PendingRequestCallback(request_id=request_id, session_id=session_id, message=message)


def _request_covering_grant_payloads(
    conn: Connection,
    row: dict[str, Any],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> list[dict[str, Any]]:
    request_members = set(_request_member_names(row))
    session_id = _request_session_id(row)
    if not request_members or not session_id:
        return []
    try:
        purpose = _request_grant_option(row).purpose
    except InvalidRequestError:
        return []

    expire_grants(conn, cache=cache)
    rows = conn.execute(
        select(vault_grants)
        .where(
            vault_grants.c.status.in_(ACTIVE_GRANT_STATES),
            vault_grants.c.session_id == session_id,
            vault_grants.c.purpose == purpose,
        )
        .order_by(vault_grants.c.created_at.desc(), vault_grants.c.id.desc())
    ).mappings()
    grants: list[dict[str, Any]] = []
    for grant_row in rows:
        grant = dict(grant_row)
        grant_members = {str(name) for name in (_loads(grant.get("member_snapshot")) or []) if str(name)}
        if request_members.issubset(grant_members):
            grants.append(_grant_payload(conn, grant, cache=cache))
    return grants


def request_callback_ready(conn: Connection, row: dict[str, Any]) -> bool:
    """Whether a resolved request's callback may be delivered yet (vs. deferred to a later sweep).

    An approved *access* request is only usable once a covering grant is delivery-ready: for a
    protected secret the DEKs are relayed to the resident agent AFTER approval, so resuming the
    agent before then would hand it a grant whose ``delivery_ready`` is still false. Sibling access
    requests approved by the same grant do not own ``vault_grants.request_id``, so readiness follows
    the active grant for the request's session/purpose whose member snapshot covers the request's
    members. Every other terminal state (provision/sign/deny/expire, and standard grants which are
    ready on approval) is deliverable immediately; no active covering grant does not block forever.
    """
    if str(row.get("request_type") or "") == "access" and str(row.get("status") or "") == "approved":
        grants = _request_covering_grant_payloads(conn, row)
        if grants and not any(grant.get("delivery_ready") for grant in grants):
            return False
    return True


def expire_overdue_requests(conn: Connection) -> None:
    """Flip any overdue pending requests to ``expired`` (arming their callback) proactively.

    Expiry is otherwise lazy (only on request reads), so an unattended timed-out request would
    never auto-resume its session until some unrelated read happened to touch it. The callback
    sweep calls this first so overdue requests are expired and picked up in the same pass.
    """
    _expire_pending_requests(conn)


def list_pending_request_callbacks(conn: Connection, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Terminal requests owed an auto-resume callback (``callback_status='pending'``)."""
    query = (
        select(vault_requests)
        .where(vault_requests.c.callback_status == "pending")
        .order_by(vault_requests.c.decided_at, vault_requests.c.id)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = conn.execute(query).mappings()
    return [dict(row) for row in rows]


def mark_request_callback(conn: Connection, request_id: str, *, status: str) -> None:
    """Record the outcome of an auto-resume callback (``sent`` / ``skipped`` / ``failed``)."""
    conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id)
        .values(callback_status=status)
    )


def _request_card(row: dict[str, Any]) -> dict[str, Any]:
    _, delivery = _request_json_payloads(row)
    card = delivery.get("card") if isinstance(delivery, dict) else None
    return card if isinstance(card, dict) else {}


def _request_member_names(row: dict[str, Any]) -> list[str]:
    card = _request_card(row)
    members: list[str] = []
    options = card.get("grant_options") if isinstance(card, dict) else None
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            snapshot = option.get("member_snapshot")
            if isinstance(snapshot, list):
                members.extend(str(name) for name in snapshot if isinstance(name, str) and name)
    if not members and row.get("secret_name"):
        members.append(str(row["secret_name"]))
    return sorted(set(members))


def _request_covers_any_member(row: dict[str, Any], members: set[str]) -> bool:
    if not members:
        return False
    return bool(set(_request_member_names(row)) & members)


def _request_members_are_subset(row: dict[str, Any], members: set[str]) -> bool:
    request_members = set(_request_member_names(row))
    return bool(request_members) and request_members.issubset(members)


def _ttl_cap_from_grant_option(option: dict[str, Any], selector: dict[str, Any]) -> int:
    raw_options = option.get("ttl_options_seconds")
    ttl_options: list[int] = []
    if isinstance(raw_options, list):
        for raw in raw_options:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                ttl_options.append(value)
    if ttl_options:
        return max(ttl_options)
    try:
        default = int(option.get("default_ttl_seconds") or 0)
    except (TypeError, ValueError):
        default = 0
    return max(1, default or _selector_ttl_seconds(selector))


def _request_grant_option(row: dict[str, Any]) -> RequestGrantOption:
    card = _request_card(row)
    options = card.get("grant_options") if isinstance(card, dict) else None
    if not isinstance(options, list) or len(options) != 1 or not isinstance(options[0], dict):
        raise InvalidRequestError("grant approval request is missing a grant option")
    option = options[0]
    grant_id = str(option.get("grant_id") or "").strip()
    if not grant_id:
        raise InvalidRequestError("grant approval request is missing a grant id")
    snapshot = option.get("member_snapshot")
    if not isinstance(snapshot, list):
        raise InvalidRequestError("grant approval request has an invalid member snapshot")
    members = [str(name) for name in snapshot if isinstance(name, str) and name]
    selector = _source_selector_payload(option.get("source_selector") if isinstance(option.get("source_selector"), dict) else {})
    purpose = str(option.get("purpose") or "run")
    if purpose not in GRANT_PURPOSES:
        raise InvalidRequestError("grant approval request has an invalid purpose")
    return RequestGrantOption(
        grant_id=grant_id,
        members=members,
        source_selector=selector,
        purpose=purpose,
        one_shot=bool(option.get("one_shot")),
        ttl_cap_seconds=_ttl_cap_from_grant_option(option, selector),
    )


def _secret_policy(row: dict[str, Any]) -> dict[str, Any]:
    return _loads(row.get("policy")) or {}


def _secret_always_ask(row: dict[str, Any]) -> bool:
    return bool(_secret_policy(row).get("always_ask"))


def _secret_access_requestable(row: dict[str, Any]) -> bool:
    return row.get("kind") != "keypair"


def _secret_access_grantable(row: dict[str, Any]) -> bool:
    if row.get("kind") == "keypair":
        return False
    if _secret_always_ask(row):
        return False
    return True


def _secret_agent_access_grantable(row: dict[str, Any]) -> bool:
    return _secret_access_grantable(row)


def _secret_agent_per_use_signable(row: dict[str, Any]) -> bool:
    if row.get("kind") != "keypair" or row.get("signer_kind") not in (None, "local"):
        return False
    if row.get("protection") != "protected":
        return True
    signing_public_key = _public_meta(row.get("public_meta")).get("signing_public_key")
    return (
        isinstance(signing_public_key, dict)
        and signing_public_key.get("curve") == "secp256k1"
        and isinstance(signing_public_key.get("public_key"), str)
        and bool(signing_public_key.get("public_key"))
    )


def sign_headless_allowed(row: dict[str, Any]) -> bool:
    """A standard local keypair with no ``always_ask`` policy signs per-use without approval.

    Mirrors the standard *access* headless fast path (``protection == "standard"`` and not
    always_ask). Protected keys always sign in the browser under approval; a standard key
    whose policy sets ``always_ask`` is opted back into per-use approval (the reserved toggle).
    """
    if row.get("kind") != "keypair" or (row.get("signer_kind") or "local") != "local":
        return False
    if row.get("protection") != "standard":
        return False
    return not _secret_always_ask(row)


def sign_needs_approval(conn: Connection, name: str) -> bool:
    """True when `vibe vault sign` must create a pending approval request for `name`."""
    return not sign_headless_allowed(_require_row(conn, name))


def _reject_unsignable_keypair(row: dict[str, Any], name: str) -> None:
    if not _secret_agent_per_use_signable(row):
        raise InvalidRequestError(f"{name} is not per-use signable")


def _reject_keypair_value_delivery(row: dict[str, Any], name: str) -> None:
    if row.get("kind") == "keypair":
        raise KeypairNotValueDeliverableError(f"{name} is a signing key; use vault_sign instead of value delivery")


def audit(
    conn: Connection,
    event: str,
    *,
    secret_name: str | None = None,
    requester: Any = None,
    delivery: Any = None,
    request_id: str | None = None,
    grant_id: str | None = None,
) -> None:
    """Append one audit row. Callers pass only non-secret summaries."""
    conn.execute(
        vault_audit.insert().values(
            id=_id("vau"),
            ts=_now(),
            event=event,
            secret_name=secret_name,
            requester=json.dumps(requester) if requester is not None else None,
            delivery=json.dumps(delivery) if delivery is not None else None,
            request_id=request_id,
            grant_id=grant_id,
        )
    )


def _require_row(conn: Connection, name: str) -> dict[str, Any]:
    row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().first()
    if row is None:
        raise SecretNotFoundError(name)
    return dict(row)


def fulfill_pending_provision_requests_for_secret(
    conn: Connection,
    name: str,
    *,
    decided_at: str | None = None,
) -> int:
    result = conn.execute(
        vault_requests.update()
        .where(
            vault_requests.c.request_type == "provision",
            vault_requests.c.secret_name == name,
            vault_requests.c.status == "pending",
        )
        .values(status="fulfilled", decided_at=decided_at or _now(), callback_status="pending")
    )
    return int(result.rowcount or 0)


def create_secret(
    conn: Connection,
    *,
    name: str,
    sealed: Sealed,
    tags: list[str] | None = None,
    protection: str = "standard",
    kind: str = "static",
    signer_kind: str | None = None,
    description: str | None = None,
    source: str = "manual",
    policy: dict[str, Any] | None = None,
    public_meta: dict[str, Any] | None = None,
    establishing_vmk: bool = False,
    provision_request_id: str | None = None,
) -> dict[str, Any]:
    """Create a secret from a caller-supplied encrypted envelope; return masked metadata.

    For ``standard`` secrets, the envelope is produced by the avault client. For
    ``protected`` secrets, the browser has already encrypted the value and built the
    opaque ``wrap_meta``. This layer never sees plaintext or keys, and stores no
    value-derived metadata.

    ``policy`` is a non-secret JSON dict (e.g. ``allowed_hosts`` + ``auth`` scheme for
    the brokered ``fetch`` mode); it never contains the value.
    """
    if not vault_crypto.is_valid_secret_name(name):
        raise InvalidSecretNameError(name)
    if protection not in {"standard", "protected"}:
        raise VaultServiceError(f"invalid protection tier: {protection!r}")
    if kind not in {"static", "keypair"}:
        raise VaultServiceError(f"invalid vault secret kind: {kind!r}")
    if kind != "keypair" and signer_kind is not None:
        raise VaultServiceError("signer_kind is only valid for keypair secrets")
    provision_row, _existing_secret = _preflight_secret_create_name(
        conn,
        name=name,
        provision_request_id=provision_request_id,
    )

    if establishing_vmk and protection == "protected":
        # Atomic single-init guard: this runs inside the write transaction (SQLite
        # serialises writers), so two concurrent first-time setups cannot both pass —
        # the loser is rejected instead of splitting the vault key history with a
        # second VMK. The browser then reloads and unlocks the established vault.
        if (
            conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.protection == "protected").limit(1)).first()
            is not None
        ):
            raise VaultAlreadyInitializedError("a protected vault already exists; unlock it instead of re-initializing")

    now = _now()
    normalized_tags = _normalize_tags(tags)
    public_meta = dict(public_meta or {})
    if description:
        public_meta["description"] = description
    try:
        conn.execute(
            vault_secrets.insert().values(
                id=_id("vlt"),
                name=name,
                tags=json.dumps(normalized_tags) if normalized_tags else None,
                kind=kind,
                protection=protection,
                signer_kind=signer_kind,
                source=source,
                ciphertext=sealed.ciphertext,
                nonce=sealed.nonce,
                wrap_meta=sealed.wrap_meta,
                public_meta=json.dumps(public_meta) if public_meta else None,
                policy=json.dumps(policy) if policy else None,
                use_count=0,
                created_at=now,
                updated_at=now,
            )
        )
    except IntegrityError as exc:
        # Two concurrent creates (e.g. Web dialog + inline card) can both pass the
        # existence check above; the loser hits the exact UNIQUE(name) or the
        # folded-name unique index here. Re-read the winning name so callers keep
        # the same exact-duplicate vs case-conflict semantics instead of seeing a 500.
        existing_name = _find_secret_name_case_insensitive(conn, name)
        if existing_name is not None and existing_name != name:
            raise SecretNameCaseConflictError(name, existing_name) from exc
        raise SecretExistsError(name) from exc
    audit(conn, "created", secret_name=name)
    decided_at = _now()
    if provision_row is not None:
        result = conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == provision_row["id"], vault_requests.c.status == "pending")
            .values(status="fulfilled", decided_at=decided_at, callback_status="pending")
        )
        if result.rowcount != 1:
            raise InvalidRequestError("provision request is not pending")
    # Once the secret exists, every same-name pending provision ask is satisfied. A
    # request-specific create still uses only that request's spec for the secret metadata,
    # but sibling waiters should not keep timing out or resurfacing stale rows.
    fulfill_pending_provision_requests_for_secret(conn, name, decided_at=decided_at)
    return _meta_payload(_require_row(conn, name))


def link_secret_to_skills(conn: Connection, secret_name: str, skills: list[str], *, source: str = "agent") -> None:
    if not skills:
        return
    row = _require_row(conn, secret_name)
    current = _row_tags(row)
    updated = list(dict.fromkeys([*current, *(skill_tag(skill) for skill in skills if skill and skill.strip())]))
    if updated == current:
        return
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == secret_name)
        .values(tags=json.dumps(updated) if updated else None)
    )
    audit(conn, "tags-updated", secret_name=secret_name, delivery={"source": source, "tags": updated})


def update_secret_tags(conn: Connection, secret_name: str, tags: list[str]) -> dict[str, Any]:
    row = _require_row(conn, secret_name)
    normalized = _normalize_tags(tags)
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == secret_name)
        .values(tags=json.dumps(normalized) if normalized else None)
    )
    audit(conn, "tags-updated", secret_name=secret_name, delivery={"tags": normalized})
    return _meta_payload(dict(row) | {"tags": json.dumps(normalized) if normalized else None})


def update_secret_classification(
    conn: Connection,
    secret_name: str,
    *,
    kind: str | None = None,
    protection: str | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    row = _require_row(conn, secret_name)
    values: dict[str, Any] = {}
    if kind is not None:
        normalized_kind = kind.strip().lower()
        if normalized_kind not in {"static", "keypair"}:
            raise VaultServiceError(f"invalid vault secret kind: {kind!r}")
        values["kind"] = normalized_kind
    if protection is not None:
        normalized_protection = protection.strip().lower()
        if normalized_protection not in {"standard", "protected"}:
            raise VaultServiceError(f"invalid protection tier: {protection!r}")
        values["protection"] = normalized_protection
    if not values:
        return _meta_payload(row)
    if values.get("kind", row.get("kind")) != row.get("kind") or values.get("protection", row.get("protection")) != row.get("protection"):
        _expire_pending_requests_for_secret(conn, secret_name, reason="request-expired-classification-changed")
        _expire_active_grants_for_secret(conn, secret_name, cache=cache, reason="grant-expired-classification-changed")
    values["updated_at"] = _now()
    conn.execute(vault_secrets.update().where(vault_secrets.c.name == secret_name).values(**values))
    audit(conn, "updated", secret_name=secret_name, delivery={"fields": sorted(values)})
    return _meta_payload(_require_row(conn, secret_name))


def get_secret_meta(conn: Connection, name: str) -> dict[str, Any]:
    return _meta_payload(_require_row(conn, name))


def get_signing_public_key(conn: Connection, name: str) -> dict[str, Any] | None:
    """Raw pinned signing public key ({curve, public_key}) from storage.

    The masked meta payload exposes only derived addresses (not the raw key), so
    server-side signature verification reads the pinned key from here instead.
    Returns ``None`` when the secret has no pinned signing key.
    """
    public_meta = _public_meta(_require_row(conn, name).get("public_meta"))
    signing_public_key = public_meta.get("signing_public_key")
    return signing_public_key if isinstance(signing_public_key, dict) else None


def store_pubkey_pin(conn: Connection, name: str, pin: dict[str, Any]) -> dict[str, Any]:
    """Store avault pubkey pin/attestation metadata without touching value fields."""
    row = _require_row(conn, name)
    public_meta = _public_meta(row.get("public_meta"))
    public_meta["avault_pubkey_pin"] = {
        key: value
        for key, value in pin.items()
        if key in {"public_key", "fingerprint", "attested_at", "attestation"}
    }
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(public_meta=json.dumps(public_meta))
    )
    audit(
        conn,
        "pubkey_pinned",
        secret_name=name,
        delivery={"fingerprint": public_meta["avault_pubkey_pin"].get("fingerprint")},
    )
    return get_secret_meta(conn, name)


def list_secrets(conn: Connection, *, tag: str | None = None) -> list[dict[str, Any]]:
    """Masked, value-free list. Never decrypts."""
    query = select(vault_secrets).order_by(vault_secrets.c.name)
    rows = [_meta_payload(dict(row)) for row in conn.execute(query).mappings()]
    if tag is not None:
        normalized = _normalize_tag(tag)
        rows = [row for row in rows if normalized in row.get("tags", [])]
    return rows


def latest_protected_vmk_wrap_meta(conn: Connection) -> str | None:
    """Return the newest protected-tier VMK wrap metadata as opaque JSON text."""
    return conn.execute(
        select(vault_secrets.c.wrap_meta)
        .where(vault_secrets.c.protection == "protected", vault_secrets.c.wrap_meta.is_not(None))
        .order_by(vault_secrets.c.updated_at.desc(), vault_secrets.c.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def rotate_secret(
    conn: Connection,
    name: str,
    sealed: Sealed,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    row = _require_row(conn, name)
    public_meta = _public_meta(row.get("public_meta"))
    public_meta.pop("preview", None)
    _expire_pending_requests_for_secret(conn, name, reason="request-expired-envelope-changed")
    _expire_active_grants_for_secret(conn, name, cache=cache, reason="grant-expired-envelope-changed")
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            wrap_meta=sealed.wrap_meta,
            public_meta=json.dumps(public_meta) if public_meta else None,
            updated_at=_now(),
        )
    )
    audit(conn, "updated", secret_name=name)
    return _meta_payload(_require_row(conn, name))


def delete_secret(conn: Connection, name: str, *, cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE) -> None:
    row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().first()
    if row is None:
        raise SecretNotFoundError(name)
    _expire_pending_requests_for_secret(conn, name, reason="request-expired-envelope-changed")
    _expire_active_grants_for_secret(conn, name, cache=cache, reason="grant-expired-envelope-changed")
    conn.execute(vault_secrets.delete().where(vault_secrets.c.name == name))
    audit(conn, "deleted", secret_name=name)


def get_secret_policy(conn: Connection, name: str) -> dict[str, Any]:
    """Return the secret's non-secret policy dict (allowed_hosts, auth scheme)."""
    return _loads(_require_row(conn, name).get("policy")) or {}


def get_envelope(conn: Connection, name: str) -> Sealed:
    """Return one standard-tier secret's stored envelope (no decrypt, no audit).

    For the brokered ``fetch`` proxy: the caller hands the envelope to the avault
    client (which decrypts + delivers), then records its own ``record_proxy_use``.
    Validate any policy (e.g. host allowlist) *before* delivering.

    Protected delivery must go through :func:`resolve_secret_access` and the
    resident avault agent so Python never opens released DEKs or plaintext.
    """
    row = _require_row(conn, name)
    if row.get("protection") != "standard":
        raise UnsupportedProtectionError(f"{name} is protected-tier; use resident-agent grant delivery")
    _reject_keypair_value_delivery(row, name)
    return _row_sealed(row)


def get_protected_envelope(conn: Connection, name: str) -> Sealed:
    row = _require_row(conn, name)
    if row.get("protection") != "protected":
        raise UnsupportedProtectionError(f"{name} is standard-tier")
    _reject_keypair_value_delivery(row, name)
    return _row_sealed(row)


def record_proxy_use(conn: Connection, name: str, *, requester: Any = None, delivery: Any = None) -> None:
    """Bump usage + write a value-free ``proxied`` audit row after a brokered request."""
    row = _require_row(conn, name)
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(last_used_at=_now(), use_count=vault_secrets.c.use_count + 1)
    )
    audit(conn, "proxied", secret_name=name, requester=requester, delivery=delivery)


def record_signing_use(
    conn: Connection,
    name: str,
    *,
    requester: Any = None,
    delivery: Any = None,
    request_id: str | None = None,
) -> None:
    """Bump signing key usage + write a value-free ``signed`` audit row."""
    _require_row(conn, name)
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(last_used_at=_now(), use_count=vault_secrets.c.use_count + 1)
    )
    audit(conn, "signed", secret_name=name, requester=requester, delivery=delivery, request_id=request_id)


def get_envelopes(conn: Connection, names: list[str]) -> dict[str, Sealed]:
    """Return the stored envelopes for the requested secrets (standard tier; no decrypt).

    Validates the WHOLE batch (all names exist + standard tier) BEFORE returning any, so a
    missing/protected name fails the request as a unit. The caller hands these envelopes to
    the avault client to deliver (child env / file), and records delivery via
    :func:`record_deliveries` only after the delivery side effect succeeds, so a failed
    delivery never shows as delivered. This layer never decrypts.
    """
    out: dict[str, Sealed] = {}
    for name in names:
        row = _require_row(conn, name)
        if row.get("protection") != "standard":
            raise UnsupportedProtectionError(f"{name} is protected-tier; use resident-agent grant delivery")
        _reject_keypair_value_delivery(row, name)
        out[name] = _row_sealed(row)
    return out


def get_key_envelope(conn: Connection, name: str) -> Sealed:
    """Return a locally-stored key envelope for signing.

    This is still envelope-only; the caller hands it to avault (standard tier) or to
    browser-side signing (protected tier). The private key never returns to Python.
    """
    row = _require_row(conn, name)
    if row.get("ciphertext") is None or row.get("nonce") is None or row.get("wrap_meta") is None:
        raise VaultServiceError(f"{name} does not have a local key envelope")
    return _row_sealed(row)


def get_signing_envelope(conn: Connection, name: str) -> Sealed:
    row = _require_row(conn, name)
    if row.get("kind") != "keypair":
        raise InvalidRequestError(f"{name} is not a signing key")
    if row.get("signer_kind") not in (None, "local"):
        raise InvalidRequestError(f"{name} is not locally signable")
    return get_key_envelope(conn, name)


def record_deliveries(conn: Connection, names: list[str], *, requester: Any = None, mode: str | None = None) -> None:
    """Bump usage + write a value-free ``delivered`` audit row per name.

    Call this only AFTER the delivery action (child spawn / file write / stream) succeeds,
    so the audit trail and usage counts never record a delivery that didn't happen.
    """
    for name in names:
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == name)
            .values(last_used_at=_now(), use_count=vault_secrets.c.use_count + 1)
        )
        audit(conn, "delivered", secret_name=name, requester=requester, delivery={"mode": mode})


def create_provision_request(
    conn: Connection,
    name: str,
    *,
    reason: str | None = None,
    spec: dict[str, Any] | None = None,
    requester: Any = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Record an agent's request for a missing secret (dynamic ask).

    If the secret already exists, the request is born ``fulfilled`` — otherwise a
    ``request --wait`` would block forever (a create for an existing name is rejected,
    so nothing would ever flip a pending row).
    """
    if not vault_crypto.is_valid_secret_name(name):
        raise InvalidSecretNameError(name)
    request_id = _id("vrq")
    now = _now()
    existing_name = _find_secret_name_case_insensitive(conn, name)
    if existing_name is not None and existing_name != name:
        raise SecretNameCaseConflictError(name, existing_name)
    already = existing_name == name
    pending_name = _find_pending_provision_name_case_insensitive(conn, name)
    if pending_name is not None and pending_name != name:
        raise SecretNameCaseConflictError(name, pending_name)
    status = "fulfilled" if already else "pending"
    normalized_spec = normalize_provision_spec(spec)
    session_id = requester.get("session_id") if isinstance(requester, dict) else None
    card = _secure_input_card(name, request_id=request_id, reason=reason, spec=normalized_spec, session_id=session_id)
    delivery_payload: dict[str, Any] = {"card": card}
    if reason:
        delivery_payload["reason"] = reason
    if normalized_spec:
        delivery_payload["spec"] = normalized_spec
    try:
        conn.execute(
            vault_requests.insert().values(
                id=request_id,
                request_type="provision",
                secret_name=name,
                requester=json.dumps(requester) if requester is not None else None,
                delivery=json.dumps(delivery_payload),
                status=status,
                message_id=message_id,
                created_at=now,
                decided_at=now if already else None,
            )
        )
    except IntegrityError as exc:
        pending_name = _find_pending_provision_name_case_insensitive(conn, name)
        if pending_name is not None and pending_name != name:
            raise SecretNameCaseConflictError(name, pending_name) from exc
        raise VaultServiceError("failed to create provision request") from exc
    audit(conn, "provision_requested", secret_name=name, requester=requester, request_id=request_id)
    return {
        "id": request_id,
        "secret_name": name,
        "status": status,
        "created_at": now,
        "card": card,
    }


def fulfill_provision(
    conn: Connection,
    request_id: str,
    sealed: Sealed,
    *,
    description: str | None = None,
) -> dict[str, Any]:
    """Store the caller-sealed value for a pending provision request."""
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
    if row is None:
        raise RequestNotFoundError(request_id)
    meta = create_secret(
        conn,
        name=row["secret_name"],
        sealed=sealed,
        description=description,
        provision_request_id=request_id,
    )
    return meta


def _secure_input_card(
    name: str,
    *,
    request_id: str,
    reason: str | None = None,
    spec: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    normalized_spec = normalize_provision_spec(spec)
    card = {
        "card_type": "secure_input",
        "request_id": request_id,
        "secret_name": name,
        # Carry the requesting session so surfaces can scope the card to its chat
        # (mirrors approval_card); the Vaults page ignores it.
        "session_id": session_id,
        "reason": reason,
        "protection_options": ["standard", "protected"],
        "default_protection": normalized_spec.get("protection") or "protected",
        "value": None,
    }
    if normalized_spec:
        card["spec"] = normalized_spec
    return card


def _source_selector_payload(source_selector: dict[str, Any] | None = None) -> dict[str, Any]:
    if source_selector is None:
        return {"env": [], "tags": []}
    if not isinstance(source_selector, dict):
        raise InvalidGrantError("source_selector must be an object")
    env = _string_list(source_selector.get("env"), field="source_selector.env") if "env" in source_selector else []
    tags = _string_list(source_selector.get("tags"), field="source_selector.tags") if "tags" in source_selector else []
    skills = _string_list(source_selector.get("skills"), field="source_selector.skills") if "skills" in source_selector else []
    normalized_tags = _normalize_tags(tags)
    normalized_tags = list(dict.fromkeys([*normalized_tags, *(skill_tag(skill) for skill in skills)]))
    payload: dict[str, Any] = {}
    if env:
        payload["env"] = env
    if normalized_tags:
        payload["tags"] = normalized_tags
    return payload or {"env": [], "tags": []}


def _selector_ttl_seconds(source_selector: dict[str, Any]) -> int:
    tags = source_selector.get("tags")
    if isinstance(tags, list) and tags:
        if any(isinstance(tag, str) and tag.startswith(SKILL_TAG_PREFIX) for tag in tags):
            return DEFAULT_GRANT_TTL_SECONDS["skill"]
        return DEFAULT_GRANT_TTL_SECONDS["tag"]
    return DEFAULT_GRANT_TTL_SECONDS["env"]


def _parse_env_selector(spec: str) -> tuple[str, str]:
    raw = spec.strip()
    if not raw:
        raise InvalidRequestError("env selector must be non-empty")
    if "=" in raw:
        env_name, secret_name = raw.split("=", 1)
        env_name = env_name.strip()
        secret_name = secret_name.strip()
    else:
        env_name = secret_name = raw
    if not _is_delivery_env_name(env_name) or not vault_crypto.is_valid_secret_name(secret_name):
        raise InvalidRequestError("env selector must use a valid env alias and secret name")
    return env_name, secret_name


def _is_delivery_env_name(name: str) -> bool:
    if not name or not name[0].isascii() or not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(char.isascii() and (char.isalnum() or char == "_") for char in name)


def expand_value_delivery_selector(
    conn: Connection,
    *,
    env: list[str] | None = None,
    tags: list[str] | None = None,
    skills: list[str] | None = None,
    source_selector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selector = _source_selector_payload(source_selector or {"env": env or [], "tags": tags or [], "skills": skills or []})
    selections: list[dict[str, Any]] = []
    env_by_secret: dict[str, str] = {}
    secret_by_env: dict[str, str] = {}

    def add_selection(row: dict[str, Any], env_name: str) -> None:
        secret_name = str(row["name"])
        _reject_keypair_value_delivery(row, secret_name)
        existing_env = env_by_secret.get(secret_name)
        if existing_env is not None:
            if existing_env != env_name:
                raise InvalidRequestError(f"{secret_name} was selected with conflicting env names")
            return
        existing_secret = secret_by_env.get(env_name)
        if existing_secret is not None and existing_secret != secret_name:
            raise InvalidRequestError(f"env {env_name} maps to multiple vault secrets")
        env_by_secret[secret_name] = env_name
        secret_by_env[env_name] = secret_name
        selections.append(
            {
                "name": secret_name,
                "env": env_name,
                "kind": row.get("kind"),
                "protection": row.get("protection"),
            }
        )

    for env_spec in selector.get("env") or []:
        env_name, secret_name = _parse_env_selector(str(env_spec))
        add_selection(_require_row(conn, secret_name), env_name)

    for tag in selector.get("tags") or []:
        normalized = _normalize_tag(str(tag))
        rows = [
            dict(row)
            for row in conn.execute(select(vault_secrets).order_by(vault_secrets.c.name)).mappings()
            if normalized in _row_tags(dict(row))
        ]
        for row in rows:
            add_selection(row, str(row["name"]))

    return {"source_selector": selector, "secrets": selections}


def _request_member_rows_for_selector(
    conn: Connection,
    *,
    source_selector: dict[str, Any],
) -> list[dict[str, Any]]:
    expanded = expand_value_delivery_selector(conn, source_selector=source_selector)
    rows: list[dict[str, Any]] = []
    for item in expanded["secrets"]:
        row = _require_row(conn, str(item["name"]))
        if row.get("protection") == "protected" or _secret_always_ask(row):
            rows.append(row)
    return rows


def _protected_delivery_names(delivery_payload: dict[str, Any]) -> list[str]:
    raw_names = delivery_payload.get("protected_secret_names")
    if not isinstance(raw_names, list):
        return []
    names: list[str] = []
    for raw_name in raw_names:
        if isinstance(raw_name, str) and raw_name:
            names.append(raw_name)
    return list(dict.fromkeys(names))


def _filter_request_rows_to_protected_names(
    rows: list[dict[str, Any]],
    protected_names: list[str],
) -> list[dict[str, Any]]:
    rows_by_name = {str(row["name"]): row for row in rows}
    filtered: list[dict[str, Any]] = []
    for name in protected_names:
        row = rows_by_name.get(name)
        if row is None:
            raise InvalidRequestError("protected_secret_names must be selected by source_selector")
        if row.get("protection") != "protected":
            raise InvalidRequestError("protected_secret_names must name protected secrets")
        filtered.append(row)
    return filtered


def _member_rows_for_names(conn: Connection, member_names: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in member_names:
        row = _require_row(conn, str(name))
        _reject_keypair_value_delivery(row, str(name))
        rows.append(row)
    return rows


def grantable_member_metas(conn: Connection, member_names: list[str]) -> list[dict[str, Any]]:
    return [_meta_payload(row) for row in _member_rows_for_names(conn, member_names) if _secret_access_grantable(row)]


def request_grantable_member_metas(conn: Connection, request_id: str) -> list[dict[str, Any]]:
    row = _load_request_for_transition(
        conn,
        str(request_id),
        request_type="access",
        allowed_statuses={"pending"},
        wrong_type_message="grant approval must complete an access request",
        wrong_status_message="grant approval request is not pending",
        expired_message="grant approval request has expired",
    )
    option = _request_grant_option(row)
    rows = _member_rows_for_names(conn, option.members)
    return [_meta_payload(row) for row in rows]


def _grant_option(
    rows: list[dict[str, Any]],
    *,
    source_selector: dict[str, Any],
    purpose: str,
    one_shot: bool,
) -> dict[str, Any]:
    members = [str(row["name"]) for row in rows]
    default_ttl_seconds = _selector_ttl_seconds(source_selector)
    option = {
        "grant_id": _id("vgr"),
        "source_selector": source_selector,
        "purpose": purpose,
        "default_ttl_seconds": default_ttl_seconds,
        "ttl_options_seconds": [seconds for seconds in GRANT_TTL_OPTIONS_SECONDS if seconds >= default_ttl_seconds],
        "session_binding_default": True,
        "member_count": len(members),
        "member_snapshot": members,
        "member_versions": [_member_version(row) for row in rows],
    }
    if one_shot:
        option["one_shot"] = True
    return option


def approval_card(
    conn: Connection,
    secret_name: str | None,
    *,
    request_id: str,
    request_type: str = "access",
    member_rows: list[dict[str, Any]] | None = None,
    source_selector: dict[str, Any] | None = None,
    purpose: str = "run",
    command: str | None = None,
    egress: str | None = None,
    skill: str | None = None,
    session_id: str | None = None,
    one_shot: bool = False,
    grantable: bool = True,
) -> dict[str, Any]:
    rows = member_rows if member_rows is not None else ([_require_row(conn, secret_name)] if secret_name else [])
    default_selector = {"env": [secret_name]} if secret_name else None
    selector = _source_selector_payload(source_selector or default_selector)
    grant_options = [_grant_option(rows, source_selector=selector, purpose=purpose, one_shot=one_shot)] if grantable and rows else []
    protected_names = [str(row["name"]) for row in rows if row.get("protection") == "protected"]
    card = {
        "card_type": "approval",
        "request_id": request_id,
        "request_type": request_type,
        "secret_name": secret_name,
        "secret_names": [str(row["name"]) for row in rows],
        "protected_secret_names": protected_names,
        "source_selector": selector,
        "purpose": purpose,
        "command": command,
        "egress": egress,
        "skill": skill,
        "session_id": session_id,
        "approve_once": True,
        "one_shot": one_shot,
        "grant_options": grant_options,
        "value": None,
    }
    if len(rows) == 1:
        card["kind"] = rows[0].get("kind")
        card["protection"] = rows[0].get("protection")
    return card


def create_access_request(
    conn: Connection,
    name: str | None = None,
    *,
    source_selector: dict[str, Any] | None = None,
    purpose: str = "run",
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    message_id: str | None = None,
    expires_at: str | None = None,
    audience: str | None = None,
) -> dict[str, Any]:
    if purpose not in GRANT_PURPOSES:
        raise InvalidRequestError(f"invalid grant purpose: {purpose!r}")
    payload_audience = audience or _request_audience_from_requester(requester)
    request_id = _id("vrq")
    delivery_payload = dict(delivery or {})
    requester_payload = requester if isinstance(requester, dict) else {}
    default_selector = {"env": [name]} if name else None
    selector = _source_selector_payload(source_selector or default_selector)
    rows = _request_member_rows_for_selector(conn, source_selector=selector)
    protected_delivery_names = _protected_delivery_names(delivery_payload)
    if protected_delivery_names:
        rows = _filter_request_rows_to_protected_names(rows, protected_delivery_names)
    if name:
        direct_row = _require_row(conn, name)
        if not _secret_access_requestable(direct_row):
            raise NotGrantableError(f"{name} is not access-requestable")
    if not rows:
        raise NotGrantableError("selector has no protected or approval-required static secrets")
    if len(rows) == 1 and _secret_always_ask(rows[0]) and rows[0].get("protection") == "standard":
        one_shot = True
    else:
        one_shot = any(_secret_always_ask(row) for row in rows)
    card = approval_card(
        conn,
        str(rows[0]["name"]) if len(rows) == 1 else name,
        request_id=request_id,
        request_type="access",
        member_rows=rows,
        source_selector=selector,
        purpose=purpose,
        command=delivery_payload.get("command"),
        egress=delivery_payload.get("egress"),
        skill=delivery_payload.get("skill") or requester_payload.get("skill"),
        session_id=requester_payload.get("session_id") or delivery_payload.get("session_id"),
        one_shot=one_shot,
    )
    delivery_payload["card"] = card
    delivery_payload["source_selector"] = selector
    delivery_payload["purpose"] = purpose
    conn.execute(
        vault_requests.insert().values(
            id=request_id,
            request_type="access",
            secret_name=str(rows[0]["name"]) if len(rows) == 1 else None,
            requester=json.dumps(requester) if requester is not None else None,
            delivery=json.dumps(delivery_payload),
            status="pending",
            message_id=message_id,
            created_at=_now(),
            expires_at=expires_at,
        )
    )
    audit(conn, "access_requested", secret_name=name, requester=requester, delivery=delivery_payload, request_id=request_id)
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(row), conn=conn, audience=payload_audience)


def create_sign_request(
    conn: Connection,
    name: str,
    *,
    digest: str,
    scheme: str,
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    message_id: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    if scheme not in SUPPORTED_SIGNATURE_SCHEMES:
        raise InvalidRequestError(f"unsupported signature scheme: {scheme}")
    row = _require_row(conn, name)
    if row.get("kind") != "keypair":
        raise InvalidRequestError(f"{name} is not a signing key")
    if row.get("signer_kind") not in (None, "local"):
        raise InvalidRequestError(f"{name} is not locally signable")
    _reject_unsignable_keypair(row, name)
    payload_audience = _request_audience_from_requester(requester)
    request_id = _id("vrq")
    delivery_payload = dict(delivery or {})
    requester_payload = requester if isinstance(requester, dict) else {}
    card = approval_card(
        conn,
        name,
        request_id=request_id,
        request_type="sign",
        command=delivery_payload.get("command") or f"sign:{scheme}",
        egress=delivery_payload.get("egress") or "signature",
        skill=delivery_payload.get("skill") or requester_payload.get("skill"),
        session_id=requester_payload.get("session_id") or delivery_payload.get("session_id"),
        grantable=False,
    )
    delivery_payload.update({"digest": digest, "scheme": scheme, "card": card})
    conn.execute(
        vault_requests.insert().values(
            id=request_id,
            request_type="sign",
            secret_name=name,
            requester=json.dumps(requester) if requester is not None else None,
            delivery=json.dumps(delivery_payload),
            status="pending",
            message_id=message_id,
            created_at=_now(),
            expires_at=expires_at,
        )
    )
    audit(conn, "sign_requested", secret_name=name, requester=requester, delivery=delivery_payload, request_id=request_id)
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(row), conn=conn, audience=payload_audience)


def _signature_bytes(raw: str) -> bytes:
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise InvalidRequestError("signature must be hex-encoded bytes") from exc


def _validate_signature_payload(scheme: str, signature: dict[str, Any]) -> None:
    if scheme not in SUPPORTED_SIGNATURE_SCHEMES:
        raise InvalidRequestError(f"unsupported signature scheme: {scheme}")
    if not isinstance(signature, dict):
        raise InvalidRequestError("signature payload must be an object")
    sig = signature.get("signature")
    if not isinstance(sig, str) or not sig:
        raise InvalidRequestError("signature payload requires a non-empty signature")
    sig_bytes = _signature_bytes(sig)
    recovery_id = signature.get("recovery_id")
    if scheme == "ecdsa-secp256k1-recoverable":
        if len(sig_bytes) != 64:
            raise InvalidRequestError("recoverable secp256k1 signatures must be 64 bytes")
        if type(recovery_id) is not int or recovery_id not in {0, 1, 2, 3}:
            raise InvalidRequestError("recoverable secp256k1 signatures require recovery_id 0..3")
        return
    if scheme == "ecdsa-secp256k1-der":
        if len(sig_bytes) < 8 or sig_bytes[0] != 0x30:
            raise InvalidRequestError("DER secp256k1 signatures must be DER-encoded")
        if recovery_id is not None:
            raise InvalidRequestError("DER secp256k1 signatures must not include recovery_id")
        return
    if scheme == "schnorr-secp256k1-bip340":
        if len(sig_bytes) != 64:
            raise InvalidRequestError("BIP340 Schnorr signatures must be 64 bytes")
        if recovery_id is not None:
            raise InvalidRequestError("BIP340 Schnorr signatures must not include recovery_id")
        return

def complete_sign_request(
    conn: Connection,
    request_id: str,
    *,
    name: str,
    digest: str,
    scheme: str,
    signature: dict[str, Any],
    requester: Any = None,
    browser_signed: bool = False,
) -> dict[str, Any]:
    row_dict = _load_request_for_transition(
        conn,
        request_id,
        request_type="sign",
        allowed_statuses={"pending", "signing"},
        wrong_type_message="signature completion must target a sign request",
        wrong_status_message="sign request is not pending",
        expired_message="sign request has expired",
    )
    if row_dict.get("secret_name") != name:
        raise InvalidRequestError("signature secret does not match the sign request")
    _, delivery = _request_json_payloads(row_dict)
    delivery_payload = delivery if isinstance(delivery, dict) else {}
    if delivery_payload.get("digest") != digest or delivery_payload.get("scheme") != scheme:
        raise InvalidRequestError("signature payload does not match the sign request")
    _validate_signature_payload(scheme, signature)
    completed_delivery = dict(delivery_payload)
    completed_delivery["signature"] = signature
    claim = conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id, vault_requests.c.request_type == "sign", vault_requests.c.status == row_dict["status"])
        .values(status="approved", decided_at=_now(), delivery=json.dumps(completed_delivery), callback_status="pending")
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("sign request is not pending")
    record_signing_use(
        conn,
        name,
        requester=requester,
        delivery={"scheme": scheme, "digest": digest, "browser_signed": bool(browser_signed)},
        request_id=request_id,
    )
    updated = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(updated), conn=conn, audience=REQUEST_AUDIENCE_AGENT)


def get_request(conn: Connection, request_id: str, *, audience: str | None = REQUEST_AUDIENCE_UI) -> dict[str, Any]:
    row_dict = _load_request_row(conn, request_id)
    return _request_row_payload(row_dict, conn=conn, audience=audience)


def claim_sign_request(
    conn: Connection,
    request_id: str,
    *,
    name: str,
    digest: str,
    scheme: str,
) -> dict[str, Any]:
    row_dict = _load_request_for_transition(
        conn,
        request_id,
        request_type="sign",
        allowed_statuses={"pending"},
        wrong_type_message="signature completion must target a sign request",
        wrong_status_message="sign request is not pending",
        expired_message="sign request has expired",
    )
    if row_dict.get("secret_name") != name:
        raise InvalidRequestError("signature secret does not match the sign request")
    _, delivery = _request_json_payloads(row_dict)
    delivery_payload = delivery if isinstance(delivery, dict) else {}
    if delivery_payload.get("digest") != digest or delivery_payload.get("scheme") != scheme:
        raise InvalidRequestError("signature payload does not match the sign request")
    claim = conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id, vault_requests.c.request_type == "sign", vault_requests.c.status == "pending")
        .values(status="signing", decided_at=_now())
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("sign request is not pending")
    updated = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(updated), conn=conn, audience=REQUEST_AUDIENCE_AGENT)


def fail_sign_request(conn: Connection, request_id: str, *, reason: str | None = None) -> dict[str, Any]:
    row_dict = _load_request_for_transition(
        conn,
        request_id,
        request_type="sign",
        allowed_statuses={"signing"},
        wrong_type_message="signature completion must target a sign request",
        wrong_status_message="sign request is not signing",
        expired_message="sign request has expired",
    )
    _, delivery = _request_json_payloads(row_dict)
    delivery_payload = dict(delivery) if isinstance(delivery, dict) else {}
    failure_payload: dict[str, Any] = {"request_type": "sign"}
    if reason:
        failure_payload["reason"] = reason
    delivery_payload["failure"] = failure_payload
    result = conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id, vault_requests.c.status == "signing")
        .values(status="failed", decided_at=_now(), delivery=json.dumps(delivery_payload), callback_status="pending")
    )
    if result.rowcount != 1:
        raise InvalidRequestError("sign request is not signing")
    audit(
        conn,
        "request-failed",
        secret_name=row_dict.get("secret_name"),
        delivery=failure_payload,
        request_id=request_id,
    )
    updated = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(updated), conn=conn, audience=REQUEST_AUDIENCE_AGENT)


def validate_sign_request(
    conn: Connection,
    request_id: str,
    *,
    name: str,
    digest: str,
    scheme: str,
) -> dict[str, Any]:
    row_dict = _load_request_for_transition(
        conn,
        request_id,
        request_type="sign",
        allowed_statuses={"pending"},
        wrong_type_message="signature completion must target a sign request",
        wrong_status_message="sign request is not pending",
        expired_message="sign request has expired",
    )
    if row_dict.get("secret_name") != name:
        raise InvalidRequestError("signature secret does not match the sign request")
    _, delivery = _request_json_payloads(row_dict)
    delivery_payload = delivery if isinstance(delivery, dict) else {}
    if delivery_payload.get("digest") != digest or delivery_payload.get("scheme") != scheme:
        raise InvalidRequestError("signature payload does not match the sign request")
    return _request_row_payload(row_dict, conn=conn, audience=REQUEST_AUDIENCE_AGENT)


def deny_request(
    conn: Connection,
    request_id: str,
    *,
    requester: Any = None,
    reason: str | None = None,
) -> dict[str, Any]:
    row_dict = _load_request_for_transition(
        conn,
        request_id,
        request_type=None,
        allowed_statuses={"pending"},
        wrong_type_message="request is invalid",
        wrong_status_message="request is not pending",
        expired_message="request has expired",
    )
    decided_at = _now()
    result = conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id, vault_requests.c.status == "pending")
        .values(status="denied", decided_at=decided_at, callback_status="pending")
    )
    if result.rowcount != 1:
        raise InvalidRequestError("request is not pending")
    delivery: dict[str, Any] = {"request_type": row_dict.get("request_type")}
    if reason:
        delivery["reason"] = reason
    audit(
        conn,
        "request-denied",
        secret_name=row_dict.get("secret_name"),
        requester=requester,
        delivery=delivery,
        request_id=request_id,
    )
    updated = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(updated), conn=conn, audience=REQUEST_AUDIENCE_AGENT)


def list_requests(
    conn: Connection,
    *,
    status: str | None = "pending",
    request_type: str | None = None,
    limit: int = 100,
    session: str | None = None,
) -> list[dict[str, Any]]:
    _expire_pending_requests(conn)
    query = select(vault_requests).order_by(vault_requests.c.created_at.desc(), vault_requests.c.id.desc())
    if status is not None:
        query = query.where(vault_requests.c.status == status)
    if request_type is not None:
        query = query.where(vault_requests.c.request_type == request_type)
    # session_id lives in the request JSON (not a column), so a session-scoped query must filter
    # in Python BEFORE limiting — else a global page could truncate this session's older rows.
    if session is None:
        query = query.limit(limit)
    rows = [dict(row) for row in conn.execute(query).mappings()]
    if session is not None:
        rows = [row for row in rows if _request_session_id(row) == session][:limit]
    return [_request_row_payload(row, conn=conn, audience=REQUEST_AUDIENCE_UI) for row in rows]


def resolve_pending_provision_request_by_name(conn: Connection, name: str) -> tuple[dict[str, Any] | None, bool]:
    _expire_pending_requests(conn)
    rows = list(
        conn.execute(
            select(vault_requests)
            .where(
                vault_requests.c.status == "pending",
                vault_requests.c.request_type == "provision",
                vault_requests.c.secret_name == name,
            )
            .order_by(vault_requests.c.created_at.desc(), vault_requests.c.id.desc())
            .limit(2)
        ).mappings()
    )
    if len(rows) == 1:
        return _request_row_payload(dict(rows[0]), conn=conn, audience=REQUEST_AUDIENCE_UI), False
    return None, len(rows) > 1


def find_pending_provision_request(conn: Connection, name: str) -> dict[str, Any] | None:
    request, _ambiguous = resolve_pending_provision_request_by_name(conn, name)
    return request


def get_pending_provision_request(conn: Connection, request_id: str) -> dict[str, Any] | None:
    _expire_pending_requests(conn)
    row = (
        conn.execute(
            select(vault_requests)
            .where(
                vault_requests.c.id == request_id,
                vault_requests.c.status == "pending",
                vault_requests.c.request_type == "provision",
            )
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return _request_row_payload(dict(row), conn=conn, audience=REQUEST_AUDIENCE_UI)


def _expire_grant_rows(
    conn: Connection,
    rows: list[dict[str, Any]],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reason: str = "grant-expired",
) -> int:
    now = _now()
    expired = 0
    for row in rows:
        conn.execute(
            vault_grants.update()
            .where(vault_grants.c.id == row["id"], vault_grants.c.status.in_(ACTIVE_GRANT_STATES))
            .values(status="expired", revoked_at=now, agent_ready=0, agent_ready_at=None)
        )
        cache.drop(row["id"])
        audit(conn, reason, grant_id=row["id"], delivery={"grant_id": row["id"]})
        expired += 1
    return expired


def _expire_active_grants_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reason: str = "grant-expired",
) -> int:
    rows = active_grant_rows_for_secret(conn, secret_name)
    return _expire_grant_rows(conn, rows, cache=cache, reason=reason)


def active_grant_rows_for_secret(conn: Connection, secret_name: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES))).mappings()
        if secret_name in (_loads(row.get("member_snapshot")) or [])
    ]


def active_grant_scopes_for_secret(conn: Connection, secret_name: str) -> list[dict[str, str]]:
    return _unique_grant_release_refs(active_grant_rows_for_secret(conn, secret_name))


def active_grant_scopes_for_session(conn: Connection, session_id: str) -> list[dict[str, str]]:
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES), vault_grants.c.session_id == session_id)
        ).mappings()
    ]
    return _unique_grant_release_refs(rows)


def agent_release_scopes_after_rows(
    conn: Connection,
    rows: list[dict[str, Any]],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> list[dict[str, str]]:
    """Return resident-agent grant ids that must be dropped after rows stop being active."""

    now = _now()
    expired_rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES), vault_grants.c.expires_at <= now)
        ).mappings()
    ]
    if expired_rows:
        _expire_grant_rows(conn, expired_rows, cache=cache)
    rows = [*rows, *expired_rows]
    rows = [dict(row) for row in rows if grant_row_has_resident_agent_ready(dict(row))]
    if not rows:
        return []
    release_refs: list[dict[str, str]] = []
    for row in rows:
        grant_id = str(row.get("id") or "")
        if not grant_id:
            continue
        cache.drop(grant_id)
        _clear_grant_agent_ready(conn, grant_id)
        release_refs.append({"grant_id": grant_id})
    return release_refs


def expire_grant(
    conn: Connection,
    grant_id: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reason: str = "grant-expired",
) -> dict[str, Any]:
    row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().first()
    if row is None:
        raise GrantNotFoundError(grant_id)
    row_dict = dict(row)
    if row_dict.get("status") in ACTIVE_GRANT_STATES:
        _expire_grant_rows(conn, [row_dict], cache=cache, reason=reason)
        row_dict = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one())
    return _grant_payload(conn, row_dict, cache=cache)


def _expire_pending_requests_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    reason: str = "request-expired",
) -> int:
    changed_members = {secret_name}
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_requests).where(
                vault_requests.c.status == "pending",
                vault_requests.c.request_type.in_(("access", "sign")),
            )
        ).mappings()
        if row.get("secret_name") == secret_name or _request_covers_any_member(dict(row), changed_members)
    ]
    if not rows:
        return 0
    return _expire_pending_request_rows(conn, rows, reason=reason, delivery_extra={"changed_secret": secret_name})


def expire_session_requests(conn: Connection, session_id: str, *, reason: str = "request-expired-session-archived") -> int:
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_requests).where(
                vault_requests.c.status == "pending",
                vault_requests.c.request_type.in_(("access", "sign")),
            )
        ).mappings()
        if _request_session_id(dict(row)) == session_id
    ]
    if not rows:
        return 0
    return _expire_pending_request_rows(conn, rows, reason=reason, delivery_extra={"session_id": session_id})


def expire_grants(conn: Connection, *, cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE) -> int:
    now = _now()
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES), vault_grants.c.expires_at <= now)
        ).mappings()
    ]
    return _expire_grant_rows(conn, rows, cache=cache)


def _validate_access_request_for_grant(
    conn: Connection,
    request_id: str,
    *,
    session_id: str | None,
    inherit_request_session: bool,
    live_members: list[str],
    source_selector: dict[str, Any],
    purpose: str,
) -> GrantApproval:
    row_dict = _load_request_for_transition(
        conn,
        request_id,
        request_type="access",
        allowed_statuses={"pending"},
        wrong_type_message="grant approval must complete an access request",
        wrong_status_message="grant approval request is not pending",
        expired_message="grant approval request has expired",
    )
    requested_session_id = _request_session_id(row_dict)
    effective_session_id = requested_session_id if session_id is None and inherit_request_session else session_id
    option = _request_grant_option(row_dict)
    if option.one_shot and requested_session_id:
        effective_session_id = requested_session_id
    if requested_session_id and effective_session_id and requested_session_id != effective_session_id:
        raise InvalidRequestError("grant session does not match the approval request")
    if option.purpose != purpose:
        raise InvalidRequestError("grant purpose does not match the approval request")
    if option.source_selector != _source_selector_payload(source_selector):
        raise InvalidRequestError("grant source selector does not match the approval request")
    if set(option.members) != set(live_members):
        raise InvalidRequestError("grant approval snapshot has stale members")
    card = _request_card(row_dict)
    raw_options = card.get("grant_options") if isinstance(card, dict) else None
    raw_option = raw_options[0] if isinstance(raw_options, list) and raw_options and isinstance(raw_options[0], dict) else {}
    versions = raw_option.get("member_versions")
    if not isinstance(versions, list):
        raise InvalidRequestError("grant approval snapshot is stale")
    versions_by_name = {
        item.get("name"): item
        for item in versions
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if set(versions_by_name) != set(option.members):
        raise InvalidRequestError("grant approval snapshot is stale")
    current_rows = {
        current["name"]: dict(current)
        for current in conn.execute(select(vault_secrets).where(vault_secrets.c.name.in_(option.members))).mappings()
    }
    for member_name in option.members:
        current = current_rows.get(member_name)
        if current is None or member_name not in live_members:
            raise InvalidRequestError("grant approval snapshot has stale members")
        if not _member_version_matches(current, versions_by_name[member_name]):
            raise InvalidRequestError("grant approval snapshot has stale members")
    return GrantApproval(
        grant_id=option.grant_id,
        members=option.members,
        session_id=effective_session_id,
        source_selector=option.source_selector,
        purpose=option.purpose,
        one_shot=option.one_shot,
        ttl_cap_seconds=option.ttl_cap_seconds,
    )


def _approve_sibling_access_requests_for_grant(
    conn: Connection,
    *,
    request_id: str,
    members: list[str],
    session_id: str | None,
    purpose: str,
    decided_at: str,
) -> int:
    if not members:
        return 0
    target_session_id = session_id
    if not target_session_id:
        approval_row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
        target_session_id = _request_session_id(dict(approval_row)) if approval_row else None
    if not target_session_id:
        return 0
    member_set = {str(name) for name in members if str(name)}

    def _same_purpose_subset(row: dict[str, Any]) -> bool:
        try:
            option = _request_grant_option(row)
        except InvalidRequestError:
            return False
        return option.purpose == purpose and _request_members_are_subset(row, member_set)

    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_requests).where(
                vault_requests.c.id != request_id,
                vault_requests.c.request_type == "access",
                vault_requests.c.status == "pending",
            )
        ).mappings()
        if _request_session_id(dict(row)) == target_session_id and _same_purpose_subset(dict(row))
    ]
    approved = 0
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        expires_at = _parse_iso_datetime(row.get("expires_at"))
        if expires_at is not None and expires_at <= now_dt:
            _expire_pending_request_rows(
                conn,
                [row],
                delivery_extra={"session_id": target_session_id},
            )
            continue
        result = conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == row["id"], vault_requests.c.status == "pending")
            .values(status="approved", decided_at=decided_at, callback_status="pending")
        )
        if result.rowcount != 1:
            continue
        audit(
            conn,
            "request-approved-by-grant",
            secret_name=row.get("secret_name"),
            delivery={"request_type": "access", "session_id": target_session_id},
            request_id=row["id"],
        )
        approved += 1
    return approved


def restore_access_request_after_failed_grant(
    conn: Connection,
    *,
    request_id: str,
    member_names: list[str] | set[str] | tuple[str, ...],
    session_id: str | None,
) -> int:
    """Make a protected grant approval retryable after resident-agent relay fails."""

    members = {str(name) for name in member_names if str(name)}
    if not request_id or not members:
        return 0
    approval_row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
    if approval_row is None or approval_row.get("status") != "approved":
        return 0
    decided_at = approval_row.get("decided_at")
    target_session_id = session_id or _request_session_id(dict(approval_row))
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_requests).where(
                vault_requests.c.request_type == "access",
                vault_requests.c.status == "approved",
            )
        ).mappings()
        if row["id"] == request_id
        or (
            _request_members_are_subset(dict(row), members)
            and target_session_id
            and row.get("decided_at") == decided_at
            and _request_session_id(dict(row)) == target_session_id
        )
    ]
    restored = 0
    for row in rows:
        result = conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == row["id"], vault_requests.c.status == "approved")
            .values(status="pending", decided_at=None, callback_status=None)
        )
        if result.rowcount != 1:
            continue
        audit(
            conn,
            "request-restored-after-grant-relay-failed",
            secret_name=row.get("secret_name"),
            delivery={"request_type": "access", "session_id": target_session_id},
            request_id=row["id"],
        )
        restored += 1
    return restored


def create_grant(
    conn: Connection,
    *,
    member_names: list[str],
    source_selector: dict[str, Any],
    purpose: str = "run",
    session_id: str | None = None,
    ttl_seconds: int | None = None,
    request_id: str | None = None,
    inherit_request_session: bool = True,
    expected_member_names: set[str] | list[str] | tuple[str, ...] | None = None,
    cache_ready: bool = True,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    if purpose not in GRANT_PURPOSES:
        raise InvalidGrantError(f"invalid grant purpose: {purpose!r}")
    if not request_id:
        raise InvalidRequestError("grant creation requires an approval request")
    selector = _source_selector_payload(source_selector)
    live_rows = _member_rows_for_names(conn, member_names)
    live_members = [row["name"] for row in live_rows]
    if not live_members:
        raise NotGrantableError("grant has no static secrets")
    approval = _validate_access_request_for_grant(
        conn,
        request_id,
        session_id=session_id,
        inherit_request_session=inherit_request_session,
        live_members=live_members,
        source_selector=selector,
        purpose=purpose,
    )
    session_id = approval.session_id
    members = approval.members
    if any(name not in live_members for name in members):
        raise InvalidRequestError("grant approval snapshot has stale members")
    if expected_member_names is not None and set(members) != set(expected_member_names):
        raise InvalidGrantError("resident agent DEKs must match the approved grant members")
    live_rows_by_name = {row["name"]: row for row in live_rows}
    one_shot_grant = approval.one_shot
    resident_cache_ready = cache_ready and any(live_rows_by_name[name].get("protection") == "protected" for name in members)
    decided_at = _now()
    claim = conn.execute(
        vault_requests.update()
        .where(
            vault_requests.c.id == request_id,
            vault_requests.c.request_type == "access",
            vault_requests.c.status == "pending",
        )
        .values(status="approved", decided_at=decided_at, callback_status="pending")
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("grant approval request is not pending")
    ttl = int(ttl_seconds or _selector_ttl_seconds(selector))
    ttl = max(1, min(ttl, approval.ttl_cap_seconds))
    now_dt = datetime.now(timezone.utc)
    expires_at = (now_dt + timedelta(seconds=ttl)).isoformat()
    grant_id = approval.grant_id
    grant_values = {
        "member_snapshot": json.dumps(members),
        "source_selector": json.dumps(selector),
        "session_id": session_id,
        "purpose": purpose,
        "status": "active",
        "request_id": request_id,
        "one_shot": 1 if one_shot_grant else 0,
        "created_at": now_dt.isoformat(),
        "expires_at": expires_at,
        "agent_ready": 1 if resident_cache_ready else 0,
        "agent_ready_at": now_dt.isoformat() if resident_cache_ready else None,
    }
    try:
        existing_grant = (
            conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id))
            .mappings()
            .first()
        )
        if existing_grant is not None:
            existing = dict(existing_grant)
            existing_selector = _loads(existing.get("source_selector")) or {}
            existing_one_shot = bool(int(existing.get("one_shot") or 0))
            if (
                existing.get("status") != "expired"
                or existing.get("request_id") != request_id
                or set(_grant_member_names(existing)) != set(members)
                or _source_selector_payload(existing_selector) != selector
                or existing.get("session_id") != session_id
                or existing.get("purpose") != purpose
                or existing_one_shot != one_shot_grant
            ):
                raise InvalidGrantError("grant id already exists")
            reused = conn.execute(
                vault_grants.update()
                .where(vault_grants.c.id == grant_id, vault_grants.c.status == "expired")
                .values(**grant_values)
            )
            if reused.rowcount != 1:
                raise InvalidGrantError("grant id already exists")
        else:
            conn.execute(vault_grants.insert().values(id=grant_id, **grant_values))
    except Exception:
        conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == request_id)
            .values(status="pending", decided_at=None, callback_status=None)
        )
        raise
    audit(
        conn,
        "granted",
        requester={"session_id": session_id} if session_id else None,
        delivery={"source_selector": selector, "purpose": purpose, "member_count": len(members)},
        request_id=request_id,
        grant_id=grant_id,
    )
    row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one()
    if not one_shot_grant:
        _approve_sibling_access_requests_for_grant(
            conn,
            request_id=request_id,
            members=members,
            session_id=session_id,
            purpose=purpose,
            decided_at=decided_at,
        )
    if resident_cache_ready:
        cache.put(grant_id, members, expires_at=expires_at)
    return _grant_payload(conn, dict(row), cache=cache)


def list_grants(
    conn: Connection,
    *,
    status: str | None = "active",
    session_id: str | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> list[dict[str, Any]]:
    expire_grants(conn, cache=cache)
    query = select(vault_grants).order_by(vault_grants.c.created_at.desc(), vault_grants.c.id.desc())
    if status == "active":
        query = query.where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES))
    elif status is not None:
        query = query.where(vault_grants.c.status == status)
    if session_id is not None:
        query = query.where(or_(vault_grants.c.session_id.is_(None), vault_grants.c.session_id == session_id))
    return [_grant_payload(conn, dict(row), cache=cache) for row in conn.execute(query).mappings()]


def get_grant_created_by_request(
    conn: Connection,
    request_id: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(vault_grants)
            .where(vault_grants.c.request_id == request_id)
            .order_by(vault_grants.c.created_at.desc(), vault_grants.c.id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return _grant_payload(conn, dict(row), cache=cache) if row is not None else None


def expire_active_grants(
    conn: Connection,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reason: str = "grant-expired-agent-cache-reset",
) -> int:
    rows = [
        dict(row)
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES))).mappings()
    ]
    return _expire_grant_rows(conn, rows, cache=cache, reason=reason)


def revoke_grant(
    conn: Connection,
    grant_id: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().first()
    if row is None:
        raise GrantNotFoundError(grant_id)
    row_dict = dict(row)
    if row_dict.get("status") not in ACTIVE_GRANT_STATES:
        raise GrantNotActiveError(grant_id)
    now = _now()
    conn.execute(
        vault_grants.update()
        .where(vault_grants.c.id == grant_id)
        .values(status="revoked", revoked_at=now, agent_ready=0, agent_ready_at=None)
    )
    cache.drop(grant_id)
    audit(conn, "grant-revoked", grant_id=grant_id, delivery={"grant_id": grant_id})
    updated = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one()
    return _grant_payload(conn, dict(updated), cache=cache)


def mark_grant_agent_ready(
    conn: Connection,
    grant_id: str,
    *,
    ttl_seconds: int | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    row = conn.execute(
        select(vault_grants).where(
            vault_grants.c.id == grant_id,
            vault_grants.c.status == "active",
        )
    ).mappings().first()
    if row is None:
        raise GrantNotActiveError(grant_id)
    row_dict = dict(row)
    members = _grant_member_names(row_dict)
    if not members:
        raise InvalidGrantError("grant has no cached members")
    ready_at_dt = datetime.now(timezone.utc)
    ready_at = ready_at_dt.isoformat()
    values: dict[str, Any] = {"agent_ready": 1, "agent_ready_at": ready_at}
    if ttl_seconds is not None:
        ttl = max(1, int(ttl_seconds))
        values["expires_at"] = (ready_at_dt + timedelta(seconds=ttl)).isoformat()
    result = conn.execute(
        vault_grants.update()
        .where(vault_grants.c.id == grant_id, vault_grants.c.status == "active")
        .values(**values)
    )
    if result.rowcount != 1:
        raise GrantNotActiveError(grant_id)
    row_dict = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one())
    cache.put(grant_id, members, expires_at=row_dict.get("expires_at"))
    return _grant_payload(conn, row_dict, cache=cache)


def revoke_session_grants(
    conn: Connection,
    session_id: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> int:
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status.in_(ACTIVE_GRANT_STATES), vault_grants.c.session_id == session_id)
        ).mappings()
    ]
    now = _now()
    revoked = 0
    for row in rows:
        result = conn.execute(
            vault_grants.update()
            .where(vault_grants.c.id == row["id"], vault_grants.c.status.in_(ACTIVE_GRANT_STATES))
            .values(status="revoked", revoked_at=now, agent_ready=0, agent_ready_at=None)
        )
        if result.rowcount != 1:
            continue
        cache.drop(row["id"])
        audit(
            conn,
            "grant-revoked-session-archived",
            grant_id=row["id"],
            delivery={"grant_id": row["id"], "session_id": session_id},
        )
        revoked += 1
    return revoked


def find_active_grant_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    session_id: str | None = None,
    purpose: str | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reserve_one_shot: bool = False,
) -> dict[str, Any] | None:
    return find_active_grant_for_secrets(
        conn,
        [secret_name],
        session_id=session_id,
        purpose=purpose,
        cache=cache,
        reserve_one_shot=reserve_one_shot,
    )


def _grant_is_always_ask_for_members(
    conn: Connection,
    row: dict[str, Any],
) -> bool:
    try:
        return bool(int(row.get("one_shot") or 0))
    except (TypeError, ValueError):
        return False


def consume_one_shot_grant(
    conn: Connection,
    grant_id: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> list[dict[str, str]]:
    row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().first()
    if row is None:
        raise GrantNotFoundError(grant_id)
    row_dict = dict(row)
    if row_dict.get("status") not in ACTIVE_GRANT_STATES:
        raise GrantNotActiveError(grant_id)
    if not _grant_is_always_ask_for_members(conn, row_dict):
        return []
    _expire_grant_rows(
        conn,
        [row_dict],
        cache=cache,
        reason="grant-expired-always-ask-consumed",
    )
    return agent_release_scopes_after_rows(conn, [row_dict], cache=cache)


def release_one_shot_reservation(
    conn: Connection,
    grant_id: str,
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().first()
    if row is None:
        raise GrantNotFoundError(grant_id)
    row_dict = dict(row)
    if row_dict.get("status") != "reserved":
        raise GrantNotActiveError(grant_id)
    if not _grant_is_always_ask_for_members(conn, row_dict):
        raise InvalidGrantError("grant is not one-shot")
    result = conn.execute(
        vault_grants.update()
        .where(vault_grants.c.id == grant_id, vault_grants.c.status == "reserved")
        .values(status="active")
    )
    if result.rowcount != 1:
        raise GrantNotActiveError(grant_id)
    audit(conn, "grant-reservation-released", grant_id=grant_id, delivery={"grant_id": grant_id})
    row_dict = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one())
    members = _grant_member_names(row_dict)
    if grant_row_has_resident_agent_ready(row_dict) and members:
        cache.put(grant_id, members, expires_at=row_dict.get("expires_at"))
    return _grant_payload(conn, row_dict, cache=cache)


def _reserve_one_shot_grant(
    conn: Connection,
    row: dict[str, Any],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any] | None:
    if not _grant_is_always_ask_for_members(conn, row):
        return _grant_payload(conn, row, cache=cache)
    if row.get("status") != "active":
        return None
    result = conn.execute(
        vault_grants.update()
        .where(vault_grants.c.id == row["id"], vault_grants.c.status == "active")
        .values(status="reserved")
    )
    if result.rowcount != 1:
        return None
    audit(conn, "grant-reserved-one-shot", grant_id=row["id"], delivery={"grant_id": row["id"]})
    updated = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == row["id"])).mappings().one())
    return _grant_payload(conn, updated, cache=cache)


def find_active_grant_for_secrets(
    conn: Connection,
    secret_names: list[str],
    *,
    session_id: str | None = None,
    purpose: str | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reserve_one_shot: bool = False,
) -> dict[str, Any] | None:
    expire_grants(conn, cache=cache)
    requested = {str(name) for name in secret_names if str(name)}
    if not requested:
        return None
    query = select(vault_grants).where(
        vault_grants.c.status.in_(ACTIVE_GRANT_STATES),
        or_(vault_grants.c.session_id.is_(None), vault_grants.c.session_id == session_id),
    )
    if purpose is not None:
        query = query.where(vault_grants.c.purpose == purpose)
    rows = [dict(row) for row in conn.execute(query).mappings()]
    candidates: list[tuple[bool, int, str, str, dict[str, Any]]] = []
    for row in rows:
        members = _loads(row.get("member_snapshot")) or []
        member_set = {str(name) for name in members if isinstance(name, str) and name}
        if not requested.issubset(member_set):
            continue
        payload = _grant_payload(conn, row, cache=cache)
        if payload.get("one_shot") is True and row.get("status") != "active":
            continue
        standard_only = all(_require_row(conn, name).get("protection") == "standard" for name in requested)
        candidates.append((
            standard_only or bool(payload.get("delivery_ready")),
            len(member_set),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
            row,
        ))
    if not candidates:
        return None
    ready_candidates = [item for item in candidates if item[0]]
    if not ready_candidates:
        return None
    candidates = ready_candidates
    member_count = min(item[1] for item in candidates)
    _, _, _, _, row = max(
        (item for item in candidates if item[1] == member_count),
        key=lambda item: (item[2], item[3]),
    )
    payload = _grant_payload(conn, row, cache=cache)
    if reserve_one_shot and payload.get("one_shot") is True:
        return _reserve_one_shot_grant(conn, row, cache=cache)
    return payload


def resolve_secret_access(
    conn: Connection,
    name: str,
    *,
    session_id: str | None = None,
    purpose: str = "run",
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    create_request: bool = True,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
    reserve_one_shot: bool = False,
) -> dict[str, Any]:
    """Resolve an agent access attempt without exposing the value.

    Standard secrets can be delivered by existing one-shot avault paths. Protected
    secrets with an active metadata grant should be delivered by the resident
    avault agent; if the agent reports that its in-memory cache is gone, callers
    expire the grant and re-run this resolver to create a fresh approval request.
    """
    row = _require_row(conn, name)
    if purpose not in GRANT_PURPOSES:
        raise InvalidRequestError(f"invalid grant purpose: {purpose!r}")
    _reject_keypair_value_delivery(row, name)
    delivery_payload = dict(delivery or {})
    effective_session_id = (
        session_id
        or _payload_session_id(delivery_payload)
        or _payload_session_id(requester)
    )
    always_ask = _secret_always_ask(row)
    if row.get("protection") == "standard" and not always_ask:
        return {"status": "standard", "secret": _meta_payload(row), "envelope": _row_sealed(row)}
    grant = find_active_grant_for_secret(
        conn,
        name,
        session_id=effective_session_id,
        purpose=purpose,
        cache=cache,
        reserve_one_shot=reserve_one_shot,
    )
    if grant is not None:
        return {
            "status": "standard" if row.get("protection") == "standard" else "agent_delivery_ready",
            "secret": _meta_payload(row),
            "grant": grant,
            "envelope": _row_sealed(row),
            "request": None,
        }
    request_payload = None
    if create_request:
        if effective_session_id is not None:
            delivery_payload["session_id"] = effective_session_id
        request_payload = create_access_request(
            conn,
            name,
            source_selector={"env": [name]},
            purpose=purpose,
            requester=requester,
            delivery=delivery_payload,
        )
    return {"status": "approval_required", "secret": _meta_payload(row), "request": request_payload}


def list_audit(conn: Connection, *, secret_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    query = select(vault_audit).order_by(vault_audit.c.ts.desc(), vault_audit.c.id.desc()).limit(limit)
    if secret_name is not None:
        query = query.where(vault_audit.c.secret_name == secret_name)
    return [dict(row) for row in conn.execute(query).mappings()]
