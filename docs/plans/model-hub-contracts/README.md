# Model Hub — Interface Contracts

Status: **FROZEN v2** · 2026-07-29 · change only via orchestrator
Supersedes FROZEN v1 (2026-07-23 10:45) outright — no back-compat, no migration shims.
Derived from: spec v2 (`../model-hub.md`), implementation plan
(`../model-hub-implementation.md`, v1 — a v2 lane plan is cut separately),
spike S1 engine survey (`docs/plans/model-hub-engine-survey.md`),
spike S2 ToS review (`docs/plans/model-hub-tos-review.md`).

## v2 ruling (owner, 2026-07-28/29)

**Ordering is a consumption property, not a supply property.** The single global
priority list was a product-model error: a source is an asset the user owns, and
how eagerly to spend it is a decision each agent backend makes for itself. So the
spend order moves onto the per-backend supply record — each backend owns an
**ordered subset** of the sources it is eligible for, plus a policy (`follow` =
跟随推荐, server-computed and auto-joining; `custom` = 自定义, user-owned and frozen).

v2 replaces v1 outright because the feature never GA'd (`VIBE_MODEL_HUB_ENABLED`
dormant by default, every backend defaults `direct`, PR #1019). Old config keys are
dropped on load rather than translated. Also folded in: server-authoritative
eligibility, a `needs_action` state class, per-agent `supply_status`, the capability
chain per (agent, model), a dry-run probe, a turn-provenance read contract, and
`adopted_by` on source create.

Cooldown and health stay **source-global**, deliberately: quota exhaustion and
network reachability are properties of the account, not of the agent that happened
to touch it (`core/handlers/model_hub/service.py::_cooldown`).

## Resolved decision, carried forward from v1 (owner, 2026-07-23 10:33)

**Hybrid supply by default + consent-gated experimental hub-held subscriptions.**
Subscription sources default `supply_channel: "native_cli"` (per-turn channel
dispatch; the CLI's own sanctioned OAuth burns the quota); api_key sources are
`"hub"`. Additionally, subscription login INTO the engine (hub-held, incl.
Claude) ships as an **experimental feature behind
`subscription_hub_experimental`** with explicit ban-risk consent (copy: S2 §9)
and per-source opt-in — never enabled silently, always visibly marked. The
`allowed_origins`-style client binding applies to native_cli sources
(sanctioned client only) and to any experimental hub-held subscription.

Everything else reflects S1 findings: runtime-declared OAuth presentation
(S1 gap ③), adapter-owned redacted resolution events with the engine usage feed
disabled (S1 gap ② — the feed leaks inbound keys), model provenance + cooldown
fields, and a standalone managed-dependency manifest/status contract. The v1
freeze item "authoritative ordered priority" survives in v2 as **authoritative
ordered per-backend subset**: the server always re-echoes the full canonical
order after any mutation; clients never compute partial reorders.

## Freeze protocol

- The orchestrator commits these files with message
  `docs(model-hub): freeze interface contracts v2` and announces the commit SHA
  in every lane brief. From then on: lanes cite, never edit; changes go through
  the orchestrator and bump `contract_version`.
- Contract tests (implementation plan §5) validate both directions against
  these schemas.

## Required-vs-optional discipline (v2)

Contracts land before implementation, so v2 fields describe a destination the
shipped v1 serializers have not reached yet. The rule that keeps that honest:

- **New contracts are fully required.** `agent-chain.schema.json`,
  `probe-result.schema.json` and `turn-provenance.schema.json` describe surfaces
  with no v1 implementation, so **every** field is in `required` — including
  nullable ones such as `resolved_model_id`, `error` and `via_mapping`, where the
  type allows `null` but omission is not permitted. That is the point: a client
  reading these never has to distinguish "absent" from "null", and a server that
  cannot compute a value has to say so explicitly.
- **Fields grafted onto a live v1 serializer are optional here and REQUIRED at the
  API boundary once that serializer lands them.** That covers
  `agent-supply.sources` / `supply_status` / `model_supply` /
  `selected_model_id`, `source.created_at`, `source.usage.projected_exhaust_at`
  and `resolution-event.severity`. Each such property says so in its own
  `description`; the implementation PR flips it to `required` in the same change
  that makes it emit.
- **Request shapes are not response shapes.** `PUT …/agents/<backend>/sources`
  takes `order` only under `policy: "custom"`, while the response object requires
  both fields — spelled out in `api.md` rather than forced through one schema, so a
  client never has to send an ignored field to express 跟随推荐.
- **Prefer an unwritable contradiction to a validated one** (added 07-29, review
  round 2). Where an invariant can be designed out of the shape, it is: the served
  attempt in `turn-provenance` lives in its own field instead of being flagged inside
  the attempt array, so "two winners", "the winner is not last" and "the summary names
  a source the array never mentions" stop being things a validator or a test has to
  catch. Where the shape cannot carry it, a draft-07 conditional does —
  `needs_action` requiring `detail_key`, `supply_interrupted` pinning `severity` and
  nulling both source endpoints, `outcome` pinning which of `served` /
  `failed_attempts` may be populated. Prose invariants are the last resort, and any
  that remain name the assertion their implementing lane owes: this lane runs the
  contract tests without editing them, so a new semantic test belongs to the
  serializer PR that makes the payload real, not here.
- **Round 3 (07-29) applied that rule to the gaps round 2 left in prose.** The
  pattern it kept finding was the same one twice: a constraint stated in a
  `description` while the shape still permitted its negation. Now conditionals —
  `kind: needs_action` pinning `severity: action_required` and its four causes
  (a revoked key that validates as `info` is an alert the user silently never
  gets); `reachable: true` pinning non-null `source_id` / `model_id` /
  `via_mapping` / `latency_ms` and a null `error` (「答了，但不知道是谁答的，另附失败原因」
  was a valid probe); `outcome: failed_terminal` and the `terminal_error` slot
  beside `served`; every `outcome` branch now pinning the slots it *forbids*, not
  just the ones it uses. Two overlapping-vocabulary cases got a **normative
  precedence** instead, since no shape can order them: the three
  `supply_interrupted` reasons, and `interrupted` before `waiting`.
- **A predicate has exactly one home** (added 07-29, round 3). Round 2 restated the
  `waiting`/`interrupted` split in three files and got it wrong in two — 「每个成员都
  需要用户处理」 instead of 「至少一个」 — which made a mixed chain match no value and
  suppressed the action-required push. Likewise the 跟随推荐 recommendation rule,
  paraphrased in `agent-supply.policy` in a way that excluded the hub-held
  subscription the spec admits. Both now point at spec §4.5 / §4.2 and paraphrase
  nothing. A determinism rule that two files describe differently is not one.
- **Rounds 4–5 (07-29) closed the shape that was generating the findings.** Round 4
  finished what round 3 started: every prose invariant that could become a conditional
  became one, some forty of them. Round 5 then found that the *form* of those
  conditionals was itself a finding generator, and the mechanism is worth naming
  because it is not obvious. Two properties make a one-directional conditional
  self-replicating: `if A then B` always leaves `if B then A?` reviewable, and draft-07
  `then: {properties: {…}}` **never enforces presence** — `properties` validates only
  fields that are actually there, so a conditional constraining an optional field is
  silently vacuous exactly when the field is missing. Nine of round 5's sixteen
  findings were byproducts of that shape rather than of the design underneath it. So
  the fix form is TOTAL, and it is the standard for anything added later: closed enums
  instead of prefix patterns (`.+` after a trusted prefix is a naming convention, not a
  redaction boundary — it happily matches `models.source.cooldown.network: <raw
  upstream body>`); biconditionals instead of implications, or an explicit `$comment`
  saying why the converse is deliberately left open; and `required` beside every
  non-null `then` constraint. A mechanical presence check guards that last rule, with
  two declared exceptions where a frozen v1 example forbids `required` — both
  fail-safe absences, and both listed in `api.md` → mechanical guards, never silent.
  Deliberate non-constraints go in a `$comment` so the next reader can tell a
  judgement from an oversight.
- **Round 6 (07-29) was a different class: contract completeness.** The form fix
  held — none of round 6's findings was a strictness byproduct. What it found instead
  was prose promising an outcome the payload had no field to carry, and one sequence
  the frozen adapter could not execute. Three shapes closed it. `SupplyGap`
  (`{backend, model_id, agents}`) gives the (backend, model) starvation set a
  declared form, under two keys that differ only in mood — `interrupted_pairs` for
  what did happen, `would_interrupt` for what a refusal predicts — because one
  predicate has one home even when two routes state it. `OAuthFlow.intent` makes the
  terminal-success response a function of the payload rather than of which button the
  client remembers pressing, so the add-source loop closes identically through both
  creation paths. And `resolution-event.reason: unclassified_error` closes a taxonomy
  hole: `state.status: error` is a blocker (`agent-chain` has always counted it with
  `needs_action`), so an interruption caused by the last runnable source erroring had
  a push obligation and no representable event — the emitter had to misclassify or
  stay silent. It became a fifth CAUSE rather than a ninth kind, which keeps the
  reason↔`detail_key` correspondence a bijection and leaves the standing 「four causes
  do not imply `kind: needs_action`」 ruling untouched. The sequencing finding is the
  one worth remembering as a rule: **a documented sequence must be executable through
  the frozen adapter surface.** Credential replacement now provisions a *replacement*
  ref, discovers through it, and only then swaps atomically — because
  `discover_models` consumes a ref only `provision_credential` can mint, so "validate
  before you provision" was unimplementable no matter how much better it reads.
- **Round 7 (07-29) found that the sequence itself was the generator, and retired
  it.** Round 6's closing sentence above — the provision → discover → swap ordering —
  was correct and still bred four findings, because a step list invites the reader to
  ask what happens between any two steps, and every answer is a new step. So the
  credential section no longer states steps: it states six INVARIANTS against one
  named commit point (atomicity, no revoked-credential window, failure preserves the
  prior state, guard before commit, per-channel executability, recovery symmetry), and
  the ordering survives as an explicitly **non-normative** illustrative sequence. The
  rule generalizes: **where a step list would need a converse for every gap between
  steps, write the properties the operation must satisfy and name the single point
  where it becomes true.** Two definitions were also collapsed to one home each. The
  DELETE guard and the repair guard now read the same 「The supply guard」 section —
  computed in the MENU identifier namespace, over EFFECTIVE selections, against
  post-change RUNNABLE supply, where "runnable" is the chain classifier's own field
  and not `chain_length > 0` (round 6's structural test passed a chain holding one
  revoked key). And `SupplyGap.agents` now names the Agents that would actually break
  INCLUDING backend-default inheritors, which **reverses round 6's parenthetical** —
  recorded as an orchestrator ruling open to owner veto, because round 6's rule
  answered 「谁明确写了这个模型」 and therefore returned an empty list on a real
  interruption, which is false reassurance where the product owes the user a named
  cause and a named victim. The remaining finding was a GRAIN mismatch that default
  naming had hidden: 「Agent」 on an event is a backend, 「Agent」 in routing is a named
  Vibe Agent, and the two coincide only because the default Agents are named after
  their backends — so §4.5's recipient rule now expands in two hops, backend → enabled
  Agents on it → scopes selecting those Agents.
- **Round 7's other class was vocabulary blast radius, and it is now mechanical.**
  Round 6 added one cause to two files and left the three other files that mirror that
  vocabulary asserting an identity they no longer had — a required field with no legal
  value, three times over, each one a documented self-contradiction rather than a gap.
  Two mechanisms let it through and both are closed: comparison ran INSIDE each file,
  so a third mirror had nothing watching it; and the checker collected `enum` only,
  while the new key was pinned as a `const`, so the guard whose whole job was this
  check passed vacuously. `const` is now read as an enum of cardinality one, every
  shared vocabulary is a registered row in the table below, and the inverse direction
  is enforced too: a field whose description names another contract file must appear in
  that table or the harness fails. That inverse is the part that stops the class —
  detection is by schema STEM, not by the `.schema.json` suffix and not by the word
  "mirror", because round 7's own missed claim used neither.
- **Existing examples stay v1-shaped on purpose.** The byte-faithful round-trip
  test (`tests/test_model_hub_config.py`) drives the shipped serializer through
  every example in `source.schema.json` and `agent-supply.schema.json`, so a v2
  field in those examples would assert that dormant v1 code already speaks v2.
  Worked v2 payloads live in `api.md` instead, where they document the target
  without lying about the present.
- Two v2 enum values (`source.state.status: needs_action`,
  `resolution-event.kind: needs_action`) are likewise targets: today's loader
  whitelist in `config/v2_config.py` rejects the former until the serializer PR
  widens it. Called out inline in the schemas.

## Vocabulary mirrors and their blast radius

draft-07 has no cross-file `$ref` the contract test can resolve, so a vocabulary shared
by two contracts is duplicated by hand. Every such duplication is registered here with
a MECHANICAL RULE, and the harness evaluates the rule rather than trusting the prose —
including the inverse direction: **a field whose description names another contract file
must appear in this table.** Adding a value to a home field is therefore never a local
edit; this table is the list of files that edit reaches.

| # | Vocabulary | Home | Mirrors | Rule |
| --- | --- | --- | --- | --- |
| M1 | the ten `detail_key` values (5 cooldown + 4 needs_action + 1 error) | `source.schema.json` → `state`, across all three status branches — the `error` key is pinned as a `const`, which counts | `probe-result.schema.json` → `error` (minus null), and the `examples` list on the home field itself | `equality` |
| M2 | source status → chain health | `source.schema.json` → `state.status` | `agent-chain.schema.json` → `chain[].health` | `projection` |
| M3 | attempt-failure causes | `resolution-event.schema.json` → `reason` | `turn-provenance.schema.json` → `failed_attempts[].reason` | `partition` |
| M4 | non-self-healing cause ↔ blocking `detail_key` | `resolution-event.schema.json` → `reason` description | `source.schema.json` → the `needs_action` and `error` `detail_key` branches | `none` |
| M5 | the supply-state taxonomy | `agent-supply.schema.json` → `supply_status` | `agent-chain.schema.json` → `supply_state`; `turn-provenance.schema.json` → `model_supply_state` | `projection` |
| M6 | backend identifiers | `agent-supply.schema.json` → `backend` | `agent-chain.schema.json` and `probe-result.schema.json` → `backend`; `turn-provenance.schema.json` → `agent` | `equality` |
| M7 | supply channel | `source.schema.json` → `supply_channel` | `agent-supply.schema.json` → `current.channel`; `turn-provenance.schema.json` → `failed_attempts[].channel`, `served.channel`, `terminal_error.channel` | `equality` |
| N1 | source recommendation rule | `agent-supply.schema.json` → `sources.policy` | — | `none` |

What the rules mean, and why three of them are not equality:

- **`equality`** — the sets are identical, in both directions. M6 carries one declared
  superset: `resolution-event.agent` is the home set plus `system`, the emitter for
  events no backend produced, which expands to no Agent and therefore to no recipient.
  A documented superset is checked as `home ∪ {declared extras}`, so a *fourth* backend
  appearing there alone still fails.
- **`projection`** — the mirror is a stated function of the home, because the two live
  at different grains. M2: `health` carries the health half only, so `active`/`standby`
  collapse into `healthy`. M5: one taxonomy at two grains — the backend rollup adds
  `degraded` (serving, but via fallback), and `model_supply_state` exists only for
  `outcome: no_candidate`, where `ok` is unreachable by construction. Both projections
  are executable, so widening the home without deciding where the new value lands fails.
- **`partition`** — M3 is not a subset relation but a total classification: every
  `resolution-event.reason` must be either an attempt failure (mirrored) or in a
  declared exclusion, and never both. Two exclusions exist. The three agent-scoped
  causes are **derived from the schema**, not restated here — they are exactly the enum
  on the branch that pins `kind: supply_interrupted` — and `recovery`/`manual`/`mapping`
  are declared, because a resolution that is not a failure has no attempt to report. A
  new `reason` fails the check until it is classified, which is the point.
- **`none`** — registered as non-comparable, with the reason. M4 is a BIJECTION, not a
  set identity: a permuted pairing has identical sets, so it is checked against the
  mapping documented in the home field's own description instead. N1 is a pointer to a
  rule (spec §4.2 plus the absent-`created_at` half-rule), not a shared value set —
  there is nothing to compare. A row with neither rule nor reason is itself a failure.

## Files

| File | Consumers |
| --- | --- |
| `source.schema.json` | L2 API, L4 UI. **v2:** +`state.status: needs_action` (+ mandatory `detail_key` there, enforced by an `if/then`), +immutable `created_at` (the sort key 跟随推荐 needs) with its **null placement now normative (07-29)** — absent sorts before every stamp, constant-epoch backfill, because a tie-break orders equals and cannot order null against a timestamp — +optional `usage.projected_exhaust_at`. No ordering field, ever. |
| `agent-supply.schema.json` | L2, L3 injection, L4/L5 UI. **v2 owns the spend order:** +`sources` {policy, order, eligibility}, +`supply_status` (incl. `waiting`), +`model_supply`, +top-level `selected_model_id`. **07-29 round 3:** the selection was hoisted out of `current` (where round 2 had it as `menu_model_id`) because `current` is null in `waiting`/`interrupted` — the two states where the drawer most needs to name the model. A persisted user choice must not inherit the nullability of a momentary fact. `policy` also stopped paraphrasing the 跟随推荐 rule and now points at spec §4.2. **+`selected_by_agent`** names the GRAIN of that selection — which Vibe Agent's `model` produced it, null when it came from `agents.<backend>.default_model` — because N Agents can share one backend with different models, so `selected_model_id` on its own answers for nobody in particular. It names a grain and does NOT reverse the frozen per-Agent ordering ruling. It is required on every hub-mode response at the API boundary (`api.md`) rather than by `required` here, for the byte-faithful reason below. |
| `agent-chain.schema.json` | **New in v2.** L2 API, L4 UI (model box drill-in, 模型菜单 drawer). Carries the CAPABILITY chain — cooling members included with `runnable: false` — and per-item `health` only; per-agent role is positional, never a copied `active`/`standby`. **+`supply_state` (07-29):** the three-class taxonomy at the (agent, model) grain, and the ONE field every model-scoped consumer reads (chain drill-in, the probe's `supply` sibling, turn record) so none of them consults the agent rollup for a model it does not describe. Its predicate is not restated here or there — spec §4.5 owns it, after round 3 found two files paraphrasing it into a different rule. |
| `probe-result.schema.json` | **New in v2.** L2 API, L4 UI (「试跑一次」). Outcome field is `reachable`; the object nests under `probe` so it never collides with the envelope's `ok`. **07-29 round 3:** `reachable` now drives conditionals in both directions, so a reachable probe cannot carry null ids or an error; and `probe_no_candidate` moved its structured half out of `detail` into a typed `supply` sibling — the shipped clients declare `detail?: string`, so the object would have rendered as `[object Object]`. It reads the requested model's `supply_state`, never `supply_status`. |
| `turn-provenance.schema.json` | **New in v2.** L2 API, L3 (writes it per turn), IM surfaces (per-turn detail). Gives spec §4.5's turn-provenance promise an actual read contract. **Shape ruling 07-29:** top-level `outcome` + `failed_attempts` + one slot for the terminating attempt; the empty attempt list is legal because a turn that never touched a source is exactly the one users inspect, and the terminating attempt lives in one place so contradictory records cannot be written at all. **Round 3 added the fourth outcome:** `failed_terminal` + a `terminal_error` slot, for §4.3's non-fallback errors (param/protocol/tool-compat, and anything after the first streamed token). Without it such a turn fit no outcome, and filing it as `exhausted` would have forced a fallback reason onto it — blaming the user's account for a malformed request. |
| `priority.schema.json` | **Removed in contract v2 — v1 tombstone, do not implement.** Nothing in v2 reads it. Kept only because dormant v1 code still emits the shape and `tests/test_model_hub_api.py` still validates against it; the lane that removes `PUT /api/models/priority` and `ModelHubConfig.priority_order` deletes this file and replaces that one assertion with an inline shape check. |
| `resolution-event.schema.json` | L2 (adapter-owned), L4 UI, L1 adapter. **v2:** +`severity` (notification tier), +`kind: needs_action`, +`kind: supply_interrupted` — the only AGENT-scoped kind, for a chain that empties without any source changing state (last enabled source removed). Its shape is enforced by conditionals, not prose: `severity` is `action_required`, source endpoints are null, and its three reasons are unusable with any other kind. Worked payload in `api.md` (a schema example would need `severity`, which the shipped emitter has no field for). **Round 5 (07-29):** `severity` decides WHETHER to interrupt, never WHOM — there is deliberately no recipient, channel, platform, or audience field, and recipients resolve at push time from the event's `agent` against the live routing table (spec §4.5 → 「Who receives an action-required push」). Carrying them here would freeze a routing snapshot into an append-only feed; the feed is a record of what happened to supply, not an outbox. |
| `oauth-flow.schema.json` | L2, L4 UI, L1 engine adapter |
| `migration-scan.schema.json` | L6, L5 UI. Unchanged by the v2 ruling — native-config import is an onboarding feature, not a priority mechanism. |
| `runtime-dependency.schema.json` | L1, L2 status API, L7 guards. **URL policy (orchestrator, 07-23 12:05):** the example URLs are placeholders; L1 ships with upstream release URLs + SHA256 integrity verification. Availability guard = L7/orchestrator deliverable BEFORE GA: mirror the pinned assets into Avibe-owned release storage (same manifest-verified backup/recovery pattern as Show Runtime, per repo release rules), then point the manifest at the mirror with upstream recorded as provenance. SHA256s never change (same bytes). L1 must NOT build the mirror or touch the Show Runtime guard. **Platform expansion (07-23 13:13):** linux-arm64 / darwin-x64 assets get pinned (+ schema platform-enum rev) together with the mirror work at L7; until then unsupported hosts fail closed, Direct = escape hatch (L1's `model_hub_engine_platform_unsupported` coverage is the intended behavior). **Cross-vendor note (07-29):** if the v2.1 cross-vendor spike (spec §10.4) concludes that a conversion pair needs a CPA plugin or a different engine build, that lands here as a pin + SHA256 revision with mirrored assets published before the manifest moves — never as user-facing configuration. |
| `api.md` | all. **v2:** `PUT /api/models/priority` removed; +`PUT /api/models/agents/<backend>/sources`, +chain query, +probe, +`adopted_by`. **07-29 round 3:** +`PUT /api/models/sources/<id>/credential` and +`POST /api/models/sources/<id>/reauth` — spec §4.5 promised the `needs_action` card a one-tap 「换 Key / 重新登录」 and there was no route to send that tap to, only create-a-new-source, which loses the source's `created_at` and its slot in every order. Recovery must not be a reorder. Also: the `detail`-is-a-string rule made normative, and the DELETE guard re-scoped to (backend, model) — a backend with four enabled sources still starves a model supplied by only the one being deleted. |
| `opencode-overlay.md` | L3, L7 (identifier-stability tests). Identifiers stay stable across per-agent reordering — a source is never encoded into the provider segment. |
| `adapter-interface.py` | L1 (implements), L2 (consumes; owns in-repo copy). Dual-copy rule: both lanes copy VERBATIM to `core/handlers/model_hub/adapter.py` in their branches — byte-identical, merge is a no-op. Added 07-23 10:55 after L1 raised the ordering race; **v1.1 07-23 11:05**: +OAuth surface with deterministic source binding (`start_oauth(source_id)` → success carries `credential_ref`), +`allowed_origins`/`invoke(origin)`/`OriginNotAllowedError` (both from L1 review findings). Unchanged by v2: candidate walking and error classification stay Python-side, so moving ordering from global to per-agent needs no adapter or engine change (spec §8). |

## Security invariants (from S1/S2, non-negotiable)

1. No credential material ever appears in any payload defined here —
   sources expose `credential_ref` only; events are adapter-redacted.
   Clarified 07-23 12:10 (L4 finding): non-reversible display data IS
   permitted — `account_label` (subscription identity from the sanctioned
   auth surface) and `masked_credential` (≤7-char prefix + "…" + last 4,
   computed once at provisioning, never re-derivable) exist precisely so
   the UI never needs anything stronger.
2. The engine's own usage feed stays disabled; events originate from our
   adapter (S1 gap ②). Extended in v2: `ProbeResult.error` and every
   `detail_key` are i18n keys — classified outcomes only, never raw upstream
   error bodies, on the probe path as much as the event path.
3. `allowed_origins`-style client binding is enforced in code: subscription
   sources are never eligible for agents outside their sanctioned client
   (S2; server-side enforcement exists for Claude anyway). In v2 this is the
   same rule the server now publishes as `sources.eligibility` — one
   implementation, projected to the UI, never re-derived there. **The binding is
   keyed on vendor and holds in both channels**: `subscription_hub_experimental`
   unlocks *how* a subscription is delivered, never *who* may consume it, so a
   hub-held Anthropic subscription is eligible for Claude Code alone (spec §4.4
   matrix, one row per vendor×channel precisely so this cannot be read loosely).
