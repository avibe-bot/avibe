from __future__ import annotations

import re
import hashlib
import hmac
import os
import secrets
import stat as stat_module
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from config import paths
from config.v2_config import V2Config, config_file_lock
from core.avibe_cloud import avibe_cloud_connect_guidance, base_public_url
from core.show_runtime_failures import ShowRuntimeFailureClass, classify_show_runtime_failure
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
from storage.models import agent_sessions, show_page_access_entries, show_pages
from storage.pagination import PageRequest, PageResult, page_sequence
from vibe.i18n import t

VISIBILITY_PRIVATE = "private"
VISIBILITY_LIMITED = "limited"
VISIBILITY_PUBLIC = "public"
VISIBILITY_OFFLINE = "offline"
VISIBILITIES = {
    VISIBILITY_PRIVATE,
    VISIBILITY_LIMITED,
    VISIBILITY_PUBLIC,
    VISIBILITY_OFFLINE,
}
ACCESS_MODE_PRIVATE = "private"
ACCESS_MODE_LIMITED = "limited"
ACCESS_MODE_PUBLIC = "public"
ACCESS_MODES = {ACCESS_MODE_PRIVATE, ACCESS_MODE_LIMITED, ACCESS_MODE_PUBLIC}
# The Limited audience is one heterogeneous set of read-only grants, OR-ed at
# admission: an email, an organization group, or "everyone in this
# organization". They are peers -- no kind outranks or implies another.
ACCESS_ENTRY_KIND_EMAIL = "email"
ACCESS_ENTRY_KIND_GROUP = "group"
ACCESS_ENTRY_KIND_ORGANIZATION = "organization"
ACCESS_ENTRY_KINDS = (
    ACCESS_ENTRY_KIND_EMAIL,
    ACCESS_ENTRY_KIND_GROUP,
    ACCESS_ENTRY_KIND_ORGANIZATION,
)
# Kinds whose meaning is relative to the organization that owns the page. A
# Personal instance has no organization, so it can hold none of them.
ORGANIZATION_ACCESS_ENTRY_KINDS = (
    ACCESS_ENTRY_KIND_GROUP,
    ACCESS_ENTRY_KIND_ORGANIZATION,
)
# Active-member roles the show-identity organization block may assert. A
# missing or unknown role is fail-closed for group and organization entries.
SHOW_ACCESS_ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
# Bound the group list one assertion may claim, so matching a visitor stays a
# bounded amount of work. It is independent of SHOW_ACCESS_GROUP_MAX_COUNT (how
# many groups a page may grant), and the list is never persisted: what outlives
# the match is the single matched entry.
SHOW_ACCESS_VISITOR_GROUP_MAX_COUNT = 256
SHOW_ACCESS_EMAIL_MAX_COUNT = 64
SHOW_ACCESS_GROUP_MAX_COUNT = 64
# Per-kind write caps. "This organization may read" is one switch, so it caps at
# one; the list kinds get the same bound the email audience already had.
SHOW_ACCESS_ENTRY_MAX_COUNTS = {
    ACCESS_ENTRY_KIND_EMAIL: SHOW_ACCESS_EMAIL_MAX_COUNT,
    ACCESS_ENTRY_KIND_GROUP: SHOW_ACCESS_GROUP_MAX_COUNT,
    ACCESS_ENTRY_KIND_ORGANIZATION: 1,
}
SHOW_ACCESS_ENTRY_VALUE_MAX_LENGTH = 320
SHARE_ID_BYTES = 8
SHOW_EVENT_WRITE_TOKEN_COOKIE = "vibe_show_event_token"
SHOW_EVENT_WRITE_TOKEN_HEADER = "X-Vibe-Show-Token"
SHOW_CLI_EVENT_TOKEN_HEADER = "X-Vibe-Show-Cli-Token"
SHOW_PAGE_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS = 30
# Only the head of index.html is scanned for the icon <link> (it lives in <head>,
# at the top). Bounds the per-page read so a huge inline page can't stall
# /api/show-pages or allocate a large string (§7.1f review).
_ICON_INDEX_HEAD_LIMIT = 64 * 1024
# Hard cap on a servable icon's file size. A page could point <link rel="icon"> at a
# large in-workspace asset (a screenshot, a generated image with a whitelisted
# extension); resolving/serving it would read the whole file into memory, and the
# token hash runs for EVERY row of /api/show-pages. Above the cap the icon is treated
# as "no icon" (letter avatar) so the inventory can't be made to allocate hundreds of
# MB. An icon is displayed ~40px — 2 MiB is already very generous (§7.1f review).
_ICON_MAX_BYTES = 2 * 1024 * 1024
# Whitelisted image extensions -> Content-Type served by the icon endpoint. A page
# icon MUST be one of these (§7.1f): anything else is treated as "no icon" so a page
# can't smuggle an executable/HTML asset through the thumbnail. Shared by the
# resolver (has-icon policy) and the serving endpoint (content-type) — one source.
SHOW_PAGE_ICON_CONTENT_TYPES: dict[str, str] = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "ico": "image/x-icon",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EMAIL_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_NORMALIZED_EMAIL_PATTERN = re.compile(
    rf"^{_EMAIL_ATOM}(?:\.{_EMAIL_ATOM})*@{_DNS_LABEL}(?:\.{_DNS_LABEL})*$"
)
_ASCII_SURROUNDING_WHITESPACE = " \t\r\n\f\v"
_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)
# A custom public share suffix lands directly in the ``/p/<share_id>/`` URL, so
# keep it to URL-safe slug characters: start and end alphanumeric, with dash and
# underscore allowed in between. 3–64 chars balances "memorable" against trivial
# squatting/guessing of an already-public page.
SHARE_ID_MIN_LENGTH = 3
SHARE_ID_MAX_LENGTH = 64
_SHARE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,62}[A-Za-z0-9]$")
_LIKE_ESCAPE = "\\"


class ShowPageError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ShowPage:
    session_id: str
    access_mode: str
    access_revision: int
    share_id: str | None
    offline_at: str | None
    created_at: str
    updated_at: str

    @property
    def offline(self) -> bool:
        return self.offline_at is not None

    @property
    def visibility(self) -> str:
        """Compatibility projection for callers not yet migrated to ShowAccess."""

        return VISIBILITY_OFFLINE if self.offline else self.access_mode


@dataclass(frozen=True)
class ShowAccessEntry:
    """One read-only grant in a Limited audience.

    ``organization_id`` is set for ``group`` and ``organization`` entries and is
    always the organization that owns the page -- never one the caller names.
    """

    kind: str
    value: str
    organization_id: str | None = None


@dataclass(frozen=True)
class ShowAccess:
    page_id: str
    access_mode: str
    share_id: str | None
    revision: int
    entries: tuple[ShowAccessEntry, ...] = ()
    # Kept as a stored field so existing constructors that pass
    # ``normalized_emails=`` still construct, and so equality for those
    # callers stays email-shaped. It is always the email slice of ``entries``.
    normalized_emails: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.normalized_emails and not self.entries:
            object.__setattr__(
                self,
                "entries",
                tuple(
                    ShowAccessEntry(kind=ACCESS_ENTRY_KIND_EMAIL, value=email)
                    for email in self.normalized_emails
                ),
            )
        object.__setattr__(
            self,
            "normalized_emails",
            tuple(
                entry.value
                for entry in self.entries
                if entry.kind == ACCESS_ENTRY_KIND_EMAIL
            ),
        )

    def entries_of_kind(self, kind: str) -> tuple[ShowAccessEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == kind)


@dataclass(frozen=True)
class ShowAccessVisitor:
    """Identity used to match a Limited ``/p`` audience.

    ``organization_id`` / ``organization_member_id`` / ``organization_role`` /
    ``group_ids`` are the optional show-identity organization block. Absence is
    fail-closed for group and organization entries and never grants them.
    """

    normalized_email: str
    organization_id: str | None = None
    organization_member_id: str | None = None
    organization_role: str | None = None
    group_ids: frozenset[str] = frozenset()

    @property
    def has_organization_block(self) -> bool:
        return bool(
            self.organization_id
            and self.organization_member_id
            and self.organization_role in SHOW_ACCESS_ORGANIZATION_ROLES
        )


def limited_show_access_grant(
    access: ShowAccess,
    visitor: ShowAccessVisitor,
) -> ShowAccessEntry | None:
    """Return the audience entry that admits a Limited visitor, if any.

    The three kinds are peers and OR-ed: an email hit, a group intersection,
    or active membership of the page's organization. Every hit is read-only;
    this function does not grant HMR, annotations, or Agent. Group and
    organization entries fail closed when the visitor has no organization
    block, including a block for a different organization.

    The matched entry -- not the visitor's membership list -- is what a caller
    persists to re-check a later request: one entry is bounded by the same
    per-entry write caps the audience already enforces, while a membership
    list is bounded only by the identity provider.
    """

    if access.access_mode != ACCESS_MODE_LIMITED:
        return None
    for entry in access.entries:
        if entry.kind == ACCESS_ENTRY_KIND_EMAIL:
            if visitor.normalized_email and visitor.normalized_email == entry.value:
                return entry
            continue
        if not visitor.has_organization_block:
            continue
        if entry.kind == ACCESS_ENTRY_KIND_GROUP:
            if (
                entry.organization_id == visitor.organization_id
                and entry.value in visitor.group_ids
            ):
                return entry
        elif entry.kind == ACCESS_ENTRY_KIND_ORGANIZATION:
            if (
                entry.value == visitor.organization_id
                and entry.organization_id == visitor.organization_id
                and visitor.organization_role in SHOW_ACCESS_ORGANIZATION_ROLES
            ):
                return entry
    return None


def limited_show_access_admits(access: ShowAccess, visitor: ShowAccessVisitor) -> bool:
    """Whether any Limited audience entry admits this visitor."""

    return limited_show_access_grant(access, visitor) is not None


def limited_show_access_grant_is_current(
    access: ShowAccess,
    grant: ShowAccessEntry | None,
) -> bool:
    """Whether a previously matched entry is still in the Limited audience.

    Re-checking the grant instead of re-running the match keeps a resumed
    visitor's proof bounded. It is fail-closed and self-healing: withdrawing
    the entry ends the grant immediately, and a visitor who still matches some
    other entry is re-admitted through a fresh identity round trip.
    """

    if grant is None or access.access_mode != ACCESS_MODE_LIMITED:
        return False
    return any(
        entry.kind == grant.kind
        and entry.value == grant.value
        and entry.organization_id == grant.organization_id
        for entry in access.entries
    )


@dataclass(frozen=True)
class ShowAccessApplyResult:
    status: str
    show_access: ShowAccess


def validate_session_id(session_id: str) -> str:
    value = (session_id or "").strip()
    if not value:
        raise ShowPageError("Session ID is required.", code="missing_session_id")
    if not _SESSION_ID_PATTERN.fullmatch(value):
        raise ShowPageError(
            "Session ID may contain only letters, numbers, underscore, dash, dot, and colon.",
            code="invalid_session_id",
        )
    return value


def validate_share_id(share_id: str) -> str:
    value = (share_id or "").strip()
    if not value:
        raise ShowPageError("A custom link is required.", code="missing_share_id")
    if not _SHARE_ID_PATTERN.fullmatch(value):
        raise ShowPageError(
            "A custom link may contain only letters, numbers, dash, and underscore, "
            f"must start and end with a letter or number, and be {SHARE_ID_MIN_LENGTH}–"
            f"{SHARE_ID_MAX_LENGTH} characters long.",
            code="invalid_share_id",
        )
    return value


def normalize_show_access_email(raw: str) -> str:
    if not isinstance(raw, str):
        raise ShowPageError("Invalid Show Page email.", code="invalid_email")
    value = raw.strip(_ASCII_SURROUNDING_WHITESPACE).translate(
        _ASCII_LOWER_TRANSLATION
    )
    if (
        len(value) > 320
        or not value
        or _NORMALIZED_EMAIL_PATTERN.fullmatch(value) is None
    ):
        raise ShowPageError("Invalid Show Page email.", code="invalid_email")
    return value


def normalize_show_access_emails(emails: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(emails, (list, tuple)):
        raise ShowPageError("Invalid Show Page email list.", code="invalid_email")
    if len(emails) > SHOW_ACCESS_EMAIL_MAX_COUNT:
        raise ShowPageError(
            f"A Show Page may grant access to at most {SHOW_ACCESS_EMAIL_MAX_COUNT} emails.",
            code="invalid_email",
        )
    return tuple(sorted({normalize_show_access_email(email) for email in emails}))


def _normalize_opaque_entry_value(raw: Any) -> str:
    """Normalize a group/organization identifier: an opaque, non-empty string."""

    if not isinstance(raw, str):
        raise ShowPageError("Invalid Show Page access entry.", code="invalid_access_entry")
    value = raw.strip()
    if not value or len(value) > SHOW_ACCESS_ENTRY_VALUE_MAX_LENGTH:
        raise ShowPageError("Invalid Show Page access entry.", code="invalid_access_entry")
    return value


def _entry_fields(raw: Any) -> tuple[Any, Any, Any]:
    if isinstance(raw, ShowAccessEntry):
        return raw.kind, raw.value, raw.organization_id
    if isinstance(raw, Mapping):
        unexpected = set(raw) - {"kind", "value", "organization_id"}
        if unexpected:
            raise ShowPageError(
                "Invalid Show Page access entry.", code="invalid_access_entry"
            )
        return raw.get("kind"), raw.get("value"), raw.get("organization_id")
    raise ShowPageError("Invalid Show Page access entry.", code="invalid_access_entry")


def normalize_show_access_entries(
    entries: Any,
    *,
    page_organization_id: str | None,
) -> tuple[ShowAccessEntry, ...]:
    """Validate one complete audience against the page's own organization.

    ``page_organization_id`` is the server's answer to "which organization owns
    this instance", so a group or organization entry is only ever stored for
    that organization: a caller-named one that disagrees is rejected rather than
    quietly rewritten, and a Personal instance (no organization) accepts email
    entries only.
    """

    if isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Iterable):
        raise ShowPageError(
            "Invalid Show Page access entry list.", code="invalid_access_entry"
        )

    normalized: dict[tuple[str, str], ShowAccessEntry] = {}
    for raw in entries:
        kind, value, organization_id = _entry_fields(raw)
        if kind not in ACCESS_ENTRY_KINDS:
            raise ShowPageError(
                "Invalid Show Page access entry.", code="invalid_access_entry"
            )
        if kind == ACCESS_ENTRY_KIND_EMAIL:
            if organization_id is not None:
                raise ShowPageError(
                    "An email Show Page access entry has no organization.",
                    code="invalid_access_entry",
                )

            normalized_value = normalize_show_access_email(value)
            entry_organization_id = None
        else:
            if page_organization_id is None:
                raise ShowPageError(
                    "This instance has no organization, so it has no "
                    f"{kind} Show Page access entries.",
                    code="invalid_access_entry",
                )
            # An organization entry names the page's organization in both
            # fields; a group entry only in ``organization_id``. Whatever the
            # caller supplied has to agree with the server's answer, which is
            # what gets stored -- a cross-organization group is rejected, not
            # rewritten into the local one.
            claimed = [organization_id]
            if kind == ACCESS_ENTRY_KIND_ORGANIZATION:
                claimed.append(value)
            if any(
                _normalize_opaque_entry_value(claim) != page_organization_id
                for claim in claimed
                if claim is not None
            ):
                raise ShowPageError(
                    "A Show Page access entry cannot name another organization.",
                    code="invalid_access_entry",
                )
            entry_organization_id = page_organization_id
            normalized_value = (
                page_organization_id
                if kind == ACCESS_ENTRY_KIND_ORGANIZATION
                else _normalize_opaque_entry_value(value)
            )
        normalized[(kind, normalized_value)] = ShowAccessEntry(
            kind=kind,
            value=normalized_value,
            organization_id=entry_organization_id,
        )

    for kind, limit in SHOW_ACCESS_ENTRY_MAX_COUNTS.items():
        if sum(1 for entry_kind, _ in normalized if entry_kind == kind) > limit:
            raise ShowPageError(
                f"A Show Page may grant access to at most {limit} {kind} entries.",
                code="invalid_access_entry",
            )
    return tuple(normalized[key] for key in sorted(normalized))


def show_access_entry_payload(entry: ShowAccessEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "value": entry.value,
        "organization_id": entry.organization_id,
    }


def show_access_payload(show_access: ShowAccess) -> dict[str, Any]:
    return {
        "page_id": show_access.page_id,
        "access_mode": show_access.access_mode,
        "share_id": show_access.share_id,
        "revision": show_access.revision,
        "normalized_emails": list(show_access.normalized_emails),
        "entries": [show_access_entry_payload(entry) for entry in show_access.entries],
    }


def _is_show_access_entries_payload(entries: Any) -> bool:
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, Mapping):
            return False
        if set(entry) - {"kind", "value", "organization_id"}:
            return False
        kind = entry.get("kind")
        value = entry.get("value")
        organization_id = entry.get("organization_id")
        if "kind" in entry and not isinstance(kind, str):
            return False
        if "value" in entry and value is not None and not isinstance(value, str):
            return False
        if (
            "organization_id" in entry
            and organization_id is not None
            and not isinstance(organization_id, str)
        ):
            return False
    return True


def parse_show_access_apply_request(payload: Any) -> dict[str, Any] | None:
    """Return store kwargs for a well-formed apply body, else None.

    Exactly one of ``target_emails`` or ``target_entries`` may be present.
    The email-only form remains the complete-replacement shorthand; the
    entry form is how group and organization grants are written.
    """

    if not isinstance(payload, Mapping):
        return None
    keys = set(payload)
    has_emails = "target_emails" in keys
    has_entries = "target_entries" in keys
    if has_emails == has_entries:
        return None
    expected = {
        "page_id",
        "expected_revision",
        "target_access_mode",
        "target_share_id",
    }
    expected.add("target_emails" if has_emails else "target_entries")
    if keys != expected:
        return None
    page_id = payload.get("page_id")
    expected_revision = payload.get("expected_revision")
    target_access_mode = payload.get("target_access_mode")
    target_share_id = payload.get("target_share_id")
    if (
        not isinstance(page_id, str)
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
        or not isinstance(target_access_mode, str)
        or not (target_share_id is None or isinstance(target_share_id, str))
    ):
        return None
    parsed: dict[str, Any] = {
        "page_id": page_id,
        "expected_revision": expected_revision,
        "target_access_mode": target_access_mode,
        "target_share_id": target_share_id,
    }
    if has_emails:
        emails = payload.get("target_emails")
        if not isinstance(emails, list) or any(
            not isinstance(email, str) for email in emails
        ):
            return None
        parsed["target_emails"] = emails
        return parsed
    entries = payload.get("target_entries")
    if not _is_show_access_entries_payload(entries):
        return None
    parsed["target_entries"] = entries
    return parsed


def show_page_dir(session_id: str) -> Path:
    return paths.get_show_page_dir(validate_session_id(session_id))


def show_event_write_token(session_id: str) -> str:
    return hmac.new(
        _load_or_create_show_event_secret().encode("utf-8"),
        validate_session_id(session_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def show_public_event_write_token(share_id: str, session_id: str) -> str:
    share_value = (share_id or "").strip()
    if not share_value:
        raise ShowPageError("A public share ID is required.", code="missing_share_id")
    session_value = validate_session_id(session_id)
    return hmac.new(
        _load_or_create_show_event_secret().encode("utf-8"),
        f"public-share:{share_value}:session:{session_value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def show_cli_event_token() -> str:
    return hmac.new(
        _load_or_create_show_event_secret().encode("utf-8"),
        b"cli-show-events",
        hashlib.sha256,
    ).hexdigest()


def _load_or_create_show_event_secret() -> str:
    secret_path = paths.get_state_dir() / "show_event_secret"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError:
        secret = ""
    if secret:
        return secret
    secret = secrets.token_urlsafe(48)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secret, encoding="utf-8")
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass
    return secret


def ensure_show_page_dir(session_id: str) -> Path:
    page_dir = show_page_dir(session_id)
    page_dir.mkdir(parents=True, exist_ok=True)
    _write_default_runtime_files(page_dir, validate_session_id(session_id))
    return page_dir


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_share_id() -> str:
    while True:
        candidate = secrets.token_urlsafe(SHARE_ID_BYTES).strip("_-")
        if _SHARE_ID_PATTERN.fullmatch(candidate):
            return candidate


def _like_pattern(value: str, *, prefix: bool = False, contains: bool = False) -> str:
    escaped = (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE + _LIKE_ESCAPE)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    if contains:
        return f"%{escaped}%"
    if prefix:
        return f"{escaped}%"
    return escaped


def _resolve_resource_access_context(user_context: Any = None):
    """Resolve the request context used by Show Page ACL checks."""

    from storage import resource_access_service

    return resource_access_service.resolve_resource_access_context(user_context)


def require_show_page_management(
    connection: Connection,
    session_id: str,
    *,
    user_context: Any = None,
) -> None:
    """Require Instance Editor authority to change the page."""

    session_id = validate_session_id(session_id)
    context = _resolve_resource_access_context(user_context)
    if not _instance_editor_or_owner(context):
        raise ShowPageError("Show Page access is not permitted.", code="resource_access_forbidden")


def require_show_page_sharing_control(
    connection: Connection,
    session_id: str,
    *,
    user_context: Any = None,
) -> None:
    """Require Instance Editor authority to widen anonymous sharing."""

    session_id = validate_session_id(session_id)
    context = _resolve_resource_access_context(user_context)
    if not _instance_editor_or_owner(context):
        raise ShowPageError("Show Page access is not permitted.", code="resource_access_forbidden")


def require_show_page_access_management(
    connection: Connection,
    session_id: str,
    *,
    user_context: Any = None,
) -> None:
    """Require Instance Editor authority to manage the audience or narrow sharing."""

    session_id = validate_session_id(session_id)
    context = _resolve_resource_access_context(user_context)
    if not _instance_editor_or_owner(context):
        raise ShowPageError("Show Page access is not permitted.", code="resource_access_forbidden")


def _instance_editor_or_owner(context: Any) -> bool:
    """The /show Workbench management capability is the Instance Editor role.

    ``show_page_email`` sessions are Viewer-only, so they can never satisfy an
    Editor check. Instance Owner passes as the top of the role ladder.
    """

    return bool(context is not None and context.has_role("editor"))


def private_url(session_id: str, *, config: V2Config | None = None) -> str | None:
    base = base_public_url(config)
    if not base:
        return None
    return urljoin(base + "/", f"show/{validate_session_id(session_id)}/")


def public_url(share_id: str | None, *, config: V2Config | None = None) -> str | None:
    if not share_id:
        return None
    base = base_public_url(config)
    if not base:
        return None
    return urljoin(base + "/", f"p/{share_id}/")


class ShowPageStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or paths.get_sqlite_state_path()
        if db_path is None:
            ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
        else:
            from storage.migrations import run_migrations

            run_migrations(self.db_path)
        self.engine = create_sqlite_engine(self.db_path)

    def close(self) -> None:
        self.engine.dispose()

    def get(self, session_id: str) -> ShowPage | None:
        session_id = validate_session_id(session_id)
        with self.engine.connect() as conn:
            row = conn.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1)).mappings().first()
            return _page_from_row(row) if row else None

    def get_access(self, page_id: str) -> ShowAccess | None:
        page_id = validate_session_id(page_id)
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(show_pages)
                    .where(show_pages.c.session_id == page_id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return _show_access_from_row(conn, row) if row else None

    def require_access_settings(
        self,
        page_id: str,
        *,
        user_context: Any = None,
    ) -> ShowAccess:
        page_id = validate_session_id(page_id)
        context = _resolve_resource_access_context(user_context)
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(show_pages)
                    .where(show_pages.c.session_id == page_id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ShowPageError(
                    "This session has no Show Page.",
                    code="show_page_not_found",
                )
            self._require_sharing_control(conn, page_id, context)
            return _show_access_from_row(conn, row)

    def apply_access(
        self,
        page_id: str,
        *,
        expected_revision: int,
        target_access_mode: str,
        target_share_id: str | None,
        target_emails: list[str] | tuple[str, ...] | None = None,
        target_entries: Any = None,
        user_context: Any = None,
    ) -> ShowAccessApplyResult:
        """Replace the whole ShowAccess aggregate under a revision check.

        The audience is a complete entry set, not a delta: whatever is not in
        ``target_entries`` is revoked. ``target_emails`` is the email-only
        shorthand for the same replacement, so a caller that passes it revokes
        every group and organization entry too. Exactly one of the two may be
        given.
        """

        page_id = validate_session_id(page_id)
        context = _resolve_resource_access_context(user_context)
        current = self.require_access_settings(page_id, user_context=context)

        try:
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                raise ShowPageError("Invalid ShowAccess revision.", code="invalid_revision")
            if target_access_mode not in ACCESS_MODES:
                raise ShowPageError("Invalid ShowAccess mode.", code="invalid_access_mode")
            if target_emails is not None and target_entries is not None:
                raise ShowPageError(
                    "Pass either a ShowAccess email list or an entry list, not both.",
                    code="invalid_show_access",
                )
            normalized_share_id = (
                validate_share_id(target_share_id)
                if target_share_id is not None
                else None
            )
            if target_access_mode == ACCESS_MODE_PRIVATE and normalized_share_id is None:
                normalized_share_id = current.share_id
        except (ShowPageError, TypeError):
            return ShowAccessApplyResult(status="invalid", show_access=current)

        status = "applied"
        now = _utc_now_iso()
        try:
            # Organization-scoped entries are stamped with the instance's
            # current organization. Hold the pairing lock that ``ensure`` /
            # ``get_for_use`` already use across that read AND the database
            # write, so a re-pair cannot land between them and persist the
            # former organization's ID.
            with config_file_lock():
                try:
                    normalized_entries = self._normalize_target_entries(
                        target_emails=target_emails,
                        target_entries=target_entries,
                    )
                    if target_access_mode == ACCESS_MODE_LIMITED:
                        if normalized_share_id is None or not normalized_entries:
                            raise ShowPageError(
                                "Limited ShowAccess requires a share ID and at least one "
                                "access entry.",
                                code="invalid_show_access",
                            )
                    elif normalized_entries:
                        raise ShowPageError(
                            "Only Limited ShowAccess may contain access entries.",
                            code="invalid_show_access",
                        )
                    if target_access_mode == ACCESS_MODE_PUBLIC and normalized_share_id is None:
                        raise ShowPageError(
                            "Public ShowAccess requires a share ID.",
                            code="invalid_show_access",
                        )
                except (ShowPageError, TypeError):
                    return ShowAccessApplyResult(status="invalid", show_access=current)
                with self.engine.begin() as conn:
                    row = (
                        conn.execute(
                            select(show_pages)
                            .where(show_pages.c.session_id == page_id)
                            .limit(1)
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        raise ShowPageError(
                            "This session has no Show Page.",
                            code="show_page_not_found",
                        )
                    self._require_sharing_control(conn, page_id, context)
                    current = _show_access_from_row(conn, row)
                    if current.revision != expected_revision:
                        return ShowAccessApplyResult(
                            status="conflict",
                            show_access=current,
                        )
                    canonical_target = (
                        target_access_mode,
                        normalized_share_id,
                        normalized_entries,
                    )
                    canonical_current = (
                        current.access_mode,
                        current.share_id,
                        current.entries,
                    )
                    if canonical_target == canonical_current:
                        return ShowAccessApplyResult(
                            status="no_change",
                            show_access=current,
                        )
                    archived = conn.execute(
                        select(agent_sessions.c.status)
                        .where(agent_sessions.c.id == page_id)
                        .limit(1)
                    ).scalar_one_or_none()
                    if archived == "archived":
                        return ShowAccessApplyResult(
                            status="invalid",
                            show_access=current,
                        )
                    result = conn.execute(
                        update(show_pages)
                        .where(
                            show_pages.c.session_id == page_id,
                            show_pages.c.access_revision == expected_revision,
                        )
                        .values(
                            access_mode=target_access_mode,
                            share_id=normalized_share_id,
                            access_revision=expected_revision + 1,
                            updated_at=now,
                        )
                    )
                    if not result.rowcount:
                        status = "conflict"
                    else:
                        conn.execute(
                            delete(show_page_access_entries).where(
                                show_page_access_entries.c.page_id == page_id
                            )
                        )
                        if normalized_entries:
                            conn.execute(
                                insert(show_page_access_entries),
                                [
                                    {
                                        "page_id": page_id,
                                        "kind": entry.kind,
                                        "value": entry.value,
                                        "organization_id": entry.organization_id,
                                        "created_at": now,
                                    }
                                    for entry in normalized_entries
                                ],
                            )
        except IntegrityError:
            status = "share_id_taken"

        latest = self.get_access(page_id)
        if latest is None:
            raise ShowPageError(
                "This session has no Show Page.",
                code="show_page_not_found",
            )
        return ShowAccessApplyResult(status=status, show_access=latest)

    def _normalize_target_entries(
        self,
        *,
        target_emails: list[str] | tuple[str, ...] | None,
        target_entries: Any,
    ) -> tuple[ShowAccessEntry, ...]:
        if target_entries is None:
            return tuple(
                ShowAccessEntry(kind=ACCESS_ENTRY_KIND_EMAIL, value=email)
                for email in normalize_show_access_emails(target_emails or ())
            )
        if isinstance(target_entries, (str, bytes, Mapping)) or not isinstance(
            target_entries, Iterable
        ):
            raise ShowPageError(
                "Invalid Show Page access entry list.", code="invalid_access_entry"
            )
        materialized = list(target_entries)
        # Resolving the instance organization reads configuration, so only an
        # audience that actually contains an organization-scoped entry pays for
        # the answer; an email-only audience never needs one.
        needs_organization = any(
            _entry_fields(raw)[0] in ORGANIZATION_ACCESS_ENTRY_KINDS
            for raw in materialized
        )
        return normalize_show_access_entries(
            materialized,
            page_organization_id=(
                self._page_organization_id() if needs_organization else None
            ),
        )

    @classmethod
    def _page_organization_id(cls) -> str | None:
        """The organization that owns this instance, or None when Personal.

        Group and organization entries are stored against this value, never
        against one a caller supplies, so a Personal instance simply has no
        organization to grant and rejects both kinds.
        """

        ownership = cls._resolve_instance_ownership()
        if not isinstance(ownership, Mapping) or ownership.get("mode") != "organization":
            return None
        organization_id = ownership.get("organization_id")
        if not isinstance(organization_id, str) or not organization_id.strip():
            return None
        return organization_id.strip()

    def get_by_share_id(self, share_id: str) -> ShowPage | None:
        share_id = (share_id or "").strip()
        if not share_id:
            return None
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(show_pages).where(show_pages.c.share_id == share_id).limit(1)).mappings().first()
            )
            return _page_from_row(row) if row else None

    def require_access(self, session_id: str, *, user_context: Any = None) -> ShowPage:
        """Return a Show Page only to an Instance Viewer (owner/editor/viewer).

        ``/show`` admission is the Instance role alone, independent of the
        sharing list and of Resource ACL (§3.2): any Viewer enters the Workbench,
        while a signed ``show_page_email`` session never does.
        """

        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        with self.engine.begin() as conn:
            row = (
                conn.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1))
                .mappings()
                .first()
            )
            if row is None:
                raise ShowPageError("This session has no Show Page.", code="show_page_not_found")
            self._require_resource_access(context)
            return _page_from_row(row)

    def require_management(self, session_id: str, *, user_context: Any = None) -> ShowPage:
        """Return a Show Page only when the caller may change its resource."""

        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        with self.engine.begin() as conn:
            row = (
                conn.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1))
                .mappings()
                .first()
            )
            if row is None:
                raise ShowPageError("This session has no Show Page.", code="show_page_not_found")
            self._require_resource_management(conn, session_id, context)
            return _page_from_row(row)

    def list(self, *, visibility: str | None = None, user_context: Any = None) -> list[ShowPage]:
        result = self.list_page(visibility=visibility, page_request=None, user_context=user_context)
        return result.items

    def list_page(
        self,
        *,
        visibility: str | None = None,
        session_id: str | None = None,
        updated_after: str | None = None,
        updated_before: str | None = None,
        query: str | None = None,
        page_request: PageRequest | None,
        user_context: Any = None,
    ) -> PageResult[ShowPage]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise ShowPageError(f"Unsupported visibility: {visibility}", code="invalid_visibility")
        statement = select(show_pages)
        if visibility is not None:
            if visibility == VISIBILITY_OFFLINE:
                statement = statement.where(show_pages.c.offline_at.is_not(None))
            else:
                statement = statement.where(
                    show_pages.c.offline_at.is_(None),
                    show_pages.c.access_mode == visibility,
                )
        if session_id:
            statement = statement.where(show_pages.c.session_id.like(_like_pattern(session_id, prefix=True), escape=_LIKE_ESCAPE))
        if updated_after:
            statement = statement.where(show_pages.c.updated_at >= updated_after)
        if updated_before:
            statement = statement.where(show_pages.c.updated_at <= updated_before)
        if query:
            pattern = _like_pattern(query, contains=True)
            clauses = [
                show_pages.c.session_id.like(pattern, escape=_LIKE_ESCAPE),
                show_pages.c.share_id.like(pattern, escape=_LIKE_ESCAPE),
                show_pages.c.access_mode.like(pattern, escape=_LIKE_ESCAPE),
            ]
            if query.lower() in VISIBILITY_OFFLINE:
                clauses.append(show_pages.c.offline_at.is_not(None))
            statement = statement.where(or_(*clauses))
        statement = statement.order_by(show_pages.c.updated_at.desc(), show_pages.c.session_id.asc())
        with self.engine.begin() as conn:
            rows = conn.execute(statement).mappings().all()
        return page_sequence([_page_from_row(row) for row in rows], page_request)

    @staticmethod
    def _require_resource_access(user_context: Any) -> None:
        if not (
            user_context is not None
            and user_context.has_role("viewer")
            and user_context.instance_access_source != "show_page_email"
        ):
            raise ShowPageError("Show Page access is not permitted.", code="resource_access_forbidden")

    @staticmethod
    def _require_resource_management(connection, session_id: str, user_context: Any) -> None:
        require_show_page_management(connection, session_id, user_context=user_context)

    @staticmethod
    def _require_sharing_control(connection, session_id: str, user_context: Any) -> None:
        require_show_page_sharing_control(connection, session_id, user_context=user_context)

    @staticmethod
    def _require_access_management(connection, session_id: str, user_context: Any) -> None:
        require_show_page_access_management(connection, session_id, user_context=user_context)

    @classmethod
    def _require_visibility_transition_control(
        cls,
        connection,
        session_id: str,
        user_context: Any,
        *,
        current_visibility: str,
        target_visibility: str,
    ) -> None:
        if target_visibility == VISIBILITY_PUBLIC or (
            current_visibility == VISIBILITY_OFFLINE
            and target_visibility != VISIBILITY_OFFLINE
        ):
            cls._require_sharing_control(connection, session_id, user_context)
            return
        cls._require_access_management(connection, session_id, user_context)

    @staticmethod
    def _require_create_access(user_context: Any) -> None:
        if user_context.has_role("editor"):
            return
        raise ShowPageError("Show Page access is not permitted.", code="resource_access_forbidden")

    @staticmethod
    def _require_project_edit_access(connection: Connection, session_id: str, user_context: Any) -> None:
        """Require Editor access to the Project that owns this session."""

        if user_context.is_instance_owner:
            return
        from storage import project_access_service

        scope_id = connection.execute(
            select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id).limit(1)
        ).scalar_one_or_none()
        project_id = project_access_service.project_id_from_scope_id(scope_id)
        if project_id is None or not project_access_service.role_allows(
            project_access_service.get_effective_project_role(connection, user_context, project_id),
            "editor",
        ):
            raise ShowPageError("Show Page access is not permitted.", code="resource_access_forbidden")

    @staticmethod
    def _resolve_instance_ownership() -> dict[str, Any]:
        from vibe import permissions

        return permissions.resolve_current_instance_ownership()

    @staticmethod
    def _reconcile_resource_policy(ownership: dict[str, Any]) -> dict[str, Any]:
        """Map the instance-ownership fence to a frontend ``ownership_status``.

        §3.2 removed show_page from the Resource ACL, so there is no policy row
        to reconcile: the status is derived from the ownership fence alone and
        ``policy`` is always ``None``. The ``conflict`` status is obsolete with
        the Resource ACL gone — there is no ``policy_organization_id`` to diverge
        from the instance organization.
        """

        status = {
            "unmanaged": "unmanaged",
            "personal": "unchanged",
            "organization": "unchanged",
            "organization_pending": "pending",
            "configuration_unavailable": "configuration_unavailable",
        }.get(ownership.get("mode"), "unmanaged")
        return {"status": status, "ownership": ownership, "policy": None}

    @classmethod
    def _existing_page_for_use(
        cls,
        connection,
        session_id: str,
        user_context: Any,
        ownership: dict[str, Any],
    ) -> ShowPage | None:
        """Reconcile and authorize an ALREADY-EXISTING page; None when there is none.

        The single owner of "may this caller use this existing page". ``ensure`` /
        ``ensure_active`` take this branch before they consider creating anything,
        and ``get_for_use`` is only this branch — so a read can never drift from
        what the create path would have enforced for the same page.
        """
        existing = (
            connection.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1))
            .mappings()
            .first()
        )
        if existing is None:
            return None
        cls._require_project_edit_access(connection, session_id, user_context)
        cls._require_resource_access(user_context)
        return _page_from_row(existing)

    def get_for_use(self, session_id: str, *, user_context: Any = None) -> ShowPage:
        """Read a page the caller may use, WITHOUT creating one.

        The read-only half of ``ensure_active``: identical reconcile-and-authorize
        enforcement for a page that exists, and ``show_page_not_found`` where
        ``ensure_active`` would have created one. Reading no longer requires taking
        the creation path, which is what keeps the one-shot ``created`` edge — and
        the "visualize this session" prompt it triggers — owned by the single
        caller that honors it.
        """
        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        ownership = self._resolve_instance_ownership()
        with config_file_lock():
            with self.engine.begin() as conn:
                page = self._existing_page_for_use(conn, session_id, context, ownership)
                if page is None:
                    # Absence is only reported to a caller allowed to work on the
                    # project, exactly as ``ensure`` checks before it creates. The
                    # route policy screens the Instance role alone, so skipping this
                    # would let an Editor without project access tell a session that
                    # HAS a page (forbidden) from one that does not (not found) —
                    # turning a read into a page-existence oracle over arbitrary
                    # session ids.
                    self._require_project_edit_access(conn, session_id, context)
                    raise ShowPageError(
                        "This session has no Show Page.",
                        code="show_page_not_found",
                    )
                return page

    def reconcile_resource_policy(
        self,
        session_id: str,
        *,
        user_context: Any = None,
    ) -> dict[str, Any]:
        """Resolve ownership outside SQLite, then reconcile one existing page."""

        session_id = validate_session_id(session_id)
        ownership = self._resolve_instance_ownership()
        with config_file_lock():
            with self.engine.begin() as connection:
                exists = connection.execute(
                    select(show_pages.c.session_id)
                    .where(show_pages.c.session_id == session_id)
                    .limit(1)
                ).scalar_one_or_none()
                if exists is None:
                    raise ShowPageError(
                        "This session has no Show Page.",
                        code="show_page_not_found",
                    )
                return self._reconcile_resource_policy(ownership)

    def ensure(self, session_id: str, *, user_context: Any = None) -> ShowPage:
        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        ownership = self._resolve_instance_ownership()
        now = _utc_now_iso()
        page = ShowPage(
            session_id=session_id,
            access_mode=ACCESS_MODE_PRIVATE,
            access_revision=0,
            share_id=self._unique_share_id(),
            offline_at=None,
            created_at=now,
            updated_at=now,
        )
        with config_file_lock():
            with self.engine.begin() as conn:
                existing = self._existing_page_for_use(conn, session_id, context, ownership)
                if existing is not None:
                    return existing
                self._require_project_edit_access(conn, session_id, context)
                self._require_create_access(context)
                conn.execute(
                    insert(show_pages).values(
                        session_id=page.session_id,
                        access_mode=page.access_mode,
                        access_revision=page.access_revision,
                        share_id=page.share_id,
                        offline_at=page.offline_at,
                        created_at=page.created_at,
                        updated_at=page.updated_at,
                    )
                )
        return page

    def ensure_active(self, session_id: str, *, user_context: Any = None) -> tuple[ShowPage, bool]:
        """Atomically ensure a page for a NON-archived session; return (page, created).

        The existing-row check, the archived check and the insert all run in ONE
        transaction so (a) a concurrent archive can't race the create (TOCTOU),
        and (b) ``created`` is derived from the insert itself — concurrent
        first-ensures don't both report "created" (which would otherwise double-
        send the visualize prompt or collide on the unique key). An existing page
        is returned untouched (archive already took it offline).
        """
        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        ownership = self._resolve_instance_ownership()
        now = _utc_now_iso()
        with config_file_lock():
            with self.engine.begin() as conn:
                existing = self._existing_page_for_use(conn, session_id, context, ownership)
                if existing is not None:
                    return existing, False
                self._require_project_edit_access(conn, session_id, context)
                self._require_create_access(context)
                status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
                ).scalar_one_or_none()
                if status is None:
                    # Unknown session — don't create an orphan page row not tied to any
                    # session lifecycle/archive cleanup (other session-scoped APIs also
                    # treat a missing session as absent).
                    raise ShowPageError(
                        "Cannot create a Show Page for an unknown session.",
                        code="session_not_found",
                    )
                if status == "archived":
                    raise ShowPageError(
                        "Cannot create a Show Page for an archived session.",
                        code="session_archived",
                    )
                created = False
                row = None
                for _ in range(20):
                    result = conn.execute(
                        insert(show_pages)
                        .prefix_with("OR IGNORE")
                        .values(
                            session_id=session_id,
                            access_mode=ACCESS_MODE_PRIVATE,
                            access_revision=0,
                            share_id=_new_share_id(),
                            offline_at=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    created = bool(result.rowcount and result.rowcount > 0)
                    row = (
                        conn.execute(
                            select(show_pages)
                            .where(show_pages.c.session_id == session_id)
                            .limit(1)
                        )
                        .mappings()
                        .first()
                    )
                    if row is not None:
                        break
                if row is None:
                    raise ShowPageError(
                        "Could not allocate a unique share ID.",
                        code="share_id_allocation_failed",
                    )
        return _page_from_row(row), created

    def is_archived(self, session_id: str) -> bool:
        """Whether the session is archived (terminal). Archived sessions reject Show Page
        mutations with ``session_archived`` — see ``ensure_active`` / ``update_visibility``
        / ``set_share_id`` here and the icon-upload guard in ``vibe.api``."""
        with self.engine.connect() as conn:
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
        return status == "archived"

    def set_offline(
        self,
        session_id: str,
        offline: bool,
        *,
        user_context: Any = None,
    ) -> ShowPage:
        """Change operational availability without changing ShowAccess."""

        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    select(show_pages)
                    .where(show_pages.c.session_id == session_id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ShowPageError(
                    "This session has no Show Page.",
                    code="show_page_not_found",
                )
            current_visibility = (
                VISIBILITY_OFFLINE
                if row["offline_at"] is not None
                else str(row["access_mode"])
            )
            target_visibility = (
                VISIBILITY_OFFLINE if offline else str(row["access_mode"])
            )
            self._require_visibility_transition_control(
                conn,
                session_id,
                context,
                current_visibility=current_visibility,
                target_visibility=target_visibility,
            )
            if not offline:
                status = conn.execute(
                    select(agent_sessions.c.status)
                    .where(agent_sessions.c.id == session_id)
                    .limit(1)
                ).scalar_one_or_none()
                if status == "archived":
                    raise ShowPageError(
                        "Cannot bring back the Show Page of an archived session.",
                        code="session_archived",
                    )
            target_offline_at = (row["offline_at"] or now) if offline else None
            if target_offline_at != row["offline_at"]:
                conn.execute(
                    update(show_pages)
                    .where(show_pages.c.session_id == session_id)
                    .values(offline_at=target_offline_at, updated_at=now)
                )
        updated = self.get(session_id)
        assert updated is not None
        return updated

    def update_visibility(self, session_id: str, visibility: str, *, user_context: Any = None) -> ShowPage:
        """Compatibility adapter over orthogonal availability and ShowAccess.

        New audience callers use :meth:`apply_access`. The legacy CLI still uses
        this adapter, but Web callers have no second audience write path.
        """

        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        if visibility not in {
            VISIBILITY_PRIVATE,
            VISIBILITY_PUBLIC,
            VISIBILITY_OFFLINE,
        }:
            raise ShowPageError(f"Unsupported visibility: {visibility}", code="invalid_visibility")
        existing = self.get(session_id)
        if existing is not None:
            with self.engine.connect() as conn:
                self._require_visibility_transition_control(
                    conn,
                    session_id,
                    context,
                    current_visibility=existing.visibility,
                    target_visibility=visibility,
                )
            page = existing
        else:
            page = None
        if visibility != VISIBILITY_OFFLINE and self.is_archived(session_id):
            raise ShowPageError(
                "Cannot republish the Show Page of an archived session.",
                code="session_archived",
            )
        if page is None:
            page = self.ensure(session_id, user_context=context)

        if visibility != VISIBILITY_OFFLINE:
            access = self.require_access_settings(session_id, user_context=context)
            result = self.apply_access(
                session_id,
                expected_revision=access.revision,
                target_access_mode=visibility,
                target_share_id=access.share_id,
                target_emails=[],
                user_context=context,
            )
            if result.status == "conflict":
                raise ShowPageError(
                    "ShowAccess changed concurrently.",
                    code="show_access_conflict",
                )
            if result.status not in {"applied", "no_change"}:
                raise ShowPageError(
                    "ShowAccess could not be updated.",
                    code=result.status,
                )
            return self.set_offline(session_id, False, user_context=context)

        return self.set_offline(session_id, True, user_context=context)

    def rotate_share(
        self,
        session_id: str,
        *,
        user_context: Any = None,
    ) -> tuple[ShowPage, str | None]:
        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        if self.is_archived(session_id):
            raise ShowPageError(
                "Cannot rotate the share link of an archived session.",
                code="session_archived",
            )
        if self.get(session_id) is None:
            self.ensure(session_id, user_context=context)
        access = self.require_access_settings(session_id, user_context=context)
        if access.access_mode not in {ACCESS_MODE_LIMITED, ACCESS_MODE_PUBLIC}:
            raise ShowPageError(
                "Share links can only be rotated while the Show Page is shared.",
                code="not_shared",
            )
        previous_share_id = access.share_id
        for _ in range(20):
            result = self.apply_access(
                session_id,
                expected_revision=access.revision,
                target_access_mode=access.access_mode,
                target_share_id=_new_share_id(),
                # Rotating the link is not an audience edit: carry the whole
                # entry set over, not just its email slice.
                target_entries=access.entries,
                user_context=context,
            )
            if result.status == "share_id_taken":
                continue
            if result.status == "conflict":
                raise ShowPageError(
                    "ShowAccess changed concurrently.",
                    code="show_access_conflict",
                )
            if result.status not in {"applied", "no_change"}:
                raise ShowPageError(
                    "The share link could not be rotated.",
                    code=result.status,
                )
            updated = self.get(session_id)
            assert updated is not None
            return updated, previous_share_id
        raise ShowPageError(
            "Could not allocate a unique share ID.",
            code="share_id_allocation_failed",
        )

    def set_share_id(
        self,
        session_id: str,
        share_id: str,
        *,
        user_context: Any = None,
    ) -> tuple[ShowPage, str | None]:
        """Replace a shared page's stable suffix through the ShowAccess writer."""
        session_id = validate_session_id(session_id)
        context = _resolve_resource_access_context(user_context)
        new_share_id = validate_share_id(share_id)
        if self.is_archived(session_id):
            raise ShowPageError(
                "Cannot change the share link of an archived session.",
                code="session_archived",
            )
        if self.get(session_id) is None:
            self.ensure(session_id, user_context=context)
        access = self.require_access_settings(session_id, user_context=context)
        if access.access_mode not in {ACCESS_MODE_LIMITED, ACCESS_MODE_PUBLIC}:
            raise ShowPageError(
                "A custom link can only be set while the Show Page is shared.",
                code="not_shared",
            )
        previous_share_id = access.share_id
        result = self.apply_access(
            session_id,
            expected_revision=access.revision,
            target_access_mode=access.access_mode,
            target_share_id=new_share_id,
            # Renaming the link is not an audience edit: carry the whole entry
            # set over, not just its email slice.
            target_entries=access.entries,
            user_context=context,
        )
        if result.status == "share_id_taken":
            raise ShowPageError(
                "That custom link is already taken. Pick another.",
                code="share_id_taken",
            )
        if result.status == "conflict":
            raise ShowPageError(
                "ShowAccess changed concurrently.",
                code="show_access_conflict",
            )
        if result.status not in {"applied", "no_change"}:
            raise ShowPageError(
                "The custom link could not be applied.",
                code=result.status,
            )
        updated = self.get(session_id)
        assert updated is not None
        return updated, previous_share_id

    def _unique_share_id(self) -> str:
        for _ in range(20):
            candidate = _new_share_id()
            if self.get_by_share_id(candidate) is None:
                return candidate
        raise ShowPageError("Could not allocate a unique share ID.", code="share_id_allocation_failed")


def _page_from_row(row: Any) -> ShowPage:
    return ShowPage(
        session_id=str(row["session_id"]),
        access_mode=str(row["access_mode"]),
        access_revision=int(row["access_revision"]),
        share_id=str(row["share_id"]) if row["share_id"] else None,
        offline_at=str(row["offline_at"]) if row["offline_at"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _show_access_from_row(connection: Connection, row: Any) -> ShowAccess:
    page_id = str(row["session_id"])
    entries = tuple(
        ShowAccessEntry(
            kind=str(entry_row["kind"]),
            value=str(entry_row["value"]),
            organization_id=(
                str(entry_row["organization_id"])
                if entry_row["organization_id"]
                else None
            ),
        )
        for entry_row in connection.execute(
            select(
                show_page_access_entries.c.kind,
                show_page_access_entries.c.value,
                show_page_access_entries.c.organization_id,
            )
            .where(show_page_access_entries.c.page_id == page_id)
            .order_by(
                show_page_access_entries.c.kind.asc(),
                show_page_access_entries.c.value.asc(),
            )
        ).mappings()
    )
    return ShowAccess(
        page_id=page_id,
        access_mode=str(row["access_mode"]),
        share_id=str(row["share_id"]) if row["share_id"] else None,
        revision=int(row["access_revision"]),
        entries=entries,
    )


class _IconLinkFinder(HTMLParser):
    """Capture the first ``<link rel="icon">`` href — and the ``<base href>`` in
    effect before it — from an HTML document.

    Tolerant, stdlib-only (html.parser, no new deps): a ``<link>`` whose ``rel``
    token set includes ``icon`` matches (so ``icon`` and ``shortcut icon`` do,
    ``apple-touch-icon`` does not). ``base_href`` holds the first ``<base href>``
    seen BEFORE the icon link (a base applies only to later URLs); recording stops
    once the icon link is found. Pure string extraction — never fetches/resolves.
    """

    def __init__(self) -> None:
        super().__init__()
        self.icon_href: str | None = None
        self.base_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.icon_href is not None:
            return
        name = tag.lower()
        values = {attr.lower(): (value or "") for attr, value in attrs}
        if name == "base":
            if self.base_href is None:
                href = values.get("href", "").strip()
                if href:
                    self.base_href = href
        elif name == "link" and "icon" in values.get("rel", "").lower().split():
            href = values.get("href", "").strip()
            if href:
                self.icon_href = href


def _resolve_show_page_icon_href(href: str, base_href: str | None) -> str | None:
    """Resolve a page's icon href to a same-workspace relative path, or None.

    Resolves with DOCUMENT semantics — the browser percent-decodes the href and any
    ``<base href>``, treats backslashes as slashes, then joins them the way it
    resolves ``<img src>`` — so ``<base href="assets/">`` + ``favicon.svg`` becomes
    ``assets/favicon.svg`` and ``%2e%2e/x`` / ``..\\x`` are caught. Returns None
    unless the result lands inside the page's own workspace AND names a static,
    whitelisted image: absolute / root-relative (``/w/…``, ``/icon.svg``) / external /
    other-scheme / malformed hrefs, parent traversal, runtime API/event paths
    (``api/`` / ``__show/`` / ``__events``), the generic ``vite.svg`` scaffold mascot,
    and non-whitelisted extensions all yield None (the letter avatar is preferred).
    Pure — no I/O.
    """

    def _normalize(value: str) -> str:
        return unquote(value).replace("\\", "/")

    def _escapes_workspace(value: str) -> bool:
        # A ref resolves INSIDE /show/<sid>/ only if it is purely RELATIVE. The
        # browser roots a leading-"/" ref (``/w/icon.svg``, ``/icon.svg``) or a
        # ``//host`` ref at the ORIGIN — not the workspace — and a ``..`` segment
        # climbs out. These must reject up front: otherwise a literal ``/w/…`` would
        # collide with the synthetic prefix below and be mis-served as if relative.
        normalized = _normalize(value)
        if not normalized or normalized.startswith("/"):
            return True
        if urlsplit(normalized).scheme:  # http:, data:, javascript:, …
            return True
        return ".." in normalized.split("/")

    # Resolve href (and any <base>) with document semantics against a synthetic
    # same-origin workspace root, rejecting non-relative refs first. ANY malformed
    # URL is treated as "no icon", never raised: _extract_icon_path runs while
    # building /api/show-pages, so one bad page must fall back to the letter avatar
    # rather than break the whole Show Pages / Dock inventory request.
    try:
        if _escapes_workspace(href):
            return None
        if base_href is not None and _escapes_workspace(base_href):
            return None
        doc_base = "http://show.invalid/w/"
        base_url = urljoin(doc_base, _normalize(base_href)) if base_href else doc_base
        resolved = urlsplit(urljoin(base_url, _normalize(href)))
    except ValueError:
        return None  # malformed href/base → no icon (never break the inventory)
    if resolved.scheme != "http" or resolved.netloc != "show.invalid":
        return None  # external / protocol-relative / other scheme (defense in depth)
    prefix = "/w/"
    if not resolved.path.startswith(prefix):
        return None  # absolute or ../ traversal escaped the workspace
    relative = resolved.path[len(prefix) :]
    if not relative:
        return None
    segments = [segment for segment in relative.split("/") if segment]
    if segments[0].lower() in {"api", "__show", "__events"}:
        return None  # runtime API/event paths are not static icons
    if any(segment.startswith(".") for segment in segments):
        # Hidden / dot segments (.git/x.png, .env.svg, assets/.secret.png) are
        # denied for icons exactly as the Show Page static server denies them
        # (_is_show_page_dot_path); the icon endpoint must not become a bypass for
        # that policy. (Sensitive non-image files are already blocked by the image
        # extension whitelist below; this closes image-extension dot-files.)
        return None
    filename = segments[-1]
    if filename.lower() == "vite.svg":
        return None  # generic scaffold mascot → letter avatar
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in SHOW_PAGE_ICON_CONTENT_TYPES:
        return None
    return relative


def _read_fd_fully(fd: int, size: int) -> bytes:
    """Read exactly ``size`` bytes from ``fd`` (handling short reads)."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_workspace_file_safely(path: Path, limit: int, *, cap: bool = False) -> bytes | None:
    """Race/DoS-safe read of a REGULAR workspace file, or None — the ONE chokepoint
    for reading agent-authored workspace files (index.html, icons).

    Opens ``path`` with getattr-guarded ``O_NOFOLLOW`` (a symlink swapped in after an
    earlier check is NOT followed) and ``O_NONBLOCK`` (opening a FIFO/device returns
    immediately instead of BLOCKING on a writer — the fstat below then refuses it,
    so a swapped-in special file can never hang an ``/api/show-pages`` request; both
    flags degrade to a plain open where absent, e.g. native Windows). It re-checks on
    the DESCRIPTOR via ``fstat`` that the target is a REGULAR file, then bounded-reads.

    ``cap=False`` (default): read up to ``limit`` bytes — a HEAD scan of a
    possibly-large file (index.html, whose ``<head>`` is at the top). ``cap=True``:
    a file LARGER than ``limit`` is refused (None) — for a file that must be read in
    FULL within a cap (an icon: hash + serve). Returns None for missing / symlink /
    non-regular / (capped) oversized / unreadable; never raises, never blocks.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            return None  # symlink target that isn't regular, FIFO/device swapped in
        if cap and info.st_size > limit:
            return None  # oversized (e.g. a screenshot advertised as an icon)
        return _read_fd_fully(fd, info.st_size if cap else min(info.st_size, limit))
    except OSError:
        return None
    finally:
        os.close(fd)


def _extract_icon_path(page_dir: Path) -> str | None:
    """The page's own favicon as a same-workspace relative path, or None.

    Reads ONLY the head (bounded) of a REGULAR ``<page_dir>/index.html`` — never a
    symlink/special file, never any other file, never the icon itself — extracts the
    first ``<link rel="icon">`` href (with any ``<base href>``), and resolves +
    validates it via :func:`_resolve_show_page_icon_href`. The returned path is served
    ONLY through ``GET /api/show-pages/<sid>/icon``; callers use its presence as the
    has-icon signal, never to compose a URL.
    """
    # Head scan of a REGULAR index.html through the safe-read chokepoint: an oversized
    # inline page — or a symlink / special file an agent dropped in (or swapped in
    # mid-check) — must not stall /api/show-pages or allocate a huge string.
    raw = _read_workspace_file_safely(page_dir / "index.html", _ICON_INDEX_HEAD_LIMIT)
    if raw is None:
        return None
    html = raw.decode("utf-8", errors="replace")
    finder = _IconLinkFinder()
    try:
        finder.feed(html)
    except Exception:
        # Malformed markup: prefer the letter avatar over guessing.
        return None
    href = (finder.icon_href or "").strip()
    if not href:
        return None
    return _resolve_show_page_icon_href(href, finder.base_href)


# Workspace-conventional favicon locations, tried IN ORDER when the page declares no
# usable <link rel="icon"> (§7.1h): most agent-built pages ship no link tag at all, so
# these give them an icon from the common spots — root first, then Vite's public/. The
# extension order (svg vector first, down to raster) covers EVERY uploadable/servable
# favicon type so an icon uploaded via POST .../icon (§7.1j, which writes favicon.<ext>)
# is resolved here regardless of its type.
_CONVENTIONAL_ICON_EXTS: tuple[str, ...] = ("svg", "ico", "png", "webp", "jpg", "jpeg")
_CONVENTIONAL_ICON_RELATIVES: tuple[str, ...] = tuple(
    f"{prefix}favicon.{ext}" for prefix in ("", "public/") for ext in _CONVENTIONAL_ICON_EXTS
)


def _resolve_icon_candidate(page_dir: Path, relative: str) -> tuple[Path, str] | None:
    """Resolve ONE already-policy-safe relative icon path to (abs path, Content-Type),
    or None when it isn't a servable regular file inside the workspace within the size
    cap. Shared by the explicit ``<link>`` href and the conventional-file fallback."""
    try:
        candidate = (page_dir / relative).resolve()
        root = page_dir.resolve()
        # Realpath must stay inside the workspace (defends against an in-workspace
        # symlink pointing out) and be a regular file of a whitelisted image type.
        if candidate != root and root not in candidate.parents:
            return None
        if not candidate.is_file():
            return None
        if candidate.stat().st_size > _ICON_MAX_BYTES:
            # An oversized "icon" (e.g. a screenshot) → letter avatar; never hashed
            # or served in full, bounding /api/show-pages + endpoint memory.
            return None
    except (ValueError, OSError):
        # A page-authored href can resolve to a filesystem-invalid path (embedded
        # NUL, an overlong filename): that is "no icon" (letter avatar), never an
        # error to surface — this helper must return None rather than raise.
        return None
    content_type = SHOW_PAGE_ICON_CONTENT_TYPES.get(candidate.suffix.lower().lstrip("."))
    if content_type is None:
        return None
    return candidate, content_type


def _public_mapped_icon_relative(relative: str) -> str | None:
    """The Vite publicDir root-mapping of a declared icon href, or None.

    Vite serves ``public/*`` at the SITE ROOT, so a browser-valid relative href like
    ``./icon.png`` (which the page resolves against the site root) can live on disk at
    ``public/icon.png`` — while our resolution is against the workspace root, so the exact
    on-disk path ``<ws>/icon.png`` misses. This returns the ``public/``-prefixed relative
    to try AFTER the exact path. None when the path is already under ``public/`` (the
    exact-path probe covers it — no ``public/public/…``). The result is a plain relative
    that still flows through :func:`_resolve_icon_candidate`, so the realpath / whitelist /
    size guards apply to the mapped path too (a hostile ``..`` href is already rejected by
    :func:`_extract_icon_path`, and the realpath check re-guards the mapped candidate)."""
    if relative.startswith("public/"):
        return None
    return f"public/{relative}"


def resolve_show_page_icon(session_id: str) -> tuple[Path, str] | None:
    """The absolute path + Content-Type of a Show Page's own icon, or None.

    The single serving chokepoint for ``GET /api/show-pages/<sid>/icon``: combines
    :func:`_extract_icon_path` (document-semantics resolution + policy) with the
    same-workspace realpath guard, regular-file check, and extension whitelist, so
    the serving layer just streams the file. An explicit ``<link rel="icon">`` WINS;
    when the page declares none, it falls back to the workspace-conventional favicon
    files (root then ``public/``) so agent-built pages — which rarely add a link —
    still get an icon (§7.1h). A declared href also honors Vite's **publicDir
    root-mapping** (§7.1j): an exact ``<ws>/<path>`` miss falls back to
    ``<ws>/public/<path>`` (Vite serves ``public/*`` at the site root) before the
    conventions. Returns None for any missing workspace / no icon / policy rejection —
    the caller answers 404 and the frontend uses the letter avatar.
    """
    page_dir = show_page_dir(session_id)
    link = _extract_icon_path(page_dir)
    # A USABLE link wins (it's first, so it's returned before any convention); but an
    # explicit link that resolves to nothing (missing file / rejected) must not strand
    # the page icon-less when a conventional favicon exists — fall through to the
    # conventions after it, so coverage is maximized (Codex §7.1h).
    #
    # For the declared link, probe the EXACT on-disk path first, then the Vite publicDir
    # mapping (``public/<path>``): a fully-compliant ``./icon.png`` can live at
    # ``public/icon.png`` because Vite serves ``public/*`` at the site root while we
    # resolve against the workspace root on disk, so the exact path misses even though the
    # browser loads it fine (owner-reported §7.1j). The mapped candidate still goes through
    # _resolve_icon_candidate, so whitelist/size/traversal-realpath guards apply to it too.
    if link:
        mapped = _public_mapped_icon_relative(link)
        link_candidates = (link, mapped) if mapped else (link,)
        relatives = (*link_candidates, *_CONVENTIONAL_ICON_RELATIVES)
    else:
        relatives = _CONVENTIONAL_ICON_RELATIVES
    for relative in relatives:
        resolved = _resolve_icon_candidate(page_dir, relative)
        if resolved is not None:
            return resolved
    return None


def show_page_icon_version(session_id: str) -> str | None:
    """An opaque cache token for a page's servable icon, or None when it has none.

    The token is a short digest of the resolved icon file's CONTENT, so any byte
    change — overwriting the favicon, repointing ``<link rel="icon">``, or a
    same-size/same-mtime regeneration — changes the token, and therefore the ``?v=``
    on the icon URL, with NO update-site enumeration anywhere in the client (the
    freshness rides the normal payload refresh). Identical bytes yield an identical
    token, so an unchanged icon stays a cache hit. Carried in the payload as the
    has-icon signal; the frontend appends it verbatim as ``?v=<token>``. The icon
    endpoint's ``?v=`` NEVER selects the file — resolution is derived only from the
    session id + workspace — it is validated as a content assertion at read time
    (see :func:`read_show_page_icon`).
    """
    resolved = resolve_show_page_icon(session_id)
    if resolved is None:
        return None
    candidate, _content_type = resolved
    # Read through the safe-read chokepoint (cap=True): bounded to the icon cap and
    # race-safe (a swap to a symlink / huge file / FIFO after resolve is refused, not
    # followed / buffered / blocked on) — the token path must be as hardened as the
    # serving path since it runs for EVERY /api/show-pages + `vibe show list` row.
    data = _read_workspace_file_safely(candidate, _ICON_MAX_BYTES, cap=True)
    if data is None:
        return None
    # Hash the icon's CONTENT so the token changes for ANY byte change — including a
    # regeneration that preserves path, size, AND mtime (`cp -p`/`rsync`-style copies,
    # deterministic build artifacts), which an mtime+size identity would miss under
    # `immutable` caching. Content-addressed: identical bytes → identical token (the
    # icon is unchanged, so the ?v= URL correctly stays a cache hit); different bytes
    # → different token → new URL → refetch. Icons are small, so hashing is cheap.
    return _icon_content_token(data)


def _icon_content_token(data: bytes) -> str:
    """The opaque icon cache token for a byte string — the one algorithm shared by
    the payload (:func:`show_page_icon_version`) and the read-time enforcement
    (:func:`read_show_page_icon`), so they can never drift apart."""
    return hashlib.sha256(data).hexdigest()[:16]


def read_show_page_icon(session_id: str, expected_version: str) -> tuple[bytes, str] | None:
    """Icon bytes + Content-Type for serving, or None — a race-safe, token-enforced
    read for ``GET /api/show-pages/<sid>/icon?v=<token>``.

    Resolution is still sid-only (:func:`resolve_show_page_icon`); ``?v=`` NEVER
    selects the file, it is validated as a CONTENT ASSERTION here so the stable URL's
    ``immutable`` cache is honest (a given URL maps to exactly one byte-content). The
    read closes the resolve→read TOCTOU: the resolved candidate is opened
    ``O_NOFOLLOW`` (a symlink swapped in after resolve fails), re-checked on the
    DESCRIPTOR via ``fstat`` (regular file, still within the size cap — a huge file
    swapped in is rejected), and bounded-read. Returns None (→ 404 no-store) for a
    missing/oversized/non-regular target, a swap, or a token mismatch (the content
    changed since the payload advertised it, or no token was supplied). No exception
    escapes.
    """
    resolved = resolve_show_page_icon(session_id)
    if resolved is None:
        return None
    candidate, content_type = resolved
    # Race-safe read through the shared chokepoint (cap=True): O_NOFOLLOW open +
    # fstat regular-file/size-cap re-check on the descriptor + bounded read, so a
    # symlink / huge file / FIFO swapped in after resolve is refused, not served.
    data = _read_workspace_file_safely(candidate, _ICON_MAX_BYTES, cap=True)
    if data is None:
        return None
    if not expected_version or _icon_content_token(data) != expected_version:
        return None  # ?v= is a content assertion: mismatch/absent → 404 (no poison)
    return data, content_type


# --- Icon self-serve upload (§7.1j) -------------------------------------------------
# A user can upload an image from the App Library as a page's icon. The upload writes
# the workspace-root CONVENTIONAL favicon (favicon.<ext>) that resolve_show_page_icon
# already picks up (§7.1h) — so the SERVER chooses the on-disk name and the client
# never supplies a path. It is the inverse of the read side and shares the same cap +
# whitelist; index.html is never edited, so an explicit usable <link rel=icon> still
# WINS (§7.1f resolution order) — the editor just covers the common no-link case.
#
# Public so the ui_server upload route can size the multipart parser / Content-Length
# guard from the same number write_show_page_icon re-checks the actual bytes against.
SHOW_PAGE_ICON_MAX_UPLOAD_BYTES = _ICON_MAX_BYTES  # 2 MiB
# Accepted upload content-types -> the single canonical on-disk extension. The owner's
# whitelist (§7.1j) is svg/png/ico/jpg/jpeg/webp — a subset of the servable set (no gif).
_ICON_UPLOAD_CONTENT_TYPE_EXT = {
    "image/svg+xml": "svg",
    "image/png": "png",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
# Accepted filename extensions -> canonical on-disk extension (jpeg folds to jpg).
_ICON_UPLOAD_NAME_EXT = {
    "svg": "svg",
    "png": "png",
    "ico": "ico",
    "jpg": "jpg",
    "jpeg": "jpg",
    "webp": "webp",
}


def _canonical_upload_icon_ext(filename: str | None, content_type: str | None) -> str | None:
    """The one on-disk extension for an uploaded icon, or None if it isn't whitelisted.

    Derived from the content-type first, then the filename suffix (jpeg→jpg). A
    recognizable, whitelisted signal that DISAGREES with the other is refused (a PNG
    announced as ``image/svg+xml`` is suspicious), so the server writes exactly the type
    it validated. Falling back to the filename is limited to a BLANK or generic
    (``application/octet-stream``) content-type: an EXPLICIT, non-generic type that isn't
    a whitelisted image is a rejection signal (a ``text/html`` body named ``logo.svg`` is
    not an icon), not something to accept on the extension alone. None → the route answers
    415 rather than guessing an extension."""
    raw_type = (content_type or "").split(";", 1)[0].strip().lower()
    type_ext = _ICON_UPLOAD_CONTENT_TYPE_EXT.get(raw_type)
    if type_ext is None and raw_type and raw_type != "application/octet-stream":
        return None  # an explicit, non-image content-type → reject, don't trust the name
    name_ext = _ICON_UPLOAD_NAME_EXT.get(Path(filename or "").suffix.lower().lstrip("."))
    if type_ext and name_ext and type_ext != name_ext:
        return None
    return type_ext or name_ext


def _unlink_quietly(path: Path) -> None:
    """Remove a directory ENTRY (a symlink is unlinked itself, never followed), ignoring
    a missing / again-racing target. Used to clear old favicon.* before an atomic write."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_root_favicon_atomically(page_dir: Path, ext: str, data: bytes) -> None:
    """Write ``<page_dir>/favicon.<ext>`` as the SOLE conventional root icon, safely, and
    WITHOUT destroying the existing icon unless the replacement actually lands.

    The new bytes go to a fresh ``O_EXCL | O_NOFOLLOW`` temp and are atomically
    ``os.replace``d onto ``favicon.<ext>`` FIRST — so a failure while opening/writing the
    temp or replacing (e.g. disk full mid-upload) raises with every existing favicon still
    in place (no data loss — §7.1j review P2). ONLY AFTER the new file lands are the
    OTHER-extension root ``favicon.*`` removed, leaving exactly one conventional source
    (e.g. a prior ``favicon.svg`` after uploading ``favicon.png``, which would otherwise
    shadow it in the resolver's svg>ico>png>… order). ``os.replace`` swaps the final NAME
    without following a symlink raced back in, and no reader sees a half-written icon.
    Threat model: the racing party is the user's own agent with local FS access, so the
    swap guards are defense-in-depth, not a boundary — but a symlinked/partial favicon
    must never become servable."""
    target = page_dir / f"favicon.{ext}"
    tmp = page_dir / f".favicon.{ext}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # Atomic: overwrites any existing favicon.<ext> in place (a symlink at the name is
        # replaced, not followed). Old favicons of every extension are still intact here.
        os.replace(tmp, target)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    # The replacement has landed — now drop the OTHER-extension root favicons so exactly
    # one conventional source remains. Done last so a failed write above never orphans the
    # page icon-less. EXCEPT a root favicon the page explicitly links from index.html:
    # deleting it would 404 the page's own <link rel=icon> (which still WINS in the
    # resolver — we never edit index.html), so preserve that one (§7.1j review P2).
    linked = _extract_icon_path(page_dir)
    for servable_ext in SHOW_PAGE_ICON_CONTENT_TYPES:
        if servable_ext == ext:
            continue
        sibling_rel = f"favicon.{servable_ext}"
        if sibling_rel == linked:
            continue
        _unlink_quietly(page_dir / sibling_rel)


def write_show_page_icon(
    session_id: str, data: bytes, *, filename: str | None, content_type: str | None
) -> str | None:
    """Write an uploaded image as the page's workspace-root conventional favicon, and
    return the FRESH ``icon_version`` (§7.1j icon self-serve).

    The SERVER derives the on-disk name (``favicon.<ext>``) from the whitelisted
    content-type/extension — the client NEVER supplies a path — enforces the 2 MiB cap,
    and writes atomically without following a symlink (see
    :func:`_write_root_favicon_atomically`). ``index.html`` is not touched, so a usable
    explicit ``<link rel=icon>`` still WINS in :func:`resolve_show_page_icon` and the
    returned version reflects whatever now resolves. Raises :class:`ShowPageError`
    (mapped to a 4xx by the route) for a malformed id / non-whitelisted type / empty or
    oversized payload; never a 500."""
    validate_session_id(session_id)
    ext = _canonical_upload_icon_ext(filename, content_type)
    if ext is None:
        raise ShowPageError(
            "The icon must be an SVG, PNG, ICO, JPEG, or WebP image.", code="invalid_icon_type"
        )
    if not data:
        raise ShowPageError("The icon file is empty.", code="icon_required")
    if len(data) > SHOW_PAGE_ICON_MAX_UPLOAD_BYTES:
        raise ShowPageError("The icon file is too large (max 2 MiB).", code="icon_too_large")
    page_dir = show_page_dir(session_id)
    page_dir.mkdir(parents=True, exist_ok=True)
    _write_root_favicon_atomically(page_dir, ext, data)
    return show_page_icon_version(session_id)


def show_page_payload(page: ShowPage, *, config: V2Config | None = None) -> dict[str, Any]:
    path = show_page_dir(page.session_id)
    private = private_url(page.session_id, config=config)
    public = public_url(page.share_id, config=config)
    url_guidance = avibe_cloud_connect_guidance(config)
    active_url = None
    if page.visibility == VISIBILITY_PRIVATE:
        active_url = private
    elif page.visibility == VISIBILITY_PUBLIC:
        active_url = public
    return {
        "session_id": page.session_id,
        "visibility": page.visibility,
        "access_mode": page.access_mode,
        "access_revision": page.access_revision,
        "path": str(path),
        # Opaque cache token (not a path): non-null iff a servable icon exists, and
        # it changes when the icon file changes so the frontend's ?v=<token> busts
        # the cache with no update-site enumeration (§7.1f versioned-URL).
        "icon_version": show_page_icon_version(page.session_id),
        "active_url": active_url,
        "private_url": private,
        "public_url": public,
        "url_available": url_guidance is None,
        "url_guidance": url_guidance,
        "share_id": page.share_id,
        "offline": page.offline,
        "offline_at": page.offline_at,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
    }


def _write_default_runtime_files(page_dir: Path, session_id: str) -> None:
    app_path = page_dir / "src" / "App.tsx"
    fresh_workspace = not app_path.exists()
    # Runtime-owned app shell + always-present workspace files. Each is written
    # once and skipped if it already exists, so an existing workspace keeps its
    # own copies (see the skip-if-exists loop below).
    files: dict[str, str] = {
        "index.html": _default_index_html(session_id),
        "src/main.tsx": _default_main_tsx(include_legacy_hash_redirect=fresh_workspace),
        "src/styles.css": _default_styles_css(),
        "api/health.ts": _default_api_health_ts(),
    }
    # Fresh-workspace starter. Only seed it into a FRESH workspace — one that does
    # not yet have its own ``src/App.tsx``. This keeps existing single-page
    # workspaces byte-for-byte unchanged (we never drop router/pages files next to
    # a page the agent already authored). A brand-new Show Page starts as a clean
    # "being generated" placeholder for the user, plus a minimal file-based router
    # and one extra page so the agent can see the multi-page affordance — it is a
    # starting point to extend or replace, not a required structure. Adding a page
    # later is just a new file under ``src/pages/`` — the router discovers it and
    # the runtime-owned shell (``index.html`` / ``src/main.tsx``) is never touched.
    if fresh_workspace:
        files.update(
            {
                "src/router.tsx": _default_router_tsx(),
                "src/pages/index.tsx": _default_page_home_tsx(),
                "src/pages/second.tsx": _default_page_second_tsx(),
                # App imports the router, so publish it after every generated
                # dependency. A partial write remains recognizable as fresh.
                "src/App.tsx": _default_app_tsx(),
            }
        )
    for relative_path, contents in files.items():
        target = page_dir / relative_path
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


def _default_index_html(session_id: str) -> str:
    escaped = _escape_html(session_id)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <base href="%BASE_URL%">
    <title>Show Page {escaped}</title>
    <!-- App icon (Avibe Dock / App Library): to give this app an icon, place a
         favicon FILE in this workspace — either `favicon.svg` (or .png/.ico) at the
         workspace root, or a `<link rel="icon" href="./your-icon.svg">` in this
         <head> pointing at a RELATIVE file here. Avibe reads that static FILE to
         render the app tile. Do NOT inject the icon from JavaScript at runtime
         (e.g. appending a <link> in a script): the icon is resolved from the file
         on disk, so a script-created one is never picked up. You can also upload one
         from the App Library (the AI page's expanded panel), which writes the
         workspace-root favicon for you. -->
    <!-- PWA: let a user "Add to Home Screen" this Show Page as a standalone app.
         We declare it standalone-capable but DELIBERATELY ship no apple-touch-icon
         or apple-mobile-web-app-title here. A page customizes its installed icon
         and name by editing this file (a custom <title> / apple-mobile-web-app-title
         and a relative apple-touch-icon.png), and a static default would compete
         with that: iOS picks the FIRST apple-touch-icon in source order, so a
         default link could shadow the page's own icon when a page appends rather
         than replaces. With none declared, a customized page's icon is the only
         one (it wins), and an un-customized page falls back to the Avibe origin's
         /apple-touch-icon.png (served auth-free; see _PWA_PUBLIC_ASSETS in
         ui_server) via iOS's root-directory icon lookup. We also do NOT link
         /manifest.webmanifest: the workbench manifest's start_url "/" would hijack
         the installed icon back to the workbench. -->
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="./src/main.tsx"></script>
  </body>
</html>
"""


def show_page_runtime_recovery_html(
    session_id: str,
    *,
    reason: str | None = None,
    language: str = "en",
) -> str:
    session_id = validate_session_id(session_id)
    escaped = _escape_html(session_id)
    escaped_reason = _escape_html(reason or "runtime_unavailable")
    loading_delay = "0s" if reason else f"{SHOW_PAGE_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS}s"
    translate = lambda key: _escape_html(t(f"show.runtimeRecovery.{key}", language))
    message_key = _show_runtime_recovery_message_key(reason)
    return f"""<!doctype html>
<html lang="{_escape_html(language)}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{translate("title")}</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f6f7f9;
        color: #172033;
      }}
      body {{
        margin: 0;
        min-height: 100vh;
      }}
      .show-recovery-shell {{
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 32px 18px;
        box-sizing: border-box;
      }}
      .show-recovery-loading {{
        position: fixed;
        inset: 0;
        display: grid;
        place-items: center;
        gap: 14px;
        background: #f6f7f9;
        font-size: 15px;
        font-weight: 760;
        color: #526078;
        animation: show-recovery-loading-out 0.18s ease {loading_delay} forwards;
      }}
      .show-recovery-loading::before {{
        content: "";
        width: 28px;
        height: 28px;
        border-radius: 999px;
        border: 3px solid rgba(23, 32, 51, 0.16);
        border-top-color: #0f172a;
        animation: show-recovery-spin 0.8s linear infinite;
      }}
      .show-recovery-panel {{
        width: min(860px, 100%);
        border: 1px solid rgba(23, 32, 51, 0.12);
        border-radius: 18px;
        background: rgba(255, 255, 255, 0.94);
        padding: clamp(24px, 5vw, 44px);
        box-shadow: 0 24px 80px rgba(23, 32, 51, 0.10);
        box-sizing: border-box;
        opacity: 0;
        visibility: hidden;
        transform: translateY(6px);
        animation: show-recovery-panel-in 0.22s ease {loading_delay} forwards;
      }}
      .show-recovery-panel p {{
        max-width: 720px;
        line-height: 1.65;
        margin: 12px 0 0;
        color: #526078;
      }}
      .show-recovery-eyebrow {{
        color: #526078;
        font-size: 13px;
        font-weight: 760;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      .show-recovery-panel h1 {{
        margin: 12px 0 0;
        font-size: clamp(32px, 7vw, 56px);
        line-height: 1;
        letter-spacing: 0;
      }}
      .show-recovery-grid {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(280px, 0.8fr);
        gap: 18px;
        margin-top: 24px;
      }}
      .show-recovery-card {{
        border: 1px solid rgba(23, 32, 51, 0.10);
        border-radius: 14px;
        background: #fff;
        padding: 16px;
      }}
      .show-recovery-card h2 {{
        margin: 0 0 10px;
        font-size: 15px;
      }}
      .show-recovery-card ul {{
        margin: 0;
        padding-left: 18px;
        color: #526078;
        line-height: 1.7;
      }}
      .show-recovery-card textarea {{
        width: 100%;
        min-height: 178px;
        resize: vertical;
        border: 1px solid rgba(23, 32, 51, 0.14);
        border-radius: 12px;
        padding: 12px;
        box-sizing: border-box;
        font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
        color: #172033;
        background: #f8fafc;
      }}
      .show-recovery-actions {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 10px;
      }}
      .show-recovery-button {{
        height: 36px;
        border: 0;
        border-radius: 10px;
        padding: 0 14px;
        background: #0f172a;
        color: #fff;
        font: 700 14px/1 Inter, ui-sans-serif, system-ui;
        cursor: pointer;
      }}
      .show-recovery-button.secondary {{
        border: 1px solid rgba(23, 32, 51, 0.12);
        background: #fff;
        color: #172033;
      }}
      .show-recovery-panel code {{
        background: rgba(82, 96, 120, 0.12);
        border-radius: 6px;
        padding: 2px 6px;
      }}
      @keyframes show-recovery-spin {{
        to {{ transform: rotate(360deg); }}
      }}
      @keyframes show-recovery-loading-out {{
        to {{ opacity: 0; visibility: hidden; }}
      }}
      @keyframes show-recovery-panel-in {{
        to {{ opacity: 1; visibility: visible; transform: translateY(0); }}
      }}
      @media (max-width: 760px) {{
        .show-recovery-grid {{
          grid-template-columns: 1fr;
        }}
      }}
      @media (prefers-reduced-motion: reduce) {{
        .show-recovery-loading,
        .show-recovery-loading::before,
        .show-recovery-panel {{
          animation-duration: 0.01ms;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="show-recovery-shell">
      <div class="show-recovery-loading">{translate("loading")}</div>
      <section class="show-recovery-panel">
        <div class="show-recovery-eyebrow">{translate("eyebrow")}</div>
        <h1>{translate("heading")}</h1>
        <p>{translate(message_key)}</p>
        <div class="show-recovery-grid">
          <div class="show-recovery-card">
            <h2>{translate("repairHeading")}</h2>
            <p>{translate("repairMessage")}</p>
            <p><code>vibe doctor repair show-runtime</code></p>
            <div class="show-recovery-actions">
              <button class="show-recovery-button" type="button" onclick="window.location.reload()">{translate("retryNow")}</button>
            </div>
          </div>
          <div class="show-recovery-card">
            <h2>{translate("statusHeading")}</h2>
            <p>{translate("retrying")}</p>
            <p>{translate("reasonLabel")}: <code>{escaped_reason}</code></p>
          </div>
        </div>
        <p>{translate("sessionLabel")}: <code>{escaped}</code></p>
      </section>
    </main>
    <script>
      let retryDelayMs = 3000;
      const checkRuntime = async () => {{
        await new Promise((resolve) => window.setTimeout(resolve, retryDelayMs));
        try {{
          const response = await fetch(window.location.href, {{
            cache: "no-store",
            credentials: "same-origin",
            headers: {{ "X-Avibe-Show-Recovery-Poll": "1" }},
          }});
          if (!response.headers.get("X-Avibe-Show-Recovery")) {{
            window.location.reload();
            return;
          }}
        }} catch (_error) {{}}
        retryDelayMs = Math.min(30000, retryDelayMs * 2);
        void checkRuntime();
      }};
      void checkRuntime();
    </script>
  </body>
</html>
"""


def _show_runtime_recovery_message_key(reason: str | None) -> str:
    if classify_show_runtime_failure(reason) is ShowRuntimeFailureClass.CONFIGURED:
        return "offlineMessage"
    if reason in {"runtime_archive_download_failed", "runtime_manifest_download_failed"}:
        return "downloadMessage"
    if reason in {"runtime_node_missing", "runtime_node_unsupported"}:
        return "nodeMessage"
    if reason == "runtime_platform_unsupported":
        return "platformMessage"
    if reason in {
        "runtime_archive_checksum_mismatch",
        "runtime_archive_size_mismatch",
        "runtime_manifest_invalid",
    }:
        return "verificationMessage"
    if reason in {"runtime_source_unsupported", "runtime_archive_url_unsupported"}:
        return "configurationMessage"
    if reason == "runtime_start_failed":
        return "startMessage"
    if reason and (reason.startswith("runtime_install_") or reason in {"runtime_git_missing", "runtime_npm_missing"}):
        return "installMessage"
    return "unavailableMessage"


def _escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _default_main_tsx(*, include_legacy_hash_redirect: bool) -> str:
    legacy_hash_redirect = (
        """
function redirectLegacyHashRoute() {
  if (!window.location.hash.startsWith("#/")) return
  const configuredBase = globalThis.__AVIBE_SHOW__?.basePath || "/"
  const basePathname = new URL(configuredBase, window.location.origin).pathname
  const baseParts = basePathname.split("/").filter(Boolean)
  const base = baseParts.length ? "/" + baseParts.join("/") + "/" : "/"
  const legacy = new URL(window.location.hash.slice(1), window.location.origin)
  const target = new URL(window.location.href)
  target.pathname = base + legacy.pathname.replace(/^\\/+/, "")
  for (const [key, value] of legacy.searchParams) target.searchParams.set(key, value)
  target.hash = legacy.hash
  window.history.replaceState(window.history.state, "", target)
}

redirectLegacyHashRoute()
"""
        if include_legacy_hash_redirect
        else ""
    )
    return (
        """import React from "react"
import { createRoot } from "react-dom/client"
import "@avibe/show-ui/styles.css"
import "./styles.css"
import App from "./App"

type VibeShowRuntimeConfig = {
  sessionId?: string
  basePath?: string
  eventsPath?: string
  streamPath?: string
  writeToken?: string
  annotation?: {
    authenticated: boolean
    mePath: string
  }
}

declare global {
  var __AVIBE_SHOW__: VibeShowRuntimeConfig | undefined
}

function readCookie(name: string): string | undefined {
  const prefix = `${name}=`
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : undefined
}

const injected: VibeShowRuntimeConfig = globalThis.__AVIBE_SHOW__ ?? {}

globalThis.__AVIBE_SHOW__ = {
  ...injected,
  sessionId: injected.sessionId ?? (window.location.pathname.match(/\\/show\\/([^/]+)/)?.[1]
    ? decodeURIComponent(window.location.pathname.match(/\\/show\\/([^/]+)/)![1])
    : undefined),
  basePath: injected.basePath ?? (window.location.pathname.match(/^(.*\\/(?:show|p)\\/[^/]+\\/)$/)?.[1] || window.location.pathname.replace(/[^/]*$/, "")),
  eventsPath: injected.eventsPath ?? "__show/events",
  streamPath: injected.streamPath ?? "__show/events?stream=1",
  writeToken: injected.writeToken ?? readCookie("vibe_show_event_token")
}
"""
        + legacy_hash_redirect
        + """
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
"""
    )


def _default_app_tsx() -> str:
    # Default app shell for a fresh Show Page (agent-editable). Standard shadcn
    # variables from styles.css are the theme authority, so no provider shadows
    # an agent's CSS overrides. This file, the pages under src/pages/, and
    # src/router.tsx are a starting point — restyle, add pages, or replace them.
    return """import { RouterView } from "./router"

export default function App() {
  return (
    <main className="mx-auto min-h-screen max-w-3xl bg-background px-4 py-8 text-foreground">
      <RouterView />
    </main>
  )
}
"""


def _default_router_tsx() -> str:
    # A tiny, dependency-free History router with file-based page discovery.
    #
    # The injected basePath is the authority for both private /show/<id>/ and public
    # /p/<share>/ surfaces. Their servers provide the matching entry-document
    # fallback, so clean nested URLs remain refreshable and shareable.
    #
    # Why file-based discovery: adding a route is just adding a file under src/pages/.
    # A folder becomes a nested path segment and a [param] file becomes a dynamic
    # segment, so the scaffold is not locked into a flat page list. Nothing here or in
    # the app shell needs editing to add a page.
    return """import type { ComponentType, MouseEvent, ReactNode } from "react"
import { useSyncExternalStore } from "react"

export type PageProps = {
  // Values captured from [param] segments, e.g. { id: "42" } for /items/42.
  params: Record<string, string>
}

type PageModule = { default: ComponentType<PageProps> }

type Segment = { name: string; dynamic: boolean }
type Route = {
  path: string
  segments: Segment[]
  Component: ComponentType<PageProps>
  dynamic: boolean
}

const PAGES_PREFIX = "./pages/"
const PAGE_SUFFIX = ".tsx"

// Eagerly import every page module at build time. This is the discovery
// mechanism: a new file under src/pages/ automatically registers a route.
const modules = import.meta.glob<PageModule>("./pages/**/*.tsx", { eager: true })

// "./pages/items/[id].tsx" -> "/items/:id"; "./pages/index.tsx" -> "/".
// Returns null for framework files (any segment starting with "_"), which lets
// an agent colocate non-page helpers under src/pages/ without creating a route.
function filePathToRoute(file: string): string | null {
  const relative = file.slice(PAGES_PREFIX.length, file.length - PAGE_SUFFIX.length)
  const parts = relative.split("/")
  if (parts[parts.length - 1] === "index") parts.pop()
  if (parts.some((part) => part.startsWith("_"))) return null
  const path = parts
    .map((part) => (part.startsWith("[") && part.endsWith("]") ? ":" + part.slice(1, -1) : part))
    .join("/")
  return path ? "/" + path : "/"
}

function toSegments(path: string): Segment[] {
  if (path === "/") return []
  return path
    .slice(1)
    .split("/")
    .map((part) => (part.startsWith(":") ? { name: part.slice(1), dynamic: true } : { name: part, dynamic: false }))
}

// Per-segment specificity mask: "0" for a static segment, "1" for a dynamic one.
// Routes are sorted ascending by this mask (compared left to right), so among
// routes of the same length a static segment always beats a [param] at the same
// position — e.g. /items/new wins over /items/:id, and /users/:id/edit wins over
// /users/:id/:action.
function routeSpecificity(segments: Segment[]): string {
  return segments.map((segment) => (segment.dynamic ? "1" : "0")).join("")
}

// A page's default export is renderable if it is a function component or a React
// "exotic" component (memo/forwardRef/lazy/…) — an object carrying $$typeof.
// Rejecting by `typeof === "function"` alone would drop memo()/forwardRef() pages.
function isRenderablePage(value: unknown): value is ComponentType<PageProps> {
  return (
    typeof value === "function" ||
    (typeof value === "object" && value !== null && "$$typeof" in value)
  )
}

export const routes: Route[] = Object.entries(modules)
  .map(([file, mod]): Route | null => {
    const path = filePathToRoute(file)
    if (!path || !isRenderablePage(mod.default)) return null
    const segments = toSegments(path)
    return { path, segments, Component: mod.default, dynamic: segments.some((s) => s.dynamic) }
  })
  .filter((route): route is Route => route !== null)
  .sort((a, b) => {
    const specA = routeSpecificity(a.segments)
    const specB = routeSpecificity(b.segments)
    if (specA !== specB) return specA < specB ? -1 : 1
    return a.path.localeCompare(b.path)
  })

// decodeURIComponent throws on a malformed escape (e.g. a link built with a raw
// "%", like /items/50%); fall back to the raw segment so a bad param degrades to
// that page instead of throwing during render and blanking the whole app.
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function matchRoute(path: string): { route: Route | null; params: Record<string, string> } {
  const parts = path === "/" ? [] : path.slice(1).split("/")
  for (const route of routes) {
    if (route.segments.length !== parts.length) continue
    const params: Record<string, string> = {}
    let matched = true
    for (let i = 0; i < parts.length; i++) {
      const segment = route.segments[i]
      if (segment.dynamic) params[segment.name] = safeDecode(parts[i])
      else if (segment.name !== safeDecode(parts[i])) {
        matched = false
        break
      }
    }
    if (matched) return { route, params }
  }
  return { route: null, params: {} }
}

function basePath(): string {
  const configured = globalThis.__AVIBE_SHOW__?.basePath
  const fallback = window.location.pathname.match(/^\\/(?:show|p)\\/[^/]+\\//)?.[0] || "/"
  const pathname = new URL(configured || fallback, window.location.origin).pathname
  const parts = pathname.split("/").filter(Boolean)
  return parts.length ? "/" + parts.join("/") + "/" : "/"
}

function normalizeRoutePath(path: string): string {
  const withLeadingSlash = path.startsWith("/") ? path : "/" + path
  return withLeadingSlash.length > 1 && withLeadingSlash.endsWith("/")
    ? withLeadingSlash.slice(0, -1)
    : withLeadingSlash
}

function readRoutePath(): string {
  const base = basePath()
  const pathname = window.location.pathname
  if (!pathname.startsWith(base)) return "/"
  const routePath = normalizeRoutePath("/" + pathname.slice(base.length))
  return routePath === "/index.html" ? "/" : routePath
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange)
  return () => window.removeEventListener("popstate", onChange)
}

export function useRoutePath(): string {
  return useSyncExternalStore(subscribe, readRoutePath, () => "/")
}

function routeUrl(to: string): URL {
  const normalizedTo = to.startsWith("/") ? to : "/" + to
  const route = new URL(normalizedTo, window.location.origin)
  const current = new URL(window.location.href)
  const target = new URL(basePath(), window.location.origin)
  const embed = current.searchParams.get("vibe-embed")
  target.pathname = basePath() + route.pathname.replace(/^\\/+/, "")
  target.search = route.search
  if (embed && !target.searchParams.has("vibe-embed")) target.searchParams.set("vibe-embed", embed)
  target.hash = route.hash
  return target
}

export function navigate(to: string): void {
  const target = routeUrl(to)
  window.history.pushState({}, "", target)
  window.dispatchEvent(new PopStateEvent("popstate"))
}

export function Link({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    navigate(to)
  }
  return <a href={routeUrl(to).toString()} className={className} onClick={onClick}>{children}</a>
}

export function RouterView() {
  const path = useRoutePath()
  const { route, params } = matchRoute(path)
  if (!route) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 text-card-foreground">
        <h1 className="text-lg font-semibold">Page not found</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          No route matches <code className="rounded bg-muted px-1.5 py-0.5">{path}</code>.
        </p>
        <p className="mt-4 text-sm">
          <Link className="font-medium underline underline-offset-4" to="/">Back to Home</Link>
        </p>
      </div>
    )
  }
  const Page = route.Component
  return <Page params={params} />
}
"""


def _default_page_home_tsx() -> str:
    # Default landing page: a live "building" placeholder the user sees right after
    # clicking "Visualize". A pulsing dot signals work in progress; after a delay
    # (only if it is genuinely taking a while) it reveals a copy-able nudge prompt.
    # It also hints the built-in UI — it renders Card + Button and leaves Badge as a
    # commented import. The agent replaces this file with the real content.
    return """import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
// More built-in UI is available too, e.g.:
// import { Badge } from "@/components/ui/badge"

const NUDGE_PROMPT = "Please visualize this session as a Show Page."
// Only offer the nudge if it is genuinely taking a while — don't nag on arrival.
const NUDGE_AFTER_MS = 90_000

export default function Home() {
  const [showNudge, setShowNudge] = useState(false)
  const [copied, setCopied] = useState(false)
  const codeRef = useRef<HTMLElement>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setShowNudge(true), NUDGE_AFTER_MS)
    return () => window.clearTimeout(timer)
  }, [])

  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(NUDGE_PROMPT)
    } catch {
      // Clipboard may be unavailable (e.g. insecure context) — select the text
      // so the viewer can copy it manually.
      const node = codeRef.current
      if (node) {
        const range = document.createRange()
        range.selectNodeContents(node)
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.addRange(range)
      }
      return
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="flex min-h-[70vh] items-center justify-center">
      <Card className="w-full max-w-md">
        <CardContent className="flex flex-col items-center gap-4 px-8 py-12 text-center">
          <span className="relative flex h-3.5 w-3.5" aria-hidden="true">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
            <span className="relative inline-flex h-3.5 w-3.5 rounded-full bg-emerald-500" />
          </span>
          <div className="space-y-1.5">
            <h1 className="text-lg font-semibold tracking-tight">Building your Show Page</h1>
            <p className="text-sm text-muted-foreground">
              Your agent is turning this session into a visual page. It will appear here automatically once it is ready.
            </p>
          </div>
          {showNudge && (
            <div className="w-full space-y-2 border-t border-border pt-4 text-left duration-500 animate-in fade-in slide-in-from-bottom-1">
              <p className="text-xs text-muted-foreground">Taking a while? Send this to your agent:</p>
              <div className="flex items-center gap-2">
                <code ref={codeRef} className="flex-1 truncate rounded bg-muted px-2 py-1.5 font-mono text-xs">
                  {NUDGE_PROMPT}
                </code>
                <Button size="sm" variant="secondary" onClick={copyPrompt}>
                  {copied ? "Copied" : "Copy"}
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
"""


def _default_page_second_tsx() -> str:
    # A second page, only to show that adding a file under src/pages/ adds a route
    # (a folder becomes a nested path; a [param] file a dynamic one) and to model a
    # tidy page using the built-in UI. Delete or replace it.
    return """import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Link } from "../router"

const code = "rounded bg-muted px-1.5 py-0.5 font-mono text-xs"

export default function SecondPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-6 py-10">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">A second page</h1>
        <p className="text-muted-foreground">
          This is <code className={code}>src/pages/second.tsx</code>. Any file under{" "}
          <code className={code}>src/pages/</code> becomes a route — a folder nests it, a{" "}
          <code className={code}>[id]</code> file makes it dynamic. Delete or replace it.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Use the built-in UI</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Import from <code className={code}>@/components/ui/*</code> — Card, Button, Badge, and more — and Tailwind
          classes work anywhere.
        </CardContent>
      </Card>
      <Link to="/" className="text-sm underline underline-offset-4">← Back</Link>
    </div>
  )
}
"""


def _default_styles_css() -> str:
    return """@import "tailwindcss";
@import "@avibe/show-ui/theme.css";

/*
 * The default multi-page demo styles itself with Tailwind utility classes and the
 * built-in shadcn components, so this entry only needs the two imports above
 * (keep them first) plus a small base. Add global CSS here as the app grows.
 */
body {
  margin: 0;
  background: var(--background);
  color: var(--foreground);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
"""


def _default_api_health_ts() -> str:
    return """export async function GET() {
  return Response.json({ ok: true, message: "Show Runtime handler is ready." })
}
"""
