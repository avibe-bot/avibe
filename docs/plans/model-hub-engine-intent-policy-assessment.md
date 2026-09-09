# Model Hub engine reasoning-intent assessment

Status (2026-09-09): maintained inactive source/test candidate independently
source-accepted on Linux arm64. The second-round recipe passes 361 deny-network
pure consumers, pinned Ruff 0.4.9 and source/document syntax checks. Following
independent source inspection and harmless kernel probes, the lane and then
the orchestrator each ran one complete sequential test/build/wire invocation
of the unchanged recipe. All three Go commands, diagnostic build, full wire
matrix and original isolation/cleanup evidence passed in both runs.
The current identities and trust limits are recorded below; historical macOS,
watcher-only failure and earlier-recipe receipts remain historical.
Exact-head Codex/CI/thread gates and final assessment inspection are still
required. Shipping additionally requires separate source-provenance and
publication authority. No Avibe runtime code, config, manifest, guard, or
workflow changed; the currently pinned/installed engine behavior is unchanged.

Owner: lane D, Session `sesm5hv4yk5c8`;
orchestrator: Session `sesvmgbdub2gp`.

Delivery: the owner's independent-PR decision at `752c1def8` supersedes the
earlier no-lane-push/combined-PR plan. This lane submits one Avibe PR containing
only its inactive source/test candidate and assessment, plus the shared
root-owned contract. The orchestrator independently verifies exact-head review,
CI and source acceptance, and alone executes any gated merge. An Avibe PR merge
does not ship the engine change or authorize engine publication.

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

## Second reviewed-head recipe correction

The same orchestrator independently diagnosed the full history before
authorizing repair: two findings-bearing heads, with repeated
execution-input provenance and isolation-ownership classes. The circuit
breaker paused edits/pushes; the recorded scope decision released only this
one coherent recipe/test/documentation correction and deny-network pure tests.
No engine thinking, registration, auth, payload, normalizer or suffix policy
change is part of this round.

The existing owners remain:

| Boundary | Owning correction |
| --- | --- |
| Execution inputs | Verify the actual full Go extraction against the pinned archive before Go runs; bind archive/tree/selected executable, source, fixture and recipe identities before/after. Measure the existing venv/lock and explicitly label guest Python/OS/util-linux and setup-only uv trust, not unverified provenance claims. |
| Invocation outputs | Parent reserves one new output directory using its existing receipt identity. Verifier owns exclusive logs/binary/candidate records there; wire explicitly selects and verifies a prior build mounted readonly. Shared caches are not a mutable latest-artifact pointer. |
| Writable roots | One canonical pre-write validator protects broad/home/protected paths across environment, exporter, verifier and wire consumers, including sudo's invoking user. The privileged CLI supports only narrow canonical `/tmp` or `/var/tmp` children and keeps hidden-home probes strict. |
| Privileged entry | Public CLI always runs `unshare`; no internal flags select re-entry. A fixed child trampoline receives an unlinked parent-owned control descriptor and checks all actual kernel namespace identities before the first mutation. Original preflight/terminal custody remains separate and closes before candidate launch. |
| Deadlines | One derived phase budget is consumed by command executor, parent and caller. Three permitted sequential Go commands fit the complete test envelope, with bounded input/preflight/setup/cleanup allowances and failure/timeout evidence. |

The detailed verified/measured/trusted/setup-only inventory is maintained in
`patches/cliproxyapi/README.md`. In particular, a frozen lock plus a measured
venv is not archive attestation of Python distributions, and shared Go caches
retain the existing Go trust model rather than a new production provenance
contract. Candidate artifacts remain supplementary evidence; only the
parent-owned receipt records trusted preflight and cleanup.

Pure consumers cover the original entry points, substituted compiler and
runtime/tool files, canonical path aliases, repeat test/build/wire runs,
output collisions, partial failures, stale build selection and fake-clock
deadline propagation. Kernel probes, complete source execution and independent
rerun of these revised bytes have now passed independently; acceptance is
bound to the unchanged complete recipe below, not inherited from the
successful historical runs. Publication remains separately blocked.

## Current second-round Linux source acceptance

On 2026-09-09 the lane completed `fullphase-sequence-2m05b_t9`. After reading
all original evidence independently, the orchestrator ran the same inspected
driver once more as `fullphase-sequence-lhboam7u`, from
06:21:31.223856Z to 06:22:52.369652Z. Both executions were sequential
test -> build -> wire, with no retry or cache reset. The orchestrator then
read all 66 original text files, including every parent/preflight/invocation
and phase log, the complete 456-line/58,205-byte engine log, full before/after
inputs, hashed output inventories, selected build and lifecycle evidence.
Exit codes or summary counts alone were not the acceptance criterion.

| Current accepted input | Identity |
| --- | --- |
| Maintained commit at execution | `7ac2386d1bc9c413ff02a4fe70778efa3d465d47` |
| Complete 21-file recipe Git tree | `5b6d733fa51d14489bc8889be4bab9bad248a643` |
| Tracked recipe source SHA-256 | `7cceaf71c292b525eaab3df38cff68184967da8ed797f989cc93a47f3c0db2fa` |
| Inspected sequential driver SHA-256 | `79fca450bdde28be11958d8c53c2a9b1066f020c261d4fb83e3d639303b45670` |
| Engine base commit | `2a6b87aca083a5bf498ac1f68a1b636c500d7aaa` |
| Complete patched engine source SHA-256 | `c49da0017dc015030cd8f460e3288381765bf1283fc49478af657f389c265fb2` |
| Unchanged native patch SHA-256 | `f55a3731e82837ed92d8943e97ef6d6521d21a78d3026a65c97b0950c3c494be` |
| Frozen Avibe fixture commit / tree | `76e269c9a9dfd3a6286ce0da81b77316ca4be38a` / `27f32c68b3b9c6b1d40fcde3a034838b58ca0636` |
| Frozen fixture archive SHA-256 | `3352c66c7b88c35ce7851a4bcfbeb3ac2e6d842199ef7dcf7c93534c0233c4cc` |
| Complete frozen fixture source SHA-256 | `61c9616c760f9bbe4750c507073deea183064ec7f55dbacc2174eac9c474031e` |
| Observed Go version / build target | `go version go1.26.4 linux/arm64`; `linux/arm64`, `CGO_ENABLED=0` |
| Verified Go archive SHA-256 | `ef758ae7c6cf9267c9c0ef080b8965f453d89ab2d25d9eb22de4405925238768` |
| Complete extracted Go tree SHA-256 | `494cfe7e2731c4125a75ddc194acfd3902657e1da9c5e846d63960aa78081f4a` |
| Actually selected Go executable SHA-256 | `299613d26b2fc429f9b1a8dbbc6f8952f7e0fb7b5b624c63ff0442a15233eaad` |
| Measured frozen-lock venv tree SHA-256 | `690bc736ecde2f7129dd1da41893b8ed31d287265e7cfd670084be369442df3b` |
| Actual verifier normalized input SHA-256 | `e65b6372725d6a2dc98ac4de8cef65eacacfcc792f97f7930734fb14911ef53b` |
| Complete observer normalized input SHA-256 | `9b9163a9f7d224893da522dd3ef1eff3782df510740687dcb2b305d23d4000a3` |

The verifier object excludes only the observer-only
`setup_only.verified_existing_uv_archive_sha256` fact. Both objects are
preserved: the observer object is not emitted candidate provenance, and
neither an execution nor extraction of uv is claimed. All six observer
before/after files share raw SHA-256
`3d393e7d6b1ee2ae96a959356f9d9c8f07627a9ed7db19e7a48597669c268fb2`;
all six candidate files share
`45a70b0d0a11425bbdfa894da77a4d99838392fe8b126fcb30c4c80b583f161e`.
The committed catalogs and engine `go.sum` retain the identities in the
historical input table below.

| Independent root-run evidence | SHA-256 |
| --- | --- |
| Original test parent receipt | `dda6346209fefd8e1d9c5d1de88bf752328d0f60505bfa4a32c6d8d18104ee24` |
| Original build parent receipt | `85abcee12ab40b801178d006707172c72fec2863013589a6e4b21cee3c60a077` |
| Original wire parent receipt | `71bcb0ce7a54bd4c1abebee601acab3de969916fa84db2497432902274a7caf0` |
| Linux diagnostic binary, 79,233,112 bytes | `638de6ce30cfb1c0c7f36a29ff60c41361ac4dad8f86d134076cbe84f7667ea4` |
| Selected build's candidate receipt | `e32e0133269819cd03c42e7c6c4702d24a743cc22cb740b682ac59aa93ce4f55` |
| Selected build artifact inventory | `9c774681d31b69988b6fd8743458af068edcee9a4f745c13564e13400843e432` |
| Complete selected build tree | `6adc585281f813ad85f87c1160d39d8a291f292790a68e94ad6a989301f7ae5a` |
| Wire candidate receipt | `8c25dcd7e6f7cc139cf4cff2c1fd8bd1cf5ec4ab8e9005fd05a185b5ad8cf4e5` |
| Complete 380-row policy matrix | `71b15e16af5cde8f022bb51dc41507506f6a9d93e8e75830cd927f6e8f26fa15` |
| Complete long-identity / image records | `cb807fd737c5294cc52c101a5a2805c96b661e0e45f9c8e458b8ef15c25a0ccb` |
| Actual frozen lifecycle records | `92ac48c264657d7de8bdb9881a9879db6161a4934baa4c1849963fc2df8cdfb0` |
| Complete engine log | `568445ac60dc48a428b7a202c3737746b4d27daae24a132527da06690d70d278` |
| Supplementary complete root readback bundle | `223850d5f86ff7ab93e25175062f76c3afce37f57e7d38a02cdb3c837e5ffb15` |

Wire selected only its own sequence's successful build, binding the original
parent receipt, invocation, full input identity, binary and complete artifact
tree before and after consumption. Parent receipts remain original root-owned
mode-0640 single-link regular files. Supplementary artifacts were read
unprivileged through anchored, no-follow, regular, single-link, bounded reads;
binary contents were hashed without export. They do not replace parent custody.

Every phase recorded actual UID 501/GID 1000, all five capability sets zero,
`NoNewPrivs=1`, distinct mount/network/PID namespaces, readonly original
inputs and exclusive invocation output. Wire's selected build was readonly.
Each preflight recorded all twelve blocked sentinel attempts: test/wire had
both private-loopback positives and errno 111 negatives; network-none build
had IPv4 errno 101/IPv6 errno 99 and no positives. Outside receives were
`[0, 0]`. Full inputs, outer mounts and boot ID were unchanged. Original and
fresh cleanup found no surviving observed process identity, private PID
namespace member or rootfs; unavailable privileged namespace links were not
invented from unprivileged observations.

Actual records reconcile 380 unique policy cases and 378 successful captures.
The two HTTP 400 cases are only Chat -> Responses, narrow profile, absent
intent, stream off/on, with zero upstream requests. All 24 complete Unicode
identities pass, including distinct long shared-prefix tails. Three sequential
image requests of 44,051,709 / 44,051,604 / 44,051,581 bytes preserve four
distinct images and non-ASCII text. All five frozen lifecycle assertions pass:
invalid Source/no restart, failed replacement rollback/recovery, committed
stream/nonstream replacement, cancellation/reuse and failed-startup cleanup.
Child outcomes are exactly `[0, 23, 0, 0, 23]`; the two injected exit-23 failures
are required rollback/cleanup evidence, not successful starts.

Remaining trust limits are explicit. Existing guest OS/kernel/Python/util-linux
are trusted prerequisites; a measured venv and frozen lock are not an
archive-attested Python distribution. Copied Go caches remain mutable trusted
setup under the locked-module/offline model; the root reused their expected
post-lane state without resetting or claiming seed equality. Three optional
Antigravity updater attempts were refused at private proxy `127.0.0.1:1`,
not successful external requests. The engine-only empty-object profile remains
a test supplement, not generated product capability. Identical diagnostic
binary hashes across these two runs do not establish cold-cache or
four-platform production-release reproducibility.

The separate 338-product-consumer private-loopback run belongs to historical
head `7e4412c94c54b2fc7855b9b29f9a317d1bc94f82`, not an executed final-head
test. Its runtime Python, four runtime/release-guard/catalog/live-resolution
test files and dependency locks remain unchanged. Finalization integrates only
landed master and updates this assessment; it does not repoint the frozen
fixture. The landed presentation row `MH-SRC-DELETE-002` is checked by the
pure catalog consumer, with no peer UI edits or new scenario allocation.
Fresh exact-head CI and Codex review remain mandatory.

## Completed source-only implementation

The source-only decision was recorded at integration `a3065a7d1`; the later
xAI and Kimi clarifications were read from integration heads
`3d0cba9e622fe3e4af706bb9073684e0b3d9b038` and
`871ce9d94f9f9c02e9cf6c8d8b5e205949bf8727`. The lane does not author edits to
the shared plan; the independent-delivery directive authorizes replaying the
six specified root-owned documentation commits into this Avibe branch.

The maintained home is `patches/cliproxyapi/`. Its patch includes the Go tests;
the small apply/test recipe validates the exact base, frozen catalogs, locked
modules, Go version, changed-file inventory and file hashes. The revised recipe
also binds the complete immutable Avibe fixture and imported recipe identity.
The fixture is an exact tracked commit export, not mutable current-worktree
imports; dirty exports fail before and after execution. A build receipt
binds the wire-tested binary to those inputs and the patch digest. It refuses
dirty source on apply, unrelated candidate edits, stale binaries, and hosts
without the verified egress-isolation mechanism.

Implemented ownership:

- Shared native-representation passthrough preserves the prepared target,
  without suffix, for Messages, Chat and verified Responses aliases.
- Kimi native fields inside a Chat envelope are detected from original
  `thinking.type`/`thinking.effort`; only the legacy effort alias is removed.
  Future values, type-only input and prepared-target normalization survive.
  Keep-only/legacy-only input still uses conversion, and suffix wins.
- Explicit disable is applied before capability gating/summary activation.
  Unknown/user-defined and configured missing-metadata conversions receive
  the original source payload. Known positive conversions still validate.
- xAI's post-translation catalog-only effort deletion is removed, along with
  its single-use helper. `stop` removal, payload overrides/filters, tool choice,
  schemas, replay and image handling remain. A focused final-egress scan of
  Claude/Codex/OpenAI-compatible/Kimi found no equivalent additional catalog
  gate; Claude's forced-tool-choice removal remains a tested protocol rule.
- Exact selected-credential model snapshots and every registration producer
  are unchanged, including the existing Chat conversion hint. No capability
  or `UserDefined` claim is fabricated.

### Patched three-protocol positive-intent matrix

The K/KN/U/L/E profiles and notation are defined in the baseline section
below. Both stream modes agree in all cells. E is a supplementary engine-only
empty-object registration, not a shape fabricated by Avibe's writer.

| Ingress -> upstream | K | KN | U | L | E |
| --- | --- | --- | --- | --- | --- |
| Messages -> Messages | A(max) | A(max) | A(max) | A(max) | A(max) |
| Responses -> Responses | R(xhigh) | R(xhigh) | R(xhigh) | R(xhigh) | R(xhigh) |
| Chat -> Chat | C(xhigh) | C(xhigh) | C(xhigh) | C(xhigh) | C(xhigh) |
| Messages -> Responses | R(xhigh) | R(max) | R(max) | R(high) | R(max) |
| Messages -> Chat | C(high) | C(high) | C(high) | C(high) | C(max) |
| Responses -> Messages | A(max) | A(xhigh) | A(xhigh) | A(high) | B(31999) |
| Responses -> Chat | C(high) | C(high) | C(high) | C(high) | C(xhigh) |
| Chat -> Messages | A(max) | A(xhigh) | A(xhigh) | A(high) | B(31999) |
| Chat -> Responses | R(xhigh) | R(xhigh) | R(xhigh) | R(high) | R(xhigh) |

Explicit disable now reaches D, R(none), or C(none), respectively, in every
cell of all nine directions and five profiles. Native absent reasoning
remains absent; native future levels, auto, summaries and budgets preserve
their prepared representation. Cross-protocol defaults remain conversion
ownership: Messages/Chat -> Responses inject `medium`; the narrow profile
maps Messages-origin default to `low` and still rejects the Chat-origin
default. Missing catalog metadata no longer strips the translated default.
There is no new native default or upstream capability promise.

Unresolved model info and actual `UserDefined=true` also preserve native
prepared payloads, as directly asserted in policy tests. Their genuine
conversions retain capability-free conversion, now source-aware. Configured
unknown rows remain `UserDefined=false`: the missing-metadata branch is an
explicit boundary policy, not a changed registration claim.

### Historical macOS and earlier-recipe evidence

This section preserves superseded validation stages; current second-round
Linux acceptance is recorded separately above.
The initial results came from the earlier macOS wildcard-loopback envelope.
They are source behavior observations, not sufficient evidence of test-owned
listener isolation or the newly enforced fixture receipt closure. The Linux
Go/build rerun passed. Wire passed all 380 policy, 24 identity and three image
cases before its watcher-only replacement failed; cancellation/reuse did not
execute. These partial results are not complete wire acceptance.
The corrected lifecycle rerun subsequently passed all three phases, including
all original matrix/identity/image cases, real child replacement and failed
replacement rollback/recovery, invalid Source refusal, cancellation/reuse and
failed-startup cleanup. Each phase's original parent receipt has twelve blocked
outside attempts, zero sentinel connections, unchanged complete inputs/outer
mounts, and reaped processes/removed rootfs. Three normal children exited zero;
the two deliberately failing children exited 23 as asserted by the consumer.
The policy and integration artifacts retain their original complete hashes.
The revised fixture/parent/probe/connection pure suite passes 225 cases under deny-all
network. Actual no-network and private-loopback probes pass with zero outside
sentinel connections; a deliberate nonzero command retains a failed terminal
receipt. Original failed probe evidence remains preserved. These probes are
not full engine acceptance or a shipped repair.

- The maintained Go recipe passes shared thinking/provider tests,
  `internal/modelconfig`, selected-model helpers, `sdk/cliproxy/auth`, existing
  thinking/summary conversion cases, and the full executor package.
  Superseded strip/clamp/native-rejection assertions were replaced explicitly;
  genuine positive conversions, Gemini-family constraints and suffix tests
  remain. No tests were skipped to get this result.
- The real HTTP matrix has 380 cases: 378 successful one-capture requests and
  two expected conversion-only 400s with zero upstream requests. Full endpoint,
  upstream model, selected fake key, non-ASCII content, reasoning fields and
  cross-Source exclusion are checked. Generated Avibe registration, origin/key
  replacement via the former config-watcher fixture, streaming, cancellation
  and reuse passed only in that historical envelope.
- Actual manager + Claude HTTP tests distinguish two Sources with the same
  alias/upstream name but different metadata, then replace one key/snapshot.
  Fake subscription tests cover Claude/Codex HTTP, Codex WebSocket, Kimi
  Messages delegation, Kimi native/legacy Chat and xAI Responses. xAI's final
  egress matrix has 80 cases; Kimi's native/legacy matrix has 40. They use
  rejecting transports, not provider accounts; the old OS loopback wildcard
  is not evidence of test-owned listener enforcement.
- 24 additional actual-engine requests preserve full long UTF-8 identities
  beyond 16 KiB, including identical long heads with distinct tails and a
  route-only model, on all three API-key protocols and both stream modes.
  Registration and egress assertions compare the full strings/UTF-8 bytes.
- Three sequential ~42 MiB native requests, each containing four distinct
  valid synthetic RGB PNGs, preserve every base64 image and the non-ASCII
  text. Only the existing mock's receive budget is raised to 48 MiB during
  these tests. Normal envelope/cache transformations remain allowed; exact
  image/text data is compared independently. This closes the engine seam,
  not a duplicate gateway-framing test.
- The patch applies cleanly to a second exact-base local checkout. A second
  diagnostic build there produces the same binary digest; both builds use the
  same isolated caches, so this is not a cold-cache or production release proof.
  All test
  engines and mock threads are stopped/reaped; no installed engine or backend
  was started. The source-only PR/CI/review delivery gates and production
  release builds are not claimed here.

Maintained commands are the `apply`, `test`, `build`, and `wire` phases in
`patches/cliproxyapi/README.md`. Evidence stays in task-owned scratch, not
committed developer-machine paths. A successful mock capture proves
preservation/routing, not that a real provider accepts a future effort value.

## Historical macOS diagnostic inputs and evidence limits

The Darwin binary hashes in this table belong only to the initial macOS
diagnosis and candidate. They are not the current Linux build identities.
The frozen engine base, patch, fixture and catalog declarations remain inputs
to the separately recorded current acceptance.

| Input | Identity |
| --- | --- |
| Avibe task base, config writer, guard, and mock inspected | `76e269c9a9dfd3a6286ce0da81b77316ca4be38a` |
| Engine repository | `router-for-me/CLIProxyAPI` |
| Packaged engine version | `v7.2.149` |
| Engine source fetched, read, tested, and compiled | `2a6b87aca083a5bf498ac1f68a1b636c500d7aaa` |
| Historical Go toolchain | `go1.26.4`, matching the pinned release workflow |
| Historical local build target | `darwin/arm64`, `CGO_ENABLED=0`; diagnostic binary, not a release reproduction |
| Historical unpatched Darwin binary SHA-256 | `f8b3a7bfe5f8be7d48a1f60506ae579a9e37877696d9f813bf6143f5ed4b5887` |
| Inactive candidate patch SHA-256 | `f55a3731e82837ed92d8943e97ef6d6521d21a78d3026a65c97b0950c3c494be` |
| Historical patched Darwin binary SHA-256 | `54af78bd25e4bd81cf383e91337c5927506d943ddf0f5458b3a98a0789d590df` |
| Patched 380-record HTTP matrix SHA-256 | `71b15e16af5cde8f022bb51dc41507506f6a9d93e8e75830cd927f6e8f26fa15` |
| Long-ID and multi-image evidence SHA-256 | `cb807fd737c5294cc52c101a5a2805c96b661e0e45f9c8e458b8ef15c25a0ccb` |
| Embedded `models.json` SHA-256 | `b19b2655a4f294605d3a347be16e67ef6ea776d70cedbd69c36629f3dbb945d9` |
| Embedded `codex_client_models.json` SHA-256 | `a044aa222836b32091fdf4c9c34030443cb8717dea65db875cf705406c332fd6` |
| Engine `go.sum` SHA-256 | `b29392b1f713b238d6232af2f8fd09e28b7f5d221847ddccf0fc35682a9b29bd` |

The initial baseline checkout remained clean after diagnosis; the source-only
candidate is now maintained as an exact-base patch and tested in task-owned
checkouts. Existing temporary checkouts were preserved. No installed
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

## Baseline Model Hub protocol matrix

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
without reasoning controls. The patched matrix retains conversion defaults
while removing the missing-catalog stripping step.

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

No subscription/OAuth wire fixture was part of the initial baseline.
The source-only acceptance above now includes fake-auth actual executor
HTTP/WebSocket tests; it never exercises real subscription accounts.

## Approved source and registration boundary

The orchestrator approved and the maintained patch implements this boundary:

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

Engine edit surface: `internal/thinking/apply.go`, the relevant provider
appliers, source-payload plumbing in executor helpers, the independently
approved xAI final-egress gate, and tests. `validate.go` is unchanged.
Actual executor tests cover both later xAI sanitization and Kimi's native
representation in a Chat envelope; an early return alone was insufficient.

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

The task-owned baseline harness and raw results are retained in scratch
(SHA-256 `8d31f89e6cb9167c7056e9acbc5539efc5c978e02ebbb9d524370e1cc96021ad`).
The harness SHA-256 is
`2d9855ac52194e7164ef0541b3bd8001f0d8e094cfd4316569efa7afdbdc64d3`.
The maintained entry point is now `patches/cliproxyapi/verify.py`, using
`wire_matrix.py` and the repository's existing mock.

Maintained hermetic construction (revised Linux lane and independent root
reruns passed):

1. Fetch only the exact engine commit into a new task directory. Give Go its
   own HOME, TMPDIR, module/build caches, and `GOENV=off`. Use the exact
   toolchain and `-mod=readonly`; dependency downloads are preparation, not
   upstream account probes.
2. Export the complete frozen Avibe commit and verify its tree, archive and
   path-independent source digest. Reuse that export's stdlib-only
   `tests/e2e/drivers/mock_llm_upstream.py`, with one ephemeral loopback
   listener per protocol. Generate fake-key config in a test-owned home.
   Use frozen `CLIProxyEngineAdapter.sync_sources` and `EngineSupervisor`.
   The existing constructor seams select only the preverified diagnostic binary,
   task environment/log and fresh private port, never an installed runtime.
   Real state validation, routing/idle barrier, transaction, atomic writer,
   health checks and owned-child stop/start remain the product's responsibility.
3. Use the approved temporary Linux mount/network/PID/chroot envelope,
   making mounts private before binding. Keep source, frozen fixture, recipe
   and toolchain read-only, expose only invocation output and the separate
   task caches for writes, hide guest/user homes and namespace handles,
   close inherited descriptors, then drop
   UID/GID/groups/capabilities and set no-new-privileges. Compile with networking
   disabled; dynamic Go listeners, engine and replacement mocks run together
   on the namespace's private loopback. No veth or host network is added.
4. Require actual IPv4/IPv6 outside-sentinel negatives and private positives,
   plus failure/process/mount cleanup receipts before the full suite. Parent
   receipts are exclusively opened outside child mounts before execution;
   preflight reaches an unlinked supervisor channel closed before candidate
   launch. Never trust a later child-writable probe file. The earlier macOS
   localhost wildcard is retired; its remaining pure/compile path denies all
   egress. Rejecting executor transports and a dead proxy are complementary
   checks, not OS isolation. `--local-model` alone is insufficient.
5. Assert native reasoning fragments, required conversion results, explicit
   negatives, no intent injection on native absence, correct model/prefix/
   credential replacement, non-ASCII preservation, response streaming, and
   cancellation. Stop and reap the child and all mock threads in `finally`.
   Consume each returned supervisor connection, including changed port/token.
   Actual-child failure regressions verify Source validation before restart,
   replacement rollback/recovery and failed-startup cleanup. Preserve all
   generated registrations; the labeled engine-only empty profile is applied
   only in the process seam before each launch.

The first Linux wire fixture depended on direct-file watcher reload after
atomic configuration replacement and timed out. The pinned watcher watches
the file itself; a raw inotify event trace was not recorded. This is a separate
diagnostic limitation, not an established Avibe Source replacement regression:
the frozen product explicitly restarts its owned child. The approved fixture
correction consumes that lifecycle without modifying the watcher, extending
deadlines, writing in place, or mocking successful restart.

The promoted maintained engine tests cover:

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
- Build `cmd/server`, run conversion/registration/auth/executor tests, and
  repeat the matrix with actual generated Avibe Source registrations. The
  empty-object engine-only profile is labeled separately, not claimed as an
  Avibe-generated capability. Long-ID and ~42 MiB image consumers close the
  additional engine seams requested during integration.

Resources after candidate and integration checks: approximately 3.5 GiB of
task-owned source, Go toolchain/modules/cache and locked Python dependencies;
an approximately 80 MiB unstripped diagnostic binary.
Build/test concurrency was `GOMAXPROCS=2`, `-p=1`, with a 1536 MiB soft Go
memory limit. The final wire child uses the same limits; the initial baseline
used a 512 MiB soft limit.
The Go limits are not OS memory caps. Build all four release targets
sequentially or on bounded native runners, not concurrent local full builds.

## Maintained source, reproducible builds, and publication order

There is no authorized public fork or published patched source today. The
maintained candidate home is the Avibe-owned source/test patch and recipe
under `patches/cliproxyapi/`, recording the
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
