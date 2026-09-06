# Model Hub E2E Test Plan

Status: routing-mode acceptance revision, contract_version 10, 2026-09-06.
Contract: `model-hub-routing-modes.md` (original `2db273891`, owner scope/synchronization
decision `c1d398d5f`, Empty Route Inheritance `352486374`). The older survey
below remains background for unrelated capabilities; routing expectations follow this revision.
Companion inventories (kept in session, summarized here): capability list C1–C81 and issue list B1–B17 produced during the 2026-09-01 survey.

## 1. Goals and ground rules

1. Exercise the **shipped surface end to end** over real HTTP: browser-visible API (`/api/models/*`) and the per-turn gateway, against a **mock upstream provider**, never real vendor credentials.
2. Assert **current behavior as baseline** where the owner has ruled (cross-vendor fidelity ruling 2026-08-08; "user owns the order" contract in `docs/plans/model-hub.md` §4.2), and mark scenarios **fix-first** or **expected-fail** where the survey found unintended drift (see §5 decision ledger).
3. Every scenario names the feature ids (C#) it covers and the evidence layer it lives in: `e2e-auto` (this suite), `unit/contract` (existing), `manual` (Incus regression).

Out of scope (documented, not dropped silently):
- Real-vendor OAuth completion (Anthropic/OpenAI login dance) — needs real accounts; covered by `manual` in Incus regression. Flow lifecycle (start/status/cancel/expire/nonce replay) **is** in scope via the hub OAuth endpoints up to the auth URL.
- OpenCode provenance (by design never written; `model-hub-l3-correlation.md`).
- Protocol-conversion fidelity beyond the functional floor (owner ruling: converted supply is functionally usable while its reasoning chain may degrade — expected, not a defect). E2E asserts: turn completes, tool call ids correlate on the final turn, stream terminates with a well-formed terminal frame. It does **not** assert reasoning/system-prompt fidelity.

## 2. Environment and fixtures

- Runtime: hermetic `AVIBE_HOME` under the test dir; `VIBE_MODEL_HUB_ENABLED=1`. Docker `tests/e2e` service with `command: ["full"]` (controller + UI in one process pair) or an equivalent local hermetic run; whichever the implementing lane proves stable first.
- Engine binary: no GitHub egress in CI. Use `VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH` / `VIBE_MODEL_HUB_ENGINE_OFFLINE` (`vibe/model_hub_runtime/installer.py:105-109`) with the pinned cliproxyapi v7.2.95 assets vendored per platform (linux-amd64/arm64, darwin-arm64/x64).
- Offline archive seeding: when the selected manifest asset uses `file://`, the harness verifies its declared size and SHA-256, then copies it into the hermetic `$AVIBE_HOME/runtime/model-hub/engine/downloads/` cache before spawning child processes. `VIBE_MODEL_HUB_ENGINE_OFFLINE=1` remains set, and non-file assets are never fetched.
- **Mock upstream provider** (new test fixture, `tests/e2e/drivers/mock_llm_upstream.py`): implements, per protocol — `POST /v1/messages`, `POST /v1/responses`, `POST /v1/chat/completions`, `GET /v1/models` — with:
  - configurable model inventory (ids only, plus Anthropic-style `display_name` and relay-style `context_length` extensions to prove they are dropped, B-list);
  - auth modes: accept / 401 / 403(banned-pattern) / 402 / 429 / quota-message / 5xx;
  - stream modes: healthy SSE per protocol, and interrupt-after-first-model-output;
  - request capture (last N bodies) so tests assert what the upstream **received** (effort stripping, model prefix, header survival).
- Auth/roles: two API tokens (owner, member) for the permission matrix.

## 3. Suites

### A. Gating and runtime lifecycle (C1,C2,C5,C9–C15)
| ID | Steps | Expect | Status |
|---|---|---|---|
| A1 | flag off | `/api/models/*` → 404 `feature_disabled`; UI redirects to `/settings/backends` | assert |
| A2 | install → start → stop happy path; status polling transitions | `installing/starting/active/stopped`; engine config regenerated with hardened values (`request-retry: 0`, `disable-cooling: true`, `usage-statistics-enabled: false` …) — conservation check on CPA upgrade | assert |
| A3 | agent in hub mode → stop runtime | 409 `runtime_in_use` with `data.backends`; **and** UI surfaces the blocking backends (B4 fix-first; baseline: generic toast) | fix-first |
| A4 | agents read fails while runtime on | toggle must not silently disable (B5) | fix-first |
| A5 | controller restart mid OAuth poll | poll reports `engine_down` copy, never a fake materialization failure (B10) | assert |

### B. API-key sources (C16–C29)
| ID | Steps | Expect | Status |
|---|---|---|---|
| B1 | observe against mock for each protocol, `protocol:'auto'` | correct protocol chosen by response shape; observation payload contract_version 10 | assert |
| B2 | custom user-declared protocol differs from response shape, authentication succeeds | declaration badge and runtime-risk warning stay visible; explicit confirmation persists exactly the chosen protocol, not an inferred one. Authentication remains required. Auto still needs matching shape evidence (2026-09-04 declaration rule; adapter consumer rejects authenticated but unshaped Auto responses). | assert |
| B3 | pull models (mock returns `display_name`, `context_length`, `pricing`, `supported_parameters`) | unrelated metadata is dropped; `supported_parameters` is retained in observation and the provenance ladder applies protocol-default tiers | assert |
| B4 | discovery fails | 422 `inventory_unavailable` unless `accept_unavailable_inventory:true`; then source commits with `state.status=error` | assert |
| B5 | replay create with same `client_nonce` | idempotent, no duplicate source | assert |
| B6 | replace key happy + rollback; rename; patch base URL | C22/C23 contracts | assert |
| B7 | sole owned default source + exact one-hop manual route, verified by reads → delete source → guard 409 → echo plan → force | actual inherited supply gap; impact report lists interrupted agents; malformed echo rejected; canonical route and exact captured default order restored independently (only the owned deleted ID may be omitted) | assert |
| B8 | force transport asymmetry (`?force=` vs body) | document current split (B6 issue); decide normalization | baseline |
| B9 | refetch after upstream inventory change | added/removed diff; **`discovered_at` preserved for pre-existing models** (currently overwritten — fix-first, see B-list) | fix-first |
| B10 | inspect catalog-managed tiers; edit user/null tiers; reload a v6 row and refresh | catalog edits return 409 `source_model_tiers_managed` with exact provenance; user tiers remain editable; refresh restores catalog truth and emits one redacted override event | assert |
| B11 | trigger each of: `mapping_target_unavailable`, `runtime_in_use`, `source_nonce_conflict`, `reauth_confirmation_required`, `turn_not_found` | UI renders human copy, never the raw `modelHub.errors.*` string (B1 — fix-first; baseline expected-fail) | fix-first |

### C. OAuth lifecycle (C30–C40, partial by §1)
| ID | Steps | Expect | Status |
|---|---|---|---|
| C1 | start hub OAuth (anthropic/openai) | auth-url presentation per vendor; status polling; cancel | assert |
| C2 | flow expiry + nonce replay | `flow_expired`; replayed nonce resumes, no duplicate flow | assert |
| C3 | native channel start-failure copy | `native_subscription_exists`, `native_login_in_progress` | assert |
| C4 | full login → materialize → adoption note | manual (Incus, real accounts) | manual |

### D. Per-turn routing, fallback, capability behavior (C41–C50, C74–C81)
| ID | Steps | Expect | Status |
|---|---|---|---|
| D1 | add api_key source first, subscription-class source second | **current**: appended at tail (key burns first). Baseline assert-current; expected value flips with decision D-1 | baseline |
| D2 | turn with hop0 healthy | served; provenance records hop; usage metered | assert |
| D3 | hop0 → 429 before first output | same-turn failover to hop1; `switch` event; hop0 cooldown 60s; chat copy unchanged (silent) | assert |
| D4 | hop0 → quota (`insufficient_quota`) | fallback, cooldown 300s | assert |
| D5 | hop0 → 401 (api_key, non-refreshable) | `credential_revoked` needs_action; UI shows repair action; no mid-turn refresh attempt | assert |
| D6 | hop0 → 5xx | fallback 30s cooldown; retry-after header **ignored** (document; decide with D-4 whether to keep) | baseline |
| D7 | stream interrupt after first output | terminal frame injected per protocol; **no replay/failover**; source still settles; next turn resolves hop1 (takeover pill visible) | assert |
| D8 | cooldown `retry_at` elapses | next resolve recovers source, chain returns to hop0, `recover` event | assert |
| D9 | all hops failing | 503 `mapping_target_unavailable` + waiting copy with retry_at | assert |
| D10 | chat selects effort `high`; fallback hop declares tiers `[]` | upstream capture strips effort only for that hop; the exact served attempt records the stripped value and declared tiers in turn provenance | assert |
| D11 | `POST /{backend}/v1/messages/count_tokens` | currently 404 `not_found_error` (D-2 decision: fix or document impact on Claude Code auto-compact) | baseline |
| D12 | env/catalog injection for claude/codex/opencode | `ANTHROPIC_BASE_URL/TOKEN` only for claude; codex catalog neutralized 4 keys; opencode overlay model projection shape | assert |
| D13 | member role matrix | member can PUT chains/PATCH mode; cannot create/delete source, start runtime, read usage (B15) — assert the dead-ends are at least *visible* errors, not silent UI | assert |

### E. Usage and events (C54–C60)
| ID | Steps | Expect | Status |
|---|---|---|---|
| E1 | run metered turns (mock emits usage incl. cache tokens) | totals/by-source/by-model/by-day consistent; `token_reports` shortfall honest | assert |
| E2 | `?days=` boundary: 1, 62, garbage, 100000 | 1/62 ok; garbage clamps; 100000 must not pass unbounded (B14) | fix-first |
| E3 | event feed pagination + redaction | no credential substring anywhere in events payload | assert |

### F. Migration (C61–C67)
| ID | Steps | Expect | Status |
|---|---|---|---|
| F1 | seed hermetic HOME with native claude/codex/opencode configs; scan | correct action matrix; opencode unsupported ids noted | assert |
| F2 | apply selection | copy-only import; native login committed before imported keys (one-time sort); original files untouched | assert |
| F3 | first open `/settings/models` after upgrade with importable items | banner visible (B2 — currently only in wizard: expected-fail until mounted) | fix-first |

### G. Guards and contract hygiene (C51–C53, B3,B7,B13,B16)
| ID | Steps | Expect | Status |
|---|---|---|---|
| G1 | guard echo with a gap missing `agents` | no confirm-loop (B7) — fix-first; baseline documents | fix-first |
| G2 | Sources PUT and compatibility chains/reorder change defaults | same effective guard plan and canonical projection; manual arrays unchanged; omitted reorder order is read-only | assert |
| G3 | malformed JSON on mode/chain routes | error code distinguishable from business refusals (B13) | fix-first |

### H. Cross-protocol functional floor (owner ruling baseline)
| ID | Steps | Expect | Status |
|---|---|---|---|
| H1 | claude-protocol turn served by openai_chat source (mock) | completes; tool call↔result tuple correlates on final turn | assert |
| H2 | same with parallel tools | passes (M0 matrix failed 5/8 — if still failing, recorded as known relay behavior per ruling, surfaced in release notes, not a gate) | baseline |
| H3 | image block passthrough | behavior documented (currently untested anywhere); at minimum: no crash, terminal frame well-formed | probe |

### I. Routing modes and compatibility

| ID | Property | Evidence |
| --- | --- | --- |
| RM-1 | Nonempty manual intent survives unrelated config/catalog/health/process transitions; equal-to-automatic nonempty saves remain manual, while absent and valid empty values inherit across all backends | Seed every supported saved shape, mutate defaults/inventory/catalog, reload and compare exact nonempty/stale/dormant arrays, catalog identities and unrelated config; canonical output maps omit empty values and manual_override is nonempty or null |
| RM-2 | Automatic matching precedes passthrough, and live health/transport restrictions never choose another tier | All backends with API-key unknown passthrough only, unmatched subscription-only defaults Unconfigured, known-model subscription admission and stale-hop retention, native Claude aliases/date/version rules, complete non-retired evidence, literal API matching, native-only matching with Hub-only invocation, tombstones and empty inventories |
| RM-3 | One planning input produces identical effective hops/origin in preview, read/list, summaries, adoption, guards, probe, launch and execution | Real API/controller boundaries, assert canonical model ids and Source ids; unknown inventory carries no invented reasoning tiers |
| RM-4 | Restore/Undo/Cancel/Save preserve normalized draft and persistence truth, including final-hop removal | Browser GET -> edit -> remove final hop or Restore -> preview null -> Undo to prior unsaved draft or Cancel -> reload; inherited Save uses DELETE, nonempty manual Save uses PUT even when equal to automatic. Cover actual inherited origin/targets or Unconfigured, no empty Manual save, failed saves, stale preview, confirmation, Done and duplicate submission |
| RM-5 | Effective removal confirmation is exact and atomic for defaults/Restore/inventory/catalog/Source writes | Refuse, alter state, retry stale plan, confirm current plan; no partial writes. Empty PUT and DELETE have identical effective-removal/supply guards, idempotency, admission/lease and rollback behavior; only nonempty manual PUT retains the visible-edit interruption-only guard. A cascade removing the final hop drops its override before recomputing inherited impact; pure reorders preserve nonempty manual arrays and need no confirmation |
| RM-6 | Pinned CPA reaches the selected upstream with the exact canonical unknown model, without fake inventory or cross-Source fallback | Real backend launch -> correlation/gateway -> adapter -> pinned CPA -> local upstream; Hub API-key credentials on all API protocols and admitted non-ASCII ids/payloads; no subscription expansion |
| RM-7 | Transport registration remains separate from inventory and capabilities | Compare truthful SourceBinding.model_ids/Source.models with deterministic route_model_ids and compiler union; load absent field as empty; unchanged target sets do not restart; registration changes drain active transport leases before supervised restart, block new invocations behind the barrier and release leases on completion/close/cancellation independently of service settlement; prove sync cancellation/rollback and concurrent Save without deadlock; Hub OAuth registration remains unchanged |
| RM-8 | Request-only unknown-model errors preserve Source health and existing failure/stream boundaries | Typed upstream model_not_found, exact Source/model attribution, no fallback or global health transition |
| RM-9 | Released config and turn records remain readable; normalization is not a validation bypass | Seed supported valid empty/populated/stale/dormant routes on every backend and direct-constructed config: validate original shapes first, normalize valid empty values without losing nonempty routes, catalog identities or unrelated sections, and persist the normalized map only on normal save. Reject malformed wrappers/hops/identifiers and dangling references before normalization. Output manual arrays require minItems 1, but accepted empty PUT/preview and legacy configuration do not. Read TurnProvenance versions 5 through 10 without requiring new optional metadata on old records |
| RM-10 | Approved help, labels and layout work on desktop/mobile in both themes | Screenshots against approved routing frames; hover/focus/touch help, Escape/outside dismissal, no row-open from badge, no overlapping labels/buttons |
| RM-11 | Read and preview are operationally pure, including normalization of valid legacy empties | With runtime stopped assert unchanged config bytes/events/engine lifecycle, no credential access or upstream egress, for nonempty manual, accepted empty and null restored drafts. Empty and null previews return the same inherited chain/origin and manual_override null without changing stored intent |
| RM-12 | Recorded error detail uses the latest retained exactly attributed turn, never a synthetic last-request history | Exercise backend/model provenance GET against the existing bounded store: exact backend/model isolation, latest persisted ordering independent of outcome, later success/cancel clearing, empty/evicted/ambiguous history, old records without optional metadata and deleted historical Sources. Capture real model_not_found with observed HTTP status; reject unsafe upstream text and invented codes. Prove retrieval never starts/syncs the engine or writes config/history/events. Browser evidence covers the Latest recorded turn label, same-record structured details, generic old-record copy, retry, pending-read invalidation on close/model change, unchanged route origin and no history writes from Cancel/Restore/Save |
| RM-13 | Every actual invocation atomically admits a current effective candidate before transport, without holding the global mutation lock during network | Deterministically pause resolution across target removal/change and Source configuration changes; exercise ordinary/fallback, bounded credential-refresh retry and probe through the same admission path. Preserve failed-Source exclusions, reject stale exact refresh pairs without fabricated attempts or synthetic upstream errors, and verify service-mutation then adapter-routing lock order. Pre-admission failure/cancellation releases exclusion; callback failure releases the lease before network; post-admission completion/cancel releases it. Concurrent independent HTTP requests progress during buffered/first-token/stream waits, with no per-request registration or persistent generation store |

All fixtures use test-owned HOME/XDG/config/credentials and local upstreams. Never use
personal state. Development acceptance uses coordinated local Incus only, preserving
master state. Mocked peer suites do not replace the pinned-engine boundary proof.

Implemented unit/contract evidence for the empty-inheritance boundary supplements
the end-to-end properties above; these are actual consuming tests, not proposed cases:

| Property | Existing consuming test |
| --- | --- |
| RM-1/RM-3/RM-11: absent/empty parity across consumers | `tests/test_model_hub_routing_modes.py::test_empty_override_equals_absence_at_all_route_consumers` |
| RM-4/RM-5: empty PUT equals guarded Restore; final-hop deletion inherits | `tests/test_model_hub_routing_modes.py::test_empty_put_has_exact_restore_guard_and_final_hop_deletion_inherits` |
| RM-9: original invalid input never becomes automatic | `tests/test_model_hub_routing_modes.py::test_invalid_empty_intent_is_validated_before_normalization` |
| RM-5: inventory final-hop changes use inherited effective impact | `tests/test_model_hub_routing_modes.py::test_final_inventory_hop_removal_uses_inherited_guard_plan` |
| RM-5/RM-7: failed sync rolls back canonical intent and permits retry | `tests/test_model_hub_routing_modes.py::test_failed_route_sync_rolls_back_canonical_intent_and_retry` |
| RM-1/RM-9: dormant/direct empty keys do not fabricate routes or guards | `tests/test_model_hub_routing_modes.py::test_direct_dormant_empty_is_absent_from_route_and_guard_enumeration` |
| RM-9/RM-11: load is read-only; normal save persists normalization | `tests/test_model_hub_config.py::test_v2_empty_route_normalizes_on_load_without_writing_until_save` |

The Python `MH-ROUTING` scenarios in
`tests/scenarios/model_hub/test_model_hub_configured_route_scenarios.py` remain
canonical catalog evidence. `ui/e2e/model-routing-origin.spec.ts` adds Playwright
evidence for RM-4: Restore, Undo and Cancel send no mutation; Save uses DELETE,
handles confirmation and retains automatic intent after reload. Its test label
does not create a catalog citation: the current browser citation resolver is
Vitest-only. No new catalog engine or browser INDEX row is required. Run this
supplement separately against the coordinated hermetic gateway fixture.

## 4. Mapping to existing coverage

Unit/contract suites already cover: config validation, guard structure, classification tables, oauth registry, usage ledger, runtime injection. E2E must **not** duplicate them; it owns the seams those can't see: double-observe cost, cooldown-vs-next-turn timing, UI copy on live errors, guard echo round-trip, engine config conservation, member-role dead-ends.

## 5. Owner decision ledger

- **D-1 subscription-first placement.** Restore "subscription before API key" at placement time (surgical change in `_apply_source_placement`, reuse dead `recommended_source_order`), incl. one-time re-sort of existing configs? Current shipped contract says append-at-tail. Affects D1.
- **D-2 `count_tokens` 404.** Serve it (proxy to upstream tokenizers or estimate) or document Claude Code context-estimation degradation in hub mode? Affects D11.
- **D-3 missing `modelHub.*` browser i18n keys + absent Python-bundle codes.** Fix now (makes B11 pass) or baseline as expected-fail for this round?
- **D-4 Retry-After honoring.** Keep flat cooldowns (simple, predictable) or honor upstream reset headers? Affects D6.
- **D-5 tier provenance ladder (approved 2026-09-03).** Apply the first available
  `upstream > catalog > user > null` rung; managed tiers are read-only, a refresh
  override is observable, and silent stripping is attributed to the exact attempt.

## 5a. Cross-lane contracts (frozen 2026-09-01)

Two implementation lanes build the suites in parallel. These contracts are the
interface between them; deviations route through the orchestrator, never
lane-to-lane.

### Mock upstream provider (owned by the pytest lane)

- Path: `tests/e2e/drivers/mock_llm_upstream.py`. **Stdlib-only** (http.server +
  threading or asyncio): it must run standalone on a bare Python 3.11+ with no
  repo install.
- Runs standalone: `python3 tests/e2e/drivers/mock_llm_upstream.py --port N
  [--host 127.0.0.1]`; also importable as a pytest fixture in `tests/e2e/`.
- Serving surface (per configured protocol correctness):
  - `GET /v1/models` — configurable inventory; supports Anthropic-style
    (`data[].id`, `display_name`, `type`) and OpenAI-style (`data[].id`,
    `created`, `owned_by`) plus relay extension fields (`context_length`,
    `pricing`) so drop-behavior can be asserted.
  - `POST /v1/messages` | `/v1/responses` | `/v1/chat/completions` — protocol-
    correct non-stream and SSE-stream responses, including the shape evidence
    auto-detect needs (`type: "message"` / `object: "response"` /
    `object: "chat.completion*"`, and family-distinctive `error.param` bodies
    for invalid probes).
- Control plane (all under `/__control/`, never proxied):
  - `POST /__control/config` — set behavior JSON: `{auth: ok|401|403_banned|402|
    429|quota_message|5xx, stream: healthy|interrupt_after_first_output,
    models: [...], protocol: anthropic|openai_responses|openai_chat,
    models_endpoint: ok|http_404|http_500|timeout|malformed_json}`.
  - `models_endpoint` (default `ok`) applies ONLY to `GET /v1/models`;
    inference endpoints are unaffected. Evaluation order per request:
    `auth` applies first to every endpoint (including `/v1/models`); only
    when `auth: ok` does `models_endpoint` apply. An empty `models` array
    with `auth: ok` and `models_endpoint: ok` is a SUCCESSFUL empty
    inventory, distinct from discovery failure. Scenario B4 (protocol proven,
    discovery failed → 422 `inventory_unavailable` → `accept_unavailable_
    inventory` commit path) is produced by `auth: ok` +
    `models_endpoint: http_500` (or `http_404`/`timeout`/`malformed_json`).
    *(Contract amendment 2026-09-02, orchestrator ruling on the pytest lane's
    B4 blocker.)*
  - `GET /__control/requests` — returns an OBJECT `{"requests": [...]}` (not
    a bare array; extensible envelope), each record carrying method, path,
    headers subset, parsed body; `DELETE /__control/requests` resets.
    *(Shape frozen 2026-09-02.)*
- The Playwright lane consumes this ONLY over HTTP via env
  `VIBE_E2E_MOCK_UPSTREAM_URL`; it never imports the Python module. Mock-
  dependent specs skip with a clear message when the env is absent.

### Environment variables (shared vocabulary)

- `VIBE_E2E_BASE_URL` — target Avibe UI origin (default `http://127.0.0.1:5123`).
- `VIBE_E2E_MOCK_UPSTREAM_URL` — running mock upstream origin.
- `VIBE_MODEL_HUB_ENABLED` — optional; Model Hub is enabled when absent. Harnesses
  may explicitly set `1` for isolation or `0` to exercise the disabled surface.
- `VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH` — offline engine manifest override for
  hermetic engine install; engine-dependent pytest scenarios must skip (not
  fail) with a clear reason when neither this nor GitHub egress is available.

### Suite conventions

- pytest: files `tests/e2e/test_model_hub_*.py`, marker `e2e_model_hub`,
  NOT part of default CI in these PRs. Scenario IDs from §3 (A1…H3) appear in
  test names or docstrings.
- Playwright: lives in `ui/e2e/`, config `ui/playwright.config.ts`, dependency
  added explicitly to `ui/package.json` devDependencies, scripts
  `npm run e2e` / `npm run e2e:headed`. Chromium only. Not wired into CI in
  this PR.
- Baseline discipline: scenarios marked `baseline` in §3 assert CURRENT
  behavior with a comment naming the open decision (D-1…D-4); `fix-first`
  scenarios land as `xfail` (pytest) / `test.fixme` (Playwright) with the
  issue reference, never as red tests.
- **Discipline tightening (orchestrator ruling 2026-09-02, post circuit
  breaker on PR #1812):**
  - *Skip policy:* only ENVIRONMENTAL preconditions may `skip` (engine binary
    unavailable BEFORE an install attempt, mock upstream absent, credentials
    required but not provisioned). Once a precondition has passed, any
    subsequent product-side failure (installer error after manifest accepted,
    OAuth-start HTTP error after engine confirmed up, etc.) is a test
    FAILURE. Every skip site states the precondition it guards.
  - *xfail granularity:* an `xfail`/`fixme` wraps ONLY the assertion known to
    be broken (e.g. the human-readable error copy). All currently enforceable
    assertions (structured API guards, state transitions, status codes) live
    in separate passing tests. A broad xfail over a mixed test is forbidden.
  - *Skip vs gap (round 5):* `skip` = this layer CANNOT execute the scenario
    (state the blocking precondition and the owning layer); `gap` = executable
    but not yet implemented (actionable; must surface in coverage
    next_priority). Never use `skip` for "not written yet."
  - *Self-audit before push (round 5):* before every push, the lane audits
    EACH spec invariant (skip policy, xfail granularity, layer honesty,
    ownership, catalog single resolver) across ALL call sites in the diff —
    invariants bind at their extensions, not at the site a review last named.
    The report lists the checklist outcome.
  - *Environment ownership invariant (orchestrator ruling 2026-09-02, round 4):*
    the harness owns every process it starts, by invariant, not by leak-list.
    Cleanup must PROVE ABSENCE: after teardown, scan the process table for any
    process whose argv references this test's unique hermetic marker (its
    `AVIBE_HOME` path) and terminate it, including detached / separately-
    sessioned engine children whose controller leader already exited. Never
    trust the recorded leader alone. Scoped strictly to the unique marker —
    no broad host-process killing — on the SUPPORTED MATRIX (macOS + Linux);
    on other platforms the opt-in subprocess harness skips with a platform
    precondition. Regression tests must cover the detached-child escape.
    SOUND ABSENCE PROOF (round 7): "absence" means no RUNNING process (state
    != zombie) references the marker. Direct children are reaped via waitpid;
    detached PIDs are polled until gone or zombie (use the process-status API,
    e.g. psutil which the product already depends on — never argv-scan alone,
    and never kill(pid,0) as an existence proof). A zombie holds no resources
    and cannot be reaped by a non-parent: a persistent zombie after the poll
    window is a logged warning, not a failure.
    STOP CONDITION (refined 2026-09-02, round 7): its substance is a RUNNING
    product process (controller, UI, managed engine) holding resources after
    teardown on a supported platform, surviving a reviewed head. If that
    happens, DO NOT patch again — reduce the owned surface: keep the
    controller+UI pair under one setsid-owned process group with pgid kill +
    waitpid, move engine-lifecycle scenarios (A2/A5/D2–D6) to skip rows owned
    by a dedicated future suite. Evidence-soundness refinements (like the
    zombie corner) and test-owned mock resources are fixed normally and do
    not trigger it.
  - *Catalog single resolver (round 4):* scenario references (top-level tests
    AND `partial_evidence.test`, AND skip-row references) resolve through ONE
    resolver shared by the checker and the metadata; the checker may not hold
    a second notion of "what a scenario references." Skip rows without a
    top-level test remain valid but their refs still resolve.
  - *Layer honesty:* a scenario this layer cannot execute is a catalog `skip`
    row with a reason and a pointer to the layer that owns it (unit suite or
    the Playwright lane) — never an `xfail` and never an unconditional-fail
    sentinel (sentinels are forbidden: they neither detect a regression nor a
    fix). Partial evidence is fine when labeled partial: a passing test that
    covers a subset of a scenario must say so in its docstring, and the
    scenario's catalog row names the covered subset.
  - *B6 rollback evidence boundary:* the pending-revocation journal at
    `$AVIBE_HOME/state/model_hub_pending_revocations.json` inside the
    test-owned hermetic home is the public evidence surface for rejected
    credential material. Tests may read it; they must NOT read engine-private
    credential blobs. If the product writes neither journal entry nor
    observable removal on failed replacement, B6 narrows to an xfail with a
    product-gap finding — never private-storage inspection.

### Playwright scope (this round)

Core flows against a live instance: capability gate redirect, runtime
install/start/stop toggle incl. blocked-stop copy, add-API-key dialog full
branches (auto-detect / manual protocol / pull models / empty-inventory
add-anyway / replace key) against the mock upstream, guard refusal → confirm
→ impact report (incl. B7 echo probe as fixme), source detail
rename/refetch/tier edit, route chain dialog add/remove/keyboard reorder,
global priority drawer, usage & logs tabs render with real metered data,
error-copy assertions (fixme where copy is known-missing per B1/D-3),
member-role dead-ends (second auth context).

## 6. Execution plan

1. Lane 1 (fixtures): mock upstream driver + offline engine manifest packaging + full-mode e2e bootstrap. No product code changes.
2. Lane 2 (scenarios A/B/E/F/G): API-level e2e against fixtures.
3. Lane 3 (scenarios D/H): turn-level e2e incl. stream capture and capture-based upstream assertions.
4. Manual C4 + native-channel D-variants folded into the next Incus regression run.
5. Fix-first items ship as separate small PRs *before* their scenario is flipped from baseline to assert.

Each lane follows `pr-delivery-loop`; suites land under `tests/e2e/test_model_hub_*.py` reusing `tests/e2e/conftest.py` and `drivers/`.
