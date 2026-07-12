from __future__ import annotations

import re
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from config import paths
from config.v2_config import V2Config
from core.avibe_cloud import avibe_cloud_connect_guidance, base_public_url
from core.show_git import format_agent_contract
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
from storage.models import agent_sessions, show_pages
from storage.pagination import PageRequest, PageResult, page_result_from_limit_plus_one

VISIBILITY_PRIVATE = "private"
VISIBILITY_PUBLIC = "public"
VISIBILITY_OFFLINE = "offline"
VISIBILITIES = {VISIBILITY_PRIVATE, VISIBILITY_PUBLIC, VISIBILITY_OFFLINE}
SHARE_ID_BYTES = 8
SHOW_EVENT_WRITE_TOKEN_COOKIE = "vibe_show_event_token"
SHOW_EVENT_WRITE_TOKEN_HEADER = "X-Vibe-Show-Token"
SHOW_CLI_EVENT_TOKEN_HEADER = "X-Vibe-Show-Cli-Token"
SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS = 30
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
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
    visibility: str
    share_id: str | None
    offline_at: str | None
    created_at: str
    updated_at: str

    @property
    def offline(self) -> bool:
        return self.visibility == VISIBILITY_OFFLINE


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


def show_page_dir(session_id: str) -> Path:
    return paths.get_show_page_dir(validate_session_id(session_id))


def show_event_write_token(session_id: str) -> str:
    return hmac.new(
        _load_or_create_show_event_secret().encode("utf-8"),
        validate_session_id(session_id).encode("utf-8"),
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
    return secrets.token_urlsafe(SHARE_ID_BYTES).rstrip("_-")


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

    def get_by_share_id(self, share_id: str) -> ShowPage | None:
        share_id = (share_id or "").strip()
        if not share_id:
            return None
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(show_pages).where(show_pages.c.share_id == share_id).limit(1)).mappings().first()
            )
            return _page_from_row(row) if row else None

    def list(self, *, visibility: str | None = None) -> list[ShowPage]:
        result = self.list_page(visibility=visibility, page_request=None)
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
    ) -> PageResult[ShowPage]:
        if visibility is not None and visibility not in VISIBILITIES:
            raise ShowPageError(f"Unsupported visibility: {visibility}", code="invalid_visibility")
        statement = select(show_pages)
        if visibility is not None:
            statement = statement.where(show_pages.c.visibility == visibility)
        if session_id:
            statement = statement.where(show_pages.c.session_id.like(_like_pattern(session_id, prefix=True), escape=_LIKE_ESCAPE))
        if updated_after:
            statement = statement.where(show_pages.c.updated_at >= updated_after)
        if updated_before:
            statement = statement.where(show_pages.c.updated_at <= updated_before)
        if query:
            pattern = _like_pattern(query, contains=True)
            statement = statement.where(
                or_(
                    show_pages.c.session_id.like(pattern, escape=_LIKE_ESCAPE),
                    show_pages.c.share_id.like(pattern, escape=_LIKE_ESCAPE),
                    show_pages.c.visibility.like(pattern, escape=_LIKE_ESCAPE),
                )
            )
        statement = statement.order_by(show_pages.c.updated_at.desc(), show_pages.c.session_id.asc())
        if page_request is not None:
            statement = statement.offset(page_request.offset).limit(page_request.limit + 1)
        with self.engine.connect() as conn:
            rows = conn.execute(statement).mappings().all()
        return page_result_from_limit_plus_one((_page_from_row(row) for row in rows), page_request)

    def ensure(self, session_id: str) -> ShowPage:
        session_id = validate_session_id(session_id)
        existing = self.get(session_id)
        if existing is not None:
            return existing
        now = _utc_now_iso()
        page = ShowPage(
            session_id=session_id,
            visibility=VISIBILITY_PRIVATE,
            share_id=None,
            offline_at=None,
            created_at=now,
            updated_at=now,
        )
        with self.engine.begin() as conn:
            conn.execute(
                insert(show_pages).values(
                    session_id=page.session_id,
                    visibility=page.visibility,
                    share_id=page.share_id,
                    offline_at=page.offline_at,
                    created_at=page.created_at,
                    updated_at=page.updated_at,
                )
            )
        return page

    def ensure_active(self, session_id: str) -> tuple[ShowPage, bool]:
        """Atomically ensure a page for a NON-archived session; return (page, created).

        The existing-row check, the archived check and the insert all run in ONE
        transaction so (a) a concurrent archive can't race the create (TOCTOU),
        and (b) ``created`` is derived from the insert itself — concurrent
        first-ensures don't both report "created" (which would otherwise double-
        send the visualize prompt or collide on the unique key). An existing page
        is returned untouched (archive already took it offline).
        """
        session_id = validate_session_id(session_id)
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            existing = (
                conn.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1))
                .mappings()
                .first()
            )
            if existing is not None:
                return _page_from_row(existing), False
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
            result = conn.execute(
                insert(show_pages)
                .prefix_with("OR IGNORE")
                .values(
                    session_id=session_id,
                    visibility=VISIBILITY_PRIVATE,
                    share_id=None,
                    offline_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            created = bool(result.rowcount and result.rowcount > 0)
            row = (
                conn.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1))
                .mappings()
                .first()
            )
        return _page_from_row(row), created

    def _is_archived(self, session_id: str) -> bool:
        with self.engine.connect() as conn:
            status = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
        return status == "archived"

    def update_visibility(self, session_id: str, visibility: str) -> ShowPage:
        session_id = validate_session_id(session_id)
        if visibility not in VISIBILITIES:
            raise ShowPageError(f"Unsupported visibility: {visibility}", code="invalid_visibility")
        # Reject republish BEFORE ``ensure`` so it doesn't first materialize a
        # default (private) page row for an archived session — that would leave
        # ``/show/<id>/`` enabled for a terminal session. The in-txn check below
        # is the atomic authority for the concurrent-archive race.
        if visibility != VISIBILITY_OFFLINE and self._is_archived(session_id):
            raise ShowPageError(
                "Cannot republish the Show Page of an archived session.",
                code="session_archived",
            )
        page = self.ensure(session_id)
        now = _utc_now_iso()
        values: dict[str, Any] = {
            "visibility": visibility,
            "updated_at": now,
            "offline_at": now if visibility == VISIBILITY_OFFLINE else None,
        }
        if visibility == VISIBILITY_PUBLIC and not page.share_id:
            values["share_id"] = self._unique_share_id()
        with self.engine.begin() as conn:
            # Archive is terminal and takes the page offline on purpose — never let
            # an archived session's page be brought back online / re-shared. Checked
            # in the SAME txn as the write so a concurrent archive can't slip in
            # between the check and the update (TOCTOU); raising here rolls back.
            if visibility != VISIBILITY_OFFLINE:
                status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
                ).scalar_one_or_none()
                if status == "archived":
                    raise ShowPageError(
                        "Cannot republish the Show Page of an archived session.",
                        code="session_archived",
                    )
            conn.execute(update(show_pages).where(show_pages.c.session_id == session_id).values(**values))
        updated = self.get(session_id)
        assert updated is not None
        return updated

    def rotate_share(self, session_id: str) -> tuple[ShowPage, str | None]:
        session_id = validate_session_id(session_id)
        # Same guard as update_visibility, before ``ensure`` materializes a page:
        # an archived session is terminal, so its share link can't be rotated /
        # re-enabled (and a stale/direct call must not create a default page).
        if self._is_archived(session_id):
            raise ShowPageError(
                "Cannot rotate the share link of an archived session.",
                code="session_archived",
            )
        page = self.ensure(session_id)
        if page.visibility != VISIBILITY_PUBLIC:
            raise ShowPageError(
                "Share links can only be rotated while the Show Page is public.",
                code="not_public",
            )
        previous_share_id = page.share_id
        new_share_id = self._unique_share_id()
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            conn.execute(
                update(show_pages)
                .where(show_pages.c.session_id == session_id)
                .values(share_id=new_share_id, updated_at=now)
            )
        updated = self.get(session_id)
        assert updated is not None
        return updated, previous_share_id

    def set_share_id(self, session_id: str, share_id: str) -> tuple[ShowPage, str | None]:
        """Set a custom public share suffix; return (page, previous_share_id).

        A custom suffix is just a chosen value for the same ``share_id`` that
        ``rotate_share`` would otherwise randomize, so this mirrors that method:
        archived sessions are terminal (guarded before ``ensure`` materializes a
        page), and the suffix can only be set while the page is public. Setting a
        new value revokes the previous public URL, exactly like a rotate.
        """
        session_id = validate_session_id(session_id)
        new_share_id = validate_share_id(share_id)
        # Pre-guard before ``ensure`` so a stale/direct call never materializes a
        # default page for an archived (terminal) session. The in-txn re-reads
        # below are the atomic authority for the concurrent-archive / concurrent
        # visibility-flip race.
        if self._is_archived(session_id):
            raise ShowPageError(
                "Cannot change the share link of an archived session.",
                code="session_archived",
            )
        self.ensure(session_id)
        now = _utc_now_iso()
        previous_share_id: str | None = None
        try:
            with self.engine.begin() as conn:
                # Read visibility, archive status, and the current suffix in the
                # SAME transaction as the write so a concurrent flip to private/
                # offline, an archive, or another session claiming the suffix
                # can't slip between the check and the update; raising rolls back.
                row = (
                    conn.execute(select(show_pages).where(show_pages.c.session_id == session_id).limit(1))
                    .mappings()
                    .first()
                )
                if row is None or row["visibility"] != VISIBILITY_PUBLIC:
                    raise ShowPageError(
                        "A custom link can only be set while the Show Page is public.",
                        code="not_public",
                    )
                status = conn.execute(
                    select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
                ).scalar_one_or_none()
                if status == "archived":
                    raise ShowPageError(
                        "Cannot change the share link of an archived session.",
                        code="session_archived",
                    )
                previous_share_id = row["share_id"]
                if new_share_id != previous_share_id:
                    # Idempotent when unchanged (skips the write, so no self-
                    # collision and no updated_at churn). Otherwise reject a
                    # suffix held by another session; the unique constraint is
                    # the final authority (IntegrityError below).
                    taken_by = conn.execute(
                        select(show_pages.c.session_id).where(show_pages.c.share_id == new_share_id).limit(1)
                    ).scalar_one_or_none()
                    if taken_by is not None and taken_by != session_id:
                        raise ShowPageError(
                            "That custom link is already taken. Pick another.",
                            code="share_id_taken",
                        )
                    conn.execute(
                        update(show_pages)
                        .where(show_pages.c.session_id == session_id)
                        .values(share_id=new_share_id, updated_at=now)
                    )
        except IntegrityError:
            raise ShowPageError(
                "That custom link is already taken. Pick another.",
                code="share_id_taken",
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
        visibility=str(row["visibility"]),
        share_id=str(row["share_id"]) if row["share_id"] else None,
        offline_at=str(row["offline_at"]) if row["offline_at"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


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
        "path": str(path),
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
    # Runtime-owned app shell + always-present workspace files. Each is written
    # once and skipped if it already exists, so an existing workspace keeps its
    # own copies (see the skip-if-exists loop below).
    files: dict[str, str] = {
        "index.html": _default_index_html(session_id),
        "src/main.tsx": _default_main_tsx(),
        "src/styles.css": _default_styles_css(),
        "api/health.ts": _default_api_health_ts(),
    }
    # Multi-page demo. Only seed it into a FRESH workspace — one that does not yet
    # have its own ``src/App.tsx``. This keeps existing single-page workspaces
    # byte-for-byte unchanged (we never drop router/pages files next to a page the
    # agent already authored) while a brand-new Show Page starts as a working
    # multi-page app the agent can inspect, run, extend, or replace. Adding a page
    # later is just a new file under ``src/pages/`` — the router discovers it and
    # the runtime-owned shell (``index.html`` / ``src/main.tsx``) is never touched.
    if not (page_dir / "src" / "App.tsx").exists():
        files.update(
            {
                "src/App.tsx": _default_app_tsx(),
                "src/router.tsx": _default_router_tsx(),
                "src/pages/index.tsx": _default_page_home_tsx(),
                "src/pages/items/index.tsx": _default_page_items_index_tsx(),
                "src/pages/items/[id].tsx": _default_page_item_detail_tsx(),
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
    <title>Show Page {escaped}</title>
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


def show_page_runtime_recovery_html(session_id: str) -> str:
    session_id = validate_session_id(session_id)
    escaped = _escape_html(session_id)
    loading_delay = f"{SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS}s"
    prompt = (
        "Please repair this avibe Show Page. Open the Show Page workspace for session "
        f"{session_id}, read the local Show Page/runtime instructions, then replace src/App.tsx "
        "with a polished React page. Use the shadcn-style components from @/components/ui and "
        "@avibe/show-ui. Do not edit index.html unless it is required. If the browser shows "
        "Ready to visualize, check src/App.tsx, src/main.tsx, src/styles.css, and the Vite/browser "
        "console for compile or runtime errors. Make the page responsive and verify it renders.\n\n"
        "Show Page history contract:\n"
        f"{format_agent_contract(numbered=True, session_id=session_id)}"
    )
    escaped_prompt = _escape_html(prompt)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Show Page recovery {escaped}</title>
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
      <div class="show-recovery-loading">Loading Show Page</div>
      <section class="show-recovery-panel">
        <div class="show-recovery-eyebrow">Vibe Show recovery</div>
        <h1>Ready to visualize</h1>
        <p>The managed Show runtime did not respond, so avibe is showing this recovery page instead of serving a raw app shell.</p>
        <div class="show-recovery-grid">
          <div class="show-recovery-card">
            <h2>Ask your agent to fix the Show Page</h2>
            <textarea id="show-recovery-agent-prompt" readonly>{escaped_prompt}</textarea>
            <div class="show-recovery-actions">
              <button class="show-recovery-button" type="button" data-copy-prompt>Copy prompt</button>
              <button class="show-recovery-button secondary" type="button" onclick="window.location.reload()">Retry</button>
            </div>
          </div>
          <div class="show-recovery-card">
            <h2>What to check</h2>
            <ul>
              <li>Wait a moment and refresh if the runtime is still starting.</li>
              <li>Ask the agent to inspect Vite and browser console errors.</li>
              <li>The main file to edit is <code>src/App.tsx</code>.</li>
              <li>Use shared UI imports like <code>@/components/ui/card</code>.</li>
            </ul>
          </div>
        </div>
        <p>Session: <code>{escaped}</code></p>
      </section>
    </main>
    <script>
      document.querySelector("[data-copy-prompt]")?.addEventListener("click", async (event) => {{
        const prompt = document.getElementById("show-recovery-agent-prompt")?.value || "";
        await navigator.clipboard.writeText(prompt);
        event.currentTarget.textContent = "Copied";
      }});
    </script>
  </body>
</html>
"""


def _escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _default_main_tsx() -> str:
    return """import React from "react"
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
  sessionId: injected.sessionId ?? (window.location.pathname.match(/\\/show\\/([^/]+)/)?.[1]
    ? decodeURIComponent(window.location.pathname.match(/\\/show\\/([^/]+)/)![1])
    : undefined),
  basePath: injected.basePath ?? (window.location.pathname.match(/^(.*\\/(?:show|p)\\/[^/]+\\/)$/)?.[1] || window.location.pathname.replace(/[^/]*$/, "")),
  eventsPath: injected.eventsPath ?? "__show/events",
  streamPath: injected.streamPath ?? "__show/events?stream=1",
  writeToken: injected.writeToken ?? readCookie("vibe_show_event_token")
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
"""


def _default_app_tsx() -> str:
    # The default multi-page demo (agent-editable). It composes the shared layout
    # (nav derived from the discovered pages) with the routed page. This file and
    # everything under src/pages/ + src/router.tsx are a starting point: the agent
    # can restyle the nav, add pages, or replace the whole thing. The runtime-owned
    # app shell (index.html + src/main.tsx) is never edited to add a page.
    return """import { useEffect } from "react"
import { ThemeProvider } from "@avibe/show-ui/theme"
import { cn } from "@/lib/utils"
import { activeLocale, Link, RouterView, navItems, useRoutePath } from "./router"

function Nav() {
  const path = useRoutePath()
  return (
    <nav className="flex flex-wrap items-center gap-1">
      {navItems.map((item) => {
        const active = path === item.to || (item.to !== "/" && path.startsWith(item.to + "/"))
        return (
          <Link
            key={item.to}
            to={item.to}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              active
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {item.label}
          </Link>
        )
      })}
    </nav>
  )
}

export default function App() {
  useEffect(() => {
    document.documentElement.lang = activeLocale()
  }, [])
  return (
    <ThemeProvider preset="zinc">
      <div className="min-h-screen bg-background text-foreground">
        <header className="sticky top-0 z-10 border-b border-border bg-background/85 backdrop-blur">
          <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3 px-4 py-3">
            <Link to="/" className="text-sm font-semibold tracking-tight">
              Show Page
            </Link>
            <Nav />
          </div>
        </header>
        <main className="mx-auto max-w-3xl px-4 py-8">
          <RouterView />
        </main>
      </div>
    </ThemeProvider>
  )
}
"""


def _default_router_tsx() -> str:
    # A tiny, dependency-free hash router with file-based page discovery.
    #
    # Why hash routing: a Show Page is served as a client-only app mounted under a
    # path prefix (/show/<id>/ privately, /p/<share>/ publicly, both proxied to the
    # same managed Vite dev server). With hash routes the browser only ever requests
    # the app root, so deep-linking and refreshing a nested route work identically in
    # both serving modes with no server cooperation, and relative URLs (assets,
    # ./api/* handlers, event endpoints) always resolve. The trade-off is a "#" in
    # the URL, which stays bookmarkable and PWA-installable.
    #
    # Why file-based discovery: adding a route is just adding a file under src/pages/.
    # A folder becomes a nested path segment and a [param] file becomes a dynamic
    # segment, so the scaffold is not locked into a flat page list. Nothing here or in
    # the app shell needs editing to add a page.
    return """import type { ComponentType, ReactNode } from "react"
import { useSyncExternalStore } from "react"

// Locale-aware demo copy. The generated demo keeps a zh/en first-run experience
// (the previous scaffold did too) without pulling in an i18n framework. Language
// is read once from the browser. Replace or extend this however you localize.
export function activeLocale(): "en" | "zh" {
  const lang = (typeof navigator !== "undefined" && navigator.language) || "en"
  return lang.toLowerCase().startsWith("zh") ? "zh" : "en"
}

export function t(en: string, zh: string): string {
  return activeLocale() === "zh" ? zh : en
}

export type PageMeta = {
  // Label shown in the nav. Falls back to a title-cased path when omitted.
  title?: string
  // Sort order within the nav (lower first).
  order?: number
  // Set false to keep a static page out of the nav (it stays directly linkable).
  nav?: boolean
}

export type PageProps = {
  // Values captured from [param] segments, e.g. { id: "42" } for /items/42.
  params: Record<string, string>
}

type PageModule = {
  default: ComponentType<PageProps>
  meta?: PageMeta
}

type Segment = { name: string; dynamic: boolean }
type Route = {
  path: string
  segments: Segment[]
  Component: ComponentType<PageProps>
  meta: PageMeta
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

function titleCase(value: string): string {
  return value.replace(/[-_]/g, " ").replace(/\\b\\w/g, (c) => c.toUpperCase())
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
    return { path, segments, Component: mod.default, meta: mod.meta ?? {}, dynamic: segments.some((s) => s.dynamic) }
  })
  .filter((route): route is Route => route !== null)
  .sort((a, b) => {
    const specA = routeSpecificity(a.segments)
    const specB = routeSpecificity(b.segments)
    if (specA !== specB) return specA < specB ? -1 : 1
    return a.path.localeCompare(b.path)
  })

export const navItems: { to: string; label: string }[] = routes
  .filter((route) => !route.dynamic && route.meta.nav !== false)
  .sort((a, b) => (a.meta.order ?? 0) - (b.meta.order ?? 0) || a.path.localeCompare(b.path))
  .map((route) => ({
    to: route.path,
    label: route.meta.title ?? (route.path === "/" ? "Home" : titleCase(route.path.slice(1).split("/").pop() || "")),
  }))

// decodeURIComponent throws on a malformed escape (e.g. a link built with a raw
// "%", like #/items/50%); fall back to the raw segment so a bad param degrades to
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
      else if (segment.name !== parts[i]) {
        matched = false
        break
      }
    }
    if (matched) return { route, params }
  }
  return { route: null, params: {} }
}

function readHashPath(): string {
  const hash = window.location.hash
  const raw = (hash.startsWith("#") ? hash.slice(1) : hash).split("?")[0]
  if (!raw) return "/"
  const path = raw.startsWith("/") ? raw : "/" + raw
  // Normalize a trailing slash so "/items/" matches the "/items" route.
  return path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange)
  return () => window.removeEventListener("hashchange", onChange)
}

export function useRoutePath(): string {
  return useSyncExternalStore(subscribe, readHashPath, () => "/")
}

export function navigate(to: string): void {
  window.location.hash = to.startsWith("/") ? to : "/" + to
}

export function Link({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  // A plain hash anchor: the browser updates the fragment and fires "hashchange"
  // without a full navigation, so no click handler or pushState is needed.
  return (
    <a href={"#" + to} className={className}>
      {children}
    </a>
  )
}

export function RouterView() {
  const path = useRoutePath()
  const { route, params } = matchRoute(path)
  if (!route) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 text-card-foreground">
        <h1 className="text-lg font-semibold">{t("Page not found", "页面不存在")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {t("No route matches", "没有匹配的路由")}{" "}
          <code className="rounded bg-muted px-1.5 py-0.5">{path}</code>.
        </p>
        <p className="mt-4 text-sm">
          <a className="font-medium underline underline-offset-4" href="#/">
            {t("Back to Home", "返回首页")}
          </a>
        </p>
      </div>
    )
  }
  const Page = route.Component
  return <Page params={params} />
}
"""


def _default_page_home_tsx() -> str:
    # Demo landing page. Generic on purpose: it teaches the "add a file = add a
    # page" pattern without implying a page must map to a topic, feature, or
    # history. The agent is free to replace or remove it. Copy is locale-aware.
    return """import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Link, t } from "../router"

export const meta = { title: t("Home", "首页"), order: 0 }

const codeClass = "rounded bg-muted px-1.5 py-0.5 font-mono text-xs"

export default function Home() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Badge>{t("Starter", "起始模板")}</Badge>
        <h1 className="text-2xl font-semibold tracking-tight">{t("A multi-page Show Page", "多页 Show Page")}</h1>
        <p className="text-muted-foreground">
          {t(
            "This workspace starts as a small multi-page app so routing is ready to use. It is only a starting point — restyle it, extend it, or replace it with whatever structure fits your app: flat pages, sections, or nested routes.",
            "这个工作区默认就是一个小型多页应用，路由开箱即用。它只是一个起点——随意改样式、扩展，或换成任何适合你的结构：扁平页面、分区，或嵌套路由。",
          )}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("Add a page", "添加页面")}</CardTitle>
          <CardDescription>{t("Routing is file-based — no config and no app-shell edits.", "文件即路由——无需配置，也不用改应用外壳。")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted-foreground">
          <p>
            {t("Create a file under ", "在 ")}
            <code className={codeClass}>src/pages/</code>
            {t(" and its location becomes the route:", " 下新建文件，它的位置就是路由：")}
          </p>
          <ul className="space-y-1">
            <li>
              <code className={codeClass}>src/pages/about.tsx</code> → <code className={codeClass}>#/about</code>
            </li>
            <li>
              <code className={codeClass}>src/pages/items/index.tsx</code> → <code className={codeClass}>#/items</code>
            </li>
            <li>
              <code className={codeClass}>src/pages/items/[id].tsx</code> → <code className={codeClass}>#/items/:id</code>
              {" "}({t("nested + dynamic", "嵌套 + 动态")})
            </li>
          </ul>
          <p>
            {t("Export a default component. Add an optional ", "默认导出一个组件。可选导出 ")}
            <code className={codeClass}>meta</code>
            {t(" to set its nav label and order.", " 来设置导航标题和排序。")}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("Nested routing", "嵌套路由")}</CardTitle>
          <CardDescription>{t("Folders become nested paths; [param] files capture values.", "文件夹变成嵌套路径；[param] 文件捕获动态值。")}</CardDescription>
        </CardHeader>
        <CardContent>
          <Link to="/items" className="font-medium text-foreground underline underline-offset-4">
            {t("Open the Items demo →", "打开 Items 示例 →")}
          </Link>
        </CardContent>
      </Card>
    </div>
  )
}
"""


def _default_page_items_index_tsx() -> str:
    # Demo list page under a folder, so the route is nested (#/items) and links
    # into a dynamic child route (#/items/:id).
    return """import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Link, t } from "../../router"

export const meta = { title: t("Items", "条目"), order: 1 }

const items = [
  { id: "1", name: t("First item", "第一个条目"), hint: t("A demo record", "示例数据") },
  { id: "2", name: t("Second item", "第二个条目"), hint: t("Another demo record", "另一条示例数据") },
  { id: "3", name: t("Third item", "第三个条目"), hint: t("One more demo record", "再来一条示例数据") },
]

export default function Items() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">{t("Items", "条目")}</h1>
        <p className="text-muted-foreground">
          {t("Each item links to a nested route such as ", "每个条目都链接到一个嵌套路由，比如 ")}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">#/items/1</code>
          {t(". Open one, then reload — the deep link loads directly.", "。打开其中一个再刷新——深链接会直接加载。")}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((item) => (
          <Link key={item.id} to={"/items/" + item.id} className="block">
            <Card className="h-full transition-colors hover:border-primary/50">
              <CardHeader>
                <CardTitle className="text-base">{item.name}</CardTitle>
                <CardDescription>{item.hint}</CardDescription>
              </CardHeader>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  )
}
"""


def _default_page_item_detail_tsx() -> str:
    # Demo dynamic route: src/pages/items/[id].tsx -> /items/:id. Reads the id
    # param and is directly deep-linkable / refreshable.
    return """import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Link, t, type PageProps } from "../../router"

const codeClass = "rounded bg-muted px-1.5 py-0.5 font-mono text-xs"

export default function ItemDetail({ params }: PageProps) {
  return (
    <div className="space-y-6">
      <Link to="/items" className="text-sm text-muted-foreground underline underline-offset-4">
        {t("← Back to Items", "← 返回 Items")}
      </Link>

      <Card>
        <CardHeader>
          {/* w-fit: CardHeader is a flex column (align-items: stretch), which
              blockifies an inline-flex Badge and stretches it to full width. */}
          <Badge className="w-fit">{t("Nested route", "嵌套路由")}</Badge>
          <CardTitle>{t("Item", "条目")} {params.id}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm text-muted-foreground">
          <p>
            {t("This page is ", "这个页面是 ")}
            <code className={codeClass}>src/pages/items/[id].tsx</code>
            {t(", matched from the URL. The ", "，根据 URL 匹配。参数 ")}
            <code className={codeClass}>id</code>
            {t(" parameter is ", " 的值是 ")}
            <code className={codeClass}>{params.id}</code>.
          </p>
          <p>{t("Reload the page — this deep link resolves on the client, in private and public modes.", "刷新页面——这个深链接会在客户端解析，私有和公开模式都一样。")}</p>
        </CardContent>
      </Card>
    </div>
  )
}
"""


def _default_styles_css() -> str:
    return """@import "tailwindcss";
@import "@avibe/show-ui/theme.css";

:root {
  color-scheme: light;
}

/*
 * The default multi-page demo styles itself with Tailwind utility classes and the
 * @avibe/show-ui shadcn components, so this entry only needs the two imports above
 * (keep them first) plus a small base. Add global CSS here as the app grows.
 */
body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
"""


def _default_api_health_ts() -> str:
    return """export async function GET() {
  return Response.json({ ok: true, message: "Show Runtime handler is ready." })
}
"""
