"""CRUD + resolve + grants + audit over the vault tables (design: docs/plans/vaults.md).

Data layer for Vaults, sibling to ``storage/messages_service.py`` etc.: functions take
a SQLAlchemy ``Connection`` and never open their own engine. This module owns the
metadata invariants around stored envelopes, approval requests, scope grants, and audit
rows so future vault behavior lands here rather than in callers.

Secret values and key material never live here. Standard-tier values are sealed by
``avault`` before this layer sees them. Protected-tier values arrive already encrypted
by the browser; this layer only stores the opaque ciphertext + wrap metadata. Scope
grants persist metadata only; any released DEK set is held in an in-memory cache and is
cleared on process restart.
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


@dataclass(frozen=True)
class GrantApproval:
    members: list[str]
    session_id: str | None


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
    """A caller attempted a delivery path that still needs the resident agent."""


class InvalidGrantError(VaultServiceError):
    pass


class GrantNotFoundError(VaultServiceError):
    pass


class GrantNotActiveError(VaultServiceError):
    pass


class NotGrantableError(VaultServiceError):
    pass


class VaultGrantDekCache:
    """Process-local cache for released protected DEKs.

    The persisted ``vault_grants`` row records only scope and expiry metadata. Browser
    approval may release a frozen set of DEKs; those are deliberately held only here so
    restart clears the grant's key material. The cache stores opaque caller-supplied
    strings (normally base64 DEKs); it never writes them to SQLite.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._deks_by_grant: dict[str, dict[str, str]] = {}

    def put(self, grant_id: str, deks_by_secret: dict[str, str]) -> None:
        with self._lock:
            self._deks_by_grant[grant_id] = dict(deks_by_secret)

    def has(self, grant_id: str, secret_name: str) -> bool:
        with self._lock:
            return secret_name in self._deks_by_grant.get(grant_id, {})

    def get(self, grant_id: str, secret_name: str) -> str | None:
        with self._lock:
            return self._deks_by_grant.get(grant_id, {}).get(secret_name)

    def covered_names(self, grant_id: str) -> list[str]:
        with self._lock:
            return sorted(self._deks_by_grant.get(grant_id, {}))

    def drop(self, grant_id: str) -> None:
        with self._lock:
            self._deks_by_grant.pop(grant_id, None)

    def clear(self) -> None:
        with self._lock:
            self._deks_by_grant.clear()


GRANT_DEK_CACHE = VaultGrantDekCache()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return {
        "name": row["name"],
        "group": row.get("group_name"),
        "tags": _loads(row.get("tags")) or [],
        "kind": row.get("kind"),
        "protection": row.get("protection"),
        "signer_kind": row.get("signer_kind"),
        "source": row.get("source"),
        "description": public_meta.get("description"),
        # Policy is non-secret (allowed hosts, auth scheme name) — safe to surface.
        "policy": _loads(row.get("policy")) or {},
        "last_used_at": row.get("last_used_at"),
        "use_count": row.get("use_count"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


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


def _grant_row_payload(row: dict[str, Any], *, cache: VaultGrantDekCache = GRANT_DEK_CACHE) -> dict[str, Any]:
    members = _loads(row.get("member_snapshot")) or []
    if not isinstance(members, list):
        members = []
    grant_id = row["id"]
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
        "cached_member_count": len(cache.covered_names(grant_id)),
    }


def _request_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    requester = _loads(row.get("requester"))
    delivery = _loads(row.get("delivery"))
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
        "card": delivery.get("card") if isinstance(delivery, dict) else None,
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


def _request_card(row: dict[str, Any]) -> dict[str, Any]:
    _, delivery = _request_json_payloads(row)
    card = delivery.get("card") if isinstance(delivery, dict) else None
    return card if isinstance(card, dict) else {}


def _secret_policy(row: dict[str, Any]) -> dict[str, Any]:
    return _loads(row.get("policy")) or {}


def _secret_is_grantable(row: dict[str, Any]) -> bool:
    if row.get("protection") != "protected":
        return False
    if row.get("kind") == "keypair":
        return False
    if _secret_policy(row).get("always_ask"):
        return False
    return True


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
        .values(public_meta=json.dumps(public_meta), updated_at=_now())
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


def rotate_secret(
    conn: Connection,
    name: str,
    sealed: Sealed,
    *,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
) -> dict[str, Any]:
    row = _require_row(conn, name)
    public_meta = _public_meta(row.get("public_meta"))
    public_meta.pop("preview", None)
    if row.get("protection") == "protected":
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


def delete_secret(conn: Connection, name: str, *, cache: VaultGrantDekCache = GRANT_DEK_CACHE) -> None:
    row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().first()
    if row is None:
        raise SecretNotFoundError(name)
    if row.get("protection") == "protected":
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

    Protected delivery is intentionally not routed through this helper until the
    resident-agent/DEK-blindbox follow-on lands; callers should use
    :func:`resolve_secret_access` to decide whether an approval/grant is needed.
    """
    row = _require_row(conn, name)
    if row.get("protection") != "standard":
        raise UnsupportedProtectionError(f"{name} is protected-tier (resident grant delivery is not wired yet)")
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
            raise UnsupportedProtectionError(f"{name} is protected-tier (resident grant delivery is not wired yet)")
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
        if _secret_is_grantable(row) and (row.get("group_name") in grantable_groups)
    ]


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
    default_ttl_seconds: int,
) -> dict[str, Any] | None:
    rows = _grantable_member_rows(conn, scope_type, scope_ref)
    members = [row["name"] for row in rows]
    if not members:
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
        "unlock_material": [material for row in rows if (material := _protected_unlock_material(row)) is not None],
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
                _scope_option(conn, "secret", secret_name, default_ttl_seconds=DEFAULT_GRANT_TTL_SECONDS["secret"]),
                _scope_option(conn, "skill", skill, default_ttl_seconds=DEFAULT_GRANT_TTL_SECONDS["skill"]) if skill else None,
                _scope_option(conn, "group", group, default_ttl_seconds=DEFAULT_GRANT_TTL_SECONDS["group"]),
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
    secret_unlock_material = _protected_unlock_material(row)
    if secret_unlock_material is not None:
        card["secret_unlock_material"] = secret_unlock_material
    return card


def create_access_request(
    conn: Connection,
    name: str,
    *,
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    message_id: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
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
    return _request_row_payload(dict(row))


def create_sign_request(
    conn: Connection,
    name: str,
    *,
    digest: str,
    scheme: str,
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
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
        )
    )
    audit(conn, "sign_requested", secret_name=name, requester=requester, delivery=delivery_payload, request_id=request_id)
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(row))


def _signature_bytes(raw: str) -> bytes:
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise InvalidRequestError("signature must be hex-encoded bytes") from exc


def _validate_signature_payload(scheme: str, signature: dict[str, Any]) -> None:
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
    raise InvalidRequestError(f"unsupported signature scheme: {scheme}")


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
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
    if row is None:
        raise RequestNotFoundError(request_id)
    row_dict = dict(row)
    if row_dict.get("request_type") != "sign":
        raise InvalidRequestError("signature completion must target a sign request")
    if row_dict.get("status") != "pending":
        raise InvalidRequestError("sign request is not pending")
    if row_dict.get("secret_name") != name:
        raise InvalidRequestError("signature secret does not match the sign request")
    _, delivery = _request_json_payloads(row_dict)
    delivery_payload = delivery if isinstance(delivery, dict) else {}
    if delivery_payload.get("digest") != digest or delivery_payload.get("scheme") != scheme:
        raise InvalidRequestError("signature payload does not match the sign request")
    _validate_signature_payload(scheme, signature)
    claim = conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id, vault_requests.c.request_type == "sign", vault_requests.c.status == "pending")
        .values(status="approved", decided_at=_now())
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("sign request is not pending")
    record_signing_use(
        conn,
        name,
        requester=requester,
        delivery={"scheme": scheme, "digest": digest, "browser_signed": True},
        request_id=request_id,
    )
    updated = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    return _request_row_payload(dict(updated))


def list_requests(
    conn: Connection,
    *,
    status: str | None = "pending",
    request_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    query = select(vault_requests).order_by(vault_requests.c.created_at.desc(), vault_requests.c.id.desc()).limit(limit)
    if status is not None:
        query = query.where(vault_requests.c.status == status)
    if request_type is not None:
        query = query.where(vault_requests.c.request_type == request_type)
    return [_request_row_payload(dict(row)) for row in conn.execute(query).mappings()]


def _expire_grant_rows(
    conn: Connection,
    rows: list[dict[str, Any]],
    *,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
    reason: str = "grant-expired",
) -> int:
    now = _now()
    expired = 0
    for row in rows:
        conn.execute(
            vault_grants.update()
            .where(vault_grants.c.id == row["id"], vault_grants.c.status == "active")
            .values(status="expired", revoked_at=now)
        )
        cache.drop(row["id"])
        audit(conn, reason, grant_id=row["id"], delivery={"scope_type": row["scope_type"], "scope_ref": row["scope_ref"]})
        expired += 1
    return expired


def _expire_active_grants_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
    reason: str = "grant-expired",
) -> int:
    rows = [
        dict(row)
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status == "active")).mappings()
        if secret_name in (_loads(row.get("member_snapshot")) or [])
    ]
    return _expire_grant_rows(conn, rows, cache=cache, reason=reason)


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
        audit(
            conn,
            reason,
            secret_name=secret_name,
            delivery={"request_type": row["request_type"]},
            request_id=row["id"],
        )
        expired += 1
    return expired


def expire_grants(conn: Connection, *, cache: VaultGrantDekCache = GRANT_DEK_CACHE) -> int:
    now = _now()
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(vault_grants.c.status == "active", vault_grants.c.expires_at <= now)
        ).mappings()
    ]
    return _expire_grant_rows(conn, rows, cache=cache)


def expire_grants_without_cached_deks(conn: Connection, *, cache: VaultGrantDekCache = GRANT_DEK_CACHE) -> int:
    """Expire active grant rows whose process-local DEK cache is gone.

    A persisted grant row is only metadata. If the daemon restarts, the cache is empty
    and the user must approve again rather than treating the row as usable.
    """
    rows = [
        dict(row)
        for row in conn.execute(select(vault_grants).where(vault_grants.c.status == "active")).mappings()
        if not cache.covered_names(row["id"])
    ]
    return _expire_grant_rows(conn, rows, cache=cache, reason="grant-expired")


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
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
    if row is None:
        raise RequestNotFoundError(request_id)
    row_dict = dict(row)
    if row_dict.get("request_type") != "access":
        raise InvalidRequestError("grant approval must complete an access request")
    if row_dict.get("status") != "pending":
        raise InvalidRequestError("grant approval request is not pending")
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
            return GrantApproval(members=members, session_id=effective_session_id)
        raise InvalidRequestError("grant scope was not offered by the approval request")
    return GrantApproval(members=live_members, session_id=effective_session_id)


def create_grant(
    conn: Connection,
    *,
    scope_type: str,
    scope_ref: str,
    session_id: str | None = None,
    ttl_seconds: int | None = None,
    created_by_request_id: str | None = None,
    deks_by_secret: dict[str, str] | None = None,
    inherit_request_session: bool = True,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
) -> dict[str, Any]:
    if scope_type not in GRANT_SCOPE_TYPES:
        raise InvalidGrantError(f"invalid grant scope_type: {scope_type!r}")
    if not created_by_request_id:
        raise InvalidRequestError("grant creation requires an approval request")
    live_members = [row["name"] for row in _grantable_member_rows(conn, scope_type, scope_ref)]
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
    members = [name for name in members if name in live_members]
    if not members:
        raise InvalidRequestError("grant approval snapshot has no currently grantable members")
    if not deks_by_secret:
        raise InvalidGrantError("grant creation requires a released DEK set")
    missing_deks = [name for name in members if not deks_by_secret.get(name)]
    if missing_deks:
        raise InvalidGrantError(f"grant DEK set is missing: {', '.join(missing_deks)}")
    claim = conn.execute(
        vault_requests.update()
        .where(
            vault_requests.c.id == created_by_request_id,
            vault_requests.c.request_type == "access",
            vault_requests.c.status == "pending",
        )
        .values(status="approved", decided_at=_now())
    )
    if claim.rowcount != 1:
        raise InvalidRequestError("grant approval request is not pending")
    ttl = int(ttl_seconds or DEFAULT_GRANT_TTL_SECONDS[scope_type])
    ttl = max(1, min(ttl, _ttl_cap_for_members(conn, members)))
    now_dt = datetime.now(timezone.utc)
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
                expires_at=(now_dt + timedelta(seconds=ttl)).isoformat(),
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
    cache.put(grant_id, {name: deks_by_secret[name] for name in members})
    return _grant_row_payload(dict(row), cache=cache)


def list_grants(
    conn: Connection,
    *,
    status: str | None = "active",
    session_id: str | None = None,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
) -> list[dict[str, Any]]:
    expire_grants(conn, cache=cache)
    expire_grants_without_cached_deks(conn, cache=cache)
    query = select(vault_grants).order_by(vault_grants.c.created_at.desc(), vault_grants.c.id.desc())
    if status is not None:
        query = query.where(vault_grants.c.status == status)
    if session_id is not None:
        query = query.where(or_(vault_grants.c.session_id.is_(None), vault_grants.c.session_id == session_id))
    return [_grant_row_payload(dict(row), cache=cache) for row in conn.execute(query).mappings()]


def revoke_grant(
    conn: Connection,
    grant_id: str,
    *,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
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
        .values(status="revoked", revoked_at=now)
    )
    cache.drop(grant_id)
    audit(conn, "grant-revoked", grant_id=grant_id, delivery={"scope_type": row_dict["scope_type"], "scope_ref": row_dict["scope_ref"]})
    updated = conn.execute(select(vault_grants).where(vault_grants.c.id == grant_id)).mappings().one()
    return _grant_row_payload(dict(updated), cache=cache)


def find_active_grant_for_secret(
    conn: Connection,
    secret_name: str,
    *,
    session_id: str | None = None,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
) -> dict[str, Any] | None:
    expire_grants(conn, cache=cache)
    rows = [
        dict(row)
        for row in conn.execute(
            select(vault_grants).where(
                vault_grants.c.status == "active",
                or_(vault_grants.c.session_id.is_(None), vault_grants.c.session_id == session_id),
            )
        ).mappings()
    ]
    stale_without_cache: list[dict[str, Any]] = []
    for row in rows:
        members = _loads(row.get("member_snapshot")) or []
        if secret_name not in members:
            continue
        if cache.has(row["id"], secret_name):
            return _grant_row_payload(row, cache=cache)
        stale_without_cache.append(row)
    if stale_without_cache:
        _expire_grant_rows(conn, stale_without_cache, cache=cache, reason="grant-expired")
    return None


def resolve_secret_access(
    conn: Connection,
    name: str,
    *,
    session_id: str | None = None,
    requester: Any = None,
    delivery: dict[str, Any] | None = None,
    create_request: bool = True,
    cache: VaultGrantDekCache = GRANT_DEK_CACHE,
) -> dict[str, Any]:
    """Resolve an agent access attempt without exposing the value.

    Standard secrets can be delivered by existing one-shot avault paths. Protected
    secrets require an active in-memory grant; otherwise this records an approval
    request and returns its structured card data.
    """
    row = _require_row(conn, name)
    if row.get("protection") == "standard":
        return {"status": "standard", "secret": _meta_payload(row), "envelope": _row_sealed(row)}
    grant = find_active_grant_for_secret(conn, name, session_id=session_id, cache=cache)
    if grant is not None:
        return {"status": "granted", "secret": _meta_payload(row), "grant": grant}
    request_payload = None
    if create_request:
        request_payload = create_access_request(
            conn,
            name,
            requester=requester,
            delivery={**(delivery or {}), "session_id": session_id},
        )
    return {"status": "approval_required", "secret": _meta_payload(row), "request": request_payload}


def list_audit(conn: Connection, *, secret_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    query = select(vault_audit).order_by(vault_audit.c.ts.desc(), vault_audit.c.id.desc()).limit(limit)
    if secret_name is not None:
        query = query.where(vault_audit.c.secret_name == secret_name)
    return [dict(row) for row in conn.execute(query).mappings()]
