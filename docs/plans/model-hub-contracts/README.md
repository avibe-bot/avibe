# Model Hub — Interface Contracts

Status: **FROZEN v1** · 2026-07-23 10:45 (+08:00) · change only via orchestrator
Derived from: signed spec (`../model-hub.md`), implementation plan
(`../model-hub-implementation.md`), spike S1 engine survey
(branch `spike/model-hub-engine`, `docs/plans/model-hub-engine-survey.md`),
spike S2 ToS review (branch `spike/model-hub-tos`,
`docs/plans/model-hub-tos-review.md`).

## Resolved decision (owner, 2026-07-23 10:33)

**Hybrid supply by default + consent-gated experimental hub-held subscriptions.**
Subscription sources default `supply_channel: "native_cli"` (per-turn channel
dispatch; the CLI's own sanctioned OAuth burns the quota); api_key sources are
`"hub"`. Additionally, subscription login INTO the engine (hub-held, incl.
Claude) ships as an **experimental feature behind
`subscription_hub_experimental`** with explicit ban-risk consent (copy: S2 §9)
and per-source opt-in — never enabled silently, always visibly marked. The
`allowed_origins`-style client binding applies to native_cli sources
(sanctioned client only) and to any experimental hub-held subscription.

Everything else reflects S1 findings:
runtime-declared OAuth presentation (S1 gap ③), adapter-owned redacted
resolution events with the engine usage feed disabled (S1 gap ② — the feed
leaks inbound keys), model provenance + cooldown fields, authoritative ordered
priority, and a standalone managed-dependency manifest/status contract.

## Freeze protocol

- After the owner decision, the orchestrator commits these files with message
  `docs(model-hub): freeze interface contracts v1` and announces the commit
  SHA in every lane brief. From then on: lanes cite, never edit; changes go
  through the orchestrator and bump `contract_version`.
- Contract tests (implementation plan §5) validate both directions against
  these schemas.

## Files

| File | Consumers |
| --- | --- |
| `source.schema.json` | L2 API, L4 UI |
| `priority.schema.json` | L2, L4 |
| `agent-supply.schema.json` | L2, L3 injection, L4/L5 UI |
| `resolution-event.schema.json` | L2 (adapter-owned), L4 UI, L1 adapter |
| `oauth-flow.schema.json` | L2, L4 UI, L1 engine adapter |
| `migration-scan.schema.json` | L6, L5 UI |
| `runtime-dependency.schema.json` | L1, L2 status API, L7 guards. **URL policy (orchestrator, 07-23 12:05):** the example URLs are placeholders; L1 ships with upstream release URLs + SHA256 integrity verification. Availability guard = L7/orchestrator deliverable BEFORE GA: mirror the pinned assets into Avibe-owned release storage (same manifest-verified backup/recovery pattern as Show Runtime, per repo release rules), then point the manifest at the mirror with upstream recorded as provenance. SHA256s never change (same bytes). L1 must NOT build the mirror or touch the Show Runtime guard. **Platform expansion (07-23 13:13):** linux-arm64 / darwin-x64 assets get pinned (+ schema platform-enum rev) together with the mirror work at L7; until then unsupported hosts fail closed, Direct = escape hatch (L1's `model_hub_engine_platform_unsupported` coverage is the intended behavior). |
| `api.md` | all |
| `opencode-overlay.md` | L3, L7 (identifier-stability tests) |
| `adapter-interface.py` | L1 (implements), L2 (consumes; owns in-repo copy). Dual-copy rule: both lanes copy VERBATIM to `core/handlers/model_hub/adapter.py` in their branches — byte-identical, merge is a no-op. Added 07-23 10:55 after L1 raised the ordering race; **v1.1 07-23 11:05**: +OAuth surface with deterministic source binding (`start_oauth(source_id)` → success carries `credential_ref`), +`allowed_origins`/`invoke(origin)`/`OriginNotAllowedError` (both from L1 review findings). |

## Security invariants (from S1/S2, non-negotiable)

1. No credential material ever appears in any payload defined here —
   sources expose `credential_ref` only; events are adapter-redacted.
   Clarified 07-23 12:10 (L4 finding): non-reversible display data IS
   permitted — `account_label` (subscription identity from the sanctioned
   auth surface) and `masked_credential` (≤7-char prefix + "…" + last 4,
   computed once at provisioning, never re-derivable) exist precisely so
   the UI never needs anything stronger.
2. The engine's own usage feed stays disabled; events originate from our
   adapter (S1 gap ②).
3. `allowed_origins`-style client binding is enforced in code: subscription
   sources are never eligible for agents outside their sanctioned client
   (S2; server-side enforcement exists for Claude anyway).
