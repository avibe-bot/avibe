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
| **L1 per-agent order core** | codex | `config/v2_config.py` per-backend `sources{policy, order}` + serializer completeness guards, resolver order resolution, `PUT /api/models/agents/<backend>/sources`, removal of the v1 global priority endpoint and `ModelHubConfig.priority_order` (which is what finally deletes the `priority.schema.json` tombstone, owner ruling #6), **the migration rewrite** (below), the eligibility and mode-projection contract fixes — **AC-19, AC-20, AC-21** — and **the coordinated `contract_version: 3` bump** every later lane rides. **The bump carries the whole v3 set, not just L1's own ACs** (§3 single-freeze ruling): every frozen-file edit any §8 criterion needs — L2's and L3's surfaces included — is authored here, so that after this lane merges no other lane edits `model-hub-contracts/**` at all | L0 |
| **L2 repair paths & guards** | codex | replacement invariants on `PUT …/credential` and `POST …/reauth`, confirm-before-irreversible native re-auth **and its `tests/scenarios/auth_setup/` closed-loop case** (below), one shared `would_interrupt` implementation behind DELETE and both repair routes, protected-set membership — **AC-2, AC-3, AC-5, AC-8, AC-12, AC-13**. Implements against L1's published v3; **edits no file under `model-hub-contracts/`** | L1 |
| **L3 provenance, probe & chain** | codex | turn-provenance write path + read route, probe route, chain projection, resolution-event emission and its record accuracy — **AC-1, AC-4, AC-7, AC-10, AC-18**, plus the record half of **AC-6, AC-9 and AC-11**, plus **emitting the in-turn error copy** spec §4.5 makes normative (below), plus **the turn-lifecycle seam** that makes successful and canceled turns reachable at all, and — per the 07-29 round-7 ruling, amended 14:04 and replaced 14:39 — the **launch env-build seam** (`modules/agents/model_hub.py`) and **gateway authorization path** (`core/handlers/model_hub/turn_gateway.py`) that carry the session-scoped gateway token and the **grain-honest attribution** it is allowed to record (07-29 14:39 ruling, below; backend adapters, wire formats and the codex restart fingerprint are explicitly out of scope). Implements against L1's published v3; **edits no file under `model-hub-contracts/`** | L1 |
| **L4 UI: overview & order** | claude | Models page overview, per-backend source order editor (跟随推荐 / 自定义), source rows, status pills — design frames V6 01–04 plus M01/M02; the OpenCode drawer follows the V6 02 pattern rather than inventing a third; **plus AC-9's page attribution — the consumer of L1's per-Agent projection, with its UI test** (07-29 14:39 ruling: the attribution is a *status* surface — what is broken right now — and L4 already owns every status surface on that page, while L5 owns journeys and reading a status is not one) | L1 |
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
there is no success-path copy to emit, every remaining form is on the failure path these
files already own, and L3's scope is unchanged. Recorded here because 「the success path
cannot reach the emitter」 is a fact a later lane will rediscover; it is a design
constraint that shaped the ruling, not an oversight to fix.

**L3 owns the turn-lifecycle seam (orchestrator ruling, 07-29 — review round 6).** The
block above settles the *copy*; this settles the *record*, and the two went opposite ways
because the push cut removed one and not the other. Provenance is owed for **every** turn,
including the ones that succeed — and a successful turn never visits `backend_failure.py`,
so nothing in L3's original scope could see it or carry the Avibe `turn_id`. L3's scope
therefore gains the module that owns the turn state machine: `SessionTurnManager`
(`core/session_turns.py` at the time of writing). **L3 verifies the exact seam at
implementation and records the file in its PR, escalating if it differs** — this plan
names the architecture, not a line number.

The binding constraint travels with it, and AC-1 and AC-4 both turn on it: **turn
completion and cancellation classification are read from the turn FSM's terminal states,
never inferred from transport.** A dropped connection and a user pressing Stop look
identical at the gateway, and only the FSM knows which one happened; an implementation
that guesses from the socket will mislabel one of them, which is precisely the record AC-4
exists to make trustworthy.

This is shared core code, so the edits are **additive hooks, coordinated at symbol level**:
L3 adds its call sites without reshaping the FSM, and nothing else in this batch touches
that file — which is what keeps a shared-core edit out of the lane-conflict class in the
first place.

**The FSM hook alone cannot correlate an attempt to a turn — correlation is established at
LAUNCH, and attribution is then recorded at the FINEST GRAIN the channel's process model
actually supports (orchestrator ruling, 07-29 14:39).** This **replaces** the 14:04 text,
which claimed turn-exact attribution uniformly; review round 8 proved that unimplementable
for codex. Review round 7 found the original gap:
`core/handlers/model_hub/turn_gateway.py` authorizes with a **per-backend** token map
(`self._tokens.get(backend)`, and `_gateway_credentials(self, backend)` in
`modules/agents/model_hub.py`), so two concurrent sessions on the same backend and model
are indistinguishable at the gateway — and AC-1's `failed_attempts` and AC-4's cancellation
classification both need that correlation to exist at all. **Per-backend tokens stay
rejected**: concurrent same-backend sessions are the norm in this product, not an edge
case.

The carrier is unchanged: **the gateway token is minted once per session.** The launch path
already mints `launch.gateway_token` (injected as `ANTHROPIC_AUTH_TOKEN` for claude and
`AVIBE_MODEL_HUB_TOKEN` for codex), so authorization itself carries the session identity,
and `turn-provenance`'s turn linkage is filled server-side at record time. What the 14:39
ruling changed is **what that identity is allowed to claim.**

**Why the uniform per-turn claim failed** (review round 8, P1 — verified in code by L0
before escalating, not relayed): codex's app-server is **shared per working directory**
(`self._transports[cwd]`, `_transport_locks[cwd]` in `modules/agents/codex/agent.py`), and
`ModelHubLaunch.fingerprint` (`modules/agents/model_hub.py:62`) hashes the gateway token
**into** the restart fingerprint — so `_get_or_create_transport` tears the shared process
down on any token change and invalidates every session bound to that cwd (`sessions_for_cwd`
→ `invalidate_thread`). A per-session token there either collides with the first session's
or thrashes everyone else's threads. Three remedies were weighed and **all three rejected**:
per-session transports / adapter surgery (a process-model change and resource multiplication
bought with debug-record precision — the machinery-for-marginal-value trade this batch has
been cutting all day); request-level identifiers (wire and contract surface for the same
marginal value); and lifting the token out of the fingerprint (**rejected hard** — the
shared transport would go on serving every session under the *first* session's token, which
is silent **misattribution**: the invented-state class we purge, and worse than no data).

**The rule that replaces it: record at the finest grain the channel supports, mark it
honestly, never guess.** It is uniform across backends, and it constrains the *record*, not
the runtime:

| Channel | Process model | Recorded grain |
| --- | --- | --- |
| claude | per-turn env injection | **turn-exact** — session token → that session's active turn from the FSM |
| codex | app-server shared per cwd | **turn-exact when the FSM shows EXACTLY ONE active turn for that (backend, cwd)** — a determination, not a guess; **coarser (workdir) carrying an explicit attribution marker** when concurrent turns make it ambiguous |
| opencode | to be verified | unchanged **verification mandate**, same pattern: session grain if server-per-session holds (the overlay path, `resolve_opencode_overlay_launch`), marked coarser grain otherwise, escalating with evidence |

**No adapter edits, no wire-format change, and no fingerprint change anywhere** — that
exclusion is the substance of the ruling rather than a leftover from the text it replaces.
**L3's scope keeps the two seams** it gained at round 7, both additive and symbol-
coordinated: the **launch env-build seam** (`modules/agents/model_hub.py`) and the **gateway
authorization path** (`core/handlers/model_hub/turn_gateway.py`), alongside the FSM hook.
Backend adapters and wire formats stay explicitly OUT of scope.

**This ruling does owe one contract term, and the evidence for it now exists** — superseding
the 14:04 text's 「adds no contract term」. `turn-provenance` gains an **attribution-grain
term** in v3: **L1 names and versions the vocabulary** (something on the order of `turn` |
`session` | `workdir`), and its whole purpose is that a coarse record says so *in the
record*, so no consumer can read a workdir-grain attempt as a turn-grain one. The
orchestrator has notified L1 directly; **L0 records the requirement, not the schema.** This
is the v4 deferral in the superseded text collapsing into v3 — correctly, because the
degraded record stopped being speculative the moment codex's shared transport was
confirmed: under concurrency it is that channel's ordinary behaviour.

The single-active-turn premise the FSM lookup rests on is **verified, not assumed** (L0,
07-29): `SessionTurnManager` keeps `active_turn_sinks` keyed by `session_key`,
`dispatch_turn` serializes streaming turns per session, and `register_turn_sink` explicitly
refuses a duplicate registration rather than clobbering the in-flight one. **One
implementation caveat for L3**, since the plan names architecture and not line numbers:
confirm the lookup keys on the same identity the gateway can produce from the token
(`session_key` and the Avibe session id are not interchangeable), and note that the manager
already carries a `turn_token` correlating emits to an exact turn — if the active-turn
lookup turns out to need it, reuse it rather than minting a second correlator.

**Classification is unchanged by any of this.** The token establishes *what an attempt may
be attributed to*; completion and cancellation still classify from the FSM's terminal
states per the round-6 constraint above. AC-4's two legs stand exactly as written — they
now pin the **unambiguous** fixture — and the 14:39 ruling adds a **third, anti-guess**
leg to AC-1 and AC-4 alike: under deliberate same-cwd concurrency, attempts are recorded
at the coarse grain and **never attributed to a turn**.

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
   through the orchestrator; it does not edit it.
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
- **L3 is the only lane outside `core/handlers/model_hub/**`** (07-29, review round 3):
  the in-turn copy puts it in `core/backend_failure.py`, `core/message_output.py` and
  `vibe/i18n/{en,zh}.json`. No other v2 lane touches those files, so the zone is a
  statement rather than a negotiation — but L3's brief names them explicitly, because a
  lane that discovers it needs a file outside its declared scope is a lane that has
  already collided with someone.
- **v3 is frozen ONCE, and L1 authors all of it** (orchestrator ruling, 07-29, review
  round 4 — this **supersedes** the round-1 declination of a full v3 pre-freeze, which
  answered 「who branches first」 without answering 「what happens when L2's and L3's own
  ACs edit frozen files after v3 has published」). Round 4 named the consequence: a bump
  at L1 followed by later contract edits inside downstream lanes would make
  `contract_version: 3` denote several mutually incompatible contract states, which is
  precisely what `model-hub-contracts/README.md`'s freeze protocol forbids. So **L1's
  coordinated v3 commit expands to carry every contract shape change any AC in §8
  requires** — L2's and L3's surfaces included, plus AC-11's nullable model for
  source-scoped system events and AC-9's per-Agent read projection. **After L1 merges,
  contracts are READ-ONLY for every remaining lane.** A shape mismatch a lane can prove
  *from its implementation* escalates to the orchestrator for a targeted **v4** with that
  evidence attached; it is never an in-lane edit, and never a silent reinterpretation of
  v3. The merge order is unchanged (L0 → L1 → {L2 ‖ L3 ‖ L4} → L5 → L6); what changed is
  how much L1 carries.
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
- **AC-6** reduces to **event-record accuracy** — which, after the downgrade recorded
  under AC-6 itself, means exactly ONE unattributed record carrying **no backend list**,
  the affected backends being derived from current orders and chains (corrected 07-29,
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
and round 12's AC-18, AC-19, AC-20, AC-21 — plus **AC-3**, whose remedy adds a route
to the contracts even though its surface is the spec, and **AC-9**, whose acceptance
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
| **AC-3** | P1 | Allow blocked sources to be re-tested after user action | `model-hub.md` (+ new route in `api.md`) | **L1 v3** (route contract) + L2 (route + state clearing) + **L5 (the Models-page action that invokes it)** with L6 scenario | **settled 07-29 — the scenario drives the page action, not the route** |
| **AC-4** | P2 | Represent canceled turns in provenance | `turn-provenance.schema.json` | **L1 v3** (contract) + L3 (emission, via the turn-lifecycle seam) with L6 scenario | **settled 07-29 — cancellation is FSM truth, never transport inference** |
| **AC-5** | P1 | Protect the menu-side model in deletion guards | `model-hub.md` | L2 (guard) with L6 scenario — **stays on v2** | no |
| **AC-6** | P1 | Record a source event once; derive per-backend impact | `model-hub.md` | L3 (event record) with L6 scenario — **stays on v2** | **settled 07-29 — reduced to the record half at 10:54, then downgraded to a single unattributed record (orchestrator ruling, owner-vetoable)** |
| **AC-7** | P1 | Represent chain and probe for Direct-mode backends | `api.md` | **L1 v3** (route scoping + Direct-mode payload) + L3 (route) + L4 (drawer affordance) with L6 scenario | no |
| **AC-8** | P2 | Exclude disabled mapping rows from the protected set | `api.md` | **L1 v3** (`api.md` half) + L2 (guard) with L6 scenario | no |
| **AC-9** | P2 | Resolve affected Agents from their effective models | `model-hub.md` + `api.md` | **L1 v3** (per-Agent read projection) + L3 (record grain) + L2 (`SupplyGap.agents`) + **L4 (the page attribution that consumes the projection, and its UI test)** with L6 scenario | **settled 07-29 14:39 — the attribution consumer is L4's; the earlier cell assigned no UI lane and left L6 to discover a failure nobody could fix** |
| **AC-10** | P2 | Constrain `ProbeResult.source_id` to the canonical `src_*` format | `probe-result.schema.json` | **L1 v3** (contract) + L3 (serializer guard) | no |
| **AC-11** | P1 | Shape `system`-emitted source events from their source | `resolution-event.schema.json` | **L1 v3** (schema, incl. the nullable `model_id`) + L3 (emission) with L6 scenario | **settled 07-29 10:54 — reduced to the shape half** |
| **AC-12** | P2 | Reconcile a failed old-credential revocation | `api.md` | **L1 v3** (credential invariants) + L2 (repair implementation) | no |
| **AC-13** | P1 | Give the `force` override a request contract on both repair routes | `api.md` | **L1 v3** (request contract) + L2 (routes) + L5 (confirm dialog) | **settled 07-29 10:44 — via AC-2's shape; guard scoped to the Hub path by orchestrator ruling** |
| **AC-14** | P2 | Split AC-8's guard mutations across fresh fixtures | `model-hub-implementation.md` | L0 (applied), enforced by L2's test build | no |
| **AC-15** | P2 | Use a legal backend/model fixture for AC-9 | `model-hub-implementation.md` | L0 (applied), enforced by L3's test build | no |
| **AC-16** | P2 | Remove the nonexistent Agent from AC-5's assertion | `model-hub-implementation.md` | L0 (applied), enforced by L2's test build | no |
| **AC-17** | P2 | Make notification counts independent of the open recipient policy | `model-hub-implementation.md` | L0 (superseded — recipient policy cut) | **settled 07-29 10:54 — by removal of the policy** |
| **AC-18** | P2 | Constrain resolution-event source references | `resolution-event.schema.json` | **L1 v3** (contract) + L3 (API-boundary guard) | no |
| **AC-19** | P2 | Close the eligibility reason-key vocabulary | `agent-supply.schema.json` | **L1 v3** (contract + locale keys) | no |
| **AC-20** | P2 | Enforce the hub-mode half of the mode invariant | `agent-supply.schema.json` | **L1 v3** (contract) | no |
| **AC-21** | P2 | Make the mirror registry encode its promised checks | `model-hub-contracts/README.md` | **L1 v3** (registry + checker) | no |

**Read the 「Owed by」 column as contract-then-implementation** (07-29, review round 5).
Where a cell begins **L1 v3**, the frozen-file edit that criterion needs is authored by
L1 inside the single coordinated bump, and the lanes named after it implement and test
against the result — they do not edit `model-hub-contracts/**` themselves. Only AC-5 and
AC-6 carry no L1 term, because their surface is the spec and neither changes a contract
shape. This is the §3 single-freeze ruling applied row by row, and it is what makes
「zero contract edits after L1 merges」 checkable rather than aspirational: any later
lane's PR that touches a file under `model-hub-contracts/` contradicts a cell in this
table and escalates for a targeted v4 instead.

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
| `api.md` | events feed row (`:43`) | 「the IM push layer keys off `severity == "action_required"`」, 「recipients are resolved at push time … against the live routing table」 |
| `api.md` | mechanical-guard table (`:755`) | 「a push whose 「去处理」 lands on a row that renders nothing」 |
| `api.md` | `POST /sources/<id>/test` recover rule (`:559`) | 「severity promotion can push it to the user as news」 — the stated *reason* the unconditional `recover` emit is wrong, so L1 must restate the reason (a 「已恢复」 line in a feed with nothing to recover from) rather than delete the sentence and lose the rule |
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
rounds, so **L1 must not treat this table as the sweep.** The sweep is mechanical and its
result, not this table, is the completion condition of the v3 bump. Run both from
`docs/plans/model-hub-contracts/`:

```
grep -rn 'Who receives an action-required push' .
grep -rniE 'push|IM 推送|notif|alert|interrupt the user|proactive' .
grep -rnE 'IM surfaces?|IM 平台|conversation surface' .
```

The first must return **zero** hits when v3 is frozen — the heading no longer exists. The
**third was added in review round 7**, because the second misses a whole class: it greps
`IM 推送`, not bare `IM`, so `README.md:258` — which still lists `turn-provenance.schema.json`
as consumed by 「IM surfaces (per-turn detail)」 — survives it untouched, and would have
frozen into v3 a consumer promise the Web-only ruling (AC-1, above) means no lane in this
batch owes. That is a **surface-ownership** promise rather than delivery semantics, which
is why the delivery-semantics pattern never saw it. Its two current hits are both seeds:
that README row, and `turn-provenance.schema.json:5`'s 「the conversation surface reveals on
demand」. **Both are now outright wrong rather than merely loose** (owner ruling 07-29 14:03):
provenance has no chat surface at all, so v3 must delete the consumer promise and the
rendering phrasing rather than narrow either to Web. The second grep returns a superset
that still needs judgement: `interrupted`/`supply_interrupted`
are state names the design keeps, and 「never pushes」 phrasing may legitimately survive as
a statement of the cut. What must not survive is any passage a later lane could read as
licence to build proactive delivery, or any pointer into a deleted heading. The rows above
are the seeds — the ones already read and characterised — and they save L1 the reading,
not the sweeping.

**L1's v3 mandate includes purging ALL delivery-semantics text the sweep returns**, not
just the rows that also change a shape. The distinction matters because these files are
the normative contract: a frozen description that still says 「always ELIGIBLE for a
proactive push」 will be read by L2/L3 as licence to build the push the owner cut, and a
dangling §4.5 reference sends the reader to a heading that no longer exists — a
transcription defect either way. None of them changes a *shape*; the fix is to restate
each as feed/UI semantics (spec §4.5), inside the same coordinated bump.

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

**Acceptance.** A successful Direct-mode turn is inspectable **through the contracted route** without any `Source` row existing: either the response validates against a documented no-source representation, or `GET …/provenance` answers a documented 「此回合无中枢记录」 error. **The tests assert route behavior, not UI** (owner ruling 07-29 14:03 — see below). A test asserts a post-feature Direct turn never yields a payload that fails `turn-provenance.schema.json`, and never yields a fabricated `src_*` id. **Attempt-to-turn attribution is not assumed, and where it cannot be had it is not invented:** `failed_attempts` is only meaningful if each attempt is known to belong to *this* turn, which the session-scoped gateway token establishes at launch (07-29 round-7 ruling as amended 14:04 and **replaced 14:39**, §3) — token → session → that session's active turn from the FSM — rather than at the gateway, where a per-backend token cannot tell two concurrent sessions apart. **The claim is grain-honest, not uniform**: turn-exact on claude, turn-exact on codex only while the FSM shows exactly one active turn for that (backend, cwd), and **coarse (workdir) with an explicit attribution marker** otherwise. So this criterion carries an **anti-guess leg**: under deliberate same-cwd concurrency, a test asserts the attempts are recorded at the coarse grain and **never attributed to a turn** — an implementation that picks one of the two live turns fails it, which is the whole point, since guessed attribution is worse than recorded coarseness. The turn linkage is filled server-side at record time and **no request-path contract field carries it**; what L1's v3 does owe is the **attribution-grain term** on `turn-provenance` (vocabulary L1's to name — see §3), because a coarse record has to say so in the record.

**Which surface — none. Owner ruling 07-29 14:03, superseding the round-6 orchestrator ruling.** The round-6 ruling scoped 「the per-turn affordance」 to the Web conversation surface's turn detail, owned by L5, with IM deferred to v2.1. **The owner has now cut the surface entirely, on both.** The reasoning is a product one and it moves the whole criterion: users should be **unaware of supply machinery**, so provenance inspection is a **debug affordance, not a user feature**, and a per-turn detail hanging off the transcript is exactly the machinery leaking into the surface it should stay behind.

So **AC-1 reduces to record + API truth**: the record is written, the contracted route answers, and Direct mode is represented **honestly at the contract level** — 「no hub record」 becomes an explicit API-level semantic instead of a rendered conversation state. Every conversation-rendering assertion is deleted from this criterion, and **no UI lane owes anything for AC-1**. What survives untouched is everything that is not inspection: the record, the route, the in-turn **error** copy (error UX, the surfacing mechanism the owner endorsed), and L1's v3 contract terms — a debug record still has to be **true** data, which is also why the session-scoped correlation ruling below stands.

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

**Spec action at round 8.** `model-hub.md` §4.5 retracts the claim that a topped-up balance is 「re-checked on the next probe or turn」 and states why both paths exclude the source; the recovery route is deliberately not frozen.

**Acceptance.** A source that is the ONLY supplier of a model, sitting in `balance_exhausted`, returns to service after the user tops up and takes the documented action — without deleting and re-adding the source, and without a credential change. Scenario `model_hub_blocked_source_recovery` (new) covers exactly that sequence and fails on the round-8 behaviour, where the state can never clear. **The scenario drives the Models-page action, not the route** (07-29 ruling): calling the recovery endpoint directly would pass while the button that reaches it does not exist, which is the exact gap this criterion was reopened to close — 「the user tops up and takes the documented action」 is only true if the action is reachable from the page.

### AC-4 — Represent canceled turns in provenance

Review round 8, P2, on `docs/plans/model-hub-contracts/turn-provenance.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3669864628). Verbatim:

> **Represent canceled turns in provenance**
>
> Avibe has explicit Stop/cancel paths that settle an in-flight turn as canceled, including `SessionTurnManager.cancel`, but none of these four outcomes can represent that terminal state. It is not `served`, fallback exhaustion, a non-fallback adapter error, or a no-candidate turn; labeling it `failed_terminal` would also require inventing one of the four error reasons and possibly a source attempt. Since this contract says provenance always exists when a turn resolves, add a canceled outcome and define how any attempt that was interrupted is recorded.

**Spec action at round 8.** `model-hub.md` §4.5 names this as AC-4 alongside AC-1; `turn-provenance.schema.json` keeps its four outcomes at round 8.

**Acceptance.** A turn settled by Stop/cancel produces a provenance record that is not `served`, not `exhausted`, not `failed_terminal` and not `no_candidate` — i.e. the vocabulary grew — and an attempt that was in flight when the cancel landed is recorded without inventing a failure reason for it. Cancelling mid-stream must not produce a record that claims a source failed. **The classification comes from the turn FSM's terminal state** (07-29 ruling, §3): a test drives a real Stop through `SessionTurnManager` and asserts the canceled outcome, and a second drives a dropped connection on an otherwise identical turn and asserts it is *not* recorded as canceled. Both legs are required, because an implementation that infers cancellation from the transport passes the first and fails the second — and it is the second that decides whether this record can be trusted. **Both legs presuppose that the in-flight attempt can be tied to the turn at all** — which is what the session-scoped gateway token plus the FSM's active-turn lookup delivers (07-29 round-7 ruling as amended 14:04 and **replaced 14:39**, §3). Without it, a concurrent same-backend turn's attempt can be recorded against the canceled turn and the second leg passes for the wrong reason — so **both legs run on the unambiguous fixture**, one active turn for that (backend, cwd), where the attribution is exact. A **third leg** covers the case that fixture excludes (07-29 14:39): two turns deliberately concurrent in the same cwd, one canceled, and the interrupted attempt is recorded at the **coarse grain, marked as such, and attributed to neither turn**. It fails an implementation that resolves the ambiguity by picking — which would let a cancellation record blame an attempt that belonged to the turn still running. The classification itself is unchanged: still FSM terminal state, never transport.

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

**Spec action at round 8, narrowed at round 9, reduced at 10:54, DOWNGRADED 07-29 (orchestrator ruling — owner-vetoable).** The finding's remedy — expand the record across every affected backend — was built for a push that no longer exists, and carrying it into the record layer created a criterion no conforming record could satisfy: `resolution-event.schema.json` has no field for a backend SET (`agent` is a single enum), so closing it would have required a frozen-schema edit and moved this criterion into the v3 set. **The ruling is that the record layer stays single-grained.** A source-scoped event is recorded ONCE, unattributed: `agent` keeps its current semantics — the discovering context, or `system` — and the record makes no claim about which backends are affected. Per-backend impact is **derived live by the consumers** from current per-agent orders, using the (backend, model) chain test the supply guard already computes: the feed renders source events as unattributed lines, and the agent status pills evaluate the question at render time. Derivation is not a downgrade of the round-8/9 reasoning but its correct home — a set frozen into the record goes stale the first time a user reorders a backend, while the derived answer cannot. **No schema change, and this criterion stays on v2.**

**Acceptance** (rewritten 07-29 to the downgrade; the round-8 and round-9 wording it replaces is the delivery-era text). One hub API-key source that sits in both Claude's and Codex's `sources.order` **and supplies a protected model on each** fails once, and **exactly one source-state failure record is written** — no per-backend fan-out, no backend list on the record, `agent` naming only the discovering context. The count is over the failure record, not over the event stream (07-29, review round 2): when a fallback covers the failure, `resolution-event.schema.json:104` requires the pair 「one `switch`, info + one `needs_action`, action_required」, each true about its own subject, so the companion `switch` is conforming traffic that does not count toward this assertion — a test written as 「exactly one event total」 would reject the contract's own behaviour and pressure the implementation into suppressing the feed's switch line. Both backends' status surfaces nevertheless report the failure — **computed from the source's CURRENT blocking state against each backend's current order and chains, never from the retained record** (corrected 07-29, review round 8: the earlier 「computed from that single record against their current orders」 wording put the normative sentence in direct conflict with the derivation-input paragraph below, and an implementer following this line alone could pass the failure leg while leaving both backends stuck at 需处理 until the record aged out) — and the negative case still holds through the derivation rather than through the record: the same source in Codex's order but in no Codex chain (a GLM-only key while Codex runs `gpt-5.6`) leaves Codex's surface unaffected. An implementation that emits one `needs_action` per affected backend fails the count assertion; one that renders only the discovering backend fails the derivation assertion.

**The derivation input is live state, never the retained record (07-29, review round 6).**
The paragraph above says the surfaces are 「computed from that single record against their
current orders」, which read literally licenses exactly the sticky status §4.5 forbids: a
source that recovers on its own — a cooldown lapsing, a quota window resetting — would
stay marked 需处理 on both surfaces until the record aged out, because the record is what
was queried. It is the wrong input. A record answers 「what happened」 and belongs to the
最近切换 feed; a status answers 「what is true now」 and reads the source's **current**
blocking state against each backend's **current** order and chains (spec §4.5, and
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

**Acceptance** (delivery half deleted 07-29 10:54; fixture repaired per AC-15). Two cases from independent fixtures, not two phases on one (corrected 07-29, review round 11: a failed source stays `needs_action` until the user acts and its health is source-global, so round 10's 「fail X again」 produces no second transition to observe). The backend is **OpenCode with prefixed selections**, because a fixed-menu backend cannot own an OpenCode menu and a bare `gpt-5.6` is not a legal OpenCode selection under `api.md`'s identifier rules. **The assertions are on the live projection, not on the record** (07-29, review round 3): `SupplyGap` is contracted as a **mutation-refusal** payload (`api.md`, the DELETE/PUT guard responses), and `resolution-event.schema.json` carries no gap field, so 「the recorded gap names Agent Y」 asserts a shape no source-failure record has — the same record-vs-derivation confusion AC-6 was downgraded to remove. What a source failure produces is one unattributed record; who it is *about* is read from `agent-supply`'s per-Agent `supply_status` and the 「模型」 page's attribution. Case A: one enabled Agent running `openai/gpt-5.6`, plus a menu model `zhipuai/glm-5.2` the user ticked and assigned to no Agent, supplied only by source X. X fails: that Agent's `supply_status` stays `ok`, the 「模型」 page attributes the failure to the **menu model and no Agent**, and the failure still appears in the 最近切换 feed. Case B: the same fixture with that Agent pointed at `zhipuai/glm-5.2` — from fresh state, or after X is explicitly repaired and recovered — and X fails: that Agent's `supply_status` becomes `interrupted` and the page names exactly that Agent. `SupplyGap.agents` is asserted where it is actually returned — **AC-5's DELETE refusal**, which is the contracted home of the empty-list case and already carries that assertion. An implementation that resolves affected Agents over every enabled Agent on the affected backend passes B and fails A.

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

**Acceptance** (delivery half deleted 07-29 10:54; the event-shape half is the whole criterion). Drive it from a declared non-turn path: `POST …/sources/<id>/test` re-discovers a source that is the last supplier of a protected model and finds it dead, producing a `needs_action` event with `agent: "system"` and `from_source` set to that source (corrected 07-29, review round 11: round 10 drove it from a Web-UI revocation, which `api.md` documents as an agent-scoped `supply_interrupted` with `from_source: null` and `reason: "no_enabled_source"` — a trigger that never emits the event under test, and `system` appears nowhere in that file). The record this path writes is **equal in kind, `reason`, `from_source` and `severity`** to the one the identical failure writes when discovered mid-turn — record equality between the two paths is the assertion, replacing round 10's recipient equality — so the consumers derive the same state either way and the feed and the 「模型」 page agree regardless of who noticed. Equality is asserted on the record's own fields, not on a backend set, per AC-6's downgrade. A `supply_interrupted` event carrying `agent: "system"` is rejected by the schema. An implementation that reads the current description literally records the re-discovery as affecting nothing and fails.

**Remedy surface, added 07-29 review round 5 — L1's coordinated v3.** The fixture above is
unbuildable against the frozen shape, and this is the defect rather than the fixture: `POST
…/sources/<id>/test` takes **no model input** — it re-probes a source — while
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

**Acceptance.** Documentation layer, discharged by **L0** (this PR). Mechanically checkable in two parts. First, no **Acceptance** paragraph in this section asserts **proactive conversation delivery** — that is, a message arriving in a scope or conversation that the user did not act to produce: AC-6 asserts that exactly one unattributed failure record is written and that consumers derive impact from it, AC-9 the resolved Agent set and `supply_status`, AC-11 record equality between two emission paths — three assertions about stored state and its rendering, none about delivery. The check is deliberately narrowed to *proactive* delivery (07-29, review round 5): a blanket 「no Acceptance paragraph states what a user receives」 is false on its face and always was, because AC-2 requires a confirmation to be **presented**, AC-7 constrains drawer affordances, AC-9 specifies page rendering and AC-18 feed rendering. Those are pull surfaces — the user opened the page, or ran the turn — and they are exactly what the push cut left as the only way supply problems reach anyone, so a check that forbade them would forbid the design. What the cut removed is the unrequested interruption, and that is the only thing this check may look for. Second, no **Acceptance** paragraph defers to a policy `model-hub.md` leaves open, which is trivially satisfied now that §4.5 states no recipient policy at all. Where 「push」 or 「recipient」 still appears in those blocks it is in a verbatim finding or a dated 「what this used to say」 clause, never in the sentence a test is written from. Failed at the head this finding was filed against, where all three counted pushes. **The runtime half of the cut is not here**: this check only proves §8 stopped *asserting* delivery, and §3's 「Silence needs a negative assertion」 rule assigns L6 the scenario that proves the implementation stopped *doing* it — L6 checkpoints both.

### AC-18 — Constrain resolution-event source references

Review round 12, P2, on `docs/plans/model-hub-contracts/resolution-event.schema.json`, [thread](https://github.com/avibe-bot/avibe/pull/1081#discussion_r3670273205). Verbatim:

> **Constrain resolution-event source references**
>
> `from_source` and `to_source` identify `Source` rows and the action-required path explicitly hands `from_source` to credential/reauth routes, but these fields accept any string rather than the canonical `^src_[a-z0-9]{8,}$` format enforced by `Source`, chains, and provenance. A serializer can therefore emit an event with `from_source: "direct"` that passes the contract but cannot be correlated with a source or opened by the remediation UI. Apply the canonical pattern to both string branches and add the same referential-existence guard required for probe source IDs.

**Disposition.** New criterion, and AC-10's defect in a second file — the same canonical id left unconstrained, found by the same reasoning. `^src_[a-z0-9]{8,}$` goes on the string branch of both `from_source` and `to_source`, with `null` retained on both (legitimately null for `supply_interrupted`); referential existence goes to the API boundary as in AC-10, because it sits outside draft-07 on the boundary this PR already declared. The existing examples (`src_claudepro1`, `src_anthkey01`) already satisfy the pattern, so no example is rewritten. **The push cut does not touch this** — the event is still recorded, and the feed's one-tap re-auth is exactly the consumer that needs `from_source` to name a real row. Frozen surface: joins the `contract_version: 3` set.

**Acceptance.** Contract layer, owed by **L1's v3** (the pattern constraint on both fields), with **L3** owning the API-boundary guard and the emission — corrected 07-29, review round 6, to match the AC table and §3's single-freeze rule; the earlier 「owed by L3」 wording predated that rule and would have had L3 editing `resolution-event.schema.json` after the freeze. `{"from_source": "direct", …}` and `{"to_source": "cli-anthropic", …}` are both rejected by `resolution-event.schema.json`; `null` on either field still validates for `supply_interrupted`; every example in the file validates after the v3 bump; and **every non-null endpoint must name an existing `Source` row at emission time** — the server-side guard rejects a `switch` whose `from_source` is well-formed but unknown *and* one whose `to_source` is (07-29, review round 2: checking only the origin lets a `switch` pass with a destination the feed cannot resolve or open, which is the same defect one field over) — with the mechanical checker asserting both are declared in `api.md`'s guard table. **The guard is on emission, not on retention** (07-29, review round 3): sources are deletable, `force=true` deletes one the guard would otherwise refuse, and the events feed is bounded by count rather than by source lifetime, so a retained row can legitimately reference a source that no longer exists — re-validating retained rows would make a legal DELETE retroactively invalidate the record of what happened before it, or push the implementation into rewriting history to keep the feed valid. Neither the record nor the schema changes when a source is deleted; what changes is the **rendering**: the feed resolves the id, and on a miss renders the remembered display name as inert text (「relay.example（已删除）」) with the one-tap re-auth affordance withdrawn, because there is nothing left to re-auth. Acceptance covers both halves — emission of an unknown id is rejected, and a feed rendered after its referenced source is deleted still loads, still shows the line, and offers no dead action. Fails today: both fields accept any string.

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

**Disposition.** New criterion, and the sharpest of the four contract findings: the registry exists so a harness can be *generated* from it, so a row that under-declares its relation is not a documentation nit — it silently removes a check while the registry reports itself complete. M4 gets an executable `bijection` rule naming both directions — **and the pairs themselves, in a machine-readable field** (07-29, review round 3). 「Bijection」 alone constrains only the two *sets*: any permutation of the reasons against the `detail_key`s satisfies it, so a mapping that renders 「订阅客户端不符」 for a consent failure passes a generated harness while being wrong in exactly the way the mirror exists to catch. The registry therefore carries the ordered pairs (a `pairs` list of `[reason, detail_key]`, one line per member), and 「bijection」 becomes what the harness *asserts about that list* — total, injective, and pairwise-matching the two files — rather than the whole of what the row says. M6's Mirrors cell gains `resolution-event.agent` with its declared extra `system` inline, so the row alone determines the check. The invariants themselves already hold in the schemas — only the registry under-declares them. Frozen surface: joins the v3 set.

**Acceptance.** Contract layer, owed by **L1**. A harness generated purely from the registry rows — with no hand-written supplements — runs both relations: the reason ↔ `detail_key` bijection in both directions, and `resolution-event.agent` ⊆ home set ∪ `{system}`. Three mutations are caught by that generated harness: deleting one `detail_key` from either side; **swapping two `detail_key`s between reasons in one file while leaving M4's `pairs` list alone** (07-29, review round 3 — the case a set-only bijection cannot see, and the one that ships wrong copy to a real user); and adding an undeclared value to `resolution-event.agent`, whose enum already holds four, `claude`/`codex`/`opencode`/`system`, so the test adds a fifth. Fails today: M4 reads `none` and M6 omits the field, so a faithful generator emits neither check and still reports the registry fully covered.

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
