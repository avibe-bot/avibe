# EverOS 1.2.1 Upgrade + project_id Integration

Status: implemented
Date: 2026-07-29

## Background

Memory currently pins `everos==1.1.3` (upstream EverMind-AI/EverMemOS). Upstream
has since shipped 1.1.4 (reliability/security fixes on paths we use: `/flush`
LLM-output retries, cascade index reliability, empty-embedding poisoning fix),
1.2.0 (`/api/v2` prefix, opt-in OpenTelemetry), and 1.2.1 (optional
embedding/rerank tiers, LanceDB schema v2, `ProviderNotConfiguredError` -> HTTP
422 `PROVIDER_NOT_CONFIGURED`).

The adapter also hardcodes `project_id="personal"`, so EverOS's
`(app_id, project_id, user_id)` isolation triple is collapsed to a single
project for all memories.

## Decisions (confirmed with owner)

- Memory launches on EverOS 1.2.1 as its initial state. The implementation
  assumes a new database and defines one initial schema; there is no data
  migration or cross-version provider-root compatibility work in scope.
- Target `everos==1.2.1` directly (skip 1.1.4 stopover).
- Move the adapter and sidecar guard to `/api/v2` routes.
- Wire `project_id` for real this version: **project = the Agent Session's
  working directory**, reported to the provider as an HMAC digest (same
  keyed-digest scheme as `derive_principal_id`), never as a raw path.
- Project identity is **fixed per Agent Session**: a Session owns its cwd and
  the stored `agent_sessions.workdir` is effectively write-once
  (`storage/agent_session_rows.py`); scope cwd changes only seed new sessions.
  Memory must read the session's stored workdir, never re-resolve from Scope.
- Not adopted this version: capability tiers (optional embedding), OpenTelemetry
  tracing, `cascade backfill` CLI. Avibe keeps requiring full LLM + embedding
  configuration.

## Phase 1 — Version bump

1. `scripts/memory_runtime/pyproject.toml`: `everos==1.2.1`; regenerate
   `uv.lock` with uv 0.9.18 (1.2.1 adds a direct `click>=8.1` dependency).
2. Centralize the version: `core/memory/artifact.py` `EVEROS_VERSION` becomes
   the single behavioral source; other modules import or interpolate it instead
   of embedding their own copy. Update every remaining pin to 1.2.1 and the new
   lock SHA256:
   - `core/memory/artifact.py` `EVEROS_VERSION` (drives
     `provider_root_format="everos-1.2.1"`, the dev fingerprint, and the
     embedded smoke script)
   - `core/memory/sidecar.py` startup version check
   - `core/memory/runtime.py` — the two `"everos-1.1.3"` fallback literals
     (lines 147 and 569) must use the canonical constant
   - `scripts/build_memory_runtime.py` `EVEROS_VERSION` / `LOCK_SHA256`
   - `scripts/generate_memory_runtime_manifest.py`
   - `scripts/memory_runtime_release_guard.py` (hardcoded `"1.1.3"`)
   - `vibe/memory_runtime_manifest.json` (version strings; `release_state`
     stays `unavailable` with placeholder hashes until a runtime release is
     actually published — none exists today, verified against GitHub)
   - Close-out gate: `rg -n '1\.1\.3'` over the repo must show no remaining
     behavioral reference (docs/changelog mentions excepted).
3. Adapter behavior review for 1.2.1:
   - `_deterministic_client_rejection` in `core/memory/everos.py`: upstream
     v1.2.1 maps `ProviderNotConfiguredError` to **422 with error code
     `PROVIDER_NOT_CONFIGURED`** (the changelog's `CAPABILITY_UNAVAILABLE` is a
     different, 503-mapped code — the tagged code and tests agree on
     `PROVIDER_NOT_CONFIGURED`). That rejection is configuration-dependent, not
     a function of the request bytes: classify it retryable by inspecting the
     error envelope code on 422, not just the status, and rewrite the docstring
     that anchors the 4xx taxonomy to the 1.1.3 build. Test against the shipped
     envelope shape.
   - 503 `CAPABILITY_UNAVAILABLE` / `EXTERNAL_SERVICE_UNAVAILABLE` land in the
     existing 5xx handling (retryable) — confirm in tests, no adapter change.
   - `/health` typed response and search score-field renames need no adapter
     change (body ignored / scores unread) — confirm in tests.
4. Comments referencing "EverOS 1.1.3" in `core/memory/process.py`,
   `modality.py`, `artifact.py` updated where behavior-relevant.

## Phase 2 — /api/v2

- `core/memory/everos.py`: `add/flush/search` routes move to
  `/api/v2/memory/*` (`/health` stays unversioned).
- `core/memory/sidecar.py` guard: route allowlist and per-route validator
  dispatch move to `/api/v2/*`; v1 routes are no longer forwarded (brutal cut,
  no alias support in the guard).

## Phase 3 — project_id = Agent Session working directory

Derivation:

- New `derive_project_id(scope_key, workdir) -> "p-" + digest[:32]` in
  `core/memory/store.py`, plus `is_project_id` shape check, mirroring the
  principal scheme. Raw paths are never persisted or sent to the provider.
- Define the project-aware store in `core/memory/schema.sql`, with non-null
  `project_ref` on queue rows. This is the only schema, not a migration, and the
  store does not carry a schema-version upgrade mechanism.

Write path (capture -> add/flush):

- `InboundTurnFacts` gains the session workdir; `Controller` fills it from the
  resolved Agent Session row's stored `workdir` (the same value the agent turn
  ran under — not re-resolved from Scope).
- `CaptureAdmission` validates it and the store persists `project_ref` on each
  queue row as part of the initial schema.
- `ProviderCapture` gains `project_ref`; `EverOSPort.add` sends it as
  `project_id` instead of the `_PROJECT_ID` constant.
- Because project is fixed per session, flush stays keyed by `session_id` and
  `_provider_session_ref` is unchanged; the store enforces the invariant that
  all rows of one session carry one `project_ref` (defensive check, not a
  grouping dimension).

`vibe memory remember` (agent-initiated capture):

- `/internal/memory/remember` in `core/internal_server.py` builds a
  `CaptureRequest` directly; it must resolve the caller session's project the
  same way reads do (below) and carry it on the request.
- The idempotency key becomes
  `agent:{principal}:{project}:{session}:{digest}` so the same remembered text
  in two projects cannot collide on one digest row.

Read path (search/profile):

- `MemoryModule.search/profile` and `MemoryProviderPort` take `project_id`.
- CLI reads (`/internal/memory/search|profile|remember`): the caller-session
  map in `core/controller.py` (`configure_memory_cli_session`) stores
  `(principal_id, project_id)` — project derived from the admitted session's
  stored workdir — so an admitted agent session reads and writes its own
  project's memories.
- UI reads (`vibe/ui_memory_routes.py` via `internal_client`): the trusted
  Controller-side internal route derives the default-cwd project
  (`derive_project_id` over the default working directory) before calling the
  provider. Resolve that directory with the same shared
  `expanduser` / `abspath` / configured-default / process-cwd fallback used by
  Agent Session creation; never hash the raw config string independently.
  Upstream 1.2.1 pins an omitted
  `project_id` to the literal string `"default"` in the query filter — it is
  not a cross-project view, and the sidecar guard would reject that literal
  anyway. Test both that UI profile/search payloads carry a `p-<32 hex>` value
  and that it exactly equals the project ID used by a default Agent Session.
  A UI project selector is follow-up work.
- Sidecar guard `_valid_scope`: `app_id == "avibe"` unchanged; `project_id`
  must match the `p-<32 hex>` shape instead of `== "personal"`.

## Phase 4 — Tests and validation

- Update version-pinned tests: `test_memory_everos.py`, `test_memory_sidecar.py`,
  `test_memory_runtime*.py`, `test_local_deps.py`,
  `test_memory_runtime_release_guard.py`.
- New coverage: project-aware store schema, project derivation/shape, guard
  scope acceptance/rejection, capture->add payload
  carrying the derived project, one-project-per-session invariant, remember
  idempotency key with project, CLI-session (principal, project) mapping, UI
  reads sending the same project as a default Agent Session, and 422
  `PROVIDER_NOT_CONFIGURED` retry classification against the shipped envelope.
- `ruff check` on changed files; focused pytest first, then the full local
  suite and CI.
- Incus regression (`回归测试` flow) for user-facing verification: capture,
  search and profile from agent sessions in two different working directories
  must be isolated.

## Phase 5 — Runtime packaging (when publishing)

- `scripts/build_memory_runtime.py` for darwin-arm64 / linux-arm64 / linux-x64;
  publish `memory-runtime-v1.2.1-1` release assets **before** flipping the
  manifest to `release_state: published` with real hashes (release-guard
  ordering rule).

## Out of scope / follow-ups

- Capability tiers (embedding-optional operation) — separate product task.
- OpenTelemetry tracing — future observability task.
- UI project selector for the memory browser (this version fixes UI reads to
  the default-cwd project).
