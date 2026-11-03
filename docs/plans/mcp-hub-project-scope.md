# MCP Hub project scope

> Status: draft v2, owner decisions 2026-11-03, review round 1 (advisor, codex-astra)
> folded in. Extends `docs/MCP_HUB.md`, `docs/plans/mcp-hub-mvp.md`,
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
- Walking a non-Project cwd upward for `.mcp.json`.
- Security isolation between Projects of the same OS user (§10).
- Importing other native project configs (`opencode.json`, Codex project config,
  Claude's per-path entries in `~/.claude.json`).
- A file watcher, or a Project delete flow (Projects only archive today).

## 3. Decisions

- **D1 Project identity.** A Project is an Avibe Project: `scopes` row with
  `scope_type='project'`, id `proj_<hex>`, folder in `scope_settings.workdir`.
  A Session's Project comes from its run target `scope_id`. *(Open: §12.3 adds a
  workdir fallback for IM scopes.)* No Project → global only.
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
  secrets in argv). It is a "secret needed here" marker, resolved only from that
  Project's cache, never from the process environment. Unresolved →
  `needs_config` listing the missing names. *(Namespace: §12.2.)*
- **D5 Values added through Avibe.** Adding or editing a project server through
  Avibe writes only `type`/`command`/`args`/`url`/`cwd` to `.mcp.json`; every env
  and header value goes to the cache. Non-secret values a team wants in the
  repository are written into `.mcp.json` by hand. *(Write-back: §12.1.)*
- **D6 Global unchanged.**

## 4. Files

```
<project folder>/.mcp.json             # repository-owned, Claude Code format, no secrets
~/.avibe/project_mcp/<project_id>/     # private cache (0700 dirs, 0600 files)
└── <name>/
    ├── secrets.json                   # {"env": {...}, "headers": {...}}
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

1. Substitute `${VAR}` in D4 fields from the cache (§12.2). Missing →
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
Scope = "global" | "project:<project_id>"
visible(scope_ids: list[Scope]) -> {name: ScopedServer(scope, name, store)}
caller_scopes(project_id | None) -> ["global"] or ["global", "project:<id>"]
```

Project wins on name. Prompt advertising counts only `ok` packages (as today);
listing shows every state (`pending_approval`, `needs_config`, `needs_login`,
`ok`, refresh failure).

Consumers, none of which decide scope on their own:

- **Prompt.** `build_mcp_prompt_blocks(project_id)`. The project id comes from
  the run target already on the `MessageContext`
  (`core/services/agent_run_target.py`, same source Skills uses through
  `managed_skill_project_base`), not from a new DB query on the loop. This covers
  Vibey, Claude, Codex, OpenCode, IM, background, and forked Sessions; forks and
  `vibe agent run` children keep their persisted `scope_id`.
- **CLI.** All reads (`--list`, `<server> --list`, `--help`, detail) move to IPC
  with the caller's `AVIBE_SESSION_ID`; the Controller maps Session → Project.
  No Session (a user terminal) → global; `--project <id>` selects a Project for
  Owners. Controller down → `unavailable`, no second offline read path.
  Management on a shadowed name (`--login`, `--logout`, `--refresh`, `--remove`,
  `--approve`) targets the visible (project) server; `--global` targets the
  global one.
- **Call.** Admission resolves the name in the caller's visible set before any
  connection; outside it → `not_found`.
- **Web.** `/api/mcp*` take `project=<id>`. Reads check
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

- **Discovery.** The existing discover pass also covers non-archived Projects
  that have a folder. It runs off the loop (`asyncio.to_thread` for file and DB
  work), one Project at a time, with a bounded per-Project server count.
- **Staleness.** When the resolver serves a Project caller, a bounded
  `stat(mtime, size)` of `.mcp.json` runs in a thread; only on change is the
  file read and each entry re-hashed. A changed entry loses approval and is
  refreshed in the background. No watcher.
- **Remove.** Through Avibe: remove the entry from `.mcp.json` (D5 write rules)
  and delete its cache directory. Removed by hand: dropped at the next staleness
  check, cache deleted then.
- **Projects.** Archived Project → its Sessions see global only; cache kept.
  Folder moved → cache kept (keyed by id); missing folder → no project servers.
  A Project row that no longer exists → its cache is an orphan and the discover
  pass deletes it.

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

## 12. Owner decisions pending

1. **Write-back.** D5 (Avibe rewrites `.mcp.json` on add/edit/remove, with §4
   safeguards) vs read-only `.mcp.json` with Avibe-added project servers living
   in the cache only.
2. **Placeholder namespace.** One map (`secrets.env` doubles as "variables" for
   placeholders anywhere) vs per field (env placeholders from `secrets.env`,
   header placeholders from `secrets.headers`).
3. **IM scopes.** Keep D1 strict, or add a fallback: when the run target scope is
   not a Project, find a Project whose folder equals the Session workdir
   (`projects_service._find_project_by_workdir`), matching how Skills follows
   the folder.
