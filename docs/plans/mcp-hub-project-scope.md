# MCP Hub project scope

> Status: draft v3 — owner decisions 2026-11-03 (D1–D6 final), review round 1
> (advisor, codex-astra) folded in. Extends `docs/MCP_HUB.md`, `docs/plans/mcp-hub-mvp.md`,
> `docs/plans/mcp-hub-oauth.md`.

## 1. Background

MCP Hub servers are global today. Avibe-added servers live in
`~/.avibe/mcp/<name>/` (`definition.json`, `oauth.json`, `current.json`,
`revisions/`); native Claude/Codex/OpenCode entries are discovered read-only.
Every Session sees the same list:

- `core/mcp_hub/prompt.py` `build_mcp_prompt_blocks()` takes no scope and lists
  every published package; `core/system_prompt_injection.py` calls it
  unconditionally.
- `vibe/mcp_cli.py` `--list`, `_published()`, `_server_list()` and
  `vibe/mcp_api.py` `list_servers`/`detail` read `~/.avibe/mcp` directly.
- `vibe mcp <server> <tool>` sends `sessionId` over IPC; the Controller uses it
  only in the connection key `(name, revision, session)`.
- Every repository function and every runtime map (`_package_locks`,
  `_oauth_tasks`, `_oauth_providers`, `_oauth_provider_locks`,
  `_pending_logins`, connections) is keyed by `name` against the global root.

Teams want a repository-owned MCP config, the Claude Code `.mcp.json` file,
without secrets in the repository, and Sessions of one project must not see
another project's servers.

## 2. Goal and non-goals

Goal: an Avibe Project can define MCP servers in `<project folder>/.mcp.json`.
Secrets live in Avibe's private cache. A Session in that Project sees global
servers plus that Project's servers in the prompt, in every `vibe mcp` read, in
the Web UI, and at call time; never another Project's.

Non-goals:

- Changing the global layout. It is already private (`0700`/`0600`, outside any
  repository) and shipped in v3.2.4; no migration.
- Security isolation between Projects of the same OS user (§10).
- Importing other native project configs (`opencode.json`, Codex project config,
  Claude's per-path entries in `~/.claude.json`).
- A file watcher, or a Project delete flow (Projects only archive today).

## 3. Decisions

- **D1 Project identity: same rule as project Skills.** The project root of a
  Session is found exactly as `core/managed_skills.py` finds project Skills:
  start at the Session working directory and walk up to the run target's
  `project_base` when the working directory is inside it
  (`managed_skill_project_base`), otherwise to the nearest `.git` ancestor;
  with neither, the working directory itself. The project config is
  `<project root>/.mcp.json`. So Web Project Sessions, IM channels whose workdir
  is a project folder, background and forked Sessions all get the same answer
  Skills gives them. The cache key (`<key>` below) is the Avibe Project id
  (`proj_<hex>`) when the root is a Project's folder
  (`projects_service._find_project_by_workdir`), otherwise
  `dir-<sha256(realpath(root))[:12]>`. A root with no `.mcp.json` → global only.
- **D2 Visibility.** Project Session sees global ∪ own Project; same name →
  the Project server shadows the global one. Other Projects are never visible.
- **D3 Approval.** Every server defined by a repository `.mcp.json`, stdio and
  HTTP, is `pending_approval` until an Owner approves it: it does not start,
  is not advertised, and cannot be called. Approval binds to
  `{"v": 1, "hash": sha256(canonical JSON of the raw .mcp.json entry)}`, i.e.
  **before** placeholder substitution, so secrets never enter the hash and
  rotating a secret does not need re-approval. Any change to the raw entry needs
  approval again.
- **D4 Placeholders.** `${VAR}` is accepted in a project `.mcp.json` only inside
  `url`, env values and header values (not in `command`/`args`, which would put
  secrets in argv). It is a "secret needed here" marker with **one namespace**:
  every placeholder, in env, headers or url, resolves from that server's cached
  variables (`secrets.json` `env`), never from the process environment. One
  token used in both an env value and a header is entered once. Unresolved →
  `needs_config` listing the missing names.
- **D5 Write-back.** Adding, editing or removing a project server through Avibe
  rewrites `<project root>/.mcp.json` atomically (§4 rules) with only
  `type`/`command`/`args`/`url`/`cwd`; every env and header value goes to the
  cache. Non-secret values a team wants in the repository are written into
  `.mcp.json` by hand. Committing the file is the user's choice.
- **D6 Global unchanged.**

## 4. Files

```
<project folder>/.mcp.json             # repository-owned, Claude Code format, no secrets
~/.avibe/project_mcp/<key>/            # private cache (0700 dirs, 0600 files); key per D1
└── <name>/
    ├── secrets.json                   # {"v": 1, "env": {...}, "headers": {...}}; env doubles as placeholder variables
    ├── approval.json                  # {"v": 1, "hash": "...", "approvedAt": "...", "by": "..."}
    ├── oauth.json                     # same shape as global (mcp-hub-oauth.md §6)
    ├── current.json                   # published revision pointer
    └── revisions/<rev>/               # merged definition / manifest / tools / commands
```

- Parsing reuses `definition_from_provider` with an `allow_placeholders` switch
  limited to D4's fields; client-specific keys stay ignored.
- Writes to `.mcp.json` (D5) are atomic, preserve other servers and unknown
  top-level and per-server keys, and are refused with a reload when the file's
  `(mtime, size, hash)` changed since it was read (external edit, git checkout).
- Unreadable or invalid `.mcp.json` → that Project has no project servers, a
  warning in the log and an error banner in the Project view; prompt, CLI and
  startup never fail. Unreadable cache files degrade that server to
  `needs_config` or unlisted.

## 5. Effective definition (load-time merge)

For each raw `.mcp.json` entry:

1. Substitute `${VAR}` in D4 fields from `secrets.env`. Missing →
   `needs_config`.
2. `env = {...file.env, ...secrets.env}`.
3. Headers merge by case-insensitive name; cache wins.
4. stdio `cwd`: absent → project folder; relative → resolved against the
   folder holding `.mcp.json` (reuse `native._resolve_cwd`).
5. OAuth from the cache `oauth.json`; an explicit `Authorization` header after
   the merge disables OAuth (existing rule).

The published revision stores this merged definition inside the private cache,
so the call path needs only scope-aware keys (§7).

## 6. Visibility: one resolver

`core/mcp_hub/scope.py` owns "which servers can this caller see":

```
Scope = "global" | "project:<key>"
project_scope(working_directory, project_base) -> Scope | None   # D1, shares Skills' root walk
visible(scopes: list[Scope]) -> {name: ScopedServer(scope, name, store)}
```

Project wins on name. Prompt advertising counts only `ok` packages (as today);
listing shows every state (`pending_approval`, `needs_config`, `needs_login`,
`ok`, refresh failure).

Consumers, none of which decide scope on their own:

- **Prompt.** `build_system_prompt_blocks` already receives the Skills inputs
  (`skills_cwd`, `skills_project_base`) on every backend path (Vibey, Claude,
  Codex, OpenCode; IM, background and forked Sessions). It passes the same two
  values to `build_mcp_prompt_blocks(cwd, project_base)`; no new DB query on the
  loop. Forks and `vibe agent run` children inherit the working directory and
  project base exactly as they do for Skills.
- **CLI.** All reads (`--list`, `<server> --list`, `--help`, detail) move to IPC
  and carry the same working directory and project base the CLI already gets for
  Skills (`AVIBE_SKILL_WORKING_DIR`, `AVIBE_SKILL_PROJECT_BASE`, else the process
  cwd) plus `AVIBE_SESSION_ID`; the Controller applies D1. A user terminal gets
  the project of its own cwd, as `vibe skill list` does. Controller down → `unavailable`, no second offline read path.
  Management on a shadowed name (`--login`, `--logout`, `--refresh`, `--remove`,
  `--approve`) targets the visible (project) server; `--global` targets the
  global one.
- **Call.** Admission resolves the name in the caller's visible set (D1 from the
  call's working directory and project base) before any connection; outside it
  → `not_found`.
- **Web.** `/api/mcp*` take `project=<proj id>` (Web manages Avibe Projects;
  `dir-` keys are reachable from Sessions and CLI only). Reads check
  `project_access_service.can_read_project`; mutations (add, remove, refresh,
  approve, configure secrets, login, logout) are Owner-only, go over IPC, and
  the Controller re-checks the Project. Reads go to the project Store, not
  straight to the global root.

## 7. Storage and runtime keys

- `repository` becomes a `Store(root)`; global is `Store(~/.avibe/mcp)`, a
  Project is `Store(~/.avibe/project_mcp/<id>)`. `_stale` cleanup and `_admit`
  operate per Store, so project packages are never treated as global leftovers.
- One key type `ScopedName(scope, name)` replaces `name` in package locks,
  connection keys (`(scope, name, revision, session)`, including the `("open",)`
  lock), `_oauth_tasks`, `_oauth_providers`, `_oauth_provider_locks`, and the
  `_pending_logins` cleanup loops.
- OAuth: the pending record stores `ScopedName`; the callback is still found by
  `state` alone (unique, one-time), so the redirect URI and callback route are
  unchanged. Pending state is in memory; after a restart the user starts login
  again (current behavior). Logout routes by `ScopedName`.

## 8. Lifecycle

- **Discovery.** The existing discover pass also covers non-archived Avibe
  Projects that have a folder; `dir-` roots are discovered on first resolve. It runs off the loop (`asyncio.to_thread` for file and DB
  work), one Project at a time, with a bounded per-Project server count.
- **Staleness.** When the resolver serves a Project caller, a bounded
  `stat(mtime, size)` of `.mcp.json` runs in a thread; only on change is the
  file read and each entry re-hashed. A changed entry loses approval and is
  refreshed in the background. No watcher.
- **Remove.** Through Avibe: remove the entry from `.mcp.json` (D5 write rules)
  and delete its cache directory. Removed by hand: dropped at the next staleness
  check, cache deleted then.
- **Projects.** Archived Project → its folder resolves like any other directory
  (D1), so its cache key falls back to `dir-`; the `proj_` cache is kept.
  Folder moved → `proj_` cache kept (keyed by id); a moved `dir-` root starts a
  new cache. Missing folder → no project servers. A `proj_` cache whose Project
  row no longer exists is an orphan the discover pass deletes; `dir-` caches
  are not cleaned automatically (small, and the folder may come back).

## 9. Surfaces

- **Web.** Skills & MCP page gets a Global / Project switch (reuse the Skills
  project-scope pattern and frames). Project view lists `.mcp.json` servers with
  state badges; a project server that shadows a global one shows "overrides
  global". Actions: Approve, Configure secrets (key/value editor writing
  `secrets.json`, showing placeholder names), Login, Remove. The two-tab Add
  dialog targets the current view's scope. Design frames under
  `ui/design/frames/skills/`; all copy through i18n.
- **CLI.** As §6.

## 10. Security

- Scope separation, not isolation: Agents run as the same OS user and could read
  `~/.avibe` or pass another `sessionId`. The guarantee is that prompt, CLI,
  Web and calls present only the caller's scope.
- No repository-defined server starts or connects before Owner approval (D3).
- Secrets never written into the project folder (D5) or into argv (D4); cache
  files `0600`.
- Native backends must not load `.mcp.json`: Claude runs with
  `--strict-mcp-config` and no `--mcp-config` (`vibe/claude_config.py`); Codex
  and OpenCode do not read `.mcp.json`.

## 11. Persisted shapes

New paths only. Global files unchanged; no migration. Every new file carries a
version field; unreadable files degrade per §4.

## 12. Resolved owner decisions (2026-11-03)

1. Write-back: Avibe rewrites `.mcp.json` atomically on add/edit/remove (D5).
2. Placeholders: one namespace (D4).
3. Project identity follows project Skills, including IM channels (D1).
