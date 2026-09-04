# Model Hub — Implementation Plan

> **2026-09-04 override — OpenCode v4 (`model-hub.md` §4.8 v4, `opencode-overlay.md` v4,
> `backend-model-catalogs.md` v3).** This document is a historical lane plan. Wherever a row,
> acceptance criterion, fixture, or handoff below says that an OpenCode menu id is
> `provider/model` or `vendor/model`, that OpenCode add-time matching uses an exact checked
> identifier or a unique bare-suffix match, or that the overlay carries normalized provider ids,
> the v4 rule governs instead: OpenCode menu ids are bare canonical model ids matched by literal
> equality, each OpenCode row carries `native_protocol`, and the Gateway-mode overlay generates
> one provider per downstream protocol (`avibe-openai`, `avibe-anthropic`) with
> `enabled_providers`; the `standard_vendors` projection of agent-supply is removed (catalogs
> spec v3, C9), so any disposition below that keeps it mode-independent is retired with it.
> The v4 backend lane (catalogs spec v3, delivery plan) owns the launch
> seam this shape changes — `modules/agents/model_hub.py` (`OpenCodeOverlay` carries a provider
> set, not one `provider_id`), `modules/agents/opencode/server.py` (overlay provider set,
> runtime-catalog filtering to that set), `modules/agents/opencode/agent.py`
> (`resolve_opencode_model_dict` addressing from the row's `native_protocol`) — and their tests,
> irrespective of the closed I1/I7/L3 assignments recorded below.


Status: **v3.0 implementation addendum** · 2026-08-09 · supersedes v2 lane authority;
follows product spec v3.0
Spec: `docs/plans/model-hub.md`
Design source: `../avibe-docs/design.pen`; the v3 interaction draft is owner-approved
as I4's implementation baseline, while I4 still owes production-complete desktop/mobile
states
Lane workflow standard: `.agents/skills/pr-delivery-loop/SKILL.md`

> **Authority banner (2026-08-07, amended 2026-08-09).** The original v1 milestones and most narrative
> below are historical records of the dormant build. **§3 and §8 are the binding
> exceptions.** In §3, only 「v3 current lanes」 is the active lane plan; the retained
> L0–L6 material is explicitly historical. In §8, AC-1 through AC-21 remain in their
> original order, AC-19 is amended in place by the final vocabulary, and AC-22 onward
> append the v3 requirements.
> contract files in this consolidation. K1 owns the final contract shape and its mechanical closure
> on this tested PR head; I1 owns the remaining prelaunch consumers
> and evidence under §3's exclusive file split.

---

## 0. Ground rules for this effort

- The terminal-version/protocol-enum closure in §8 must coexist on one tested PR
  head. Commit boundaries are not a CI gate. The remaining final-shape consumers and
  evidence may land in later PRs, but the complete handoff is a release prerequisite.
  After I1 lands the contract set, it is read-only to downstream lanes; deviations route
  through the orchestrator, never lane-to-lane.
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
2. ~~`priority.schema.json` — ordered source-id list (single global list).~~
   **Removed in contract v2**: the global list is gone; the file remains only as a
   v1 tombstone. The order lives in `agent-supply.schema.json` → `sources`.
3. `agent-supply.schema.json` — per backend: mode(hub|direct), menu_kind
   (fixed|open), current{model_id, source_id}?, mappings[] (fixed-menu only:
   builtin_id → target_model_id), menu{featured|full, checked_ids[]} (open only);
   **v2** also `sources`{policy(follow|custom), order[]} — this backend's ordered
   source subset — plus `supply_status` and `model_supply`.
4. `resolution-event.schema.json` — 最近切换 entry: ts, agent, from_source,
   to_source?, reason(quota|error|recovery|cooldown_skip), billing_impact?.
5. `oauth-flow.schema.json` — flow_id, form(A_paste_code|B_device_code|
   C_callback_replay), state machine states, url?, device_code?, error?;
   mirrors existing `BackendOAuthPanel` semantics.
6. `migration-scan.schema.json` — per backend: detected items(kind, masked
   detail, action from `model-hub.md` §6's authoritative import-action matrix,
   selected).
7. `api.md` — REST endpoint list (paths, verbs, request/response schema refs,
   error envelope): sources CRUD + test/discovery, per-backend source order
   (v2: `PUT /api/models/agents/<backend>/sources`, replacing the v1 global
   `PUT /api/models/priority`), agent mode switch, mappings CRUD, menu config,
   custom models, events feed, migration scan/apply,
   oauth start/status/submit/cancel.
8. `opencode-overlay.md` — **v4 (2026-09-04):** bare menu ids, one generated provider per
   downstream protocol (`avibe-openai`, `avibe-anthropic`) with `enabled_providers`,
   transport redirection, gateway token injection, serve
   config-hash restart rule; identifier stability invariant stated as a test
   requirement.
9. **v2 additions** (2026-07-29): `agent-chain.schema.json` (capability chain per
   (agent, model)), `probe-result.schema.json` (「试跑一次」), and
   `turn-provenance.schema.json` (per-turn model@source record). All three are
   read-only surfaces with no v1 implementation, so every field is required.

## 3. Lanes (v3 current plan, approved 2026-08-07; revised 2026-08-09)

### v3 current lanes (binding)

The v3 batch separates product truth and fidelity evidence from implementation
lanes with exclusive file ownership. K1 and K2 may run in parallel. K1 owns this
orchestrator-authorized S-1 contract consolidation and the generated authority guard;
after this head, those contract files transfer to I1 and then freeze for downstream
lanes. The K5/K6/I1–I7 split is **tentative pending the UI lane's design-to-spec reconciliation table**;
that reconciliation may change content but is not expected to change file ownership.

| Lane | Executor preference | Exclusive scope | Depends on |
| --- | --- | --- | --- |
| **K1 spec v3 sync** | codex | This PR's planning documents plus the owner-authorized final S-1/protocol edits under `docs/plans/model-hub-contracts/`, their byte-mirror/version consumers, and the live-input authority guard. It does not implement the remaining Gateway runtime | — |
| **K2 conversion fidelity** | codex | Preserve the recorded M0 measurements and go/no-go rows; record the 2026-08-08 owner waiver of an official-API attribution re-test and the accepted relay-attributed reasoning degradation without rewriting the evidence or adding product/UI scope | — |
| **K5 UI specification closure** | codex | Sole ownership of `docs/plans/model-hub-ui-spec.md` across three serialized rounds: (1) the frame registry in in-flight PR #1336; (2) K4 gap-reference upgrades plus the AC-49 withdrawn-evidence cleanup and AC-50 live-backoff presentation registration; (3) the G-32 interaction specification. K5 edits no contract, implementation, locale, or UI file. **K3 residual-queue transfer recorded by PM ruling 2026-08-11 23:09.** | round 1 is independently in flight; round 2 waits for K4 merged; round 3 waits for rounds 1 and 2 merged and must merge before I4's second increment |
| **K6 post-#1312 Turn-outcome contract closure** | codex; dispatch at activation | One bounded contract round, dispatched by the orchestrator only after K4 and #1312 merge. Exclusive scope is `docs/plans/model-hub.md` §4.5's Turn-outcome matrix/consumer segment; `docs/plans/model-hub-contracts/{api.md,turn-provenance.schema.json,resolution-event.schema.json,mirror-registry.json}`; and the corresponding binding/ledger rows in this document. It closes only registered G-34 plus any §4.5 gap row left by the merged #1312 head, changes either schema only when the selected outcome/copy/payload requires it, and owns no implementation, test, UI, or locale file. **Single-round scope and dispatch edge fixed by PM ruling 2026-08-12 02:00.** | K4 + #1312 merged; must merge before I7 starts |
| **I1 contracts and config core** *(tentative)* | codex | All files under `docs/plans/model-hub-contracts/`; `config/v2_config.py`; `core/controller.py`; `core/handlers/model_hub/adapter.py` and the byte-identical contract interface; `core/handlers/model_hub/{service,resolver,classification,errors,provenance,rpc,request,events}.py`; `vibe/{ui_server,model_hub_client}.py`; `tests/test_model_hub_config.py`; `tests/test_model_hub_api.py`; resolver unit coverage through #1312; `tests/test_model_hub_injection.py`; `tests/test_controller_model_hub_gate.py`; `tests/test_model_hub_l3.py` through #1312's same-tested-head transition. I1 also finishes #1312's already-open shared native-launch-module edits before the transfer recorded solely in I7's row. `tests/test_multi_platform_runtime.py` and `tests/test_claude_cli_path.py` are read-only dependency checks during I1: I1 repairs breakage caused by removed resolver symbols but does not refactor them. I1 owns the §8 final contracts, same-tested-head closure, §4.3 configured-chain executor, serializers, API envelopes, shared validation, and the default-off release gate while #1312 is active. After #1312 merges, only K6's bounded contract scope transfers immediately; the implementation/test files explicitly named by I7 remain unavailable to I7 until K6 merges, and the other I1 files retain their stated owner. **Turn-outcome transfer revised 2026-08-12 02:00.** | K1. **Merges first** |
| **I2 runtime transport** *(tentative)* | codex | `core/handlers/model_hub/turn_gateway.py`; `vibe/model_hub_runtime/**`; `tests/test_model_hub_runtime.py`; after #1312 merges, `tests/test_model_hub_l3.py` transfers to I2 for managed-Gateway runtime behavior, except for I7's one authorized AC-50 strict-xfail marker deletion. I2 owns authoritative raw shaped-error and `stream_started` facts produced by the managed Gateway only; this boundary flips at the first user-visible model-output byte, never at HTTP status, headers, or another response byte. Native backend callback facts and every classification/state decision belong to I7. The K4 increment additionally owns AC-38's install worker, persisted status/lease, orphan reconciliation, and runtime-level no-op assertions without moving the HTTP/API boundary out of I7. **Native failure ownership revised 2026-08-11 22:37.** | I1 + K4 merged; `test_model_hub_l3.py` transfer waits for #1312 merged |
| **I3 subscription custody and native import** *(tentative)* | codex | `core/handlers/model_hub/{oauth,native_oauth,revocations,migration}.py`; `tests/test_model_hub_oauth.py`; `tests/scenarios/model_hub/test_model_hub_migration_scenarios.py`; `tests/scenarios/auth_setup/catalog.yaml`; `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`. The migration scenario and complete auth-setup catalog/test loop are the sole carve-outs from I5's scenario tree. The K4 increment owns AC-48's OAuth registry: it atomically claims the exact nonce/vendor/channel tuple before provider work, coalesces concurrent retries to one pending terminal result, releases failure/task cancellation only before a flow exists, atomically converts success to one echoed-nonce flow, retains an explicitly canceled nonce-bearing flow as the same terminal `cancelled` flow until its existing expiry, and releases the tuple at expiry. AUTH-SETUP-210 blocks the first provider call, overlaps the same-nonce retry, and proves provider invocation exactly once. For AC-52, AUTH-SETUP-109 is the Hub-held re-auth closed loop: missing/false acknowledgement rejects before any adapter/provider call, true acknowledgement starts exactly one Hub flow, and terminal status/repair projection agrees; I7 owns the API/service gate and I5 only consumes the completed scenario. **Explicit-cancel totality revised 2026-08-12 00:37; Hub re-auth scenario binding revised 2026-08-12 01:30.** | K4 merged + #1312 merged |
| **I4 Sources / Gateway UI** *(tentative; one lane, two increments)* | codex | `ui/src/components/settings/models/**`; `ui/src/i18n/*.json`; `vibe/i18n/*.json`; UI tests. Both i18n trees are I4-exclusive. The current increment consumes the already-specified install/unsupported-host surfaces for AC-38/AC-43; each model row may open frame 02, render the drawing, and consume the chain read projection, but no chain-write caller is wired and Save remains disabled. Exact-chain editing and its UI tests belong to the second increment only after K5's third-round G-32 specification merges. That second increment sends visible noninterrupting hop removal as one ordinary PUT and echoes both refusal-plan arrays unchanged only when protected supply would be interrupted, after I7 freezes both fixtures; the current increment continues to consume master's pre-I7 refusal shape and must not add inactive confirmation fields. It consumes AC-36/37/39–42/44–50 only after K5 round 2 changes their G rows from gaps to contract references and registers the live-backoff presentation; AC-51's read-before-retry client rule activates in the second increment after I7 freezes the deletion/recreate fixture. AC-53's materialization-error `interrupted_pairs` consumer activates only in that second increment after K5 round 2 registers E6 and I7 freezes positive/negative API payload fixtures. AC-54's `accept_unavailable_inventory` producer also belongs to the second increment after K5 round 2 names state ⑤ and I7 freezes both server-result cells; clean creation and pull-origin state ⑤′ never emit it. This is an ordering split inside I4, not a new lane identity. **Revised through the 2026-08-12 04:40 PM ruling.** | current increment: I1 envelopes + K4 contracts; second increment: K5 rounds 1–3 merged + frozen I7/I2/I3 fixtures |
| **I7 contract-completion implementation (backend)** | codex | After K6 merges, exclusive ownership transfers from I1 for `config/v2_config.py`; `core/handlers/model_hub/{service,resolver,classification,rpc,request,errors,events}.py`; `modules/agents/model_hub.py`; `vibe/{ui_server,model_hub_client}.py`; `tests/test_model_hub_config.py`; `tests/test_model_hub_api.py`; and `tests/test_model_hub_resolution.py`. For the Model Hub failure-callback seam only, I7 also exclusively owns `modules/agents/base.py`, `modules/agents/claude_agent.py`, `modules/agents/codex/{agent,event_handler}.py`, `modules/agents/opencode/{agent,poll_loop}.py`, exact cross-backend phase fixtures in `tests/{test_claude_agent_sessions,test_codex_agent,test_opencode_server}.py`, and mechanical signature consumers in `tests/{test_claude_cli_path,test_multi_platform_runtime}.py`. I7 owns AC-35–AC-47 and AC-50–AC-54's backend producers/tests plus AC-48's API/service half. `service.py` is the sole shared guard-plan owner and implements every guard-totality row and the error-to-required-array relation; it also enforces AC-52's both-channel re-auth acknowledgement before any OAuth adapter call and AC-53's materialization-error response condition, and AC-54's unavailable-inventory consent gate against the repeated server observation. `modules/agents/model_hub.py` owns the native classification/state decision: backend callbacks supply raw failure shape and phase only, and no native path writes a persistent network cooldown. The transferred resolver/classifier/event boundary owns `retired: false` filtering and all four network-failure cells; its post-output executor/provenance fixture consumes K6's G-34 row without inventing an outcome id, copy key, or payload shape. I7 delegates OAuth tuple claim/coalescing and terminal-flow expiry to I3's registry; its service/API half branches explicit cancellation by nonce presence, retaining the I3 flow for nonce-bearing reconciliation and forgetting only no-nonce flows. Its first AC-50 mechanical action is the sole owner-authorized I2-file exception: delete the strict xfail marker from `tests/test_model_hub_l3.py::test_probe_transport_failures_await_ac50_backoff_contract` in the same commit that makes its forward contract assertions XPASS; it otherwise consumes I2 managed-Gateway facts and I3 OAuth fixtures without editing their files. It never edits contracts, UI/i18n, scenario trees, or any other I2–I5 file. **Revised through the 2026-08-12 04:40 PM ruling.** | K4 + #1312 + K6 merged; every listed transfer and native callback seam activates only after all three merge |
| **I5 scenario validation** *(tentative)* | codex | `tests/scenarios/model_hub/**` except I3's native-import scenario; `tests/scenario_harness/**` | I1–I4 + I7 |
| **I6 release-gate removal** *(tentative)* | codex | After I5 merges, exclusive ownership transfers from I1 for `is_model_hub_enabled()` and every call site, `_init_model_hub()`, `core/controller.py`, and `tests/test_controller_model_hub_gate.py`. Delete the gate function rather than leaving a constant-true shell; invert the gate test so the final default state always constructs the v3 aggregate | I5 merged and all Model Hub scenarios green |

**Executor ruling (owner, 2026-08-09; I7/K5/K6 registry completed 2026-08-12).** Every new K5, K6, and I1–I7 lane uses
`codex`, including frontend work. Visual-fidelity risk is closed by process rather than
by changing executor: `design.pen` is the pixel-level authority, and a separate Codex
acceptance thread compares the built UI against its design frames. Already-dispatched
Claude lanes complete their current specification work; subsequent implementation uses
the table above.

**Merge order:** K1 first for product authority; K2 remains independent evidence. K5
round 1 is independently in flight as PR #1336; round 2 starts only after K4 merges, and
round 3 starts only after K5 rounds 1 and 2 merge. I1 must merge before I2–I5. K6 is
dispatched only after #1312 merges and must merge after K4 and before I7; I7's whole lane,
including every AC-50 producer and fixture, therefore depends on K4 + #1312 + K6 rather
than a partial activation. I2 and I3 may proceed under their existing edges while K6 runs;
I7 starts only after the three-way edge closes. I4's current increment may
consume the already-specified install/host contracts and render frame 02 from the chain
read projection with Save disabled; its second increment waits for the G-32 specification
in K5 round 3 and frozen I7/I2/I3 fixtures. I5 closes integration after I1–I4 and
I7; I6 removes the release gate only after that evidence is merged. Every lane follows
`pr-delivery-loop`; no lane merges itself.

**Exclusive-file circuit breakers.** `service.py`, `classification.py`, `events.py`,
`tests/test_model_hub_config.py`, `core/handlers/model_hub/resolver.py`, and
`tests/test_model_hub_resolution.py` are I1-only through #1312 and remain unavailable to
K6, then transfer to I7 only after K6 merges under the explicit row above. The §4.5
Turn-outcome matrix/consumer segment and
`docs/plans/model-hub-contracts/{api.md,turn-provenance.schema.json,resolution-event.schema.json,mirror-registry.json}`
are I1/#1312-only until #1312 merges, K6-only for its one bounded registered-gap round,
and read-only to I7. `docs/plans/model-hub-ui-spec.md` is K5-only for all three
serialized rounds; K4 and I1–I7 consume it without editing it. Both adapter copies move
together under I1.
`modules/agents/model_hub.py` is I1-only through #1312 and then transfers to I7 only after
K6 merges; it does
not transfer to I2. `tests/test_model_hub_injection.py` remains I1-owned. `core/controller.py`
and `tests/test_controller_model_hub_gate.py` remain I1-only until their explicit I6
release-gate transfer. The narrow Model Hub failure-callback seams and exact fixtures named
in I7's row belong only to I7 after K4, #1312, and K6 merge; no ownership of unrelated behavior
in those backend files is implied. `tests/test_model_hub_l3.py` has one serial ownership
transfer: I1 alone updates its versioned contract/provenance fixtures through #1312; after
#1312, I2 owns the file except for I7's one AC-50 strict-xfail marker deletion. I2–I5 stop and report to the orchestrator if their work
requires any I1/I7 file not explicitly transferred to them. `ui/src/i18n/*.json` is I4-only: I3 or I5 requests any new
migration-scenario note key through the orchestrator and never edits UI i18n locally.
The authority guard discovers every Python file outside the resolver package that
imports `core.handlers.model_hub.resolver` and requires each discovered file to occur
in exactly one binding lane scope; the importer list is never copied into the guard.
For the K4 implementation batch, I7's listed files and narrow backend callback seams
activate only after K4, #1312, and the dispatched K6 round merge.
I7 must not edit I2 runtime files, I3 OAuth/custody files, I4 UI/i18n files, or I5
scenario files; those lanes consume the frozen contracts and exchange fixtures instead.
Any newly required implementation file stops the lane for an ownership ruling rather
than widening I7 by inference.

**Final-contract anti-drift rule.** I1 owns every final-shape edit under
`model-hub-contracts/` and the exact mechanical closure in §8 through #1312. K6 is the
sole orchestrator-dispatched exception after #1312 and may edit only its registered
Turn-outcome closure scope; after K6 merges, those files are read-only to I2–I7. All
remaining handoff rows still must land before release;
an implementation-proven mismatch
escalates for an orchestrator-owned targeted revision; it is never patched by the
discovering lane.

**ChatGPT pre-default vendor gate.** Immediately before I1 encodes ChatGPT Hub custody
as the product default, I1 re-verifies the current OpenAI terms and product language
identified in `model-hub-tos-review.md` §2.1 and §3.1 under §11's timing gate, records dated evidence in its PR,
and asks the owner to adjudicate any material change. A failed or inconclusive check
blocks only that default; it does not silently substitute another recommendation or
block API-key implementation. K2 fidelity work and I5 post-implementation evidence do
not own this precondition.

### Historical v2 lane plan (non-binding; retained for traceability)

The v1 lane split (L1–L7, built for the global priority list) is **superseded** and
lives in git history at `251f8e7b`. The lane IDs below are the v2 ones, and every
lane reference inside AC-1 through AC-21 means one of these historical implementers.
Nothing in this subsection overrides K1/K2/I1–I5 above.

Dispatch preference (owner 2026-07-13): balance claude/codex; rigor-critical and
cryptography-adjacent backend → codex; product-voice / design-fidelity UI → claude.
Every brief cites: spec, this plan, the contracts dir, repo `AGENTS.md`,
`pr-delivery-loop` SKILL — all by absolute path — plus explicit file scope and
no-touch zones.

| Lane | Executor | Scope | Depends on |
| --- | --- | --- | --- |
| **L0 spec sync** | claude | `docs/plans/**` only: commit AC-14 to AC-21 into §8, apply the AC-14 to AC-17 record repairs, sync spec §4.5 and §8 to the push cut, write this lane plan | — |
| **L1 per-agent order core** | codex | `config/v2_config.py` per-backend `sources{policy, order}` + serializer completeness guards, resolver order resolution, **the per-Agent read projection on `GET /api/models/agents` — both its `api.md` term and the `service.py` assembler that fills it** (AC-9, owner ruling 07-29 16:50: one more member of this row's agents-payload extensions, because the projection is computed from resolver state this lane already owns), `PUT /api/models/agents/<backend>/sources` **exposed through the shared route layer** (see 「No-touch zones」), removal of the v1 global priority endpoint and `ModelHubConfig.priority_order` (which is what finally deletes the `priority.schema.json` tombstone, owner ruling #6), **the migration rewrite** (below), **the ordinary source-creation commit paths** — `_commit_new_source_locked` and both its `api_key` and OAuth callers, which today append to the very field this row deletes and return no `adopted_by`, so this row owes the automatic insertion into every eligible `follow` order plus the `adopted_by` projection `api.md` already freezes and L5 already consumes, with tests covering `follow` adoption and the `custom` new-source hint for **ordinary creation** as well as migration (ruling 07-29 17:41, review round 16 — the row previously named only `migration.py`, which would have left source creation writing a deleted field), the eligibility and mode-projection contract fixes — **AC-19, AC-20, AC-21** — and **the coordinated `contract_version: 3` bump** every later lane rides. **The bump carries the whole v3 set, not just L1's own ACs** (§3 single-freeze ruling): every frozen-file edit any §8 criterion needs — L2's and L3's surfaces included — is authored here, so that after this lane merges no other lane edits `model-hub-contracts/**` at all | L0 |
| **L2 repair paths & guards** | codex | replacement invariants on `PUT …/credential` and `POST …/reauth` **and those routes' exposure on the shared route layer** (see 「No-touch zones」), confirm-before-irreversible native re-auth **and its `tests/scenarios/auth_setup/` closed-loop case** (below), one shared `would_interrupt` implementation behind DELETE and `PUT …/credential` **only** (the native re-auth confirmation is unconditional, not guard-computed — AC-13's ruling: what a narrower account supplies is knowable only *after* the login, which is the irreversible act; corrected 07-29 17:41, review round 16), protected-set membership — **AC-2, AC-3, AC-5, AC-8, AC-12, AC-13**. Implements against L1's published v3; **edits no file under `model-hub-contracts/`** | L1 |
| **L3 provenance, probe & chain** | codex | turn-provenance write path + read route, probe route, chain projection, resolution-event emission and its record accuracy — **AC-1, AC-4, AC-7, AC-10, AC-18**, plus the record half of **AC-6, AC-9 and AC-11**, plus **emitting the in-turn error copy** spec §4.5 makes normative (below), plus **the turn-lifecycle seam** that makes successful and canceled turns reachable at all, and — per the 07-29 round-7 ruling, amended 14:04, replaced 14:39, settled 15:07, narrowed 15:42 and cut back 16:20 — **four seams** carrying the **process-scoped** gateway credential: the **launch env-build seam** (`modules/agents/model_hub.py`), the **gateway authorization path** (`core/handlers/model_hub/turn_gateway.py`), **the Codex call site for cwd threading only** (`request.working_path` into the launch resolution), and **the Claude call site for session-identity threading only** (07-29 16:20 — the same additive-parameter class, not adapter surgery). **Plus the pre-launch supply-failure mapping**: a supply failure raised out of the launch resolution — `ModelHubService.resolve` raises `mapping_target_unavailable` / `engine_down` before any backend process, transport or turn gateway exists — is mapped to §4.5's copy forms **once, at the resolve boundary** in `modules/agents/model_hub.py`, the seam this row already owns, so all three backends inherit it instead of three adapters each authoring product copy (ruling 07-29 17:41, review round 16 — see below). Plus its own routes on the shared route layer (provenance, chain, probe — see 「No-touch zones」). **L3's FIRST deliverable is the correlation-mechanism design note** (07-29 16:20): the registry, scope keys, FSM lookup and their coverage are designed there, not here, bounded by the invariants below. Implements against L1's published v3; **edits no file under `model-hub-contracts/`** | L1 |
| **L4 UI: overview & order** | claude | Models page overview, per-backend source order editor (跟随推荐 / 自定义), source rows, status pills — design frames V6 01–04 plus M01/M02; the OpenCode drawer follows the V6 02 pattern rather than inventing a third; **plus AC-9's page attribution — the consumer of L1's per-Agent projection, with its UI test** (07-29 14:39 ruling), **plus AC-7's Direct-mode drawer gate and AC-18's deleted-source feed state, each with its UI test, plus `ui/src/i18n/{en,zh}.json` ownership for AC-19's eligibility-reason keys** (07-29 15:07 ruling; those reasons render in L4's own surfaces — source rows and drawer gray-outs — so the keys follow the surface. L5 appends its own journey strings under normal i18n practice; no shared-file conflict is expected, and if both lanes add keys they coordinate at symbol level, not by locking the file). **The razor, recorded once: a *status* surface — what is broken right now — is L4's; a *journey* is L5's, and reading a status is not a journey.** That is the same test the 14:39 AC-9 ruling applied, generalized at 15:07 so later surfaces do not each need their own ruling | L1 |
| **L5 UI: supply journeys** | claude | the `adopted_by` loop, confirm dialogs (delete, elective replacement, re-auth irreversibility), dry-run, chain preview, and the Models-page 需处理 state the in-turn copy points at; **the Models-page action that invokes AC-3's recovery route** (07-29 ruling — the topped-up user's path runs through this action, and AC-3's scenario drives it through the action rather than calling the route directly); the **in-turn error copy's UI wiring** on the Web side (the copy itself is L3's — see §3); quota projection optional. **No provenance surface at all** (owner ruling 07-29 14:03 — see the cut list) | L2, L3, L4 |
| **L6 integration close-out** | either | AC checkpoint across all of §8, scenario catalog completion, Incus regression evidence, user docs EN/ZH in `avibe-docs` | all |

**Migration belongs to L1** (orchestrator ruling, 07-29 — review round 1 of the L0 PR
found no lane owned it). `core/handlers/model_hub/migration.py` currently appends every
imported source to `updated.priority_order`, the exact field L1 deletes, so the two
changes are one change: the append is **deleted**, imported sources join each backend's
order through `follow`-mode auto-inclusion, and a `custom`-policy agent gets the
new-source hint instead of a silent insertion. Leaving it to a later lane would mean
merging a migration that writes a field that no longer exists.

**In-turn copy is backend work, not UI work** (L0 proposal, 07-29 review round 3 —
**orchestrator confirms at merge**, since it moves scope between lanes). The push cut
made the in-turn copy the only actionable notice a supply failure produces, and every
platform has to produce it: Slack, Discord, Telegram, Lark/Feishu, WeChat and Web all
render the same turn result. That path is Python — `core/handlers/model_hub/turn_gateway.py`
where Model Hub meets the live turn, `core/backend_failure.py` / `core/message_output.py`
where a terminal outcome becomes a message, and `vibe/i18n/{en,zh}.json`, which repo
`AGENTS.md` requires for every backend user-facing string and which today holds no
supply copy at all. L5's UI scope is the Models page (§3 records the one exception the
07-29 conversation-surface ruling added), so leaving the copy there would have shipped it
to Web only and left every IM user with a
generic failure — the exact regression the cut's 「surface it in the turn」 promise
cannot afford. It moves to **L3**, which already owns the moment it must be emitted at:
the same turn resolution that writes provenance and the resolution event. L5 keeps the
Models-page 需处理 state the copy points at, and the dialogs.

**This file set is sufficient, and round 4's objection is what proves it** (orchestrator
ruling, 07-29 review round 4). Round 4 argued the set could not emit copy on a
**successful** fallback — a recovered turn returns a normal result, so it passes through
neither `backend_failure` nor terminal-error handling — and asked for
`core/message_dispatcher.py`. The observation is correct and the conclusion inverted:
**the spec was over-promising, not the scope**. §4.5 now makes a survived turn silent, so
there is no success-path copy to emit, every remaining **post-launch** form is on the
failure path these files already own, and round 4's conclusion holds for those.
**The claim held only for post-launch failures, and round 16 found the gap** (finding
`3672822540`, ruling 07-29 17:41). A turn with no runnable candidate fails inside the
*launch resolution*, before a backend process, a transport or the turn gateway exists:
Codex renders it through its adapter's generic `except Exception` startup copy and
Claude's call site has no typed handling at all, so neither `backend_failure.py` nor
`message_output.py` is ever reached. That path is L3's too, mapped **once** at the
resolve boundary (L3's row above) — which is why the scope grew by one seam rather than
by three adapters. Recorded here because 「the success path
cannot reach the emitter」 is a fact a later lane will rediscover; it is a design
constraint that shaped the ruling, not an oversight to fix.

**L3's turn-lifecycle seam is scope, and the reason is recorded once** (orchestrator
ruling, 07-29 — review round 6). The block above settles the *copy*; this settles the
*record*, and the two went opposite ways because the push cut removed one and not the
other. Provenance is owed for **every FSM-tracked turn**, including the ones that succeed —
and a successful turn never visits `backend_failure.py`, so nothing in L3's original scope
could see it or carry the Avibe `turn_id`. **「FSM-tracked」 is the whole obligation, not a
hedge on it**: 「no FSM truth → no record」 leaves only two ways to satisfy a wider reading,
expanding the FSM (deferred to v2.1) or writing an approximate record (forbidden). L3's
scope therefore gains the module that owns the turn state machine; **L3 verifies the exact
file at implementation and records it in its PR, escalating if it differs** — this plan
names the architecture, not a line number.

The binding constraint travels with it, and AC-1 and AC-4 both turn on it: **turn
completion and cancellation classification are read from the turn FSM's terminal states,
never inferred from transport.** A dropped connection and a user pressing Stop look
identical at the gateway, and only the FSM knows which one happened; an implementation
that guesses from the socket will mislabel one of them, which is precisely the record AC-4
exists to make trustworthy.

The turn FSM is shared core code, so L3's edits there are **additive hooks, coordinated at
symbol level**: L3 adds its call sites without reshaping the FSM, and nothing else in this
batch touches that file — which is what keeps a shared-core edit out of the lane-conflict
class in the first place.

**L3 owns the correlation mechanism, and designs it in its own note** (orchestrator
ruling, 07-29 16:20, accepting L0's proposal). The mechanism that ties a gateway attempt
back to an Avibe turn — the token registry, the scope keys, the FSM lookup, and the
coverage they imply — **is not designed in this plan and is not written by L0**. It is
**L3's first deliverable when its lane starts**: a design note under `docs/plans/`, named
by L3, written by the lane with its hands on the call sites and free to verify each one
before committing to it.

The cut is a first-principles one, not a scheduling convenience. This plan transcribes
decisions; the correlation text was a *mechanism being designed*, in a lane that cannot
read its own call sites into scope and cannot rule on lane boundaries. Three review rounds
punctured it in the same place three times — the Codex `cwd` carrier, the Claude session
identity, the route layer's ownership — while every other part of this batch converged.
The lane that will build it is the lane that can design it.

What stays here is what was **ruled**, and every one of these binds the note:

- **Per-backend tokens are rejected** (round-7 finding, standing).
  `core/handlers/model_hub/turn_gateway.py` authorizes from a per-backend token map today,
  so two concurrent sessions on the same backend and model are indistinguishable at the
  gateway — and AC-1's `failed_attempts` and AC-4's cancellation classification both need
  that correlation to exist at all. Concurrent same-backend sessions are the norm in this
  product, not an edge case.
- **The gateway credential is PROCESS-SCOPED** (07-29 15:07): minted on first use per
  process scope and stable for that process's lifetime, superseding the 14:04 「minted once
  per session」 wording that review round 9 showed codex cannot carry. **Mint-on-first-use
  per scope key is idempotent** (15:42), which is why nothing has to observe process
  restarts. *Which* scope key each backend uses, and what the registry does with it, is the
  note's to state and to verify.
- **Write only when the attribution is exact** (07-29 15:07, guarded 16:35): a
  `turn-provenance` record is written when the FSM shows exactly one active turn for the
  scope **and nothing untracked is using that scope**; anything else writes **no record at
  all**. The guard is not a refinement, it repairs a false invariant: a Web turn and an
  IM/CLI turn can share one codex `(backend, cwd)` transport, and then 「exactly one
  FSM-tracked turn」 is true while 「exactly one user of the scope」 is false — the tracked
  turn's record would absorb the other's attempt, which is the misattribution this batch
  exists to purge. **How** the gateway establishes 「no untracked use」 — or how FSM
  coverage grows until the case cannot arise — belongs to L3's design note, which carries
  it as **a constraint the note must satisfy**, not as an open question it may answer with
  silence. Absence is honest, and it is *all* that remains: the 15:42 ruling deleted
  the event-log half of this pairing, because events trace transitions and a healthy
  attempt is silent on every path (AC-1, AC-4). This replaced the 14:39
  coarse-grain-with-a-marker design, which round 9 showed unwritable: `turn-provenance.schema.json` requires `turn_id` and the only
  contracted read is `GET /api/models/turns/<turn_id>/provenance`, so a record attributed
  to no turn could be neither written nor read back.
- **No FSM truth → no record** (07-29 15:42) — the same rule at its natural boundary. v2
  provenance covers FSM-tracked turns; **IM and CLI turns write no provenance in v2**,
  recorded as a limitation rather than papered over. The loss is debug-marginal because the
  source-grained resolution-event feed stays channel-independent, so those turns' failures
  and switches are still traced even though their per-turn attempt lists do not exist.
  **v2.1 candidate**: extend FSM registration to the IM/CLI dispatch paths — the write rule
  is path-agnostic, so nothing in §4.5 changes when the coverage widens.
- **The exclusion, stated as what it protects** (07-29 15:42, narrowing 15:07): **no
  process-model or lifecycle changes, no wire-format changes, no fingerprint-semantics
  changes.** 「No adapter edits」 was the wrong perimeter — review round 10 showed it forbade
  the one thing the mechanism needs while protecting nothing extra. **Additive call-site
  parameter threading in a backend adapter is ALLOWED.** Those three protections are also
  the compressed form of the three remedies ruled out at 14:39: per-session transports
  (process model), request-level identifiers (wire format), and lifting the token out of
  the restart fingerprint (fingerprint semantics — rejected hardest, because the shared
  transport would then serve every session under the *first* session's token, which is
  silent **misattribution**, the invented-state class this batch purges).
- **This owes NO contract term, and the one the 14:39 text owed is RETRACTED** (07-29
  15:07). Three deletions: **`turn_id` stays REQUIRED** — no nullable identity, no new read
  route, no list endpoint; **the attribution-grain term is retracted from L1's v3**, since
  a grain marker is vestigial once every written record is turn-exact by construction; and
  **the coarse / workdir grain leaves the recorded vocabulary entirely**. Should the
  opencode verification later find a shared server, the grain question returns **with
  evidence**, for a targeted term — the standing deferred-with-evidence rule.

**The fourth seam is ruled** (orchestrator, 07-29 16:20, closing review round 12): the
additive-threading grant **extends to the Claude call site**. Threading the live session
identity into launch resolution is the same parameter-pass class as the Codex `cwd` —
`resolve_model_hub_launch(controller, "claude", requested_model)` is handed no session
identity today, while `composite_key` and `base_session_id` sit in the surrounding block,
so without the grant the identity chain terminated one hop short for claude. §3's L3 row
carries all four seams as **scope**; what happens inside them is the note's.

**Classification is unchanged by any of this.** The token establishes *what an attempt may
be attributed to*; completion and cancellation still classify from the FSM's terminal
states per the round-6 constraint above. AC-4's two legs stand exactly as written — they
now pin the **unambiguous** fixture — and a **third leg** covers what that fixture excludes,
restated 07-29 15:42 so that it needs nothing the frozen contracts do not already have:
**ambiguous-leg-absent plus control-leg-present.** Under deliberate same-cwd concurrency,
neither turn's provenance contains the ambiguous attempt; run **the same fixture
sequentially**, with no concurrency, and the provenance record MUST be written. The pairing
still defeats silent data-dropping, but structurally rather than by contracting a new
trace: an implementation that guesses fails the ambiguous leg, and one that quietly drops
everything fails the control. The 15:07 wording's positive half — 「the resolution-event log
DOES contain the source-level outcome」 — is **deleted**: events trace *transitions*, a
healthy attempt is silent by design on every path, and an attempt nobody can attribute
legitimately leaves no trace. No new kind, no new reason, v3 untouched. AC-1 adjusts to the
same write rule.

**Silence needs a negative assertion, or it is unenforceable (07-29, review round 6).**
Every criterion in §8 asserts that something is present; a rule whose content is 「nothing
appears」 cannot be caught by any of them, so the superseded 「已自动换线」 tail could be
reintroduced with every gate green. **L6 owns the assertion**, on the existing quota-
failover scenario: when the fallback succeeds, the turn's delivered output contains the
answer **and no supply copy at all** — no switch tail, no error card, no advisory line —
while `GET /api/models/turns/<turn_id>/provenance` — the contracted route, not the bare turn path (corrected 07-29, review round 7) — still shows both attempts. The two halves must be
asserted together in one case, because that pairing is the entire ruling: the record is
kept, the interruption is not. Asserting only the transcript would also pass an
implementation that silently stopped recording provenance.

**Native re-auth owes an auth-setup scenario** (07-29, review round 3). The confirm →
abort → honest-failure ordering AC-13 makes normative is a multi-step auth flow, and
repo `AGENTS.md` is explicit about what those owe: update
`tests/scenarios/auth_setup/catalog.yaml` and add or update a closed-loop case under
`tests/scenarios/auth_setup/test_auth_setup_scenarios.py`. §5's Model Hub catalog does
not discharge it — a different harness over a different capability — so the work is
named in **L2**'s scope beside the flow it covers, rather than left to L6's catalog
sweep, where an unlisted scenario is invisible by construction. Unit and contract tests
cannot see the ordering this exercises: they can assert a confirmation exists, not that
it preceded an irreversible login and could abort it.

**Merge order.** L0 first and alone — it unblocks every other lane. Then **L1**, which
owns the v3 contract bump. **L2 and L3 then run in parallel** off L1, with **L4**
starting alongside them. **L5** joins once L2/L3/L4 have landed the surfaces it wires.
**L6** finalizes.

Two known risks, stated so the lanes plan around them instead of discovering them:

1. **L2 and L3 both touch `core/handlers/model_hub/service.py`.** They are
   **symbol-level no-touch** to each other — L2 owns the repair and guard functions,
   L3 the provenance/probe/chain ones — and **L2 merges first**, so L3 rebases onto
   it rather than the reverse. A lane that needs a symbol the other owns routes
   through the orchestrator; it does not edit it. **The one collision already known is
   pre-granted: `test_source`.** AC-3 puts the route and state-clearing change there —
   L2's repair symbol — while AC-11 needs the source-state event emitted on that same
   non-turn path, which is L3's. Under the merge order above L2 lands its change first,
   and **L3 adds the single emission call inside that symbol on its rebase under this
   standing grant**, with no fresh orchestrator round-trip (ruling 07-29 17:41, review
   round 16: the risk list exists so lanes plan around collisions instead of
   discovering them, so a known one is named and allocated here).
2. **Docs-heavy PRs do not get reviewed by default.** The Codex bot skips docs-only
   diffs, so any lane whose PR is docs-shaped forces the review with `@codex review`
   and confirms the 👀 inside the trigger window, per `pr-delivery-loop`.

**Cut and deferred — no lane builds these:**

- **Proactive push machinery — CUT** (owner ruling 2026-07-29 10:54). No recipient
  resolution, no delivery layer, no scope fan-out, no home-scope fallback. Events are
  recorded; the failing turn and the 「模型」 page are the surfaces (spec §4.5).
- **Provenance as a chat-surface feature — CUT** (owner ruling 2026-07-29 14:03,
  superseding the round-6 orchestrator ruling that had scoped it to the Web conversation
  surface). Users should be **unaware of supply machinery**; provenance inspection is a
  **debug affordance, not a user feature**, and it appears **nowhere in the chat surface —
  neither Web nor IM**. AC-1 reduces to record + API truth accordingly. What is *not* cut:
  the record itself, the contracted route, and the in-turn error copy — error UX is the
  surfacing mechanism the owner endorsed, and it is a different thing from inspection.
- **The 请求日志 / 诊断 page** — v2.1 candidate, **not an AC and owed by no lane in this
  batch**. If provenance ever gets a surface it is that page, in the Models page's 高级
  area — the row already exists as designed (V6 01: 跨厂商自动顶替 · 请求日志 · 诊断) and
  stays as designed. The page ships when it ships.
- **Fallback spend attribution** — v2.1 (spec §10.3).
- **Cross-vendor models as first-class menu entries** — v2.1, gated on the
  agentic-fidelity spike (spec §10.4). Cross-vendor supply itself already works and
  is in scope; what is deferred is promoting it to a first-class menu concept.
- **Per-Vibe-Agent rows on the Models page** — ruled out this milestone (owner
  2026-07-29 10:44 #3). Backend rows may show 「N 个 Agent 在用」 with a deep link into
  Agent settings; `SupplyGap.agents` keeps the agent grain in the contract for a
  later UI upgrade.

**No-touch zones.** The zones are **time-ordered, not just file-ordered**, because the
merge order already separates the lanes that would otherwise collide:

- **L1 owns `config/v2_config.py` and `core/handlers/model_hub/{resolver,service,rpc,
  migration}.py`** for the priority→per-agent surgery and its endpoints. It is the only
  lane in those files while it is open, and no symbol-level split applies to it — the
  surgery crosses symbols by nature.
- **After L1 merges**, L2 and L3 branch from post-L1 master and share
  `core/handlers/model_hub/**` under a **symbol-level** no-touch: L2 owns the repair and
  guard functions, L3 the provenance/probe/chain ones. A collision — two lanes needing
  the same symbol — escalates to the orchestrator rather than being resolved by
  whichever lane pushes second.
- **The public route layer is a SHARED surface, governed by the same two rules** (07-29
  16:20 ruling, closing review round 12 — before it, the endpoint surface belonged to no
  lane at all, so no lane could legally delete `/api/models/priority` or expose its own
  routes). The surface is `vibe/ui_server.py`'s models routes, `vibe/model_hub_client.py`,
  and `core/handlers/model_hub/rpc.py` behind them. Rule one, **each lane adds and removes only its
  own routes and wrappers**: **L1** deletes the priority GET/PUT and adds
  `agents/<backend>/sources`; **L2** the repair routes; **L3** the provenance, chain and
  probe routes. Rule two, **sequencing is by merge order** — L1 first, L2 and L3 branching
  post-L1 under symbol-level no-touch to each other, and a collision escalates to the
  orchestrator rather than being resolved by whoever pushes second. Same shape as
  `core/handlers/model_hub/**` above, and named separately because three lanes edit the
  same three files and none of them owns the file.
- **L3 is the only lane outside `core/handlers/model_hub/**` and that route layer** (07-29, review round 3):
  the in-turn copy puts it in `core/backend_failure.py`, `core/message_output.py` and
  `vibe/i18n/{en,zh}.json`. No other v2 lane touches those files, so the zone is a
  statement rather than a negotiation — but L3's brief names them explicitly, because a
  lane that discovers it needs a file outside its declared scope is a lane that has
  already collided with someone.
- **One contract shape lands atomically with all of its consumers** (orchestrator
  ruling, 07-29, review round 4). The discovering lane may not publish a contract edit
  and leave downstream code or tests on another meaning of the same version. The first
  contract-owning implementation lane therefore carries every affected shape,
  consumer, and checker in one commit. After it merges, contracts are **read-only** for
  every remaining lane. A mismatch proven by implementation escalates to the
  orchestrator with evidence; it is never an in-lane edit or silent reinterpretation.
- L4 and L5 split `ui/src/components/settings/models/**` by subdirectory, and **that
  split is the whole of their UI scope again** (owner ruling 07-29 14:03). Review round 7
  had widened L5 into `ui/src/components/workbench/` to make AC-1's conversation-surface
  rendering implementable; the owner then cut that surface entirely, so the exception goes
  with it. The **in-turn error copy is unaffected and stays L5's** on the UI side — it is
  error UX, the owner-endorsed surfacing mechanism, not provenance inspection. If wiring it
  turns out to need a file outside the split, L5 records the exact file at implementation
  and says so in its PR, per the pattern the round-6 seam ruling established.

**GA gate (outside the v2 lane batch).** The v2 batch ships **flag-off and does not
GA**, so the following are GA-blocking deliverables that no lane in this batch owns and
that nobody may read the lane table's silence as having cut:

- **Managed-runtime availability guarantees** (`model-hub-contracts/README.md:263`, v1's
  L7): mirror the pinned engine assets into Avibe-owned release storage under the same
  manifest-verified backup/recovery pattern as Show Runtime, then point the manifest at
  the mirror with upstream recorded as provenance. SHA256s never change (same bytes).
  The platform expansion pinned to the same work — linux-arm64 / darwin-x64 assets plus
  the schema platform-enum revision — travels with it; until then unsupported hosts fail
  closed and Direct mode is the escape hatch.

The contracts README's wording stays as written: it is still true, and still GA-blocking.

## 4. Product gates

- **Subscription custody (owner 2026-08-07, amended 2026-08-08):** Claude recommends
  native custody and ChatGPT recommends Hub custody; both explicit alternatives remain
  supported. Only Claude Hub shows the factual risk sentence. Hub-held subscriptions
  may enter cross-vendor configured chains with no experimental or consent gate. I1's
  ChatGPT default remains subject to the current-vendor re-verification in §3.
- Cross-vendor supply is a normal explicit configured-chain capability, not an experimental
  placeholder or warning surface.
- Mode onboarding: existing installations with no Model Hub state start in Direct and
  switch only by user action; fresh installations start in Gateway. The product is
  available when the global enable environment variable is absent.

## 5. Verification layers

- **Unit**: configured-chain execution, serializer completeness, overlay generation
  (identifier stability invariant), and native-config import parsers. Add/import tests
  prove that every one-time match is written at the deterministic position chosen by
  §4.2's sole placement policy, remains visible and adjustable, and never creates a
  persisted policy discriminator or “not enabled” state.
- **Contract**: REST API against `model-hub-contracts` schemas (both
  directions), engine adapter against pinned engine version.
- **Scenario**: `tests/scenarios/model_hub/catalog.yaml` — at minimum:
  quota-exhausted failover & recovery switchback, a configured per-model reorder
  takes effect next turn, a model mapping applies only to its named backend/menu model,
  OpenCode identifier stability
  across mode switch, native-config import non-destructiveness, OAuth forms A/B/C happy
  path + timeout/cancel. Scenario IDs appear in PR descriptions.
- **Behavioral (Incus)**: real backend turns in both modes; 最近切换 log
  reflects induced quota errors; UI pixel pass vs the exported design frames
  (**V6 01–04 plus M01/M02 for v2**, per §3 — this line's original 「V4 frames」 is
  the v1 reference).

## 6. Open items carried from spec §10

1. Remaining mocks (empty state / Dark / mobile / copy pass) — feed L4/L5;
   not blocking lane start (contracts govern behavior, mocks govern polish).
2. en.json wording pass (Hub / Direct locked; rest of EN copy during L4/L5).
3. `design.pen` frames must be saved (Cmd+S) and re-exported before L4/L5
   dispatch — lanes verify against the exported files, never against the live
   canvas. For the v2 lanes that means **V6 01–04 and M01/M02** (§3); the frames
   this item originally named were V4's.

## 8. Implementation acceptance criteria (AC-1–AC-51; v3/K4 addenda through 2026-08-11)

**Current authority.** AC-1 through AC-21 retain their existing order and historical
record; AC-19's final acceptance is amended in place below. S-1 supersedes every
historical acceptance phrase in AC-1 through AC-21 that names a `follow | custom`
policy, enrollment, a separate mapping phase, or runtime Source/model
matching; the original quotations remain evidence of what the earlier review found, not
executable final-shape instructions. AC-22 onward are the binding v3 additions. K1 owns
the owner-authorized S-1/protocol contract freeze and §8's live-input closure on this
tested head; after that freeze, I1 owns the remaining prelaunch consumers and any
orchestrator-approved contract correction, while I2–I6 own the remaining consumers and
evidence under §3.

Review round 8 of the spec-v2 PR (#1081) returned six findings, five P1 and one P2.
Rounds 6 and 7 had each answered a review by adding a spec section, and each new
section generated the next round's findings; round 8 was pre-committed to stop that.
So the split here is mechanical rather than editorial: **a finding that was two
statements in these docs contradicting each other was fixed in the spec** (AC-5, AC-6,
plus the retractions noted under AC-1 to AC-3), and **a finding that would need a new
route, a new enum value, or a product decision is recorded here verbatim** instead of
being designed inside a review reply by an author who wants the thread closed.

**Round 9** returned six more findings, one P1 and five P2, under the same rule and was
pre-committed as the last round answered with edits. Five were true contradictions and
were fixed in place: the missing converse in `probe-result.schema.json` (a transport
failure could carry a measured latency), §10 still defining `reachable` as 「the upstream
answered」, the version conflict this section itself carried, source fan-out testing raw
`sources.order` instead of the chain grain, and a protected-set term naming fixed-menu
selection state the contract does not persist. The sixth is fixed only as far as a
document can fix it — the chain and probe routes are now scoped to Hub mode — and the
Direct-mode representation it asks for is recorded below as **AC-7**.

**Round 10** returned six more findings, two P1 and four P2, on that same head, and
they are **recorded, not fixed**: round 9 was the last round answered with edits, and
appending criteria is the only edit this round permits. Each was checked against the
line it cites before being written down — none is a strictness complaint, and none was
already covered by AC-1 to AC-7 — and they arrive in three pairs worth naming for
whoever picks them up. **A definition written for one purpose, reused where the
tolerance differs:** the delete guard's protected set is deliberately wide, because a
model the user ticked deserves protection from a silent delete, but reusing that union
to decide who gets an interruption push makes it too wide (**AC-9**), and letting it
count mapping rows the resolver ignores makes even the guard too wide (**AC-8**). **A
contract file restating something the normative side already settled elsewhere:**
`source_id` promises `Source` identity without `Source`'s pattern (**AC-10**), and a
schema description sends `system`-emitted source events to nobody while §4.5 routes
them from their source (**AC-11**). **A promise the same document cannot keep:** two
credential invariants that cannot both hold (**AC-12**), and a `force=true` retry with
no request field to carry it (**AC-13**). Nothing in this round was answered with new
prose in `model-hub.md` or the contracts.

**Round 11** returned six more findings, two P1 and four P2, and is the first round
whose findings are all about **this section rather than the spec**: round 10's record
carried four acceptance tests that cannot be run as written and two bookkeeping cells
that contradict the criterion above them. So the answer is neither a spec edit nor a new
AC — appending AC-14 to AC-19 would leave the defective tests standing beside notes
saying they are defective, and ship a document arguing with itself. Each is corrected
where it sits, annotated 「corrected 07-29, review round 11」 at the point of change, and
none of it touches `model-hub.md`, a contract file, or a standing ruling: AC-9's two
phases could not both run on one fixture, AC-10 asked frozen examples to survive a bump
it performs itself, AC-11 drove its test from a trigger `api.md` documents as a
different event, AC-12 let an in-memory queue pass for 「durable」, and AC-9's and
AC-13's owner-call cells read `no` where their own criteria say a decision governs. Two
of the six had one cause: the versioning paragraph keyed off 「changes a shape」 when the
freeze protocol keys off editing a frozen file, which is why AC-8 and AC-12 were
exempted wrongly. That root is fixed once, below, and both invariants the round exposed
now carry a mechanical check.

**Round 12** returned eight findings, all P2, and they split two ways. **Four are
defects in this section's acceptance record** — the same class as all six of round
11's, in a section round 11 had just corrected: a fixture that consumes its own
precondition (**AC-14**), a fixture the identifier rules forbid (**AC-15**), an
assertion naming an Agent its own setup never declares (**AC-16**), and push-count
assertions that silently assumed one branch of an unsettled owner decision
(**AC-17**). **Four are contract defects** (**AC-18** to **AC-21**), two of them
missed instances of classes the spec PR had explicitly swept in round 3 — closed
vocabularies and partial predicates. A class sweep that leaves instances behind is
itself a generator, which is why the loop was closed by directive rather than at a
natural zero. Round 12's findings lived in PR #1081's description only; committing
them here was lane L0's opening task.

**Owner rulings, 2026-07-29 10:44 and 10:54.** The 10:44 pass settled every pending
item at once; the 10:54 addendum then **cut proactive push entirely** (spec §4.5 and
its revision note). Both are applied throughout this section rather than appended to
it, so no criterion below asks for a notification:

- **AC-2 is settled** — an explicit irreversibility warning before the native login,
  and honest failure states after it — and **AC-13's** `force` confirmation reuses
  that shape. Hub-channel api_key replacement stays transactional, unconfirmed.
- **AC-6** reduces to **event-record accuracy** — which, after the downgrade recorded
  under AC-6 itself, means exactly ONE unattributed record carrying **no backend list**,
  the affected backends being derived from current configured chains (corrected 07-29,
  review round 9: 「which backends the record names」 was the delivery-era framing and
  contradicted AC-6's own acceptance) —
  **AC-11** to the **event-shape half**, and **AC-9** to **which Agents a supply gap
  names** for the delete guard, the confirm dialogs, the feed and the 「模型」 page.
  Their delivery halves are void.
- **AC-17 is superseded**, not deleted: the unsettled recipient policy it insulated
  §8 against no longer exists, so the dependency is closed by removal rather than by
  qualification. Its block records that.
- **`SupplyGap.agents` including the Agents that inherit a backend default stands**
  (ruling #4). It was always guard and confirm payload, never a recipient list.
- **The Models page stays backend-grained this milestone** (ruling #3): no
  first-class per-Vibe-Agent rows, `SupplyGap.agents` unaffected (§3, cut list).

The round narratives above are the record of what each round found at the time it
found it. Where they describe pushes, the criterion below governs, not the narrative.

These are not suggestions and not backlog. Each is a test the implementing lane must
pass. **This PR does not reopen v2 — and a lane that closes an AC by changing a frozen
shape publishes a new contract version** (07-29, review round 9). Those two halves were
in conflict as written: round 8 said the contracts 「stay at `contract_version: 2`」 and
then told AC-4 to grow the `outcome` vocabulary and AC-1 to add a no-source
representation 「against the frozen contract」, which would ship two mutually
incompatible v2 shapes with no signal a client could switch on — precisely what the
freeze protocol in `model-hub-contracts/README.md` exists to prevent (「lanes cite,
never edit; changes go through the orchestrator and bump `contract_version`」).
Mechanically, **the trigger is editing a frozen file, not changing a JSON shape**
(07-29, review round 11). Round 10 drew the line at 「changes a shape」 and wrote three
exemptions on that reading; all three were wrong, because the protocol above forbids
lanes from editing these files at all, so any edit is the version event — a `pattern`
that narrows what validates, a prose invariant that changes when a route refuses, and
a new field are one class. AC-8 rewrites the protected set in frozen `api.md` and so
changes when DELETE returns `source_last_supplier`; AC-12 rewrites that file's
credential invariants; AC-2's remedy edits its re-auth flow whichever UX the owner
picks. So the v3 set is **every AC whose surface is a file under
`model-hub-contracts/`** — AC-1, AC-2, AC-4, AC-7, AC-8, AC-10, AC-11, AC-12, AC-13,
and round 12's AC-18, AC-19, AC-20, AC-21 — plus **AC-3**, whose remedy extends an existing
contracted route even though its surface is the spec, and **AC-9**, whose acceptance
needs a per-Agent read projection in `api.md` that does not exist yet (07-29, review
round 5 — AC-9 was previously listed as staying on v2). **Lane L1 authors the whole of
v3, in one coordinated commit** (§3's single-freeze ruling, 07-29 review round 4): it goes
through the orchestrator, sets `contract_version` to **3**, updates the mirror table,
sweeps the delivery language the push cut stranded (「v3 handoff notes」 below), and
states the client-visible delta in its PR description. **「The whole of v3」 means every
contract shape any §8 criterion requires, including criteria whose implementing lane is
L2 or L3** — AC-11's nullable `model_id` and AC-9's per-Agent projection are written by
L1 alongside its own, because the alternative is L2 and L3 each editing a frozen file
after the bump, which would make one version number name several incompatible contract
states. Later lanes then implement against a contract that is settled, and edit nothing
under `model-hub-contracts/` at all. Several of these narrow what already validates — AC-10 and AC-18 add a
`pattern`, AC-19 closes an enum, AC-11 forbids `system` on a backend-scoped kind — so
a payload a v2 serializer emits today can stop validating, which is exactly the
client-visible delta the bump exists to announce. AC-20 is the opposite shape and is
deliberately **not required**, so no frozen example is invalidated by it. **AC-5 and
AC-6 stay on v2**: their surface is the spec, and they change guard and
record semantics no contract file states. AC-6 stays there **because of its downgrade**
— the round-8 remedy would have needed an affected-backend field on
`resolution-event.schema.json` and pulled the criterion into v3; recording once and
deriving impact needs no field, so the frozen event shape is unchanged. **AC-14 to AC-17 are not a version event
either** — their surface is this document, and lane L0 discharged them before any
lane opened. AC-5 and AC-8 are the same guard from two sides, and under the single-freeze
ruling that split is now between *lanes*, not within one: **L1's v3 lands AC-8's `api.md`
half, and L2 implements both guards against it** — which is why L2 must read AC-5's spec
text and AC-8's contract text as one criterion even though only one of them is frozen.

| AC | Sev | Finding | Surface | Owed by | Owner call needed |
| --- | --- | --- | --- | --- | --- |
| **AC-1** | P1 | Define provenance for Direct-mode turns | `turn-provenance.schema.json` | **L1 v3** (contract) + L3 (record + route), with L6 scenario. **No UI lane** | **settled 07-29 14:03 — record + API truth only; no chat surface, Web or IM** |
| **AC-2** | P1 | Reconcile irreversible native re-auth before returning failure | `api.md` | **L1 v3** (re-auth flow contract, **including a server-enforced acknowledgement** — see below) + L2 (orchestration) + L5 (confirm copy) | **settled 07-29 10:44 — confirm before the irreversible login** |
| **AC-3** | P1 | Allow blocked sources to be re-tested after user action | `model-hub.md` + final `api.md` (`POST /api/models/sources/<id>/refresh`) | **I1** (one saved refresh/recovery route, state clearing, and shape) + I4 (Models-page action) + I5 (scenario) | **consolidated 08-09 — final authority is `/refresh`; no parallel `/test` recovery route** |
| **AC-4** | P2 | Represent canceled turns in provenance | `turn-provenance.schema.json` | **L1 v3** (contract) + L3 (emission, via the turn-lifecycle seam) with L6 scenario | **settled 07-29 — cancellation is FSM truth, never transport inference** |
| **AC-5** | P1 | Protect the menu-side model in deletion guards | `model-hub.md` | L2 (guard) with L6 scenario — **stays on v2** | no |
| **AC-6** | P1 | Record a source event once; derive per-backend impact | `model-hub.md` | L3 (event record) with L6 scenario — **stays on v2** | **settled 07-29 — reduced to the record half at 10:54, then downgraded to a single unattributed record (orchestrator ruling, owner-vetoable)** |
| **AC-7** | P1 | Represent chain and probe for Direct-mode backends | `api.md` | **L1 v3** (route scoping + Direct-mode payload) + L3 (route) + **L4 (the Direct-mode drawer gate — withdrawing 试跑一次 and the chain view — with its UI test; 07-29 15:07 ruling: a status surface, per the razor in §3's L4 row)** with L6 scenario | no |
| **AC-8** | P2 | Exclude disabled mapping rows from the protected set | `api.md` | **L1 v3** (`api.md` half) + L2 (guard) with L6 scenario | no |
| **AC-9** | P2 | Resolve affected Agents from their effective models | `model-hub.md` + `api.md` | **L1 v3** (per-Agent read projection) + **L1 (projection assembler)** + L3 (record grain) + L2 (`SupplyGap.agents`) + **L4 (the page attribution that consumes the projection, and its UI test)** with L6 scenario | **settled 07-29 14:39 — the attribution consumer is L4's; the earlier cell assigned no UI lane and left L6 to discover a failure nobody could fix**; **producer settled 07-29 16:50 — the assembler is L1's too: it already owns `service.py`'s agents payload and the resolver state the projection reads (`supply_status` derivation, effective-model resolution), so contract and producer sit in one lane** |
| **AC-10** | P2 | Constrain `ProbeResult.source_id` to the canonical `src_*` format | `probe-result.schema.json` | **L1 v3** (contract) + L3 (serializer guard) | no |
| **AC-11** | P1 | Shape `system`-emitted source events from their source | `resolution-event.schema.json` | **L1 v3** (schema, incl. the nullable `model_id`) + L3 (emission) with L6 scenario | **settled 07-29 10:54 — reduced to the shape half** |
| **AC-12** | P2 | Reconcile a failed old-credential revocation | `api.md` | **L1 v3** (credential invariants) + L2 (repair implementation) | no |
| **AC-13** | P1 | Give the `force` override a request contract on both repair routes | `api.md` | **L1 v3** (request contract) + L2 (routes) + L5 (confirm dialog) | **settled 07-29 10:44 — via AC-2's shape; guard scoped to the Hub path by orchestrator ruling** |
| **AC-14** | P2 | Split AC-8's guard mutations across fresh fixtures | `model-hub-implementation.md` | L0 (applied), enforced by L2's test build | no |
| **AC-15** | P2 | Use a legal backend/model fixture for AC-9 | `model-hub-implementation.md` | L0 (applied), enforced by L3's test build | no |
| **AC-16** | P2 | Remove the nonexistent Agent from AC-5's assertion | `model-hub-implementation.md` | L0 (applied), enforced by L2's test build | no |
| **AC-17** | P2 | Make notification counts independent of the open recipient policy | `model-hub-implementation.md` | L0 (superseded — recipient policy cut) | **settled 07-29 10:54 — by removal of the policy** |
| **AC-18** | P2 | Constrain resolution-event source references | `resolution-event.schema.json` | **L1 v3** (contract) + L3 (API-boundary guard) + **L4 (the deleted-source feed state and its UI test — 07-29 15:07 ruling, same razor)** | no |
| **AC-19** | P2 | Close the final eligibility reason-key vocabulary | `agent-supply.schema.json` | **I1** (final contract) + **I4** (`ui/src/i18n/{en,zh}.json` mirrors) | **settled 08-09 — retired consent/OpenCode reasons removed; closed-vocabulary invariant retained** |
| **AC-20** | P2 | Enforce the hub-mode half of the mode invariant | `agent-supply.schema.json` | **L1 v3** (contract) | no |
| **AC-21** | P2 | Make the mirror registry encode its promised checks | `model-hub-contracts/README.md` | **L1 v3** (registry + checker) | no |
| **AC-22** | P1 | Make one persisted ordered per-model chain the only Gateway routing configuration and execute it verbatim | `model-hub.md` + final `agent-supply.schema.json`, `agent-chain.schema.json`, `api.md`, and provenance/event mirrors | **I1** (final contracts, add-time match, §4.3 executor, shared tests) + I2 (runtime consumer) + I4 (Gateway UI) + I5 (scenario) | **amended 08-09 S-1 — configuration chooses; runtime only walks and classifies** |
| **AC-23** | P1 | Make subscription custody vendor-specific, enforce one native Source per backend, and distinguish a Native Gateway hop from Direct mode | `model-hub.md` + final source/OAuth/supply/API/adapter contracts | **I1** (contracts/defaults/shared guard) + I2 (dispatch) + I3 (OAuth/native import) + I4 (product states) + I5 (scenario) | **amended 08-09 — Claude native, ChatGPT Hub; Direct and Native are distinct product terms** |
| **AC-24** | P1 | Show the sole subscription-routing warning when, and only when, Claude is added as a Hub-held Source | `model-hub.md` + Models UI/i18n + scenario evidence | I4 (flow and copy) + I5 (scenario); I1 owns any contract term if implementation proves one necessary | **settled 08-07 — informational warning, not consent** |
| **AC-25** | P1 | Split subscription recommendations by vendor and disable duplicate native creation | `model-hub.md` + final source/OAuth/API contracts + add-flow UI evidence | **I1** (defaults + shared singleton guard) + I3 (OAuth) + I4 (guidance) + I5 (scenario) | **amended 08-08 — vendor defaults retained; 08-07 takeover notice withdrawn** |
| **AC-26** | P1 | Complete Source add/detail operations: add-only connectivity test/discovery, one guarded saved refresh, manual model add/remove, and editable per-model reasoning-efforts lists with no defaults | `model-hub.md` + final `source.schema.json`, probe/API/adapter contracts | **I1** (shape + routes + service) + I2 (runtime effort use) + I4 (flows) + I5 (scenario) | **amended 08-09 — one saved mutation; no latency or last-check presentation** |
| **AC-27** | P1 | Make every stored protocol response-observed before Save and immutable afterward | `model-hub.md` + final source/probe/API/adapter contracts | **I1** (shape + API) + I2 (runtime transport) + I3 (OAuth/import observation) + I4 (failure-only manual probe-order hint) + I5 (scenario) | **amended 08-09 — manual input orders probes but does not create persistent provenance or bypass observation** |
| **AC-28** | P1 | Converge protocol identity on exactly Anthropic, OpenAI Responses, and OpenAI Chat Completions | `model-hub.md` + final source/API/adapter/overlay mirrors | **I1** (same-tested-head protocol/version closure + shared tests) + I2 (transport) + I4 (failure copy only) + I5 (scenario) | **settled 08-09 — retain Chat Completions; no `openai_compatible` alias** |
| **AC-29** | P1 | Validate every persisted Source through the canonical final-shape validator | `model-hub-implementation.md` + final Source/config validation and native-import scenario evidence | **I1** (canonical validation boundary) + I3 (import writer + migration scenario) | **settled 08-09 — owner-routed investigation finding** |
| **AC-30** | P1 | Derive takeover from the resolved chain without a new field or false exhausted state | `model-hub.md` + final chain/API/mirror/locale projections | **I1** (projection + mechanical mirrors) + I2 (live current-hop input) + I4 (pull-surface rendering) + I5 (scenario) | **settled 08-09 — takeover is a projection, not stored state** |
| **AC-31** | P1 | Make existing-install Direct onboarding visible and reversible | `model-hub.md` + final mode API/UI/scenario evidence | **I1** (mode envelope/onboarding default) + I4 (Direct/Gateway groups and actions) + I5 (scenario) | **settled 08-09 — Direct remains a supported per-backend mode** |

**Read the 「Owed by」 column as contract-then-implementation** (07-29, review round 5).
For historical AC-1–AC-21, a cell beginning **L1 v3** records the lane that authored the
contract surface before its consumers. For AC-22 onward, I1 lands the final contracts
and the terminal version/protocol closure on one tested head; I2–I5 complete their exclusive consumers
and evidence before release under the rule above.
Only AC-5 and AC-6 carry no contract-owner term because their surface is the spec. In
both plans, any downstream lane touching `model-hub-contracts/**` escalates to the
orchestrator rather than publishing an in-lane reinterpretation.

The last column takes exactly two values, and round 11's mechanical check reads it that
way: `no` means the criterion never turned on an owner decision, and a **settled** cell
names the ruling that closed one it did. The check — an AC whose criterion mentions an
owner decision must not read `no` — therefore still bites after a ruling lands, because
「settled」 is not 「no」. Nine criteria carry a settled cell (AC-1, AC-2, AC-3,
AC-4, AC-6, AC-9, AC-11, AC-13, AC-17); the other twelve never depended on a call
(corrected 07-29, review round 15 — the inventory read 「six」 and 「fifteen」, which
misfiled AC-1, AC-3 and AC-4 as criteria that never turned on a decision, in the very
paragraph that describes the mechanical check). Several of the nine were settled by an
orchestrator ruling rather than an owner one — AC-6's downgrade and AC-13's guard scoping
among them; every such cell is recorded as owner-vetoable and named as such in its
criterion, which is why they read 「settled」 and not `no`.

**No AC waits on an owner call any more** (07-29, rulings 10:44 and 10:54). Round 11
recorded three that did: AC-2 outright, plus the half of AC-13 and of AC-9 that each
one's own criterion said a decision governs. AC-2 is settled — confirm before the
irreversible login — and AC-13's re-auth confirmation is that same shape rather than a
second answer to the question. AC-9's half is settled **by removal**: the zero-scope
fallback only mattered because something was being delivered, and delivery is cut. All
twenty-one criteria are implementable as written. Five groups must still be owned
together, because each is one question on more than one surface and independent answers
will disagree:

- **AC-1 + AC-4** — both touch `turn-provenance.schema.json`; 「which terminal states
  exist」 is the same question twice.
- **AC-1 + AC-7** — what a Hub-shaped contract says about a backend that is not on Hub.
- **AC-6 + AC-9 + AC-11** — all three are the **event record**: how many records a
  source failure writes, which Agents a supply gap names, and what shape an event
  carries when the emitter is `system`. Until the 10:54 ruling they were the delivery
  rule, and recording was the half that survived it — the half the feed, the status
  pills and the confirm dialogs actually read. AC-6 fixes the record at **one,
  unattributed** and moves per-backend impact to live derivation, AC-9 defines the
  Agent set the guards carry, AC-11 fixes the `system` emitter. They must be owned
  together because AC-11's 「affects something」 and AC-9's 「names an Agent」 both read
  their grain off AC-6's answer.
- **AC-2 + AC-13** — one repair path. AC-13's confirmation of a refused `force` is
  where AC-2's pre-login irreversibility warning lives; the owner's ruling made them
  one shape, so building them apart re-splits a decision that is closed.
- **AC-19 + AC-20** — both narrow `agent-supply.schema.json` on the same payload, and
  both were missed instances of round 3's sweeps; landing one without the other leaves
  a half-swept file that reads as fully swept.

**AC-5 + AC-8** are also the same guard from two sides — the namespace of the protected
set and its membership — so whoever fixes one should re-read the other's test. **AC-14
to AC-17** are not implementation work: lane L0 applied them to the blocks they repair,
and they are listed so the repairs are traceable to the findings that caused them.

The counts and completion statements above apply to the historical AC-1–AC-21
inventory. The ledger had **thirty-three** criteria at the K1 freeze and now has
**fifty-one** after the owner-settled K4 addenda below. AC-22 onward are owner-settled;
only the explicitly owner-vetoable final-shape and vocabulary choices in the v3 spec
remain open to veto, not to lane-level invention.

### Final contract shape handoff — owner rulings 2026-08-09

K1 freezes the S-1 routing and protocol vocabulary in the contract files on this PR.
I1 owns the remaining implementation closure and any contract field not changed by this
freeze. The mechanical gate is a **same tested PR head**, not a commit boundary: the
registered version and protocol consumers must coexist on that head before I1 merges.

- The terminal `contract_version` is owner-fixed at **5** in every registered version consumer:
  `docs/plans/model-hub-contracts/mirror-registry.json`,
  `docs/plans/model-hub-contracts/agent-chain.schema.json`,
  `docs/plans/model-hub-contracts/probe-result.schema.json`,
  `docs/plans/model-hub-contracts/runtime-dependency.schema.json`,
  `docs/plans/model-hub-contracts/turn-provenance.schema.json`,
  `core/handlers/model_hub/service.py`, `core/handlers/model_hub/provenance.py`,
  `tests/test_model_hub_config.py`, `tests/test_model_hub_api.py`, and
  `tests/test_model_hub_l3.py`.
- Removing `openai_compatible` adds its schema and adapter consumers to that same tested-head closure:
  `docs/plans/model-hub-contracts/source.schema.json`,
  `docs/plans/model-hub-contracts/adapter-interface.py`, and its byte-identical
  runtime copy `core/handlers/model_hub/adapter.py`.

The version closure contains independently testable Registry, Agent
chain, Probe result, Runtime dependency, and Turn provenance. CI evaluates the complete
PR head and does not require one commit. **“CI does not prevent splitting” does not mean
an intermediate revision conforms to the final protocol.** Landing points outside the
generated closure may ship in later independent implementation PRs. Model Hub remains
unshipped and gated, so those intermediate PRs may be incomplete, but every row in this
handoff must land before the feature can be released or enabled. The contract entries
below are the exhaustive final-shape handoff:

#### Authority-table registry

`mirror-registry.json` registers every closed decision domain below. The registry stores
only authority and consumer extraction locations plus relation rules; it never stores a
copied result set or expected member count. An unregistered table is descriptive, not
authoritative. Contract rows, implementation prose, and tests may mirror or reference a
registered authority but cannot create an unlisted branch.

| Registry ID | Decision domain | Sole authority | Registered consumer class |
| --- | --- | --- | --- |
| `M1` | Source state detail keys | `source.schema.json` state branches | probe schema and locale detail objects |
| `M2` | Source state to chain health | `source.schema.json` status | chain health schema |
| `M3` | Attempt-failure causes | `resolution-event.schema.json` reason | provenance failed-attempt schema and classification code |
| `M4` | Non-self-healing cause to remedy key | resolution-event reason plus registered pair relation | Source detail schema and both locale trees |
| `M5` | Backend `supply_status` | §4.5 supply-status table | AgentSupply, AgentChain, provenance, and locale projections |
| `M6` | Backend identifiers | AgentSupply backend | chain, probe, provenance, and event schemas |
| `M7` | Supply channel | Source channel | chain, provenance, probe, OAuth, and adapter consumers |
| `M8` | Native process availability | AgentChain blocker | AgentSupply, probe, and locale consumers |
| `D1` | Credential-failure branches | §4.3 credential matrix `Decision` column | API/implementation decision markers and resolver tests |
| `D2` | Turn outcome plus copy discriminator | §4.5 turn-outcome matrix `Decision` column | provenance/API markers and locale copy |
| `D3` | Source mutation envelopes | §4.5 total mutation matrix `Decision` column | API route/envelope markers and API tests |
| `D4` | Native import actions | §6 import matrix `Decision` column | migration schema, code, locale, and scenario consumers |
| `D5` | Source protocols | `source.schema.json` protocol enum | adapter, config/runtime type, and spec consumers |
| `D6` | Configuration eligibility reasons | `agent-supply.schema.json` reason enum | §4.4, service projection, and both UI locale objects |
| `D7` | Unsaved observation outcomes | `observation-result.schema.json` outcome enum | adapter result type and API contract consumers |
| `D8` | Unsaved observation discovery outcomes | `observation-result.schema.json` discovery enum | adapter result type and API contract consumers |

`M5` and `D6` are the newly exposed authorities from the circuit-breaker audit: the
earlier four-table sweep omitted supply-health and eligibility even though both were
closed decision domains. AC-33's generated test reads the listed live inputs in one
invocation and rejects orphan rows, orphan branches, stale snapshots, and unowned
external resolver importers.

| ID | Contract file | Final required shape |
| --- | --- | --- |
| **FC-01** | `README.md` | Names §4.3 as the sole configured-chain execution authority and §4.2 as the sole Add-time Source placement authority; documents the same-tested-head terminal closure, the prelaunch full-handoff gate, and downstream read-only ownership; indexes the contract files and terminal `contract_version: 5`; describes Sources, exact configured routes, add-time matching, channel custody, configuration eligibility, inventory, adoption, and pull-only supply visibility without reproducing either authority. |
| **FC-02** | `mirror-registry.json` | Mechanically covers every closed enum and cross-file identity used by the final files: three protocols, backend ids, Source/model origins, exact-hop blocker/reason/detail-key/remedy pairs (including `native_cli_unavailable` and live connection backoff in AgentChain, ProbeResult, the API marker, and both frontend locale objects), credential refresh-capability branches, guard/OAuth/Source-create state machines, the guard-error-to-required-plan-array relation, network phase totality, turn outcome/discriminator/copy-key rows, native-import actions and locale keys, event agents, the sole remaining eligibility reason, `added_to`/`adopted_by` shapes, channel defaults, the four `supply_status` labels, the derived takeover label, and UI locale homes. The D21 fixture compares every consumer individually to the reason authority and mutation-tests ProbeResult drift; a surviving consumer cannot mask a missing peer. API-boundary-only refusal values register their contract field, named negative route test, and forbidden UI/i18n scope instead of fabricating a render consumer. The same live-input invocation proves retired route literals are absent repo-wide and path identities do not reappear in the Source-model request bodies. Every mirror names an executable relation and terminal version **5**. **D21 consumer-completeness revision: 2026-08-11 23:49 K4.** |
| **FC-03** | `source.schema.json` | **Revised 2026-08-11 by the owner-authorized K4 completion and 19:56 network ruling.** `Source` requires canonical id, kind, vendor, `protocol: anthropic | openai_responses | openai_chat`, credential reference/custody, channel, ordinary `created_at` audit metadata, state/usage, and an unordered model inventory whose ids are unique within that Source. Every model is `{id, origin: "discovered" | "manual", reasoning_efforts: string[], retired?: boolean, display_name?, discovered_at?}`; omission means `retired: false`, only a discovered row may be true, and true is a persistent tombstone excluded from supply. The list is required, may be empty, and its reasoning list is editable for either origin. Only shaped explicit upstream errors enter existing non-permanent Source classifications; unclassified transport failures never write persistent health. Hub-held subscriptions require no flag, consent field, or acknowledgement record. |
| **FC-04** | `oauth-flow.schema.json` | Subscription flows carry explicit vendor and `supply_channel`; an omitted channel resolves Claude to `native_cli` and ChatGPT to `hub`, while both opposite explicit choices remain legal. The native option exposes `native_source_already_exists` plus `existing_source_id` before login when the backend singleton is occupied. Claude Hub carries one informational risk-copy key; no other path carries warning or consent state. |
| **FC-05** | `agent-supply.schema.json` | Each backend stores `mode: hub | direct`, one explicit `sources.order`, and exactly one `{hops: [{source_id, model_id}, ...]}` row per menu model. `hops` is always present and may be empty; neither order has a policy discriminator. It enforces at most one native Source per backend and exposes `adopted_by: [{backend, menu_model}]` from persisted references; Source-card attribution reuses that projection. `supply_status` is the sole backend-health projection, and every `model_supply` row with `chain_length: 0` must carry `has_runnable_hop: false`. Fresh-install construction uses Gateway; the onboarding service supplies Direct for an existing installation with no Model Hub state. No separate mapping, matching, enrollment, or takeover field exists. **Empty-chain correlation revision: 2026-08-11 22:37 K4.** |
| **FC-06** | `agent-chain.schema.json` | Returns the stored exact Source/model hops in the same order with only `current`, `runnable`, closed blocker reasons (`source_missing`, `model_unsupported`, `native_cli_unavailable`, Source-state reasons, and live `models.source.backoff.connection_failed`), and retry metadata added. `health: backoff` is a Source-scoped in-memory connection throttle before the first user-visible model-output byte with a `retry_at` strictly later than the read assembler's captured time; an expired overlay is normalized to underlying non-backoff health before serialization and never persists in Source/config state. Only output from that affected Source clears its streak; another Source's fallback output does not. The live overlay applies only over an otherwise healthy, exact-capability-present hop. Cooldown, needs-action, error, missing-Source, and unsupported-model facts suppress it and retain their established projection, so durable blockers roll up `interrupted`; only the native-process exception preserves backoff health/deadline while `native_cli_unavailable` takes the reason slot. A nonempty process-available chain blocked only by cooldown/ordinary backoff validates as `waiting` and cannot validate as `interrupted`. The shape has no route-policy or capability-matching discriminator. Takeover derives from stored first hop plus live current position and adds no field. **Blocker-precedence revision: 2026-08-11 23:49 K4.** |
| **FC-07** | `observation-result.schema.json` + `probe-result.schema.json` | Keeps unsaved Add Source connectivity/protocol/inventory observation separate from saved-Route runtime probes. Add-flow results report classified reachability/authentication plus an observed protocol only after a real upstream response, never a credential ref or request/status evidence detail. A failed observation may request a manual three-value probe-order hint but cannot save a protocol without response proof. `ProbeResult` remains the configured-Route runtime domain; its distinct live backoff key for unclassified pre-output connection failure implies exactly `channel: hub`, `reachable: false`, and `latency_ms: null`, rejecting native probes and measured-latency results. **Probe relation revision: 2026-08-11 23:49; observation evidence expansion remains withdrawn by the 20:35 owner ruling.** |
| **FC-08** | `turn-provenance.schema.json` | Gateway turn records contain no route-policy discriminator; they record exact requested/configured model ids, exact Source attribution, ordered attempts, and the closed outcome set owned by §4.5's authoritative Turn-outcome copy matrix. `requested_model_changed` is derived from those exact ids and is never persisted. The schema mirrors the outcome set without reproducing its branches. A canceled in-flight attempt carries no fabricated Source failure. No separate mapping field, compatibility discriminator, or internal-version conversion state exists. |
| **FC-09** | `resolution-event.schema.json` | Removes `mapping_applied` and its `mapping` reason: an explicit model mapping is stored configuration, not a runtime resolution event. Remaining events carry exact Source/model attribution with mechanically mirrored reason/detail/severity fields. The feed is a pull surface; event descriptions contain no proactive-delivery or recipient-resolution contract. No mapping field, route-rewrite event, or internal-version conversion state exists. |
| **FC-10** | `adapter-interface.py` | Defines the three protocols, response-backed protocol observation, connectivity classification, credential refresh capability, discovery, all-inventory reasoning-efforts validation, transient credential cleanup on every unsaved-flow exit, and durable reconciliation on revoke failure. `invoke(reasoning_effort: str | None)` stays singular because §4.3 passes zero or one exact member. Runtime-local `engine_down` is distinct from Source failure. The checked-in interface and runtime adapter are byte-identical. |
| **FC-11** | `opencode-overlay.md` | **Superseded 2026-09-04 by `model-hub.md` §4.8 v4 and `opencode-overlay.md` v4: OpenCode menu identity is the bare canonical model id, add-time matching is literal equality, and the overlay generates one provider per downstream protocol — no normalized provider ids anywhere.** Former shape: keeps stable normalized provider ids for menu identity and add-time match suggestions, supports the three protocols, and pins the exact configured Source/model hop per invocation. Runtime performs no provider matching, and no vendor metadata chooses a saved protocol. |
| **FC-12** | `api.md` | **Revised 2026-08-11 by the owner-authorized K4 completion; guard/nonce simplified by the 20:35 owner subtraction ruling, with deletion/recreate, process-local reservation, direct-Route scope, guard error/plan relations, canonical plan order, blocker/Probe/credential-response ownership, both-channel re-auth acknowledgement, OAuth explicit-cancel totality, and terminal materialization-error response ownership closed through 2026-08-12 01:28.** Contracts Source CRUD, add-only unsaved connectivity/protocol observation and discovery, the sole saved `POST /sources/<id>/refresh` mutation, the unified `/sources/<source_id>/models` subresource for manual creation, all-inventory reasoning-list edits, discovered-model retirement, and manual deletion, the explicit backend Source-order PUT, exact route GET/PUT, mode, events/provenance, native-config import, OAuth, and Direct-mode responses. Source create returns exact `added_to` positions; Source cards use `adopted_by`; an optional nonce is reserved only in process before work and persisted on the Source only at commit, while committed retries conflict and reconcile through the ordinary Source list. Nonce uniqueness covers only live-process reservations and live Sources: clients read before retry, and restart or deletion plus list miss makes a same-nonce request a fresh create. OAuth start claims its nonce/vendor/channel tuple before provider work and coalesces a concurrent retry; failure or task cancellation before a flow exists releases after cleanup, while explicit cancellation of a nonce-bearing committed flow retains that same terminal `cancelled` flow until its existing expiry and never starts the provider on a delayed retry. A no-nonce cancel still forgets. Every Source/inventory mutation mirrors §4.5's total matrix row-for-row, including unguarded writes that cannot change `id`, `origin`, or Routes. Every guard lead error requires its corresponding nonempty evidence array; both plan arrays are duplicate-free and `would_interrupt` uses canonical outer `(backend, model_id)` plus inner stable-Agent-id order; every nonempty forced guarded-impact plan requires exact echoes of the refusal's `would_remove_hops` and `would_interrupt`. A missing or changed echo returns the same 409 family with the new plan and never removes an unconfirmed hop, while an empty recomputed plan follows ordinary success with force/echo inert. API-key credential success uses only `{source, removed_hops, interrupted}` and exposes repair through `Source.state`. Successful terminal OAuth re-auth alone owns the complete `{recovered, interrupted_pairs}` tail and may report an empty array; a terminal materialization error instead uses the standard error envelope and carries nonempty `interrupted_pairs` if and only if acquisition-stage Source mutation has already stranded supply, otherwise omitting the field. A visible noninterrupting `route_replace` removal is ordinary success; only protected-supply interruption enters that guard. The network totality table uses `stream_started` at the first user-visible model-output byte: it admits shaped errors before or after that boundary to existing Source classifications, gives an unclassified pre-output connection failure bounded live backoff, clears the streak only on later output from that same Source, normalizes an expired overlay before API serialization, and makes an unclassified post-output interruption event-only. A live overlay applies only over a healthy capability-present hop; cooldown, needs-action, error, missing Source, and unsupported model keep their stronger projections, while native-process unavailability is the sole reason-priority exception. Probe `connection_failed` is confined to the Hub/unreachable/null-latency shape. Forced Source/manual-model deletion preserves survivor order; discovered-model DELETE instead persists `retired: true`, retains the row through refresh, and uses the same exact-hop and protected-supply guards. OAuth-start rejects a duplicate native Source before adapter invocation. No saved `/test`, separate saved discovery, route policy, separate mapping, takeover, experimental-consent, observation-evidence detail, durable pre-create claim, receipt/digest, or vendor-guessed protocol surface exists. |
| **FC-13** | `migration-scan.schema.json` | Describes only the user-facing copy-only import of existing Claude, Codex, and OpenCode local configuration. Its action enum and one contract example/fixture per value mirror §6's authoritative Native-config import action matrix, including the `keep_native` default and the rejected/deferred `reauth` and `controlled_import` cases. Successful `keep_native` and `import` items run Add Source's one-time match and sole §4.2 placement policy, then report the same visible `added_to` positions. Originals are never modified or deleted, duplicate native selection fails before OAuth or partial commit, and it contains no Model Hub internal contract/data conversion. |
| **FC-14** | `runtime-dependency.schema.json` | Defines the single local engine asset, immutable version/SHA, loopback binding, lifecycle/health, management and Gateway tokens, and fail-closed behavior. `installing` is exactly the unverified non-listening shape with null installed version; any verified/listening runtime settles outside that transition. Engine availability is local Gateway health and never mutates an upstream Source cooldown. This entry does not widen the GA asset-mirror or platform-matrix research scope. **Installing-shape revision: 2026-08-11 23:09 K4.** |

The final set contains no `subscription_hub_experimental`, `experimental_consent_at`,
per-source consent record, `consent_required`, `opencode_api_key_only`,
`openai_compatible`, scalar model-entry `reasoning_effort`, `follow | custom`,
route `policy`, `order_enrolled_by`, separate `mappings` field, `mapping_applied` event,
`mapping` event reason, compatibility
discriminator, or internal contract-conversion transaction. I1 verifies the contract,
core, and shared-test absences; I2–I5 and I7 verify their owned runtime, UI, fixture, scenario,
and locale surfaces before release.

**Implementation and test landing checklist (complete before release).** The K5, K6, I1–I5, and I7 PRs
collectively touch every applicable row and leave no downstream compatibility task:

| Landing point | Required result |
| --- | --- |
| `config/v2_config.py` | Final Source/model/protocol/configured-route types, one canonical validation boundary shared by every persisted-Source writer and subsequent load, singleton and exact-pair uniqueness validation, one explicit Source order per backend and one explicit hops array per menu model, persisted discovered-model retirement plus the committed Source-create `client_nonce` column, serializer completeness, fresh-install Gateway construction, and existing-install-with-no-Hub-state Direct onboarding. No durable pre-create claim, receipt, digest, tombstone, terminal-envelope snapshot, plaintext key, or policy state exists. A committed nonce survives with its Source, Source deletion releases it, and the post-read same-nonce request creates a new id. Unclassified transport failure and live connection backoff never serialize into Source/config state. **Retirement/nonce/network revision: 2026-08-11 21:14 K4.** |
| `core/handlers/model_hub/{service,resolver,classification,errors}.py` | One §4.3 stored-chain executor and one Add Source implementation of §4.2's placement policy; the latter chooses and persists each matched hop position before returning `added_to`. Also owns the complete credential-failure matrix keyed by refresh capability, closed error classes, `adopted_by`, Source-global health, local-engine distinction, and all route/inventory guards. After K4, #1312, and K6 merge, I7 owns `resolver.py`, `classification.py`, and `tests/test_model_hub_resolution.py`: Add-time matching, inventory membership, new-Route validation, runnability, and invocation admit only `retired: false` rows; the network classifier executes every shaped/unclassified × `stream_started: false/true` cell at the first user-visible model-output boundary and alone owns live connection-backoff state. Only later output from the affected Source resets its streak; another Source's successful fallback does not. Before serialization, the read assembler normalizes an expired overlay to underlying non-backoff health. Concurrent cooldown, needs-action, error, missing-Source, or unsupported-model facts suppress a live overlay and keep the stronger projection; none may roll up as waiting. Simultaneous native-process unavailability is the sole exception and takes reason precedence while backoff health/deadline remains. `service.py` is the single shared guard planner for every matrix row; it recomputes and compares duplicate-free refusal arrays, emits SupplyGap rows in canonical `(backend, model_id)` order with stable-id-sorted `agents`, enforces each lead error's corresponding nonempty plan array, commits noninterrupting `route_replace` normally, and uses `source_last_supplier` only for interruption. It is also the sole AC-53 materialization decision owner: a failed terminal settlement attaches the exact nonempty acquisition-stage `interrupted_pairs` report only after persisted route impact, otherwise omitting the field. Discovered-model DELETE persists the retirement tombstone and refresh never revives it. Source-create reserves its nonce only in process before work, releases it only after retained-material reconciliation, atomically reclaims it after release, persists it only on success, and returns committed conflict for list-based reconciliation; process restart or deletion releases the live-only nonce and the post-read same-nonce request is a fresh create. Runtime never matches, places, substitutes, or writes a network health verdict. A static-key `401` performs no retry; only refresh-capable credentials receive one bounded refresh. **Turn-outcome dependency revision: 2026-08-12 02:00 K4.** |
| `modules/agents/model_hub.py`; Model Hub failure callbacks in `modules/agents/{base,claude_agent}.py`, `modules/agents/codex/{agent,event_handler}.py`, and `modules/agents/opencode/{agent,poll_loop}.py` | After K4, #1312, and K6 merge, I7 moves the native launch seam from persistent 30-second network cooldowns to AC-50's phase-aware decision. Each backend callback supplies exact failure shape and `stream_started`, which flips only at the first user-visible model-output byte; `modules/agents/model_hub.py` alone classifies and chooses persistent Source state, live connection backoff, or event-only handling. Exact cross-backend fixtures in `tests/{test_claude_agent_sessions,test_codex_agent,test_opencode_server}.py` cover both phases and prove zero persistent write for unclassified transport failures; the post-output same-current-hop fixture consumes K6's G-34 outcome/copy/payload row exactly. `tests/{test_claude_cli_path,test_multi_platform_runtime}.py` receive only required signature adaptations. I2 owns the equivalent raw phase facts only for the managed Gateway. **Turn-outcome dependency revision: 2026-08-12 02:00 K4.** |
| `core/handlers/model_hub/{oauth,native_oauth,revocations,migration}.py` | Vendor-specific OAuth defaults, duplicate-native rejection before adapter work, all-exit transient credential cleanup, durable revoke reconciliation, and pre-provider exact-tuple OAuth-start claim/coalescing/release/flow conversion and nonce echo. The registry releases only a failed/task-canceled start before a flow exists; it retains an explicitly canceled nonce-bearing committed flow as the same terminal `cancelled` flow until existing expiry, then releases the tuple. The row also owns the complete four-row native-config action matrix with original files untouched. Every imported Source passes the canonical final-shape validator before commit; no import writer bypasses it with direct dataclass construction. **OAuth explicit-cancel revision: 2026-08-12 00:37 K4.** |
| `core/handlers/model_hub/{rpc,request,provenance,events}.py`, `vibe/ui_server.py`, `vibe/model_hub_client.py`, `core/controller.py` | Final API envelopes/routes mirror the guarded-mutation and guard-totality matrices, including current-plan refusal, exact plan-echo confirmation, empty-plan ordinary success, Source-delete, discovered-model retirement, ordinary noninterrupting Route replacement versus interrupted refusal, existing-chain reorder, runtime install/no-op, all three nonce-retry states, read-before-retry plus restart/deletion fresh-create behavior, nonce-bearing canceled-flow replay/expiry versus no-nonce forget, terminal materialization error/report totality, and live connection-backoff reads. `rpc.py`, `request.py`, `ui_server.py`, and `model_hub_client.py` only carry refusal plans when a refusal exists; they do not mint or persist confirmation state. For AC-53, `ui_server.py` and `model_hub_client.py` preserve an exact nonempty `interrupted_pairs` error member and omit it otherwise; they never synthesize `[]`, `flow`, or future-tense `would_interrupt` on an error. After K6, `events.py` transfers from I1 to I7 and records each network-totality cell without turning an unclassified failure into persistent state; unclassified interruption after user-visible model output is event-only. Provenance mirrors the complete K6-frozen turn-outcome matrix, including `canceled` and G-34's selected row, and never invents an outcome id, copy key, or payload shape. Direct-mode responses remain explicit. During implementation, absence of `VIBE_MODEL_HUB_ENABLED` keeps the controller/routes/UI disabled; explicit enablement must construct the v3 aggregate. This is a release-control gate, not a compatibility layer. I6 removes it only after I5 evidence is green. **Turn-outcome dependency revision: 2026-08-12 02:00.** |
| `core/handlers/model_hub/adapter.py` | Exact byte mirror of `model-hub-contracts/adapter-interface.py`, including three protocols and observation/cleanup signatures. |
| `core/handlers/model_hub/turn_gateway.py`, `vibe/model_hub_runtime/**` | Exact stored-hop execution, three-protocol transport, credential refresh capability, authoritative `stream_started` facts at the first user-visible model-output byte, pre-output fallthrough, post-output no replay, and local Gateway failures at any request phase excluded from Source state and fallback. I2 transports raw phase/shaped-error evidence but does not own the network taxonomy, Source decision, or live-backoff executor transferred to I7. The runtime owns the persisted install worker/lease, orphan reconciliation, host detection, installed-state no-op, and the exact installing shape `{installed_version: null, verified: false, listening: null}` without owning HTTP routes. Turn copy is selected only from the K6-frozen authoritative outcome matrix after I7's state decision and live annotation of the same stored chain. **Turn-outcome dependency revision: 2026-08-12 02:00 K4.** |
| `ui/src/components/settings/models/**` | Final Source/Gateway types and calls; protocol selector only after failed observation as a probe-order hint; Source cards reuse `adopted_by`; Add Source renders the stored backend Source order plus returned `added_to` positions without inferring newness from order; state ⑤ alone emits `accept_unavailable_inventory: true`, while clean creation and pull-origin state ⑤′ omit it; backend groups consume `supply_status`; takeover derives from the chain; saved Source details exposes only guarded Refresh models; Direct groups expose reversible mode actions. For frame 02, I4's current increment opens the editor from each model row, renders the drawing, and consumes the chain read projection only: it wires no chain-write caller and keeps Save disabled. Exact stored-hop/mapping editing and exact two-array plan echo belong to I4's second increment after K5 round 3 and I7 refusal fixtures merge. Live connection-backoff presentation also belongs to the second increment after K5 round 2 corrects its UI-spec copy and I7 freezes producer fixtures. AC-53 error-gap rendering likewise belongs to the second increment after K5 round 2 registers E6 and I7 freezes positive/negative payloads; `modelsApi.ts` carries only an actually present nonempty `interrupted_pairs`, and `OAuthConnectDialog.tsx` refetches Source state after the materialization error. The current increment consumes master's pre-I7 refusal shape and must not add inactive confirmation fields. K5 round 2 separately removes the withdrawn observation request/status detail slots. The current increment also consumes the already-specified install/unsupported-host states; the second consumes the remaining K4 projections/actions only after their named K5 round-2 upgrades. **Materialization-response revision: 2026-08-12 01:28 by PM ruling.** No bottom-only “new” section, policy control, “not enabled” hint, latency/last-check copy, separate experimental-consent surface, separate mapping object, takeover field, supplying-backends field, or vendor default protocol exists. Any new-item marker is transient presentation state, not a route field. `vendorMeta.ts` may order probes but cannot choose a saved protocol. Narrowing `SourceProtocol` must be checked against production and test files, not only the production-only `tsconfig.app.json` program. |
| `ui/src/i18n/{en,zh}.json`, `vibe/i18n/{en,zh}.json` | Exact mirrored final reason/detail and `supply_status` keys, including `native_cli_unavailable` and `route_unconfigured`; every Turn-outcome copy-matrix key; the derived takeover label; distinct Direct/Native terms; the sole Claude Hub warning; compact protocol-observation failure copy; and no retired policy, separate experimental-consent, or mechanism-copy keys. After K5 round 2's UI-spec sync, I4's second increment adds the distinct short connection-backoff copy for `models.source.backoff.connection_failed`; it never reuses quota-cooldown wording. A registry-generated guard compares closed outcome/discriminator, blocker/remedy, and credential-remedy relations with both locale sets. |
| `tests/test_model_hub_config.py` | The adapter parity and terminal-version gates remain exact; round-trip/completeness fixtures use only final Source and route shapes. It validates committed Source `client_nonce` persistence and live-only uniqueness, deletion release, rejects durable claim/receipt/digest/tombstone fields and unregistered observation evidence, and mechanically closes OAuth cancel totality: pending-start cancellation releases, explicit nonce-bearing cancellation retains the same terminal flow until expiry, no-nonce cancellation forgets, and the old blanket-forget promise is absent. It proves live backoff cannot serialize as Source state, rejects pure cooldown/backoff chains mislabeled `interrupted`, and keeps durable/capability blockers interrupted under a concurrent deadline. D21 compares AgentChain, ProbeResult, the API marker, and both UI locales individually, with a ProbeResult mutation that must break equality. D22 proves the same five OAuth terminal decision ids exist exactly once in authority and API, success re-auth may carry an empty complete report, materialization errors carry an exact nonempty report if and only if persisted acquisition impact exists, and every other error omits the member. The narrow K4 ledger assertion binds AC-52 and the I3/auth-setup landing row to AUTH-SETUP-109, its two exact scenario files, the pre-adapter negative cells, the provider-once positive cell, the terminal repair projection, and the K4+#1312 activation edge. The same fixture rejects zero-length model supply marked runnable, each installing-shape contradiction, every guard error paired with an empty required plan array, duplicate hop/gap plan entries, and any credential response that revives the OAuth-only repair tail. The K4 AC-54 fixture validates the optional boolean, rejects non-booleans, and freezes every repeat-observation × consent row plus its owner/files/activation handoff. K6 never owns this file; after K6 freezes G-34, I7 adds the mechanical matrix/schema consumer fixture under the three-way activation edge. **Turn-outcome handoff revision: 2026-08-12 02:00 K4.** |
| `tests/test_model_hub_api.py` | The current line-1204 `experimental_consent_at` assertion becomes an absence assertion; API fixtures cover unique final model entries, edited-effort and retirement preservation across rediscovery, distinct add-only unsaved operations versus the sole guarded saved refresh, all-exit discovery cleanup, three protocols, observation-before-save, all three pre-observation nonce retry states, deterministic policy-chosen Add Source placement with the returned persisted position, and every guarded-mutation row. OAuth API fixtures prove nonce-bearing explicit cancel retains the same `cancelled` flow and exact-tuple retry performs zero provider starts, existing expiry releases it for one fresh start, and no-nonce cancel forgets. Terminal materialization fixtures prove a native re-auth failure after acquisition has stranded at least one sibling emits that exact nonempty `interrupted_pairs` in the standard error envelope with no `flow`; the same materialization error with no gap and every non-materialization error omit the member rather than sending `[]`. Successful terminal re-auth keeps the complete array and may send `[]`. Guard fixtures cover every totality cell: unforced empty/nonempty plans, forced nonempty exact/missing/different plan echoes, and a forced request whose old nonempty echo recomputes to an empty plan. They prove every exactly confirmed nonempty plan commits once, every other nonempty forced plan returns the current refusal without removing a hop, every empty plan follows ordinary success, a noninterrupting `route_replace` removal succeeds once without 409, an interrupting replacement refuses with `source_last_supplier`, every produced guard code has its schema-required nonempty evidence array, every produced plan array is duplicate-free, and permuted guard inputs emit canonically ordered SupplyGap rows and agent ids. Source-create fixtures prove process-local pre-work reservation, read-before-retry, in-progress conflict/no work, released atomic reserve/exactly one fresh attempt including after restart, committed conflict plus list lookup of exactly one nonce-bearing Source, AC-26 pending-revocation settlement, and Source-delete release followed by same-nonce fresh creation with a new id and one upstream attempt. AC-54 adds repeated-observation fixtures for failed inventory with omitted/false rejection, true empty-inventory commit, successful rediscovery with stale true, and protocol-unproved rejection regardless of the flag. Observation fixtures reject request/status evidence not present in the six-field contract. Network fixtures cover all four shaped/transport × `stream_started: false/true` cells at the first user-visible model-output boundary, same-Source output reset, other-Source fallback non-reset, clocked future/expired read assembly, bounded auto-clearing live backoff, and zero config writes for unclassified failures. Concurrent-transition fixtures overlay an active deadline with healthy, cooldown, needs-action, error, missing-Source, unsupported-model, and unavailable-native facts: only healthy/capability-present hops project ordinary backoff; durable/capability blockers keep their established projection and `interrupted`; the native exception preserves deadline with reason priority. Probe payload fixtures cover the exact Hub/unreachable/null-latency relation. Credential PUT payloads use only the standard guarded Source envelope; the OAuth repair tail remains flow-only. AgentSupply fixtures prove the valid `{chain_length: 0, has_runnable_hop: false}` pair, reject the corresponding true pair, and prove pure process-available cooldown/backoff chains are waiting rather than interrupted. The same file covers Source deletion from all backend Source orders and routes with survivor order preserved, discovered retirement, existing-chain reorder, exact installing shape plus runtime install/no-op, duplicate-native pre-adapter rejection, adoption, and absence of policy/enrollment/takeover/supplying sibling fields. **OAuth terminal fixture revision: 2026-08-12 01:28.** |
| `tests/test_model_hub_{resolution,runtime,oauth,l3}.py` | After K6 merges, `tests/test_model_hub_resolution.py` transfers to I7 and proves `retired: false` filtering in Add-time matching, inventory membership, new-Route validation, runnability, and invocation plus the complete network-classification/live-backoff table; I2 retains runtime/l3 files and supplies exact managed-Gateway raw shaped/transport facts and `stream_started` phase at the first user-visible model-output byte without owning state decisions, while I3 retains OAuth. Together they cover sole §4.3 stored-chain consumption, no runtime matching or substitution branch, exact effort membership, every K6-frozen turn-outcome/copy row including G-34, blocked `no_candidate` detail, `native_cli_unavailable`, engine loss before/during/after streaming with no Source mutation or replay, persisted install/restart/no-op behavior, canceled provenance, vendor OAuth defaults, and OAuth nonce released/in-flight/committed totality with pre-provider claim, concurrent coalescing, cleanup release, atomic flow conversion, echo, explicit canceled-flow replay with provider-zero, and expiry-triggered fresh start. Until I7 lands AC-50, only `test_probe_transport_failures_await_ac50_backoff_contract` carries the corrected narrow authorization: it already asserts the future `connection_failed` live-backoff projection and zero Source/config writes under strict xfail, while `test_chain_projection_and_probe_latency_partition` and all unrelated assertions remain active. I7's first mechanical action deletes only that marker in the same commit that makes these forward assertions XPASS; an accidental early implementation is therefore a failing XPASS. I1 moves the versioned contract/provenance fixtures in `test_model_hub_l3.py` to terminal version **5** through #1312; afterward I2 owns that file's runtime updates except for the one deletion. **Turn-outcome dependency revision: 2026-08-12 02:00.** |
| `ui/src/components/settings/models/**/*.test.*` | No protocol control on the normal add flow, honest manual probe-order fallback, final inventory editing, visible and adjustable backend Source order plus policy-chosen Add Source placement, no position-based newness or bottom-only new section, one guarded saved refresh button, adopted/supply-status projection consumption, derived takeover versus exhausted rendering, reversible Direct mode, distinct Native copy, and no policy/not-enabled/latency or separate experimental-consent surface. Under the 2026-08-11 18:10 PM ruling, the current-increment frame-02 fixture proves that a model row opens the drawn editor, renders the chain read projection, keeps Save disabled, and emits no chain mutation. Exact configured-chain editing and its save/refusal/reconciliation tests are second-increment obligations after K5 round 3 and I7 fixtures merge; a visible noninterrupting hop removal sends one ordinary PUT with no wire confirmation, while a protected-supply interruption echoes both returned refusal arrays, replaces the displayed plan when either differs after recomputation, and never forces a nonempty plan without an exact echo. The Add Source client reads Sources before a lost-response retry and treats a released nonce as fresh. Only state ⑤ sends `accept_unavailable_inventory: true`; clean creation, every retry that has not received that consent, and pull-origin state ⑤′ omit it. After K5 round 2 and I7 fixtures, the same second increment proves live connection backoff uses its distinct short-delay copy rather than quota cooldown copy; K5 round 2 removes the withdrawn observation request/status slots. I4 supplies a mechanical type-check gate that includes these test files despite `tsconfig.app.json` excluding them. |
| `tests/scenarios/auth_setup/{catalog.yaml,test_auth_setup_scenarios.py}` | After K4 and #1312 merge, I3 owns AUTH-SETUP-210 and AUTH-SETUP-109. AUTH-SETUP-210 blocks the first OAuth-start provider call, loses that caller's response, overlaps a same-nonce retry that coalesces to the same terminal flow/provider, then asserts provider start exactly once. AUTH-SETUP-109 selects a Hub-held Source and proves missing and false re-auth acknowledgement return `reauth_confirmation_required` before any adapter/provider call, true acknowledgement starts exactly one Hub flow, and terminal status plus repair projection agree. Pending-start failure/task-cancellation release and committed-flow replay cover the other nonce rows; I3 unit/API fixtures separately prove explicit canceled-flow replay starts no provider and expiry permits exactly one fresh start. I5 consumes both completed scenarios but does not edit either file. **Hub re-auth scenario binding revised 2026-08-12 01:30 by PM ruling.** |
| `tests/scenarios/model_hub/**`, `tests/scenario_harness/model_hub_native_oauth.py` | End-to-end final-shape setup, all four native-import action rows, reversible Direct/Gateway onboarding, subscription custody, protocol observation, one-time add matching plus deterministic persisted placement, exact configured route execution and mapping, guarded saved refresh and Source-delete envelopes, silent successful takeover, truthful blocked/native/engine terminal copy, and exhaustion failure without takeover semantics. The I3-owned migration scenario validates each imported Source, serializes the full result, and reloads it through the same canonical validator. |

#### Sealed current-consumer findings — reviewed head `5cffd3fff7`

The exact-head review on 2026-08-09 found one premise superseded by the owner's
zero-migration ruling and eight implementation consumers that still expose the pre-S-1
shape. This is the review-loop circuit breaker's scope decision: K1 does not grow into
the I1/I2/I4 implementation batch. The eight live defects below are binding release
work under their existing ACs and landing rows; the default-off release gate remains in
place until those lanes and I5 evidence merge. The review text is retained verbatim so
the implementing lane receives the evidence rather than a paraphrase.

| Review thread | AC / disposition | Landing point | Responsible lane |
| --- | --- | --- | --- |
| `3742846987` | Historical owner ruling 2026-08-09: no internal v4-to-v5 data migration. Superseded by the 3.0.10 upgraded-install incident: `V2Config.load` now performs a disk-boundary migration and preserves strict `from_payload` validation; unrecoverable sections start with safe defaults while the original file is backed up. | Final-contract handoff and `config/v2_config.py` load boundary | K1 ruling superseded; migration/recovery is owned by `config/v2_config.py` |
| `3742846989` | AC-22; valid prelaunch consumer gap | Agent projection/write flow in `service.py` and Models route editor | I1 + I4 |
| `3742846991` | AC-26; valid prelaunch consumer gap | Source serializer plus all Models `SuppliedModel` consumers and mocks | I1 + I4 |
| `3742846992` | AC-30; valid prelaunch consumer gap | No-candidate provenance producer, runtime blocker propagation, and pull-surface rendering | I1 + I2 + I4 |
| `3742846996` | AC-33 / terminal-version handoff; valid prelaunch consumer gap | Outer Model Hub REST envelope and its contract/API tests | I1 |
| `3742846998` | AC-22; valid prelaunch consumer gap | Source-order PUT/read projection and Models Source-order editor | I1 + I4 |
| `3742847000` | AC-26; valid prelaunch consumer gap | Model-delete refusal/force/success envelope and confirmation consumer | I1 + I4 |
| `3742847001` | AC-26; valid prelaunch consumer gap | Per-model reasoning-efforts inventory editor and UI evidence | I4 |
| `3742847002` | AC-26; valid prelaunch consumer gap | Refresh merge implementation and API test | I1 |

> **Preserve v4 model provenance when loading config**
>
> On any upgraded installation with saved Model Hub models, the existing JSON contains
> `provenance` because the parent serializer wrote that key, but this loader now reads
> only `origin`; every non-empty legacy model inventory therefore raises
> `Config 'model_hub.sources.models.origin' is invalid` during `V2Config.load()`. Accept
> and migrate the legacy key before requiring the v5 spelling so persisted Sources
> remain usable after upgrade.

> **Keep persisted mappings in the live UI projection**
>
> When Claude or Codex already has custom model mappings, removing them from every Agent
> response makes `AgentCard` display those routes as Global because it reads
> `agent.mappings`; more seriously, the next route edit in
> `SettingsModelsPage.setModelRoute` reconstructs the total mapping write from that
> now-empty projection and can overwrite all previously stored mappings with only the
> newly edited one. Do not strip this field until the current Web UI is migrated to an
> equivalent authoritative route read/write flow.

> **Align the Source model discriminator with the current UI**
>
> For every Source returned by the live API, this serializer now emits `origin`, while
> `ui/src/components/settings/models/types.ts::SuppliedModel`, `OpenCodeMenuDrawer`, and
> the custom-model helpers still read `provenance`. Consequently manual models loaded
> from the server are no longer recognized as editable/custom models, even though the
> mock API continues using the old property and masks the regression. Update the Web UI
> consumers in the same transition or retain a compatible projection.

> **Populate blockers for blocked no-candidate turns**
>
> When a non-empty route has no runnable hop because Sources need re-auth, key
> replacement, top-up, or native CLI repair, the v5 provenance record still writes
> `blockers: []`, making it indistinguishable from an unconfigured route and preventing
> consumers from rendering the required remedy. Fresh implementation evidence beyond
> the earlier documentation thread is that both `mark_no_candidate` paths retain only
> `supply_state`, and this new producer hardcodes the blocker array; carry the exact
> blocked-hop identities and reasons into this record.

> **Bump the outer REST envelope to contract version 5**
>
> Every Model Hub endpoint still builds its success and failure envelope from
> `CONTRACT_VERSION`, which remains 3, even though this change declares the final REST
> contract to be v5 and bumps the nested chain, probe, and runtime payloads to 5. Any
> client that validates the advertised v5 envelope will therefore reject every response
> before it can inspect the nested object; advance the outer constant with the rest of
> the atomic contract transition.

> **Complete the policy-free source-order transition**
>
> When a v5 caller follows the new API contract and sends
> `PUT /agents/<backend>/sources` with `{order: [...]}`, `set_agent_sources` still
> rejects it because the implementation accepts only the retired `policy` bodies; at
> the same time this read projection now omits `policy`, causing the current
> `SourceOrderDrawer` to default every persisted Custom order to Follow. Migrate the
> write handler and Web UI together instead of removing only the read discriminator.

> **Return the guarded cascade envelope for model deletion**
>
> When a manual model is referenced by configured routes, the non-forced delete returns
> only the generic `mode_switch_blocked` error, so the UI receives neither
> `would_remove_hops` nor `would_interrupt` and has no information with which to present
> the required force confirmation. A forced delete then returns only the Source despite
> pruning references, omitting the corresponding `removed_hops` and `interrupted`
> result; implement the shared guarded-mutation envelope so the deletion can be
> confirmed and reported without silently losing route information.

> **Expose reasoning-effort editing in the Models UI**
>
> For every model created through the current Models page, this dialog hardcodes
> `reasoning_efforts: []`, and repo-wide UI search finds no React caller of the newly
> added `updateModelReasoningEfforts` API. Users therefore cannot declare any supported
> reasoning effort for either discovered or manual inventory entries, despite the new
> capability being persisted and exposed by the server; add the specified inventory
> editor rather than shipping an unreachable PATCH method.

> **Preserve per-model discovery timestamps on refresh**
>
> When refresh returns a model ID that was already in the discovered slice, the code
> correctly preserves its edited display name and reasoning capabilities but always
> replaces `discovered_at` with the refresh time. That destroys the model's original
> discovery timestamp on every successful unchanged refresh and contradicts the
> separate `last_discovered_at` field that records inventory freshness; copy
> `existing.discovered_at` for retained IDs and stamp only newly discovered entries.

#### Sealed current-consumer findings — reviewed head `f57f6f2b1f`

This is the second reviewed head on the same prelaunch-consumer root-cause class, so the
review-loop circuit breaker remains open: K1 records the complete inventory and does not
patch I1/I2/I3/I4 implementation. Every finding is valid implementation evidence against
the final contract and remains release-blocking under the default-off gate.

| Review thread | AC / disposition | Landing point | Responsible lane |
| --- | --- | --- | --- |
| `3742889356` | AC-19; valid prelaunch producer/consumer vocabulary gap | `_source_eligibility`, final TypeScript union, and both locale objects | I1 + I4 |
| `3742889357` | AC-26; valid prelaunch inventory-edit gap | Existing-model display-name edit payload and Models API/UI test | I4 |
| `3742889359` | AC-22 / FC-13; valid prelaunch import-result gap | `migration_apply`, RPC/result type, and native-import UI flow | I1 + I3 + I4 |
| `3742889361` | AC-26; valid prelaunch invocation gap | Exact-model effort membership in the shared service/runtime invocation path | I1 + I2 |

> **Keep emitted eligibility keys in the UI vocabulary**
>
> When a Claude or Codex subscription Source is projected for the OpenCode agent,
> `_source_eligibility` still emits `models.eligibility.opencode_api_key_only`
> (`core/handlers/model_hub/service.py:1903-1904`), even though this change removes that
> value from the TypeScript union and both locale files. The resulting `/agents` payload
> violates the newly closed vocabulary and the UI can display an untranslated key.
> Fresh implementation evidence beyond the earlier documentation finding is that the
> live service producer was not migrated; map this case to the retained reason or update
> the producer together with the vocabulary.

> **Preserve reasoning capabilities during display-name edits**
>
> When an existing manual model has a non-empty `reasoning_efforts` list, opening its
> current edit dialog submits `reasoning_efforts: []`; `add_custom_model` treats that
> existing entry as an update and assigns the submitted list at
> `core/handlers/model_hub/service.py:2321-2325`. Thus changing only the display name
> silently erases the persisted capabilities. Fresh evidence beyond the earlier
> unreachable-editor comment is this destructive existing-entry upsert path; submit the
> current list or use a mutation that does not replace capabilities.

> **Return the contracted migration placement results**
>
> When native-config import succeeds, this v5 route now promises `added_to`, but
> `ModelHubService.migration_apply` still returns only `{applied, sources}`
> (`core/handlers/model_hub/service.py:3166-3176`), and the frontend
> `MigrationApplyResult` likewise has no placement field. A client following the final
> contract therefore cannot receive or display the exact Route positions created by the
> import. Update the service, RPC/UI type, and import flow together, or remove this field
> from the authoritative response contract.

> **Enforce per-model reasoning capabilities before invocation**
>
> When a turn requests a reasoning effort that is absent from this newly persisted list,
> the value is never consulted: the only later engine call forwards the original request
> unchanged at `core/handlers/model_hub/service.py:3271-3282`. Consequently an
> unsupported effort reaches the upstream instead of being replaced with `null`,
> producing the terminal parameter failures the v5 execution contract is intended to
> prevent. Apply the exact-model capability check in the shared invocation path so
> Claude, Codex, and OpenCode inherit the same behavior.

#### Sealed current-consumer findings — reviewed head `8572a542ab`

This is the third reviewed head on the same prelaunch-consumer root-cause class. The
review-loop circuit breaker therefore remains open and the sealed implementation-ledger
ruling continues to apply: K1 records these release-blocking consumers without patching
I1/I2/I3/I4 implementation while the default-off release gate remains in force.

| Review thread | AC / disposition | Landing point | Responsible lane |
| --- | --- | --- | --- |
| `3742927535` | AC-33; valid prelaunch contract-mirror gap | Nested chain/probe version constants, mock responses, and frontend contract fixtures | I1 + I4 |
| `3742927537` | AC-26 + AC-29; valid prelaunch canonical-validation gap | Final Source/model payload and persisted-config validator plus omission fixtures | I1 |
| `3742927538` | AC-26 / FC-12; valid prelaunch guarded-mutation result gap | Source-delete service, RPC, HTTP/client envelope, and confirmation/reconciliation consumer | I1 + I4 |

> **Bump the frontend nested contract mirrors with the backend**
>
> When the Models UI or its mock client consumes a chain or probe, the backend and
> schemas now advertise nested contract version 5, but
> `ui/src/components/settings/models/types.ts:15-17` still defines both versions as 4
> and `modelsApi.ts:781,811` builds mock responses from those stale constants. The live
> client blindly casts responses to these v4 types, while mock-mode tests continue
> exercising v4 payloads, so the frontend no longer provides contract-version coverage
> for the v5 transition; update the TypeScript constants and affected fixtures in the
> same atomic bump.

> **Reject final model entries that omit reasoning efforts**
>
> When a v5 persisted model or Source-create payload omits `reasoning_efforts`, this
> default silently accepts it and serializes the entry back with `[]`, even though
> `source.schema.json` requires the field and the final contract defines an empty array
> as an explicit declaration that the model supports no reasoning effort. This lets
> malformed final-shape data bypass the canonical config boundary and changes omission
> into a capability decision; require the key to be present rather than defaulting it.

> **Return cascade details from Source deletion**
>
> When a forced Source deletion removes configured route hops,
> `ModelHubService.delete_source` returns nothing and both
> `core/handlers/model_hub/rpc.py:31-33` and `vibe/ui_server.py:3124-3125` discard all
> mutation detail. This new v5 route contract promises `removed_hops` and `interrupted`,
> so the client cannot reconcile or report which routes were pruned after a successful
> cascade; return the contracted guarded-mutation envelope through the service, RPC,
> and HTTP layers.

#### Sealed contract-and-consumer findings — reviewed head `8287d41da3`

This fourth reviewed head remains behind the same default-off release gate. Two findings
identify final-contract closure still owed by I1 and two identify canonical consumer
validation still owed by I1; none identifies two contradictory normative sentences.
Under the sealed-ledger ruling, K1 records all four without changing contract or
implementation files.

| Review thread | AC / disposition | Landing point | Responsible lane |
| --- | --- | --- | --- |
| `3742980271` | AC-26 + AC-29; valid prelaunch identity-validation gap | Per-Source model-id uniqueness at config, Source-create, and discovery boundaries plus mutation fixtures | I1 |
| `3742980274` | AC-22 + AC-33; valid prelaunch final-contract persistence gap | Final `agent-supply.schema.json` Route rows and V2 config serialize/reload boundary | I1 |
| `3742980278` | AC-23 + AC-33; valid prelaunch final-contract rejection gap | Final OAuth/API failure envelope, pre-adapter singleton guard, and add-flow consumer | I1 + I3 + I4 |
| `3742980279` | AC-26 + AC-29; valid prelaunch credential-boundary gap | Canonical reasoning-effort validator and create/persist/PATCH negative fixtures | I1 |

> **Enforce unique model IDs within each Source**
>
> When a Source-create payload or discovery response contains the same model ID twice
> with different metadata, this new uniqueness invariant is not enforced:
> `ModelHubConfig.from_payload` checks duplicate Source IDs only, and
> `_apply_discovered_models` appends one entry per discovery result. The persisted
> inventory can therefore contain ambiguous identities;
> `update_model_reasoning_efforts` updates only the first match while
> `delete_custom_model` removes every match. Reject duplicate per-Source model IDs at
> the V2 config boundary and deduplicate or reject discovery results.

> **Add persisted routes to AgentSupply**
>
> When Add Source or the route PUT writes `B.routes[M].hops`, the claimed persistence
> shape has nowhere to store it: `agent-supply.schema.json` has
> `additionalProperties: false` but no `routes` property, and
> `ModelHubAgentSupplyConfig` still persists the retired `mappings` list instead.
> Consequently the exact chains required by §4.3 cannot survive serialization/reload,
> and implementations cannot satisfy FC-05 without changing this supposedly final
> schema. Add the per-menu Route rows to both the schema and the V2 config boundary.

> **Contract duplicate-native OAuth rejection**
>
> When a backend already has its singleton `native_cli` Source, AC-23 requires OAuth
> start to return `native_source_already_exists` with `existing_source_id` before
> invoking the adapter, but neither `oauth-flow.schema.json` nor `api.md` defines that
> error or response field; repo-wide occurrences are limited to this prose. A server
> and client can therefore invent incompatible rejection envelopes—or begin the
> irreversible login despite following every frozen contract. Define the exact failure
> envelope in the authoritative API/schema and cover the pre-adapter rejection.

> **Reject credentials in reasoning-effort values**
>
> When a Source-create body, persisted config, or reasoning-effort PATCH contains a
> credential-looking value such as an API key, this validator accepts it as an ordinary
> non-empty effort string and `to_payload` writes it back verbatim. The surrounding
> create path explicitly rejects credential material in model IDs and display names,
> but neither that scan nor `_validated_reasoning_efforts` checks the new list, so
> plaintext credentials can enter config and API responses despite the contract's
> no-plaintext-credentials invariant. Apply the same credential-material rejection to
> every reasoning-effort entry at the canonical config boundary.

After I1 lands, `model-hub-contracts/**` is read-only to I2–I5. An implementation-proven
mismatch is reported to the orchestrator for a targeted decision; the discovering lane
does not edit or reinterpret the contract locally.

### Final-language sweep — push delivery wording

These existing contract descriptions assert a delivery layer that is absent from the
final product. They are listed for **I1** to sweep in its contract PR. Each final
description states feed/UI pull semantics from spec §4.5.

| File | Where | What it still asserts |
| --- | --- | --- |
| `resolution-event.schema.json` | `severity` description | 「proactive IM push」, 「the push layer MUST key off this field」, 「recipients are resolved AT DELIVERY」 |
| `resolution-event.schema.json` | `agent` description | `system` 「expands to no Agent and therefore to no recipient」 (also AC-11's surface), and the same §4.5 heading reference as `turn-provenance` |
| `agent-supply.schema.json` | `supply_status` / `model_supply` descriptions | 「feed only, NEVER an IM push」 and 「`severity: action_required`, proactive push」 |
| `agent-chain.schema.json` | `supply_state` descriptions | a 「suppressed action-required push」 as the reason a value exists |
| `source.schema.json` | `state` description | a source 「owed an `action_required` push」 |
| `turn-provenance.schema.json` | `model_supply_state` description | points at §4.5 「Who receives an action-required push」, a heading that no longer exists |
| `README.md` | mirror-registry prose | the blast radius of a vocabulary described in push terms |
| `README.md` | the round-7 note (`:178`, `:228`) | 「§4.5's recipient rule now expands in two hops, backend → enabled Agents → scopes」 and 「expands to no Agent and therefore to no recipient」 — the two-hop recipient resolution the 10:54 cut superseded |
| `api.md` | events feed row (`:43`) | 「the IM push layer keys off `severity == "action_required"`」, 「recipients are resolved at push time … against the live routing table」 |
| `api.md` | mechanical-guard table (`:755`) | 「a push whose 「去处理」 lands on a row that renders nothing」 |
| `api.md` | `POST /sources/<id>/refresh` recover rule (`:559`) | 「severity promotion can push it to the user as news」 — the stated *reason* the unconditional `recover` emit is wrong, so I1 must restate the reason (a 「已恢复」 line in a feed with nothing to recover from) rather than delete the sentence and lose the rule |
| `api.md` | `supply_interrupted` worked payload (`:685-693`) | 「always ELIGIBLE for a proactive push and never feed-only」, `agent: "codex"` as 「the addressing input」, 「the push layer resolves it to the scopes whose routing currently selects a Codex Vibe Agent」, and 「resolving at delivery rather than at emit」 as the kind's rationale |

**The table above is a seed list, not a census — corrected twice, and that is the point
(07-29, review rounds 5 and 6).** Round 4 said 「8 sites」, round 5 raised it to 「12 sites,
six of them dangling cross-references」, and round 6 found both the count and one
attribution still wrong: the dangling 「Who receives an action-required push」 reference
this table placed on `turn-provenance.schema.json`'s `model_supply_state` is actually in
its **`agent` description (`:22`)**, and at least six further delivery-semantics passages
sit outside the twelve — `resolution-event.schema.json` `:49`, `:83`, `:94` and
`README.md` `:98`, `:109`, `:145-146`.

A hand-counted list of a moving target has now generated a finding in three consecutive
rounds, so **I1 must not treat this table as the sweep.** The sweep is mechanical and its
result, not this table, is the completion condition of the final contract commit. Run both from
`docs/plans/model-hub-contracts/`:

```
grep -rn 'Who receives an action-required push' .
grep -rniE 'push|IM 推送|notif|alert|interrupt the user|proactive' .
grep -rnE 'IM surfaces?|IM 平台|conversation surface' .
```

The first must return **zero** hits in the final contract set — the heading no longer exists. The
**third was added in review round 7**, because the second misses a whole class: it greps
`IM 推送`, not bare `IM`, so `README.md:258` — which still lists `turn-provenance.schema.json`
as consumed by 「IM surfaces (per-turn detail)」 — survives it untouched, and would have
frozen into v3 a consumer promise the Web-only ruling (AC-1, above) means no lane in this
batch owes. That is a **surface-ownership** promise rather than delivery semantics, which
is why the delivery-semantics pattern never saw it. Its two current hits are both seeds:
that README row, and `turn-provenance.schema.json:5`'s 「the conversation surface reveals on
demand」. **Both are now outright wrong rather than merely loose** (owner ruling 07-29 14:03):
provenance has no chat surface at all, so the final contract must omit the consumer promise and the
rendering phrasing rather than narrow either to Web. The second grep returns a superset
that still needs judgement: `interrupted`/`supply_interrupted`
are state names the design keeps, and 「never pushes」 phrasing may legitimately survive as
a statement of the cut. What must not survive is any passage a later lane could read as
licence to build proactive delivery, or any pointer into a deleted heading. The rows above
are the seeds — the ones already read and characterised — and they save I1 the reading,
not the sweeping.

**I1's final-contract mandate includes purging ALL delivery-semantics text the sweep returns**, not
just the rows that also change a shape. The distinction matters because these files are
the normative contract: a description that still says 「always ELIGIBLE for a
proactive push」 will be read by L2/L3 as licence to build the push the owner cut, and a
dangling §4.5 reference sends the reader to a heading that no longer exists — a
transcription defect either way. None of them changes a *shape*; the fix is to restate
each as feed/UI semantics (spec §4.5), inside I1's contract PR.

One entry changes meaning rather than just wording, per AC-6's downgrade — and with it
the **root the expansion starts from** (07-29, review round 2). The
`resolution-event.agent` description's 「consumers MUST expand: backend → Vibe Agents →
scopes」 was a *recipient* rule anchored at the event's own backend. Leaving live
derivation anchored there would rebuild the exact one-backend defect AC-6 exists to
remove: an event carrying `agent: "claude"` because Claude's turn discovered the failure
can never reach Codex, however many Codex chains the source sits in. L1 restates the
rule so that for a **source-scoped** kind the derivation starts from **`from_source`** —
a backend is affected when that source is **blocking now** and appears in the capability
chain of one of its protected models, both evaluated against current state rather than
against the event (§4.5, and AC-6's acceptance) — while `agent` stays what it says it
is, the discovering context. The
`backend → Vibe Agents → scopes` hop keeps its original root only for **backend-scoped**
`supply_interrupted`, where the backend genuinely is the event's subject rather than a
consequence of it. No field is added to that schema (AC-6).

### AC-1 — Define provenance for Direct-mode turns

Review round 8, P1, on `docs/plans/model-hub-contracts/turn-provenance.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864623). Verbatim:

> **Define provenance for Direct-mode turns**
>
> Direct remains the existing-user default and can run entirely from native configuration without any Model Hub `Source`, but a successful provenance record must contain a `src_*` identifier here. A successful post-feature Direct turn therefore cannot validate: the implementation must either fabricate a source or return `turn_not_found`, despite the contract promising provenance for each turn. Add an explicit Direct/no-source representation, or explicitly scope the endpoint and conversation affordance to Hub-mode turns.

**Spec action at round 8.** `model-hub.md` §4.5 「Turn provenance」 now states that the frozen interface covers Hub-mode turns and names this as AC-1; the schema is unchanged.

**Acceptance.** A successful Direct-mode turn is inspectable **through the contracted route** without any `Source` row existing: either the response validates against a documented no-source representation, or `GET …/provenance` answers a documented 「此回合无中枢记录」 error. **The tests assert route behavior, not UI** (owner ruling 07-29 14:03 — see below). A test asserts a post-feature Direct turn never yields a payload that fails `turn-provenance.schema.json`, and never yields a fabricated `src_*` id. **An exactly-attributable-Hub turn with no record must not answer like a turn nobody ever saw** (07-29 17:41 ruling, closing round 16's `3672822511`): the write rule declines to write for concurrent same-scope turns and for mixed tracked/untracked use, and the only contracted absence today is `turn_not_found` — which is also the answer for an unknown `turn_id`, so the two collide on precisely the indistinguishability `model-hub.md` forbids on the Direct branch. The route answers these two **distinguishably**, and the server **derives it live** at read time — the turn is known to the session store and no provenance row exists — rather than storing a marker for a record it deliberately did not write. **Which code name carries it is part of this criterion's existing L1 v3 call**, not a second one, and the `model-hub.md` every-turn promise is scoped to exact attribution to match. A test asserts an ambiguous Hub turn is not answered with `turn_not_found`. **Attempt-to-turn attribution is not assumed, and where it cannot be had it is not invented:** `failed_attempts` is only meaningful if each attempt is known to belong to *this* turn, which the **process-scoped** gateway credential establishes at launch (07-29 round-7 ruling as amended 14:04, replaced 14:39, **settled 15:07** and cut back to invariants 16:20, §3) rather than at the gateway, where a per-backend token cannot tell two concurrent sessions apart. **The write rule is: record only when attribution is exact.** So this criterion carries an **anti-guess leg**, restated 15:42 as *ambiguous-leg-absent plus control-leg-present* (this sentence still carried the superseded 15:07 pair through round 12 — the §3 and AC-4 restatements landed and this one was missed): under deliberate same-cwd concurrency, a test asserts that **neither turn's provenance contains the ambiguous attempt**, and **the same fixture run sequentially** — no concurrency — **must produce the record**. **A third, named case — *mixed use* — covers the guard the write rule gained at 16:35** (§3): one FSM-tracked Web turn and one **untracked** IM/CLI turn sharing a single process scope, where the tracked turn's provenance must not contain the untracked caller's attempt. It is a distinct fixture from the concurrency case, not a variant of it — there the FSM sees two turns and declines; here it sees exactly one and would attribute confidently, which is why 「exactly one tracked turn」 alone is not a safe test to write against. An implementation that picks one of the two live turns fails the first leg; one that quietly discards everything fails the control; one that reads only the FSM's count fails mixed use. Both legs are needed for the same reason as before — guessed attribution is worse than no record, but a silent drop is not the same thing as an honest absence — while asserting **nothing about the event log**, which the 15:42 ruling deleted from this pairing: events trace transitions, healthy attempts are silent by design, and an unattributable attempt legitimately leaves no trace. The turn linkage is filled server-side at record time and **no request-path contract field carries it**; `turn_id` stays **required**, and L1's v3 owes **no attribution-grain term** — it was retracted at 15:07 (§3), because every written record is turn-exact by construction.

**Which surface — none. Owner ruling 07-29 14:03, superseding the round-6 orchestrator ruling.** The round-6 ruling scoped 「the per-turn affordance」 to the Web conversation surface's turn detail, owned by L5, with IM deferred to v2.1. **The owner has now cut the surface entirely, on both.** The reasoning is a product one and it moves the whole criterion: users should be **unaware of supply machinery**, so provenance inspection is a **debug affordance, not a user feature**, and a per-turn detail hanging off the transcript is exactly the machinery leaking into the surface it should stay behind.

So **AC-1 reduces to record + API truth**: the record is written, the contracted route answers, and Direct mode is represented **honestly at the contract level** — 「no hub record」 becomes an explicit API-level semantic instead of a rendered conversation state. Every conversation-rendering assertion is deleted from this criterion, and **no UI lane owes anything for AC-1**. What survives untouched is everything that is not inspection: the record, the route, the in-turn **error** copy (error UX, the surfacing mechanism the owner endorsed), and L1's v3 contract terms — a debug record still has to be **true** data, which is also why the session-scoped correlation ruling below stands.

**The record's coverage in v2 is bounded, and the criterion says so** (07-29 15:42 ruling): provenance is written only for turns the turn FSM tracks, because the write rule needs FSM truth to be exact, and **no FSM truth → no record**. IM and CLI turns therefore write **no** provenance in v2. Their failures and switches are still traced, because the source-grained resolution-event feed covers every channel, which is what makes the gap debug-marginal rather than a hole in the story. So AC-1's fixture (like AC-4's) drives a turn on an **FSM-tracked dispatch path** — L3's design note enumerates them, and the avibe/Web path is the one in v2 — and it carries **one consistency assertion**: the same fixture driven through a non-FSM path writes **nothing** — not a partial record, not a record with a synthesized `turn_id`. **v2.1 candidate**: extend FSM registration to the IM/CLI dispatch paths; provenance follows for free, since the write rule is path-agnostic and needs no restatement when coverage widens.

If provenance ever gets a surface, it is the **请求日志 / 诊断 page** in the Models page's 高级 area, and that page is a **v2.1 candidate owed by no lane in this batch** (cut list, §3). The 高级 row ships as designed either way.

### AC-2 — Reconcile irreversible native re-auth before returning failure

Review round 8, P1, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864639). Verbatim:

> **Reconcile irreversible native re-auth before returning failure**
>
> For `native_cli`, OAuth has already replaced the credential in the CLI-owned store before post-login discovery or the supply guard runs, and these lines acknowledge that the old login cannot be restored. Nevertheless, the shared repair contract later permits discovery failure or an elective-gap refusal while retaining the old `Source` row as though the prior supply were intact. The next native turn then uses the new CLI account while the persisted models and state still describe the old one. Native re-auth needs confirmation before the irreversible login, or it must commit and report the resulting gaps instead of using the rollback/refusal semantics of engine-owned credentials.

**Spec action at round 8.** `api.md` invariant 5 now scopes invariant 3's guarantee on this channel to its weak sense and forbids presenting a post-re-auth refusal as though the prior supply were intact; the remedy is deliberately not chosen there.

**Owner ruling (2026-07-29 10:44) — confirm before the irreversible act.** Of the two remedies, the owner picked the first: native re-auth shows an explicit irreversibility warning **before** the login starts — it replaces the current login immediately, there is no rollback, and a failed new login means re-authenticating the original account — and the flow can still be aborted at that point. Failure states then render honestly: the old login is gone, and a retry entry is offered rather than a screen implying the previous account is still there. **Hub-channel `api_key` replacement is unaffected** and stays transactional, carrying **no irreversibility warning**, because it is reversible. The exemption is from *that* confirmation only (07-29, review round 2): an elective replacement that would narrow supply still meets the `source_last_supplier` refusal and its explicit `force` override (AC-13). That one is the **conditional supply-gap** confirmation, and it is computable on this channel precisely because discovery precedes commit — the property native re-auth lacks. Reading 「no confirmation step」 as blanket is how L2/L5 would drop the force dialog. AC-13's `force` confirmation reuses this shape rather than inventing a second one.

**Acceptance.** For a `native_cli` source, no path leaves the persisted `models`/`state` describing an account the CLI no longer holds. A test drives a re-auth whose post-login discovery fails and asserts the ruled semantics: the irreversibility confirmation was presented before the login and could abort it, and after the failure the response reports the resulting gaps instead of presenting the prior supply as intact. The confirmation is **unconditional** — it does not consult a supply guard, which pre-login is uncomputable (AC-13). A second test asserts the Hub-channel `api_key` path shows no such **irreversibility** confirmation, and a third holds the guard that path does keep: an elective replacement of a healthy key that would narrow supply is still refused with `source_last_supplier` until `force` (AC-13). Skipping the unconditional warning is not skipping the conditional supply-gap confirmation. Silent divergence between row and store fails all three.

**The confirmation has to be enforced server-side, and v3 owes the term (07-29, review round 7).** As contracted today, `POST /api/models/sources/<id>/reauth` takes no request body (`api.md:31`), so the acknowledgement exists only in the UI: a direct API caller, a scripted client, or a UI regression that drops the dialog starts the irreversible login unacknowledged — and every test above still passes, because they all drive the UI path. The requirement, not the mechanism, is that the route cannot begin an irreversible login it cannot prove was acknowledged. **L1's v3 authors how** (a validated acknowledgement field on the request, or a prepare/confirm transition — L0 does not choose), and a **negative route test** that calls the endpoint without the acknowledgement and asserts the login never started joins AC-2's evidence, owed by L2.

### AC-3 — Allow blocked sources to be re-tested after user action

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864631). Verbatim:

> **Allow blocked sources to be re-tested after user action**
>
> A `balance_exhausted` source remains `needs_action` until a request observes the top-up, but this sentence's promised “next probe or turn” cannot perform that observation: the turn pipeline excludes every `needs_action` source as non-runnable, and the probe returns `probe_no_candidate` when the chain has no runnable member. If it is the only supplier, the source is therefore never called again and can never recover after the user tops up. Add a source-specific recovery probe that may test a blocked source after explicit user action, or otherwise define how the state is cleared safely.

**Final route ruling (2026-08-09).** `model-hub.md` §4.5 retracts the claim that a
topped-up balance is re-checked on a normal turn. The one explicit saved-Source path is
`POST /api/models/sources/<id>/refresh`.

**Acceptance.** A Source that is the only supplier of a model, sitting in
`balance_exhausted`, returns to service after the user tops up and invokes the
Source-details action — without deletion, recreation, or a credential change. Scenario
`model_hub_blocked_source_recovery` drives that visible action and asserts it calls the
sole saved refresh/recovery mutation. The same route may test `needs_action` and
`error`, and AC-11 drives its non-turn event fixture through it.
There is no parallel `/test` or `/recover` Source mutation.

### AC-4 — Represent canceled turns in provenance

Review round 8, P2, on `docs/plans/model-hub-contracts/turn-provenance.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864628). Verbatim:

> **Represent canceled turns in provenance**
>
> Avibe has explicit Stop/cancel paths that settle an in-flight turn as canceled, including `SessionTurnManager.cancel`, but none of these four outcomes can represent that terminal state. It is not `served`, fallback exhaustion, a non-fallback adapter error, or a no-candidate turn; labeling it `failed_terminal` would also require inventing one of the four error reasons and possibly a source attempt. Since this contract says provenance always exists when a turn resolves, add a canceled outcome and define how any attempt that was interrupted is recorded.

**Final-shape action (2026-08-09).** Round 8 left the contract with four outcomes;
FC-08 now requires `canceled` as the fifth terminal outcome in I1's final contract and
same-tested-head closure.

**Acceptance.** A turn settled by Stop/cancel produces a provenance record that is not `served`, not `exhausted`, not `failed_terminal` and not `no_candidate` — i.e. the vocabulary grew — and an attempt that was in flight when the cancel landed is recorded without inventing a failure reason for it. Cancelling mid-stream must not produce a record that claims a source failed. **The classification comes from the turn FSM's terminal state** (07-29 ruling, §3): a test drives a real Stop through `SessionTurnManager` and asserts the canceled outcome, and a second drives a dropped connection on an otherwise identical turn and asserts it is *not* recorded as canceled. Both legs are required, because an implementation that infers cancellation from the transport passes the first and fails the second — and it is the second that decides whether this record can be trusted. **Both legs presuppose that the in-flight attempt can be tied to the turn at all** — which is what the **process-scoped** gateway credential plus the FSM's active-turn lookup delivers (07-29 round-7 ruling as amended 14:04, replaced 14:39, and **settled 15:07**, §3). Without it, a concurrent same-backend turn's attempt can be recorded against the canceled turn and the second leg passes for the wrong reason — so **both legs run on the unambiguous fixture**, one active turn for that scope, where the attribution is exact. A **third leg** covers the case that fixture excludes, restated 07-29 15:42 so that it asserts only against what the frozen contracts already carry — **ambiguous-leg-absent plus control-leg-present**: two turns deliberately concurrent in the same cwd, one canceled, and **neither turn's provenance contains the interrupted attempt**; then **the same fixture run sequentially**, without concurrency, **must** produce the provenance record. It fails an implementation that resolves the ambiguity by picking — which would let a cancellation record blame an attempt that belonged to the turn still running — and equally one that drops silently, since the control leg would come up empty. The 15:07 wording's second half (「the resolution-event log does contain its source-level outcome」) is deleted: events trace transitions, healthy attempts are silent by design, and an unattributable attempt legitimately leaves no trace — the control leg carries the anti-dropping burden without contracting a new kind or reason. The earlier 14:39 wording (「recorded at the coarse grain, marked as such」) is superseded: `turn-provenance.schema.json` requires `turn_id`, so no such record could be written. The classification itself is unchanged: still FSM terminal state, never transport.

### AC-5 — Protect the menu-side model in deletion guards

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864615). Verbatim:

> **Protect the menu-side model in deletion guards**
>
> For a mapping such as `claude-opus-4-6 → glm-5.2`, this top-level spec says the protected set contains the mapping target, while `api.md`'s normative single-home definition requires the mapping's menu-side `builtin_id`. Following this wording tests a resolved ID in the menu namespace, so deletion can miss the actually selected built-in model, proceed without `force`, and interrupt the Agent on its next turn. Replace “mapping targets” with the menu-side models that own mapping rows.

**Spec action at round 8.** FIXED IN SPEC at round 8: `model-hub.md` now protects the menu-side `builtin_id` of every mapping row, matching `api.md`'s single definition of the guard. No implementation debt — this criterion exists so the fix cannot regress.

**Acceptance** (repaired 07-29, review round 12 — see AC-16; fixture pinned 07-29, review round 5). With a single mapping `claude-opus-4-6 → glm-5.2`, one source supplying the target, and **no Agent selecting either model — explicitly or by inheritance**, DELETE of that source is refused without `force`, and the refusal names the affected pair with `SupplyGap.agents: []`. 「Or by inheritance」 is what the fixture must pin, not merely state: ruling #4 counts an Agent that runs a model through `agents.<backend>.default_model` as using it, so a fixture that only says 「no Agent selects either model」 while leaving the backend default at `claude-opus-4-6` puts every enabled Claude Agent in `SupplyGap.agents` and makes the expected empty list wrong. **The fixture therefore sets `agents.claude.default_model` to a third model that is outside the pair and not supplied by the source under test** — so the inheriting Agents run something this DELETE does not touch, and they stay out of the gap for the right reason rather than by accident. Pinning the default is the repair that preserves the fixture's intent; adding an Agent to the expected list would not, because the whole point is to isolate the mapping-namespace term. The empty list is the point: assigning an Agent here would protect the model through the Agent-selection term instead, and the test would stop isolating the mapping-namespace defect — it is also the exact case AC-9's recorded text depends on. Agent-facing confirmation copy is tested in its own fixture, where an Agent does select the model. A guard that compares resolved ids against menu identifiers matches nothing and must fail this test.

### AC-6 — Resolve source events for every affected backend

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864611). Verbatim:

> **Resolve source events for every affected backend**
>
> When a hub API-key source appears in multiple backends' `sources.order`, a failure discovered on Claude changes the source-global health for Codex as well. Expanding only the event's single backend therefore omits Codex-routed scopes from the required push, contradicting the preceding rule that source-scoped events affect every Agent whose order contains the source. Resolve source-scoped events from `from_source` across all backend orders (or emit one event per affected backend), while retaining backend-only expansion for `supply_interrupted`.

**Spec action at round 8, narrowed at round 9, reduced at 10:54, DOWNGRADED 07-29 (orchestrator ruling — owner-vetoable).** The finding's remedy — expand the record across every affected backend — was built for a push that no longer exists, and carrying it into the record layer created a criterion no conforming record could satisfy: `resolution-event.schema.json` has no field for a backend set (`agent` is a single enum). The record therefore stays single-grained. A source-scoped event is recorded once, unattributed: `agent` keeps its current semantics — the discovering context, or `system` — and the record makes no claim about which backends are affected. Under S-1, per-backend impact is **derived live by consumers from persisted per-model chains**: status surfaces read the Source's current blocking state only for exact hops that reference it. The record never stores fan-out, and runtime never constructs another chain.

**Acceptance** (final S-1 reading). One Hub API-key Source referenced by an exact Claude
route hop and an exact Codex route hop fails once, and exactly one source-state failure
record is written — no per-backend fan-out or backend list, with `agent` naming only the
discovering context. Both backends' status surfaces derive the effect from the Source's
current blocker against their persisted chains, never from the retained record. A Source
with no Codex hop leaves Codex unaffected. Emitting one `needs_action` per affected
backend fails the record count; rendering only the discovering backend fails the live
chain projection.

**The derivation input is live state, never the retained record (07-29, review round 6).**
The historical paragraph above said the surfaces were 「computed from that single record
against their current orders」, which read literally licenses exactly the sticky status §4.5 forbids: a
source that recovers on its own — a cooldown lapsing, a quota window resetting — would
stay marked 需处理 on both surfaces until the record aged out, because the record is what
was queried. It is the wrong input. A record answers 「what happened」 and belongs to the
最近切换 feed; a status answers 「what is true now」 and reads the source's **current**
blocking state against each backend's **current persisted chains** (spec §4.5, and
`model-hub.md`'s 「source and agent status read live state, not events」). The record is
what makes the failure *visible in the feed*; it is not what makes the status *true*.

So the acceptance has a recovery leg, and it is not optional: after the same source
returns to a non-blocking state, **both** backends' status surfaces clear on the next read
with **no new event required to unstick them**, while the original failure record stays in
the feed unchanged. An implementation that derives status by querying the event log passes
the failure half and fails this one — which is the whole reason the leg exists, because
the failure half alone cannot tell the two designs apart.

### AC-7 — Represent chain and probe for Direct-mode backends

Review round 9, P1, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669986813). Verbatim:

> **Scope chain and probe endpoints away from Direct mode**
>
> Existing users remain in Direct mode, where `sources` and `selected_model_id` are null and there is no Model Hub source order, yet these endpoints are declared without a mode restriction. A chain request must therefore return an empty `interrupted` chain for a model the native CLI can run, while a probe has neither a default model nor the required `src_*` identity for a valid `ProbeResult`. Define a Direct-specific representation or error and keep the chain/probe affordances from presenting Hub starvation for Direct backends.

**Spec action at round 9.** Half fixed, half recorded. `api.md` now scopes both route rows to `mode: hub` and says why an empty chain is the wrong answer rather than a harmless one: `chain: []` means 「Hub has nothing that can serve this」, which is a false alarm about a backend whose native CLI is running that model fine. What Direct returns *instead* is a shape decision — a documented `direct_mode` error the drawer renders as 「该后端未接入模型中心」, or a mode-specific payload naming the native model with no chain — and this document chooses neither, because either choice edits `api.md` and belongs to the lane that also answers AC-1. Whichever is chosen publishes `contract_version: 3` per the versioning rule above.

**Acceptance.** With `mode: direct`: a chain request for the model that backend is actually running returns the chosen Direct representation and never `ok: true, chain: []`; a probe request returns the same representation and never a `ProbeResult` with a fabricated `source_id`; and the agent drawer offers neither 「试跑一次」 nor a chain view for that backend. An implementation that leaves the Hub-shaped 200 in place fails the first two, and a UI that keeps the affordances and renders the refusal as 中断 fails the third.

### AC-8 — Exclude disabled mapping rows from the protected set

**Current v3 disposition (2026-08-09).** The live-mapping acceptance block retained
below is historical and non-executable. AC-22's final-shape acceptance supersedes it
with configured-route fixtures that exercise protected-model and disabled-configuration
behavior without creating a mapping field or resolver branch.
The guard invariant survives; the historical fixture shape does not.

Review round 10, P2, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099008). Verbatim:

> **Ignore disabled mappings when building the protected set**
>
> When a persisted mapping has `enabled: false`, the resolver treats the built-in model as identity (`core/handlers/model_hub/resolver.py` checks `item.enabled`), but this definition protects every mapping row regardless of that flag. If the disabled row's model is not otherwise selected, deleting or rotating its only source will be refused with `source_last_supplier` even though no live selection would break; only enabled mapping rows should contribute through this term.

**Spec action at round 10.** RECORDED, not fixed. The premise checks out on all three sides: `agent-supply.schema.json` requires `enabled` on every mapping row and its frozen example carries `enabled: false`; `resolver.py` applies a row only when `item.enabled`; and term 2 of the protected set says 「every model that owns a mapping row」 with no filter. **The direction is not open, though** — the same paragraph already rejects over-protection in its own words: treating models nobody selected as protected 「trains the user to pass `force=true` reflexively and hollows out the whole guard」. A disabled row is exactly that case, so this is the guard's own principle applied to the term it was written next to.

**Acceptance** (repaired 07-29, review round 12 — see AC-14). Four **independent** fixtures, one per (route × `enabled`) combination, each built from the same shape: one backend, one mapping row `claude-opus-4-6 → glm-5.2`, `claude-opus-4-6` selected by no Agent and not the backend default, and one source supplying `glm-5.2`. With `enabled: false`: DELETE of that source succeeds without `force` (fixture 1), and an elective `PUT …/credential` onto a narrower key is not refused (fixture 2). With `enabled: true`: both refuse with `source_last_supplier` (fixtures 3 and 4). The fixtures cannot be phases of one run — the successful DELETE consumes the source the credential assertion needs, and running the narrowing PUT first mutates the capabilities the DELETE assertion is meant to inspect, so each assertion must start from the intended initial state. Together they fail an implementation that reads mapping rows without their flag and equally fail one that drops mapping protection altogether.

### AC-9 — Fan out source events only through Agents that use the affected model

Review round 10, P2, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099009). Verbatim:

> **Restrict source-event fan-out to Agents using the affected model**
>
> When a source appears only in the chain of an unassigned `menu.checked` entry, an unused mapping, or an unused backend default, this rule marks the backend affected; the preceding two-hop rule then notifies every scope routed to every enabled Agent on that backend. That recreates the irrelevant action-required pushes this round is intended to eliminate, because the protected-model union deliberately includes models with `SupplyGap.agents: []`. Resolve affected named Agents from their effective models and fan out only through those Agents, leaving menu-only failures in the feed/settings surface.

**Spec action at round 10, settled 07-29 10:54.** RECORDED, not fixed — and it was the round-9 narrowing one level deeper, on the hop round 9 did not touch. Round 9 narrowed hop 1 (which backends are affected) from `sources.order` membership to the chain of a protected model; this narrows hop 2 (which Agents inside an affected backend) to the Agents whose effective model is the one that lost supply. The second hop was too wide because the protected set is *deliberately* wider than the live selections — it protects a model the user ticked and assigned to nobody, which is right for refusing a delete and wrong for announcing an interruption, which is why `SupplyGap.agents` is allowed to be empty. **With push cut, the finding's delivery half is void and its grain half survives intact.** There is no recipient set to narrow; what remains is the distinction between 「a model some Agent actually runs lost supply」 and 「a ticked-but-unassigned menu model lost supply」, which the feed, the row's status pill and the agent-facing 需处理 state must still tell apart — a menu-only failure must not render an Agent as interrupted. Ruling #4 stands unchanged: `SupplyGap.agents` includes the Agents inheriting `agents.<backend>.default_model`, because they do use the model; it is guard and confirm payload — the *rule* for resolving 「谁受影响」, which the UI applies to answer the same question from `agent-supply`, not a payload the UI receives on a source failure. The standing open decision this block used to defer to (zero-scope fallback) no longer exists.

**Acceptance** (delivery half deleted 07-29 10:54; fixture repaired per AC-15). Two cases from independent fixtures, not two phases on one (corrected 07-29, review round 11: a failed source stays `needs_action` until the user acts and its health is source-global, so round 10's 「fail X again」 produces no second transition to observe). The backend is **OpenCode with bare selections (v4; the fixture ids below were prefixed before 2026-09-04)**, because a fixed-menu backend cannot own an OpenCode menu and, under v4, only a bare id such as `gpt-5.6` is a legal OpenCode selection (`api.md`); the prefixed form is what is rejected. **The assertions are on the live projection, not on the record** (07-29, review round 3): `SupplyGap` is contracted as a **mutation-refusal** payload (`api.md`, the DELETE/PUT guard responses), and `resolution-event.schema.json` carries no gap field, so 「the recorded gap names Agent Y」 asserts a shape no source-failure record has — the same record-vs-derivation confusion AC-6 was downgraded to remove. What a source failure produces is one unattributed record; who it is *about* is read from `agent-supply`'s per-Agent `supply_status` and the 「模型」 page's attribution. Case A: one enabled Agent running `gpt-5.6`, plus a menu model `glm-5.2` the user ticked and assigned to no Agent, supplied only by source X. X fails: that Agent's `supply_status` stays `ok`, the 「模型」 page attributes the failure to the **menu model and no Agent**, and the failure still appears in the 最近切换 feed. Case B: the same fixture with that Agent pointed at `glm-5.2` — from fresh state, or after X is explicitly repaired and recovered — and X fails: that Agent's `supply_status` becomes `interrupted` and the page names exactly that Agent. `SupplyGap.agents` is asserted where it is actually returned — **AC-5's DELETE refusal**, which is the contracted home of the empty-list case and already carries that assertion. An implementation that resolves affected Agents over every enabled Agent on the affected backend passes B and fails A.

**Remedy surface, added 07-29 review round 5 — L1's coordinated v3.** Round 3 retargeted
these assertions onto a per-Agent projection that **no contracted read payload serves**, so
the acceptance above cannot be executed as written: `GET /api/models/agents` returns one
`AgentSupply` per *backend*, whose `selected_by_agent` and `supply_status` are singular,
and `SupplyGap.agents` — the only per-Agent shape in the contract — is returned solely by
mutation refusals. The retarget was still correct in direction (the record layer genuinely
has no gap field, per AC-6's downgrade); it just landed on a projection that has to exist
first. **Shape intent for L1: `api.md` gains a minimal per-Agent read projection on the
existing `GET /api/models/agents` response — a list of the **enabled** named Agents on
that backend, each with the model it effectively runs and its own `supply_status`** —
scoped to enabled Agents (added 07-29, review round 9) because a disabled Agent cannot
take a turn, so it has no live capability to report, and `api.md`'s protected set
already excludes disabled Agents on exactly that ground; a disabled Agent whose model is
ALSO the backend default stays protected there through the default term, and is still
absent from this projection. Computed from the
same effective-model rule ruling #4 already fixed. **It cannot reuse the `SupplyGap.agents`
entry shape, and round 5 was wrong to say so (corrected 07-29, review round 7):**
`SupplyGap` is `{backend, model_id, agents: string[]}` (`api.md:301`), so an entry there is
a bare Agent name and carries neither an effective model nor a `supply_status`. The
projection needs its own object with all three fields; **L1 authors and versions that
object in v3** — L0 records the requirement and the three fields it must carry, not the
schema. Reusing the name `agents` for a differently-shaped list would be worse than
inventing one, so L1 should also pick a name that cannot be mistaken for `SupplyGap.agents` (explicit selection, or
inheritance of `agents.<backend>.default_model`). That is the projection Case A and Case B
above read, and the one the 「模型」 page needs to attribute a failure to an Agent rather
than to a backend. L0 records the requirement and does not edit `api.md`; until v3 lands,
read this AC's two cases as specifying the intended behavior.

### AC-10 — Constrain `ProbeResult.source_id` to the canonical `src_*` format

Review round 10, P2, on `docs/plans/model-hub-contracts/probe-result.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099013). Verbatim:

> **Constrain probe source IDs to the canonical Source format**
>
> For every Hub-mode probe, `source_id` is supposed to identify the attempted `Source`, whose contract uses `^src_[a-z0-9]{8,}$`; the chain and provenance schemas enforce the same format. Here any nonempty string validates, so a serializer can emit values such as `"direct"` or an adapter label and still pass the promised API-payload contract test, while the UI cannot correlate the probe with a source row. Apply the canonical pattern here and add the corresponding referential-existence guard.

**Spec action at round 10.** RECORDED, not fixed. Verified: `source.schema.json`, `agent-chain`, `agent-supply` (twice), `priority` and `turn-provenance` (three times) all pin `^src_[a-z0-9]{8,}$`, while `probe-result.source_id` carries `minLength: 1` alone. The two halves land in different places and only the first is a schema change. The `pattern` belongs in this file. The **referential-existence** half — that the id names a row that exists — sits outside draft-07 on the same boundary this PR already declared for `model_supply` uniqueness, so it belongs in `model-hub.md` §4.4's server-validated invariant list and as a row in `api.md`'s mechanical-guard table, not as a schema keyword nobody can write. Note the edge this removes: `"direct"` is exactly the string a well-meaning serializer would invent for AC-7's Direct case, so an implementation that closes AC-7 by faking a probe payload is caught by a contract test instead of by a bug report.

**Acceptance.** `{"source_id": "direct", …}` and `{"source_id": "cli-anthropic", …}` are both rejected by `probe-result.schema.json`; every example in that file validates against it after the v3 bump, with `contract_version` the only field the bump changes — the existing `source_id` values (`src_chatgptplus`, `src_relay9c1x`) already satisfy the new `pattern`, so it costs no example a rewrite (corrected 07-29, review round 11: round 10 asked the frozen examples to 「still validate byte-identically」, which this AC's own `const: 2` → `const: 3` bump makes unsatisfiable); and a probe response whose `source_id` is well-formed but names no `Source` row is rejected by the server-side guard, with the mechanical checker asserting that guard is declared. A serializer test that only asserts 「a nonempty string」 fails all three.

### AC-11 — Route `system`-emitted source events from their source

Review round 10, P1, on `docs/plans/model-hub-contracts/resolution-event.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099015). Verbatim:

> **Route system-originated source events by their source**
>
> When an action-required source event is produced by a Web UI/settings mutation, its historical emitter can legitimately be `agent: "system"`, but this description says every such event expands to no Agent and therefore no recipient. That contradicts the normative delivery rule in `model-hub.md`, which says source-scoped events derive affected backends from `from_source` and that Web mutations and mid-turn failures resolve recipients identically, so following this schema text drops the required push. Reserve the zero-recipient interpretation for events with no affected source, route source-scoped `system` events from their source, and disallow `system` on backend-scoped `supply_interrupted`.

**Spec action at round 10, reduced 07-29 10:54.** RECORDED, not fixed, and this was the one round-10 finding where the two texts were flatly opposed. The schema says `system` 「expands to no Agent and therefore to no recipient」; §4.5 said 「a Web UI mutation and a mid-turn failure resolve their recipients identically」 and derives a source-scoped kind's affected backends from the failed source. §4.5 is the normative side: `agent` was never the addressing key (round 7 made it a backend identifier consumers must expand), and the zero-recipient sentence is right only for the case it was written for — an event with no affected source, such as a manual recovery that changed nothing about supply. **With push cut, 「recipient」 drops out of both texts and what is left is the event shape**: the schema's `system` sentence still tells a consumer that such an event affects nothing, which is false for a settings-page revocation and would leave that failure out of the feed and out of the 「模型」 page's 需处理 state. The two remedies the finding asks for are unchanged and are both shape rules: reserve the zero-expansion reading for events with no affected source, and disallow `system` on backend-scoped `supply_interrupted`. Note the grain this now works at, per AC-6's downgrade: 「affects something」 means the record carries a `from_source` the consumers can derive impact from — not that the record itself names backends, which it never does.

**Acceptance** (delivery half deleted 07-29 10:54; route consolidated 2026-08-09).
Drive the non-turn path through `POST …/sources/<id>/refresh`: it re-discovers a Source
that is the last supplier of a protected model and finds it dead, producing a
`needs_action` event with `agent: "system"` and `from_source` set to that Source. The
record is equal in kind, `reason`, `from_source`, and `severity` to the identical
mid-turn failure, so feed and Models-page consumers derive the same state regardless of
who noticed. A `supply_interrupted` event carrying `agent: "system"` is rejected. AC-3
and this criterion name the same saved refresh/recovery route; no `/test` alias exists.

**Remedy surface, added 07-29 review round 5 — L1's coordinated v3.** The fixture above is
unbuildable against the frozen shape, and this is the defect rather than the fixture: `POST
…/sources/<id>/refresh` takes **no model input** — it re-probes a source — while
`resolution-event.schema.json` lists `model_id` in `required` as a bare `{"type":
"string"}`. A source that supplies several models therefore leaves the implementation three
bad options: fabricate one model, emit one event per model (which breaks AC-6's 「recorded
ONCE, unattributed」 rule), or invent a sentinel string the consumers must special-case.
**Shape intent for L1: `model_id` becomes nullable (`["string", "null"]`) for
source-scoped events, with `null` meaning 「this event is about the source, not about one
model」** — the same distinction `from_source`/`to_source` already encode as nullable, so it
adds no new concept. The conditional that keeps model-scoped kinds non-null, and the
consumer rendering for a null model, are L1's to write with the rest of v3; L0 records the
requirement and does not edit the file. Until that lands, read this AC's fixture as
specifying the intended behavior, not a payload that validates today.

### AC-12 — Reconcile a failed old-credential revocation

Review round 10, P2, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099017). Verbatim:

> **Reconcile failed old-credential revocations**
>
> When the post-commit revocation of the old credential fails, invariant 2 explicitly leaves that handle live as an orphan, but the next invariant promises that success revokes the old handle and that no path leaves two live handles for one source. An implementation cannot satisfy both statements, and treating the failure as an unspecified cleanup problem can retain upstream credentials indefinitely. Define a durable pending-revocation record and retry/reconcile it, or weaken the latter invariant to acknowledge that recorded failure state.

**Spec action at round 10.** RECORDED, not fixed. Both sentences sit in the same invariant list: invariant 2 says a failed revoke 「leaves an orphaned handle in the engine store, which is a cleanup problem, not a supply problem」, and invariant 3 says 「no path leaves two live handles for one source」. The orphan IS a second live handle, so invariant 3's 「no path」 is false as written. Which side gives way is the lane's call, but it is narrower than it looks: invariant 3 is load-bearing for the property that a rotated-away credential stops working upstream, so weakening it to 「except a recorded pending revocation」 preserves that property only if the record is durable and actually retried. Silently tolerating the orphan is the one option this criterion forbids — a credential the user believes they rotated away would keep working indefinitely, which is a security outcome, not a cleanup backlog.

**Acceptance.** A credential replacement whose post-commit revoke fails leaves a durable, observable pending-revocation record for the old handle; the service is then **reconstructed against the same persisted state** — a fresh instance, or the process restarted — and only that new instance runs the reconcile pass, which revokes the handle (corrected 07-29, review round 11: asserting the record and then reconciling on the same instance passes with an in-memory queue, which is the one implementation 「durable」 exists to forbid). The source serves normally throughout, because this failure is not a supply problem. No end state exists in which the old handle is live and nothing records that it is. A test that injects a revoke failure and asserts only 「the repair succeeded」 does not satisfy this: it must read the record back after reconstruction, and assert the handle is gone after that instance's reconcile.

### AC-13 — Give the `force` override a request contract on both repair routes

Review round 10, P1, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099019). Verbatim:

> **Define how clients submit the force override**
>
> When an elective replacement of a healthy credential discovers a narrower model set, this branch requires the client to retry with `force=true`, but the declared credential request is only `{key}`, the re-auth route has no request body, and the status poll is read-only. The UI therefore has no contracted way to confirm `source_last_supplier`—especially after an OAuth flow has already completed—so implementations must either strand the repair or invent incompatible wire semantics. Add the override to the request contract and define a resumable confirmation path for re-auth completion.

**Spec action at round 10.** RECORDED, not fixed. Verified against the route table: `PUT …/sources/<id>/credential` declares `{key}`, `POST …/sources/<id>/reauth` declares no request body, and `GET …/oauth/status/<flow_id>` is a poll. The elective branch nevertheless refuses 「unless `force=true`」 using DELETE's refusal shape — a shape DELETE can carry because DELETE takes a query parameter and these two routes have nowhere to put one. The key half is a one-field addition. **The re-auth half is the hard one, and it is not a naming problem:** by the time the refusal is computed the user's login has already been replaced, so 「retry with `force=true`」 cannot mean 「send the request again」 — re-running OAuth on `native_cli` is the irreversible act AC-2 is about. It needs a confirmation that resumes the completed flow, which is why this criterion and AC-2 belong to one lane: AC-2's 「pre-login confirmation that can still abort」 and this criterion's 「resumable confirmation after the fact」 were two answers to one question. **The owner picked the first (2026-07-29 10:44):** re-auth confirms before the irreversible login, so this criterion's `force` confirmation reuses that shape rather than adding a resumable post-hoc one. The key half — a declared override field on `PUT …/credential` — is unaffected and still owed.

**The two halves take different confirmations** (orchestrator ruling, 07-29 — review round 1 of the L0 PR): the re-auth confirmation is **unconditional**, and the computed `would_interrupt` guard belongs **only to the `api_key`/Hub path**. The reason is that the guard is not computable on the native path: which models a narrower account can reach is knowable only *after* the login, and the login is the irreversible act. Requiring a computed guard before it would make the confirmation depend on a fact that does not exist yet — so what the owner ruled was a flat irreversibility warning, shown every time, not a supply prediction. The Hub path has the opposite shape: discovery precedes commit, so there the guard is real and the refusal is computed from it.

**Acceptance** (re-auth half settled 07-29 10:44 by AC-2's ruling; guard scope fixed 07-29 by the ruling above). An elective `PUT …/credential` onto a narrower key is refused with `source_last_supplier` and the exact guarded response from §4.5's authoritative Source-mutation envelope matrix; the identical request carrying that row's override commits with that row's success envelope. For re-auth, the assertion is the **unconditional** one: every `native_cli` re-auth presents AC-2's irreversibility confirmation before the login starts and can be aborted there, **regardless of what the new account will turn out to supply**, and no path reaches a completed OAuth flow that then needs a second round trip to confirm. A UI that must re-run the OAuth flow to confirm fails this test; so does one that lets the login complete and only then discovers it cannot commit; and so does one that tries to gate the confirmation on a pre-login supply computation, which cannot be performed.

### AC-14 — Split AC-8's guard mutations across fresh fixtures

Review round 12, P2, on `docs/plans/model-hub-implementation.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273196). Verbatim:

> **Split the guard mutations across fresh fixtures**
>
> This acceptance sequence cannot exercise both routes twice on the same fixture: in the disabled-mapping case, the successful DELETE removes the source before the credential request or enabled-mapping phase can use it, while running the narrowing credential PUT first changes the source capabilities that the DELETE phase is supposed to inspect. Use independent fixtures for each combination of route and `enabled` value so all four assertions test the intended initial state.

**Disposition.** Repairs **AC-8's acceptance block**, applied above. Four independent fixtures, one per (route × `enabled`) combination, replacing the single fixture whose successful DELETE consumes the source the credential phase needs. Same class as round 11's AC-9 repair. AC-8's criterion is unchanged, and so is the guard semantics it tests — this is a defect in how the test was specified, not in what it asserts.

**Acceptance.** Contract/integration layer, owed by **L2** with the AC-8 tests it repairs. The four assertions run from four independent fixtures and each observes the state AC-8 describes; a suite that chains them onto one fixture cannot even reach the second assertion, because the source is gone. Fails today: AC-8's block, before this repair, specified exactly that unrunnable sequence.

### AC-15 — Use a legal backend/model fixture for AC-9

Review round 12, P2, on `docs/plans/model-hub-implementation.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273198). Verbatim:

> **Use a valid backend/model fixture for AC-9**
> *[Superseded for OpenCode on 2026-09-04: menu ids are bare canonical model ids (§4.8 v4); the prefixed form no longer exists.]*
> The fixture combines a bare `gpt-5.6` selection with an “OpenCode-menu” entry on the same backend, but `api.md`'s identifier rules require OpenCode selections to use prefixed `vendor/model` IDs, while a fixed-menu Codex backend cannot also own an OpenCode menu. Consequently Case B cannot point this Agent at the described ticked model without changing backend/menu semantics; define the fixture as OpenCode with a prefixed selection such as `openai/gpt-5.6`, or use a fixed-menu-only scenario.

**Disposition.** Repairs **AC-9's acceptance fixture**, applied above: one OpenCode backend with bare selections (`gpt-5.6` running, `glm-5.2` ticked-but-unassigned — v4 ids; they were `openai/gpt-5.6` and `zhipuai/glm-5.2` before 2026-09-04). The previous fixture asked a fixed-menu backend to own an OpenCode menu, which `api.md`'s identifier rules forbid, so Case B could not be reached at all. AC-9's criterion and the round-9/round-10 narrowing rulings are unchanged. Independent of the 10:54 push cut — it repairs the fixture, which the surviving grain half still needs.

**Acceptance.** Contract/integration layer, owed by **L3** with the AC-9 tests it repairs. Every identifier in AC-9's fixture validates against `api.md`'s identifier rules for the backend that owns it, and Case B is constructible without changing backend or menu semantics mid-test. Under v4 the bare id is the only admissible OpenCode form — a prefixed id such as `openai/gpt-5.6` is rejected — so the fixture is built exactly as written.

### AC-16 — Remove the nonexistent Agent from AC-5's assertion

Review round 12, P2, on `docs/plans/model-hub-implementation.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273200). Verbatim:

> **Remove the nonexistent Agent from AC-5's assertion**
>
> This setup declares only a mapping and a source, yet requires the confirmation to name an Agent that would break. Mapping rows are intentionally protected even when assigned to no Agent, and the same section later relies on such menu-only gaps having `SupplyGap.agents: []`; assigning an Agent here would instead protect the model through the Agent-selection term and stop the test from isolating the mapping-namespace defect. Assert the affected pair with an empty Agent list here, and test Agent-facing copy in a separate fixture.

**Disposition.** Repairs **AC-5's acceptance block**, applied above, and closes a contradiction between two of this section's own criteria: AC-5's fixture declares no Agent, and AC-9's recorded text depends on exactly that case (`SupplyGap.agents: []`). AC-5 now asserts the refusal plus the empty Agent list; Agent-facing confirmation copy moves to its own fixture. The round-8 spec fix AC-5 guards is unchanged.

**Acceptance.** Contract/integration layer, owed by **L2** with the AC-5 tests it repairs. The mapping-namespace fixture asserts `SupplyGap.agents: []` and never names an Agent, and a separate fixture with a real Agent selection covers the Agent-facing copy. Fails today: AC-5's block, before this repair, asserted a name its own setup never declares — the assertion could only pass by adding an Agent that would mask the defect under test.

### AC-17 — Make notification counts independent of the open recipient policy

Review round 12, P2, on `docs/plans/model-hub-implementation.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273202). Verbatim:

> **Make notification counts independent of the open policy**
>
> This requires every routed scope to receive a push even though `model-hub.md` lines 571-578 explicitly leave unresolved whether recipients are all routed scopes or only recently active ones. Under the latter valid owner decision, an inactive routed scope makes this acceptance test fail despite correct delivery logic; mark every asserted scope recently active, or defer the exact count until that policy is settled. The same qualification is needed for the later AC-9 and AC-11 push assertions.

**Disposition. SUPERSEDED by the 07-29 10:54 push cut — recorded, not applied.** The finding is genuine and was correctly diagnosed: three push-count assertions (AC-6, AC-9 Case B, AC-11) silently assumed one branch of the then-unsettled recipient policy. Its remedy — qualify each fixture as 「recently active」 so the count holds under either branch — is now unbuildable and unnecessary, because the dependency closed by **removal** rather than by resolution: with no proactive delivery, `model-hub.md` §4.5 no longer contains a recipient policy, open or settled, and the three assertions it qualified have had their delivery halves deleted (AC-6 → affected-backend record, AC-9 → resolved-Agent set, AC-11 → record equality). None of the three now asserts a push count, so none can depend on how recipients would have been chosen. This block is retained rather than deleted so the finding is not rediscovered as a live gap; it is the second unsettled-decision leak found in this section (round 11 fixed the owner-call cells), and the class is what L6 should keep watching for.

**Acceptance.** Documentation layer, discharged by **L0** (this PR). Mechanically checkable in two parts. First, no **Acceptance** paragraph in this section asserts **proactive conversation delivery** — that is, a message arriving in a scope or conversation that the user did not act to produce: AC-6 asserts that exactly one unattributed failure record is written and that consumers derive impact from it, AC-9 the resolved Agent set and `supply_status`, AC-11 record equality between two emission paths — three assertions about stored state and its rendering, none about delivery. The check is deliberately narrowed to *proactive* delivery (07-29, review round 5): a blanket 「no Acceptance paragraph states what a user receives」 is false on its face and always was, because AC-2 requires a confirmation to be **presented**, AC-7 constrains drawer affordances, AC-9 specifies page rendering and AC-18 feed rendering. Those are pull surfaces — the user opened the page, or ran the turn — and they are exactly what the push cut left as the only way supply problems reach anyone, so a check that forbade them would forbid the design. What the cut removed is the unrequested interruption, and that is the only thing this check may look for. Second, no **Acceptance** paragraph defers to a policy `model-hub.md` leaves open, which is trivially satisfied now that §4.5 states no recipient policy at all. Where 「push」 or 「recipient」 still appears in those blocks it is in a verbatim finding or a dated 「what this used to say」 clause, never in the sentence a test is written from. Failed at the head this finding was filed against, where all three counted pushes. **The runtime half of the cut is not here**: this check only proves §8 stopped *asserting* delivery, and §3's 「Silence needs a negative assertion」 rule assigns L6 the scenario that proves the implementation stopped *doing* it — L6 checkpoints both.

### AC-18 — Constrain resolution-event source references

Review round 12, P2, on `docs/plans/model-hub-contracts/resolution-event.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273205). Verbatim:

> **Constrain resolution-event source references**
>
> `from_source` and `to_source` identify `Source` rows and the action-required path explicitly hands `from_source` to credential/reauth routes, but these fields accept any string rather than the canonical `^src_[a-z0-9]{8,}$` format enforced by `Source`, chains, and provenance. A serializer can therefore emit an event with `from_source: "direct"` that passes the contract but cannot be correlated with a source or opened by the remediation UI. Apply the canonical pattern to both string branches and add the same referential-existence guard required for probe source IDs.

**Disposition.** New criterion, and AC-10's defect in a second file — the same canonical id left unconstrained, found by the same reasoning. `^src_[a-z0-9]{8,}$` goes on the string branch of both `from_source` and `to_source`, with `null` retained on both (legitimately null for `supply_interrupted`); referential existence goes to the API boundary as in AC-10, because it sits outside draft-07 on the boundary this PR already declared. The existing examples (`src_claudepro1`, `src_anthkey01`) already satisfy the pattern, so no example is rewritten. **The push cut does not touch this** — the event is still recorded, and the feed's one-tap re-auth is exactly the consumer that needs `from_source` to name a real row. Frozen surface: joins the `contract_version: 3` set.

**Acceptance.** Contract layer, owed by **L1's v3** (the pattern constraint on both fields), with **L3** owning the API-boundary guard and the emission — corrected 07-29, review round 6, to match the AC table and §3's single-freeze rule; the earlier 「owed by L3」 wording predated that rule and would have had L3 editing `resolution-event.schema.json` after the freeze. `{"from_source": "direct", …}` and `{"to_source": "cli-anthropic", …}` are both rejected by `resolution-event.schema.json`; `null` on either field still validates for `supply_interrupted`; every example in the file validates after the v3 bump; and **every non-null endpoint must name an existing `Source` row at emission time** — the server-side guard rejects a `switch` whose `from_source` is well-formed but unknown *and* one whose `to_source` is (07-29, review round 2: checking only the origin lets a `switch` pass with a destination the feed cannot resolve or open, which is the same defect one field over) — with the mechanical checker asserting both are declared in `api.md`'s guard table. **The guard is on emission, not on retention** (07-29, review round 3): sources are deletable, `force=true` deletes one the guard would otherwise refuse, and the events feed is bounded by count rather than by source lifetime, so a retained row can legitimately reference a source that no longer exists — re-validating retained rows would make a legal DELETE retroactively invalidate the record of what happened before it, or push the implementation into rewriting history to keep the feed valid. Neither the record nor the schema changes when a source is deleted; what changes is the **rendering**. **Where the remembered name comes from was left unstated until 07-29, review round 9** — the event carries ids, not a display-name snapshot, so 「the remembered display name」 named nothing the feed could reach. It is the **recorded human strings**: `human_zh` / `human_en` are **required on every event** (`resolution-event.schema.json:7`) and are composed at *record* time with the source display name already embedded — the file's own example, `「Claude Code:Claude Pro 本周期额度用完 → 已切到 Anthropic API Key(按量)」` (:168), names both endpoints inside the sentence. **The recorded strings ARE the snapshot; no schema field is added** (orchestrator ruling, 07-29 15:07, verified against the schema before transcription). So: **the feed renders the recorded human string verbatim**, and the 「（已删除）」 marker is **derived at render time** from the id failing to resolve against live sources, with the one-tap re-auth affordance withdrawn because there is nothing left to re-auth. The corollary binds the emission side, **scoped to the kinds that have a source subject** (corrected 07-29, review round 10): every kind whose endpoints name a source — `switch`, `channel_switch`, `recover`, `cooldown`, `needs_action`, `skip` — must name that source in its string template, and a template that does not is a defect in the template, fixed in this spec, never by adding a field. It cannot bind the two kinds that have no source subject, and read over them the round-9 wording asked the emitter to fabricate one: `supply_interrupted` pins **both** endpoints to `null` by conditional — the chain emptied, nothing switched — and legacy v4 `mapping_applied` was decided by the pre-candidate mapping step, so its line names models rather than sources (「claude-opus-4-6 → glm-5.2(经映射)」, the schema's own note). Excluding them costs the snapshot nothing: neither references a source, so neither can outlive one's deletion. Acceptance covers both halves — emission of an unknown id is rejected, and a feed rendered after its referenced source is deleted still loads, still shows the line, and offers no dead action. Fails today: both fields accept any string.

### AC-19 — Close the eligibility reason-key vocabulary

Review round 12, P2, on `docs/plans/model-hub-contracts/agent-supply.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273208). Verbatim:

> **Close the eligibility reason-key vocabulary**
>
> When `eligible` is false, this branch accepts any nonempty `reason_key`, although the contract names three distinct remedies and the UI must resolve the value through its locale files. A typo or invented value therefore validates but leaves the ineligible row without translatable copy or an actionable remedy. Constrain this branch to the declared `subscription_wrong_client`, `opencode_api_key_only`, and `consent_required` keys, extending the enum alongside locale support when a new eligibility cause is introduced.

**Final disposition (owner 2026-08-09).** The closed-vocabulary invariant survives,
but the historical three-member set quoted above does not. The final enum is exactly
`["models.eligibility.subscription_wrong_client"]`; `consent_required` retired with
per-Source consent, and `opencode_api_key_only` retired when Hub-held subscriptions
became valid OpenCode inputs. This paragraph explicitly supersedes the quoted enum and
every earlier acceptance sentence that required either retired member.

**Acceptance.** I1 rejects a typo, an unqualified key, either retired key, and every
unlisted value; the one fully qualified final key validates. I4 carries the matching
entry in both UI locale files. A generated guard compares schema enum and both locale
sets in both directions. A future cause ships its enum member and locale copy together.

### AC-20 — Enforce the hub-mode half of the mode invariant

Review round 12, P2, on `docs/plans/model-hub-contracts/agent-supply.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273210). Verbatim:

> **Enforce the hub-mode half of the mode invariant**
>
> The schema pins all Hub-only projections to null in Direct mode but never enforces the converse, so a response with `mode: "hub"` and present-but-null `selected_model_id`, `sources`, `supply_status`, and `model_supply` still validates even after the API-boundary presence checks land. Such a payload leaves the Hub drawer without an order or selected model and makes the chain/probe defaults unusable while claiming Hub mode. Add a Hub branch that constrains these fields to their non-null shapes; it can remain non-required here so the frozen v1 examples that omit the fields still validate.

**Final disposition (S-1, 2026-08-09).** The hub-mode completeness invariant survives,
but the final five projections are `selected_model_id`, `sources`, `routes`,
`supply_status`, and `model_supply`. `mappings` is not a final field. `selected_by_agent` and
`current` remain legitimately nullable, and `menu`, `builtin_models`, and
`standard_vendors` remain mode-independent. *[Retired 2026-09-04: the `standard_vendors` projection is removed by catalogs spec v3 C9; see the v4 override at the top of this document.]*

**Acceptance.** I1 builds every fixture from an otherwise-valid payload. A Hub payload
with any of the five final projections explicitly null is rejected by the Hub branch;
the same payload with one explicit route row per menu model, including empty `hops`,
validates. `sources` contains an explicit `order` and no policy. `selected_by_agent: null`
and `current: null` still validate. Direct mode has
its documented representation. The mechanical checker records the branch as a declared
non-required exception for historical examples while rejecting `mappings` or a policy
discriminator in the final shape.

### AC-21 — Make the mirror registry encode its promised checks

Review round 12, P2, on `docs/plans/model-hub-contracts/README.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273212). Verbatim:

> **Make the mirror registry encode its promised checks**
>
> The table cannot drive two checks the surrounding text promises: M4 labels the non-self-healing reason-to-`detail_key` relation as `none` even though `api.md` requires a mechanically checked bijection, and M6 omits `resolution-event.agent` from its Mirrors cell even though lines 226-230 say that exact superset is checked as the home set plus `system`. A harness generated from these rows can therefore skip both relations while reporting the registry complete. Give M4 an executable bijection rule and list `resolution-event.agent` in M6 with its declared extra.

**Disposition.** New criterion, and the sharpest of the four contract findings: the registry exists so a harness can be *generated* from it, so a row that under-declares its relation is not a documentation nit — it silently removes a check while the registry reports itself complete. M4 gets an executable `bijection` rule naming both directions — **and the pairs themselves, in a machine-readable field** (07-29, review round 3). 「Bijection」 alone constrains only the two *sets*: any permutation of the reasons against the `detail_key`s satisfies it, so a mapping that renders 「订阅客户端不符」 for a consent failure passes a generated harness while being wrong in exactly the way the mirror exists to catch. The registry therefore carries the ordered pairs (a `pairs` list of `[reason, detail_key]`, one line per member), and 「bijection」 becomes what the harness *asserts about that list* — total, injective, and pairwise-matching the two files — rather than the whole of what the row says. M6's Mirrors cell gains `resolution-event.agent` with its declared extra `system` inline, so the row alone determines the check. The invariants themselves already hold in the schemas — only the registry under-declares them. Frozen surface: joins the v3 set.

**Acceptance.** Contract layer, owed by **L1**. A harness generated purely from the registry rows — with no hand-written supplements — runs both relations: the reason ↔ `detail_key` bijection in both directions, and `resolution-event.agent` ⊆ home set ∪ `{system}`. Three mutations are caught by that generated harness: deleting one `detail_key` from either side; **swapping two `detail_key`s between reasons in one file while leaving M4's `pairs` list alone** (07-29, review round 3 — the case a set-only bijection cannot see, and the one that ships wrong copy to a real user); and adding an undeclared value to `resolution-event.agent`, whose enum already holds four, `claude`/`codex`/`opencode`/`system`, so the test adds a fifth. Fails today: M4 reads `none` and M6 omits the field, so a faithful generator emits neither check and still reports the registry fully covered.

### AC-22 — Make one ordered per-model route chain the only Gateway routing model

**Owner rulings 2026-08-07 and 2026-08-09 S-1.** Per-model ordering remains in scope,
but `follow | custom` does not. Every `(backend, menu model)` stores one ordered `hops`
array of exact `(source_id, model_id)` pairs. Add Source matches once and writes those
pairs; the Gateway UI edits the same configuration; runtime walks it verbatim.

**Acceptance.** Config, schema, API, and UI contain one persisted backend Source order
and no placement-policy or route-policy discriminator, missing-row default, separate
mapping object, or runtime Source/model matching branch. Every menu model owns an explicit
`hops` array, including an empty array for an unconfigured route. For every accepted
Add/import match, fixtures assert that §4.2's sole placement policy chooses one
deterministic position, the transaction persists and returns that position, the Gateway
shows it, and the user can adjust it.
They do not assert the current policy's concrete position. Existing Source-order entries
and hops preserve their relative order, no “not enabled” state is emitted, and
refresh/catalog/runtime paths never rerun placement. Explicit edits preserve submitted
order and model ids and cannot change another menu model.

A table-driven runtime suite executes the normative §4.3 pseudocode against the stored
array. It fails any implementation that normalizes a provider, walks inventory to pick a
model, prepends Native, or otherwise constructs a second chain. Every hop is rechecked
only for live runnability immediately before its turn. Exact configured reasoning-effort
membership is honored. Credential fixtures execute every authoritative matrix row;
fallthrough follows stored order, and a static-key `401` makes no duplicate request.

Turn fixtures execute every §4.5 outcome row: a nonempty all-blocked chain produces
`no_candidate` with its actual blockers; an empty chain produces `route_unconfigured`;
`native_cli_unavailable` is `interrupted`; local engine loss before the request,
mid-request, and after streamed output is terminal `engine_down` with no Source mutation,
replay, or next-hop walk; streamed Source failures persist attributable state without
replay; retry copy appears only when the next turn's same stored chain has a different
current hop.

Source-card attribution consumes `adopted_by: [{backend, menu_model}]`. The backend
subtitle and Usage page consume the exact four-value `supply_status`; no independent
walk or parallel prose status exists. Takeover derives from the stored first hop and
live current position without a field.

Mutation fixtures cover non-forced exact-hop refusal, confirmed cascade with survivor
order intact, background stale-hop retention, and the forced Source-delete success
envelope. Deletion removes the Source id from every backend Source order and Route chain
in the same transaction and the result passes canonical serialization/reload validation. No path
substitutes another model or claims invalidated supply survived.

Fresh installs write `mode: "hub"`; existing installations with no Model Hub state
start in `direct`. Until I5's evidence is merged, absence of `VIBE_MODEL_HUB_ENABLED`
keeps the controller, API, and Models UI disabled; I1 proves the v3 aggregate only under
explicit enablement, and I6 later deletes the gate in one ownership transfer. A newly introduced menu model gets an explicit empty route and is
not retroactively matched. Historical AC-5/AC-8 mapping fixtures are non-executable;
I1 replaces their guard intent with configured-route fixtures. The final no-parallel-
mapping shape remains owner-vetoable; a veto blocks I1 rather than reviving two route
authorities.

### AC-23 — Make subscription custody vendor-specific with a native singleton

**Owner ruling 2026-08-07, amended that afternoon.** Claude subscriptions recommend
and default to `native_cli`; Claude Hub custody is optional. ChatGPT subscriptions
recommend and default to `hub`; native Codex login remains supported but receives no
default guidance. Vendor custody recommendation is an input only to §4.2's Add-time
placement policy; the stored output is authoritative. Any Hub-held subscription may
participate in any backend's configured chain; its stored position and model mapping are
authoritative.
The 2026-08-08 orchestrator ruling, owner-vetoable, permits at most one `native_cli`
Source per backend; extra accounts use Gateway custody. Native account selection is
deferred until an official CLI supports stable profiles.

**Acceptance.** An omitted OAuth channel resolves by vendor, Claude → `native_cli` and
ChatGPT → `hub`, while both explicit alternatives remain accepted. With a native row
present, another native creation is disabled in the UI and rejected at the OAuth-start
API boundary with `native_source_already_exists` plus `existing_source_id` **before the
adapter is called**; a spy proves the adapter received no request. §4.3 executor tests
are the sole acceptance authority for configured precedence, channel dispatch,
per-hop live revalidation, pre-stream fallthrough, post-stream
no-replay, and recovery.

Mode and hop labels are mechanically distinct in UI and locale fixtures: **Direct**
appears only for backend `mode: direct`, while a `native_cli` hop inside Gateway mode is
**Native**. Product copy contains no “not through Gateway” explanation, and no Native
hop is allowed to imply or mutate Direct mode.

Before ChatGPT Hub becomes default-on, I1's PR includes a dated re-verification of
`model-hub-tos-review.md` §2.1 and §3.1 under §11's timing gate. A material or inconclusive vendor change blocks
that default and is escalated to the owner; passing by omission, relying on K2 fidelity,
or deferring the check to I5 fails this criterion.

Every successful native-to-Hub handoff is silent (owner ruling 2026-08-08, superseding
the 2026-08-07 afternoon notice). No session message, setting, outbox row, or push path
is created. The takeover badge, connector state, recent-switch record, and usage remain
available on the Model Gateway and Usage pull surfaces. Negative fixtures also prove
that this silence does not suppress the existing truthful failure when no hop is
runnable. Streamed terminal rendering follows §4.5's authoritative matrix: retry copy is
allowed only after a persisted Source blocker makes a different hop current, while a
non-fallback request error truthfully says that switching Sources will not help.

The eligibility matrix has two independently tested axes: native subscription origins
remain restricted to their own backend client, while Hub-held Claude and ChatGPT
subscriptions are eligible for Claude, Codex, and OpenCode chains. Explicit Hub add
works with neither `subscription_hub_experimental` nor a consent timestamp. The reason
vocabulary has neither `consent_required` nor `opencode_api_key_only`: Hub-held
subscriptions are eligible for OpenCode, while a native subscription in any
non-sanctioned client uses the channel-specific `subscription_wrong_client` reason.
This supersedes AC-19's historical three-member enum, not AC-19's closed-vocabulary
invariant; the final mirror registry checks every remaining member against both locale files. Native
wrong-client use remains rejected; this is channel binding, not a cross-vendor ban on
Hub Sources.

### AC-24 — Warn only when Claude is added as a Hub-held Source

**Owner ruling 2026-08-07.** The sole subscription-routing warning belongs to the
explicit opt-in path that adds a Claude subscription to the Gateway. It states that
Anthropic explicitly prohibits this use, enforces server-side blocks, real account
bans have occurred, and the path may fail intermittently. It is informational, not a
consent gate or persisted acknowledgement.

**Acceptance.** The warning appears exactly once before the Claude Hub-held OAuth
flow begins. It does not appear for Claude native login, ChatGPT native login,
ChatGPT Hub-held login, adding a Hub-held subscription to a cross-vendor configured chain, native
quota takeover, or automatic return to native. No server flag, Source consent stamp,
or repeat prompt is required. UI copy and scenario evidence cover every row of that
matrix. AC-2 and AC-13's irreversibility confirmations remain intact: those guard a
credential-repair operation and are not subscription-routing warnings.

### AC-25 — Split subscription guidance by vendor and disable duplicate native creation

**Owner ruling 2026-08-07 afternoon, retained by the 2026-08-08 amendment.** The add
flow recommends Claude native custody
for compliance and ChatGPT Hub custody for product utility. A native path remains
available for ChatGPT, but the UI does not present it as the default.

**Acceptance.** Add Claude defaults to `native_cli`; Add ChatGPT defaults to `hub`.
Both explicit alternatives persist and resolve correctly. Add-flow custody defaults and
labels are tested for both vendors without asserting a concrete route position. When the backend already has
its singleton native Source, the native choice is visibly unavailable and cannot
submit a second row.

### AC-26 — Complete Source add and detail operations

**Owner rulings 2026-08-07 afternoon and 2026-08-08 design acceptance.** Source setup
must include manual connectivity testing, compatible model discovery, manual model
add/remove, and an editable per-model reasoning-efforts capability list with no default
item, selected state, or prefill. The existing probe, refresh, and discovery mechanisms
are reused. The list is editable on every inventory entry, including discovered entries
(owner-vetoable orchestrator scope ruling, 2026-08-08), because discovery commonly
returns ids without effort metadata; editing it never changes the entry's `origin`.

**Acceptance.** Add Source exposes one non-persisting submission that combines
connectivity classification with response-backed protocol observation. Source details
instead exposes exactly one mutating action, “Refresh models” / 「重新拉取」, backed by
`POST /api/models/sources/<id>/refresh`: it uses the stored adapter, refreshes inventory
and health, and clears `needs_action`/`error` only on current recovery evidence. Source
details has no “Test connectivity” button and no separate saved discovery route. Its
request, guarded refusal, and success mirror the refresh row of §4.5's authoritative
Source-mutation envelope matrix. An inventory shrink runs the same exact-hop and
protected-route supply-gap guards as Source/model deletion.
The UI and API never label the saved and unsaved operations as each other. The unsaved variant
returns classified reachability, authentication, and a protocol only when a real
upstream response proves it, without persisting a Source, changing route configuration, or
running an Agent turn. For an API-key test, success, authentication failure, adapter
error, timeout, and cancellation each revoke the transient provisioned
ref before the operation settles. A fault-injected revoke failure writes the existing
durable pending-revocation record, and a reconstructed service reconciles it; no response
contains the ref and no final state leaves live material unreferenced and untracked.
Unsaved Add Source model discovery has the same independently provisioned transient-ref
cleanup on success, failure, adapter error, timeout, and cancellation, plus the same
fault-injected reconciliation proof. Third-party Anthropic-compatible and
OpenAI-compatible Sources can fetch models in Add Source; after Save, refresh is the
single path to the same discovery result. The result distinguishes added, removed,
unchanged, and failed discovery while preserving manual entries. A successful full
replacement advances `last_discovered_at`; failure leaves it unchanged. Source detail
may render only “Model list updated at …” / 「型号列表更新于…」 from that value. No
latency or “last checked” field, copy, fixture, or route heuristic is introduced.

`source.models` no longer relies on bare strings. Every item carries exact `id`,
`origin: "discovered" | "manual"`, and required `reasoning_efforts: string[]`. The list
may be empty and declares supported values; it never declares a selected or default
value, and ids are unique within a Source. Discovery creates `origin: "discovered"`;
user-added entries create `origin: "manual"`. Rediscovering the same id preserves the
user-edited `reasoning_efforts` list plus existing `display_name` and `discovered_at`;
an upstream result cannot reset them to empty/default values.
`PATCH /api/models/sources/<source_id>/models/<model_id>` accepts
`{reasoning_efforts}` for an existing discovered or manual
entry, atomically validates the complete list through the selected adapter, returns
`{"source": Source}`, preserves `origin`, and leaves every route chain unchanged.
An invocation-level spy covers exact configured hops and asserts §4.3:
the turn-requested effort is passed through the unchanged single-value invocation
parameter only when it exactly matches that exact model entry's list; a missing
or non-member value passes `null`, with no approximate mapping or downgrade. This makes
a persistence/UI-only implementation fail.
Base URL replacement, credential replacement, explicit refresh/recovery, and
user-authored model deletion stage inventory and run AC-22's exact-hop **and** protected-route supply-gap
guards before committing. Their JSON bodies and canonical guarded/success responses
mirror their rows in FC-12; force-cascade fixtures cover each mutation. Native CLI
and Hub OAuth re-auth instead
use AC-2's pre-login acknowledgement and retain newly invalid hops as visible,
non-runnable entries with exactly `reason: "model_unsupported"` and `retry_at: null`
after the irreversible exchange.
UI evidence covers loading, reachable, authentication failure, discovery failure,
empty result, merge result, manual add, all-inventory reasoning-efforts list editing
with an empty/no-default state, guarded removal, and mobile treatment. The control form
follows `design.pen`; this spec does not prescribe it. Explanatory copy lives behind
compact info affordances rather than permanent instruction blocks.

### AC-27 — Observe protocol before Save and keep it immutable afterward

**Owner ruling 2026-08-09, clarified by the owner-routed UI handoff the same day.**
This supersedes the 2026-08-07 afternoon manual-choice ruling. The normal Add Source
form has no protocol selector. A stored value must be traceable to a real response from
that upstream; vendor, Base URL, and a user's manual hint may order probes but cannot
produce a saved conclusion.

**Acceptance.** The Add action reuses its connectivity interaction to observe protocol
before the Source commits. A proven result is stored atomically with the Source. An
ambiguous or failed result persists nothing and displays the only manual protocol entry
point in the product: a one-time three-value probe-order hint whose selected adapter must
itself receive a verifying upstream response before Save. Tests fail any vendor-table,
Base-URL, manual-hint, timeout, or catch-all path that writes a protocol without that
evidence.

After Save, connectivity retest, discovery, refresh, credential/Base-URL replacement,
and restart all preserve the stored value byte-for-byte and invoke only that adapter.
Changing protocol requires a new Source. The stored shape has no manual/automatic
provenance marker and no protocol-level unverified value. “Add anyway” is available only
after the protocol was proved and a different result, such as model inventory, remains
unavailable; that uncertainty is a health fact. UI evidence shows no normal selector,
an honest failed-add state, and the compact verified-hint fallback without inventing a
default. Invariant: every saved Source has one response-proven protocol; every path
without that proof produces no Source.

### AC-28 — Converge the protocol vocabulary on three transports

**Owner ruling 2026-08-09.** The final enum is exactly
`anthropic | openai_responses | openai_chat`. `openai_chat` is the Chat
Completions-compatible transport; no `openai_compatible` alias exists. Chat Completions
stays because the platform API remains supported and many third-party/open-source
upstreams expose no Responses API.

**Acceptance.** Schemas, examples, adapter/runtime branches, OpenCode overlay, API
payloads, UI types, locale keys, mirror registry, fixtures, and scenario data expose
exactly the three values. `vibe/model_hub_runtime/config.py` and `client.py` contain one
Chat path, and no save path or vendor metadata table synthesizes a protocol. Mechanical
tests reject a fourth enum member and catch any `openai_compatible` occurrence outside
the explicit absence assertion in this handoff.

### AC-29 — Validate every persisted Source through the canonical validator

**Owner-routed investigation finding 2026-08-09.** Every path that writes Model Hub
configuration produces a Source accepted by the same canonical validator used on the
next configuration load. Creation, OAuth, and native-config import may differ in how
they obtain data; they do not differ in what constitutes a valid persisted Source.

**Acceptance.** I1 exposes one canonical final-shape validation boundary and I3 routes
every native-import result through it before commit rather than constructing an
unchecked persisted dataclass. The migration scenario imports each supported native
item, validates the resulting Source through that boundary, serializes it, and reloads
the complete configuration successfully. A protocol or model shape rejected on reload
must be rejected before the import reports success.

### AC-30 — Derive takeover without storing a second state

**Owner design-reconciliation ruling 2026-08-09, retained by S-1.** Takeover is true
exactly when the configured chain's current hop is not its first stored hop and that
first hop is unavailable for a self-healing quota/cooldown reason. It is a projection
of visible configuration plus live runnability, not a stored sibling state.

**Acceptance.** Resolver/UI fixtures cover four transitions: a recoverably blocked
first hop with a later current hop renders takeover; recovery of the first hop clears
it; a configured chain whose first hop is healthy and intentionally not native does
not fabricate takeover; and a chain with no runnable hop resolves to the exact
`no_candidate | exhausted` result selected by §4.3, without any takeover badge,
connector color, or other takeover visual semantics. No
schema, API envelope, serializer, or UI type adds a stored takeover boolean or sibling
routing field. The Model Gateway and Usage pages derive the view from the same chain
projection, and mirror-registry tests require the takeover label plus all four
`supply_status` labels to exist in both locale sets. Successful fallback remains silent.

### AC-31 — Make Direct onboarding visible and reversible

**Owner design-reconciliation ruling 2026-08-09.** Direct is a supported backend-wide
mode and the first Models-page state for an existing installation with no Model Hub
state; Native names a `native_cli` hop inside Gateway mode and never names a mode.

**Acceptance.** For every existing-install backend in Direct, the Models page renders a
Direct-labelled backend group, its current self-managed configuration summary, no
Gateway chain, and one explicit Switch to Gateway action in that group. Gateway mode
offers Switch to Direct. A round-trip scenario switches one backend Direct → Gateway →
Direct while another backend remains unchanged, preserves saved Sources and route
configuration, and leaves native config byte-identical. UI and locale guards reserve
Direct for `mode: direct`, reserve Native for `native_cli` hops, and reject “not through
Gateway” mechanism copy.

### AC-32 — Keep route selection stable while live runnability changes

**Owner S-1 transfer, 2026-08-09.** Add-time matching and placement write visible
configuration. Runtime may inspect whether an exact stored hop can execute now, but it
may not choose different route membership or order from hidden live inputs.

**Acceptance.** Given byte-identical persisted Model Hub configuration and an identical
turn request, the ordered `(source_id, model_id)` Route selected for execution is
byte-identical even when wall-clock time, Source quota, Source health, or native-process
availability differs. Those live inputs may change only each stored hop's
`runnable/reason/retry_at` annotation and the current execution position; they may
neither add, remove, reorder, remap, nor substitute a hop. A behavior-level test runs
the same configuration/request against changed live inputs and compares membership and
order before inspecting the permitted annotations.

### AC-33 — Generate the authority closure from live inputs

**Circuit-breaker ruling, 2026-08-09.** `mirror-registry.json` is the sole registry for
closed decision domains. A table or enum absent from that registry is not authoritative;
prose may reference a registered authority but may not add a branch. A section-level
vocabulary change updates its referenced AC in the same head.

**Acceptance.** One test invocation reads the current Markdown tables, every registered
schema path under `docs/plans/model-hub-contracts/`, registered Python/type consumers,
and registered locale objects directly from the worktree. It computes authority and
consumer member sets in that invocation and fails in both directions: a consumer value
without an authority row is an orphan branch, and an authority row without a consumer
is an orphan row. The checker contains no copied decision member, member count, or
expected registry-id list. A future external snapshot is accepted only with a content
fingerprint recomputed from its producing artifacts in the same invocation; missing or
stale input fails rather than skips.

The same invocation discovers Python files outside the resolver package that import
`core.handlers.model_hub.resolver` and requires every discovered path to appear in
exactly one binding lane scope. Zero owners and multiple owners both fail. This makes
consumer ownership part of the generated closure instead of another hand-maintained
file list. Contract, code, and locale consumers are included through the registry's
live extraction relations; a hand-written consumer inventory is not accepted as evidence.

**Generated closure findings for this head (owner ruling 2026-08-09).** These five
findings were the seed set for AC-33's live closure run. They are recorded with their
original wording and resolved in the registered authority tables; no consumer may add
a parallel branch outside those tables.

| Finding / root cause | AC | Registered authority / landing point | Lane |
| --- | --- | --- | --- |
| **Define failures of the credential refresh operation.** When a refresh-capable credential receives its first 401 but the refresh operation itself times out, is rejected, or returns an invalid response, neither the existing refresh row nor the retry-after-401 row applies. | AC-22 + AC-33 | §4.3 Credential-failure decision matrix: `credential.refresh_failed`; `api.md` marker and resolver/adapter tests | I1 + I2 |
| **Assign the model-hub injection suite to a lane.** The suite imports final-shape config/resolver symbols and exercises the shared backend launch boundary, but no active lane owned it. | AC-22 + AC-33 | v3 current lanes, I1 scope; dynamic resolver-import ownership check | I1 |
| **Add permission denial to the terminal-copy matrix.** A request-scoped `permission_denied` failure was not named by the exhaustive request-failure row. | AC-22 + AC-33 | §4.5 Turn-outcome copy matrix: `turn.request_nonfallback`; no new discriminator | I1 + I2 |
| **Contract the guard on direct route edits.** Whole-array route replacement lacked the guarded refusal, force confirmation, and success envelope defined for other inventory mutations. | AC-22 + AC-33 | §4.5 Source-mutation envelope matrix: `mutation.route_replace`; `api.md` route PUT | I1 + I5 |
| **Remove the redundant mapping discriminator.** `requested_model_changed` duplicated the fact derivable from exact requested/configured model ids while the contract prohibited a separate mapping discriminator. | AC-22 + AC-33 | FC-08 and exact-hop/provenance shapes; derive from ids | I1 + I2 |

### AC-34 — Make Source-model ownership and routes one total resource contract

**Owner ruling, revised 2026-08-11.** Source inventory is a subresource of one Source.
A `discovered` entry is an upstream record whose user correction is persisted as
`retired: true`; a user may also edit its declared `reasoning_efforts`. User-authored
entries alone can be created, and their DELETE removes the row.

**Acceptance.** Model creation, capability-list replacement, and user-authored deletion
use only the three `/api/models/sources/<source_id>/models` family routes registered in
§4.5's total Source-mutation matrix. Source identity is carried only by the path, never
by those request bodies. POST and PATCH cannot change an existing `id`, `origin`, or
Route, so their matrix rows are explicitly unguarded; DELETE reuses the registered
guarded Source envelope. Rediscovery of the same id continues to preserve the edited
`reasoning_efforts`, `display_name`, `discovered_at`, and retirement tombstone as one
total merge rule. DELETE on a discovered row stages retirement and uses the same guard;
confirmed success retains the row and excludes it from supply. The generated same-run
closure also proves that no Source-model request body repeats `source_id`; this is a
derived check, not a copied occurrence count.

### AC-35 — Preserve the G-9 Source-order tombstone

**Acceptance.** For a fixture with at least two nonempty stored Routes, every successful
`PUT /api/models/agents/<backend>/sources` changes and re-echoes only
`sources.order`. Before/after Route documents are byte-identical, and the route exposes
no `force`, guarded `409`, `would_remove_hops`, or `would_interrupt` branch. The API
route table and §4.5 explicitly keep this write outside the exhaustive Source-mutation
matrix.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc,request}.py`, `vibe/{ui_server,model_hub_client}.py`,
and `tests/test_model_hub_api.py` for the byte-before/after Route fixture. There is no
I4 write consumer. After that fixture freezes, I5 consumes it in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-36 — Expose G-11 CLI installation presence per backend

**Acceptance.** Every AgentSupply API row includes one server-produced boolean
`cli_present`, including absent executables and Direct-mode backends. In a three-backend
fixture, the zero-installed state is mechanically equivalent to
`all(agent.cli_present is false)`. Changing login or process-readiness fixtures without
changing executable presence cannot change the boolean.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc}.py`, `vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_api.py` for CLI detection and the three-row AgentSupply fixture.
After K5 round 2 upgrades G-11 and that fixture freezes, I4's second increment owns
`ui/src/components/settings/models/{types.ts,SettingsModelsPage.tsx,modelRows.ts,modelRows.test.ts}`.
I5 then consumes the frozen backend/UI result in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-37 — Persist G-3 discovered-model retirement

**Acceptance.** FC-03 and FC-12 are the single final-shape authorities: DELETE on a
discovered model stages `retired: true`, applies the same exact-hop and protected-supply
guards as manual deletion, and on confirmed success retains exactly one row with the
same `id`, `origin`, and edited metadata. Manual DELETE still removes the row. Refresh
fixtures in which the upstream both includes and omits that id retain `retired: true`;
matching, model-capability eligibility, new-Route validation, runnability, and
invocation fixtures never consume the retired row.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, ownership of
`config/v2_config.py`, `core/handlers/model_hub/{service,resolver,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_{config,api,resolution}.py` transfers from I1 to I7. Those files
own persistence, refresh/DELETE/guard behavior, and `retired: false` filtering in
Add-time matching, inventory membership, new-Route validation, runnability, and
invocation. After K5 round 2 upgrades G-3 and I7 freezes the guarded Source fixtures, I4's
second increment owns `ui/src/components/settings/models/{types.ts,SourceRow.tsx,SourceRowMenu.tsx,SourceRow.test.tsx,modelRows.ts,modelRows.test.ts}`.
I5 then consumes the result in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-38 — Own the G-10 runtime installation state on the server

**Acceptance.** Runtime health validates against exactly the six registered decisions.
On a supported host, install persists `installing` before work with exactly
`installed_version: null`, `verified: false`, and `listening: null`; status reload and a
concurrent repeated install return that same state and start one job. One positive schema
fixture validates that shape, while separate negative fixtures make each of the three
fields contradictory and fail validation. Verified success
settles at `not_started` with null `error_key`; failure settles at `not_installed` with
`settings.models.install.fail.detail`. On process reconstruction, an orphaned
`installing` fixture either verifies an already-complete target to `not_started`, claims
one fresh job while staying `installing`, or settles at `not_installed` with that key;
it never remains ownerless. On an unsupported exact `host_platform`, the route performs
no download, returns `runtime_platform_unsupported`, and status remains
`not_installed`. Calls from `not_started`, `ok`, `degraded`, and `down` return the exact
current RuntimeDependency with HTTP 200, perform zero downloads, preserve the verified
binary, and neither start, stop, nor restart the process. Install has the same
authentication and CSRF negative fixtures as runtime start, while `/start` never
installs.

**Binding handoff (owner / files / activation).** After K4 and #1312 merge, I2 owns
`core/handlers/model_hub/turn_gateway.py`, `vibe/model_hub_runtime/**`, and
`tests/test_model_hub_runtime.py` for installer lifecycle, persisted lease/recovery, and
runtime no-op fixtures. After K6 also merges, I7 owns
`core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, `tests/test_model_hub_config.py`, and
`tests/test_model_hub_api.py` for the schema, RPC/HTTP/client, CSRF, and error boundary
without editing I2 files. Once both producers
freeze success, failure, unsupported, concurrent, installed-no-op, and restart fixtures,
I4's current increment owns
`ui/src/components/settings/models/{types.ts,runtimeLifecycle.ts,SettingsModelsPage.tsx,RuntimeNotStartedAction.test.tsx}`
plus `ui/src/i18n/{en,zh}.json` and `vibe/i18n/{en,zh}.json`. I5 consumes the combined
result in `tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-39 — Apply G-13 order to existing chains and close G-26

**Acceptance.** Seed Routes containing repeated listed Sources and multiple unlisted
Sources, invoke reorder, and sort the original hops by §4.6's exact stable key. The
response equals that expected order for every Route; the multiset of exact
`(source_id, model_id)` pairs and every explicit model mapping are unchanged. A second
invocation is byte-identical. No matching, add/remove, guard, force, or interruption
path runs. The route is the registered existing-chain consumer of `sources.order`.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc,request}.py`, `vibe/{ui_server,model_hub_client}.py`,
and `tests/test_model_hub_api.py` for the reorder route and before/after property fixture.
After K5 round 3 specifies G-32, K5 round 2 upgrades G-13/G-26, and that fixture
freezes, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,ModelRoutePicker.tsx,reorder.ts,reorder.test.ts}`.
I5 then consumes it in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-40 — Commit the G-14 native takeover transaction atomically

**Acceptance.** For `direct` → `hub` with a sanctioned recognized CLI login and no
native Source, one transaction creates exactly one backend-bound `native_cli` Source,
applies `placement-v1`, commits all accepted exact matches, changes mode, and returns an
AgentSupply that already contains those results. Injected failures at every commit seam
leave both mode and Source/Route state unchanged. Existing-native, absent-login,
unrecognized-login, non-transition, and repeated-request fixtures create zero Sources;
`cli_present` alone never satisfies recognition.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`config/v2_config.py`, `core/handlers/model_hub/{service,rpc,request}.py`,
`vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_{config,api}.py` for recognition reuse and the atomic transaction.
After K5 round 2 upgrades G-14 and I7 freezes all five transition fixtures, I4's second
increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,BackendSupplyModeCard.tsx,AgentCard.test.tsx}`.
I5 then consumes them in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-41 — Close the G-19 post-commit cancellation boundary

**Acceptance.** AC-26 and I1 continue to own every pre-commit cancellation cleanup
fixture. For cancellation after the durable Source commit, the server exposes no abort
branch: the Source, accepted placements, and AgentSupply state complete normally and
are coherent on the next read even when the response is never received. The fixture
contains neither Source deletion nor committed-credential revocation after that point.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc,request}.py`, `vibe/{ui_server,model_hub_client}.py`,
and `tests/test_model_hub_api.py` for cancellation ownership, commit-boundary faults,
and coherent readback. After K5 round 2 upgrades G-19 and I7 freezes both boundary fixtures,
I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,AddApiKeyDialog.tsx,asyncLifetime.ts,asyncLifetime.test.ts}`.
I5 then consumes them in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-42 — Reload G-20 Source adoption facts

**Acceptance.** Every Source returned by list, detail/mutation, API-key create, and OAuth
create carries `adopted_by` with the schema's exact item shape. After process restart,
the Source list projection equals the complete set of persisted `(backend, menu_model)`
references to that Source, uniquely sorted by backend then menu model, independent of
hop health and without a client chain walk.
In creation responses, top-level `adopted_by` is byte-equal to
`source.adopted_by`.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc}.py`, `vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_api.py` for Source read assembly and sorted serializer fixtures.
After K5 round 2 upgrades G-20 and those fixtures freeze, I4's second increment owns
`ui/src/components/settings/models/{types.ts,SourceRow.tsx,SourceRow.test.tsx,AdoptionNote.tsx,AdoptionNote.test.tsx}`.
I5 then consumes them in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-43 — Derive G-24 host support from the server platform

**Acceptance.** Every runtime API payload includes the server-detected
`host_platform`. Installation support is true if and only if that exact string appears
in `manifest.assets[].platform`; changing only the browser user agent or client platform
cannot change it. The unsupported-host install fixture performs no asset request.

**Binding handoff (owner / files / activation).** After K4 and #1312 merge, I2 owns
`vibe/model_hub_runtime/**` and `tests/test_model_hub_runtime.py` for host detection and
the manifest producer. After K6 also merges, I7 owns
`core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and `tests/test_model_hub_api.py` for the API
projection and exact 422. Once both supported/unsupported fixtures freeze, I4's current
increment owns
`ui/src/components/settings/models/{types.ts,runtimeLifecycle.ts,SettingsModelsPage.tsx,RuntimeNotStartedAction.test.tsx}`
plus `ui/src/i18n/{en,zh}.json` and `vibe/i18n/{en,zh}.json`. I5 consumes the combined
result in `tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-44 — Distinguish G-25 all-stale Routes from empty Routes

**Acceptance.** Every `model_supply` row includes `has_runnable_hop`, and its value
equals `any(hop.runnable for hop in the complete exact AgentChain)`. A nonempty fixture
whose every hop is stale yields `{chain_length: N, has_runnable_hop: false}` with
`N > 0`; an empty fixture yields `{chain_length: 0, has_runnable_hop: false}`. No
consumer infers the boolean from length. The schema accepts the empty/false pair and
rejects `{chain_length: 0, has_runnable_hop: true}`; producer and API fixtures cover
both cells.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc}.py`, `vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_api.py` for exact-chain annotation and live/all-stale/empty
AgentSupply fixtures. After K5 round 2 upgrades G-25 and those fixtures freeze, I4's second
increment owns
`ui/src/components/settings/models/{types.ts,modelRows.ts,modelRows.test.ts,sufficiency.ts,sufficiency.test.ts}`.
I5 then consumes them in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-45 — Define the complete G-27 SourceCreate request

**Acceptance.** `source-create.schema.json` rejects every property outside its seven
registered fields, requires `vendor` and nonempty write-only `key`, and accepts optional
display, endpoint, full three-value probe order, client nonce, and explicit unavailable-
inventory consent. Contract examples
validate. Create responses and logs contain no plaintext key, and request fixtures
cannot submit server-owned identity, protocol conclusion, inventory, health, usage,
custody, or timestamp fields.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc,request}.py`, `vibe/{ui_server,model_hub_client}.py`,
and `tests/test_model_hub_api.py` for request validation and provisioning boundaries.
After K5 round 2 upgrades G-27 and I7 freezes valid, empty-inventory, and rejected-body
fixtures, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,AddApiKeyDialog.tsx,dialogFields.tsx,dialogFields.test.tsx}`.
I5 then consumes them in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-46 — Bind and number every G-28 guarded mutation plan

**Acceptance.** Every `would_remove_hops` and `removed_hops` item includes one-based
`position` in its named pre-mutation Route. Cross-Route output sorts by backend, menu
model, then position. For the same planned mutation, refusal and confirmed success
report byte-identical RouteHopRef arrays even though the latter commits the cascade.
Every refusal validates against `guard-refusal.schema.json`, has a nonempty current
plan, and pairs its lead error with the corresponding nonempty evidence array:
route/source-model errors require `would_remove_hops`, while `source_last_supplier`
requires `would_interrupt`. Positive fixtures cover all three relations; negative
fixtures keep the other array nonempty while leaving the required array empty and fail
schema validation. Both plan arrays are sets: a structurally duplicate `RouteHopRef` or
`SupplyGap` item fails schema validation, and positive plus duplicate-negative fixtures
cover each array. `would_interrupt` has one canonical producer order: ascending
`(backend, model_id)`, with every nested `agents` array ascending by stable Agent id.
Producer fixtures permute both input levels and assert byte-identical ordered output.
The fixture covers every row of the force/plan/echo totality matrix: unforced empty
success; unforced nonempty refusal; forced nonempty exact two-array echo success; forced
nonempty absent or differing echo returning the newly recomputed plan; and a world change
where the forced retry carries the old nonempty echo but recomputes to an empty plan and
takes ordinary success without a 409 or removed hop. A direct `route_replace` fixture
removes one hop while retaining runnable protected supply and succeeds once without 409;
its paired interruption fixture refuses with the existing `source_last_supplier` code
and enters the same echo matrix. No token, digest, version receipt,
server-side confirmation state, or parallel plan-changed discriminator exists. The
closed error vocabulary, error-to-plan relation, and guard-decision vocabulary are
checked through `mirror-registry.json`.

**Binding handoff (owner / files / activation).** K4 freezes
`docs/plans/model-hub-contracts/{guard-refusal.schema.json,api.md,mirror-registry.json}`.
After K4, #1312, and K6 merge, I7 owns the sole shared guard planner and exact-array comparison in
`core/handlers/model_hub/service.py`; transport-only changes in
`core/handlers/model_hub/{rpc,request,errors}.py` and
`vibe/{ui_server,model_hub_client}.py`; and every totality/concurrent fixture in
`tests/test_model_hub_api.py`. K4's negative schema-relation fixture lives in
`tests/test_model_hub_config.py`; K6 never owns that test, and I7 consumes and extends it
only after all three prerequisites merge.
After K5 round 3 specifies G-32, K5 round 2 upgrades G-28, and I7 freezes those
fixtures, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,ModelRoutePicker.tsx,ModelRoutePicker.test.tsx}`
for numbered rows, ordinary noninterrupting removal, interruption-only two-array plan
echo, and replacement by a newly refused plan.
I4's current increment owns none of this confirmation path. I5 then consumes the frozen result in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-47 — Reconcile G-29 lost Source-create responses

**Acceptance.** When SourceCreate supplies a valid `client_nonce`, the server atomically
reserves that unique value in process before observation or credential work and stores
no durable claim, digest, terminal envelope, or plaintext credential with it. The
committed Source alone persists and
echoes the nonce unchanged so list reads expose exactly one match. After a lost response,
the client reads Sources before retrying; a match terminates reconciliation and a miss
permits the same-nonce state-machine action. One table-driven
fixture covers every state/action cell. `nonce.in_flight` returns HTTP 409
`source_create_in_progress` before observation or provisioning. `nonce.released` first
settles AC-26 cleanup, then the same-nonce retry atomically reserves the value and performs
exactly one fresh attempt. `nonce.committed` returns HTTP 409 `source_nonce_conflict`
without upstream work or a replay promise; the client then reads Sources and recognizes
the committed row by exact nonce. Source deletion releases the nonce and makes it
claimable again; AC-51 owns the positive fresh-create boundary after that deletion.
After a simulated lost response, in-progress waits/retries, committed conflict reads,
and a released/list-miss retry all terminate without a separate endpoint.
Process termination ends the work and its in-memory reservation. Reconstruction
reconciles only AC-26 durable pending-revocation material; the next same-nonce attempt is
fresh and no ownerless claim is reconstructed.
Omitting the nonce preserves existing create behavior.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`config/v2_config.py`, `core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_{config,api}.py` for committed-column validation, process-local
reservation, cleanup/restart, Source-delete release, and the complete state/action AC-47
fixture. After K5 round 2 upgrades G-29 and I7 freezes in-progress conflict, committed conflict
plus list lookup, released reclaim, cleanup/recovery, Source-delete release, and
lost-response fixtures, I4's second increment
owns `ui/src/components/settings/models/{types.ts,modelsApi.ts,AddApiKeyDialog.tsx,asyncLifetime.ts,asyncLifetime.test.ts}`.
I5 then consumes them in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-48 — Reconcile G-30 lost OAuth-start responses

**Acceptance.** When OAuth start supplies `client_nonce`, the server atomically claims
the exact `(client_nonce, vendor, channel)` tuple before provider work and every flow
response echoes it. One table-driven fixture covers all three states and every exit:
released claims start exactly once, including after cleanup from a provider-start
failure/task cancellation before a flow exists; an in-flight same-tuple retry coalesces
to the same pending terminal result without a second provider invocation; and committed
retries return the same `flow_id`, state, and presentation. Provider success atomically
converts the claim to a flow with a non-null date-time `expires_at`, and every later
response for that flow preserves the same bounded deadline. An ordinary or
presentation-only flow without a nonce retains the existing nullable expiry branch.
Explicitly canceling a nonce-bearing committed flow
cancels provider work but retains that same bounded terminal flow as `state: "cancelled"`
until its existing `expires_at`; a same-tuple retry inside that window returns the
canceled flow with zero provider starts. Clocked expiry releases the tuple, and the first
same-tuple retry afterward starts exactly one fresh flow. Explicit cancellation without
a nonce forgets the flow. A different tuple cannot resolve to the retained flow, and a
new user action always generates a new nonce. This zero-new-concept closure keeps
`contract_version: 5` under the PM ruling of 2026-08-12 00:37.

The contract fixture accepts a nonce-bearing flow with a date-time expiry, rejects that
same flow with `expires_at: null`, and accepts the null expiry after `client_nonce` is
omitted. These three cells mechanically prove both the nonce implication and preservation
of the pre-existing non-nonce branch.

**Binding handoff (owner / files / activation).** After K4 and #1312 merge, I3 owns
`core/handlers/model_hub/oauth.py`, `tests/test_model_hub_oauth.py`,
`tests/scenarios/auth_setup/catalog.yaml`, and
`tests/scenarios/auth_setup/test_auth_setup_scenarios.py`. AUTH-SETUP-210 blocks the
first provider call, loses that caller's response, overlaps a same-nonce retry, receives
the same terminal flow/provider through both callers, and proves provider start exactly
once. The OAuth unit table additionally covers pending-start cleanup/release, explicit
cancel followed by same-tuple canceled replay with zero provider calls, existing-expiry
release followed by one fresh provider start, different-tuple isolation, and no-nonce
forget. After K6 also merges, I7 owns only
`core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and `tests/test_model_hub_api.py` for the service
and API envelope, including the nonce-aware cancel branch and clocked expiry boundary.
After K5 round 2 upgrades G-30 and I3/I7 freeze same-tuple, different-tuple,
omitted-nonce, released/in-flight/committed, canceled-replay/expiry, and concurrent
lost-response fixtures, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,OAuthConnectDialog.tsx,oauthResult.test.ts}`.
I5 only consumes the completed flow in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_native_oauth_scenarios.py}` after
all producer and UI fixtures settle.

### AC-49 — Withdraw the observation evidence expansion

**Acceptance.** Under the 2026-08-11 20:35 owner subtraction ruling,
`observation-result.schema.json` has `contract_version` plus exactly the established six result fields and rejects
an `evidence`, `request`, or `status` sibling. Model Hub contracts and implementation
lanes add no evidence producer, mirror vocabulary, or UI payload field; protocol and
discovery decisions continue to use the existing semantic result only.

**Binding handoff (owner / files / activation).** K4 freezes the absence in
`docs/plans/model-hub-contracts/{observation-result.schema.json,api.md,mirror-registry.json}`.
After K4 merges, K5 round 2 alone owns `docs/plans/model-hub-ui-spec.md` §1.5 and removes
its request/status slots under the 20:35 ruling; no backend or I4 implementation file is
activated by AC-49.

### AC-50 — Classify network failures without inventing persistent health

**Acceptance.** One table-driven fixture covers the Cartesian product of failure shape
(`shaped` explicit closed upstream classification versus unclassified `transport`) and
phase (`stream_started: false` versus `stream_started: true`). `stream_started` is false
until the first user-visible model-output byte and true from that byte onward; HTTP
status, headers, and other response bytes do not cross it. The existing
`network_failure.*_before_first_byte` and `network_failure.*_after_first_byte` decision
IDs retain their spelling but refer to this canonical model-output boundary. Both shaped
cells enter exactly their existing non-permanent Source classification and unchanged
recovery rule; phase changes only retry/replay behavior. Unclassified pre-output
connection failure retains Source/config bytes exactly, emits the redacted network
event, and creates one
Source-scoped in-memory `health: backoff` projection with
`reason: models.source.backoff.connection_failed`, a future `retry_at`, and the exact
bounded delays 1, 2, 4, 8, 16, then 30 seconds. Expiry makes the hop runnable; a later
first user-visible model-output byte from that same affected Source, Source
endpoint/credential replacement, or process reconstruction clears the streak. A
successful fallback from another Source is a negative fixture and does not clear it.
Before serialization, the API/read assembler captures one read time: a live backoff
must have `retry_at` strictly after that time, while an expired overlay is normalized to
the Source's underlying non-backoff health and runnability and is never emitted as stale
backoff. Clocked positive/expired fixtures cover this boundary in I7 API/service tests;
the contract fixture records the boundary because Draft-07 cannot compare a date-time to
the moving clock. While the deadline is live, the overlay applies only over an otherwise
healthy hop whose exact Source/model capability remains present. Concurrent cooldown,
`needs_action`, `error`, `source_missing`, or `model_unsupported` suppresses the overlay
and emits that blocker's established health/reason/retry facts; the four non-self-healing
rows roll up `interrupted`, so `waiting` cannot mask a durable blocker. The sole overlay
exception is the native-process row below. A nonempty process-available chain blocked only by
cooldown/backoff must validate as `waiting` and must fail validation as `interrupted`;
positive and negative schema fixtures cover that converse. If a `native_cli` process is
simultaneously unavailable, its actionable
`native_cli_unavailable` fact takes the single reason slot; `health: backoff` and the
future `retry_at` remain unchanged, `runnable` stays false, and the chain is
`interrupted`. Restoring the process reveals any still-live connection backoff.
Unclassified post-output interruption emits only the event: it writes no
Source/config state, creates no backoff, performs no replay, and does not change the
chain. Schema fixtures reject the removed persistent network/timeout cooldown keys,
reject backoff on Source state, and reject any backoff hop without false runnability,
one of the two precedence-valid closed reasons, and a date-time deadline. A ProbeResult
carrying `models.source.backoff.connection_failed` implies exactly `channel: hub`,
`reachable: false`, and `latency_ms: null`; native-channel and non-null-latency fixtures
fail. Contract version
stays 5 under the owner’s
pre-release semantic correction of 2026-08-11 19:44–19:56 and the blocker/Probe closure
of 2026-08-11 23:49.

**Binding handoff (owner / files / activation).** K4 freezes
`docs/plans/model-hub-contracts/{source,agent-chain,agent-supply,probe-result}.schema.json`,
`api.md`, and `mirror-registry.json`. After K4 and #1312 merge, the orchestrator dispatches K6
for one bounded round over `docs/plans/model-hub.md` §4.5's Turn-outcome segment and
`docs/plans/model-hub-contracts/{api.md,turn-provenance.schema.json,resolution-event.schema.json,mirror-registry.json}`.
K6 freezes G-34's exact outcome id, copy key, and payload shape plus any §4.5 gap row left
by #1312; it edits either schema only when that selected shape requires it and owns no
implementation or test file. After K4, #1312, and K6 merge, ownership transfers from
I1 to I7 for `core/handlers/model_hub/{service,resolver,classification,errors,events,rpc}.py`,
`modules/agents/model_hub.py`, `vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_{api,resolution}.py`. For the Model Hub failure-callback seam only,
the same three-way activation transfers `modules/agents/base.py`, `modules/agents/claude_agent.py`,
`modules/agents/codex/{agent,event_handler}.py`,
`modules/agents/opencode/{agent,poll_loop}.py`, exact phase fixtures in
`tests/{test_claude_agent_sessions,test_codex_agent,test_opencode_server}.py`, and
mechanical signature consumers in
`tests/{test_claude_cli_path,test_multi_platform_runtime}.py` to I7. I7 alone owns the
native classifier/state decision, cross-backend raw-shape/phase handoff, live-backoff
executor/read projection, Source no-write assertions, same- versus other-Source reset,
clocked live/expired API assembly, the concurrent transition matrix for cooldown,
needs-action, error, missing Source, unsupported model, and native-process precedence,
the exact ProbeResult relation, and the event-only cell plus its K6-defined G-34
executor/provenance fixture;
no native callback may persist the old 30-second network cooldown.
I2 retains
`core/handlers/model_hub/turn_gateway.py`, `vibe/model_hub_runtime/**`,
`tests/test_model_hub_runtime.py`, and `tests/test_model_hub_l3.py`; it supplies the raw
shaped/transport and `stream_started` facts at the model-output boundary for the managed
Gateway but does not classify or
mutate Source/backoff state. Under the 2026-08-11 20:20 gate ruling as corrected through
the 21:58 PM ruling, the future contract assertions live alone in the adjacent strict-xfail test
`tests/test_model_hub_l3.py::test_probe_transport_failures_await_ac50_backoff_contract`;
the mixed chain/probe test remains active. I7's first mechanical action is to delete that
strict marker in the same commit that makes its `connection_failed` live-backoff and zero
Source/config-write assertions XPASS; no other I2-file
edit is authorized. After K5 round 2 registers the distinct short-backoff copy and I7
freezes all four cells plus same-Source reset, other-Source non-reset, clock, and cap
fixtures, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,modelRows.ts,modelRows.test.ts,sufficiency.ts,sufficiency.test.ts}`
and both locale trees. I5 then consumes the frozen behavior in
`tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-51 — Make G-29 nonce release after Source deletion explicit

**Acceptance.** Nonce uniqueness exists only while a live-process reservation or live
Source owns the value. A positive fixture commits a nonce-bearing Source, deletes that
Source, reads the Source list and observes no matching nonce, then submits the same nonce
again. The server treats that request as a fresh create: it performs exactly one new
upstream attempt and commits exactly one new Source with a new server-issued id and the
same nonce. No tombstone, gone result, receipt, digest, replay, or deleted-Source
protection state exists. The supported UI client always performs the D-36 Source read
before a lost-response retry; a stale client that skips that read and recreates a deleted
Source is explicitly outside the single-user threat model. A restart fixture proves the
in-process reservation disappears, AC-26 pending-revocation material settles, and the
next same-nonce request is one fresh attempt; no durable claim collection is serialized.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`config/v2_config.py`, `core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_{config,api}.py` for live-only uniqueness, restart release,
Source-delete release, new-id recreation, and upstream-once fixtures. After K5 round 2 upgrades G-29 and I7 freezes
those fixtures, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,AddApiKeyDialog.tsx,asyncLifetime.ts,asyncLifetime.test.ts}`
for the mandatory read-before-retry client branch. I5 then consumes the frozen behavior
in `tests/scenarios/model_hub/{catalog.yaml,test_model_hub_live_resolution_scenarios.py}`.

### AC-52 — Require acknowledgement before OAuth re-auth on either supply channel

**Acceptance.** `POST /api/models/sources/<id>/reauth` starts no OAuth adapter work
unless the request carries `{"acknowledge_irreversible": true}`. This is identical for
Hub OAuth and `native_cli` Sources: missing or false acknowledgement returns the existing
`reauth_confirmation_required` error before the adapter is called, and a true
acknowledgement permits exactly one ordinary re-auth start. The rule does not extend to
transactional API-key replacement through `PUT /credential`. A negative Hub fixture and
the corresponding native fixture prove zero adapter calls; positive fixtures prove both
channels cross the same server boundary. This additive pre-release correction keeps
`contract_version: 5` under the PM ruling of 2026-08-12 00:15.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and `tests/test_model_hub_api.py` for the shared
pre-adapter acknowledgement gate and both-channel positive/negative route fixtures. At
the earlier K4 + #1312 edge, I3 owns `tests/scenarios/auth_setup/catalog.yaml` and
`tests/scenarios/auth_setup/test_auth_setup_scenarios.py` for AUTH-SETUP-109. The case
selects a Hub-held Source, proves missing and false acknowledgement return
`reauth_confirmation_required` before any adapter/provider call, proves true
acknowledgement starts exactly one Hub flow, and closes only after terminal status and the
repair read projection agree. `core/handlers/model_hub/oauth.py` remains downstream of
the I7 gate and requires no ownership transfer. I5 consumes the completed scenario but
does not edit either I3 file. Existing UI confirmation consumes the same request field;
this criterion creates no UI-spec or locale change. K4's narrow
`tests/test_model_hub_config.py` ledger assertion freezes the scenario id, both files,
journey, lane split, and activation edge before implementation starts.

### AC-53 — Make OAuth materialization-error route impact observable exactly once

**Acceptance.** OAuth status/submit implements all five rows of the authoritative
`oauth_terminal.*` matrix. A successful terminal re-auth response owns the complete
`interrupted_pairs` array and may return it empty. A local terminal materialization error
uses the standard error envelope and never returns `flow`: when acquisition-stage Source
mutation has already left one or more exact `(backend, model_id, agents)` supply gaps,
the envelope contains that exact nonempty `interrupted_pairs`; when no persisted
interruption exists, the member is absent rather than `[]`. The same materialization
error code may take either branch, so code identity alone cannot decide the payload.
Ordinary adapter failure/cancellation remains successful `{flow}` state settlement, and
every non-materialization error omits `interrupted_pairs`. The error member never aliases
the future-tense guard field `would_interrupt`, and no new field, error code, or contract
version is introduced.

A positive fixture drives native re-auth materialization failure after acquisition has
already stranded a sibling and asserts the exact nonempty report, standard error
envelope, absent `flow`, persisted Source state, and subsequent Source-list refetch. The
negative table drives the same materialization error with no gap, a materialization
failure before route impact, and an ordinary non-materialization error; all omit the
member. A success fixture proves re-auth still sends the complete array and accepts `[]`.

**Binding handoff (owner / files / activation).** After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,errors}.py`, `vibe/{ui_server,model_hub_client}.py`, and
`tests/test_model_hub_api.py` for the sole materialization decision, exact error-envelope
transport, persisted-impact positive fixture, same-error/no-gap negative fixture, and
ordinary-error negative fixture. After K4 merges, K5 round 2 alone records E6 in
`docs/plans/model-hub-ui-spec.md`; it edits no contract or implementation file. After
that K5 round and the I7 payload fixtures freeze, I4's second increment owns
`ui/src/components/settings/models/{modelsApi.ts,OAuthConnectDialog.tsx,apiFailure.test.ts,oauthResult.test.ts}`
for exact optional error-member consumption and the Source-list refetch. I5 consumes the
settled behavior only after I7 and I4 finish; it owns no AC-53 producer. K4's contract
fixture in `tests/test_model_hub_config.py` freezes the five decision ids, D22 mirror,
presence/absence totality, and this lane/file/activation handoff.

### AC-54 — Require explicit consent before saving unavailable inventory

**Acceptance.** `SourceCreate.accept_unavailable_inventory` is an optional boolean whose
omission is `false`. The server repeats response-backed observation for every create and
uses that new result rather than trusting the preceding unsaved observation. When the new
result proves a protocol and returns `discovery: failed`, omission or `false` returns the
existing classified `discovery_failed` without a Source or committed credential, after
AC-26 cleanup settles; `true` commits exactly one Source with the proved protocol,
`models: []`, and the existing uncertain health projection. A successful discovery follows
the ordinary create path regardless of the boolean, including a legitimately empty
inventory. A result without protocol proof, reachability, or authentication still rejects
regardless of the boolean. Thus the field authorizes only the state-⑤ inventory-failure
cell and never supplies protocol or inventory evidence. `contract_version` remains 5.

The K4 contract fixture validates omission, explicit `false`, and explicit `true`; rejects
non-booleans; and mechanically finds every row of the server-observation × consent table.
Its negative row requires `discovery_failed`, no Source, no committed credential, and
AC-26 cleanup. Its positive row requires the proved protocol, empty inventory, and exactly
one commit. A clean-observation fixture proves a stale `true` is inert rather than turning
a newly successful repeat observation into a failure.

**Binding handoff (owner / files / activation).** This K4 micro-round freezes
`docs/plans/model-hub-contracts/{source-create.schema.json,api.md}` and
`tests/test_model_hub_config.py`. After it merges, K5 round 2 alone updates
`docs/plans/model-hub-ui-spec.md` state ⑤ to name `accept_unavailable_inventory`; no other
UI-spec state emits it. After K4, #1312, and K6 merge, I7 owns
`core/handlers/model_hub/{service,rpc,request,errors}.py`,
`vibe/{ui_server,model_hub_client}.py`, and `tests/test_model_hub_{config,api}.py` for the
repeat-observation gate and positive/negative API fixtures. After K5 round 2 merges and I7
freezes those fixtures, I4's second increment owns
`ui/src/components/settings/models/{types.ts,modelsApi.ts,AddApiKeyDialog.tsx,dialogFields.test.tsx,asyncLifetime.test.ts}`
for sending `true` only from state ⑤ and omitting the field from clean creation, retry, and
pull-origin state ⑤′. I5 consumes the settled behavior after I7 and I4; it owns no AC-54
producer.

### K4 open contract-gap registry

| Gap | Missing contract truth | Consumer | Activation edge |
| --- | --- | --- | --- |
| **G-34 — truncated stream with the same current hop** | §4.5 has no Turn-outcome row for a stream truncated after user-visible model output when the network policy writes no Source state and the same hop remains `current`. The current `streamed_fallback` behavior depends on the persistent cooldown that I7 must remove, so it cannot be treated as the future row. This entry records the absence only; it does not choose an outcome id, copy key, or payload shape. G-33 remains the K5/UI-owned OAuth device-code/helper presentation gap and never names this behavior. | K6 contract authority, then I7 executor/provenance implementation and its exact turn fixture | After K4 and #1312 merge and the §4.5 Turn-outcome matrix segment is unfrozen, the orchestrator dispatches K6 for its single registered-gap round; K6 must merge before I7 starts. **PM rulings: 2026-08-12 00:18 and 02:00.** |

### Post-consolidation review ledger — sealed 2026-08-08

The orchestrator-authorized consolidation commit closes the constructive prose loop.
The 2026-08-09 owner rulings explicitly reopen the no-migration final shape,
response-observed protocol, enumerated 18-thread closeout, and the two narrowed UI-lane
AC handoffs routed by the owner; they do not repeal the seal for review-generated scope.
After that head, a review finding permits a normative-text edit only when it identifies
two specific normative sentences that contradict each other. Every other actionable
finding is copied verbatim into this ledger with its AC, implementation landing point,
and responsible lane; the review reply cites this ruling and the thread is resolved.
No new explanatory prose is added elsewhere in the planning documents.

The four findings that caused the consolidation — OpenCode provider normalization,
native Source identity, per-hop live runnability, and enrollment versus adoption — are
resolved by the final-shape handoff above. S-1 removes enrollment entirely; `adopted_by`
now projects persisted route references, while §4.3 owns only live execution. They
predate the seal and are not deferred ledger items.

**S-1 architecture disposition (owner 2026-08-09).** The next exact-head review did
trigger the forward stop. S-1 supplies the required design action rather than another
lane patch: §4.3 is now machine-checkable pseudocode over one persisted chain, with no
runtime matching. Its bounded rewrite closes the five reviewed contradictions in their
authoritative homes: blocked `no_candidate` copy and all-phase `engine_down` in the
Turn-outcome matrix; the final one-member eligibility enum in AC-19; Source deletion
from every persisted route in the mutation/postcondition contract; and
`native_cli_unavailable` in the blocker/supply-status relation. These findings are
architecture inputs to S-1, not sealed-ledger deferrals.

| Finding (verbatim) | AC | Implementation landing point | Responsible lane |
| --- | --- | --- | --- |
| **Guard the saved Fetch models mutation**<br><br>When a user runs the newly specified “Fetch models” action from saved Source details and upstream discovery removes a referenced model, lines 185–191 say that operation replaces the discovered slice, but this exhaustive guard list covers refresh/recovery and manual deletion without covering saved discovery. Fresh evidence beyond the earlier inventory-shrink thread is that Fetch models is now a separate saved-Source mutation; unless it is explicitly defined as an alias of guarded refresh or given the same exact-hop and supply-gap handling, it can invalidate a Custom hop or the last Follow supplier without refusal, cascade confirmation, or stale-hop retention. | AC-26 | `core/handlers/model_hub/service.py`; final `api.md`; `tests/test_model_hub_api.py`; saved Source-details Fetch-models UI test | I1 (shape/guard) + I4 (action) + I5 (scenario) |
| **Include the required auth-setup scenarios**<br><br>When contract rounds change the multi-step OAuth flows—vendor-specific defaults, duplicate-native rejection before adapter work, re-auth acknowledgement, polling, cancellation, and retry—the final landing checklist names only `tests/scenarios/model_hub/**` and the lower-level native OAuth harness. That can leave the repository's auth/setup catalog and its closed-loop user journey on the old flow even while all listed Model Hub tests pass; add or update the mandated `tests/scenarios/auth_setup/catalog.yaml` entry and `test_auth_setup_scenarios.py` case in this handoff.<br><br>AGENTS.md reference: [AGENTS.md:L253-L253](https://github.com/avibe-bot/avibe/blob/7984aabf4e1d9d541084c7078dba093f2832045d/AGENTS.md#L253-L253) | AC-23 + AC-25 | `tests/scenarios/auth_setup/catalog.yaml`; `tests/scenarios/auth_setup/test_auth_setup_scenarios.py` | I1 (final auth envelopes) + I3 (catalog/test) + I5 (integration gate) |
| **Define protocol observation for OAuth-created Sources**<br><br>When a Claude/Codex native login or a Hub-held subscription OAuth flow completes, the resulting Source still requires a `protocol`, but the specified observation workflow covers only the API-key Add form and never defines how OAuth credentials are probed before commit. The current OAuth creation path uses `core/handlers/model_hub/service.py::_default_protocol(binding.vendor)`, which this rule explicitly forbids; removing that default leaves OAuth creation without a contract-valid value, while retaining it violates AC-27. Define response-backed observation and failure/credential-cleanup behavior for OAuth Sources, or make `protocol` conditional where native dispatch does not use it. | AC-23 + AC-27 | final `source.schema.json`, `oauth-flow.schema.json`, `probe-result.schema.json`, and `api.md`; `core/handlers/model_hub/{service,oauth,native_oauth,revocations}.py`; OAuth/API/scenario tests | I1 (final shape/API) + I3 (OAuth observation/cleanup) + I5 (scenario) |
| **Define discovery collisions with manual model IDs**<br><br>When a user manually adds model id `foo` and a later Fetch models result also contains `foo`, the preceding discovery rule says to preserve the manual entry while these lines require unique ids and say discovery creates a discovered entry. Without collision precedence, an implementation must either emit duplicate ids, fail the refresh, or overwrite the user's manual origin and edited capability metadata. Define a deterministic coalescing rule for manual/discovered collisions and cover it in the discovery fixtures. | AC-26 | final `source.schema.json`, `api.md`, and adapter interface; `core/handlers/model_hub/service.py`; API, adapter, Source-details UI, and scenario fixtures | I1 (shape/consumer) + I4 (result rendering) + I5 (scenario) |
| **Observe protocols before committing imported Sources**<br><br>When a user applies native-config import, this universal invariant conflicts with the retained import flow in §6: the current scanners derive `protocol` from vendor or local wire configuration (`core/handlers/model_hub/migration.py:180-220`, `285-306`, and `489-520`), and `_source_from_item` persists that value directly at lines 596-605 without obtaining an upstream response. I1 must therefore either preserve import while violating AC-27, or enforce AC-27 and prevent an imported Source from being created whenever the upstream is unavailable; FC-12/FC-13 define neither observation nor its failure/credential-cleanup behavior. Require response-backed observation during import, or explicitly scope and contract an exception.<br><br>AGENTS.md reference: [AGENTS.md:L145-L147](https://github.com/avibe-bot/avibe/blob/0ce001cfd62fdab736492c094914aa32a4991688/AGENTS.md#L145-L147) | AC-23 + AC-27 | final `migration-scan.schema.json`, `probe-result.schema.json`, and `api.md`; `core/handlers/model_hub/{migration,service,revocations}.py`; native-import API and scenario tests | I1 (final shape/API) + I3 (import observation/cleanup) + I5 (scenario) |
| **Assign the actual auth-setup scenario file**<br><br>Fresh evidence after the recorded auth-scenario thread is the repository layout: repo-wide search finds the harness at `tests/scenarios/auth_setup/test_auth_setup_scenarios.py`, while this lane assigns the nonexistent `tests/test_auth_setup_scenarios.py`. Because I5 owns only the Model Hub scenario tree, the real closed-loop OAuth/setup test remains outside every active lane and can stay stale while the listed work completes; correct the path and ownership here and in the landing ledger.<br><br>AGENTS.md reference: [AGENTS.md:L253-L253](https://github.com/avibe-bot/avibe/blob/0309b62d167ede209bd429afc401144c93cc677a/AGENTS.md#L253-L253) | AC-23 + AC-25 | `tests/scenarios/auth_setup/catalog.yaml`; `tests/scenarios/auth_setup/test_auth_setup_scenarios.py` | I3 |
| **Assign the shared backend launch seam to an active lane**<br><br>**Closed by PM rulings 2026-08-11 22:37 and 2026-08-12 02:00.** Through #1312, I1 owns `modules/agents/model_hub.py`. After K4, #1312, and K6 merge, I7 exclusively owns that shared classification/state seam plus the narrow Model Hub failure callbacks in Claude, Codex, and OpenCode. Backend callbacks supply raw failure shape and exact `stream_started` phase, whose boundary is the first user-visible model-output byte; I7 removes the native 30-second persistent network cooldown and implements AC-50's live-backoff/event-only totality, consuming K6's G-34 Turn-outcome row for the post-output same-current-hop case. I2 retains only managed-Gateway raw phase facts. Exact cross-backend phase fixtures and mechanical signature consumers are named in the binding table and landing checklist, leaving one active owner and one three-way activation edge.<br><br>AGENTS.md reference: [AGENTS.md:L28-L30](https://github.com/avibe-bot/avibe/blob/0309b62d167ede209bd429afc401144c93cc677a/AGENTS.md#L28-L30) | AC-22 + AC-50 | `modules/agents/model_hub.py`; Model Hub callbacks in `modules/agents/{base,claude_agent}.py`, `modules/agents/codex/{agent,event_handler}.py`, `modules/agents/opencode/{agent,poll_loop}.py`; `tests/{test_claude_agent_sessions,test_codex_agent,test_opencode_server,test_claude_cli_path,test_multi_platform_runtime}.py` | I7 after K4 + #1312 + K6 merge |
| **Specify the add-time model matching algorithm**<br><br>**Closed by owner ruling 2026-08-09: `matching-v1`. Amended 2026-09-04 (§4.2, §4.8 v4): the OpenCode branch — exact checked identifier or unique bare-suffix match — is retired; OpenCode menu ids match by literal model-id equality exactly like Codex.** The Add Source path transcribes the former resolver semantics: Claude native aliases use discovered-only inventory and the deterministic `(version_tuple, date_or_zero, model_id)` maximum; OpenCode (v4), Codex, and non-native sources use literal equality. The stored hop is the concrete upstream id and runtime never repeats matching. | AC-22 | `model-hub.md` §4.2 `matching-v1`, `opencode-overlay.md`, `api.md`, `core/handlers/model_hub/{service,resolver}.py`, and backend fixtures in `tests/test_model_hub_{api,resolution}.py` | I1 |
| **Assign the controller gate removal to I1**<br><br>Fresh evidence beyond the resolved default-off thread is that this landing row promises an available Models controller but assigns only handler files and `vibe/ui_server.py`; neither the active lane scopes nor the complete checklist includes `core/controller.py` or `tests/test_controller_model_hub_gate.py`. The current controller returns early when `VIBE_MODEL_HUB_ENABLED` is absent, and that test explicitly expects `model_hub_runtime is None`, so I1 can satisfy every named file/test while fresh installations still have no runtime. Add the controller and its gate test to I1's owned transition.<br><br>AGENTS.md reference: [AGENTS.md:L145-L147](https://github.com/avibe-bot/avibe/blob/0309b62d167ede209bd429afc401144c93cc677a/AGENTS.md#L145-L147) | AC-22 + AC-31 | `core/controller.py`; `tests/test_controller_model_hub_gate.py` | I1 |

**Forward stop.** After the S-1 head, if a review reports at least three findings in the same
runtime/fallthrough, Source identity/inventory, or route-data-semantics classes, freeze
this lane without editing. Return the complete inventory to the owner for the separate
design action of expressing the algorithm in a machine-checkable form such as
pseudocode or a decision table.

## 7. Kickoff checklist (orchestrator)

**This is the v1 kickoff and it is spent.** It ran: its surveys are on disk
(`model-hub-engine-survey.md`, `model-hub-tos-review.md`), the contracts dir was
authored and frozen from them, and the v1 build shipped dormant. Its `L1/L2/L4` are
**v1** lane IDs, not the v2 ones in §3. Do not run it again — the v2 kickoff is §3's
merge order: L0 lands, then L1 with the `contract_version: 3` bump, then L2/L3/L4 in
parallel, then L5, then L6. Kept below as the record of how the effort opened.

- [ ] Owner approves this plan (lanes, sequencing, gates).
- [ ] design.pen saved; V4 frames re-exported into a stable reference dir.
- [ ] S1–S3 dispatched (S1/S3 codex, S2 research either).
- [ ] Contracts dir authored from S1 output; frozen and announced.
- [ ] L1/L2/L4 briefs written (scope, no-touch, contracts, review protocol) and dispatched.
