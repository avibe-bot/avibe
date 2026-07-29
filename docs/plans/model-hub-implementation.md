# Model Hub — Implementation Plan

Status: draft v1 · 2026-07-23 · follows the signed product spec
Spec (signed 2026-07-23): `docs/plans/model-hub.md`
Design source: `../avibe-docs/design.pen` frames `产品改造 V4 01r – 09`
Lane workflow standard: `~/vibe-remote-project/.agents/skills/pr-delivery-loop/SKILL.md`

> **Superseded for ordering (2026-07-29).** This document plans the **v1** build,
> which shipped dormant. Spec v2 moved the spend order from one global list to a
> per-backend ordered subset (`model-hub.md` §0, §4.2; contracts at
> `contract_version: 2`). Anything below that assumes a global priority list is
> historical. This document is not being rewritten in place, only annotated where
> it would otherwise contradict the frozen contracts. **§3 and §8 are the
> exceptions: both are v2-current and binding** — §3 now carries the v2 lane plan
> (approved 2026-07-29), which replaces the v1 lane split, and §8 the acceptance
> criteria the spec-v2 review loop handed to implementation instead of answering
> them with more spec.

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

## 3. Lanes (v2 plan, approved 2026-07-29)

The v1 lane split (L1–L7, built for the global priority list) is **superseded** and
lives in git history at `251f8e7b`. The lane IDs below are the v2 ones, and every
lane reference in §8 means one of these.

Dispatch preference (owner 2026-07-13): balance claude/codex; rigor-critical and
cryptography-adjacent backend → codex; product-voice / design-fidelity UI → claude.
Every brief cites: spec, this plan, the contracts dir, repo `AGENTS.md`,
`pr-delivery-loop` SKILL — all by absolute path — plus explicit file scope and
no-touch zones.

| Lane | Executor | Scope | Depends on |
| --- | --- | --- | --- |
| **L0 spec sync** | claude | `docs/plans/**` only: commit AC-14 to AC-21 into §8, apply the AC-14 to AC-17 record repairs, sync spec §4.5 and §8 to the push cut, write this lane plan | — |
| **L1 per-agent order core** | codex | `config/v2_config.py` per-backend `sources{policy, order}` + serializer completeness guards, resolver order resolution, `PUT /api/models/agents/<backend>/sources`, removal of the v1 global priority endpoint and `ModelHubConfig.priority_order` (which is what finally deletes the `priority.schema.json` tombstone, owner ruling #6), **the migration rewrite** (below), the eligibility and mode-projection contract fixes — **AC-19, AC-20, AC-21** — and **the coordinated `contract_version: 3` bump** every later lane rides | L0 |
| **L2 repair paths & guards** | codex | replacement invariants on `PUT …/credential` and `POST …/reauth`, confirm-before-irreversible native re-auth, one shared `would_interrupt` implementation behind DELETE and both repair routes, protected-set membership — **AC-2, AC-3, AC-5, AC-8, AC-12, AC-13** | L1 |
| **L3 provenance, probe & chain** | codex | turn-provenance write path + read route, probe route, chain projection, resolution-event emission and its record accuracy — **AC-1, AC-4, AC-7, AC-10, AC-18**, plus the record half of **AC-6, AC-9 and AC-11** | L1 |
| **L4 UI: overview & order** | claude | Models page overview, per-backend source order editor (跟随推荐 / 自定义), source rows, status pills — design frames V6 01–04 plus M01/M02; the OpenCode drawer follows the V6 02 pattern rather than inventing a third | L1 |
| **L5 UI: supply journeys** | claude | the `adopted_by` loop, confirm dialogs (delete, elective replacement, re-auth irreversibility), dry-run, chain preview, and wiring the in-turn error copy spec §4.5 makes normative; quota projection optional | L2, L3, L4 |
| **L6 integration close-out** | either | AC checkpoint across all of §8, scenario catalog completion, Incus regression evidence, user docs EN/ZH in `avibe-docs` | all |

**Migration belongs to L1** (orchestrator ruling, 07-29 — review round 1 of the L0 PR
found no lane owned it). `core/handlers/model_hub/migration.py` currently appends every
imported source to `updated.priority_order`, the exact field L1 deletes, so the two
changes are one change: the append is **deleted**, imported sources join each backend's
order through `follow`-mode auto-inclusion, and a `custom`-policy agent gets the
new-source hint instead of a silent insertion. Leaving it to a later lane would mean
merging a migration that writes a field that no longer exists.

**Merge order.** L0 first and alone — it unblocks every other lane. Then **L1**, which
owns the v3 contract bump. **L2 and L3 then run in parallel** off L1, with **L4**
starting alongside them. **L5** joins once L2/L3/L4 have landed the surfaces it wires.
**L6** finalizes.

Two known risks, stated so the lanes plan around them instead of discovering them:

1. **L2 and L3 both touch `core/handlers/model_hub/service.py`.** They are
   **symbol-level no-touch** to each other — L2 owns the repair and guard functions,
   L3 the provenance/probe/chain ones — and **L2 merges first**, so L3 rebases onto
   it rather than the reverse. A lane that needs a symbol the other owns routes
   through the orchestrator; it does not edit it.
2. **Docs-heavy PRs do not get reviewed by default.** The Codex bot skips docs-only
   diffs, so any lane whose PR is docs-shaped forces the review with `@codex review`
   and confirms the 👀 inside the trigger window, per `pr-delivery-loop`.

**Cut and deferred — no lane builds these:**

- **Proactive push machinery — CUT** (owner ruling 2026-07-29 10:54). No recipient
  resolution, no delivery layer, no scope fan-out, no home-scope fallback. Events are
  recorded; the failing turn and the 「模型」 page are the surfaces (spec §4.5).
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
- Only L1 performs the `contract_version` bump, and any later contract edit a lane's AC
  requires rides that version through the orchestrator — lanes still never edit
  `model-hub-contracts/**` on their own.
- L4 and L5 split `ui/src/components/settings/models/**` by subdirectory.

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

## 8. Implementation acceptance criteria (review rounds 8-12, 2026-07-29)

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
- **AC-6** reduces to **event-record accuracy** (which backends the record names),
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
and round 12's AC-18, AC-19, AC-20, AC-21 — plus **AC-3**, whose remedy adds a route
to the contracts even though its surface is the spec. **Lane L1 performs the bump**
(§3): it goes through the orchestrator, sets `contract_version` to **3**, updates the
mirror table, sweeps the delivery language the push cut stranded (「v3 handoff notes」
below), and states the client-visible delta in its PR description; every later lane's
contract edit rides that version, and bumps again only if it lands in a different
release. Several of these narrow what already validates — AC-10 and AC-18 add a
`pattern`, AC-19 closes an enum, AC-11 forbids `system` on a backend-scoped kind — so
a payload a v2 serializer emits today can stop validating, which is exactly the
client-visible delta the bump exists to announce. AC-20 is the opposite shape and is
deliberately **not required**, so no frozen example is invalidated by it. **AC-5,
AC-6 and AC-9 stay on v2**: their surface is the spec, and they change guard and
record semantics no contract file states. AC-6 stays there **because of its downgrade**
— the round-8 remedy would have needed an affected-backend field on
`resolution-event.schema.json` and pulled the criterion into v3; recording once and
deriving impact needs no field, so the frozen event shape is unchanged. **AC-14 to AC-17 are not a version event
either** — their surface is this document, and lane L0 discharged them before any
lane opened. AC-5 and AC-8 are the same guard from two sides, so a lane owning both
lands the `api.md` half inside the v3 bump and the spec half outside it.

| AC | Sev | Finding | Surface | Owed by | Owner call needed |
| --- | --- | --- | --- | --- | --- |
| **AC-1** | P1 | Define provenance for Direct-mode turns | `turn-provenance.schema.json` | L3 (contract + route) with L6 scenario | no |
| **AC-2** | P1 | Reconcile irreversible native re-auth before returning failure | `api.md` | L2 (re-auth orchestration) + L5 (confirm copy) | **settled 07-29 10:44 — confirm before the irreversible login** |
| **AC-3** | P1 | Allow blocked sources to be re-tested after user action | `model-hub.md` | L2 (route + state clearing) with L6 scenario | no |
| **AC-4** | P2 | Represent canceled turns in provenance | `turn-provenance.schema.json` | L3 (contract) with L6 scenario | no |
| **AC-5** | P1 | Protect the menu-side model in deletion guards | `model-hub.md` | L2 (guard) with L6 scenario | no |
| **AC-6** | P1 | Record a source event once; derive per-backend impact | `model-hub.md` | L3 (event record) with L6 scenario | **settled 07-29 — reduced to the record half at 10:54, then downgraded to a single unattributed record (orchestrator ruling, owner-vetoable)** |
| **AC-7** | P1 | Represent chain and probe for Direct-mode backends | `api.md` | L3 (route scoping) + L4 (drawer affordance) with L6 scenario | no |
| **AC-8** | P2 | Exclude disabled mapping rows from the protected set | `api.md` | L2 (guard) with L6 scenario | no |
| **AC-9** | P2 | Resolve affected Agents from their effective models | `model-hub.md` | L3 (record grain) + L2 (`SupplyGap.agents`) with L6 scenario | **settled 07-29 10:54 — the open half was the push policy, now cut** |
| **AC-10** | P2 | Constrain `ProbeResult.source_id` to the canonical `src_*` format | `probe-result.schema.json` | L3 (contract + serializer guard) | no |
| **AC-11** | P1 | Shape `system`-emitted source events from their source | `resolution-event.schema.json` | L3 (schema + emission) with L6 scenario | **settled 07-29 10:54 — reduced to the shape half** |
| **AC-12** | P2 | Reconcile a failed old-credential revocation | `api.md` | L2 (repair invariants) | no |
| **AC-13** | P1 | Give the `force` override a request contract on both repair routes | `api.md` | L2 (routes) + L5 (confirm dialog) | **settled 07-29 10:44 — via AC-2's shape; guard scoped to the Hub path by orchestrator ruling** |
| **AC-14** | P2 | Split AC-8's guard mutations across fresh fixtures | `model-hub-implementation.md` | L0 (applied), enforced by L2's test build | no |
| **AC-15** | P2 | Use a legal backend/model fixture for AC-9 | `model-hub-implementation.md` | L0 (applied), enforced by L3's test build | no |
| **AC-16** | P2 | Remove the nonexistent Agent from AC-5's assertion | `model-hub-implementation.md` | L0 (applied), enforced by L2's test build | no |
| **AC-17** | P2 | Make notification counts independent of the open recipient policy | `model-hub-implementation.md` | L0 (superseded — recipient policy cut) | **settled 07-29 10:54 — by removal of the policy** |
| **AC-18** | P2 | Constrain resolution-event source references | `resolution-event.schema.json` | L3 (contract + API-boundary guard) | no |
| **AC-19** | P2 | Close the eligibility reason-key vocabulary | `agent-supply.schema.json` | L1 (contract + locale keys) | no |
| **AC-20** | P2 | Enforce the hub-mode half of the mode invariant | `agent-supply.schema.json` | L1 (contract) | no |
| **AC-21** | P2 | Make the mirror registry encode its promised checks | `model-hub-contracts/README.md` | L1 (registry + checker) | no |

The last column takes exactly two values, and round 11's mechanical check reads it that
way: `no` means the criterion never turned on an owner decision, and a **settled** cell
names the ruling that closed one it did. The check — an AC whose criterion mentions an
owner decision must not read `no` — therefore still bites after a ruling lands, because
「settled」 is not 「no」. Six criteria carry a settled cell (AC-2, AC-6, AC-9, AC-11,
AC-13, AC-17); the other fifteen never depended on a call. Two of the six were settled
by an orchestrator ruling rather than an owner one (AC-6's downgrade and AC-13's guard
scoping); both are recorded as owner-vetoable and named as such in their criteria, which
is why they read 「settled」 and not `no`.

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

### v3 handoff notes — delivery language the push cut stranded in frozen files

Lane L0 could not fix these: editing a frozen file **is** the version event (above), so
a docs-only spec-sync PR that touched them would have published `contract_version: 3`
by accident. They are listed here for **L1** to sweep inside its coordinated bump. None
of them changes a shape — each is description or prose text asserting a delivery layer
that no longer exists, and the fix is to restate it as feed/UI semantics (spec §4.5).

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
| `api.md` | events feed row, mechanical-guard table | 「the IM push layer keys off `severity == "action_required"`」 and 「a push whose 「去处理」 lands on a row that renders nothing」 |

Two of these are **dangling cross-references** after L0's rewrite rather than merely
stale wording: `turn-provenance` and `resolution-event` both point at a §4.5 heading —
「Who receives an action-required push」 — that no longer exists. L1 repoints them at
§4.5's surfacing rule.

One entry changes meaning rather than just wording, per AC-6's downgrade — and with it
the **root the expansion starts from** (07-29, review round 2). The
`resolution-event.agent` description's 「consumers MUST expand: backend → Vibe Agents →
scopes」 was a *recipient* rule anchored at the event's own backend. Leaving live
derivation anchored there would rebuild the exact one-backend defect AC-6 exists to
remove: an event carrying `agent: "claude"` because Claude's turn discovered the failure
can never reach Codex, however many Codex chains the source sits in. L1 restates the
rule so that for a **source-scoped** kind the derivation starts from **`from_source`** —
a backend is affected when that source appears in the capability chain of one of its
protected models, evaluated against current per-agent orders (§4.5, and AC-6's
acceptance) — while `agent` stays what it says it is, the discovering context. The
`backend → Vibe Agents → scopes` hop keeps its original root only for **backend-scoped**
`supply_interrupted`, where the backend genuinely is the event's subject rather than a
consequence of it. No field is added to that schema (AC-6).

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

**Owner ruling (2026-07-29 10:44) — confirm before the irreversible act.** Of the two remedies, the owner picked the first: native re-auth shows an explicit irreversibility warning **before** the login starts — it replaces the current login immediately, there is no rollback, and a failed new login means re-authenticating the original account — and the flow can still be aborted at that point. Failure states then render honestly: the old login is gone, and a retry entry is offered rather than a screen implying the previous account is still there. **Hub-channel `api_key` replacement is unaffected** and stays transactional, carrying **no irreversibility warning**, because it is reversible. The exemption is from *that* confirmation only (07-29, review round 2): an elective replacement that would narrow supply still meets the `source_last_supplier` refusal and its explicit `force` override (AC-13). That one is the **conditional supply-gap** confirmation, and it is computable on this channel precisely because discovery precedes commit — the property native re-auth lacks. Reading 「no confirmation step」 as blanket is how L2/L5 would drop the force dialog. AC-13's `force` confirmation reuses this shape rather than inventing a second one.

**Acceptance.** For a `native_cli` source, no path leaves the persisted `models`/`state` describing an account the CLI no longer holds. A test drives a re-auth whose post-login discovery fails and asserts the ruled semantics: the irreversibility confirmation was presented before the login and could abort it, and after the failure the response reports the resulting gaps instead of presenting the prior supply as intact. The confirmation is **unconditional** — it does not consult a supply guard, which pre-login is uncomputable (AC-13). A second test asserts the Hub-channel `api_key` path shows no such **irreversibility** confirmation, and a third holds the guard that path does keep: an elective replacement of a healthy key that would narrow supply is still refused with `source_last_supplier` until `force` (AC-13). Skipping the unconditional warning is not skipping the conditional supply-gap confirmation. Silent divergence between row and store fails all three.

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

**Acceptance** (repaired 07-29, review round 12 — see AC-16). With a single mapping `claude-opus-4-6 → glm-5.2`, one source supplying the target, and **no Agent selecting either model**, DELETE of that source is refused without `force`, and the refusal names the affected pair with `SupplyGap.agents: []`. The empty list is the point: assigning an Agent here would protect the model through the Agent-selection term instead, and the test would stop isolating the mapping-namespace defect — it is also the exact case AC-9's recorded text depends on. Agent-facing confirmation copy is tested in its own fixture, where an Agent does select the model. A guard that compares resolved ids against menu identifiers matches nothing and must fail this test.

### AC-6 — Resolve source events for every affected backend

Review round 8, P1, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864611). Verbatim:

> **Resolve source events for every affected backend**
>
> When a hub API-key source appears in multiple backends' `sources.order`, a failure discovered on Claude changes the source-global health for Codex as well. Expanding only the event's single backend therefore omits Codex-routed scopes from the required push, contradicting the preceding rule that source-scoped events affect every Agent whose order contains the source. Resolve source-scoped events from `from_source` across all backend orders (or emit one event per affected backend), while retaining backend-only expansion for `supply_interrupted`.

**Spec action at round 8, narrowed at round 9, reduced at 10:54, DOWNGRADED 07-29 (orchestrator ruling — owner-vetoable).** The finding's remedy — expand the record across every affected backend — was built for a push that no longer exists, and carrying it into the record layer created a criterion no conforming record could satisfy: `resolution-event.schema.json` has no field for a backend SET (`agent` is a single enum), so closing it would have required a frozen-schema edit and moved this criterion into the v3 set. **The ruling is that the record layer stays single-grained.** A source-scoped event is recorded ONCE, unattributed: `agent` keeps its current semantics — the discovering context, or `system` — and the record makes no claim about which backends are affected. Per-backend impact is **derived live by the consumers** from current per-agent orders, using the (backend, model) chain test the supply guard already computes: the feed renders source events as unattributed lines, and the agent status pills evaluate the question at render time. Derivation is not a downgrade of the round-8/9 reasoning but its correct home — a set frozen into the record goes stale the first time a user reorders a backend, while the derived answer cannot. **No schema change, and this criterion stays on v2.**

**Acceptance** (rewritten 07-29 to the downgrade; the round-8 and round-9 wording it replaces is the delivery-era text). One hub API-key source that sits in both Claude's and Codex's `sources.order` **and supplies a protected model on each** fails once, and **exactly one source-state failure record is written** — no per-backend fan-out, no backend list on the record, `agent` naming only the discovering context. The count is over the failure record, not over the event stream (07-29, review round 2): when a fallback covers the failure, `resolution-event.schema.json:104` requires the pair 「one `switch`, info + one `needs_action`, action_required」, each true about its own subject, so the companion `switch` is conforming traffic that does not count toward this assertion — a test written as 「exactly one event total」 would reject the contract's own behaviour and pressure the implementation into suppressing the feed's switch line. Both backends' status surfaces nevertheless report the failure, computed from that single record against their current orders, and the negative case still holds through the derivation rather than through the record: the same source in Codex's order but in no Codex chain (a GLM-only key while Codex runs `gpt-5.6`) leaves Codex's surface unaffected. An implementation that emits one `needs_action` per affected backend fails the count assertion; one that renders only the discovering backend fails the derivation assertion.

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

**Acceptance** (repaired 07-29, review round 12 — see AC-14). Four **independent** fixtures, one per (route × `enabled`) combination, each built from the same shape: one backend, one mapping row `claude-opus-4-6 → glm-5.2`, `claude-opus-4-6` selected by no Agent and not the backend default, and one source supplying `glm-5.2`. With `enabled: false`: DELETE of that source succeeds without `force` (fixture 1), and an elective `PUT …/credential` onto a narrower key is not refused (fixture 2). With `enabled: true`: both refuse with `source_last_supplier` (fixtures 3 and 4). The fixtures cannot be phases of one run — the successful DELETE consumes the source the credential assertion needs, and running the narrowing PUT first mutates the capabilities the DELETE assertion is meant to inspect, so each assertion must start from the intended initial state. Together they fail an implementation that reads mapping rows without their flag and equally fail one that drops mapping protection altogether.

### AC-9 — Fan out source events only through Agents that use the affected model

Review round 10, P2, on `docs/plans/model-hub.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670099009). Verbatim:

> **Restrict source-event fan-out to Agents using the affected model**
>
> When a source appears only in the chain of an unassigned `menu.checked` entry, an unused mapping, or an unused backend default, this rule marks the backend affected; the preceding two-hop rule then notifies every scope routed to every enabled Agent on that backend. That recreates the irrelevant action-required pushes this round is intended to eliminate, because the protected-model union deliberately includes models with `SupplyGap.agents: []`. Resolve affected named Agents from their effective models and fan out only through those Agents, leaving menu-only failures in the feed/settings surface.

**Spec action at round 10, settled 07-29 10:54.** RECORDED, not fixed — and it was the round-9 narrowing one level deeper, on the hop round 9 did not touch. Round 9 narrowed hop 1 (which backends are affected) from `sources.order` membership to the chain of a protected model; this narrows hop 2 (which Agents inside an affected backend) to the Agents whose effective model is the one that lost supply. The second hop was too wide because the protected set is *deliberately* wider than the live selections — it protects a model the user ticked and assigned to nobody, which is right for refusing a delete and wrong for announcing an interruption, which is why `SupplyGap.agents` is allowed to be empty. **With push cut, the finding's delivery half is void and its grain half survives intact.** There is no recipient set to narrow; what remains is the distinction between 「a model some Agent actually runs lost supply」 and 「a ticked-but-unassigned menu model lost supply」, which the feed, the row's status pill and the agent-facing 需处理 state must still tell apart — a menu-only failure must not render an Agent as interrupted. Ruling #4 stands unchanged: `SupplyGap.agents` includes the Agents inheriting `agents.<backend>.default_model`, because they do use the model; it is guard and confirm payload, and now also the UI's answer to 「谁受影响」. The standing open decision this block used to defer to (zero-scope fallback) no longer exists.

**Acceptance** (delivery half deleted 07-29 10:54; fixture repaired per AC-15). Two cases from independent fixtures, not two phases on one (corrected 07-29, review round 11: a failed source stays `needs_action` until the user acts and its health is source-global, so round 10's 「fail X again」 produces no second transition to observe). The backend is **OpenCode with prefixed selections**, because a fixed-menu backend cannot own an OpenCode menu and a bare `gpt-5.6` is not a legal OpenCode selection under `api.md`'s identifier rules. Case A: one enabled Agent running `openai/gpt-5.6`, plus a menu model `zhipuai/glm-5.2` the user ticked and assigned to no Agent, supplied only by source X. X fails: the recorded gap resolves to **no** Agents (`SupplyGap.agents: []`), that Agent's `supply_status` stays `ok`, and the failure still appears in the 最近切换 feed and on the 「模型」 page. Case B: the same fixture with that Agent pointed at `zhipuai/glm-5.2` — from fresh state, or after X is explicitly repaired and recovered — and X fails: the gap names exactly that Agent and its `supply_status` becomes `interrupted`. An implementation that resolves affected Agents over every enabled Agent on the affected backend passes B and fails A.

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

**Acceptance** (delivery half deleted 07-29 10:54; the event-shape half is the whole criterion). Drive it from a declared non-turn path: `POST …/sources/<id>/test` re-discovers a source that is the last supplier of a protected model and finds it dead, producing a `needs_action` event with `agent: "system"` and `from_source` set to that source (corrected 07-29, review round 11: round 10 drove it from a Web-UI revocation, which `api.md` documents as an agent-scoped `supply_interrupted` with `from_source: null` and `reason: "no_enabled_source"` — a trigger that never emits the event under test, and `system` appears nowhere in that file). The record this path writes is **equal in kind, `reason`, `from_source` and `severity`** to the one the identical failure writes when discovered mid-turn — record equality between the two paths is the assertion, replacing round 10's recipient equality — so the consumers derive the same state either way and the feed and the 「模型」 page agree regardless of who noticed. Equality is asserted on the record's own fields, not on a backend set, per AC-6's downgrade. A `supply_interrupted` event carrying `agent: "system"` is rejected by the schema. An implementation that reads the current description literally records the re-discovery as affecting nothing and fails.

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

**Acceptance** (re-auth half settled 07-29 10:44 by AC-2's ruling; guard scope fixed 07-29 by the ruling above). An elective `PUT …/credential` onto a narrower key is refused with `source_last_supplier` + `would_interrupt`, and the identical request carrying the documented override commits and reports `interrupted_pairs` — with the override travelling through a declared request field, not a query string the contract never mentions. For re-auth, the assertion is the **unconditional** one: every `native_cli` re-auth presents AC-2's irreversibility confirmation before the login starts and can be aborted there, **regardless of what the new account will turn out to supply**, and no path reaches a completed OAuth flow that then needs a second round trip to confirm. A UI that must re-run the OAuth flow to confirm fails this test; so does one that lets the login complete and only then discovers it cannot commit; and so does one that tries to gate the confirmation on a pre-login supply computation, which cannot be performed.

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
>
> The fixture combines a bare `gpt-5.6` selection with an “OpenCode-menu” entry on the same backend, but `api.md`'s identifier rules require OpenCode selections to use prefixed `vendor/model` IDs, while a fixed-menu Codex backend cannot also own an OpenCode menu. Consequently Case B cannot point this Agent at the described ticked model without changing backend/menu semantics; define the fixture as OpenCode with a prefixed selection such as `openai/gpt-5.6`, or use a fixed-menu-only scenario.

**Disposition.** Repairs **AC-9's acceptance fixture**, applied above: one OpenCode backend with prefixed selections (`openai/gpt-5.6` running, `zhipuai/glm-5.2` ticked-but-unassigned). The previous fixture asked a fixed-menu backend to own an OpenCode menu, which `api.md`'s identifier rules forbid, so Case B could not be reached at all. AC-9's criterion and the round-9/round-10 narrowing rulings are unchanged. Independent of the 10:54 push cut — it repairs the fixture, which the surviving grain half still needs.

**Acceptance.** Contract/integration layer, owed by **L3** with the AC-9 tests it repairs. Every identifier in AC-9's fixture validates against `api.md`'s identifier rules for the backend that owns it, and Case B is constructible without changing backend or menu semantics mid-test. Fails today: `gpt-5.6` unprefixed on an OpenCode-menu backend is rejected by those rules, so the fixture cannot be built as written.

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

**Acceptance.** Documentation layer, discharged by **L0** (this PR). Mechanically checkable in two parts. First, no **Acceptance** paragraph in this section states what any scope, conversation or user receives: AC-6 asserts that exactly one unattributed failure record is written and that consumers derive impact from it, AC-9 the resolved Agent set and `supply_status`, AC-11 record equality between two emission paths — three assertions about stored state and its rendering, none about delivery. Second, no **Acceptance** paragraph defers to a policy `model-hub.md` leaves open, which is trivially satisfied now that §4.5 states no recipient policy at all. Where 「push」 or 「recipient」 still appears in those blocks it is in a verbatim finding or a dated 「what this used to say」 clause, never in the sentence a test is written from. Failed at the head this finding was filed against, where all three counted pushes.

### AC-18 — Constrain resolution-event source references

Review round 12, P2, on `docs/plans/model-hub-contracts/resolution-event.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273205). Verbatim:

> **Constrain resolution-event source references**
>
> `from_source` and `to_source` identify `Source` rows and the action-required path explicitly hands `from_source` to credential/reauth routes, but these fields accept any string rather than the canonical `^src_[a-z0-9]{8,}$` format enforced by `Source`, chains, and provenance. A serializer can therefore emit an event with `from_source: "direct"` that passes the contract but cannot be correlated with a source or opened by the remediation UI. Apply the canonical pattern to both string branches and add the same referential-existence guard required for probe source IDs.

**Disposition.** New criterion, and AC-10's defect in a second file — the same canonical id left unconstrained, found by the same reasoning. `^src_[a-z0-9]{8,}$` goes on the string branch of both `from_source` and `to_source`, with `null` retained on both (legitimately null for `supply_interrupted`); referential existence goes to the API boundary as in AC-10, because it sits outside draft-07 on the boundary this PR already declared. The existing examples (`src_claudepro1`, `src_anthkey01`) already satisfy the pattern, so no example is rewritten. **The push cut does not touch this** — the event is still recorded, and the feed's one-tap re-auth is exactly the consumer that needs `from_source` to name a real row. Frozen surface: joins the `contract_version: 3` set.

**Acceptance.** Contract layer, owed by **L3**. `{"from_source": "direct", …}` and `{"to_source": "cli-anthropic", …}` are both rejected by `resolution-event.schema.json`; `null` on either field still validates for `supply_interrupted`; every example in the file validates after the v3 bump; and **every non-null endpoint** must name an existing `Source` row — the server-side guard rejects a `switch` whose `from_source` is well-formed but unknown *and* one whose `to_source` is (07-29, review round 2: checking only the origin lets a `switch` pass with a destination the feed cannot resolve or open, which is the same defect one field over) — with the mechanical checker asserting both are declared in `api.md`'s guard table. Fails today: both fields accept any string.

### AC-19 — Close the eligibility reason-key vocabulary

Review round 12, P2, on `docs/plans/model-hub-contracts/agent-supply.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273208). Verbatim:

> **Close the eligibility reason-key vocabulary**
>
> When `eligible` is false, this branch accepts any nonempty `reason_key`, although the contract names three distinct remedies and the UI must resolve the value through its locale files. A typo or invented value therefore validates but leaves the ineligible row without translatable copy or an actionable remedy. Constrain this branch to the declared `subscription_wrong_client`, `opencode_api_key_only`, and `consent_required` keys, extending the enum alongside locale support when a new eligibility cause is introduced.

**Disposition.** New criterion. `enum: ["models.eligibility.subscription_wrong_client", "models.eligibility.opencode_api_key_only", "models.eligibility.consent_required"]` on the ineligible branch — **fully qualified**, because that is what the frozen contract actually emits (`agent-supply.schema.json:94`, `api.md:214`); the finding's bare short names are the causes, not the wire values, and an enum written from them would reject every conforming payload. With the extension rule — a new cause ships its enum member and its locale copy in the same change — stated where the enum lives. **Missed instance of a class this PR swept in round 3** (closed vocabularies), which is why it carries the extension rule rather than just the enum: an enum with no stated extension path is the reason the sweep left instances behind. Frozen surface: joins the v3 set.

**Acceptance.** Contract layer, owed by **L1**, which also carries the v3 bump. `{"eligible": false, "reason_key": "models.eligibility.subscription_wrong_clint"}` is rejected; so is the unqualified `"subscription_wrong_client"`; each of the three declared fully-qualified keys validates; and the UI locale files contain a key for each enum member, checked mechanically so the two cannot drift. Fails today: any nonempty string validates, so the typo above passes the contract and renders as a raw key.

### AC-20 — Enforce the hub-mode half of the mode invariant

Review round 12, P2, on `docs/plans/model-hub-contracts/agent-supply.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273210). Verbatim:

> **Enforce the hub-mode half of the mode invariant**
>
> The schema pins all Hub-only projections to null in Direct mode but never enforces the converse, so a response with `mode: "hub"` and present-but-null `selected_model_id`, `sources`, `supply_status`, and `model_supply` still validates even after the API-boundary presence checks land. Such a payload leaves the Hub drawer without an order or selected model and makes the chain/probe defaults unusable while claiming Hub mode. Add a Hub branch that constrains these fields to their non-null shapes; it can remain non-required here so the frozen v1 examples that omit the fields still validate.

**Disposition.** New criterion. A `hub` branch constraining **the four projections the finding names** — `selected_model_id`, `sources`, `supply_status`, `model_supply` — to their non-null shapes, **not required**, so the frozen v1 examples that omit them still validate. Exactly those four, and no more: `selected_by_agent` and `current` are declared `["string","null"]` / `["object","null"]` and are legitimately null in Hub mode (no Agent has selected, nothing is current), so pinning them non-null would reject conforming payloads — the invariant is about Hub projections that must exist for the drawer to function, not about every nullable field in the file. The four deliberately mode-independent fields (`mappings`, `menu`, `builtin_models`, `standard_vendors`) stay out of both branches. **Missed instance of the partial-predicate class**, also swept in round 3 — and it is the exact shape that sweep named: a `then` whose `properties` never bite because nothing requires the field. Note the interaction with the `required` guard in `api.md`'s mechanical-guard table: this branch is a **declared exception** to it, deliberately non-required, so the checker must recognise it as such rather than flag it. Frozen surface: joins the v3 set.

**Acceptance.** Contract layer, owed by **L1**. Every fixture below is built from an **otherwise-valid** Hub payload, varying only the projections under test (07-29, review round 2): `backend`, `mode` and `menu_kind` are the file's top-level `required` set, so a fixture that omits them is rejected today for a reason this branch has nothing to do with, and a test copied from it would pass vacuously while all four null projections stayed accepted. `{"backend": "claude", "mode": "hub", "menu_kind": "fixed", "selected_model_id": null, "sources": null, "supply_status": null, "model_supply": null}` is rejected, and rejected *by the Hub branch* — the same payload with the four projections carrying their non-null shapes validates, which is what proves the rejection came from the branch and not from a missing required field; the frozen v1 examples that omit those fields still validate; a Hub payload with `selected_by_agent: null` and `current: null` still validates, since those are not part of the branch; the four mode-independent fields validate identically under both modes; and the mechanical checker records this branch as a declared non-required exception instead of reporting it as a partial predicate. Fails today: that payload validates, and the Hub drawer receives it with no order and no selected model.

### AC-21 — Make the mirror registry encode its promised checks

Review round 12, P2, on `docs/plans/model-hub-contracts/README.md`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273212). Verbatim:

> **Make the mirror registry encode its promised checks**
>
> The table cannot drive two checks the surrounding text promises: M4 labels the non-self-healing reason-to-`detail_key` relation as `none` even though `api.md` requires a mechanically checked bijection, and M6 omits `resolution-event.agent` from its Mirrors cell even though lines 226-230 say that exact superset is checked as the home set plus `system`. A harness generated from these rows can therefore skip both relations while reporting the registry complete. Give M4 an executable bijection rule and list `resolution-event.agent` in M6 with its declared extra.

**Disposition.** New criterion, and the sharpest of the four contract findings: the registry exists so a harness can be *generated* from it, so a row that under-declares its relation is not a documentation nit — it silently removes a check while the registry reports itself complete. M4 gets an executable `bijection` rule naming both directions; M6's Mirrors cell gains `resolution-event.agent` with its declared extra `system` inline, so the row alone determines the check. The invariants themselves already hold in the schemas — only the registry under-declares them. Frozen surface: joins the v3 set.

**Acceptance.** Contract layer, owed by **L1**. A harness generated purely from the registry rows — with no hand-written supplements — runs both relations: the reason ↔ `detail_key` bijection in both directions, and `resolution-event.agent` ⊆ home set ∪ `{system}`. Deleting one `detail_key` from either side, or adding an undeclared value to `resolution-event.agent` — whose enum already holds four, `claude`/`codex`/`opencode`/`system`, so the test adds a fifth — is caught by that generated harness. Fails today: M4 reads `none` and M6 omits the field, so a faithful generator emits neither check and still reports the registry fully covered.

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
