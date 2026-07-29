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

## 8. Implementation acceptance criteria (review rounds 8-11, 2026-07-29)

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
`model-hub-contracts/`** — AC-1, AC-2, AC-4, AC-7, AC-8, AC-10, AC-11, AC-12, AC-13 —
plus **AC-3**, whose remedy adds a route to the contracts even though its surface is
the spec. The first of them to land goes through the orchestrator, bumps
`contract_version` to **3**, updates the mirror table, and states the client-visible
delta in its PR description; the others ride that version if they land in the same
release and bump again if they do not. Two of the round-10 three narrow what already
validates — AC-10 adds a `pattern`, AC-11 forbids `system` on a backend-scoped kind —
so a payload a v2 serializer emits today can stop validating, which is exactly the
client-visible delta the bump exists to announce. **AC-5, AC-6 and AC-9 stay on v2**:
their surface is the spec, and they change guard and fan-out semantics no contract
file states. AC-5 and AC-8 are the same guard from two sides, so a lane owning both
lands the `api.md` half inside the v3 bump and the spec half outside it.

| AC | Sev | Finding | Surface | Owed by | Owner call needed |
| --- | --- | --- | --- | --- | --- |
| **AC-1** | P1 | Define provenance for Direct-mode turns | `turn-provenance.schema.json` | L2 (contract + route) with L7 scenario | no |
| **AC-2** | P1 | Reconcile irreversible native re-auth before returning failure | `api.md` | L1 (re-auth orchestration) + L4 (confirm copy) | **yes — product decision** |
| **AC-3** | P1 | Allow blocked sources to be re-tested after user action | `model-hub.md` | L2 (route + state clearing) with L7 scenario | no |
| **AC-4** | P2 | Represent canceled turns in provenance | `turn-provenance.schema.json` | L2 (contract) with L7 scenario | no |
| **AC-5** | P1 | Protect the menu-side model in deletion guards | `model-hub.md` | L2 (guard) with L7 scenario | no |
| **AC-6** | P1 | Resolve source events for every affected backend | `model-hub.md` | L2 (event fan-out) with L7 scenario | no |
| **AC-7** | P1 | Represent chain and probe for Direct-mode backends | `api.md` | L2 (route scoping) + L4 (drawer affordance) with L7 scenario | no |
| **AC-8** | P2 | Exclude disabled mapping rows from the protected set | `api.md` | L2 (guard) with L7 scenario | no |
| **AC-9** | P2 | Fan out source events only through Agents that use the affected model | `model-hub.md` | L2 (event fan-out) with L7 scenario | **yes — zero-scope fallback** |
| **AC-10** | P2 | Constrain `ProbeResult.source_id` to the canonical `src_*` format | `probe-result.schema.json` | L2 (contract + serializer guard) | no |
| **AC-11** | P1 | Route `system`-emitted source events from their source | `resolution-event.schema.json` | L2 (schema + delivery) with L7 scenario | no |
| **AC-12** | P2 | Reconcile a failed old-credential revocation | `api.md` | L2 (repair invariants) | no |
| **AC-13** | P1 | Give the `force` override a request contract on both repair routes | `api.md` | L2 (routes) + L4 (confirm dialog) with L7 scenario | **yes — via AC-2** |

**Three ACs wait on an owner call, not on engineering** (07-29, review round 11): AC-2
outright, plus the half of AC-13 and of AC-9 that each one's own criterion already says
a decision governs. AC-13's re-auth confirmation is one of the two answers AC-2's
decision picks between; AC-9's narrowing makes a menu-only failure resolve to zero
scopes by construction, so the standing zero-scope fallback decision settles whether
that failure is announced anywhere outside the feed and the 「模型」 page. Both keep an
implementable half that can land first — AC-13's declared override field on
`PUT …/credential`, and AC-9's fan-out narrowing itself — so only AC-2 blocks a lane
outright. The other ten are implementable as written. Four groups must be owned
together, because each is one question on more than one surface and independent answers
will disagree:

- **AC-1 + AC-4** — both touch `turn-provenance.schema.json`; 「which terminal states
  exist」 is the same question twice.
- **AC-1 + AC-7** — what a Hub-shaped contract says about a backend that is not on Hub.
- **AC-6 + AC-9 + AC-11** — all three are the action-required delivery rule: which
  backends are affected, which Agents inside them are, and who receives an event no
  backend emitted. AC-6 narrows the first hop, AC-9 the second, AC-11 fixes the case
  where the emitter is `system`.
- **AC-2 + AC-13** — one repair path. AC-13's 「resumable confirmation for re-auth
  completion」 is exactly where AC-2's 「pre-login confirmation that can still abort」
  would live, and AC-2's owner decision picks between them.

**AC-5 + AC-8** are also the same guard from two sides — the namespace of the protected
set and its membership — so whoever fixes one should re-read the other's test.

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

**Spec action at round 8, narrowed at round 9.** FIXED IN SPEC: `model-hub.md` §4.5 defines the affected backends as a SET — for source-scoped kinds, every backend the failed source actually supplies; for `supply_interrupted`, the named backend. Round 8 wrote 「actually supplies」 as `sources.order` membership, and round 9 corrected it to the (backend, model) chain grain the supply guard already uses, because a `follow` order holds every eligible source: an API-key source that no chain on that backend can run would otherwise interrupt its scopes. No implementation debt; guards the fix.

**Acceptance.** One hub API-key source that sits in both Claude's and Codex's `sources.order` **and supplies a protected model on each** fails once: every scope routed to an Agent on EITHER backend receives exactly one push. A one-hop implementation notifies only the discovering backend's scopes and must fail this test. The round-9 half is the negative case — the same source in Codex's order but in no Codex chain (a GLM-only key while Codex runs `gpt-5.6`) must produce NO push to Codex-routed scopes, which a raw-order implementation fails.

### AC-7 — Represent chain and probe for Direct-mode backends

Review round 9, P1, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669986813). Verbatim:

> **Scope chain and probe endpoints away from Direct mode**
>
> Existing users remain in Direct mode, where `sources` and `selected_model_id` are null and there is no Model Hub source order, yet these endpoints are declared without a mode restriction. A chain request must therefore return an empty `interrupted` chain for a model the native CLI can run, while a probe has neither a default model nor the required `src_*` identity for a valid `ProbeResult`. Define a Direct-specific representation or error and keep the chain/probe affordances from presenting Hub starvation for Direct backends.

**Spec action at round 9.** Half fixed, half recorded. `api.md` now scopes both route rows to `mode: hub` and says why an empty chain is the wrong answer rather than a harmless one: `chain: []` means 「Hub has nothing that can serve this」, which is a false alarm about a backend whose native CLI is running that model fine. What Direct returns *instead* is a shape decision — a documented `direct_mode` error the drawer renders as 「该后端未接入模型中心」, or a mode-specific payload naming the native model with no chain — and this document chooses neither, because either choice edits `api.md` and belongs to the lane that also answers AC-1. Whichever is chosen publishes `contract_version: 3` per the versioning rule above.

**Acceptance.** With `mode: direct`: a chain request for the model that backend is actually running returns the chosen Direct representation and never `ok: true, chain: []`; a probe request returns the same representation and never a `ProbeResult` with a fabricated `source_id`; and the agent drawer offers neither 「试跑一次」 nor a chain view for that backend. An implementation that leaves the Hub-shaped 200 in place fails the first two, and a UI that keeps the affordances and renders the refusal as 中断 fails the third.

### AC-8 — Exclude disabled mapping rows from the protected set

Review round 10, P2, on `docs/plans/model-hub-contracts/api.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099008). Verbatim:

> **Ignore disabled mappings when building the protected set**
>
> When a persisted mapping has `enabled: false`, the resolver treats the built-in model as identity (`core/handlers/model_hub/resolver.py` checks `item.enabled`), but this definition protects every mapping row regardless of that flag. If the disabled row's model is not otherwise selected, deleting or rotating its only source will be refused with `source_last_supplier` even though no live selection would break; only enabled mapping rows should contribute through this term.

**Spec action at round 10.** RECORDED, not fixed. The premise checks out on all three sides: `agent-supply.schema.json` requires `enabled` on every mapping row and its frozen example carries `enabled: false`; `resolver.py` applies a row only when `item.enabled`; and term 2 of the protected set says 「every model that owns a mapping row」 with no filter. **The direction is not open, though** — the same paragraph already rejects over-protection in its own words: treating models nobody selected as protected 「trains the user to pass `force=true` reflexively and hollows out the whole guard」. A disabled row is exactly that case, so this is the guard's own principle applied to the term it was written next to.

**Acceptance.** One backend, one mapping row `claude-opus-4-6 → glm-5.2` with `enabled: false`, `claude-opus-4-6` selected by no Agent and not the backend default, and one source supplying `glm-5.2`. DELETE of that source succeeds **without** `force`, and an elective `PUT …/credential` onto a narrower key is not refused. Flipping the row to `enabled: true` must make both refuse with `source_last_supplier` — the same test run twice, which fails an implementation that reads mapping rows without their flag and equally fails one that drops mapping protection altogether.

### AC-9 — Fan out source events only through Agents that use the affected model

Review round 10, P2, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099009). Verbatim:

> **Restrict source-event fan-out to Agents using the affected model**
>
> When a source appears only in the chain of an unassigned `menu.checked` entry, an unused mapping, or an unused backend default, this rule marks the backend affected; the preceding two-hop rule then notifies every scope routed to every enabled Agent on that backend. That recreates the irrelevant action-required pushes this round is intended to eliminate, because the protected-model union deliberately includes models with `SupplyGap.agents: []`. Resolve affected named Agents from their effective models and fan out only through those Agents, leaving menu-only failures in the feed/settings surface.

**Spec action at round 10.** RECORDED, not fixed — and it is the round-9 narrowing one level deeper, on the hop round 9 did not touch. Round 9 narrowed hop 1 (which backends are affected) from `sources.order` membership to the chain of a protected model; this narrows hop 2 (which Agents inside an affected backend) to the Agents whose effective model is the one that lost supply. The second hop is still too wide because the protected set is *deliberately* wider than the live selections — it protects a model the user ticked and assigned to nobody, which is right for refusing a delete and wrong for sending a push, which is why `SupplyGap.agents` is allowed to be empty. **This reverses nothing already ruled:** the two hops still run from every affected backend, and `SupplyGap.agents` still includes the Agents that inherit the backend default — those Agents do use the model, so they stay recipients. **It does interact with the standing open decision:** once the recipient set is narrowed this way, a menu-only failure resolves to zero scopes by construction, so whichever way the owner settles the zero-scope fallback decides whether it is announced anywhere outside the feed and the 「模型」 page. Implement the two together or they will disagree.

**Acceptance.** Two cases from independent fixtures, not two phases on one (corrected 07-29, review round 11: a failed source stays `needs_action` until the user acts and its health is source-global, so round 10's 「fail X again」 produces no second transition to observe). Case A: a backend with one enabled Agent running `gpt-5.6`, plus an OpenCode-menu model the user ticked and assigned to no Agent, supplied only by source X. X fails: the scope routed to that Agent receives **no** action-required push, while the failure still appears in the feed and on the 「模型」 page. Case B: the same fixture with that Agent pointed at the ticked model — from fresh state, or after X is explicitly repaired and recovered — and X fails: that scope receives exactly one push. An implementation that fans out over every enabled Agent on the affected backend passes B and fails A.

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

**Spec action at round 10.** RECORDED, not fixed, and this is the one round-10 finding where the two texts are flatly opposed. The schema says `system` 「expands to no Agent and therefore to no recipient」; §4.5 says 「a Web UI mutation and a mid-turn failure resolve their recipients identically」 and derives a source-scoped kind's affected backends from the failed source. A key the user revokes on the settings page is source-scoped with a `system` emitter, so the two rules disagree about whether its `needs_action` push is delivered — and the schema's reading is the one that drops it. §4.5 is the normative side: `agent` was never the addressing key (round 7 made it a backend identifier consumers must expand), and the zero-recipient sentence is right only for the case it was written for — an event with no affected source, such as a manual recovery that changed nothing about supply.

**Acceptance.** Drive it from a declared non-turn path: `POST …/sources/<id>/test` re-discovers a source that is the last supplier of a protected model and finds it dead, producing a `needs_action` event with `agent: "system"` and `from_source` set to that source (corrected 07-29, review round 11: round 10 drove it from a Web-UI revocation, which `api.md` documents as an agent-scoped `supply_interrupted` with `from_source: null` and `reason: "no_enabled_source"` — a trigger that never emits the event under test, and `system` appears nowhere in that file). Every scope routed to an Agent that uses the affected model receives exactly one action-required push — the same recipients and the same count as when the identical failure is discovered mid-turn, and that equality is the assertion. A `supply_interrupted` event carrying `agent: "system"` is rejected by the schema. An implementation that reads the current description literally delivers zero pushes for the re-discovery case and fails.

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

**Spec action at round 10.** RECORDED, not fixed. Verified against the route table: `PUT …/sources/<id>/credential` declares `{key}`, `POST …/sources/<id>/reauth` declares no request body, and `GET …/oauth/status/<flow_id>` is a poll. The elective branch nevertheless refuses 「unless `force=true`」 using DELETE's refusal shape — a shape DELETE can carry because DELETE takes a query parameter and these two routes have nowhere to put one. The key half is a one-field addition. **The re-auth half is the hard one, and it is not a naming problem:** by the time the refusal is computed the user's login has already been replaced, so 「retry with `force=true`」 cannot mean 「send the request again」 — re-running OAuth on `native_cli` is the irreversible act AC-2 is about. It needs a confirmation that resumes the completed flow, which is why this criterion and AC-2 belong to one lane: AC-2's 「pre-login confirmation that can still abort」 and this criterion's 「resumable confirmation after the fact」 are two answers to one question, and the owner's AC-2 decision picks which one gets built.

**Acceptance.** An elective `PUT …/credential` onto a narrower key is refused with `source_last_supplier` + `would_interrupt`, and the identical request carrying the documented override commits and reports `interrupted_pairs` — with the override travelling through a declared request field, not a query string the contract never mentions. For re-auth, a completed flow whose guard refuses can be confirmed and committed without a second OAuth round trip, or is refused before the login it cannot undo, per AC-2's answer. A UI that must re-run the OAuth flow to confirm fails this test, and so does one that cannot confirm at all.

## 7. Kickoff checklist (orchestrator)

- [ ] Owner approves this plan (lanes, sequencing, gates).
- [ ] design.pen saved; V4 frames re-exported into a stable reference dir.
- [ ] S1–S3 dispatched (S1/S3 codex, S2 research either).
- [ ] Contracts dir authored from S1 output; frozen and announced.
- [ ] L1/L2/L4 briefs written (scope, no-touch, contracts, review protocol) and dispatched.
