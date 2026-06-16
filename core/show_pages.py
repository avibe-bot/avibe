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

from config import paths
from config.v2_config import V2Config
from core.avibe_cloud import avibe_cloud_connect_guidance, base_public_url
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
    files = {
        "index.html": _default_index_html(session_id),
        "src/main.tsx": _default_main_tsx(),
        "src/App.tsx": _default_app_tsx(),
        "src/styles.css": _default_styles_css(),
        "api/health.ts": _default_api_health_ts(),
    }
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
        "console for compile or runtime errors. Make the page responsive and verify it renders."
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
    return """import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ThemeProvider } from "@avibe/show-ui/theme"

export default function App() {
  return (
    <ThemeProvider preset="zinc">
      <main className="page">
        <Card className="panel">
          <CardHeader>
            <CardTitle>Ready to visualize</CardTitle>
            <CardDescription>This Show Page is served by the managed React runtime.</CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={() => void fetch("./api/health")}>Call handler</Button>
          </CardContent>
        </Card>
      </main>
    </ThemeProvider>
  )
}
"""


def _default_styles_css() -> str:
    return """body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f6f7f9;
  color: hsl(var(--avs-foreground));
}

.page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}

.panel {
  width: min(560px, 100%);
}
"""


def _default_api_health_ts() -> str:
    return """export async function GET() {
  return Response.json({ ok: true, message: "Show Runtime handler is ready." })
}
"""
