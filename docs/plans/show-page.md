# Show Page Plan

## Summary

Show Page gives every Vibe Remote Agent Session one canonical visual
presentation surface.

When chat text is not enough, an agent can ask Vibe Remote for a
session-scoped local directory, write a small web experience into that
directory, and send the user a URL. The user opens the URL through the
existing Vibe Remote Web UI / Vibe Cloud tunnel and sees the generated
visualization, report, walkthrough, chart, or interactive explanation.

This is an agent-facing artifact surface:

- chat messages are for conversation
- file attachments are for deliverables
- Show Pages are for visual explanation

The first implementation is CLI-first and static-file-first. Hot reload,
React dev servers, and full-stack app hosting are deferred until the static
Show Page model is stable.

## Vision

Agents often need to explain work in forms that do not fit well inside IM
messages: incident timelines, dependency graphs, test dashboards, architecture
maps, interactive diffs, UI proposals, and visual reports that the user can
revisit after the chat scrolls away.

Show Page makes visual communication a first-class part of Vibe Remote. The
agent writes web files. Vibe Remote owns the session binding, URL shape,
visibility, authentication, CLI ergonomics, and future Web UI entry points.

## Design Philosophy

### Session-scoped, not document-scoped

One Agent Session maps to one Show Page. A session normally represents one
topic or workstream, so one visual surface is enough. The page can still
contain tabs, anchors, internal navigation, or client-side routes, but Vibe
Remote only manages one canonical page per session.

### Local-first data plane

Page contents live on the user's machine under Vibe Remote's local data
directory:

```text
~/.vibe_remote/show/<session-id>/
~/.vibe_remote/show/<session-id>/index.html
```

Vibe Cloud and `avibe.bot` provide identity, tunnel, and public URL
reachability. They should not become the data-plane storage layer for page
contents.

### CLI-first agent contract

The agent process cannot reliably infer the Vibe Agent Session ID from ambient
context, so V1 commands require an explicit `--session-id`.

The core contract is stable even if the physical path changes later:

```bash
vibe show path --session-id <session-id>
```

The command creates or resolves the session's Show Page workspace and prints
the directory path. The agent can then write `index.html` and related assets
there.

### Visibility is explicit and canonical

Each Show Page has exactly one active URL path at a time:

- private: the private URL path is active
- public: the public share URL path is active
- offline: no URL path is active

This avoids ambiguity where the same content is reachable through both a
private session URL and a public share URL.

### Private by default

Private pages reuse the existing remote Web UI authorization model. When Vibe
Cloud tunnel access is enabled, remote browser requests already go through
OAuth and receive a local authenticated Web UI session cookie. Private Show
Page requests stay inside that same guard.

### Public means a deliberate unauthenticated share

Public pages do not expose the session ID. They use an independent share ID and
a short public path:

```text
/p/<share-id>/
```

The public path is a narrow authentication bypass for public Show Page assets
only. It must still preserve host validation, path containment, share lookup,
visibility checks, and offline checks.

### Offline, not file deletion

Taking a Show Page offline revokes URL access only. It does not delete local
files. User-facing copy should consistently say that the page is "offline" or
"taken down" so users do not assume the agent removed local code.

## Confirmed Product Decisions

- One Agent Session has at most one Show Page.
- V1 commands require `--session-id`; no automatic session inference.
- Private pages reuse existing Web UI / remote-access login state.
- Public pages use an independent share ID and skip authentication only for the
  public Show Page route.
- Commands support switching between private, public, and offline states.
- A page has one active canonical path based on its current visibility.
- Public share IDs support revocation by rotation.
- No automatic cleanup, quota, or time-based lifecycle in V1.
- Manual takedown is soft delete: revoke access without deleting local files.
- Web UI management panels are out of scope for V1.
- The Show Pages prompt injection is configurable from Web UI messaging
  settings. Turning it off removes the agent guidance only; CLI commands and
  page serving remain available.
- Hot reload and managed dev-server hosting are out of scope for V1.

## URL Model

Assume the user's remote access public URL is:

```text
https://alex-app.avibe.bot
```

Private canonical URL:

```text
https://alex-app.avibe.bot/show/<session-id>/
```

Public canonical URL:

```text
https://alex-app.avibe.bot/p/<share-id>/
```

Rules:

- private visibility serves only `/show/<session-id>/...`
- public visibility serves only `/p/<share-id>/...`
- switching from private to public disables the private path
- switching from public to private disables the public path
- rotating a public share ID invalidates the old URL
- offline pages return a stable response explaining that the page is offline

## CLI Model

V1 keeps the command surface small and scriptable:

```bash
vibe show path --session-id <session-id>
vibe show status --session-id <session-id>
vibe show update --session-id <session-id> --visibility public
vibe show update --session-id <session-id> --visibility private
vibe show update --session-id <session-id> --visibility offline
vibe show update --session-id <session-id> --rotate-share
```

The command model is centered on three actions:

- `path`: get the workspace to write files
- `status`: inspect current state
- `update`: change published state or rotate a public share link

Every command that returns state supports `--json` so agents can consume it
without parsing human text.

CLI output should progressively disclose next useful actions. JSON output
should be detailed and stable enough for agents to consume, including
`session_id`, `visibility`, `path`, `active_url`, `private_url`, `public_url`,
`share_id`, `offline`, timestamps, a human message, and suggested
`next_actions`.

State transition commands should include the new active URL and, when useful,
the URL or share ID that just became inactive.

## System Prompt Integration

Vibe Remote injects shared capability guidance through
`core/system_prompt_injection.py`. Show Pages belong in that same prompt rather
than in backend-specific wording:

- Claude receives it through `SessionHandler._build_claude_system_prompt(...)`.
- OpenCode receives it as the per-turn `system` value in `prompt_async(...)`.
- Codex receives it as `developerInstructions` when a thread starts or resumes.

The Show Pages block should sit near "Send files" because it is an output and
presentation surface, not a background execution primitive like tasks,
watches, and hooks.

Recommended ordering, without numbered section titles:

```text
# Vibe Remote

Silent replies
Send files
Show Pages
Quick-reply buttons
Scheduled tasks, watches, and hooks
User Context and Preferences
```

The prompt should guide agents toward better visual communication practices:

- use Show Pages only when a visual page improves understanding
- choose the visual form that best fits the information
- use diagrams, mind maps, flowcharts, timelines, charts, dashboards, or
  comparison views when appropriate
- apply good information hierarchy, typography, spacing, contrast, and mobile
  compatibility
- keep pages private by default
- publish publicly only when the user explicitly asks for a shareable link
- ask briefly when a Show Page would help but the user's preference is unclear
- send the active URL and a short explanation after creating or updating a page

The prompt should mention useful reference libraries without limiting the
agent: native HTML/CSS/JavaScript, Excalidraw-style static SVG/PNG diagrams,
React Flow, Mermaid, Markmap, Chart.js, and Cytoscape.js.

Show Page guidance is always included in the shared system prompt for every
agent backend. There is no configuration toggle or Web UI setting for it;
legacy configurations containing `show_pages_prompt` are accepted and the
retired field is ignored and omitted on the next save.

## State Model

Show Page state is local runtime state, not global configuration:

```text
show_pages
  session_id TEXT PRIMARY KEY
  visibility TEXT NOT NULL       -- private | public | offline
  share_id TEXT                  -- independent public token
  offline_at TEXT
  created_at TEXT NOT NULL
  updated_at TEXT NOT NULL
```

The local file path is derived from `session_id` and does not need to be stored
unless a later version supports custom roots.

Important invariants:

- `session_id` is the aggregate root
- `share_id` is never derived from `session_id`
- `visibility=offline` disables serving but does not delete files
- switching visibility updates canonical URL behavior but does not move files
- revoking public access changes `share_id`

## Serving Model

The Web UI server owns Show Page serving because it already owns local auth
enforcement and remote-access host validation.

Private route:

```text
GET /show/<session-id>/<asset-path>
```

Public route:

```text
GET /p/<share-id>/<asset-path>
```

Serving rules:

- default `/` under a page to `index.html`
- serve only files inside the resolved Show Page directory
- reject path traversal
- show a small user-facing offline page when a known Show Page has been taken
  down
- do not expose arbitrary files outside the Show Page root
- do not make public auth bypass apply to API routes or the rest of the Web UI
- add strict response headers where practical without breaking normal static
  HTML/CSS/JS usage

## Dynamic App And Hot Reload Direction

There are mature ways to support React components and hot loading, but they are
a V2 capability and should not be mixed into static V1.

The deeper split is between two product concepts:

- Show Page: a durable session artifact that Vibe Remote can safely serve
- Show App: a live local process that Vibe Remote supervises and proxies

Static Show Page is mainly a file-serving, URL, and permission problem. Show
App is also a process lifecycle, port allocation, WebSocket proxy, dependency,
log, and security-boundary problem.

Recommended future path:

- start with static React/Vite builds served through the Show Page routes
- add private Vite dev-server proxying only after the static model is stable
- defer public dev-server sharing until there is an explicit security policy
- treat Next.js-style full-stack app hosting as a later "Show App" adapter, not
  as part of the static Show Page foundation

## Non-Goals For V1

- Multiple Show Pages per session.
- A Web UI management panel.
- Automatic cleanup or quota management.
- Hosted storage in Vibe Cloud.
- Public URLs based on session IDs.
- Running arbitrary Next.js or backend server code as part of static Show Page
  serving.
- Hot reload or managed dev-server proxying.
