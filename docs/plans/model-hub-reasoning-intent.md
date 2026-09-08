# Provider inventory and reasoning intent

Owner decision: 2026-09-08 12:33 +08. This supersedes the Source tier editor
and the resolver's exact-declaration forwarding rule, not model-menu selection,
subscription OAuth, credential replacement, or routing policy.

## Contract

- Provider inventory exposes model identity and inventory actions, not a second
  reasoning configuration surface. This holds for every metadata provenance and
  both discovered and manual models. Remove the empty advanced toggle as well.
- Manual creation needs only the model ID. It sends an empty capability list
  through the existing API and does not fetch upstream inventory.
- Removing controls does not rewrite stored capability metadata, remove models,
  alter existing routes or change another consumer's compatibility API.
- The shared resolver forwards the caller's reasoning request unchanged to the
  managed adapter for each exact Source/model attempt. Capability declarations
  are not an allowlist. Fallback and credential retry start from the same caller
  intent, preserving protocol, headers and non-effort fields.
- Protocol errors keep their existing terminal behavior. No silent fallback or
  synthesized effort is added. Historical stripping telemetry remains readable;
  new resolver attempts do not claim stripping that no longer happens.

## Proxy engine boundary

This change proves forwarding through Avibe's gateway and resolver to its
adapter. It does not claim end-to-end preservation through the pinned proxy
engine's translators. CLIProxyAPI v7.2.149 (`2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`)
has a second capability policy: `internal/modelconfig/model_info.go` marks
configured API-key models non-user-defined, and `internal/thinking/apply.go`
can strip thinking for models with no registered capability or validate a
configured level list. Removing the Avibe filter alone cannot close that gap.

Source model registration is intentionally unchanged here: simply omitting its
thinking metadata can make the engine strip more parameters, not fewer. A
separate engine policy/release change needs verification with a local mock
upstream across protocol families before an end-to-end guarantee is possible.
No live credentials or operational Avibe instance are required or used.

## Evidence

- Unit: every valid provenance shape, empty/mismatched capability metadata,
  direct and nested effort, Anthropic thinking, and absence of reasoning intent.
- Scenario: MH-EFFORT-001 and D10 traverse the HTTP turn gateway and exact
  adapter attempts; existing API capability ownership remains covered by B10.
- UI: inventory and manual-add controls independent of capability metadata;
  existing provider management, mutation settlement, OAuth and replacement.
- Browser: isolated English/Chinese desktop/mobile provider fixture, with no
  live API access. Full local Incus and proxy-engine wire verification remain
  separate evidence layers, not inferred from a fake adapter.
