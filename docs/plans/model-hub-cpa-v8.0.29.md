# Model Hub CPA v8.0.29 Upgrade Candidate

Status: local upgrade candidate; the packaged runtime remains pinned to v8.0.23
until the replacement assets are published and adoption is authorized.

## Change contract

Prepare the Avibe-managed Model Hub engine upgrade from CLIProxyAPI v8.0.23
to v8.0.29 at source commit
`5fc580b1d72d87a7bcfe5cfb6f479c1aea5625b3`, and verify it once end to end in an
isolated local Incus worktree environment. Do not restart the host runtime or
change the persistent master regression environment. PR #2680 remains Draft
and unmerged until the previously agreed 3.2.1 stable release sequencing is
satisfied; the lighter acceptance policy does not authorize publication or merge.

The trial carries the five compatibility patches that still apply to v8.0.29:

1. preserve caller-native OpenAI Chat token fields;
2. bind OAuth callback forwarders to loopback and keep pasted callbacks usable;
3. keep the static Antigravity catalog available until entitlement probing
   answers;
4. add an HTTP/2 health check to the Codex uTLS connection; and
5. add the same health check to the standard and proxy fallback transports.

The Codex per-event SSE patch is intentionally absent because upstream v8.0.28
added `CodexSSEScanner`, and v8.0.29 retains it.

## Upstream changes to verify

The v8.0.29 source includes changes in boundaries that Model Hub consumes:

- model-registration fencing and epoch-checked publication for concurrent auth
  refreshes;
- unique Gemini-to-Claude tool-use IDs across turns;
- retryable `MALFORMED_FUNCTION_CALL` translation and failure usage reporting;
- Antigravity cached-token accounting and Claude tool-schema union flattening;
- configured TLS-handshake timeout/retry support, disabled by default; and
- auth-file removal tracking and single-file snapshotting.

These changes motivate the focused acceptance path below, not an expanding
protocol or race-test matrix. The root-level
`requests.tls-handshake-timeout-seconds` setting is not rendered by Avibe, so
the new handshake bound must not become an accidental behavior change.

## Deferred engine config-reload patch (3.2.2, issue #2686)

Plan this Avibe-owned fix into the v8.0.29 patch set in PR #2680, and offer the
same fix upstream. **Do not implement this new patch before the 3.2.1 stable
tag exists.** Release candidates do not satisfy that boundary. The checked
GitHub tag inventory still contains only 3.2.1 RC tags; no stable tag is present.
This is not a 3.2.1 release blocker. The supervisor-side guard in issue #2687 is
separate Avibe work and is not part of this engine patch.

Read-only inspection of the v8.0.29 source confirms the reported mechanism:
`management/config_basic.go:WriteConfig` truncates the live file in place,
`watcher/events.go` watches the file inode, and `reloadConfigIfChanged` hashes
one read but calls a loader that reads again without serializing the full
reload. The timer mutex protects debounce bookkeeping, not reload execution.
The issue reports a defaults reset under disk load; this has not been proven
to cause the separate Claude initialization timeout in the candidate E2E.

Implement the following as one coherent patch after the stable-tag boundary:

1. **Atomic replacement:** write a temporary file in the config directory,
   sync and close it, replace `config.yaml` atomically, and sync the directory
   where supported. Preserve permissions and the existing normalization/error
   semantics. A Go regression must show concurrent readers only observe the
   complete old or new document, never an empty or partial write.
2. **Observe replacement:** watch the containing directory and filter events
   to the config's normalized path/base name, without broadening auth-file
   handling. A Go regression must prove successive rename replacements remain
   observed and unrelated directory files do not trigger config reloads.
3. **One read, ordered application:** parse the same bytes used for change
   detection and serialize the full read/parse/apply/hash transition, preserving
   existing config-loading semantics. Deterministically synchronized Go tests
   must cover replacement between reads and overlapping reloads, proving that
   a stale completion cannot overwrite the latest accepted configuration.

Keep these tests at their owning Go boundaries rather than add an E2E matrix.
Once implemented, update ordered patch metadata/checksums, rebuild and verify
all six installation assets, and use the one relevant local Incus service E2E
for the final patched candidate. Existing five-patch artifacts are baseline
evidence, not acceptance for the future patch. Publish workflow-owned assets
before any separately authorized packaged-pin change; do not create a separate
v8.0.23 patch release for this work. Remove the patch only after an exact future
upstream source contains the equivalent behavior and regressions.

## Acceptance policy (owner decision, 2026-10-15)

The owner requested a direct candidate upgrade followed by one local regression
E2E, rather than clearing every exploratory trial item before adoption.

- Keep the exact source SHA, ordered patch identities, archive/binary digests and
  release guard. Build the six supported installation assets; this is packaging,
  not a six-platform E2E matrix. Publish assets before changing the packaged pin.
- Run `MH-SERVICE-001` once on the candidate in an isolated local Incus worktree:
  install and start the real service/engine, create Sources, complete Vibey and
  Claude Code turns, replace a Source credential, discover/register a new model,
  complete another turn, and verify restart/orphan cleanup.
- Extend that existing acceptance case rather than add another suite. The
  observable invariant is that an accepted Source credential/model update reaches
  the running engine and serves the next Agent turn. Existing B6 coverage stops
  at API/discovery success, so it cannot catch a saved but unroutable update.
- Keep the observed SDK concurrency failure as a known risk, not a separate
  adoption blocker. An E2E pass does not prove the upstream race is resolved.
- Do not require the remaining exploratory race, protocol or callback-port
  probes as additional adoption gates. Normal PR review/CI, release sequencing,
  and explicit publication/merge authority remain unchanged.

## Current evidence (2026-10-16)

- Six unpublished candidate archives (darwin arm64/x64, Linux amd64/arm64,
  Windows amd64/arm64) were built from the exact source and five pinned patches
  with Go 1.26.9 and the unchanged release builder's flags/helpers. The local
  release guard verifies the complete asset set, sizes, archive/binary digests,
  sidecars and manifest. Candidate manifest SHA-256:
  `b53c236ce02c3fcb0218824153df4bb3d1927246a3b889752c07a633857d9f2b`.
  This is local build/verification evidence, not publication or six-platform
  runtime acceptance. The packaged manifest remains unchanged.
- After repairing the identity assertion, the single local Linux Arm64
  installed-service `MH-SERVICE-001` run passed in **46.36 seconds** with the
  exact v8.0.29 candidate. It covered install/version/health, Source creation,
  Vibey and Claude Code turns, Source credential replacement and a newly
  discovered model serving another turn, engine survival after a hard service
  kill, restart/orphan reaping, and clean stop. The temporary target was deleted;
  the existing local master stayed healthy with its PID unchanged. The Source
  update/version-check extension and its catalog edit remain uncommitted in the
  owner worktree; the identity repair does not silently include those prior edits.
- The former orphan assertion was a false negative: the same live PID and
  identity marker survived while psutil's reported `create_time` shifted by one
  second. Engine checks now compare the inherited `AVIBE_PROCESS_IDENTITY`
  fingerprint, as the product reaper does. Service/UI cleanup retains its
  separate unmarked-record check. Focused regressions cover drift, recycled
  PIDs, missing/unreadable/malformed identities, and service cleanup.
- Preserve the earlier Claude initialization-timeout result and the subsequent
  orphan-assertion failure as historical evidence; neither establishes issue
  #2686 as its cause. No product dependency declaration, SDK override, timeout,
  or test retry was changed. However, the fresh environment's unconstrained
  installation resolved Claude Agent SDK **0.2.168**, versus **0.2.167** in the
  preceding run. This is a validation deviation: the full pass is not a strict
  same-SDK before/after comparison. The assertion regression is independently
  reproduced without an SDK. Do not silently expand the trial matrix or rerun
  acceptance to erase this qualification.
- Environment preparation initially failed before pytest on a truncated Python
  build-dependency download. One preparation resume on the same disposable
  target succeeded; the acceptance itself executed once. The packaged engine
  pin, release sequencing hold, and publication/adoption authority are unchanged.
- The exact v8.0.29 tag was checked out and all five patches applied in the
  listed order with `git apply --check --whitespace=error`.
- Avibe's engine builder, release-guard and scenario catalog tests pass:
  **24 passed**. Changed-file Ruff checks pass.
- Go 1.26.9 is available as an explicitly selected toolchain. With all five
  patches and `CGO_ENABLED=0`, the executor, helper and management packages
  pass; `go test -run Antigravity ./sdk/cliproxy/` also passes.
- A native darwin-arm64 engine builds with Go 1.26.9, `-trimpath`,
  `-buildvcs=false`, and version/source linker metadata. Its binary SHA-256 is
  `4e2cf3f9c8564f3bb5f8a742bec52e3777ade63cb7fb534641d668758e402693`.
  This is a local test build, not a six-platform release artifact.
- Hosted passthrough scenarios `MH-ENGINE-HOSTED-001` through `005`:
  **5 passed**, using a local archive and offline manifest selected through
  `VIBE_MODEL_HUB_ENGINE_MANIFEST_PATH`. The fixtures exercise non-ASCII
  hosted items, declaration/include preservation, partial images, citations,
  tool usage, replay and absence of injected image tools. The engine runs
  under the existing isolated adapter against a synthetic loopback upstream;
  this does not prove live-provider or complete Vibey-turn behavior. The
  Responses replay assertion compares the entire ordered non-reasoning
  sequence, including message anchors. An assertion-only mutation probe
  verifies unchanged and normalized-reasoning cases are accepted, while
  reordered, duplicated, dropped, or answer-crossing calls and a dropped
  answer are rejected; both real-engine hosted suites pass with that check.
- The same five hosted cases also pass on the published v8.0.23-3 baseline,
  downloaded into a separate temporary directory and verified against the
  unchanged packaged manifest. This checks that the new suite remains usable
  by the nightly workflow before adoption.
- The existing engine contract and wire suites completed against the same
  offline binary: **26 passed, 1 skipped** in 917.76 seconds. The skipped
  `test_oauth_callback_forwarder_is_loopback_only_and_optional` binds vendors'
  fixed callback ports and requires `AVIBE_E2E_ISOLATED_NETWORK=1`. It was not
  forced on the host; it is recorded as unexecuted evidence, not an additional
  adoption gate. Passing these suites does not resolve the SDK concurrency
  failure below.

### Known risk: intermittent upstream registration failure

The full `sdk/cliproxy` run failed at
`TestPutOpenAICompatConcurrentWithAuthFieldsPatch_Issue6559`:

```text
service_openai_compat_race_test.go:194:
expected registered openai-compatibility auth to support final model "alias-9"
```

A focused replay on the exact source, with Go 1.26.9 and fresh test-owned homes,
reproduced the same assertion in **2 of 20 stock runs** and **1 of 20 patched
runs**. A separate patched single run passed. This demonstrates an intermittent
failure present without Avibe's patches, not a clean SDK pass or proof that
registration is safe. Retain the failed-run evidence and do not hide it with
retries. Under the owner's 2026-10-15 acceptance decision, this is a known risk
rather than a separate adoption blocker. The local service E2E exercises Source
replacement, model registration and an actual subsequent turn, but cannot prove
this concurrent upstream test has been fixed. This upgrade does not patch that
separate upstream boundary.

Reproduce inside a disposable stock or five-patch v8.0.29 checkout:

```sh
GOTOOLCHAIN=go1.26.9 CGO_ENABLED=0 go test -count=20 -timeout 60s \
  -run '^TestPutOpenAICompatConcurrentWithAuthFieldsPatch_Issue6559$' \
  ./sdk/cliproxy
```

## Review-scope decision (2026-10-15)

The same assertion-coverage class appeared on reviewed heads `d428e4eeac`
(Responses membership-only replay checks) and `53de3827b2` (Anthropic checking
only one message's content). The delivery loop paused before another fix to
inventory both findings and audit all five scenarios. Root cause: a collection
preservation contract was asserted through a partial projection rather than
its complete ordered contents and cardinality.

The orchestrator's scope decision is a test-only, contract-preserving repair
across this small suite: compare complete replay histories, full tool lists,
and complete decoded response-event sequences. The only allowed wire
normalizations are explicit: Responses reasoning normalization remains outside
the hosted contract, `include` values may be reordered but not lost or
duplicated, and Anthropic may add top-level tool/content-block `cache_control`
metadata or represent a text string as a text block. Cache normalization is
shallow; it must never erase identically named fields inside tool inputs or
hosted results. Real-engine captures confirm these existing transformations.
No runtime, upstream patch, or release-policy change is authorized by this
review repair. Both real-engine hosted suites were rerun: **5 passed on each
version**. A before/after probe executing the actual test assertions verified
**26 assertion-only cases** across all five scenarios, including previously
accepted missing/duplicate/reordered messages, declarations and stream events,
allowed canonicalization, and preservation of nested business fields named
`cache_control`. This is assertion evidence, not 26 additional engine E2Es.
The upstream concurrency failure is retained as a known risk under the owner's
later acceptance decision above.

## Known by design

- This trial does not enable upstream `latest` resolution and does not change
  the selected runtime for existing Avibe installations.
- The five Avibe compatibility patches remain owned by Avibe even where the
  upstream release contains adjacent fixes.
- The HTTP/2 PING health checks remain Avibe policy because upstream deliberately
  leaves post-connect timeout policy to downstream callers.
- Hosted tests target the real engine boundary, not provider execution or the
  complete Vibey tool lifecycle. Synthetic fixture release strings are frozen
  payloads, not claims about the latest upstream release.
- Timing measurements from v8.0.28 do not count as v8.0.29 evidence. Native
  test-build success does not replace the six-platform release build and guard.
