# Memory Beta: Settings Entry, User Isolation, and Agent Writes

Spec for [issue #983](https://github.com/avibe-bot/avibe/issues/983), the
product-direction follow-up to #979. This document freezes the design before
implementation. The guiding constraint is simplicity: reuse existing seams,
add no new frameworks, and keep the diff as small as the requirements allow.

Revision note: this version incorporates review findings on the sidecar
single-owner contract, Workbench identity propagation, read scoping,
`remember` failure semantics, and principal derivation.

## Background

The current Memory implementation (branch `memory-plugin-docs` lineage) has:

- a provider-independent core (`core/memory/module.py`) with a local SQLite
  queue (`core/memory/store.py`) drained by a worker into an EverOS sidecar
  (`core/memory/everos.py`, `core/memory/process.py`)
- two independent capture paths: IM in
  `core/handlers/message_handler.py` (`capture_memory_from_im`, admin-DM-only)
  and Workbench in `vibe/ui_server.py`
  (`_handoff_workbench_memory_capture`)
- one install-wide principal: `memory_meta` is a singleton row whose
  `principal_id` is bound as the sidecar's owner at process start
  (`core/memory/runtime.py:233`); the sidecar rejects any request whose
  sender/user id differs from that owner (`core/memory/sidecar.py`,
  `_validate_add` / `_validate_search`) — a single global pool with no
  per-user dimension and no provenance field
- a `/memory` slash command surface across IM and Workbench
  (`core/memory/commands.py`, `controller.handle_memory_command`,
  `ui_server` interception)
- a Workbench `/memory` page carrying status/profile/search **and** the
  enable + endpoint configuration UI
  (`ui/src/components/workbench/MemoryPage.tsx`)
- a read-only `vibe memory` CLI (`status`/`profile`/`search`) gated by a
  session-bound capability token (`core/memory/cli_access.py`); the
  internal read gate currently fails open — requests with no capability
  headers are allowed (`core/internal_server.py`, `_memory_read_denied`)
- lazy installation that is already mostly correct: `memory-runtime` is not
  in `reconcile_startup_dependencies()`, and `MemoryRuntime.reconcile()`
  resolves without downloading

## Goal

1. Memory Beta lives entirely under Settings; Workbench navigation drops it.
2. Lazy installation stays strict and gains a regression guard.
3. One capture boundary in core covers Workbench and all IM platforms, for
   all session users, stored per user — not one administrator pool.
4. `/memory` is deleted everywhere.
5. Agents get one explicit, session-scoped write operation via the CLI.

Non-goals (per issue): authorization/consent policy, group or workspace
memory, cross-platform identity linking, agent-driven configuration or
destructive operations, provider-side provenance (kept local this
iteration), IM attachment capture.

## Design

### 1. Settings entry (Workbench exit)

**Decision: one Settings page with staged rendering; the Settings nav item
appears only when Memory is fully set up; before that the page is reachable
from the `memory-runtime` dependency row.**

Remove:

- Workbench sidebar item (`ui/src/components/workbench/WorkbenchSidebar.tsx`)
- mobile capability tab (`ui/src/components/workbench/CapabilityTabs.tsx`)
- `/memory` route and `MemoryPage` import in `ui/src/App.tsx`

Add `SettingsMemoryPage` at route `/settings/memory`, built by moving the
existing `MemoryPage` tab components (settings, status, profile, search)
rather than rewriting them. The page renders by state:

| State | Nav item | Page content |
|---|---|---|
| runtime not installed | hidden | pointer back to Settings → Dependencies (install lives there, unchanged) |
| installed, not configured/enabled | hidden | provider endpoint form + explicit Enable toggle (the current settings tab) |
| installed + configured + enabled | visible ("Memory · Beta") | full management: settings, status, profile, search |

The `memory-runtime` row in `SettingsDependenciesPage.tsx` gains a
"Configure" link to `/settings/memory` once installed. Because the route
always exists and Dependencies always links to it, there is no circular
setup flow even while the nav item is hidden.

**Read scoping for this page: owner-local-only, fixed to `avibe:local`.**
The Memory browser routes are guarded by
`is_direct_loopback_memory_request` (`vibe/ui_server.py:1284`), which
rejects proxying, Docker bridges, LAN setup hosts, and remote-access
cookies — so Settings only ever has one possible viewer: the local owner.
The page's profile/search therefore read the fixed local-owner identity
`avibe:local`; there is no per-viewer scoping to build. Remote Workbench
users read their own memory through `vibe memory profile`/`search` inside
their agent sessions (section 3c). The browser never sends — and cannot
send — a user selector.

### 2. Strict lazy installation

**Decision: no behavioral change; add guards so it stays true.**

Current behavior already satisfies the requirement: startup reconciliation
excludes `memory-runtime`, `MemoryRuntime.reconcile()` only calls
`resolve_python()` (no download), and downloads happen solely inside the
explicit install job (`vibe/api.py:_prepare_memory_runtime_job` →
`runtime.install_artifact()`).

Work items:

- add a regression test asserting that controller startup with Memory
  disabled — and with Memory enabled but the runtime missing — performs zero
  artifact downloads (assert `MemoryArtifactManager.ensure` is never called
  outside the install job path)
- verify the wheel/sdist carry no runtime archive (manifest only); assert in
  the existing packaging test if one covers bundled assets
- installing the runtime must not flip `memory.enabled`; this is already the
  case — cover it in the same test

### 3. One capture boundary, per-user storage

This is the substantive change, and it ships as one vertical slice
(milestone 1): capture unification, principal derivation, sidecar
multi-principal support, and scoped reads are interdependent and are not
independently useful.

#### 3a. Capture boundary

**Decision: the single injection point is the existing capture site in
`MessageHandler.handle_message` (`core/handlers/message_handler.py:207`),
which already runs after `get_session_info()` resolves the session and
before dispatch. The Workbench path in `ui_server` is deleted, not unified
into a new abstraction.**

This works because Workbench turns already flow through the same core path:
`ui_server` → `/internal/dispatch*` → `dispatch_turn()` →
`MessageHandler.handle_user_message`.

**Workbench identity propagation (prerequisite):** today the Workbench
dispatch payload carries neither `user_id` nor `message_id`, so
`_build_workbench_context` falls back to `user_id="workbench"` — which
would collapse every browser user into one shared identity. The context
builder already reads both fields from the payload
(`core/internal_server.py:934-938`); the fix is confined to the sender:
`ui_server` resolves the Workbench identity server-side per request —
direct-loopback owner sessions resolve to `local`, remote-access users to
their `web_push_user_key` — and includes it as `user_id`, plus the
persisted user-row id as `message_id`, in `dispatch_payload`. The local
owner thus captures under `avibe:local`, matching what the Settings page
reads (section 1). Capture requires a real resolved identity — the
`"workbench"` fallback (and any empty identity) is never captured.

**Idempotency keys:** IM unchanged (`im:{platform}:{native_message_id}`);
Workbench uses `workbench:{message_row_id}` — stable, and available once
`message_id` is propagated.

**Busy sessions: flushed human turns ARE captured, and the queue merges
per user.** When a session is busy, `SessionTurnManager.submit()`
(`core/session_turns.py:719`) enqueues the message and returns without ever
reaching `MessageHandler` — only the later flush does. Excluding flushed
turns would therefore permanently drop every message sent to a busy
session. Instead:

- flushed human turns go through the same MessageHandler capture site as
  direct dispatches — still exactly one injection point
- the queue merges only **consecutive messages of the same user**; segments
  never span users (today the merge deliberately drops the owner when
  browser users mix — that case no longer arises)
- the flush writes the segment's `user_id`, a stable `message_id` (the last
  merged row's id), and the ordinary-human marker back into the dispatched
  context, so capture sees a fully attributed message
- one merged same-user segment is one capture. This granularity is
  accepted; no per-original-message pending-capture metadata is introduced.

Admission (`memory_im_admitted` → `memory_capture_admitted`):

- turn source is human, including flushed human queue turns
  (scheduled/harness turns never reach this branch)
- platform is `avibe` (Workbench) or an IM platform in the existing DM
  whitelist; group/channel messages remain out of scope
- Memory is enabled
- **drop the `is_admin` requirement** — every resolved session user is
  eligible
- a real user identity resolved (see above); otherwise skip silently

Deletions in `vibe/ui_server.py`: `_handoff_workbench_memory_capture`,
`_workbench_memory_capture_is_eligible`, and the capture-related use of the
`_memory_cli_admitted` message metadata (the CLI-capability plumbing itself
stays — it serves section 5).

**Attachments:** `context.files` carries generic `FileAttachment`
(`local_path`/`url`/`content`), while capture needs `CaptureAttachment`
(`kind`/`name`/`uri`/`ext`); the safe conversion (path validation + MIME
mapping) currently lives in `ui_server`. That one conversion function moves
into `core/memory/`, unchanged in behavior. This iteration captures only
Workbench attachments already written to local disk under the controlled
attachment directory; IM attachments are not captured, and no download
pipeline is built.

#### 3b. Per-user principals, derived — no mapping table

**Decision: principals are derived by keyed hash from the existing
`memory_meta.scope_key`; there is no principal table, no lazy row creation,
and raw platform user ids are never persisted in the Memory store.**

```
user_key     = f"{platform}:{context.user_id}"        # e.g. slack:U123, avibe:<wb-user>
principal_id = "u-" + HMAC_SHA256(meta.scope_key, user_key).hex()[:32]
```

- the queue (`memory_capture_queue`) stores `principal_id` and
  `provenance` (`'user_input'` / `'agent'`) per row; provenance survives
  tombstoning (only payload text is erased)
- `CaptureRequest` (`core/memory/types.py`) gains `principal_id` and
  `provenance`; the worker passes the per-row `principal_id` to EverOS as
  the sender/user id
- the same human on two platforms owns two separate memories; linking them
  is explicitly out of scope this iteration

**Schema and dev-state reset:** Memory has never shipped, so the initial
schema migration (`core/memory/migrations/0001_initial.sql`) is edited in
place — no new migration in the chain, no compatibility code, and **no
automatic destructive reset in the startup path** (auto-detecting a legacy
schema and deleting files would itself be compatibility logic, contradicting
the premise). Pre-existing local dev state is reset explicitly, once: delete
the whole Memory state directory (queue DB + EverOS provider root) by hand
before running the new code — the Settings clear flow is not a substitute,
as it only deletes queue rows and empties the provider root without
dropping the SQLite file or re-running the changed initial schema. Fresh
test environments simply start from an empty directory. The legacy
singleton `principal_id` disappears with the reset.

**Sidecar multi-principal contract:** the sidecar's current validators
reject any sender/user id that differs from the single owner bound at
process start. That changes to:

- `EverOSProcess` no longer receives an `owner_id`
- all three request validators — `_validate_add`, `_validate_search`, and
  `_validate_get` (`core/memory/sidecar.py:196`, which also binds the owner
  today) — accept any principal matching `^u-[0-9a-f]{32}$`: strict shape
  validation replaces owner equality. The UDS is already private to the
  controller, so the owner check was defense-in-depth; the shape check
  preserves its value of rejecting arbitrary or legacy identifiers while
  allowing an open set of controller-derived principals
- `search`/`profile` filter by the requested principal, per request

The isolation guard tests must exercise the **real sidecar validators**
with two distinct principals (add + search + get), not only the stub
provider — a stub-level pass would have hidden the 403 this contract
change exists to prevent.

#### 3c. Scoped reads everywhere

**Decision: every internal read carries an explicit principal scope;
requests without one are refused. The current fail-open (no capability
headers → global read) is removed.**

`/internal/memory/profile` and `/internal/memory/search` resolve their
scope from exactly one of two trusted carriers:

1. **Capability token** (CLI, agents): `MemoryCliAccessRegistry` binds each
   token to the pair `(session_id, principal_id)` at issuance time (the
   controller knows the admitting context). Validation returns the bound
   principal, and the endpoint queries only it. If a session's resolved
   principal ever changes, the token is rotated — a token issued for the
   old principal does not validate. No request parameter selects a user —
   there is nothing for a caller to override. This is how remote Workbench
   users read their own memory: `vibe memory profile`/`search` inside their
   agent sessions.
2. **Server-resolved Workbench identity** (Settings page): `ui_server`
   passes the server-resolved `user_key` (always `avibe:local` for
   Settings, per section 1) over the UDS in an internal header plus an HMAC
   proof bound to the HTTP method, path, and user key. The launcher delivers
   the proof secret once to the UI and controller processes without placing
   it in agent-inherited environment variables. A user-key header without a
   valid proof is refused, including when the caller also holds another
   user's capability. The **controller** derives the principal using
   `scope_key`, so the UI process never touches the Memory store. The
   browser-facing `/api/memory/*` routes accept no user parameter and keep
   their existing `is_direct_loopback_memory_request` admission.

Requests presenting neither carrier get 403. `status` remains unscoped
(install-level operational state, no memory content).

Two-user isolation is accepted at this layer — the capability/internal
endpoint tests in the test plan — not by opening Settings as two different
users, which the loopback-only admission makes impossible by design.

### 4. Remove `/memory`

**Decision: full deletion, no replacement, no slash-command framework.**

Delete:

- `core/memory/commands.py` (`parse_memory_command`,
  `is_memory_command_candidate`) and all call sites, including the
  capture-path guards that excluded `/memory` text from capture (once the
  command no longer exists, such text is ordinary user input)
- IM side: `Controller.handle_memory_command`, `_memory_command_text`,
  `_send_memory_inert_reply`, and the `"memory"` entry in the controller
  command registry (`core/controller.py:737`)
- Workbench side: the interception in `ui_server.py` (~line 8046),
  `_workbench_memory_command_response`,
  `_workbench_memory_command_is_text_only`, and `_memory_command_result`.
  **`is_direct_loopback_memory_request` (`vibe/ui_server.py:1284`) is NOT
  deleted** — it also guards every Memory Settings/API browser route
  (settings, status, profile, search, clear) and the capability admission,
  all of which survive this change. Only its `/memory`-interception call
  site goes away.
- i18n: `memory.command.*` keys in `ui/src/i18n/en.json` / `zh.json` and
  backend equivalents
- docs: `/memory` references in `docs/COMMANDS.md`, `docs/COMMANDS_ZH.md`,
  `docs/SLACK_SETUP.md` (including the Slack native slash-command entry)
- tests: command flows in `tests/test_memory_slice3.py`,
  `tests/test_ui_memory_routes.py`, `ui/src/lib/memoryCommandResult.test.ts`

Remaining direct user surfaces: the Settings UI (section 1) and the
`vibe memory` CLI.

### 5. Agent-initiated write

**Decision: the frozen contract is a CLI subcommand only —
`vibe memory remember` — not a registered agent tool, and not both.**

Rationale: all three backends (OpenCode, Claude Code, Codex) already run
CLI commands, the capability-token authorization chain exists, and a
registered-tool surface would require per-backend tool plumbing that this
iteration does not need (YAGNI). If a native tool surface is wanted later it
can wrap the same internal endpoint.

Contract (frozen):

```
vibe memory remember "<text>"
```

- **scope**: the principal is resolved from the caller's capability token,
  same chain as `profile`/`search` (section 3c). There is no user
  parameter.
- **provenance**: enqueued with `provenance="agent"`; automatic capture
  uses `"user_input"`. Provenance is recorded and persisted **locally** on
  the queue row (surviving tombstoning) and is testable there. It is NOT
  forwarded into the EverOS add payload this iteration — the provider
  message schema stays exactly `sender_id`/`role`/`timestamp`/`content`
  and the sidecar's exact-key validation is unchanged. Provider-side
  provenance is future work requiring a sidecar schema change.
- **idempotency**: `source_message_id = "agent:{session_id}:{sha256(text)}"`;
  the store's existing digest-based dedup makes replays of identical text in
  the same session no-ops (reported as `duplicate`).
- **size limit**: text ≤ 4,000 characters; over-limit input is rejected
  before enqueue.
- **failure behavior (outcome-based, not an enumerated list)**: the CLI
  exits 0 **iff** the capture outcome is `accepted` or `duplicate` —
  "accepted into the local queue", not "persisted to provider"; provider
  flush stays asynchronous, consistent with automatic capture. Every other
  closed outcome exits non-zero with its category printed: Memory
  disabled, invalid/expired capability, invalid/over-limit input,
  `memory_queue_full`, `memory_low_disk_space`, clear in progress,
  `memory_store_unavailable`. Agents must not retry on exit 0 and should
  treat non-zero as "not stored" without inventing their own retry loops.
- **untrusted recall**: unchanged — recalled content is data, not
  instructions; the existing `_MEMORY_CLI_PROMPT` guidance stays and is
  extended to cover `remember`.
- **exclusions**: no clear, no export, no delete, no configuration through
  this surface. The prompt's existing prohibition is narrowed accordingly
  (capture via `remember` is now allowed; the rest stays forbidden).

Implementation: a new `remember` subcommand in `vibe/cli.py` posting to a
new `/internal/memory/remember` handler (thin wrapper over
`MemoryModule.capture` with `provenance="agent"`), plus the prompt update in
`core/system_prompt_injection.py`. Admission changes, per surface:

- **IM**: `memory_cli_prompt_admitted` drops its admin-only condition —
  any human session user with Memory enabled gets the prompt and a token.
- **Workbench**: the `avibe` branch of prompt admission requires
  `memory_cli_admitted`, which `ui_server` today sets only from
  `is_direct_loopback_memory_request()` (`vibe/ui_server.py:8008`) — so
  remote Workbench users would never get a CLI capability, contradicting
  section 3c's "remote users read via `vibe memory`". Fix: `ui_server`
  sets `memory_cli_admitted=True` for the direct-loopback owner **and**
  for requests authenticated by the existing remote-access cookie
  middleware that carry a stable subject (which also supplies the
  `user_key`). Unauthenticated remote requests, and authenticated ones
  without a stable subject, stay `false`.

## Test plan

- **unit**: principal derivation (stable, distinct per user_key, correct
  shape); provenance stamping and persistence across tombstoning for both
  paths; `remember` outcome→exit-code mapping across all closed outcomes;
  idempotency and size limit; admission matrix for
  `memory_capture_admitted` (human/scheduled/flushed × platform × enabled
  × identity present); Workbench CLI-capability admission: direct-loopback
  owner and authenticated remote-access user with a stable subject are
  admitted, unauthenticated remote and subject-less requests are not
- **isolation** (acceptance-critical, two layers):
  - internal-endpoint level with a stubbed provider: two sessions with
    different users; each `profile`/`search` returns only its own data;
    requests with no scope carrier get 403
  - **real sidecar validators**: add + search + get requests with two
    distinct derived principals pass shape validation and stay partitioned,
    covering `_validate_add`, `_validate_search`, and `_validate_get`
    directly (profile is not a distinct sidecar request — it goes through
    `/memory/search` — so its isolation is covered by the
    internal-endpoint/provider layer above); a non-conforming principal is
    rejected
- **workbench identity and queueing**: dispatch payload carries
  `user_id`/`message_id`; the `"workbench"` fallback identity is never
  captured; a busy-session message is captured exactly once via its flush;
  the queue never merges messages from different users, and a merged
  same-user segment yields one capture attributed to that user
- **lazy install**: startup-performs-no-download guard (section 2)
- **removal**: grep-level assertions that no `memory.command.*` i18n keys or
  `/memory` handling remain; deleted tests removed rather than skipped
- **UI**: `cd ui && npm run build`; staged-rendering states of
  `SettingsMemoryPage` covered by existing component-test patterns
- **Settings read proof**: a caller holding user B's valid capability but
  forging `X-Avibe-Memory-User-Key: avibe:local` receives 403; valid proofs
  are bound to method/path/user-key and the proof secret is not present in
  the child process environment
- **manual (Incus regression)**: Workbench capture without the admin flag,
  IM DM capture as a non-admin user, the local owner's Settings page
  showing only `avibe:local` memory, a second (IM) user reading only their
  own data via `vibe memory` in an agent session, `/memory` typed into IM
  behaving as plain text, install → configure → enable flow from a clean
  state

## Milestones

1. **User isolation, one vertical slice** — interdependent and shipped
   together (reviewable as a stacked series, but only useful atomically):
   schema update in place + explicit dev-state reset, principal
   derivation, Workbench identity propagation, capture unification (delete
   the ui_server path, attachment converter moved to core, per-user queue
   merging with flush-side attribution), sidecar multi-principal
   validation across all three validators, scoped reads (fail-closed gate,
   `(session_id, principal_id)`-bound tokens, server-resolved `avibe:local`
   for Settings, Workbench `memory_cli_admitted` extended to authenticated
   remote-access users with a stable subject), and the two-layer isolation
   tests.
2. **Agent write**: `remember` subcommand, `/internal/memory/remember`,
   prompt update, outcome→exit-code tests.
3. **`/memory` removal**: code, i18n, docs, tests.
4. **UI move**: Workbench exit, `SettingsMemoryPage`, dependency-row link.
5. **Guards**: lazy-install regression test, Incus regression pass.

Milestones 2–5 are independent of each other and only depend on 1.
