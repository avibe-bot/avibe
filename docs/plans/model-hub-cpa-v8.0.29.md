# Model Hub CPA v8.0.29 Trial

Status: evidence-only trial; the packaged runtime remains pinned to v8.0.23.

## Change contract

Evaluate upgrading the Avibe-managed Model Hub engine from CLIProxyAPI v8.0.23
to v8.0.29 at source commit
`5fc580b1d72d87a7bcfe5cfb6f479c1aea5625b3`. Do not change the runtime manifest,
serve a new engine asset, or restart a user's CPA process in this trial.

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

These are compatibility-positive in isolation, but the registration, watcher,
translator, and usage changes require runtime evidence before adoption. The
root-level `requests.tls-handshake-timeout-seconds` setting is not rendered by
Avibe in this trial, so the new handshake bound must not become an accidental
behavior change.

## Trial completion gates (not yet met)

The preparation PR adds the patch set and test harness. It does not claim the
trial is complete; the following checks must pass before adoption is proposed.

- All five patches apply in manifest order to the exact v8.0.29 source commit.
- No v8.0.23 manifest or Avibe-owned release asset is overwritten by the trial.
- CPA focused tests pass with the repository's pinned Go 1.26.9 toolchain.
- The Model Hub engine contract and wire tests pass for buffered and streaming
  OpenAI Chat, Responses, Anthropic Messages, Google/Antigravity, OAuth/source
  lifecycle, and hosted-tool passthrough cases.
- Registration races do not publish stale model catalogs after a credential
  replacement, refresh, disable, or re-enable.
- Antigravity static-catalog behavior, native token-field preservation, OAuth
  loopback behavior, and the two HTTP/2 dead-path health checks are measured on
  the v8.0.29 patched binary.
- Adoption remains a separate follow-up that updates the manifest, publishes
  all supported assets, records archive and binary digests, and runs the
  release guard.

## Current evidence (2026-10-15)

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
  forced on the host; that callback test remains an isolated-network adoption
  gate. Passing these suites does not clear the SDK concurrency failure below.

### Adoption blocker: intermittent upstream registration failure

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
registration is safe. Do not hide it with retries or label it an accepted
non-change. Root-cause analysis and a verified resolution are required before
adoption; this preparation PR does not patch that separate upstream boundary.

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
The upstream concurrency failure remains an adoption blocker.

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
