# Model Hub — Implementation Plan

Status: draft v1 · 2026-07-23 · follows the signed product spec
Spec (signed 2026-07-23): `docs/plans/model-hub.md`
Design source: `../avibe-docs/design.pen` frames `产品改造 V4 01r – 09`
Lane workflow standard: `~/vibe-remote-project/.agents/skills/pr-delivery-loop/SKILL.md`

---

## 0. Ground rules for this effort

- Contracts freeze **before** parallel lanes open (§2). Deviations route through
  the orchestrator, never lane-to-lane.
- Every lane: own worktree (`.worktrees/avibe/<branch>/`), one branch one PR,
  non-draft, Codex-bot review loop owned by the lane, zero unresolved threads
  on head before hand-back. Orchestrator does final review + merge.
- Serializer completeness rule (issue #939 lesson): every new config field must
  be covered by `config_to_payload` and the CI completeness guards in the same PR.
- User-facing strings via `ui/src/i18n/en.json` + `zh.json` (zh copy comes from
  the V4 mocks verbatim; en needs a wording pass — Hub / Direct locked).
- User-facing verification happens in the local Incus regression environment
  only; hub behavior additionally gets scenario-level automation (§5).

## 1. Milestones (dependency order)

| M | Content | Exit gate |
| --- | --- | --- |
| M0 | **De-risk spikes** (serial, small) | spike reports merged as docs |
| M1 | Hub core: engine runtime + config schema + REST API + event log | contract tests green; engine runs supervised on 127.0.0.1 |
| M2 | Backend injection: Claude env / Codex `-c` / OpenCode overlay + Direct-mode preservation & mode switch | per-backend scenario tests green in both modes |
| M3 | UI: Models page, source dialogs, OAuth connect, backend supply-mode card, model menus, migration dialog | pixel check vs design.pen; `npm run build` gate |
| M4 | Migration backend (scan/import/re-auth) + wizard & banner triggers + empty states | non-destructive property proven by tests (originals byte-identical) |
| M5 | E2E scenario sweep + Incus regression + user docs (avibe-docs EN/ZH) | owner acceptance checklist (spec §11) passes end-to-end |

M0 spikes:
- **S1 engine capability re-verification** (spec §8): pin current CPA release,
  verify from source: OAuth vendor list & flow shapes (map to connect forms
  A/B/C), protocol conversion matrix, model listing, auth file formats,
  management API. Output: `docs/plans/model-hub-engine-survey.md` + pinned
  version/SHA256. Blocks M1 scope decisions.
- **S2 subscription-reuse ToS & billing review** (spec §10.2): product-risk
  memo per vendor. Blocks *defaults* for subscription flows, not the build.
- **S3 runtime-dependency reuse audit**: confirm the Show Runtime managed-
  dependency machinery (manifest, download, verify, prepare) generalizes;
  decide reuse vs sibling implementation. Half-day, folds into S1 report.

## 2. Contracts to freeze (files, before lanes open)

Location: `docs/plans/model-hub-contracts/` — JSON Schema + one example payload
per type. UI and backend lanes both cite these; changes go through the
orchestrator only.

1. `source.schema.json` — Source: id, kind(subscription|api_key), vendor,
   protocol, base_url?, display_name, billing(monthly|metered), state
   (active|standby|cooldown{retry_at}), usage(cycle_pct?|month_spend?),
   models[] (supplied model ids), custom_models[].
2. `priority.schema.json` — ordered source-id list (single global list).
3. `agent-supply.schema.json` — per backend: mode(hub|direct), menu_kind
   (fixed|open), current{model_id, source_id}?, mappings[] (fixed-menu only:
   builtin_id → target_model_id), menu{featured|full, checked_ids[]} (open only).
4. `resolution-event.schema.json` — 最近切换 entry: ts, agent, from_source,
   to_source?, reason(quota|error|recovery|cooldown_skip), billing_impact?.
5. `oauth-flow.schema.json` — flow_id, form(A_paste_code|B_device_code|
   C_callback_replay), state machine states, url?, device_code?, error?;
   mirrors existing `BackendOAuthPanel` semantics.
6. `migration-scan.schema.json` — per backend: detected items(kind, masked
   detail, action(import|reauth|controlled_import), selected).
7. `api.md` — REST endpoint list (paths, verbs, request/response schema refs,
   error envelope): sources CRUD + test/discovery, priority reorder, agent
   mode switch, mappings CRUD, menu config, custom models, events feed,
   migration scan/apply, oauth start/status/submit/cancel.
8. `opencode-overlay.md` — generated provider entries (standard vendor ids +
   `custom/`), transport redirection, gateway token injection, serve
   config-hash restart rule; identifier stability invariant stated as a test
   requirement.

## 3. Lanes

Dispatch preference (owner 2026-07-13): balance claude/codex; rigor-critical
backend → codex-lean; product-voice / design-fidelity UI → claude-lean.
Every brief cites: spec, this plan, the contracts dir, repo `AGENTS.md`,
`pr-delivery-loop` SKILL — all by absolute path — plus explicit file scope and
no-touch zones.

| Lane | Executor lean | Scope (files) | Depends on |
| --- | --- | --- | --- |
| L1 engine runtime & credentials | codex | new `vibe/model_hub_runtime/` (or Show-Runtime generalization per S3), engine supervisor, key/token generation, fail-closed + Direct escape | S1/S3 |
| L2 config schema + API + events | codex | `config/v2_config.py` (sole owner), new `core/handlers/model_hub*.py`, REST endpoints, serializer guards, event log store | contracts; L1 interface |
| L3 backend injection & modes | codex | `modules/agents/claude_agent.py` / `codex/` / `opencode/` (env, `-c`, overlay + serve hash), mode plumbing; Direct path untouched-by-default proof | L2 API |
| L4 UI: Models page + sources + OAuth connect | claude | `ui/src/components/settings/models/**` (new dir), add-source menu/dialogs, OAuth dialog reusing `BackendOAuthPanel` shell | contracts |
| L5 UI: menus + mapping + backend card + migration dialog | claude | `ui/src/components/settings/models/menus/**`, backend page 供给方式 card, migration dialog | contracts; L4 shared primitives |
| L6 migration backend | codex | scan/import of native configs (Claude settings.json, Codex auth.json controlled import, opencode providers), re-auth orchestration, non-destructive tests | L2 |
| L7 scenario tests + regression + docs + availability guard | either (split) | `tests/scenarios/model_hub/**` catalog + harness, Incus verification script hooks, `avibe-docs` user docs EN/ZH; **engine-asset availability guard** (decided 07-23: mirror pinned CPA assets into Avibe-owned release storage pre-GA, manifest → mirror URLs, upstream as provenance; same manifest-verified backup/recovery pattern as Show Runtime) + **platform-set expansion** (07-23 13:13, from L1 review: add linux-arm64 / darwin-x64 assets — pin + SHA256 + schema platform-enum rev — together with the mirror work; until then unsupported hosts fail closed with Direct as escape hatch, scenario `model_hub_engine_platform_unsupported`) | all |

No-touch zones: only L2 edits `config/v2_config.py`; only L3 edits
`modules/agents/**`; L4/L5 split `ui/src/components/settings/**` by
subdirectory as listed; nobody edits contracts in-lane.

Sequencing: S1–S3 → freeze contracts → L1+L2 start (codex ×2) with L4 in
parallel (claude); L3/L5 join as their dependencies stabilize; L6 after L2;
L7 continuous, finalizes last. Rough sizes: L1 M, L2 L, L3 M, L4 L, L5 M,
L6 M, L7 M.

## 4. Product gates

- **Resolved (owner 2026-07-23 10:33, after S2):** default = hybrid supply —
  subscription sources are `native_cli` channel (per-turn channel dispatch,
  CLI-sanctioned OAuth); hub-held subscription login (incl. Claude-in-engine)
  ships ONLY behind `subscription_hub_experimental` with explicit ban-risk
  consent (copy from S2 §9) and per-source opt-in. API-key paths ungated.
  L3 owns channel dispatch; L2 owns the flag + consent recording; L4 owns
  the consent dialog + 实验 marking.
- Cross-vendor auto-fallback remains default-off experimental (spec §9);
  no lane builds UI for it beyond the advanced placeholder row.
- Mode default: existing users stay in Direct until they migrate (no silent
  flips); fresh installs default to Hub. Wizard/banner triggers per spec §6.

## 5. Verification layers

- **Unit**: resolution projection, serializer completeness, overlay generation
  (identifier stability invariant), migration parsers.
- **Contract**: REST API against `model-hub-contracts` schemas (both
  directions), engine adapter against pinned engine version.
- **Scenario**: `tests/scenarios/model_hub/catalog.yaml` — at minimum:
  quota-exhausted failover & recovery switchback, priority reorder takes
  effect next turn, mapping applies to CC only, OpenCode identifier stability
  across mode switch, migration non-destructiveness, OAuth forms A/B/C happy
  path + timeout/cancel. Scenario IDs appear in PR descriptions.
- **Behavioral (Incus)**: real backend turns in both modes; 最近切换 log
  reflects induced quota errors; UI pixel pass vs exported V4 frames.

## 6. Open items carried from spec §10

1. Remaining mocks (empty state / Dark / mobile / copy pass) — feed L4/L5;
   not blocking lane start (contracts govern behavior, mocks govern polish).
2. en.json wording pass (Hub / Direct locked; rest of EN copy during L4/L5).
3. `design.pen` V4 frames must be saved (Cmd+S) before L4/L5 dispatch — lanes
   verify against the exported frames.

## 7. Kickoff checklist (orchestrator)

- [ ] Owner approves this plan (lanes, sequencing, gates).
- [ ] design.pen saved; V4 frames re-exported into a stable reference dir.
- [ ] S1–S3 dispatched (S1/S3 codex, S2 research either).
- [ ] Contracts dir authored from S1 output; frozen and announced.
- [ ] L1/L2/L4 briefs written (scope, no-touch, contracts, review protocol) and dispatched.
