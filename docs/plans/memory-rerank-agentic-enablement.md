# Memory: enable EverOS rerank (Tier 3) and agentic search

Status: approved by owner 2026-08-14. This document is the source of truth for
the two delivery PRs below. Update it when scope changes materially.

Evidence baseline: Avibe `master @ 7fe4d19f`; pinned EverOS `1.2.3`
(`core/memory/artifact.py:55`). Related decision record:
`docs/memory-everos-reuse-audit.md` (recovery workstream — separate scope).

## Background

Avibe currently pins EverOS at capability **Tier 2** (`[llm]` + `[embedding]`)
and deliberately fails closed on everything above it:

- Every search request hardcodes `enable_llm_rerank: False`
  (`core/memory/everos.py`), and the in-sidecar guard rejects any other value
  (`core/memory/sidecar.py::_validate_search`).
- The generated `everos.toml` blanks `[rerank].model` / `[rerank].base_url`,
  and `_validate_generated_config` asserts they stay blank
  (`core/memory/process.py`). No rerank credentials exist in `MemoryConfig`.
- `method: "agentic"` is unreachable through three independent gates:
  `EverOSPort.agentic_budget_enforced` is a constant `False`
  (`core/memory/everos.py`), `MemoryModule.recall` /
  `resolve_recall_mode` return `memory_capability_unavailable`
  (`core/memory/module.py`), and the sidecar `method` allowlist is
  `{keyword, vector, hybrid}` (`core/memory/sidecar.py`).

EverOS official guidance (docs verified in the EverOS 1.2.3 source tree,
`docs/api.md`, `EVEROS_INTEGRATION_zh.md`, `CHANGELOG.md`):

- Configuring `[rerank]` upgrades the sidecar to **Tier 3**, which unlocks
  `method=agentic`. Tier upgrades require a sidecar restart. `GET /health`
  reports `capabilities.rerank` and a `disabled_features` list
  (`agentic_search` appears there when rerank is absent).
- Recommended usage ladder: hybrid defaults first; `agentic` is reserved for
  complex, decomposable queries (slow/expensive: 1 LLM sufficiency call always,
  +1 LLM multi-query call and 3 parallel retrievals when round 1 is judged
  insufficient, plus 1–2 cross-encoder rerank batches; the episode path also
  reads the cluster snapshot and fetches the full owner corpus).
- **Key nuance:** Avibe's searches are all `user_id`-owned episode/profile
  lookups. For that owner kind, `enable_llm_rerank` is a documented no-op
  (agent_case/agent_skill fusion only) and the automatic cross-encoder hybrid
  lane applies only to agent skills. Therefore the only rerank-quality payoff
  for Avibe's current usage is **`method=agentic`** (which runs its own
  internal cross-encoder rerank loop).
- Agent-side agentic was broken before 1.2.2 and stabilized exactly at 1.2.3
  (skill-shaped rerank passage fix) — the pinned version is the first safe one.

## Goal

1. Add an optional third memory endpoint (**rerank**) to product config and
   plumb it into the EverOS sidecar so it starts at Tier 3 when configured.
2. Open a bounded, opt-in **agentic** recall path for agent callers via the
   `vibe memory search` CLI, keeping `hybrid` the default everywhere.

## Owner decisions (2026-08-14)

1. **Provider-neutral rerank config.** Three user-filled fields
   (`base_url`, `model`, `api_key`) exactly like the existing `llm` /
   `embedding` endpoints. No provider preset, no default vendor.
2. **Agentic exposure = CLI only.** Agents choose the mode per query, guided
   by system-prompt wording ("complex multi-hop recall only"). The Web UI
   memory search stays hybrid-only for now.
3. **Budget gate resolved Avibe-side.** No upstream budget contract exists in
   EverOS 1.2.3. Avibe enforces a wall-clock timeout (≤ 30 s, matching the
   existing `RecallPolicy` budget validation in `core/memory/types.py`) on
   agentic requests and records round telemetry (EverOS logs
   `agentic_search_decision round=round1|round2`). `agentic_budget_enforced`
   becomes true once that enforcement is real.

## Out of scope

- `enable_llm_rerank: true` (no-op for user-owned memories; revisit only if
  agent_id-owned memories are adopted). The sidecar guard keeps rejecting it.
- The EverOS knowledge subsystem.
- UI-initiated agentic search.
- Any change to memorize/flush/rebuild/recovery behavior
  (`docs/plans/memory-rebuild-and-recovery-ladder.md` owns that).

## Delivery plan — two sequential PRs

PR2 starts only after PR1 merges (both touch `core/memory/everos.py` and
`core/memory/runtime.py`; sequencing avoids contract drift and merge risk).

### PR1 — rerank endpoint config + Tier 3 unlock

Scope (indicative files):

- `config/v2_config.py`: `MemoryProcessingConfig` gains an **optional**
  `rerank: MemoryEndpointConfig` (same field names/limits as `llm` /
  `embedding`). Memory may be enabled without it (stays Tier 2). A partially
  filled rerank endpoint is a validation error. Persisted-shape rule applies:
  config files written by released versions (no `rerank` key) must load
  unchanged; add load fixtures.
- `core/memory/runtime.py` (`_provider_kwargs` / `_process_settings`) and
  `core/memory/process.py`: pass rerank settings through
  `EverOSProcessSettings`; inject `EVEROS_RERANK__BASE_URL`,
  `EVEROS_RERANK__MODEL`, `EVEROS_RERANK__API_KEY` into the child env only
  when configured (mirror the llm/embedding pattern; api_key never touches
  disk).
- `core/memory/process.py` generated-config: when rerank is **not**
  configured, keep the current blanked `[rerank]` section (explicit Tier 2).
  When configured, the generated toml + env must deterministically yield a
  constructable reranker — verify EverOS toml/env precedence in the pinned
  wheel and, if toml wins over env for non-secret keys, write the configured
  `model`/`base_url` into the generated `[rerank]` section (api_key stays
  env-only). Update `_validate_generated_config` to assert consistency with
  the persisted product config instead of asserting always-blank.
- `core/memory/everos.py` health parsing: the current strict equality on the
  5-key capability set must tolerate EverOS's typed
  `capabilities`/`disabled_features` health shape without flipping healthy
  sidecars to `memory_provider_response_invalid`. Surface `rerank` capability
  and `agentic_search` disabled/enabled state to the existing UI status
  labels (`ui/.../memoryStatusPresentation.ts` already has both labels).
- UI: memory settings page gains the third (optional) endpoint form,
  reusing the existing endpoint form pattern; all strings via
  `ui/src/i18n/en.json` + `zh.json`.
- Preflight: if a rerank endpoint is configured, extend the existing
  endpoint preflight pattern with a rerank probe consistent with how
  llm/embedding are probed; a missing rerank endpoint must not degrade any
  existing Tier 2 behavior.

Acceptance (PR1):

- With no rerank config: byte-identical generated toml/env versus master
  (regression fixture), all existing memory flows unchanged, health shows
  rerank capability false / agentic_search disabled without erroring.
- With rerank configured (fake/stub endpoint in tests): child env carries the
  three `EVEROS_RERANK__*` vars; generated-config validation passes; health
  snapshot reports `rerank: true`.
- Older persisted config without the `rerank` key loads and saves cleanly.
- `agents.*`-style config save reconciliation still applies (rolling refresh
  path; no second restart requirement).

### PR2 — bounded agentic recall path (after PR1 merges)

Scope (indicative files):

- `core/memory/everos.py`: `_search_data` accepts `method="agentic"`; apply a
  per-request wall-clock timeout (≤ 30 s) for agentic calls;
  `agentic_budget_enforced` returns true iff timeout enforcement is active.
  `enable_llm_rerank` stays `False` in every request body.
- `core/memory/sidecar.py` `_validate_search`: add `"agentic"` to the method
  allowlist; keep every other guard (including `enable_llm_rerank is False`).
- `core/memory/module.py` / `core/memory/types.py`: `resolve_recall_mode`
  routes `agentic` → available only when health reports the capability
  (rerank + llm + embed present, `agentic_search` not disabled); otherwise
  fail closed with `memory_capability_unavailable` exactly as today.
- `core/memory/runtime.py`: keep `_recall_all_projects` rejecting agentic
  (`--project all` + agentic stays unsupported).
- `vibe/internal_client.py` + `core/internal_server.py` + `vibe/cli.py`:
  `vibe memory search --mode {hybrid|keyword|vector|agentic}` (default
  `hybrid`). Parser-backed contract tests required — injected system-prompt
  CLI examples are live callers (see AGENTS.md §7).
- `core/system_prompt_injection.py`: extend the memory CLI guidance with one
  sentence: agentic mode is for complex, multi-hop recall only; default stays
  hybrid.
- Telemetry: log/record agentic usage and result (at minimum: mode, duration,
  success/timeout) so the round-2 rate and latency can be measured from logs.

Acceptance (PR2):

- Rerank unconfigured → `--mode agentic` returns the existing
  `memory_capability_unavailable` error surface (never an EverOS 422 leaking
  through, never a hang).
- Rerank configured (stubbed sidecar in tests) → agentic request reaches the
  sidecar with `method="agentic"`, `enable_llm_rerank=False`, and times out
  cleanly at the budget with a typed error.
- Default CLI behavior byte-identical to master (`hybrid`, limit 8).
- Behavioral deltas documented in user docs: agentic episodes return empty
  `atomic_facts`; with unlimited `top_k` EverOS caps agent kinds at 10.
- `--project all --mode agentic` rejected with a clear error.

## Contracts frozen for both lanes

- Config field names: `memory.processing.rerank.{base_url,model,api_key}`
  (optional section), identical semantics/limits to the existing
  `MemoryEndpointConfig`.
- Child env names: `EVEROS_RERANK__BASE_URL`, `EVEROS_RERANK__MODEL`,
  `EVEROS_RERANK__API_KEY`.
- Recall mode vocabulary: `auto | keyword | vector | hybrid | agentic`
  (existing `RecallMode`); CLI flag `--mode`, default `hybrid`.
- Sidecar request shape: unchanged key set; `method` allowlist grows by
  `"agentic"` in PR2 only; `enable_llm_rerank` remains hard-`False`.
- Agentic budget: wall-clock ≤ 30 s, enforced in `EverOSPort`.

Deviations from these contracts route through the PM, never lane-to-lane.
