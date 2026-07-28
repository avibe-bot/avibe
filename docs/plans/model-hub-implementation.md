# Model Hub — Implementation Plan

Status: draft v1 · 2026-07-23 · follows the signed product spec
Spec (signed 2026-07-23): `docs/plans/model-hub.md`
Design source: `../avibe-docs/design.pen` frames `产品改造 V4 01r – 09`
Lane workflow standard: `~/vibe-remote-project/.agents/skills/pr-delivery-loop/SKILL.md`

> **Superseded for ordering (2026-07-29).** This document plans the **v1** build,
> which shipped dormant. Spec v2 moved the spend order from one global list to a
> per-backend ordered subset (`model-hub.md` §0, §4.2; contracts at
> `contract_version: 2`). Anything below that assumes a global priority list is
> historical. The v2 lane plan is cut as a separate document; this one is not
> being rewritten in place, only annotated where it would otherwise contradict
> the frozen contracts. **§8 is the exception: it is v2-current and binding** —
> the acceptance criteria the spec-v2 review loop handed to implementation
> instead of answering them with more spec.

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
   detail, action(import|reauth|controlled_import), selected).
7. `api.md` — REST endpoint list (paths, verbs, request/response schema refs,
   error envelope): sources CRUD + test/discovery, per-backend source order
   (v2: `PUT /api/models/agents/<backend>/sources`, replacing the v1 global
   `PUT /api/models/priority`), agent mode switch, mappings CRUD, menu config,
   custom models, events feed, migration scan/apply,
   oauth start/status/submit/cancel.
8. `opencode-overlay.md` — generated provider entries (standard vendor ids +
   `custom/`), transport redirection, gateway token injection, serve
   config-hash restart rule; identifier stability invariant stated as a test
   requirement.
9. **v2 additions** (2026-07-29): `agent-chain.schema.json` (capability chain per
   (agent, model)), `probe-result.schema.json` (「试跑一次」), and
   `turn-provenance.schema.json` (per-turn model@source record). All three are
   read-only surfaces with no v1 implementation, so every field is required.

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
  quota-exhausted failover & recovery switchback, a per-backend source reorder
  takes effect next turn (v2; v1 read "priority reorder"), mapping applies to
  CC only, OpenCode identifier stability
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

## 8. Implementation acceptance criteria (review round 8, 2026-07-29)

Review round 8 of the spec-v2 PR (#1081) returned six findings, five P1 and one P2.
Rounds 6 and 7 had each answered a review by adding a spec section, and each new
section generated the next round's findings; round 8 was pre-committed to stop that.
So the split here is mechanical rather than editorial: **a finding that was two
statements in these docs contradicting each other was fixed in the spec** (AC-5, AC-6,
plus the retractions noted under AC-1 to AC-3), and **a finding that would need a new
route, a new enum value, or a product decision is recorded here verbatim** instead of
being designed inside a review reply by an author who wants the thread closed.

These are not suggestions and not backlog. Each is a test the implementing lane must
pass; the contracts stay at `contract_version: 2` and are not reopened to accommodate
them. Where an AC grows a vocabulary (AC-4) or adds a route (AC-3), that lands in the
implementing PR against the frozen contract, with the schema change in the same commit
as the test that needs it.

| AC | Sev | Finding | Surface | Owed by | Owner call needed |
| --- | --- | --- | --- | --- | --- |
| **AC-1** | P1 | Define provenance for Direct-mode turns | `turn-provenance.schema.json` | L2 (contract + route) with L7 scenario | no |
| **AC-2** | P1 | Reconcile irreversible native re-auth before returning failure | `api.md` | L1 (re-auth orchestration) + L4 (confirm copy) | **yes — product decision** |
| **AC-3** | P1 | Allow blocked sources to be re-tested after user action | `model-hub.md` | L2 (route + state clearing) with L7 scenario | no |
| **AC-4** | P2 | Represent canceled turns in provenance | `turn-provenance.schema.json` | L2 (contract) with L7 scenario | no |
| **AC-5** | P1 | Protect the menu-side model in deletion guards | `model-hub.md` | L2 (guard) with L7 scenario | no |
| **AC-6** | P1 | Resolve source events for every affected backend | `model-hub.md` | L2 (event fan-out) with L7 scenario | no |

**AC-2 blocks its lane until the owner answers.** The other five are implementable as
written. AC-1 and AC-4 both touch `turn-provenance.schema.json`, so one lane should own
them together — the vocabulary question 「which terminal states exist」 is the same
question twice.

### AC-1 — Define provenance for Direct-mode turns

Review round 8, P1, on `docs/plans/model-hub-contracts/turn-provenance.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864623). Verbatim:

> **Define provenance for Direct-mode turns**
>
> Direct remains the existing-user default and can run entirely from native configuration without any Model Hub `Source`, but a successful provenance record must contain a `src_*` identifier here. A successful post-feature Direct turn therefore cannot validate: the implementation must either fabricate a source or return `turn_not_found`, despite the contract promising provenance for each turn. Add an explicit Direct/no-source representation, or explicitly scope the endpoint and conversation affordance to Hub-mode turns.

**Spec action at round 8.** `model-hub.md` §4.5 「Turn provenance」 now states that the frozen interface covers Hub-mode turns and names this as AC-1; the schema is unchanged.

**Acceptance.** A successful Direct-mode turn is inspectable without any `Source` row existing: either the response validates against a documented no-source representation, or `GET …/provenance` answers a documented 「此回合无中枢记录」 error and the per-turn affordance renders that state. A test asserts a post-feature Direct turn never yields a payload that fails `turn-provenance.schema.json`, and never yields a fabricated `src_*` id.

### AC-2 — Reconcile irreversible native re-auth before returning failure

Review round 8, P1, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864639). Verbatim:

> **Reconcile irreversible native re-auth before returning failure**
>
> For `native_cli`, OAuth has already replaced the credential in the CLI-owned store before post-login discovery or the supply guard runs, and these lines acknowledge that the old login cannot be restored. Nevertheless, the shared repair contract later permits discovery failure or an elective-gap refusal while retaining the old `Source` row as though the prior supply were intact. The next native turn then uses the new CLI account while the persisted models and state still describe the old one. Native re-auth needs confirmation before the irreversible login, or it must commit and report the resulting gaps instead of using the rollback/refusal semantics of engine-owned credentials.

**Spec action at round 8.** `api.md` invariant 5 now scopes invariant 3's guarantee on this channel to its weak sense and forbids presenting a post-re-auth refusal as though the prior supply were intact; the remedy is deliberately not chosen there.

**Acceptance.** For a `native_cli` source, no path leaves the persisted `models`/`state` describing an account the CLI no longer holds. Whichever remedy the owner picks, a test drives a re-auth whose post-login discovery fails and asserts the observable end state matches the chosen semantics — a pre-login confirmation that can still abort, or a committed swap whose response reports the resulting gaps. Silent divergence between row and store fails the test.

### AC-3 — Allow blocked sources to be re-tested after user action

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864631). Verbatim:

> **Allow blocked sources to be re-tested after user action**
>
> A `balance_exhausted` source remains `needs_action` until a request observes the top-up, but this sentence's promised “next probe or turn” cannot perform that observation: the turn pipeline excludes every `needs_action` source as non-runnable, and the probe returns `probe_no_candidate` when the chain has no runnable member. If it is the only supplier, the source is therefore never called again and can never recover after the user tops up. Add a source-specific recovery probe that may test a blocked source after explicit user action, or otherwise define how the state is cleared safely.

**Spec action at round 8.** `model-hub.md` §4.5 retracts the claim that a topped-up balance is 「re-checked on the next probe or turn」 and states why both paths exclude the source; the recovery route is deliberately not frozen.

**Acceptance.** A source that is the ONLY supplier of a model, sitting in `balance_exhausted`, returns to service after the user tops up and takes the documented action — without deleting and re-adding the source, and without a credential change. Scenario `model_hub_blocked_source_recovery` (new) covers exactly that sequence and fails on the round-8 behaviour, where the state can never clear.

### AC-4 — Represent canceled turns in provenance

Review round 8, P2, on `docs/plans/model-hub-contracts/turn-provenance.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864628). Verbatim:

> **Represent canceled turns in provenance**
>
> Avibe has explicit Stop/cancel paths that settle an in-flight turn as canceled, including `SessionTurnManager.cancel`, but none of these four outcomes can represent that terminal state. It is not `served`, fallback exhaustion, a non-fallback adapter error, or a no-candidate turn; labeling it `failed_terminal` would also require inventing one of the four error reasons and possibly a source attempt. Since this contract says provenance always exists when a turn resolves, add a canceled outcome and define how any attempt that was interrupted is recorded.

**Spec action at round 8.** `model-hub.md` §4.5 names this as AC-4 alongside AC-1; `turn-provenance.schema.json` keeps its four outcomes at round 8.

**Acceptance.** A turn settled by Stop/cancel produces a provenance record that is not `served`, not `exhausted`, not `failed_terminal` and not `no_candidate` — i.e. the vocabulary grew — and an attempt that was in flight when the cancel landed is recorded without inventing a failure reason for it. Cancelling mid-stream must not produce a record that claims a source failed.

### AC-5 — Protect the menu-side model in deletion guards

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864615). Verbatim:

> **Protect the menu-side model in deletion guards**
>
> For a mapping such as `claude-opus-4-6 → glm-5.2`, this top-level spec says the protected set contains the mapping target, while `api.md`'s normative single-home definition requires the mapping's menu-side `builtin_id`. Following this wording tests a resolved ID in the menu namespace, so deletion can miss the actually selected built-in model, proceed without `force`, and interrupt the Agent on its next turn. Replace “mapping targets” with the menu-side models that own mapping rows.

**Spec action at round 8.** FIXED IN SPEC at round 8: `model-hub.md` now protects the menu-side `builtin_id` of every mapping row, matching `api.md`'s single definition of the guard. No implementation debt — this criterion exists so the fix cannot regress.

**Acceptance.** With a single mapping `claude-opus-4-6 → glm-5.2` and one source supplying the target, DELETE of that source is refused without `force`, and the confirm copy names the Agent that would break. A guard that compares resolved ids against menu identifiers matches nothing and must fail this test.

### AC-6 — Resolve source events for every affected backend

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864611). Verbatim:

> **Resolve source events for every affected backend**
>
> When a hub API-key source appears in multiple backends' `sources.order`, a failure discovered on Claude changes the source-global health for Codex as well. Expanding only the event's single backend therefore omits Codex-routed scopes from the required push, contradicting the preceding rule that source-scoped events affect every Agent whose order contains the source. Resolve source-scoped events from `from_source` across all backend orders (or emit one event per affected backend), while retaining backend-only expansion for `supply_interrupted`.

**Spec action at round 8.** FIXED IN SPEC at round 8: `model-hub.md` §4.5 now defines the affected backends as a SET — every backend whose `sources.order` contains `from_source` for source-scoped kinds, the named backend for `supply_interrupted`. No implementation debt; guards the fix.

**Acceptance.** One hub API-key source in both Claude's and Codex's `sources.order` fails once, and every scope routed to an Agent on EITHER backend receives exactly one push. A one-hop implementation notifies only the discovering backend's scopes and must fail this test.

## 7. Kickoff checklist (orchestrator)

- [ ] Owner approves this plan (lanes, sequencing, gates).
- [ ] design.pen saved; V4 frames re-exported into a stable reference dir.
- [ ] S1–S3 dispatched (S1/S3 codex, S2 research either).
- [ ] Contracts dir authored from S1 output; frozen and announced.
- [ ] L1/L2/L4 briefs written (scope, no-touch, contracts, review protocol) and dispatched.
