# Model Hub — Interface Contracts

Status: **FROZEN v4 (targeted)** · 2026-07-29 · change only via orchestrator
Advances `agent-chain` and `probe-result` from contract version 3 to 4. The
same-assumption audit makes prose-only corrections in `source` and `agent-supply`;
all other schema shapes and versions remain at v3.
Derived from: spec v2 (`../model-hub.md`), implementation plan
(`../model-hub-implementation.md`),
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

## v3 handoff notes (single freeze, orchestrator 2026-07-29)

v3 is the batch's only post-v2 contract state. L1 authors this freeze once; every
later lane treats `model-hub-contracts/**` as read-only and implements against it.
An implementation-proven mismatch is escalated for a targeted v4 rather than edited
in a later lane. No v3 term is deferred to v4.

Client-visible changes:

- `GET /api/models/agents` adds `named_agents`, with each enabled named Agent's
  effective model and live `supply_status`.
- Hub with no explicit Agent model and no backend default reports a null selection,
  null `current`, and null rollup. This is deliberate: no pinned selection means each
  turn resolves the model carried by that request. Choosing `builtin_models[0]` would
  invent state the user never selected, the same false-state class as a fabricated
  source or recovery.
- Direct chain/probe calls and Direct or ambiguously unattributed provenance reads
  return documented errors instead of fabricated Hub data.
- Turn provenance adds `outcome: canceled` and `canceled_attempt`; resolution events
  allow `model_id: null` only for source-scoped system events.
- Canonical `src_` patterns, closed eligibility reasons, repair confirmations,
  credential force, blocked-source re-test, enabled-mapping guard scope, and durable
  revocation reconciliation are frozen in `api.md` and the schemas.
- Resolution events remain single records. v3 adds neither affected-backend fan-out
  nor an attribution-grain marker.

## Targeted v4 process

Targeted revisions are orchestrator-authorized, evidence-pinned amendments: code
citations are required in the schema `$comment`s, and the revision is delivered as
a dedicated contract commit merged into the implementing lane's branch. Chat
messages alone never unfreeze a file. The first instance is this commit,
`docs(model-hub): amend channel-aware native CLI contracts v4`, consolidating
escalations #2+#3 as one class-level correction against master `807d1eee`; it remains
owner-vetoable.

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
  `docs(model-hub): freeze interface contracts v3` and announces the commit SHA
  in every lane brief. From then on: lanes cite, never edit; changes go through
  the orchestrator and bump `contract_version`.
- **Targeted v4 process instance trail — entry 2 (v4b).** Following v4a commit
  `37285675`, the dedicated commit
  `docs(model-hub): scope hub OAuth reauth rollback invariant v4b` is
  orchestrator-authorized, evidence-pinned to review finding `3675168031` and
  `vibe/model_hub_runtime/state.py:135-166`, and owner-vetoable. It corrects only
  the hub-OAuth stable-ref repair invariant; chat messages alone did not unfreeze
  these files.
- Contract tests (implementation plan §5) validate both directions against
  these schemas.

## Required-vs-optional discipline (v4)

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
  `agent-supply.sources` / `supply_status` / `model_supply` / `named_agents` /
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
  (a revoked key that validates as `info` is presented as self-healing);
  `reachable: true` pinning non-null `source_id` / `model_id` /
  `via_mapping` / `latency_ms` and a null `error` (「答了，但不知道是谁答的，另附失败原因」
  was a valid probe); `outcome: failed_terminal` and the `terminal_error` slot
  beside `served`; every `outcome` branch now pinning the slots it *forbids*, not
  just the ones it uses. Two overlapping-vocabulary cases got a **normative
  precedence** instead, since no shape can order them: the three
  `supply_interrupted` reasons, and `interrupted` before `waiting`.
- **A predicate has exactly one home** (added 07-29, round 3). Round 2 restated the
  `waiting`/`interrupted` split in three files and got it wrong in two — 「每个成员都
  需要用户处理」 instead of 「至少一个」 — which made a mixed chain match no value and
  suppressed the action-required state. Likewise the 跟随推荐 recommendation rule,
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
  no representable state event — the emitter had to misclassify or stay silent.
  It became a fifth CAUSE rather than a ninth kind, which keeps the
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
  naming had hidden: 「Agent」 on an event is a backend, while `named_agents` is a
  live projection of named Vibe Agents. The two coincide only for a default Agent
  whose name happens to equal its backend.
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
The executable source of this table is `mirror-registry.json`; the contract harness
loads it and mutation-tests every comparable row.

| # | Vocabulary | Home | Mirrors | Rule |
| --- | --- | --- | --- | --- |
| M1 | the ten `detail_key` values (5 cooldown + 4 needs_action + 1 error) | `source.schema.json` → `state`, across all three status branches — the `error` key is pinned as a `const`, which counts | `probe-result.schema.json` → `error` (minus null and its probe-local native readiness key), and the `examples` list on the home field itself | `equality` |
| M2 | source status → chain health | `source.schema.json` → `state.status` | `agent-chain.schema.json` → `chain[].health` | `projection` |
| M3 | attempt-failure causes | `resolution-event.schema.json` → `reason` | `turn-provenance.schema.json` → `failed_attempts[].reason` | `partition` |
| M4 | non-self-healing cause ↔ blocking `detail_key` | `resolution-event.schema.json` → `reason` | `source.schema.json` → the `needs_action` and `error` `detail_key` branches | `bijection` |
| M5 | the supply-state taxonomy | `agent-supply.schema.json` → `supply_status` | `named_agents[].supply_status`; `agent-chain.schema.json` → `supply_state`; `turn-provenance.schema.json` → `model_supply_state` | `projection` |
| M6 | backend identifiers | `agent-supply.schema.json` → `backend` | `agent-chain.schema.json` and `probe-result.schema.json` → `backend`; `turn-provenance.schema.json` and `resolution-event.schema.json` → `agent` (`system` is the declared event-only extra) | `equality` |
| M7 | supply channel | `source.schema.json` → `supply_channel` | `agent-supply.schema.json` → `current.channel`; `agent-chain.schema.json` → `chain[].channel`; `probe-result.schema.json` → `channel`; all four turn-provenance attempt slots | `equality` |
| M8 | native CLI process availability | `agent-chain.schema.json` → `chain[].reason` (`native_cli_unavailable`) | `probe-result.schema.json` → native not-ready `error` (`models.probe.native_cli_unavailable`) | `mapping` |
| N1 | source recommendation rule | `agent-supply.schema.json` → `sources.policy` | — | `none` |

What the rules mean, and why the non-equality rules differ:

- **`equality`** — the sets are identical, in both directions. M6 carries one declared
  superset: `resolution-event.agent` is the home set plus `system`, used for a
  source-scoped non-turn operation. A documented superset is checked as
  `home ∪ {declared extras}`, so a fourth backend appearing there alone still fails.
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
- **`mapping`** — M8 is one concept in two field conventions: a bare classifier in
  the chain and an i18n key in the probe error slot. The explicit one-to-one map is
  checked in both directions; adding a spelling on either side fails until paired.
- **`bijection`** — M4 checks both sets and each explicit cause→detail-key pair;
  a permuted pairing therefore fails even when the two sets still match.
- **`none`** — registered as non-comparable, with the reason. N1 is a pointer to a
  rule, not a shared value set. A row with neither rule nor reason is itself a failure.

## Files

| File | Consumers |
| --- | --- |
| `source.schema.json` | L2 API, L4 UI. **v2:** +`state.status: needs_action`, immutable `created_at`, optional `usage.projected_exhaust_at`. Targeted v4 audit changes no shape: healthy source state and a lapsed `retry_at` do not assert process-local native CLI availability. No ordering field, ever. |
| `agent-supply.schema.json` | L2, L3 injection, L4/L5 UI. Owns `sources`, the selected-route rollup, and v3 `named_agents`. Targeted v4 audit changes no shape: `supply_status` is derived from complete process-aware runnability, so unavailable native CLI supply is `interrupted`, not cooldown-only `waiting`. Hub with no pinned selection remains null throughout. |
| `agent-chain.schema.json` | **New in v2, targeted v4 amendment.** L2 API, L4 UI (model box drill-in, 模型菜单 drawer). Carries the CAPABILITY chain and preserves visibility/order for blocked members. Each item now distinguishes source-global `health` from resolve-time channel availability: `runnable = health-permits AND process-available`; Hub availability is definitionally true, while an unavailable native CLI item at any health stays dimmed in place with `reason: native_cli_unavailable`. Cooldown plus unavailable is `interrupted`, not `waiting`. |
| `probe-result.schema.json` | **New in v2, targeted v4 amendment.** L2 API, L4 UI (「试跑一次」). Outcome field is `reachable`; the object nests under `probe` so it never collides with the envelope's `ok`. The probe selects the first runnable item; unavailable chain items are skipped. Hub keeps v3's completed-request truth table. Native CLI re-verifies process readiness, never claims completion evidence, and pins `latency_ms: null` in both directions. It reads the requested model's `supply_state`, never `supply_status`. |
| `turn-provenance.schema.json` | L2 API, L3 writer. v3 adds the FSM-truth `canceled` outcome and a reason-free `canceled_attempt` slot. Records exist only when attribution is exact; Direct and ambiguous absence are route errors in `api.md`. |
| `resolution-event.schema.json` | L2 emitter, L4 UI. v3 permits nullable `model_id` only for source-scoped system events, rejects `system` on backend-scoped interruption, and keeps one record with impact derived live. |
| `oauth-flow.schema.json` | L2, L4 UI, L1 engine adapter |
| `migration-scan.schema.json` | L6, L5 UI. Unchanged by the v2 ruling — native-config import is an onboarding feature, not a priority mechanism. |
| `runtime-dependency.schema.json` | L1, L2 status API, L7 guards. **URL policy (orchestrator, 07-23 12:05):** the example URLs are placeholders; L1 ships with upstream release URLs + SHA256 integrity verification. Availability guard = L7/orchestrator deliverable BEFORE GA: mirror the pinned assets into Avibe-owned release storage (same manifest-verified backup/recovery pattern as Show Runtime, per repo release rules), then point the manifest at the mirror with upstream recorded as provenance. SHA256s never change (same bytes). L1 must NOT build the mirror or touch the Show Runtime guard. **Platform expansion (07-23 13:13):** linux-arm64 / darwin-x64 assets get pinned (+ schema platform-enum rev) together with the mirror work at L7; until then unsupported hosts fail closed, Direct = escape hatch (L1's `model_hub_engine_platform_unsupported` coverage is the intended behavior). **Cross-vendor note (07-29):** if the v2.1 cross-vendor spike (spec §10.4) concludes that a conversion pair needs a CPA plugin or a different engine build, that lands here as a pin + SHA256 revision with mirrored assets published before the manifest moves — never as user-facing configuration. |
| `api.md` | All route and envelope contracts. Targeted v4 keeps the shared envelope at v3 while documenting nested v4 chain/probe channel truth, orthogonal native availability, first-runnable probe selection, and stale readiness re-verification. The remaining route contracts are unchanged. |
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
