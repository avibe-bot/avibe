# Model Hub — Interface Contracts

Status: **FROZEN v2** · 2026-07-29 · change only via orchestrator
Supersedes FROZEN v1 (2026-07-23 10:45) outright — no back-compat, no migration shims.
Derived from: spec v2 (`../model-hub.md`), implementation plan
(`../model-hub-implementation.md`, v1 — a v2 lane plan is cut separately),
spike S1 engine survey (`docs/plans/model-hub-engine-survey.md`),
spike S2 ToS review (`docs/plans/model-hub-tos-review.md`).

## v2 ruling (owner, 2026-07-28/29)

**Ordering is a consumption property, not a supply property.** The single global
priority list was a product-model error: a source is an asset the user owns, and
how eagerly to spend it is a decision each agent backend makes for itself. So the
spend order moves onto the per-backend supply record — each backend owns an
**ordered subset** of the sources it is eligible for, plus a policy (`follow` =
跟随推荐, server-computed and auto-joining; `custom` = 自定义, user-owned and frozen).

v2 replaces v1 outright because the feature never GA'd (`VIBE_MODEL_HUB_ENABLED`
dormant by default, every backend defaults `direct`, PR #1019). Old config keys are
dropped on load rather than translated. Also folded in: server-authoritative
eligibility, a `needs_action` state class, per-agent `supply_status`, the effective
chain per (agent, model), a dry-run probe, and `adopted_by` on source create.

Cooldown and health stay **source-global**, deliberately: quota exhaustion and
network reachability are properties of the account, not of the agent that happened
to touch it (`core/handlers/model_hub/service.py::_cooldown`).

## Resolved decision, carried forward from v1 (owner, 2026-07-23 10:33)

**Hybrid supply by default + consent-gated experimental hub-held subscriptions.**
Subscription sources default `supply_channel: "native_cli"` (per-turn channel
dispatch; the CLI's own sanctioned OAuth burns the quota); api_key sources are
`"hub"`. Additionally, subscription login INTO the engine (hub-held, incl.
Claude) ships as an **experimental feature behind
`subscription_hub_experimental`** with explicit ban-risk consent (copy: S2 §9)
and per-source opt-in — never enabled silently, always visibly marked. The
`allowed_origins`-style client binding applies to native_cli sources
(sanctioned client only) and to any experimental hub-held subscription.

Everything else reflects S1 findings: runtime-declared OAuth presentation
(S1 gap ③), adapter-owned redacted resolution events with the engine usage feed
disabled (S1 gap ② — the feed leaks inbound keys), model provenance + cooldown
fields, and a standalone managed-dependency manifest/status contract. The v1
freeze item "authoritative ordered priority" survives in v2 as **authoritative
ordered per-backend subset**: the server always re-echoes the full canonical
order after any mutation; clients never compute partial reorders.

## Freeze protocol

- The orchestrator commits these files with message
  `docs(model-hub): freeze interface contracts v2` and announces the commit SHA
  in every lane brief. From then on: lanes cite, never edit; changes go through
  the orchestrator and bump `contract_version`.
- Contract tests (implementation plan §5) validate both directions against
  these schemas.

## Required-vs-optional discipline (v2)

Contracts land before implementation, so v2 fields describe a destination the
shipped v1 serializers have not reached yet. The rule that keeps that honest:

- **New contracts are fully required.** `agent-chain.schema.json` and
  `probe-result.schema.json` describe surfaces with no v1 implementation, so every
  field is `required` and the examples are complete.
- **Fields grafted onto a live v1 serializer are optional here and REQUIRED at the
  API boundary once that serializer lands them.** That covers
  `agent-supply.sources` / `supply_status` / `model_supply` and
  `resolution-event.severity`. Each such property says so in its own
  `description`; the implementation PR flips it to `required` in the same change
  that makes it emit.
- **Existing examples stay v1-shaped on purpose.** The byte-faithful round-trip
  test (`tests/test_model_hub_config.py`) drives the shipped serializer through
  every example in `source.schema.json` and `agent-supply.schema.json`, so a v2
  field in those examples would assert that dormant v1 code already speaks v2.
  Worked v2 payloads live in `api.md` instead, where they document the target
  without lying about the present.
- Two v2 enum values (`source.state.status: needs_action`,
  `resolution-event.kind: needs_action`) are likewise targets: today's loader
  whitelist in `config/v2_config.py` rejects the former until the serializer PR
  widens it. Called out inline in the schemas.

## Files

| File | Consumers |
| --- | --- |
| `source.schema.json` | L2 API, L4 UI. **v2:** +`state.status: needs_action` (+ `detail_key` causes), +optional `usage.projected_exhaust_at`. No ordering field, ever. |
| `agent-supply.schema.json` | L2, L3 injection, L4/L5 UI. **v2 owns the spend order:** +`sources` {policy, order, eligibility}, +`supply_status`, +`model_supply`. |
| `agent-chain.schema.json` | **New in v2.** L2 API, L4 UI (model box drill-in, 模型菜单 drawer). |
| `probe-result.schema.json` | **New in v2.** L2 API, L4 UI (「试跑一次」). |
| `priority.schema.json` | **Removed in contract v2 — v1 tombstone, do not implement.** Nothing in v2 reads it. Kept only because dormant v1 code still emits the shape and `tests/test_model_hub_api.py` still validates against it; the lane that removes `PUT /api/models/priority` and `ModelHubConfig.priority_order` deletes this file and replaces that one assertion with an inline shape check. |
| `resolution-event.schema.json` | L2 (adapter-owned), L4 UI, L1 adapter. **v2:** +`severity` (notification tier), +`kind: needs_action`. |
| `oauth-flow.schema.json` | L2, L4 UI, L1 engine adapter |
| `migration-scan.schema.json` | L6, L5 UI. Unchanged by the v2 ruling — native-config import is an onboarding feature, not a priority mechanism. |
| `runtime-dependency.schema.json` | L1, L2 status API, L7 guards. **URL policy (orchestrator, 07-23 12:05):** the example URLs are placeholders; L1 ships with upstream release URLs + SHA256 integrity verification. Availability guard = L7/orchestrator deliverable BEFORE GA: mirror the pinned assets into Avibe-owned release storage (same manifest-verified backup/recovery pattern as Show Runtime, per repo release rules), then point the manifest at the mirror with upstream recorded as provenance. SHA256s never change (same bytes). L1 must NOT build the mirror or touch the Show Runtime guard. **Platform expansion (07-23 13:13):** linux-arm64 / darwin-x64 assets get pinned (+ schema platform-enum rev) together with the mirror work at L7; until then unsupported hosts fail closed, Direct = escape hatch (L1's `model_hub_engine_platform_unsupported` coverage is the intended behavior). |
| `api.md` | all. **v2:** `PUT /api/models/priority` removed; +`PUT /api/models/agents/<backend>/sources`, +chain query, +probe, +`adopted_by`. |
| `opencode-overlay.md` | L3, L7 (identifier-stability tests). Identifiers stay stable across per-agent reordering — a source is never encoded into the provider segment. |
| `adapter-interface.py` | L1 (implements), L2 (consumes; owns in-repo copy). Dual-copy rule: both lanes copy VERBATIM to `core/handlers/model_hub/adapter.py` in their branches — byte-identical, merge is a no-op. Added 07-23 10:55 after L1 raised the ordering race; **v1.1 07-23 11:05**: +OAuth surface with deterministic source binding (`start_oauth(source_id)` → success carries `credential_ref`), +`allowed_origins`/`invoke(origin)`/`OriginNotAllowedError` (both from L1 review findings). Unchanged by v2: candidate walking and error classification stay Python-side, so moving ordering from global to per-agent needs no adapter or engine change (spec §8). |

## Security invariants (from S1/S2, non-negotiable)

1. No credential material ever appears in any payload defined here —
   sources expose `credential_ref` only; events are adapter-redacted.
   Clarified 07-23 12:10 (L4 finding): non-reversible display data IS
   permitted — `account_label` (subscription identity from the sanctioned
   auth surface) and `masked_credential` (≤7-char prefix + "…" + last 4,
   computed once at provisioning, never re-derivable) exist precisely so
   the UI never needs anything stronger.
2. The engine's own usage feed stays disabled; events originate from our
   adapter (S1 gap ②). Extended in v2: `ProbeResult.error` and every
   `detail_key` are i18n keys — classified outcomes only, never raw upstream
   error bodies, on the probe path as much as the event path.
3. `allowed_origins`-style client binding is enforced in code: subscription
   sources are never eligible for agents outside their sanctioned client
   (S2; server-side enforcement exists for Claude anyway). In v2 this is the
   same rule the server now publishes as `sources.eligibility` — one
   implementation, projected to the UI, never re-derived there.
