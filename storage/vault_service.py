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
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

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


def _preview(value: str) -> str:
    """Non-secret masked hint for list/detail views (last few chars, like #555)."""
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


def _require_row(conn: Connection, name: str) -> dict[str, Any]:
    row = conn.execute(select(vault_secrets).where(vault_secrets.c.name == name)).mappings().first()
    if row is None:
        raise SecretNotFoundError(name)
    return dict(row)


def create_secret(
    conn: Connection,
    *,
    name: str,
    value: str,
    group: str = DEFAULT_GROUP,
    tags: list[str] | None = None,
    protection: str = "standard",
    description: str | None = None,
    source: str = "manual",
    policy: dict[str, Any] | None = None,
    machine_key: bytes | None = None,
    key_path: Path | None = None,
) -> dict[str, Any]:
    """Create a standard-tier secret and return its masked metadata.

    ``policy`` is a non-secret JSON dict (e.g. ``allowed_hosts`` + ``auth`` scheme for
    the brokered ``fetch`` mode); it never contains the value.
    """
    if not vault_crypto.is_valid_secret_name(name):
        raise InvalidSecretNameError(name)
    if protection != "standard":
        raise UnsupportedProtectionError("only the standard tier is available in P0")
    if conn.execute(select(vault_secrets.c.id).where(vault_secrets.c.name == name)).first() is not None:
        raise SecretExistsError(name)

    ensure_default_group(conn)
    sealed = vault_crypto.seal_standard(value.encode("utf-8"), machine_key=machine_key, key_path=key_path)
    now = _now()
    public_meta = {"preview": _preview(value)}
    if description:
        public_meta["description"] = description
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
    audit(conn, "created", secret_name=name)
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
    new_value: str,
    *,
    machine_key: bytes | None = None,
    key_path: Path | None = None,
) -> dict[str, Any]:
    row = _require_row(conn, name)
    if row.get("protection") != "standard":
        raise UnsupportedProtectionError("only the standard tier is available in P0")
    sealed = vault_crypto.seal_standard(new_value.encode("utf-8"), machine_key=machine_key, key_path=key_path)
    public_meta = _loads(row.get("public_meta")) or {}
    public_meta["preview"] = _preview(new_value)
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


def open_secret_value(
    conn: Connection,
    name: str,
    *,
    machine_key: bytes | None = None,
    key_path: Path | None = None,
) -> str:
    """Decrypt one standard-tier secret WITHOUT auditing or bumping usage.

    For callers (e.g. the brokered ``fetch`` proxy) that act on the value and then
    record their own event via :func:`record_proxy_use`. Validate any policy (e.g.
    host allowlist) *before* calling this so a denied request never decrypts.
    """
    row = _require_row(conn, name)
    if row.get("protection") != "standard":
        raise UnsupportedProtectionError(f"{name} is protected-tier (approval is P1)")
    sealed = Sealed(ciphertext=row["ciphertext"], nonce=row["nonce"], wrap_meta=row["wrap_meta"])
    return vault_crypto.open_standard(sealed, machine_key=machine_key, key_path=key_path).decode("utf-8")


def record_proxy_use(conn: Connection, name: str, *, requester: Any = None, delivery: Any = None) -> None:
    """Bump usage + write a value-free ``proxied`` audit row after a brokered request."""
    row = _require_row(conn, name)
    conn.execute(
        vault_secrets.update()
        .where(vault_secrets.c.name == name)
        .values(last_used_at=_now(), use_count=(row.get("use_count") or 0) + 1)
    )
    audit(conn, "proxied", secret_name=name, requester=requester, delivery=delivery)


def resolve(
    conn: Connection,
    names: list[str],
    *,
    requester: Any = None,
    mode: str | None = None,
    machine_key: bytes | None = None,
    key_path: Path | None = None,
) -> dict[str, str]:
    """Decrypt and return the requested secret values (standard tier).

    Records a value-free ``delivered`` audit row and bumps usage per secret. Raises
    ``SecretNotFoundError`` for an unknown name and ``UnsupportedProtectionError`` for a
    protected-tier secret (P1 routes those through approval instead).
    """
    out: dict[str, str] = {}
    for name in names:
        row = _require_row(conn, name)
        if row.get("protection") != "standard":
            raise UnsupportedProtectionError(f"{name} is protected-tier (approval is P1)")
        sealed = Sealed(ciphertext=row["ciphertext"], nonce=row["nonce"], wrap_meta=row["wrap_meta"])
        value = vault_crypto.open_standard(sealed, machine_key=machine_key, key_path=key_path)
        out[name] = value.decode("utf-8")
        conn.execute(
            vault_secrets.update()
            .where(vault_secrets.c.name == name)
            .values(last_used_at=_now(), use_count=(row.get("use_count") or 0) + 1)
        )
        audit(conn, "delivered", secret_name=name, requester=requester, delivery={"mode": mode})
    return out


def create_provision_request(
    conn: Connection,
    name: str,
    *,
    reason: str | None = None,
    skill: str | None = None,
    requester: Any = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """Record an agent's request for a missing secret (dynamic ask)."""
    request_id = _id("vrq")
    now = _now()
    conn.execute(
        vault_requests.insert().values(
            id=request_id,
            request_type="provision",
            secret_name=name,
            requester=json.dumps(requester) if requester is not None else None,
            delivery=json.dumps({"reason": reason, "skill": skill}) if (reason or skill) else None,
            status="pending",
            message_id=message_id,
            created_at=now,
        )
    )
    audit(conn, "provision_requested", secret_name=name, requester=requester, request_id=request_id)
    return {"id": request_id, "secret_name": name, "status": "pending", "created_at": now}


def fulfill_provision(
    conn: Connection,
    request_id: str,
    value: str,
    *,
    group: str = DEFAULT_GROUP,
    description: str | None = None,
    machine_key: bytes | None = None,
    key_path: Path | None = None,
) -> dict[str, Any]:
    """Store the value the user supplied for a pending provision request."""
    row = conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().first()
    if row is None:
        raise RequestNotFoundError(request_id)
    meta = create_secret(
        conn,
        name=row["secret_name"],
        value=value,
        group=group,
        description=description,
        machine_key=machine_key,
        key_path=key_path,
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
