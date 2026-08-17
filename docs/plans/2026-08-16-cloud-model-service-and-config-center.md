# Cloud Model Service & Unified Config Center

- **Status**: spec approved-in-discussion, M0 in progress
- **Date**: 2026-08-16
- **Owner decisions recorded**: 2026-08-16 (see §3)
- **Repos**: `avibe-bot-backend` (primary), `avibe` (client), `avibe-docs` (docs + design.pen)
- **Fact baseline**: backend facts verified against `origin/main` @ `f542028`, checkout synced to `a3f8e4b` on 2026-08-16. Implementation lanes must re-verify cited symbols against their branch head.

## 1. Problem

Model-backed product features currently have two inconsistent supply models:

- **Voice input** is cloud-served and free-unlimited: local Avibe and the browser call avibe.bot
  (`/v1/audio/transcriptions`, `/v1/voice/dictations`, `/api/cloud/voice/*`), which forwards to
  DashScope (`qwen3-asr-flash`) plus a transcript-cleanup LLM chain. All upstream API keys are
  **global env vars** (`DASHSCOPE_API_KEY`, `DEEPSEEK_API_KEY`, …in `lib/settings.ts`). There is
  **no per-org/per-user key, no usage persistence, no quota**.
- **Memory** is fully user-configured: the everos sidecar calls user-supplied OpenAI-compatible
  endpoints (slots `memory.processing.{llm,embedding,rerank,multimodal}`, each
  `base_url + model + api_key`, `config/v2_config.py:1661`). Memory **cannot be enabled without
  keys** — the exact configuration burden we want to remove. It has zero cloud coupling today.

There is no organization-level model governance, no platform-admin role, and no usage visibility.

## 2. Goal

One **Cloud Model Service** with one **Config Center**, serving two scopes through one codebase:

- **Organization scope (enterprise)**: an org admin configures capability slots (provider endpoint,
  model, API key) once; every instance in that org consumes those upstreams through avibe.bot.
  Cost lands on the org's own provider account (BYOK). Employees see no model configuration.
- **Platform scope (personal)**: instances not under an enterprise org use the platform's own
  upstreams (today's env keys, moved into config) with a limited free allowance. The author's
  email gets a **platform admin** role to manage this pool.
- **Usage metering** in both scopes, attributed **per instance**, token-granular, with a dashboard.
  No money conversion, no billing — v2.

### Naming and positioning

- **Cloud Model Service** = the serving face on avibe.bot (proxy endpoints + resolution + metering).
- **Config Center** = its admin surface (org console mount + platform admin mount).
- Distinct from the local **Model Hub** (`avibe-docs/concepts/model-hub.mdx`), which gateways the
  user's *own* agent-backend models on the user's machine. Docs must keep the two concepts apart:
  Model Hub = your models for your agents, locally; Model Service = Avibe-supplied feature models
  (voice, memory), cloud-side.
- This adds **memory processing as the second explicit content-processing exception** in
  `avibe-bot-backend/AGENTS.md` (today voice is the only sanctioned one). The amendment ships with
  M2 and was product-approved by the owner on 2026-08-16.

## 3. Owner decisions (2026-08-16, verbatim intent)

1. **No billing system.** A statistics dashboard is enough. "扣额度" means the upstream provider
   account is consumed; the org bears that cost because the org configured the key (BYOK).
2. **Metering granularity = per instance** (not per user; Avibe accounts by instance): tokens per
   instance + total. v1 rough is fine; no currency conversion (v2).
3. **Full reuse mandate**: personal and enterprise share ONE schema, ONE backend logic path, ONE
   UI component set. No parallel implementations, UI or backend.
4. **Personal**: memory settings keep a manual-configuration entry; default = cloud free pool;
   user may switch to their own keys at any time (also the escape hatch when free quota is gone
   and before paid plans exist). **Enterprise**: no manual entry at all; org config is the only path.
5. **Platform admin allowlist = exactly the owner's email** for now. Capabilities: platform-pool
   model config, global usage, limit adjustment. Never enters org spaces.
6. **Enterprise detection**: `organizations.plan = 'enterprise'` (column exists, display-only
   today), set manually by the platform admin in v1; no purchase flow.
7. **Sequencing**: build the full path first; milestone pacing delegated to the PM.

## 4. Non-goals (v1)

- Billing, settlement, invoices, currency conversion, seat accounting, plan purchase flows.
- Per-user attribution inside an org (instance-level only).
- Arbitrary provider protocol adapters: v1 wire contract is **OpenAI-compatible** upstreams only.
  ASR additionally requires DashScope-compatible audio chat-completions; realtime ASR requires the
  DashScope realtime protocol. Non-conforming providers are out of scope until a real need appears.
- **Rerank slot in cloud mode** — deferred: optional in the engine and it uses a nonstandard wire
  path (model name as URL path, `core/memory/everos.py:672`). Local manual mode keeps rerank.
- Voice BYOK for personal users (manual entry exists only in Memory settings, per decision #4).
- Changing the Model Hub or agent-backend routing in any way.

## 5. Concept model

### 5.1 Capability slots

A **ModelServiceConfig** exists per scope owner and contains up to one slot per capability:

| Capability | Consumed by | Upstream wire shape |
| --- | --- | --- |
| `asr` | voice transcription + dictation (+ optional `realtime_model`) | DashScope-compatible `chat/completions` with `input_audio`; realtime WS |
| `chat` | memory LLM, voice transcript cleanup | OpenAI `chat/completions` (streaming supported) |
| `embedding` | memory embeddings | OpenAI `embeddings` |
| `multimodal` | memory IM-attachment/vision capture | OpenAI `chat/completions` |

Each slot: `provider_label` (display), `base_url`, `model`, `api_key` (write-only),
`realtime_model` + `realtime_url` (asr only, optional — realtime is available iff **both** are
set; the realtime WS endpoint is a distinct URL, not derived from the HTTP `base_url`), `enabled`.

**Unset multimodal**: there is no server-side fallback. The client leaves the sidecar's multimodal
endpoint unset, so the engine itself falls back to its chat endpoint (everos's own behavior); the
`/v1/model/mm/*` proxy path serves only a configured multimodal slot and returns
`model_service_not_configured` otherwise; `capabilities.multimodal` in §8.2 reports the dedicated
slot only. The client keys its local attachment-capture gate to *effective* multimodal
availability (dedicated slot **or** chat fallback), so the fallback actually engages.

### 5.2 Scope resolution (the one rule)

For every model call, resolve the calling **instance**, then:

1. `instance.organization_id` → org with `kind='organization'` AND `plan='enterprise'`
   → **organization scope**. Scope binding is by plan alone — never by whether slots happen to be
   configured.
2. Otherwise → **platform scope** (free pool), subject to platform limits.
3. Missing/disabled slot in the bound scope → `model_service_not_configured` (503).
   **Never silently fall across scopes**: an enterprise misconfiguration must surface as an error,
   not leak onto the platform's free pool. An enterprise org that configures only some
   capabilities gets exactly those capabilities; the rest stay off (the config UI's empty states
   say so).

**UI banner visibility follows scope binding, not slot completeness.** Once an organization is
enterprise-bound, its managed-model banner remains visible even when one or every slot is empty;
an empty slot is an active-scope configuration error, not a reason to make the UI look platform-
scoped. Before enterprise activation, the staging UI does not show that runtime-management banner.

**Provisioning order (adjudicated 2026-08-16, PR #232)**: org config is **pre-stageable** — org
owner/admin can read/write their org's config regardless of plan; runtime resolution ignores it
until `plan='enterprise'`. One principle: **plan gates resolution; roles gate configuration.**
Recommended activation sequence (documented, not code-enforced): stage slots → verify
(`POST …/model-service/verify`, §6.3) → flip plan.

Resolution + metering is **one middleware** shared by every model endpoint (existing voice routes
and the new proxy routes). This is the code-level embodiment of decision #3.

### 5.3 Client modes (memory)

- `organization`: local Avibe auto-points the memory sidecar at the cloud proxy with the instance
  model key; the manual endpoints UI is not rendered; settings show a read-only "configured by
  your organization" state. The local Memory on/off toggle and the engine-restart action remain
  available (owner-decided 2026-08-16): the org manages the model source, not the local feature
  switch or engine lifecycle.
- `platform` (personal default): same cloud wiring, zero config, free-pool quota; settings offer
  "Use custom endpoints" which switches to
- `custom` (personal manual): today's local direct-connection config, unchanged.
- **Mode initialization on upgrade**: an existing installation with a complete manual configuration
  stays `custom`; the `platform` default applies only where no complete manual config exists.
  Upgrades never silently replace a user's own providers.

Voice needs **no client change**: existing endpoints keep their contracts; the backend resolves
scope per instance behind them.

## 6. Backend design (`avibe-bot-backend`)

### 6.1 Schema (Drizzle, additive migrations)

- `model_service_configs`: `id`, `scope_kind` (`platform` | `organization`), `organization_id`
  (null for platform; unique per org), `revision` (optimistic concurrency, matches org patterns),
  `limits` jsonb (platform row only: `{ per_instance_monthly_tokens: { <capability>: n }, enforce: bool }`
  — *monthly* = **UTC calendar month**, bucket key `YYYY-MM`, shared by reservations, aggregation,
  and enforcement), timestamps, `updated_by_user_id`. Exactly one platform row (unique partial index).
- `model_service_slots`: PK (`config_id`, `capability`); `provider_label`, `base_url`, `model`,
  `realtime_model`, `realtime_url` (both nullable, asr only), `api_key_ciphertext`, `enabled`,
  timestamps, `updated_by_user_id`.
- `model_usage_events`: `id`, `call_id` (unique index — shared with the reservation's call
  identity; a retried insert after an ambiguous database result is idempotent), `probe` (bool,
  default false), `estimated` (bool, default false), `window_bucket` (`YYYY-MM`, copied from the
  reservation when one exists so monthly aggregation matches the bucket rule), `occurred_at`,
  `instance_id` (nullable for probe events), `organization_id` (nullable),
  `scope_kind`, `capability`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `status` (`ok` | `error`). Indexes: (`organization_id`, `occurred_at`), (`instance_id`,
  `occurred_at`), (`scope_kind`, `occurred_at`). v1 dashboards aggregate on read; add rollups only
  if measurably slow.
- `model_quota_reservations`: `id` (caller-supplied call identity — the idempotency key),
  `instance_id`, `capability`, `window_bucket` (`YYYY-MM`, UTC), `reserved_tokens`, `state`
  (`reserved` | `settled` | `released`), `created_at`, `expires_at`, `dispatched_at` (stamped
  just before the upstream call). Settlement is idempotent on the call identity; the reaper
  **releases** expired reservations that were never dispatched (certainly-not-sent) and **retains**
  dispatched ones as conservative charges (possibly-accepted semantics, §6.4). Realtime
  extensions share the parent reservation's dispatch state; conservative whole-reservation
  retention on ambiguous session death is accepted v1 imprecision (free-pool estimates, not
  billing). Settlement always applies to the reservation's `window_bucket`, fixed at reservation
  time, even across a month boundary. Reservations exist only on
  enforcement-on paths.
- `model_access_keys`: `instance_id` (PK), `key_hash`, `created_at`, `rotated_at`, plus
  `previous_key_hash` + `previous_valid_until`. Opaque key (`mak_` prefix), shown once at mint,
  SHA-256 stored — same custody discipline as device secrets. **Rotation contract — three
  properties; the mechanism (state machine + tests) is owned by the M2 backend implementation in
  `avibe-bot-backend/lib/model-service/access-key.ts` plus its store transaction and route tests,
  and deliberately not prescribed here**: (1) *grace-safe* — after any rotation, the key the sidecar
  is actually using keeps working long enough (≥ 24 h) for the managed settings ladder to apply
  the new one; (2) *retry-safe* — retrying a rotation whose response was lost, any number of
  times, can never invalidate a key still in use; (3) *on-demand only* — no scheduled rotation
  (rotation is for suspected leaks and hygiene). The schema carries whatever fields the
  implementation needs (`previous_key_hash`, `previous_valid_until`, activation tracking) to
  satisfy these properties.

### 6.2 Key custody

- Slot API keys encrypted at rest with AES-256-GCM under a new env `MODEL_SERVICE_KEY_SECRET`;
  decrypted only for a model call, a bounded save/explicit-verification probe, or the instance
  status key-availability check. A save first encrypts the candidate, decrypts that managed
  ciphertext, and uses only the decrypted value for any upstream probe; every enabled candidate
  must complete the encrypt/decrypt round trip before persistence, including when `force: true`
  skips the upstream probe. Status uses decrypt-validate-and-discard and never retains or returns
  plaintext. This status-only read-path exception supersedes the earlier proxy-only wording.
  All read APIs return `has_api_key: true/false`, never the value. Keys and ciphertext never enter
  API responses, logs, Sentry, or usage events. Missing or invalid key-secret material for a
  committed saved slot is reported as a high-priority Sentry event tagged by scope, capability,
  instance, and config revision, using only scrubbed metadata. Emission is transition-deduplicated
  by scope, config revision, and capability so status polling cannot create one event per instance
  per minute; the first observing instance remains a context tag but is not part of the
  deduplication key. Explicit admin verification uses the fixed non-customer
  `model-service-probe` context when there is no calling instance; transition-window claimless
  realtime uses its existing fixed `unattributed-legacy` context. Neither value identifies a
  customer instance. Both adapters atomically upsert `available`/`unavailable` state in a shared
  transition table; only a successfully persisted transition to `unavailable` grants permission
  to emit the outage event. A successful managed decrypt of committed saved ciphertext attempts
  the `available` transition on every call; candidate save round trips never read or write alert
  state. If that recovery write fails, the usable call still proceeds, the write is retried on the
  next successful decrypt, and the persistence failure itself is reported directly to Sentry
  without relying on the unavailable transition table. Concurrent and cold-started Vercel
  isolates therefore observe the same edges without making alert infrastructure part of service
  availability.

### 6.3 APIs

- **Org admin (user session; org `owner`/`admin` of a `kind='organization'` org; uniform gate for
  all three routes, independent of plan so config can be pre-staged before activation)**:
  `GET/PUT /api/organizations/{orgId}/model-service` (slots CRUD, revision-checked),
  `GET /api/organizations/{orgId}/model-service/usage?from&to` (per-instance rows + totals; before
  enterprise activation it contains exactly the org's pre-activation probe events), and
  `POST /api/organizations/{orgId}/model-service/verify` — one bounded probe per enabled staged
  slot (1-token chat, `"OK"` embedding, minimal ASR) through the shared egress client, allowed
  regardless of plan (the org admin exercising the org's own keys); probe calls are metered as
  organization-scope usage with `probe: true` and `instance_id: null` (the caller is an admin
  session, not an instance) — totals include them, per-instance tables exclude them. Probes cover
  HTTP wire shapes only; the realtime WS protocol is not probed in v1 (recorded follow-up, §12).
  Ships with M1 so stage → verify → flip needs no plan flip to test. When the org already
  resolves as enterprise (**active scope**), `PUT` runs the same bounded probes inline against the
  changed slots and rejects on failure (`model_service_verification_failed`, 422) unless
  `force: true` — a live org's config change is never blindly activated (mirrors the local
  settings ladder's probe-before-apply). Both successful verify responses (200) and failed verify
  responses (422) carry the scope's current `revision`, so a caller can settle its optimistic
  snapshot without another read. Managed-key custody failures are never force-overridable: if any
  enabled candidate cannot complete its encrypt/decrypt round trip, `PUT` returns
  `model_service_key_unavailable` (503), performs no upstream probe, and persists nothing.
- **Platform admin (user session; email ∈ `PLATFORM_ADMIN_EMAILS`, new env following the
  `ORGANIZATION_CREATION_ALLOWED_EMAILS` pattern)**:
  `GET/PUT /api/admin/model-service` (platform slots + limits), `GET /api/admin/model-service/usage`
  (global, per-instance, per-scope — **stays global with per-scope breakdown, never narrowed to
  platform-only**; the per-instance list is bounded top-N by usage with an explicit instance count
  and truncation marker, matching the approved design; cursor pagination is a recorded follow-up,
  not v1 — both adjudicated 2026-08-16), and `POST /api/admin/model-service/verify`, with the same
  bounded probes and 200/422 `revision` contract as organization verify. Platform admin also gets the v1 lever to set
  `organizations.plan` (`PUT /api/admin/organizations/{orgId}/plan`).
- **Scope-generic verification and usage**: an active-scope `PUT` (organization or platform) probes
  every changed enabled slot before save and rejects atomically on probe failure unless
  `force: true`; route handlers do not reimplement scope-specific probe policy. Every usage summary
  exposes the same `by_capability` aggregate alongside its scope-specific/global breakdowns, so
  the shared console panel consumes one contract.
- **Instance-facing (device-secret headers, existing `/api/v1` family)**:
  `GET /api/v1/instances/{instanceId}/model-service` → mode/capability/identity payload (§8.2);
  `POST /api/v1/instances/{instanceId}/model-access-key` → mint/rotate `mak_` key. Mint and rotate
  share one 200 response shape:

  ```json
  {
    "key": "mak_...",
    "created_at": "2026-08-17T00:00:00Z",
    "rotated": false,
    "previous_valid_until": null
  }
  ```

  First mint returns `rotated: false` and `previous_valid_until: null`; rotation returns
  `rotated: true` and the old key's grace deadline in `previous_valid_until`. `key` is shown only
  in this response and is unrecoverable from every read surface.
- **Proxy (bearer `mak_` key)**: `POST /v1/model/chat/completions`, `POST /v1/model/embeddings`,
  `POST /v1/model/mm/chat/completions` — OpenAI-compatible, SSE streaming passthrough on chat.
  The upstream `model` is **always taken from the resolved slot; client-supplied `model` is
  ignored** (this also closes today's open model-forwarding on the voice routes, which currently
  pass any client `model` string upstream with the platform key).
- **Existing voice routes**: unchanged external contracts; internally rewired through resolution +
  metering. The cloud-token mint must carry the instance id claim end-to-end so browser-direct
  voice attributes to the right instance (verify; add the claim if absent). **Claim rollout**:
  tokens lacking the claim are rejected (401) at the rewired routes; released clients self-heal —
  they refresh at half-life and re-mint once on a 401 (verified client behavior,
  `ui/src/lib/avibeFetch.ts`) — the worst case on HTTP paths is one silent retry. The realtime WS
  path has **no verified in-session retry**, so that route alone accepts claimless tokens for a
  12 h transition window after deploy (attributed to platform scope as unattributed-legacy), then
  enforces; every other rewired route enforces immediately.
  **Operational ordering constraint**: the transition window must precede any enterprise
  activation — no org is flipped to `enterprise` within the window (M1 rollout deploy-order rule),
  so unattributed-legacy usage can only ever be platform-scope in fact and invariant 2 holds.
  **Error mapping on legacy voice routes** (invariant 6): `model_service_not_configured` surfaces
  through the existing voice error vocabulary (`asr_not_configured` 503 family; a missing chat
  slot degrades cleanup per §6.5-5). On required ASR and the dedicated cleanup endpoint,
  `model_service_key_unavailable` (503), `model_quota_exhausted` (429), and
  `model_service_unavailable` (503) pass through as new additive codes — released clients already
  treat unknown codes as generic errors of their status class. Composite voice flows catch every
  optional cleanup failure, including `model_service_key_unavailable`, and return the successful
  raw ASR transcript per §6.5-5.

### 6.4 Quota (platform scope only, v1)

Metering is always on. Enforcement is a flag, **default off (observe-only)** until real usage data
sets sane caps; when on, over-cap free-pool calls fail fast with `model_quota_exhausted` (429)
before any upstream call. Organization scope is never quota-limited in v1.

**Admission is reservation-based (adjudicated 2026-08-16)**: short-transaction atomic reservation
per call (fixed per-capability reservation constants; realtime reserves then extends atomically per
audio chunk), then exactly-once settlement — `usage-reported` settles actual; `usage-absent`
charges the full reservation (usage-less providers cannot make positive quotas free);
transport-ambiguous retains the conservative charge; certainly-not-sent releases. Limits updates
serialize against the same quota key. On enforcement-on paths the gateway also **bounds each
call's theoretical maximum to its reservation** (clamping `max_tokens` / rejecting oversized
inputs), so N admitted calls can never exceed N reservations by construction; upstream-reported
input variance may overshoot marginally and is absorbed by the next window. **Reservation-write
failure fails closed on enforcement-on paths** (`model_service_unavailable`, 503; no upstream call
made). **Settlement-write failure after a completed upstream call never voids the served
response**: the response is delivered, the un-settled reservation is later reaped as a retained
conservative charge, and the failure is loudly surfaced. Pure-telemetry event writes stay
fail-open per §8.3. While enforcement is **off**, `usage-absent` responses are still metered with
the documented per-capability estimate, flagged `estimated: true` — the observation period's data,
from which caps will be set, must not understate usage. Cloud user tokens: TTL drops 12 h → 1 h (clients already refresh at half-life);
revocation-on-use is a recorded follow-up.

### 6.5 Consolidated enforcement architecture (adjudicated 2026-08-16, post independent security review)

Every cross-cutting property has exactly one code owner; routes never re-implement any of them:

1. **ModelServiceConfigService** — the only config write path: key⇄address binding, encryption,
   revision, sanitized responses.
2. **ModelServiceStore** — atomic contracts with identical semantics in both adapters:
   `resolveCallSnapshot` (instance+org+plan+config+limits in ONE snapshot — every security decision
   reads one consistent state), `reserveQuota`/`extendQuota`/`settleQuota`, and
   `readUsageSummarySnapshot`; key-unavailable alert edges use a small additive transition table
   keyed by scope/config revision/capability and atomic `available`/`unavailable` upserts.
3. **ModelServiceUpstreamClient** — the only egress owner: destination classification
   (allow-only-global-unicast per the IANA IPv4/IPv6 special-purpose registries; transition/tunnel
   prefixes — NAT64 incl. local-use, 6to4, Teredo — rejected outright), **TLS-only upstreams**
   (`https`/`wss`; plaintext rejected at validation and at connect — decrypted keys never traverse
   cleartext), connection-time pinning for
   HTTP + redirects + WS, response/payload caps, secret scrubbing of ALL provider-controlled
   strings (incl. WS error frames and close reasons — provider error codes map to our stable codes,
   never pass through raw), and structured tri-state usage extraction
   (`usage-reported` / `usage-absent` / `transport-failure`, the last split into
   certainly-not-sent vs possibly-accepted). Routes never call `fetch`/`WebSocket` or parse usage.
4. **ModelServiceGateway** — the only call-lifecycle owner, fixed order:
   snapshot → reserve → just-in-time decrypt → client → exactly-once settlement → error mapping.
   The shared saved-key effectiveness predicate is the only owner of `key_status` judgment;
   status serialization and call routing consume that result rather than defining another test.
5. **Voice orchestration** — product composition only (required ASR + optional cleanup); cleanup
   failure of ANY kind (quota or upstream) degrades to the raw transcript in composite flows; the
   dedicated cleanup endpoint alone returns 429 on quota.

### 6.6 Voice cleanup chain

Organization scope: cleanup uses the org `chat` slot only (single upstream, no chain, no
cross-scope fallback). Platform scope: existing env-driven multi-provider chain may remain as an
internal implementation detail of the platform slots during migration. **Once a platform config is
saved, platform cleanup is single-upstream too** — the env chain is legacy-era resilience, not
carried into saved configs (adjudicated 2026-08-16; cleanup stays optional and degrades to the raw
transcript). While the legacy chain remains active, every provider attempt in one logical cleanup
meters as its own usage event — failed billed attempts included. First-save materialization must
preserve effective cleanup behavior for the
materialized provider — dialect/endpoint equivalence proven by a regression test; the §8.1 payload
shape is not extended for this.

## 7. Client design (`avibe`)

- New resolution client: fetch `/api/v1/instances/{id}/model-service` (poll/cached + on pairing
  events); persists the server-resolved **scope** (`organization` | `platform`), capability
  availability, `embedding_identity`, and the minted model key (config-scrubbed like other
  secrets). The local **mode** layers the user's choice on top: `organization` scope forces cloud
  wiring; otherwise the local choice selects `platform` (default) or `custom` — `custom` is
  client-local and never appears in the server payload (§8.2).
- **Memory**: in `organization`/`platform` modes, the runtime feeds the sidecar cloud endpoints +
  `mak_` key via the existing `EVEROS_*` env plumbing (`core/memory/process.py`); `custom` mode is
  the unchanged current path. Mode changes route through the existing settings-change ladder.
  Cloud mode injects **fixed model aliases** into `EVEROS_*__MODEL` (the proxy selects the real
  upstream model and ignores client-sent names); the embedding alias embeds `embedding_identity`
  so provider-call diagnostics stay distinguishable across identity changes. Cloud memory mode
  requires **both** `chat` and `embedding` enabled in the bound scope — anything less counts as
  no memory capability (the no-transition rule below applies), since the engine cannot start
  without a complete LLM + embedding pair. If an **active** cloud scope later disables either
  slot, member instances pause memory processing (capture keeps queuing in the durable outbox),
  surface the capability-off state, and resume automatically when the org re-enables the pair —
  never a silent fallback to `custom` or platform. The resume passes through the standard identity
  check: an embedding identity that changed while paused gates on the rebuild-confirmation flow
  before processing restarts.
- **Embedding identity**: the cloud config's `embedding_identity` participates in the sidecar's
  vector-space identity exactly like a local embedding config change — an org admin changing the
  embedding slot triggers the existing rebuild flow on every member instance, with the admin UI
  warning about exactly this blast radius before saving.
- **Rotation & key-change semantics (verified against code, 2026-08-16)**: an API-key change
  (slot key or mak; base_url/model unchanged) is non-identity on both sides — Avibe's identity is
  exactly `(embedding.base_url, embedding.model)` (`core/memory/runtime.py:3721-3735`, docstring:
  "Non-identity fields (API keys, LLM settings) may change … without invalidating a completed
  rebuild"), and everos 1.2.3 never persists any provider fingerprint (key lives only in process
  env → `@functools.cache`d settings → singleton HTTP client; EVEROS_ROOT holds no config-derived
  state; embedding dimension is the code constant 1024). A key change therefore applies through
  the existing managed settings ladder: preflight probe child with candidate env → quiesce (30 s;
  in-flight flushes awaited via `asyncio.shield`, durable outbox, no double-write) → graceful
  sidecar restart. A failed probe leaves the old sidecar running and rolls the config back.
  These managed-restart semantics apply only to keys the sidecar itself holds (custom-mode slot
  keys, the mak). **Org/platform slot keys live server-side only**: rotating them requires no
  client action, restarts nothing, and changes nothing in the status payload.
- **Mode-switch semantics**: switching `custom ↔ cloud` changes the base_url the sidecar sees and
  is an identity change by definition unless the upstream is identical — the existing
  rebuild-confirmation flow governs it. In cloud modes the client's identity input is the status
  payload's `embedding_identity` (upstream base_url+model), never the proxy URL — so org upstream
  changes propagate as rebuilds, and mak rotation does not.
- **Enterprise-attachment transition**: when an instance whose memory runs in `custom` mode becomes
  enterprise-managed, the client pauses memory processing (capture keeps queuing in the durable
  outbox; nothing is lost), surfaces a one-time transition notice, and performs the identity change
  through the same rebuild-confirmation flow on the user's acknowledgment — forced management
  changes the model source, but the rebuild is never silent. If the org provides no **cloud memory
  capability** (the chat + embedding pair not both enabled — embedding-only and chat-only orgs
  alike), no transition fires: an existing working `custom`
  configuration keeps running unchanged (the org has not provided a replacement model source), and
  the manual editor stays hidden only for *new* configuration; the transition applies when the org
  later enables memory slots. **Custom preservation is grandfathering only**, not a new-configuration
  entitlement: a fresh install attached to an organization without the memory pair renders the
  managed read-only state with Memory unavailable until the organization enables both slots — it
  never exposes manual setup and never falls back to the platform scope (PM ruling 2026-08-17).
- **Settings UI (Memory)**: three states per §5.3. Copy through `ui/src/i18n/{en,zh}.json`; show
  state, not mechanism; the enterprise state is one sentence, not a tour.
- **Voice**: no changes.

## 8. Frozen contracts (v1)

Lanes build against these shapes; deviations route through the PM, never lane-to-lane.

### 8.1 Slot payload (org & platform, identical — reuse mandate)

```json
{
  "revision": 12,
  "slots": {
    "asr":       { "provider_label": "DashScope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-asr-flash", "realtime_model": "qwen3-asr-realtime", "realtime_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference", "has_api_key": true, "enabled": true },
    "chat":      { "provider_label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash", "has_api_key": true, "enabled": true },
    "embedding": { "provider_label": "DashScope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "text-embedding-v4", "has_api_key": true, "enabled": true },
    "multimodal": null
  },
  "limits": { "per_instance_monthly_tokens": { "asr": 200000, "chat": 500000, "embedding": 1000000 }, "enforce": false }
}
```

`PUT` sends the same shape with `api_key` present only when (re)setting it; `limits` accepted only
on the platform scope. Omitted `api_key` means **keep the currently-effective key, bound to the
address it was entrusted to**: the kept key (stored, or env-materialized on the platform scope's
first save) stays valid only while the slot's **address fields** are unchanged from the addresses
that key was saved/materialized against — `base_url`, and for the asr slot also `realtime_url`;
**any address-field change requires `api_key` in the same PUT**
(`model_service_api_key_required`, 400). *Supersedes the same-day "no address-conditioned special
case" adjudication (PR #232 head 3)*: without this binding, write-only custody is hollow — any
admin could recombine the stored key with an attacker-controlled public address and harvest it.
UI consequence (M1b): while any address field is edited (base URL; the realtime URL on the voice
slot too), "leave blank to keep the saved key" is no longer offered and the key input becomes
required.

### 8.2 Instance status payload

```json
{
  "mode": "organization",
  "capabilities": { "asr": true, "chat": true, "embedding": true, "multimodal": false },
  "embedding_identity": "emb-9f3ac2",
  "quota": { "enforced": false },
  "revision": 12
}
```

`mode` ∈ `organization | platform`. `embedding_identity` is an opaque hash of the embedding slot's
(base_url, model) and changes iff vector-space identity changes. It is `null` exactly when
`capabilities.embedding` is false because the embedding slot itself is missing, disabled, or
unavailable through the shared effectiveness predicate. A chat-only degradation leaves the healthy
embedding capability and identity intact; the released client pauses cloud memory through its
existing `chat && embedding` pair predicate. Before serializing a saved scope, the endpoint consumes
that predicate for every enabled slot and immediately discards any plaintext. It includes the
platform scope's approved legacy-env recovery only when the normalized saved and env provider
addresses match; organization scope never receives that recovery. A slot with neither decryptable
saved custody nor eligible recovery is reported unavailable and the response adds a per-slot reason
map, for example `"degraded": { "asr": "model_service_key_unavailable" }`; `degraded` is omitted
when no slot is degraded.

### 8.3 Usage row

```json
{ "instance_id": "inst_...", "organization_id": "org_...", "scope_kind": "organization",
  "capability": "chat", "model": "deepseek-v4-flash",
  "prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
  "status": "ok", "occurred_at": "2026-08-16T04:00:00Z" }
```

`status: "error"` events carry upstream-reported token usage whenever the failure response includes
it (induced failures — `finish_reason: length`/`content_filter`, malformed payloads — cannot bypass
metering or quota). A metering-write failure never breaks the served call: the response/transcript
is still delivered, the failure is reported loudly (Sentry + structured log), and connections close
in a controlled way. A worker crash between upstream completion and the event insert loses only
that telemetry row — the reservation reaper keeps enforcement truthful — and falls under the same
accepted, monitored v1 gap. Fail-closed metering is deliberately deferred to M3, when enforcement
turns on.

### 8.4 Error codes (stable, machine-readable `code` field)

- `model_service_not_configured` (503) — resolved scope lacks an enabled slot for the capability.
- `model_service_api_key_required` (400) — an enabled save candidate has no submitted, retained,
  or materialized API key, or any candidate (enabled or disabled) changes `base_url` or
  `realtime_url` while trying to retain ciphertext bound to the prior address. Address changes
  never carry prior ciphertext forward. This client-input validation runs before managed custody
  validation and never emits a custody alert.
- `model_service_key_unavailable` (503) — an enabled save candidate cannot complete its managed
  encrypt/decrypt round trip after an actual key has been selected, or an enabled saved slot has
  neither decryptable custody nor an eligible same-scope platform env recovery under the shared
  effectiveness predicate. The failure is not overrideable by `force: true`, never maps to
  `model_service_not_configured`, makes no upstream call, and persists no candidate change. A
  rejected pre-save custody check writes a scrubbed structured log only: it creates no transition
  row and emits no ops alert because the request response and admin UI are its presentation surface.
  For unrecovered saved-slot failures, status marks the affected capability false with the same
  reason. Every saved-key decryption-failure transition, including one masked by approved platform
  recovery, emits one scrubbed high-priority Sentry event per scope/config revision/capability;
  repeated failures at the same active edge emit none, and successful decryption of the committed
  saved ciphertext rearms a later failure through the persisted `available` state. Candidate
  round trips never rearm that state. Failure to persist the committed recovery state never changes
  the successful call or truthful status result.
- `model_service_unavailable` (503) — quota **reservation** persistence failed on an
  enforcement-on path, before any upstream call. A **settlement** failure after upstream completion
  never produces this error: the response is served and the un-settled reservation is reaped as a
  retained charge (§6.4).
- `model_quota_exhausted` (429) — platform scope, enforcement on, cap reached. No upstream call made.
- `model_service_verification_failed` (422) — inline probe of a changed slot failed on an
  active-scope `PUT`; nothing was saved (override with `force: true`).
- `model_upstream_error` (502) — upstream provider failure (message scrubbed of secrets).
- Existing voice error codes unchanged.

## 9. Invariants (acceptance is verified against these, not case lists)

1. Every cloud-served model call resolves to exactly one scope and writes **at most one** usage
   event carrying that scope and the calling instance id (verification probes and transition-window
   legacy calls carry their documented alternative attribution instead: `probe: true` /
   unattributed-legacy) — exactly one, except when the metering
   write itself fails, which is loudly surfaced and never breaks the served call (accepted,
   monitored v1 gap while enforcement is off; enforcement-on paths are governed by fail-closed
   reservations per §6.4). Failure responses bearing upstream usage are metered with that usage.
   Dashboard aggregates equal the sum of recorded events.
2. An instance in an enterprise-configured org never consumes platform credentials or platform
   quota, for any capability; a missing or disabled organization slot yields
   `model_service_not_configured`, while an enabled slot with unavailable key custody yields
   `model_service_key_unavailable`, never a silent fallback.
3. No API response, log line, Sentry event, usage event, or error message ever contains a slot API
   key or its ciphertext; a `mak_` key appears exactly once — in the body of its own mint/rotate
   response (shown once by design) — and nowhere else; config reads expose only `has_api_key`.
   Every enabled saved slot is decrypt-valid before persistence, and status reflects the shared
   runtime effectiveness result, including only the approved platform env recovery, rather than
   ciphertext presence.
4. The upstream model invoked is always the configured slot's model, regardless of any
   client-supplied model string (documented exception: the legacy env cleanup chain, until a
   platform config is saved — each attempt's actual model is metered per §6.6).
5. A fresh personal instance **paired with Avibe Cloud** gets working voice and (post-M2) memory
   with zero model configuration; unpaired installations keep today's behavior (cloud voice
   unavailable, memory manual-only); an enterprise member instance renders no manual
   model-configuration affordance.
6. Released voice clients keep working unchanged across the M1 rewiring (device-secret path,
   cloud-token path, realtime WS).
7. While `capabilities.embedding` remains true, `embedding_identity` changes iff the embedding
   slot's (base_url, model) changes, and every member instance's sidecar treats it exactly like a
   local embedding change (rebuild ladder). It is `null` exactly when the embedding capability is
   unavailable; chat-only degradation leaves the identity unchanged while the client pair predicate
   pauses memory.
8. With enforcement on, an over-cap free-pool call fails before reaching any upstream.
9. Changing any API key (slot key or mak) with base_url and model unchanged never alters
   `embedding_identity`, never triggers a rebuild or re-embedding, and never loses or duplicates
   queued memory writes (outbox redelivers only provably-unsubmitted work; ambiguous submissions
   fence to `manual_required`, never replay).
10. After a mak rotation, the previous key remains accepted until the grace window ends; a sidecar
    still running on the old key keeps working until the client applies the new one.

## 10. Milestones

- **M0 — prep (done/ongoing)**: backend checkout synced to origin/main (done 2026-08-16); this
  spec; design.pen frames for the Config Center + client memory states; owner approves visuals
  (hard gate for UI lanes).
- **M1 — Config Center core + enterprise voice (backend only)**: schema migrations; key custody;
  org/platform config APIs; platform-admin gate + plan lever; resolution + metering middleware
  wired into all existing voice routes (incl. realtime + cleanup); usage dashboards (shared panel,
  two mounts); env→platform-scope seeding (ordinary env fallback ends when platform config is
  saved; the address-matched emergency recovery in §8.2 remains available for an undecryptable
  saved key).
  Acceptance: invariants 1–4, 6 hold on staging (`dev.avibe.tech`); an enterprise org with its own
  DashScope key sees its voice usage in its dashboard and the platform pool records none of it.
- **M2 — memory over the cloud**: `mak_` mint/rotate; OpenAI-compatible proxy (chat / embeddings /
  mm, streaming); backend AGENTS.md content-processing amendment; avibe client mode resolution,
  sidecar cloud wiring, settings states, embedding-identity integration, i18n; avibe-docs pages
  (Model Service concept + memory setup rewrite, en/zh 1:1); Model Hub concept-boundary note.
  Acceptance: invariants 1–5, 7, 9, 10; a fresh personal instance enables memory with zero config;
  an enterprise member's memory hits the org upstream; mode initialization ships with
  released-shape load fixtures per the avibe persisted-shape rule.
- **M3 — personal quota enforcement + polish**: review observed usage; platform admin sets caps;
  flip enforcement (invariant 8); client-side friendly quota-exhausted handling in memory + voice
  (message + switch-to-custom hint in memory); release notes; org-profile README sync if
  positioning copy changed.

Lane dispatch: all implementation lanes → `codex` (owner dispatch preference 2026-08-09), each via
`pr-delivery-loop`, mutual no-touch zones between concurrent lanes, contracts per §8. Acceptance /
design-fidelity supervision runs as a separate codex thread against design.pen frames.

## 11. Risks

- **Positioning**: this formally adds a second content-processing exception to a backend whose
  charter is control-plane-only. Owner-approved; the AGENTS.md amendment is part of M2's deliverable,
  and personal-mode local-first copy must stay truthful (memory cloud mode is default-on for
  personal — docs must say so plainly).
- **Enterprise keys server-side** is a deliberate departure from "keys stay on the user's machine";
  mitigated by §6.2 custody rules and truthful enterprise-mode copy.
- **Embedding blast radius**: an org embedding change rebuilds every member's index (§7); the admin
  UI warning + identity versioning are the containment.
- **everos compatibility**: the sidecar is a pinned third-party engine; cloud wiring reuses its
  existing env contract rather than patching it. Any incompatibility discovered escalates to the PM
  before workarounds.

## 12. Follow-ups (explicitly v2+)

Cost/currency conversion and per-provider pricing tables; billing/settlement; plan purchase flows;
rerank in cloud mode; non-OpenAI-compatible providers; per-user attribution; usage rollup tables;
cursor pagination on usage reports; realtime-WS verification probe in the org verify endpoint;
cloud-token revocation-on-use; a terminal-error contract for SSE streaming responses on the M2
proxy; organization-scope limits shape; per-capability cap-completeness validation on the platform
limits editor; platform-admin warning copy for pool-wide embedding rebuilds (M1b design note).
