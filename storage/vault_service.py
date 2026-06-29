"""CRUD + resolve + grants + audit over the vault tables (design: docs/plans/vaults.md).

Data layer for Vaults, sibling to ``storage/messages_service.py`` etc.: functions take
a SQLAlchemy ``Connection`` and never open their own engine. This module owns the
metadata invariants around stored envelopes, approval requests, scope grants, and audit
rows so future vault behavior lands here rather than in callers.

Secret values and key material never live here. Standard-tier values are sealed by
``avault`` before this layer stores them. Protected-tier values arrive already
encrypted by the browser; this layer only stores the opaque ciphertext + wrap
metadata. Scope grants persist metadata only; protected delivery material is owned by
the resident avault agent, not by Python.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from storage import vault_crypto
from storage.models import vault_audit, vault_grants, vault_groups, vault_links, vault_requests, vault_secrets
from storage.vault_crypto import Sealed

DEFAULT_GROUP = "default"
GRANT_SCOPE_TYPES = {"secret", "skill", "group"}
DEFAULT_GRANT_TTL_SECONDS = {"secret": 300, "skill": 900, "group": 900}
GRANT_TTL_OPTIONS_SECONDS = (300, 900, 3600)
SUPPORTED_SIGNATURE_SCHEMES = {
    "ecdsa-secp256k1-recoverable",
    "ecdsa-secp256k1-der",
    "schnorr-secp256k1-bip340",
}
REQUEST_AUDIENCE_AGENT = "agent"
REQUEST_AUDIENCE_UI = "ui"
REQUEST_AUDIENCES = {REQUEST_AUDIENCE_AGENT, REQUEST_AUDIENCE_UI}


@dataclass(frozen=True)
class GrantApproval:
    members: list[str]
    session_id: str | None


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


def _meta_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Masked, value-free metadata for a secret row."""
    public_meta = _public_meta(row.get("public_meta"))
    kind = row.get("kind")
    protection = row.get("protection")
    payload = {
        "name": row["name"],
        "group": row.get("group_name"),
        "tags": _loads(row.get("tags")) or [],
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
        payload["signing_public_key"] = {
            key: value
            for key, value in signing_public_key.items()
            if key in {"curve", "public_key"}
        }
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


def _grant_is_active(row: dict[str, Any]) -> bool:
    if row.get("status") != "active":
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
        "scope_type": row["scope_type"],
        "scope_ref": row["scope_ref"],
        "session_id": row.get("session_id"),
        "status": row.get("status"),
        "created_by_request_id": row.get("created_by_request_id"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "revoked_at": row.get("revoked_at"),
        "member_snapshot": members,
        "member_count": len(members),
        "runtime_member_count": len(runtime_members),
        "delivery_ready": readiness.delivery_ready,
        "delivery_status": readiness.delivery_status,
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


def _unique_grant_scopes(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    scopes: list[dict[str, str]] = []
    for row in rows:
        key = (str(row["scope_type"]), str(row["scope_ref"]))
        if key in seen:
            continue
        seen.add(key)
        scopes.append({"scope_type": key[0], "scope_ref": key[1]})
    return scopes


def _hydrate_card_unlock_material(conn: Connection, row: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    card = dict(card)
    secret_name = row.get("secret_name")
    if secret_name:
        secret_row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == secret_name)).mappings().first()
        if secret_row is not None and (material := _protected_unlock_material(dict(secret_row))) is not None:
            card["secret_unlock_material"] = material
    hydrated_options: list[dict[str, Any]] = []
    for option in card.get("scope_options") or []:
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
        card["scope_options"] = hydrated_options
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
            .values(status="expired", decided_at=now)
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


def _request_card(row: dict[str, Any]) -> dict[str, Any]:
    _, delivery = _request_json_payloads(row)
    card = delivery.get("card") if isinstance(delivery, dict) else None
    return card if isinstance(card, dict) else {}


def _secret_policy(row: dict[str, Any]) -> dict[str, Any]:
    return _loads(row.get("policy")) or {}


def _secret_access_grantable(row: dict[str, Any]) -> bool:
    if row.get("kind") == "keypair":
        return False
    if _secret_policy(row).get("always_ask"):
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


def ensure_default_group(conn: Connection) -> None:
    """Insert the implicit ``default`` group if absent (the migration seeds it, but
    keep this defensive for DBs built another way)."""
    exists = conn.execute(select(vault_groups.c.name).where(vault_groups.c.name == DEFAULT_GROUP)).first()
    if exists is None:
        conn.execute(
            vault_groups.insert().values(
                name=DEFAULT_GROUP,
                description="Default group",
                grantable=1,
                max_grant_ttl_seconds=900,
                created_at=_now(),
            )
        )


def _ensure_group(conn: Connection, name: str) -> None:
    """Create the group row if it's missing so a secret's ``group_name`` FK is satisfied.

    The Vaults UI / ``--group`` expose arbitrary group labels; without this an unseeded
    group would trip the FK with a generic ``FOREIGN KEY constraint failed`` instead of
    the group option just working.
    """
    if conn.execute(select(vault_groups.c.name).where(vault_groups.c.name == name)).first() is None:
        try:
            conn.execute(
                vault_groups.insert().values(
                    name=name,
                    description="Default group" if name == DEFAULT_GROUP else None,
                    grantable=1,
                    max_grant_ttl_seconds=900,
                    created_at=_now(),
                )
            )
        except IntegrityError:
            # A concurrent create inserted this brand-new group between our check and insert.
            # The row now exists (all the FK needs), so swallow the PK conflict and continue —
            # otherwise the loser's otherwise-valid secret create would fail with a raw error.
            pass


def _require_row(conn: Connection, name: str) -> dict[str, Any]:
    row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().first()
    if row is None:
        raise SecretNotFoundError(name)
    return dict(row)


def create_secret(
    conn: Connection,
    *,
    name: str,
    sealed: Sealed,
    group: str = DEFAULT_GROUP,
    tags: list[str] | None = None,
    protection: str = "standard",
    kind: str = "static",
    signer_kind: str | None = None,
    description: str | None = None,
    source: str = "manual",
    policy: dict[str, Any] | None = None,
    public_meta: dict[str, Any] | None = None,
    establishing_vmk: bool = False,
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
    if conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).first() is not None:
        raise SecretExistsError(name)

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

    _ensure_group(conn, group)
    now = _now()
    public_meta = dict(public_meta or {})
    if description:
        public_meta["description"] = description
    try:
        conn.execute(
            vault_secrets.insert().values(
                id=_id("vlt"),
                name=name,
                group_name=group,
                tags=json.dumps(tags) if tags else None,
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
        # Two concurrent creates (e.g. Web dialog + inline card) can both pass the existence
        # check above; the loser hits the UNIQUE(name) constraint here. Surface it as the same
        # SecretExistsError → 409 so the racing already-fulfilled ask is handled, not a 500.
        raise SecretExistsError(name) from exc
    audit(conn, "created", secret_name=name)
    # Any pending dynamic-ask (provision) request for this name is now satisfied,
    # regardless of which create path stored it (CLI / API / inline card) — so a
    # `vibe vault request --wait` resolves instead of timing out.
    conn.execute(
        vault_requests.update()
        .where(
            vault_requests.c.request_type == "provision",
            vault_requests.c.secret_name == name,
            vault_requests.c.status == "pending",
        )
        .values(status="fulfilled", decided_at=_now())
    )
    return _meta_payload(_require_row(conn, name))


def get_secret_meta(conn: Connection, name: str) -> dict[str, Any]:
    return _meta_payload(_require_row(conn, name))


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


def list_secrets(conn: Connection, *, group: str | None = None) -> list[dict[str, Any]]:
    """Masked, value-free list. Never decrypts."""
    query = select(vault_secrets).order_by(vault_secrets.c.name)
    if group is not None:
        query = query.where(vault_secrets.c.group_name == group)
    return [_meta_payload(dict(row)) for row in conn.execute(query).mappings()]


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
    skill: str | None = None,
    requester: Any = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Record an agent's request for a missing secret (dynamic ask).

    If the secret already exists, the request is born ``fulfilled`` — otherwise a
    ``request --wait`` would block forever (a create for an existing name is rejected,
    so nothing would ever flip a pending row).
    """
    request_id = _id("vrq")
    now = _now()
    already = conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).first() is not None
    status = "fulfilled" if already else "pending"
    card = _secure_input_card(name, request_id=request_id, reason=reason, skill=skill)
    delivery_payload: dict[str, Any] = {"card": card}
    if reason:
        delivery_payload["reason"] = reason
    if skill:
        delivery_payload["skill"] = skill
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
    group: str = DEFAULT_GROUP,
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
        group=group,
        description=description,
    )
    conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id)
        .values(status="fulfilled", decided_at=_now())
    )
    return meta


def _secure_input_card(
    name: str,
    *,
    request_id: str,
    reason: str | None = None,
    skill: str | None = None,
) -> dict[str, Any]:
    return {
        "card_type": "secure_input",
        "request_id": request_id,
        "secret_name": name,
        "reason": reason,
        "skill": skill,
        "protection_options": ["standard", "protected"],
        "default_protection": "protected",
        "value": None,
    }


def _grant_member_rows(conn: Connection, scope_type: str, scope_ref: str) -> list[dict[str, Any]]:
    if scope_type == "secret":
        return [_require_row(conn, scope_ref)]
    if scope_type == "skill":
        rows = (
            conn.execute(
                select(vault_secrets)
                .select_from(vault_links.join(vault_secrets, vault_links.c.secret_name == vault_secrets.c.name))
                .where(vault_links.c.skill_name == scope_ref)
                .order_by(vault_secrets.c.name)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
    if scope_type == "group":
        rows = (
            conn.execute(
                select(vault_secrets)
                .where(vault_secrets.c.group_name == scope_ref)
                .order_by(vault_secrets.c.name)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
    raise InvalidGrantError(f"invalid grant scope_type: {scope_type!r}")


def _grantable_member_rows(conn: Connection, scope_type: str, scope_ref: str) -> list[dict[str, Any]]:
    rows = _grant_member_rows(conn, scope_type, scope_ref)
    if not rows:
        return []
    group_names = {row.get("group_name") for row in rows if row.get("group_name")}
    grantable_groups = {
        row["name"]
        for row in conn.execute(select(vault_groups).where(vault_groups.c.name.in_(group_names))).mappings()
        if int(row.get("grantable") or 0) == 1
    }
    return [
        row
        for row in rows
        if _secret_access_grantable(row) and (row.get("group_name") in grantable_groups)
    ]


def grantable_member_metas(conn: Connection, scope_type: str, scope_ref: str) -> list[dict[str, Any]]:
    return [_meta_payload(row) for row in _grantable_member_rows(conn, scope_type, scope_ref)]


def _ttl_cap_for_members(conn: Connection, member_names: list[str]) -> int:
    if not member_names:
        return min(DEFAULT_GRANT_TTL_SECONDS.values())
    rows = (
        conn.execute(
            select(vault_groups.c.max_grant_ttl_seconds)
            .select_from(vault_secrets.join(vault_groups, vault_secrets.c.group_name == vault_groups.c.name))
            .where(vault_secrets.c.name.in_(member_names))
        )
        .scalars()
        .all()
    )
    caps = [int(row) for row in rows if row is not None]
    return min(caps) if caps else 900


def _scope_option(
    conn: Connection,
    scope_type: str,
    scope_ref: str,
    *,
    requested_secret: str,
    default_ttl_seconds: int,
) -> dict[str, Any] | None:
    rows = _grantable_member_rows(conn, scope_type, scope_ref)
    members = [row["name"] for row in rows]
    if not members or requested_secret not in members:
        return None
    ttl_cap = _ttl_cap_for_members(conn, members)
    capped_default = min(default_ttl_seconds, ttl_cap)
    return {
        "scope_type": scope_type,
        "scope_ref": scope_ref,
        "default_ttl_seconds": capped_default,
        "ttl_options_seconds": [seconds for seconds in GRANT_TTL_OPTIONS_SECONDS if seconds <= ttl_cap],
        "session_binding_default": True,
        "member_count": len(members),
        "member_snapshot": members,
        "member_versions": [_member_version(row) for row in rows],
    }


def approval_card(
    conn: Connection,
    secret_name: str,
    *,
    request_id: str,
    request_type: str = "access",
    command: str | None = None,
    egress: str | None = None,
    skill: str | None = None,
    session_id: str | None = None,
    grantable: bool = True,
) -> dict[str, Any]:
    row = _require_row(conn, secret_name)
    group = row.get("group_name") or DEFAULT_GROUP
    scope_options: list[dict[str, Any]] = []
    if grantable:
        scope_options = [
            option
            for option in (
                _scope_option(
                    conn,
                    "secret",
                    secret_name,
                    requested_secret=secret_name,
                    default_ttl_seconds=DEFAULT_GRANT_TTL_SECONDS["secret"],
                ),
                (
                    _scope_option(
                        conn,
                        "skill",
                        skill,
                        requested_secret=secret_name,
                        default_ttl_seconds=DEFAULT_GRANT_TTL_SECONDS["skill"],
                    )
                    if skill
                    else None
                ),
                _scope_option(
                    conn,
                    "group",
                    group,
                    requested_secret=secret_name,
                    default_ttl_seconds=DEFAULT_GRANT_TTL_SECONDS["group"],
                ),
            )
            if option is not None
        ]
    card = {
        "card_type": "approval",
        "request_id": request_id,
        "request_type": request_type,
        "secret_name": secret_name,
        "kind": row.get("kind"),
        "protection": row.get("protection"),
        "command": command,
        "egress": egress,
        "session_id": session_id,
        "approve_once": True,
        "scope_options": scope_options,
        "value": None,
    }
    return card


def create_access_request(
    conn: Connection,
    name: str,
    *,
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    message_id: str | None = None,
    expires_at: str | None = None,
    audience: str | None = None,
) -> dict[str, Any]:
    row = _require_row(conn, name)
    if not _secret_access_grantable(row):
        raise NotGrantableError(f"{name} is not access-grantable")
    payload_audience = audience or _request_audience_from_requester(requester)
    request_id = _id("vrq")
    delivery_payload = dict(delivery or {})
    requester_payload = requester if isinstance(requester, dict) else {}
    card = approval_card(
        conn,
        name,
        request_id=request_id,
        request_type="access",
        command=delivery_payload.get("command"),
        egress=delivery_payload.get("egress"),
        skill=delivery_payload.get("skill") or requester_payload.get("skill"),
        session_id=requester_payload.get("session_id") or delivery_payload.get("session_id"),
    )
    delivery_payload["card"] = card
    conn.execute(
        vault_requests.insert().values(
            id=request_id,
            request_type="access",
            secret_name=name,
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
        .values(status="approved", decided_at=_now(), delivery=json.dumps(completed_delivery))
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("sign request is not pending")
    record_signing_use(
        conn,
        name,
        requester=requester,
        delivery={"scheme": scheme, "digest": digest, "browser_signed": bool(signature.get("browser_signed"))},
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
        .values(status="failed", decided_at=_now(), delivery=json.dumps(delivery_payload))
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
        .values(status="denied", decided_at=decided_at)
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
) -> list[dict[str, Any]]:
    _expire_pending_requests(conn)
    query = select(vault_requests).order_by(vault_requests.c.created_at.desc(), vault_requests.c.id.desc()).limit(limit)
    if status is not None:
        query = query.where(vault_requests.c.status == status)
    if request_type is not None:
        query = query.where(vault_requests.c.request_type == request_type)
    return [
        _request_row_payload(dict(row), conn=conn, audience=REQUEST_AUDIENCE_UI)
        for row in conn.execute(query).mappings()
    ]


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
            .where(vault_grants.c.id == row["id"], vault_grants.c.status == "active")
            .values(status="expired", revoked_at=now, agent_ready=0, agent_ready_at=None)
        )
        cache.drop(row["id"])
        audit(conn, reason, grant_id=row["id"], delivery={"scope_type": row["scope_type"], "scope_ref": row["scope_ref"]})
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
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status == "active")).mappings()
        if secret_name in (_loads(row.get("member_snapshot")) or [])
    ]


def active_grant_scopes_for_secret(conn: Connection, secret_name: str) -> list[dict[str, str]]:
    return _unique_grant_scopes(active_grant_rows_for_secret(conn, secret_name))


def active_grant_scopes_for_session(conn: Connection, session_id: str) -> list[dict[str, str]]:
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status == "active", vault_grants.c.session_id == session_id)
        ).mappings()
    ]
    return _unique_grant_scopes(rows)


def agent_release_scopes_after_rows(
    conn: Connection,
    rows: list[dict[str, Any]],
    *,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> list[dict[str, str]]:
    """Return resident-agent scopes that must be dropped after grant rows stop being active.

    The agent cache is keyed by scope, not grant id. Keeping a scope is valid only
    when the remaining active grants for that scope still cover every member that
    had been cached under the removed rows.
    """

    now = _now()
    expired_rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status == "active", vault_grants.c.expires_at <= now)
        ).mappings()
    ]
    if expired_rows:
        _expire_grant_rows(conn, expired_rows, cache=cache)
    rows = [*rows, *expired_rows]
    rows = [dict(row) for row in rows if grant_row_has_resident_agent_ready(dict(row))]
    if not rows:
        return []
    active_rows = [
        dict(row)
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status == "active")).mappings()
        if grant_row_has_resident_agent_ready(dict(row))
    ]
    active_by_scope: dict[tuple[str, str], set[str]] = {}
    for row in active_rows:
        key = (str(row["scope_type"]), str(row["scope_ref"]))
        active_by_scope.setdefault(key, set()).update(_grant_member_names(row))

    removed_by_scope: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (str(row["scope_type"]), str(row["scope_ref"]))
        removed_by_scope.setdefault(key, set()).update(_grant_member_names(row))

    release_scopes: list[dict[str, str]] = []
    for (scope_type, scope_ref), removed_members in removed_by_scope.items():
        if removed_members.issubset(active_by_scope.get((scope_type, scope_ref), set())):
            continue
        stale_scope_grants = [
            dict(row)
            for row in conn.execute(
                select(vault_grants).where(
                    vault_grants.c.scope_type == scope_type,
                    vault_grants.c.scope_ref == scope_ref,
                )
            ).mappings()
        ]
        for stale_row in stale_scope_grants:
            cache.drop(str(stale_row["id"]))
            _clear_grant_agent_ready(conn, str(stale_row["id"]))
        conn.execute(
            vault_grants.update()
            .where(
                vault_grants.c.scope_type == scope_type,
                vault_grants.c.scope_ref == scope_ref,
                vault_grants.c.status == "active",
            )
            .values(agent_ready=0, agent_ready_at=None)
        )
        release_scopes.append({"scope_type": scope_type, "scope_ref": scope_ref})
    return release_scopes


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
    if row_dict.get("status") == "active":
        _expire_grant_rows(conn, [row_dict], cache=cache, reason=reason)
        row_dict = dict(conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one())
    return _grant_payload(conn, row_dict, cache=cache)


def _expire_pending_requests_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    reason: str = "request-expired",
) -> int:
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_requests).where(
                vault_requests.c.secret_name == secret_name,
                vault_requests.c.status == "pending",
                vault_requests.c.request_type.in_(("access", "sign")),
            )
        ).mappings()
    ]
    if not rows:
        return 0
    return _expire_pending_request_rows(conn, rows, reason=reason)


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
            select(vault_grants).where(vault_grants.c.status == "active", vault_grants.c.expires_at <= now)
        ).mappings()
    ]
    return _expire_grant_rows(conn, rows, cache=cache)


def _validate_access_request_for_grant(
    conn: Connection,
    request_id: str,
    *,
    scope_type: str,
    scope_ref: str,
    session_id: str | None,
    inherit_request_session: bool,
    live_members: list[str],
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
    requested_secret = row_dict.get("secret_name")
    if requested_secret not in live_members:
        raise InvalidRequestError("grant scope does not cover the requested secret")
    requested_session_id = _request_session_id(row_dict)
    effective_session_id = requested_session_id if session_id is None and inherit_request_session else session_id
    if requested_session_id and effective_session_id and requested_session_id != effective_session_id:
        raise InvalidRequestError("grant session does not match the approval request")
    card = _request_card(row_dict)
    allowed_scopes = card.get("scope_options") if isinstance(card, dict) else None
    if isinstance(allowed_scopes, list) and allowed_scopes:
        for option in allowed_scopes:
            if not (
                isinstance(option, dict)
                and option.get("scope_type") == scope_type
                and option.get("scope_ref") == scope_ref
            ):
                continue
            snapshot = option.get("member_snapshot") or []
            if not isinstance(snapshot, list):
                raise InvalidRequestError("grant scope has an invalid approval snapshot")
            members = [str(name) for name in snapshot if isinstance(name, str) and name]
            if requested_secret not in members:
                break
            versions = option.get("member_versions")
            if not isinstance(versions, list):
                raise InvalidRequestError("grant scope approval snapshot is stale")
            versions_by_name = {
                item.get("name"): item
                for item in versions
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            if set(versions_by_name) != set(members):
                raise InvalidRequestError("grant scope approval snapshot is stale")
            current_rows = {
                current["name"]: dict(current)
                for current in conn.execute(select(vault_secrets).where(vault_secrets.c.name.in_(members))).mappings()
            }
            for member_name in members:
                current = current_rows.get(member_name)
                if current is None or member_name not in live_members:
                    raise InvalidRequestError("grant approval snapshot has stale members")
                if not _member_version_matches(current, versions_by_name[member_name]):
                    raise InvalidRequestError("grant approval snapshot has stale members")
            return GrantApproval(members=members, session_id=effective_session_id)
        raise InvalidRequestError("grant scope was not offered by the approval request")
    return GrantApproval(members=live_members, session_id=effective_session_id)


def _approve_sibling_access_requests_for_grant(
    conn: Connection,
    *,
    created_by_request_id: str,
    members: list[str],
    session_id: str | None,
    decided_at: str,
) -> int:
    if not members:
        return 0
    target_session_id = session_id
    if not target_session_id:
        approval_row = conn.execute(select(vault_requests).where(vault_requests.c.id == created_by_request_id)).mappings().first()
        target_session_id = _request_session_id(dict(approval_row)) if approval_row else None
    if not target_session_id:
        return 0
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_requests).where(
                vault_requests.c.id != created_by_request_id,
                vault_requests.c.request_type == "access",
                vault_requests.c.status == "pending",
                vault_requests.c.secret_name.in_(members),
            )
        ).mappings()
        if _request_session_id(dict(row)) == target_session_id
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
            .values(status="approved", decided_at=decided_at)
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
    created_by_request_id: str,
    member_names: list[str] | set[str] | tuple[str, ...],
    session_id: str | None,
) -> int:
    """Make a protected grant approval retryable after resident-agent relay fails."""

    members = {str(name) for name in member_names if str(name)}
    if not created_by_request_id or not members:
        return 0
    approval_row = conn.execute(select(vault_requests).where(vault_requests.c.id == created_by_request_id)).mappings().first()
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
                vault_requests.c.secret_name.in_(members),
            )
        ).mappings()
        if row["id"] == created_by_request_id
        or (
            target_session_id
            and row.get("decided_at") == decided_at
            and _request_session_id(dict(row)) == target_session_id
        )
    ]
    restored = 0
    for row in rows:
        result = conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == row["id"], vault_requests.c.status == "approved")
            .values(status="pending", decided_at=None)
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
    scope_type: str,
    scope_ref: str,
    session_id: str | None = None,
    ttl_seconds: int | None = None,
    created_by_request_id: str | None = None,
    inherit_request_session: bool = True,
    expected_member_names: set[str] | list[str] | tuple[str, ...] | None = None,
    cache_ready: bool = True,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    if scope_type not in GRANT_SCOPE_TYPES:
        raise InvalidGrantError(f"invalid grant scope_type: {scope_type!r}")
    if not created_by_request_id:
        raise InvalidRequestError("grant creation requires an approval request")
    live_rows = _grantable_member_rows(conn, scope_type, scope_ref)
    live_members = [row["name"] for row in live_rows]
    if not live_members:
        raise NotGrantableError(f"{scope_type}:{scope_ref} has no grantable static secrets")
    approval = _validate_access_request_for_grant(
        conn,
        created_by_request_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        session_id=session_id,
        inherit_request_session=inherit_request_session,
        live_members=live_members,
    )
    session_id = approval.session_id
    members = approval.members
    if any(name not in live_members for name in members):
        raise InvalidRequestError("grant approval snapshot has stale members")
    if expected_member_names is not None and set(members) != set(expected_member_names):
        raise InvalidGrantError("resident agent DEKs must match the approved grant members")
    live_rows_by_name = {row["name"]: row for row in live_rows}
    resident_cache_ready = cache_ready and any(
        live_rows_by_name[name].get("protection") != "standard" for name in members
    )
    decided_at = _now()
    claim = conn.execute(
        vault_requests.update()
        .where(
            vault_requests.c.id == created_by_request_id,
            vault_requests.c.request_type == "access",
            vault_requests.c.status == "pending",
        )
        .values(status="approved", decided_at=decided_at)
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("grant approval request is not pending")
    ttl = int(ttl_seconds or DEFAULT_GRANT_TTL_SECONDS[scope_type])
    ttl = max(1, min(ttl, _ttl_cap_for_members(conn, members)))
    now_dt = datetime.now(timezone.utc)
    expires_at = (now_dt + timedelta(seconds=ttl)).isoformat()
    grant_id = _id("vgr")
    try:
        conn.execute(
            vault_grants.insert().values(
                id=grant_id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                member_snapshot=json.dumps(members),
                session_id=session_id,
                status="active",
                created_by_request_id=created_by_request_id,
                created_at=now_dt.isoformat(),
                expires_at=expires_at,
                agent_ready=1 if resident_cache_ready else 0,
                agent_ready_at=now_dt.isoformat() if resident_cache_ready else None,
            )
        )
    except Exception:
        conn.execute(
            vault_requests.update()
            .where(vault_requests.c.id == created_by_request_id)
            .values(status="pending", decided_at=None)
        )
        raise
    audit(
        conn,
        "granted",
        requester={"session_id": session_id} if session_id else None,
        delivery={"scope_type": scope_type, "scope_ref": scope_ref, "member_count": len(members)},
        request_id=created_by_request_id,
        grant_id=grant_id,
    )
    row = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one()
    _approve_sibling_access_requests_for_grant(
        conn,
        created_by_request_id=created_by_request_id,
        members=members,
        session_id=session_id,
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
    if status is not None:
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
            .where(vault_grants.c.created_by_request_id == request_id)
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
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status == "active")).mappings()
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
    if row_dict.get("status") != "active":
        raise GrantNotActiveError(grant_id)
    now = _now()
    conn.execute(
        vault_grants.update()
        .where(vault_grants.c.id == grant_id)
        .values(status="revoked", revoked_at=now, agent_ready=0, agent_ready_at=None)
    )
    cache.drop(grant_id)
    audit(conn, "grant-revoked", grant_id=grant_id, delivery={"scope_type": row_dict["scope_type"], "scope_ref": row_dict["scope_ref"]})
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
            select(vault_grants).where(vault_grants.c.status == "active", vault_grants.c.session_id == session_id)
        ).mappings()
    ]
    now = _now()
    revoked = 0
    for row in rows:
        result = conn.execute(
            vault_grants.update()
            .where(vault_grants.c.id == row["id"], vault_grants.c.status == "active")
            .values(status="revoked", revoked_at=now, agent_ready=0, agent_ready_at=None)
        )
        if result.rowcount != 1:
            continue
        cache.drop(row["id"])
        audit(
            conn,
            "grant-revoked-session-archived",
            grant_id=row["id"],
            delivery={"scope_type": row["scope_type"], "scope_ref": row["scope_ref"], "session_id": session_id},
        )
        revoked += 1
    return revoked


def find_active_grant_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    session_id: str | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any] | None:
    return find_active_grant_for_secrets(conn, [secret_name], session_id=session_id, cache=cache)


def find_active_grant_for_secrets(
    conn: Connection,
    secret_names: list[str],
    *,
    session_id: str | None = None,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any] | None:
    expire_grants(conn, cache=cache)
    requested = {str(name) for name in secret_names if str(name)}
    if not requested:
        return None
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(
                vault_grants.c.status == "active",
                or_(vault_grants.c.session_id.is_(None), vault_grants.c.session_id == session_id),
            )
        ).mappings()
    ]
    candidates: list[tuple[bool, int, str, str, dict[str, Any]]] = []
    for row in rows:
        members = _loads(row.get("member_snapshot")) or []
        member_set = {str(name) for name in members if isinstance(name, str) and name}
        if not requested.issubset(member_set):
            continue
        payload = _grant_payload(conn, row, cache=cache)
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
    return _grant_payload(conn, row, cache=cache)


def resolve_secret_access(
    conn: Connection,
    name: str,
    *,
    session_id: str | None = None,
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    create_request: bool = True,
    cache: VaultGrantRuntimeCache = GRANT_RUNTIME_CACHE,
) -> dict[str, Any]:
    """Resolve an agent access attempt without exposing the value.

    Standard secrets can be delivered by existing one-shot avault paths. Protected
    secrets with an active metadata grant should be delivered by the resident
    avault agent; if the agent reports that its in-memory cache is gone, callers
    expire the grant and re-run this resolver to create a fresh approval request.
    """
    row = _require_row(conn, name)
    _reject_keypair_value_delivery(row, name)
    if row.get("protection") == "standard":
        return {"status": "standard", "secret": _meta_payload(row), "envelope": _row_sealed(row)}
    delivery_payload = dict(delivery or {})
    effective_session_id = (
        session_id
        or _payload_session_id(delivery_payload)
        or _payload_session_id(requester)
    )
    grant = find_active_grant_for_secret(conn, name, session_id=effective_session_id, cache=cache)
    if grant is not None:
        return {
            "status": "agent_delivery_ready",
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
            requester=requester,
            delivery=delivery_payload,
        )
    return {"status": "approval_required", "secret": _meta_payload(row), "request": request_payload}


def list_audit(conn: Connection, *, secret_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    query = select(vault_audit).order_by(vault_audit.c.ts.desc(), vault_audit.c.id.desc()).limit(limit)
    if secret_name is not None:
        query = query.where(vault_audit.c.secret_name == secret_name)
    return [dict(row) for row in conn.execute(query).mappings()]
