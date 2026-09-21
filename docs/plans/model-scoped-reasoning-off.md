# Model-scoped reasoning Off

Owner decision: 2026-09-21. Revises PR #2084's backend-wide Off proposal.

## Contract

- The existing backend model row's `reasoning_efforts` is the source of
  selectable options. `none` means explicitly disabling extended thinking;
  an absent effort still means no override. `medium` remains unchanged.
- `none` belongs to the shared display vocabulary, not to protocol defaults,
  backend fallbacks, or automatically populated capability lists. Recognizing
  the value does not declare that a model supports it.
- Users can opt an individual model into `none` through the existing model
  editor. Merely opening or saving an existing row does not add the value.
  Built-in model rows, stored metadata, provenance, and default selection stay
  unchanged. No schema change or migration is needed.
- Claude normalization uses the same exact model declaration as other efforts.
  After selection, the Claude SDK translates `none` to
  `thinking: {"type": "disabled"}` without an effort override.
- OpenCode projects only declared variants. For Anthropic, a declared `none`
  becomes `thinking: {"type": "disabled"}`; other declared values keep their
  existing payloads. OpenAI-shaped variants keep `reasoningEffort: <value>`.
- Codex keeps its existing model-catalog and transport behavior: this change
  neither invents Off for native models nor prevents an explicitly configured
  compatible model from advertising it.
- Shared resolver intent forwarding, source discovery/provenance, routing,
  retry, and proxy-engine policy are not changed. Model metadata controls the
  selectable UI; it does not introduce a new gateway allowlist.
- Unknown/custom effort strings still round-trip. Existing `supports_reasoning`
  semantics and empty-versus-missing catalog behavior remain unchanged.
- Existing Agent creation/model-switch default selection remains intact:
  prefer an available `medium`, otherwise use the declared list's first option.
  Adding the vocabulary entry never reorders that declared list.
- Claude cache reuse compares the effective normalized reasoning choice as a
  process-creation setting, for main sessions, subagents, and in-flight creates.
  A changed choice waits for idle and rebuilds through the existing generation
  lifecycle with the native resume ID; unchanged choices reuse the process.
  Model-only control requests remain unchanged when effective reasoning matches.
- Web channel, user, and thread route saves normalize against the same exact
  backend model row as the picker, including an inherited Agent model. Native
  mode keeps its native catalog. Empty declarations stay empty; another model's
  `none` never grants support. Inherited model fields are not materialized.

## Validation invariants

- For all three backends, a model declaring `none` exposes it, while unrelated,
  missing, empty, default-only, and ordinary tier lists never acquire it.
- UI model editing persists Off only after an explicit selection, preserves
  custom values, and does not change existing selections or default behavior.
  English/Chinese pickers label the value as Off/关闭, not Default.
- Claude SDK options distinguish absent, ordinary, custom, declared Off, and
  undeclared Off requests on both native and Model Hub launch paths.
- OpenCode native/custom-provider and Model Hub projections preserve all
  existing ordinary-tier payloads, including custom model effort values, and
  translate only an explicitly declared `none`.
- Catalog serialization and existing routing/retry tests stay green. Tests use
  isolated state and native-transport fakes; they do not prove live provider
  support for an operator's custom model declaration.
- Resolver tests include direct/nested `none` and disabled thinking, plus
  provider fallback with mismatched/empty source capability metadata. Each
  attempt preserves the caller's original payload, protocol, and headers.

## Compatibility review scope decision

The independent review of `5ec19f1ac` found two boundary omissions: cache
identity did not include the selected process-level reasoning setting, and
Web routing writes used only the generic Claude vocabulary. Both were
reproduced with failing consumer tests before repair. Fix these two owners
using the existing lifecycle and model catalog; do not change shared gateway
fingerprints, provider policy, route selection, retry, or persisted schemas.

## Known by design

- No blanket Off backfill: support depends on the exact provider/model route.
- No universal Off-to-medium fallback or re-enabling thinking on rejection.
- No migration, deployment, release, host restart, or live-model invocation is
  part of this implementation.
- Live provider acceptance and the proxy engine's protocol translation policy
  remain separate verification layers. The existing boundary documented in
  `model-hub-reasoning-intent.md` is not changed by a model-menu option.
- Exact-head automatic Codex review remains a delivery gate; a missing
  external-fork review pickup must not be treated as a pass or manually
  triggered contrary to the repository policy.
