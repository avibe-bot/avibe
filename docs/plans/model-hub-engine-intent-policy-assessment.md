# Model Hub engine reasoning-intent assessment

Status: diagnosis and implementation proposal; engine policy and publication
await the orchestrator's decision. No product config or manifest is changed.

Owner: lane D, Session `sesm5hv4yk5c8`, delegated Run `55eef726163d`;
orchestrator: Session `sesvmgbdub2gp`.

## Recommendation

The pinned engine cannot express the requested policy through its existing
configuration. A small source change is necessary. For example, a native
Responses request with `reasoning.effort: none` reaches a mock upstream as
`low` for the pinned `gpt-5.4` catalog row, or as `high` when the configured
levels are `[high, low]`. A custom model without metadata loses the entire
reasoning object. These are actual HTTP observations, not adapter simulations.

Keep Avibe's Source/model registration and identity intact. Change the engine's
shared thinking boundary to distinguish native wire intent from conversion:
preserve native controls independently of catalog metadata, preserve explicit
disable through conversion, and use capability-free conversion when the
selected model has no thinking metadata. Continue using known metadata for
genuine cross-protocol positive-intent conversion.

Maintain the candidate source/test patch against the exact upstream commit,
with a reproducible local build and HTTP fixture. Do not ship an Avibe manifest
change until an authorized, truthful source/release route exists and all
required assets are published and verified.

## Exact inputs and evidence limits

| Input | Identity |
| --- | --- |
| Avibe task base, config writer, guard, and mock inspected | `76e269c9a9dfd3a6286ce0da81b77316ca4be38a` |
| Engine repository | `router-for-me/CLIProxyAPI` |
| Packaged engine version | `v7.2.149` |
| Engine source fetched, read, tested, and compiled | `2a6b87aca083a5bf498ac1f68a1b636c500d7aaa` |
| Go toolchain actually used | `go1.26.4`, matching the pinned release workflow |
| Local build target | `darwin/arm64`, `CGO_ENABLED=0`; diagnostic binary, not a release reproduction |
| Diagnostic binary SHA-256 | `f8b3a7bfe5f8be7d48a1f60506ae579a9e37877696d9f813bf6143f5ed4b5887` |
| Embedded `models.json` SHA-256 | `b19b2655a4f294605d3a347be16e67ef6ea776d70cedbd69c36629f3dbb945d9` |
| Embedded `codex_client_models.json` SHA-256 | `a044aa222836b32091fdf4c9c34030443cb8717dea65db875cf705406c332fd6` |
| Engine `go.sum` SHA-256 | `b29392b1f713b238d6232af2f8fd09e28b7f5d221847ddccf0fc35682a9b29bd` |

The task-owned checkout is
`/tmp/avibe-lane-d-engine-TpeDb7/source`. Its Git tree remained clean after
testing. Existing temporary checkouts were listed and preserved. No installed
engine, engine-internal user state, account credentials, or user Avibe process
was used.

Source references below are relative to the exact engine checkout unless
explicitly labeled Avibe. This assessment does not substitute upstream HEAD,
downloaded release bytes, or a refreshed online catalog for the fixed source.
It does not claim that the currently installed binary was exercised.

## Registration and execution ownership

Avibe `vibe/model_hub_runtime/config.py:67-147` writes the union of discovered
and routed model IDs, preserving `name == alias == upstream model` and each
Source prefix. Nonempty Source reasoning levels are registered strongest-first.
An absent entry emits no `thinking`. OAuth bindings use auth files instead of
YAML API-key values.

The engine has two different model consumers:

1. `sdk/cliproxy/service_models.go:671-790` builds public model registrations.
   Native configured API-key rows are initially `UserDefined=true`; Chat
   compatibility rows are `false` and get synthetic `[low, medium, high]`
   thinking metadata when omitted.
2. `sdk/cliproxy/auth/api_key_model_capabilities.go:112-279` builds and binds a
   private snapshot for the selected credential, alias, and actual upstream
   model. `internal/modelconfig/model_info.go:13-26` resolves the suffix-free
   upstream name against the static catalog, overlays explicit metadata, and
   always sets `UserDefined=false`. Chat compatibility also supplies the
   synthetic level list here. Thus a configured *unknown name* does not take
   the unknown/user-defined thinking path during ordinary API-key execution.

`internal/runtime/executor/helps/model_capabilities.go:18-27` consumes that
selected snapshot. The Claude, Codex HTTP/SSE/WebSocket, and OpenAI-compatible
executors call it after request translation and before final payload rules and
transport. The selected snapshot must remain attached to the exact credential
and upstream model; changing the public registry flag alone cannot repair this.

OAuth attempts ordinarily use the registry lookup path instead of the configured
API-key snapshot. A shared thinking fix therefore needs both paths tested.
No OAuth lifecycle, credential refresh, prefix, alias, model-selection, fallback,
or credential-replacement change is required.

## Pinned policy and why configuration cannot solve it

`internal/thinking/apply.go:196-349` does the following:

- Unknown models (`modelInfo == nil`) and actual `UserDefined=true` rows bypass
  `ValidateConfig` but still go through extraction and a provider applier.
- Known/non-user-defined rows with `Thinking == nil` lose recognized thinking
  controls and summary controls through `StripThinkingConfig`. The comment that
  describes this case as passthrough is inconsistent with the implementation.
- With metadata, it extracts suffix intent first, then original source intent
  for configured models, then translated-body intent.
- `mapConfiguredHighIntent` can map `max`/`xhigh` before validation when the
  format labels differ or the model type differs from the protocol family.
- `ValidateConfig` then normalizes, validates, and clamps, and the provider
  applier can make further representation changes.

`internal/thinking/validate.go:38-195` distinguishes provider *families*, not
identical wire protocols. OpenAI Chat and Responses are one validation family,
but require different field shapes. Conversely, Responses ingress is
`openai-response` and its executor target is `codex`: those label differences
trigger configured high-intent mapping despite native Responses reasoning.
Chat compatibility's model type is `openai-compatibility`, which causes
clamping even on Chat-to-Chat requests.

The validator can reject native out-of-range budgets or unsupported levels;
convert a budget to the nearest registered level; replace `auto`; and change
`none` to `support.Levels[0]` when its flags do not allow disabling. Avibe emits
levels strongest-first, so that last fallback can enable *high* effort.

Empty `thinking: {}` is not a disable policy or a complete bypass. It creates
a non-nil zero-valued support object: native Claude `auto` became disabled in
the baseline, and Claude adaptive `max` became a numeric budget. An arbitrarily
wide list/range would be a capability claim, still change representation, and
fail for future fields/levels.

There is no configuration-exposed `UserDefined` flag. `is-compat` handles
history/signatures or Codex multi-agent compatibility, not this validation.
Payload default/override/filter rules run too late to prevent validation
errors and cannot generically preserve arbitrary caller values. Suffixes
encode an explicit override and still undergo capability policy. Plugin
normalizers cannot reliably bypass the later bound-model check; enabling a
plugin is not a configuration-only repair.

## Complete Model Hub protocol matrix

These tables cover all nine combinations of the three Model Hub wire
protocols. Each cell was measured through both streaming and non-streaming
HTTP ingress into the compiled, unmodified engine. Both variants agreed for
the reported reasoning fragments.

Fixture profiles:

| Profile | Actual model registration |
| --- | --- |
| K | Known supported upstream name, omitted configured thinking: `claude-opus-4-6` for Messages; `gpt-5.4` for Responses/Chat |
| KN | Known static row with nil thinking: `claude-3-5-haiku-20241022`; using it on non-Claude mock routes deliberately tests static lookup, not real vendor model support |
| U | Unknown configured upstream name `lane-d-custom`, omitted thinking |
| L | Unknown name `lane-d-narrow`, explicit fixture levels `[high, low]` |
| E | Unknown name `lane-d-empty`, explicit empty `thinking: {}` |

For Chat targets, omitted thinking in K/KN/U becomes the engine's synthetic
`[low, medium, high]`. These are diagnostic fixtures only, not proposed
registration claims.

### Positive explicit intent

Messages input uses adaptive `output_config.effort: max`. Responses and Chat
inputs use their native `xhigh`; Responses also requests `summary: auto`.
`A(max)` means adaptive Messages thinking and `output_config.effort: max`;
`R(xhigh)` and `C(xhigh)` mean native Responses/Chat effort fields. `B(n)`
means Messages `enabled` with `budget_tokens: n`. `S` means thinking controls
stripped. `400` means no upstream request.

| Ingress -> upstream | K | KN | U | L | E |
| --- | --- | --- | --- | --- | --- |
| Messages -> Messages | A(max) | S | S | 400 | B(63999) |
| Responses -> Responses | R(xhigh) | S | S | R(high) | R(xhigh) |
| Chat -> Chat | C(high) | C(high) | C(high) | C(high) | C(xhigh) |
| Messages -> Responses | R(xhigh) | S | S | R(high) | R(max) |
| Messages -> Chat | C(high) | C(high) | C(high) | C(high) | C(max) |
| Responses -> Messages | A(max) | S | S | A(high) | B(31999) |
| Responses -> Chat | C(high) | C(high) | C(high) | C(high) | C(xhigh) |
| Chat -> Messages | A(max) | S | S | A(high) | B(31999) |
| Chat -> Responses | R(xhigh) | S | S | R(high) | R(xhigh) |

Cross-protocol requests into Messages carry `thinking.display: summarized`
when the source summary semantics request it. Responses targets preserve the
source summary where represented. The numeric budgets above also reflect
Claude's post-conversion `max_tokens` relation; they are not metadata-neutral
passthrough.

### Explicit disable

Messages input uses `thinking.type: disabled`; the other inputs use native
`effort: none`. `D` means Messages disabled; R/C show the actual effort.

| Ingress -> upstream | K | KN | U | L | E |
| --- | --- | --- | --- | --- | --- |
| Messages -> Messages | D | S | S | D | D |
| Responses -> Responses | R(low) | S | S | R(high) | R(none) |
| Chat -> Chat | C(low) | C(low) | C(low) | C(high) | C(none) |
| Messages -> Responses | R(low) | S | S | R(high) | R(none) |
| Messages -> Chat | C(low) | C(low) | C(low) | C(high) | C(none) |
| Responses -> Messages | D | S | S | D | D |
| Responses -> Chat | C(low) | C(low) | C(low) | C(high) | C(none) |
| Chat -> Messages | D | S | S | D | D |
| Chat -> Responses | R(low) | S | S | R(high) | R(none) |

### Absence and additional native shapes

No native reasoning input remains absent on all three same-protocol routes,
for every profile. Cross-protocol Messages/Chat -> Responses injects the
translator's `medium` default before thinking policy. K/E retain it; KN/U
strip it; L maps the Messages-origin default to `low` but rejects the
Chat-origin default with 400. All other absent-input cross routes remain
without reasoning controls. Preserve this existing conversion/default
behavior unless separately authorized to change it.

Additional measured native cases:

- Messages K accepts budget 4096, rejects budget 256, and changes enabled
  thinking with no budget to budget 63999. L maps both budgets to adaptive
  `low`; E changes enabled-without-budget to disabled. KN/U strip all of them.
- Responses K maps `auto` to `medium`; L maps it to `low`; KN/U strip it.
  Chat K/KN/U map `auto` to `medium`; L maps it to `low`.
- Future explicit effort `ultra` produces 400 in K/L for Messages/Responses
  and K/KN/U/L for Chat. KN/U Messages/Responses strip it. E forwards it.
- Summary-only Responses is stripped for KN/U and retained for K/L/E.
  Messages adaptive `display: omitted` is stripped for KN/U and retained for
  K/L/E. Native Chat `reasoning.exclude: true` survives these fixtures.

### Unknown/user-defined versus configured-unknown source paths

The HTTP matrix intentionally exercises real config registration. The
following separate source behavior must not be mislabeled HTTP coverage:

| Selected model info | Same-protocol policy | Cross-protocol policy |
| --- | --- | --- |
| `nil` model info | No catalog validation; extract/apply native controls, so not guaranteed byte preservation | Translate then apply capability-free representation; original source handling differs from the configured path |
| Actual `UserDefined=true` | Same as nil, even if metadata exists | Same unvalidated branch; provider appliers still convert and rewrite fields |
| Configured unknown, `UserDefined=false`, `Thinking=nil` | Strip recognized controls | Strip translated controls |
| Non-user-defined with levels/range | Validate, map, clamp, or reject | Convert plus validate/map/clamp |

`applyUserDefinedModel` receives the already translated body and does not
receive `sourceBody`; its extraction also lacks the `openai-response` alias
handled by `extractSourceThinkingConfig`. Merely redirecting every configured
row into that branch can therefore lose source intent or rewrite a compatible
native field. Existing upstream conversion tests use synthetic registered
user-defined rows; their behavior does not prove configured API-key behavior.

### Subscription executor qualifications

- Claude OAuth reaches the Claude shared request path; Codex OAuth reaches the
  Codex HTTP/SSE/WebSocket paths. Same-protocol policy must apply to their
  registry-selected models without changing credential handling.
- Kimi selects Claude Messages when ingress is Claude, delegating to
  `ClaudeExecutor`; otherwise it uses Chat transport but its thinking applier
  converts legacy `reasoning_effort` into native `thinking.type/effort`.
  Do not equate this Kimi dialect with ordinary Chat reasoning based only on
  the HTTP endpoint. Its hardcoded origin requires a rejecting test transport
  mapped to loopback for source tests.
- xAI uses the Responses representation and embeds the Codex thinking applier,
  while passing `xai` as its thinking format. Its Responses wire alias must be
  assessed explicitly, not inferred from the executor identifier.
- Gemini/Antigravity/Interactions are not Model Hub's three public protocol
  choices. Keep their existing conversion behavior; run their existing
  conversion regression tests for shared-code changes.

No subscription/OAuth wire fixture was run in this assessment. Those tests
remain required before declaring the source patch complete.

## Minimal source and registration plan

Implement only after the orchestrator records approval of this scope:

1. In the shared thinking entry point, distinguish identical reasoning wire
   representations from provider families. Messages->Messages and Chat->Chat
   preserve native controls; `openai-response`/`codex` (and the verified xAI
   Responses target) share the Responses reasoning representation. Chat and
   Responses do not. Keep Kimi's native conversion explicit.
2. For same-representation requests without an explicit model suffix, return
   the current target payload without catalog-driven extraction/reapplication,
   stripping, level mapping, or budget validation. Preserve normalizers'
   deliberate changes: do not restore the whole original request or undo
   model, protocol, credential, payload-rule, or signature handling.
3. Extract explicit original-source disable before capability gating. Keep
   suffix precedence. An explicit `ModeNone` must reach the provider's native
   disable representation without falling back to a catalog level or being
   stripped. Reuse the existing provider disable writers, moving this case
   before their capability guards as necessary. Skip summary activation after
   explicit disable. This addresses all nine disable rows, not only native
   requests.
4. For genuine conversion with no thinking metadata, reuse a clearly named
   unvalidated conversion path and provider-compatible writers. Pass the
   source payload/format into it so source intent is not re-extracted from the
   wrong target shape. A nil support pointer is missing knowledge, not an
   instruction to delete caller fields. Do not forge `UserDefined=true` or a
   broad `ThinkingSupport` merely to reach this path.
5. Keep known-metadata positive cross-protocol conversion, summary visibility,
   protocol constraints (including forced Claude tool choice), and suffix
   representation logic. A native passthrough does not disable executor
   protocol checks or imply that an upstream will accept every request.

Expected engine edit surface: `internal/thinking/apply.go`, the relevant
provider appliers for explicit disable/missing metadata, and focused tests.
`validate.go` remains the conversion validator; if disable handling needs
adjustment there, distinguish explicit disable before generic normalization.
The later implementation must verify this boundary with tests rather than
assuming an early return alone closes every consumer.

Registration policy: leave Avibe's real `model_reasoning_efforts`, Source
prefixes, aliases, routed IDs, and persisted shapes unchanged. Do not synthesize
levels, flags, ranges, or capability claims. Do not remove existing metadata.
The upstream's existing Chat fallback remains a conversion hint in this
minimal proposal; removing that fallback from both registration owners is a
separate scope decision, not necessary to preserve native requests. It must
not become a native-request allowlist. No new persistent provenance model,
config toggle, plugin, or custom-model flag is proposed.

## Concrete validation route

Baseline completed on 2026-09-09 +08:

- Existing `internal/thinking/...` and `internal/modelconfig` tests passed.
- Existing `test` package cases matching `^TestThinking` passed, including
  body/suffix, user-defined, adaptive Claude, and other provider conversions.
- The unmodified `cmd/server` compiled with locked modules.
- 380 requests traversed real engine HTTP ingress, registration/auth selection,
  translators, executors, and a real loopback mock upstream. 358 returned 200
  with exactly one capture; 22 returned 400 before upstream admission. These
  counts describe pinned behavior, not acceptance success.
- Every captured request had the expected endpoint, exact upstream model,
  selected Source's fake outbound key, and no inbound gateway bearer. No
  request reached a different Source mock. Every input included non-ASCII
  text. Raw reasoning fragments were compared across stream modes.

The task-only harness is
`/tmp/avibe-lane-d-engine-TpeDb7/diagnose_wire.py`; raw results are
`/tmp/avibe-lane-d-engine-TpeDb7/baseline-wire.json`
(SHA-256 `8d31f89e6cb9167c7056e9acbc5539efc5c978e02ebbb9d524370e1cc96021ad`).
The harness SHA-256 is
`2d9855ac52194e7164ef0541b3bd8001f0d8e094cfd4316569efa7afdbdc64d3`.
Preserve this task-owned evidence for the implementation continuation; it is
not a maintained test entry point yet.

Hermetic construction:

1. Fetch only the exact engine commit into a new task directory. Give Go its
   own HOME, TMPDIR, module/build caches, and `GOENV=off`. Use the exact
   toolchain and `-mod=readonly`; dependency downloads are preparation, not
   upstream account probes.
2. Reuse Avibe's stdlib-only
   `tests/e2e/drivers/mock_llm_upstream.py`, with one ephemeral loopback
   listener per protocol. Generate fake-key config in a test-owned home.
   Spawn the newly built binary, never the installer or managed live runtime.
3. On this macOS host, the baseline used `sandbox-exec` to deny all outbound
   networking except localhost, deny filesystem writes except the task
   directory and `/dev`, and deny reads of the user's Avibe/backend homes.
   Give the child an explicit minimal environment and distinct XDG dirs.
   `--local-model`, disabled panel updates/plugins, and a dead loopback
   HTTP(S) proxy supplement that boundary. The real Antigravity version
   updater attempted only the dead loopback proxy; no external request ran.
   `--local-model` alone is insufficient.
4. For portable CI, use the existing SDK/Go test pattern with the actual auth
   manager, configured registration, and real executor, plus a rejecting
   transport that permits only test-owned listeners. Start the actual HTTP API
   for the end-to-end layer. Alternatively run the real binary and mock in a
   test-owned network namespace with loopback only. Do not rely on fake
   adapter tests or a proxy environment variable alone.
5. Assert native reasoning fragments, required conversion results, explicit
   negatives, no intent injection on native absence, correct model/prefix/
   credential replacement, non-ASCII preservation, response streaming, and
   cancellation. Stop and reap the child and all mock threads in `finally`.

After approval, promote the diagnostic matrix into maintained engine tests:

- Shared policy tests for nil model info, user-defined, known nil support,
  explicit empty/narrow/ranged support, source/target aliases, suffix-vs-body
  precedence, summary visibility, `none`, `auto`, unknown future levels,
  budget 256/4096, and missing intent.
- Actual manager+executor tests with two credentials advertising the same
  alias/upstream name but different metadata. Require exact selected model
  info and auth, including API-key replacement. Existing fake executors are
  supplementary assertions, not wire evidence.
- HTTP matrix above for both stream modes, plus suffix overrides and truthful
  configured negative support fixtures. Existing static nil support and an
  explicit caller disable remain distinct.
- Subscription tests use fake auth data and injectable loopback transports.
  Include Claude, Codex HTTP/WebSocket, Kimi dialect selection, and xAI; reject
  all non-test destinations. Do not execute login, refresh, browser, or cloud
  account flows.
- Then build `cmd/server`, run focused translator/conversion tests and
  registration/auth tests, and repeat the HTTP matrix on the patched binary.
  Avibe integration subsequently uses generated Source registrations rather
  than a manually inflated capability fixture.

Resources measured so far: approximately 1.6 GiB of task-owned source,
toolchain, dependencies, and cache; an 81 MiB unstripped diagnostic binary.
Build/test concurrency was `GOMAXPROCS=2`, `-p=1`, with a 1536 MiB soft Go
memory limit. The wire child used two Go threads and a 512 MiB soft limit.
The Go limits are not OS memory caps. Build all four release targets
sequentially or on bounded native runners, not concurrent local full builds.

## Maintained source, reproducible builds, and publication order

There is no authorized public fork or published patched source today. The
smallest maintained candidate home is an Avibe-owned source patch series plus
tests/recipe under a proposed `patches/cliproxyapi/` directory, recording the
exact upstream base, patch digest, dependency lock, and build-input receipt.
Apply it only to a fresh task-owned upstream checkout. The temporary checkout
is execution state, not the permanent source of truth. A public upstream
contribution may later become the maintained source home with authorization.
Do not silently create or select another organization's repository.

Reproducibility requires more than `source_sha`:

- Pin Go `1.26.4`, `go.sum`, the exact base plus patch, OS/architecture, CGO
  setting, compiler/SDK or Linux build-image digest, build flags, and metadata.
- Freeze both model catalogs. The pinned upstream release workflow runs
  `.github/scripts/refresh-model-catalogs.sh`, which fetches
  `router-for-me/models` **main** before compilation. Therefore the source
  SHA alone does not reproduce upstream release catalog bytes. Use the
  committed catalog bytes for the candidate, or pin a separately recorded
  catalog commit and hashes. Never silently refresh HEAD during acceptance.
- Use `-trimpath`, a fixed commit-derived build date, and deterministic archive
  entry order/owners/mtimes and gzip metadata. Repeat builds in two fresh
  directories and compare binary/archive hashes before claiming bit-for-bit
  reproducibility.
- Match Avibe's four platforms: Darwin arm64/amd64 and Linux amd64/arm64.
  The existing release uses CGO-enabled hosted builds and GLIBC 2.17-baseline
  Linux assets. The CGO-disabled diagnostic build does not prove those asset
  contracts. Preserve the selected production build variant unless a separate
  platform/packaging decision changes it.

Release dependency order:

| Stage | Required result | Authority |
| --- | --- | --- |
| Source/tests | Reviewed local patch, exact build receipt, passing wire matrix | Orchestrator source-scope approval; no publication implied |
| Source provenance | Either accepted upstream commit/release, or an explicitly authorized maintained-source and provenance contract | External submission/source-home decision; not authorized in this lane |
| Release assets | All four verified archives, extracted-binary hashes, sizes, sidecars, matching manifest copy | Explicit tag/release/asset publication authority |
| Availability | Read back published bytes; run manifest-keyed verification and preserve recoverable backup | Authorized release workflow; guard remains enforced |
| Avibe dependency pin | Update packaged manifest and frozen tests to the actually available, truthful release | Orchestrator integration/normal PR gates after asset availability |
| Installation/runtime | Existing lifecycle converges installed dependency and later starts/replaces the managed process | Separate owner authorization; not part of source implementation |

Avibe's guard (`scripts/model_hub_engine_release_guard.py:94-185`) requires
`source == router-for-me/CLIProxyAPI`, the matching upstream source URL, a
matching `vX.Y.Z` release tag, four exact asset names, and Avibe-owned release
URLs. `fetch-source` verifies upstream release bytes against the manifest;
the scheduled workflow backs up and restores only the exact manifest's bytes.
It is not a local-patch build or source-provenance system.

The compatible delivery route without changing this guard is an authorized
upstream contribution/release followed by verified mirroring into an
Avibe-owned release and then an Avibe pin update. If shipping an Avibe-only
patch becomes necessary, source identity/build provenance and maintenance
ownership require a separate explicit decision and equally strong guard
contract. Relabeling patched bytes as upstream, inventing a fork URL, weakening
verification, or pinning assets before publication is not an option.

This is a real release-authorization/provenance blocker for shipping, not a
blocker to approved local source/test implementation. This assessment does not
declare delivery gates passed or claim the Model Hub issue is fixed.
