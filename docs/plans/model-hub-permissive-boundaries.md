# Model Hub permissive request boundaries

## Ownership and authorization

Owner approval on 2026-09-09 authorizes implementation and parallel Codex
delegation following the preceding request-path audit. The user-started Session
remains the orchestrator and final reviewer. This is not authorization to merge,
publish an engine or application release, deploy, update/restart the running
local Avibe, mutate user configuration, or advance the primary checkout.

Implementation starts from default-branch commit
`e4924db41bcbf37581cda08cf1376a899e70906c`. Independent lanes share this committed
contract before they branch. Avibe changes integrate into one task branch and
one normally reviewed PR; no lane stacks a PR on an unmerged peer.

## Evidence and goal

A Codex Blender Session failed at the authenticated Avibe gateway's 16 MiB
request boundary. Reconstructing its latest persisted context checkpoint and
subsequent model items found 20 inline PNG images totaling approximately 42 MiB.
This was not a capture of the rejected wire request. The new user text was
39 UTF-8 bytes. A hermetic 18 MiB aiohttp request passed with the boundary
disabled, but the read/parse/reserialize fixture recorded about 76.5 MiB of peak
Python allocations, excluding the engine. Removing every resource bound is
therefore not the immediate remedy.

The product should accept legitimate multi-image requests, preserve explicit
model intent, and never equate absent capability knowledge with demonstrated
non-support. Resource ownership, authentication, cancellation, routing, and
persisted identity remain real constraints.

## Shared invariants

1. **Request envelope.** The common authenticated gateway accepts valid request
   bodies through 128 MiB for Messages, Responses, and Chat Completions, with
   consistent byte semantics for Content-Length and chunked bodies. Oversize
   input returns a structured, non-secret 413 describing the local boundary,
   before upstream admission. It does not mark a provider unhealthy, consume an
   alternative provider, or silently truncate history. Do not add image-count,
   turn-count, output-length, or normal-inference-duration caps.
2. **Capabilities.** An absent override remains distinct from an explicit
   negative or an explicit user selection. Custom model projection must not
   silently strip supplied images or force reasoning to `none` solely because
   catalog metadata is absent. Do not inherit a different model's provider-only
   features, context size, tools, or expensive defaults. Preserve native known
   rows, explicit negatives, persisted shapes, and model-selection ownership.
3. **Limit authority.** Catalog context/output metadata assists planning; it
   must not silently replace explicit user configuration with a stale default.
   Preserve useful automatic compaction and backend schema requirements.
   Determine authority from existing fields/owners where possible. A new
   persisted provenance model requires an orchestrator decision before editing.
4. **Model identity.** Reassess the 256-character admission bound across manual
   input, discovery, routing, UI/API, metering, and load. Do not replace it with
   another arbitrary number or remove it without checking downstream identity
   and usage-key behavior. No truncation, identity merging, secret admission,
   startup breakage, or silent loss of historical usage is permitted. The lane
   reports its smallest complete proposal before implementation.
5. **Engine intent.** Inspect the exact pinned CLIProxyAPI v7.2.149 source
   `2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`. Same-protocol explicit reasoning
   intent must not be removed or downgraded solely by incomplete capability
   metadata. Preserve required cross-protocol translation, OAuth lifetimes,
   credential replacement, Source/model identity, and all routing policy.
   Do not remove registration metadata as a shortcut: it can worsen stripping.
   Report the source/build/release plan before implementing engine policy.
   Never point a shipped manifest at missing assets or claim wire preservation
   from a fake-adapter-only test.
6. **Non-goals.** Keep bounded observation copies, temporary-file spill
   thresholds, chunk sizes, authentication, cancellation cleanup, real upstream
   failures, and existing retry ownership. Do not weaken CI, migrate unrelated
   state, add a detection save gate, replay live credentials, or change unrelated
   pending PRs.

## Boundary ownership

| Boundary | Producer | Consumer | Contract owner |
| --- | --- | --- | --- |
| Native model request | Claude/Codex/OpenCode | authenticated turn gateway | gateway lane |
| Request model/protocol/header envelope | gateway | resolver and managed adapter | existing shared request owner, unchanged |
| Backend model capability/limit projection | selected backend model catalog | CLI launch/catalog consumers | capability lane |
| Model ID admission and durable usage identity | setup/discovery | config, routes, ledger, UI | identity lane, orchestrator approval |
| Reasoning wire conversion | managed engine | exact selected mock upstream | engine lane, orchestrator approval |
| Integration, authority/scenario inventory, PR gates | all lanes | owner | orchestrator |

No new signature or credential field is introduced. Existing local bearer
authentication and exact Source/model binding must remain unchanged.

## Parallel delivery

- **Gateway lane:** owns gateway request-size/error handling and focused tests
  plus only the locale/catalog entries required for its new error surface.
- **Capability lane:** owns backend catalog projection, CLI limit precedence,
  and their consuming tests. Report cross-lane changes rather than editing
  gateway/engine/identity code.
- **Identity lane:** owns the complete identifier/ledger boundary diagnosis
  first; implementation is gated on the orchestrator's recorded decision.
- **Engine lane:** owns exact-pinned-source diagnosis and a reproducible
  hermetic wire/build proposal first; policy edits, publication, and a manifest
  change are separate decisions.
- **Orchestrator:** owns this plan, integration branch, final review, combined
  validation, PR submission, durable PR/CI observation, and close-out.

Lane commits remain local until integrated. Lanes must not push, open PRs,
merge, create release tags/assets, deploy, or edit a peer's worktree. Each lane
returns a stable Session/Run ID, commit SHA, tests actually run, known gaps, and
any contract decision required. A callback is evidence to inspect, not approval.

## Acceptance

- Exercise real hermetic HTTP ingress with a multi-image-size payload exceeding
  16 MiB; compare exact input reaching the fake upstream/adapter, including
  non-ASCII text and image data. Check both body framing shapes, threshold
  boundaries, bad credentials, malformed JSON, cancellation, and no provider
  health mutation for a local 413.
- Cover unknown custom models, known built-ins, explicit supported/unsupported
  choices, aliases, user-selected reasoning, and context/output precedence in
  consuming launch/catalog paths.
- For any identifier change, cover every admission surface and legacy persisted
  IDs, non-ASCII names, collision-shaped names, reload and usage stability.
- For engine policy, require actual pinned engine-to-mock-upstream wire evidence
  for applicable same- and cross-protocol paths; distinguish source proof,
  built artifacts, published availability, and installed behavior.
- Run focused Python checks and pinned Ruff, then relevant Model Hub suites,
  authority closure and scenario/catalog checks. UI changes require UI tests,
  TypeScript and build. Use only isolated fixtures/local regression targets.
- Require current-head Codex pass, all expected CI jobs, and zero unresolved
  paginated threads. Record findings-bearing heads/root causes before edits;
  enforce the review circuit breaker. No automatic merge authorization exists.

## Decisions and status

- Initial decision: implement the 128 MiB compatibility boundary, not unlimited
  buffering; investigate the identifier and engine seams before choosing edits.
- Initial findings-bearing review heads: zero. No PR has been submitted yet.

### Capability and launch-limit decision (2026-09-09)

The orchestrator independently inspected catalog projection, Claude environment
construction, its SessionHandler call site, and injection/CLI-path consuming
tests at the shared contract base. The current Hub path both tombstones limit
variables as though they were credentials and promotes catalog limits into
highest-priority launch settings. Native CLI launches also overwrite explicit
environment limits. These are competing owners, not evidence that a persisted
provenance system is needed.

Approved scope:

- Keep existing BackendModel storage and the requested alias's metadata
  authority. A saved non-null limit remains that selected model's planning
  description; do not infer field provenance from row-level `origin`.
- For Claude, leave credential/connection tombstones intact but remove the
  two context/output limit variables from that boundary. Catalog limits fill
  only absent subprocess variables. Empty explicit values are not permission
  to invent another override. Do not inject model limits in the Hub connection
  settings override; preserve native project/local settings precedence.
- Verify the real launch consumer as well as pure environment helpers: parent
  environment, explicit settings, absent limits, selected aliases, and both Hub
  and native CLI paths. Do not broaden the enabled native setting sources.
- Codex keeps its native configuration precedence if consumer evidence
  confirms it. Custom capability absence must allow supplied images/reasoning
  without importing a different model's private features or expensive default
  effort. Explicit negatives and nonempty lists retain their meanings.
- OpenCode retains the selected BackendModel as owner of the managed Hub
  provider's planning metadata. Preserve separate native providers; do not merge
  arbitrary native provider objects into a managed authentication definition.
- No new persisted schema, UI policy, engine policy, credential flow, routing
  policy, or output cap is approved by this decision.

The remaining uncertainty is the native CLI consumer's treatment of absent
capabilities and conflicting settings. Hermetic consumer evidence must confirm
the representation and precedence before integration; a schema requirement or
unexpected consumer override calls for another bounded decision, not a fallback
to silent filtering.

### Identifier decision (2026-09-09, local +08)

Lane C's diagnosis is committed as
`e3f3628230333b141449684ba2f9af90ad0e3d1a`. The orchestrator independently
inspected live and persisted ledger key owners, their consuming closure test,
the selective parser and inventory projection, new-ID admission, OpenCode
aggregate loading, and the typeahead refusal. A separate read-only synthetic
run confirmed 12 valid long/Unicode/folded-literal identities remain distinct
with stable read-back keys. It also reproduced the pre-existing whitespace
normalization exception and admission of unencodable surrogate text.

Approved smallest complete scope:

- Remove the 256-character admission ceiling and its editor, schema, locale,
  and typeahead-query mirrors. Keep canonical outer-whitespace spelling,
  nonblank text, complete credential scanning, exact Source/model pairing, and
  backend/source eligibility policy.
- Require newly admitted IDs and unsaved observation IDs to be representable
  as UTF-8, without replacement, truncation, Unicode normalization, or case
  folding. This is a transport/storage validity check, not a new length cap.
  Do not apply new admission rules to legacy persisted identities.
- Separate OpenCode aggregate loading and whole-list editing from new-ID
  admission while retaining existing spelling, nonblank, native-protocol,
  menu, and route-membership invariants.
- Extend the existing selective parser with a default-empty lossless-string
  path option. Opt only inventory string-row IDs, object-row IDs, and
  supported-parameter string values into it. Decide at value-token start;
  object keys and unrelated values keep the existing bounded behavior.
  Preserve malformed-JSON rejection, duplicate-member/scope semantics,
  document completion, spooling, and the discovery deadline.
- Keep the 200-character ledger head, digest calculation, persisted key
  reader, stored rows, label joins, and retention unchanged. New admission can
  safely include folded-looking literals because live derivation folds those
  literals again; persisted key recognition is intentionally a different owner.
- Limit shared service/config changes to the diagnosed admission, discovery,
  typeahead, and load seams. Lane B's projection and lane A's gateway remain
  independently owned. No new schema revision, dependency, ledger namespace,
  engine setting, or arbitrary replacement size is approved.

Selected identity/parameter facts necessarily occupy memory proportional to
their full values. This decision does not remove generic HTTP/resource limits
or promise arbitrary URI-size support. Consuming tests must cover lexical
JSON expansion, long credential-shaped tails, exact runtime IDs, key-literal
closure, reload, and the UI's whole-ID submission.

Known-by-design legacy exceptions: an already loadable long all-whitespace
identity can be misattributed by the existing persisted-key normalization;
unencodable historical identities can also fail later encoding. Neither is
introduced by removing the limit, and blank/unencodable new IDs are refused.
Historical merged counters cannot identify their original owner, so this PR
must not rekey or split them speculatively. Preserve the diagnostic evidence
and record that attribution-policy follow-up separately; do not claim a proof
over every malformed legacy string.

### Native consumer follow-up

The orchestrator inspected Codex 0.153.2's
`models-manager/src/model_info.rs::with_config_overrides` and lane B's actual
app-server consumer test. Codex takes the minimum of explicit
`model_context_window` and catalog `max_context_window`; Avibe currently
synthesizes both catalog fields from one BackendModel planning value.

Approve removing `max_context_window` only when applying an explicit saved
BackendModel `context_window` override. Retain `context_window` as the planning
fallback and remove its obsolete catalog compaction threshold as before.
Native rows without that override and native-CLI launches remain unchanged.
The consumer test must prove both the fallback and an explicit larger context,
alongside the native compaction setting. No Codex output cap is introduced.

Claude's bundled CLI consumer honors explicit output limits, but its normal
context planner does not necessarily consume `CLAUDE_CODE_MAX_CONTEXT_TOKENS`;
the diagnosed context branch checks it only with compaction disabled. Retain
the approved environment/settings preservation, prove actual output and
Avibe-to-SDK context delivery, and document this native planner limitation.
Do not disable compaction or claim end-to-end context control from environment
injection alone.

### Engine source-only decision

Lane D's complete assessment is at
`2a756b4b19a1478d06e1cc2a382aad0a9268acb5`. The orchestrator independently
verified the clean exact upstream SHA, read the shared thinking entry point,
validation and configured-model consuming tests, inspected the HTTP fixture,
and verified the baseline artifact digest and all 380 result records. The
recorded native disable-to-positive outcomes agree with the inspected
capability clamp. These are baseline defects, not patched acceptance results.

Approve a maintained, inactive candidate source/test patch series under
`patches/cliproxyapi/`, against upstream
`2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`, with a small apply/test recipe
and frozen input receipt. This is a local source-maintenance decision only,
not authorization for a public fork, upstream contribution, release assets,
manifest changes, installation, or runtime replacement.

Source scope:

- Distinguish identical reasoning wire representations from broad provider
  families at the shared thinking entry point. Without an explicit suffix,
  preserve the already prepared native target payload instead of performing
  catalog-driven strip/map/validate/reapply. Recognize verified Responses
  aliases; do not conflate Chat/Responses or Kimi's different dialect.
- Extract explicit disable before capability gating, retain suffix priority,
  reuse native disable writers before their support guards, and never turn
  disable into a positive level or reactivate it through summary handling.
- For genuine conversion with missing thinking metadata, use the existing
  capability-free conversion responsibility, supplying original source
  payload/format instead of extracting source fields from translated data.
  Preserve positive conversion with known metadata and real protocol
  constraints, normalizer/payload-rule ownership, and forced tool choice.
- Preserve all registration metadata, including the existing Chat conversion
  hint, exact selected credential snapshot, aliases/prefixes, routing, OAuth,
  replacement, and refresh lifetimes. No fabricated UserDefined/capability
  claim, broad validator deletion, new user setting, or runtime config change.

Acceptance must include the maintained policy tests, patched HTTP matrix
through real registration/execution, source/auth identity and replacement,
stream/cancellation, and fake-auth subscription HTTP/WebSocket/dialect tests
with external egress rejected. Keep source execution isolated and concurrency
bounded. Use the committed engine catalogs, locked Go modules and exact
toolchain; no catalog refresh from a moving branch.

Release/packaging is deliberately separate. The existing manifest guard only
accepts a truthful upstream identity and verified available four-platform
assets. Do not weaken it or label locally patched bytes as upstream. Local
diagnostic builds are not four-platform production reproducibility evidence.
An upstream acceptance/release or a separately authorized Avibe source/
provenance release contract is required before shipping this engine change.
The Avibe PR may carry the reviewed inactive patch, but must explicitly state
that its current engine pin and installed behavior remain unchanged.

### xAI post-translation capability gate

The orchestrator independently inspected the exact pinned source's
`prepareResponsesRequestTo` chain, `sanitizeXAIResponsesBody`,
`xaiSupportsReasoningEffort`, both existing catalog-strip tests, and the new
fake-OAuth executor consumer and failing wire record. A whole-source search
found one production caller of the capability helper: the sanitizer removes
`reasoning.effort` after shared thinking and payload rules when registry
thinking levels are absent. This is another catalog policy owner, not a
Responses representation constraint. Shared passthrough alone cannot satisfy
the accepted intent contract.

Approve this additional source-only scope:

- Remove only the catalog-conditioned reasoning deletion from
  `sanitizeXAIResponsesBody`. Keep its `stop` removal and every other xAI
  schema, tool-choice, replay, image, normalizer, and payload-rule constraint.
  Preserve the current prepared target fields; do not restore the whole
  original request or override configured payload rules.
- Remove the now-unconsumed private `xaiSupportsReasoningEffort` helper,
  its obsolete assertion-only test, unused imports, and the sanitizer's
  now-unused model argument. No registry or registration policy change.
- Replace catalog-strip assertions with actual executor evidence for unknown,
  known-nil, empty/narrow, and supported metadata populations; native future
  effort and explicit disable remain present, missing native intent remains
  absent, and `stop` still does not reach Responses. Cover cross-protocol
  conversion and stream/non-stream paths without claiming upstream support.
- Reconcile the complete manager/credential-replacement and Kimi fixture
  failures from the same test run independently of this xAI defect. A passing
  xAI case alone is not source acceptance. Check the final egress preparation
  of the other supported executor paths for an equivalent downstream
  catalog-only gate; report another boundary before expanding changes.

This narrowly supersedes the earlier blanket preservation of normalizers
only for the identified catalog deletion. All changes stay in the inactive
exact-base source/test patch. Avibe runtime/config/manifest, release guards,
publication authority, and the installed engine remain unchanged.

### Kimi native representation clarification

The orchestrator inspected the pinned Kimi executor's original/prepared payload
flow, the original Kimi applier and extraction precedence, the current shared
native-format gate, and the failing actual OAuth executor test. An OpenAI Chat
transport can already carry Kimi's native `thinking` object. Treating every
`openai` to `kimi` pair as conversion rejects a native future effort using
catalog levels, although extraction already gives the native object priority.
This is the same representation-identity defect, not evidence to remove
genuine cross-protocol validation.

Approve this bounded source-only clarification:

- With no model suffix, recognize `openai` to `kimi` as native only when the
  original source contains `thinking.type` or `thinking.effort`. A `keep`-only
  object and a legacy `reasoning_effort` input do not establish native intent.
- Preserve the prepared target payload and its native object without restoring
  source fields or interpreting unknown effort/type values. Remove only the
  legacy `reasoning_effort` alias, consistent with the existing Kimi wire writer.
  Native disabled input must not be re-enabled or replaced by the legacy alias.
- Preserve suffix priority and the existing conversion owner for legacy-only,
  keep-only, and genuine cross-protocol inputs. No new interface, metadata,
  configuration, registration, routing, or authentication policy is needed.
- Verify native enabled/future/disabled/type-only input, conflicting legacy
  input, keep-only conversion, suffix priority, and prepared-target authority
  through policy tests and actual streaming/non-streaming executor captures.

The expected result is exact prepared native intent reaching the loopback
upstream, with the legacy alias absent; it is not a claim that a real upstream
supports an arbitrary future value. Any target normalization or another
protocol constraint that makes this preservation unsafe requires evidence and
a new scope decision. The patch remains inactive and the engine pin unchanged.
