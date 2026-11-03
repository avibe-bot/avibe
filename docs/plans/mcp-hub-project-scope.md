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
  with neither, the working directory itself. Reuse `_project_directories` as
  is: absolute `project_base` only, `PROJECT_ROOT_MAX_DIRECTORIES` bound, and
  unreadable paths degrade to the working directory. The project config is
  `<project root>/.mcp.json`. So Web Project Sessions, IM channels whose workdir
  is a project folder, background and forked Sessions all get the same answer
  Skills gives them. The cache key (`<key>` below) is the Avibe Project id
  (`proj_<hex>`) when the root is a Project's folder
  (`projects_service._find_project_by_workdir`; archived included — archive
  changes nothing here), otherwise `dir-<sha256(realpath(root))[:12]>`. A root
  with no `.mcp.json` → global only.
- **D2 Visibility.** Project Session sees global ∪ own Project; same name →
  the Project server shadows the global one. Other Projects are never visible.
- **D3 Approval.** Every server defined by a repository `.mcp.json`, stdio and
  HTTP, is `pending_approval` until an Owner approves it: it does not start,
  connect, refresh or advertise, and cannot be called. Approval binds to the
  fingerprint (canonical JSON) of the **raw** entry, before placeholder
  substitution — secrets never enter it and rotating a secret does not need
  re-approval. Any change to the raw entry needs approval again. The record is
  one file holding the fingerprint; no version, timestamp or author fields.
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
    ├── secrets.json                   # {"env": {...}, "headers": {...}}; env doubles as placeholder variables
    ├── approval.json                  # {"fingerprint": "..."} of the raw .mcp.json entry (D3)
    ├── oauth.json                     # same shape as global (mcp-hub-oauth.md §6)
    ├── current.json                   # published revision pointer
    └── revisions/<rev>/               # merged definition / manifest / tools / commands
```

- Parsing reuses `definition_from_provider` unchanged (so client-specific keys
  stay ignored): placeholders are resolved in the raw entry first, then the
  parser runs; an unsubstituted placeholder never reaches it.
- Writes to `.mcp.json` (D5) reuse `native.py`'s compare-and-replace atomic
  write and preserve other servers and unknown top-level and per-server keys; a
  changed file is re-read, never overwritten from stale memory.
- Unreadable or invalid `.mcp.json` or cache files: that entry is skipped with a
  warning; prompt, CLI and startup never fail. One boundary catch, no
  per-file enumeration; a bad file is never overwritten by Avibe.

## 5. Effective definition (load-time merge)

For each raw `.mcp.json` entry:

1. Substitute `${VAR}` in D4 fields from `secrets.env`; a missing variable
   makes the entry show as `needs_config` with the missing names — derived on
   load, not a persisted state.
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
  loop. One asymmetric call site must keep its value as-is:
  `modules/agents/opencode/agent.py` (~1858) passes a locally computed
  `project_base`, not `managed_skill_project_base(context)`, and gates
  `skills_cwd` on `binding_bound`; MCP reuses exactly those values so the two
  features stay aligned. Forks and `vibe agent run` children inherit the
  working directory and project base exactly as they do for Skills.
- **CLI.** All reads (`--list`, `<server> --list`, `--help`, detail) move to IPC
  and carry the same working directory and project base the CLI already gets for
  Skills (`AVIBE_SKILL_WORKING_DIR`, `AVIBE_SKILL_PROJECT_BASE`, else the process
  cwd) plus `AVIBE_SESSION_ID`; the Controller applies D1. A user terminal gets
  the project of its own cwd, as `vibe skill list` does. Controller down →
  `unavailable`, no second offline read path. Management on a shadowed name
  targets the visible (project) server; `--global` targets the global one.
  Mutations reuse the CLI's existing Owner authentication — one rule for all
  scopes; calling an approved server creates its runtime cache as usual.
- **Call and management contract.** `vibe mcp <server> <tool>` sends
  `workingDirectory` and `projectBase` (filled as above) plus `sessionId`. The
  Controller derives the scope with D1 and resolves the name in that visible
  set before any connection; outside it → `not_found`. Missing inputs → global
  scope. Every management operation (add, `--login`, `--logout`, `--refresh`,
  `--remove`, `--approve`, configure secrets) carries the same two inputs and is
  validated the same way. Scope ids are never client-supplied: the Controller
  derives the scope itself from those inputs.
- **Web.** `/api/mcp*` take `project=<proj id>` (Web manages Avibe Projects;
  `dir-` keys are reachable from Sessions and CLI only). Reads check
  `project_access_service.can_read_project`; mutations (add, remove, refresh,
  approve, configure secrets, login, logout) are Owner-only, go over IPC, and
  the Controller re-checks the Project. Reads use the project scope root, not
  the global root.

## 7. Storage and runtime keys

- `repository` keeps its module functions. `mcp_root()` / `package_dir()` and
  the functions built on them take the scope root (`~/.avibe/mcp` for global,
  `~/.avibe/project_mcp/<key>` for a project), defaulting to the global root so
  existing global callers are unchanged. No new Store class. `_stale` cleanup
  and `_admit` run against the scope's root, so project packages are never
  treated as global leftovers.
- One key type `ScopedName(scope, name)` replaces `name` in package locks,
  connection keys (`(scope, name, revision, session)`, including the `("open",)`
  lock), `_oauth_tasks`, `_oauth_providers`, `_oauth_provider_locks`, and the
  `_pending_logins` cleanup loops.
- OAuth: the pending record stores `ScopedName`; the callback is still found by
  `state` alone (unique, one-time), so the redirect URI and callback route are
  unchanged. Pending state is in memory; after a restart the user starts login
  again (current behavior). Logout routes by `ScopedName`.

## 8. Lifecycle

- **Loading.** On demand, when a caller's scope first resolves. No periodic
  per-Project pass and no pre-discovery of all Projects. Concurrency is the
  existing per-package lock under the package `ScopedName`; no second
  coordinator. File and DB work stays off the Controller loop
  (`asyncio.to_thread`).
- **Staleness.** On resolve, the current `.mcp.json` is read in a thread and
  compared entry-by-entry against the cached fingerprints; unchanged entries
  keep their approval and publication, changed or new entries need approval
  again (D3), removed entries become invisible immediately. No watcher, no
  mtime fast path.
- **Remove.** Through Avibe: remove the entry from `.mcp.json` (D5 write rules)
  and delete its cache directory. Removed by hand: dropped at the next staleness
  check, cache deleted then.
- **Projects.** Archive changes nothing: the folder still resolves to its
  `proj_` cache and approvals. A `proj_` cache whose Project row no longer
  exists stays on disk unused (harmless, private); removing it is not a
  feature. Removing a server through Avibe deletes its cache directory
  together with the `.mcp.json` entry.

## 9. Surfaces

- **Web.** Skills & MCP page gets a Global / Project switch (reuse the Skills
  project-scope pattern and frames). Project view lists `.mcp.json` servers with
  state badges and their scope. Actions: Approve, Configure secrets (key/value editor writing
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

## 11. Reuse map (binding for implementation)

Build on these; add a new mechanism only where this table has no entry.

| Need | Reuse | Change needed |
|---|---|---|
| Project root | `core/managed_skills.py` `_project_directories` (+ `_working_directory`, `managed_skill_project_base`) | expose the root it already computes; no second walk |
| Session/CLI inputs | `skills_cwd` / `skills_project_base` already passed to `build_system_prompt_blocks`; CLI env `AVIBE_SKILL_WORKING_DIR` / `AVIBE_SKILL_PROJECT_BASE` from `managed_skill_environment` | pass them on; no new env vars, no DB lookup |
| Project id from folder | `storage/projects_service._find_project_by_workdir` | none |
| Web project picker and access | `ui/.../skills/ProjectPicker.tsx`; `project_access_service.can_read_project` / `can_manage_project` | none |
| Package store | `core/mcp_hub/repository.py` (publish, read_published, summarize, list, rollback) | `mcp_root()` / `package_dir()` take the scope root; same functions for both scopes |
| Parse `.mcp.json` entries | `definition.definition_from_provider` | none (placeholders substituted first) |
| Relative `cwd` | `native._resolve_cwd` | none |
| Atomic compare-and-replace write | `config.atomic_io.write_atomic(..., before_replace=...)` as `native.py` uses it | none |
| Discover, refresh, publish, connections, call, needs_login | `core/mcp_hub/runtime.py` existing paths and per-package locks | key by `ScopedName` instead of `name` |
| OAuth storage, provider, callback | `core/mcp_hub/oauth.py` `AvibeTokenStorage`, `read_state`, the start link and callback route | storage path from the scope's package dir; pending record keeps `ScopedName` |
| Prompt rows and paging | `core/mcp_hub/prompt.py` `build_mcp_prompt_blocks`, `catalog_page_of` | add the scope inputs |
| CLI and Web transport | `vibe/internal_client.mcp_request`, `vibe/mcp_api.run`, `_mcp_route` | add scope inputs to payloads |
| Secrets editor UI | `McpAddForm` key/value rows (env/headers) | reuse for Configure secrets |
| Add dialog | `McpAddDialog` two tabs | target the current scope |
| Confirm approve | existing `confirm-dialog` primitive | none |
| Native MCP off in backends | `vibe/claude_config.py` `--strict-mcp-config` | none |

## 12. Persisted shapes

New paths only. Global files unchanged; no migration. New files use the shapes
of §4 and D3 (a `secrets.json` of env/headers, a fingerprint file); no version
fields — the format is defined by this release and evolved by safe degradation
when a real change comes. Unreadable files degrade per §4.

## 13. Resolved owner decisions (2026-11-03)

Round-3 trim (both reviewers, folded): drop per-file version fields, the
`--projects`/`--remove-project-cache` tooling, the archived-Project fallback
branch, per-root server-count bounds, and the dedicated single-flight
coordinator; reuse the existing package locks, atomic write, Owner
authentication and project access service instead. `dir-` scopes stay (D1 is
the Skills rule; astra) with no special authority tier (astra).

1. Write-back: Avibe rewrites `.mcp.json` atomically on add/edit/remove (D5).
2. Placeholders: one namespace (D4).
3. Project identity follows project Skills, including IM channels (D1).
