"""CRUD + resolve + audit over the vault tables (design: docs/plans/vaults.md).

Data layer for Vaults, sibling to ``storage/messages_service.py`` etc.: functions take
a SQLAlchemy ``Connection`` and never open their own engine. This module owns the one
place that decrypts a stored secret (``resolve``) and the one place that writes audit
rows, so future invariants land here rather than in callers.

P0 scope: the **standard tier** only (machine-key envelope, ``storage/vault_crypto``).
Creating/resolving a ``protected`` secret raises — that tier (password/passkey, approval,
browser-side decryption) and scope grants are P1. Secret values are accepted/returned as
UTF-8 ``str``; nothing here ever logs or audits a value.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from storage import vault_crypto
from storage.models import vault_audit, vault_groups, vault_requests, vault_secrets
from storage.vault_crypto import Sealed

DEFAULT_GROUP = "default"
_PREVIEW_TAIL = 4


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


class UnsupportedProtectionError(VaultServiceError):
    """A protected-tier operation was attempted before P1 ships it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def value_preview(value: str) -> str:
    """Non-secret masked hint for list/detail views (last few chars, like #555).

    Computed by the caller from the plaintext *before* sealing (avault returns only
    ciphertext), then stored as non-secret metadata via :func:`create_secret`.
    """
    if not value:
        return ""
    if len(value) <= _PREVIEW_TAIL:
        return "•" * len(value)
    return "…" + value[-_PREVIEW_TAIL:]


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _meta_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Masked, value-free metadata for a secret row."""
    public_meta = _loads(row.get("public_meta")) or {}
    return {
        "name": row["name"],
        "group": row.get("group_name"),
        "tags": _loads(row.get("tags")) or [],
        "kind": row.get("kind"),
        "protection": row.get("protection"),
        "signer_kind": row.get("signer_kind"),
        "source": row.get("source"),
        "description": public_meta.get("description"),
        "preview": public_meta.get("preview", ""),
        # Policy is non-secret (allowed hosts, auth scheme name) — safe to surface.
        "policy": _loads(row.get("policy")) or {},
        "last_used_at": row.get("last_used_at"),
        "use_count": row.get("use_count"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


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
    preview: str = "",
    group: str = DEFAULT_GROUP,
    tags: list[str] | None = None,
    protection: str = "standard",
    description: str | None = None,
    source: str = "manual",
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a standard-tier secret from an avault-sealed envelope; return masked metadata.

    The value is sealed by the caller via the avault client (this layer never sees
    plaintext or keys). ``preview`` is the caller-computed non-secret last-4 hint.

    ``policy`` is a non-secret JSON dict (e.g. ``allowed_hosts`` + ``auth`` scheme for
    the brokered ``fetch`` mode); it never contains the value.
    """
    if not vault_crypto.is_valid_secret_name(name):
        raise InvalidSecretNameError(name)
    if protection != "standard":
        raise UnsupportedProtectionError("only the standard tier is available in P0")
    if conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).first() is not None:
        raise SecretExistsError(name)

    _ensure_group(conn, group)
    now = _now()
    public_meta = {"preview": preview}
    if description:
        public_meta["description"] = description
    try:
        conn.execute(
            vault_secrets.insert().values(
                id=_id("vlt"),
                name=name,
                group_name=group,
                tags=json.dumps(tags) if tags else None,
                kind="static",
                protection="standard",
                source=source,
                ciphertext=sealed.ciphertext,
                nonce=sealed.nonce,
                wrap_meta=sealed.wrap_meta,
                public_meta=json.dumps(public_meta),
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
    preview: str = "",
) -> dict[str, Any]:
    row = _require_row(conn, name)
    if row.get("protection") != "standard":
        raise UnsupportedProtectionError("only the standard tier is available in P0")
    public_meta = _loads(row.get("public_meta")) or {}
    public_meta["preview"] = preview
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            wrap_meta=sealed.wrap_meta,
            public_meta=json.dumps(public_meta),
            updated_at=_now(),
        )
    )
    audit(conn, "updated", secret_name=name)
    return _meta_payload(_require_row(conn, name))


def delete_secret(conn: Connection, name: str) -> None:
    if conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).first() is None:
        raise SecretNotFoundError(name)
    conn.execute(vault_secrets.delete().where(vault_secrets.c.name == name))
    audit(conn, "deleted", secret_name=name)


def get_secret_policy(conn: Connection, name: str) -> dict[str, Any]:
    """Return the secret's non-secret policy dict (allowed_hosts, auth scheme)."""
    return _loads(_require_row(conn, name).get("policy")) or {}


def get_envelope(conn: Connection, name: str) -> Sealed:
    """Return one standard-tier secret's stored envelope (no decrypt, no audit).

    For the brokered ``fetch`` proxy: the caller hands the envelope to the avault
    client (which decrypts + delivers), then records its own ``record_proxy_use``.
    Validate any policy (e.g. host allowlist) *before* delivering. Protected-tier
    raises rather than being handed off — this layer never decrypts.
    """
    row = _require_row(conn, name)
    if row.get("protection") != "standard":
        raise UnsupportedProtectionError(f"{name} is protected-tier (approval is P1)")
    return Sealed(ciphertext=row["ciphertext"], nonce=row["nonce"], wrap_meta=row["wrap_meta"])


def record_proxy_use(conn: Connection, name: str, *, requester: Any = None, delivery: Any = None) -> None:
    """Bump usage + write a value-free ``proxied`` audit row after a brokered request."""
    row = _require_row(conn, name)
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(last_used_at=_now(), use_count=vault_secrets.c.use_count + 1)
    )
    audit(conn, "proxied", secret_name=name, requester=requester, delivery=delivery)


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
            raise UnsupportedProtectionError(f"{name} is protected-tier (approval is P1)")
        out[name] = Sealed(ciphertext=row["ciphertext"], nonce=row["nonce"], wrap_meta=row["wrap_meta"])
    return out


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
    conn.execute(
        vault_requests.insert().values(
            id=request_id,
            request_type="provision",
            secret_name=name,
            requester=json.dumps(requester) if requester is not None else None,
            delivery=json.dumps({"reason": reason, "skill": skill}) if (reason or skill) else None,
            status=status,
            message_id=message_id,
            created_at=now,
            decided_at=now if already else None,
        )
    )
    audit(conn, "provision_requested", secret_name=name, requester=requester, request_id=request_id)
    return {"id": request_id, "secret_name": name, "status": status, "created_at": now}


def fulfill_provision(
    conn: Connection,
    request_id: str,
    sealed: Sealed,
    *,
    preview: str = "",
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
        preview=preview,
        group=group,
        description=description,
    )
    conn.execute(
        vault_requests.update()
        .where(vault_requests.c.id == request_id)
        .values(status="fulfilled", decided_at=_now())
    )
    return meta


def list_audit(conn: Connection, *, secret_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    query = select(vault_audit).order_by(vault_audit.c.ts.desc(), vault_audit.c.id.desc()).limit(limit)
    if secret_name is not None:
        query = query.where(vault_audit.c.secret_name == secret_name)
    return [dict(row) for row in conn.execute(query).mappings()]
