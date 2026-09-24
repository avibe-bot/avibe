# Model Hub CPA v7.3.16 Upgrade

Status: blocked before changing the packaged pin, 2026-09-24. The stock
candidate does not preserve the compatibility contract below. No installed
runtime was upgraded or restarted.

## Change contract

Upgrade the stock Model Hub engine from CLIProxyAPI v7.2.149 to v7.3.16,
source commit `c404af96ebacedf8168b3c2bdbf4449a21cd1c1e`. Publish the
byte-for-byte upstream assets as `model-hub-engine-v7.3.16-1` before changing
the packaged pin. Preserve the existing four-platform matrix and the
[dependency lifecycle](model-hub-cpa-dependency.md).

The upgrade must preserve:

- Exact Source, credential, and upstream model selection, including unlisted
  models. Avibe remains the owner of cross-Source fallback.
- Management API credential inventory and OAuth flow identities.
- Buffered and streaming Messages, Chat Completions, and Responses behavior,
  including tool calls, usage, cancellation, and active-stream reload.
- Verified atomic installation, previous-generation retention, and separation
  between installing the dependency and starting the engine.

## Validation

- Verify each upstream archive's published size and SHA-256, inspect its
  executable digest, and verify all nine mirrored assets using the existing
  release guard. Read back the published release before adopting its manifest.
- Update the frozen packaged-manifest test together with the production pin.
  Run focused runtime, release-guard, adapter, dependency, and routing tests.
- Exercise the real pinned binary with synthetic credentials and loopback
  upstreams through the existing isolated Model Hub E2E harness.
- Bind review and CI evidence to the PR's current head. Do not merge, deploy,
  or restart the user's runtime as part of this change.

## Known by design

- No new provider entry points or changes to the supported platform matrix.
- No automatic tracking of upstream `latest`.
- `patches/cliproxyapi` remains an inactive candidate based on v7.2.149; this
  upgrade neither applies it nor claims its reasoning-policy repair is shipped.
- Synthetic wire tests do not establish live vendor OAuth login/refresh or
  external provider availability.

## Qualification findings

### Stock Chat requests lose the caller's token-limit field

The real v7.2.149 and v7.3.16 Darwin ARM64 release binaries were exercised
through Avibe's turn gateway and adapter against a synthetic loopback upstream.
Both buffered and streaming requests carrying `max_completion_tokens: 32`
retain that field on v7.2.149. On v7.3.16 the captured request instead carries
`max_tokens: 32`, and the preservation assertions fail in both modes.

This is introduced by upstream commit
`690f4f3116b6cbec3b82a61793d35db69dc0d6fa`, not by Avibe's gateway.
At the candidate source SHA, both paths in
`internal/runtime/executor/openai_compat_executor.go` call
`helps.NormalizeOpenAIMaxTokens`. The per-model
`use-max-completion-tokens` option is a boolean defaulting to false; neither
value expresses preservation of whichever native field the caller supplied.
Setting it globally true reverses the incompatibility instead of fixing it.
Payload configuration runs before the normalization, so it is not a
post-normalization restoration mechanism.

The maintained `MH-ROUTING-008` wire test now includes a tool continuation with
non-ASCII arguments and output, and requires native Chat token-limit fidelity.
Do not weaken the assertion to approve the candidate. An upstream correction
or a separately approved minimal engine patch is needed.

### Claude credential migration changes Avibe's ownership key

Upstream commit `fc96a87fa66c0cfd8b4746449a178955229d9db8` changes Claude
credential filenames to include an organization/account hash.
`internal/api/handlers/management/auth_files_fields.go::saveTokenRecord`
preserves metadata while saving the new name and deleting the matching legacy
file. Avibe's `_complete_oauth` checks existing ownership by the returned
filename, so a migrated name can be mistaken for a new credential. Cross-Source
login can then reassign its prefix while leaving the original Source's ref
pointing at the removed name; failed binding cleanup can target the new file.

Before adoption, reconcile file identity migration without changing the
existing Source, credential ref, or prefix owner. Cover same-Source reauth,
cross-Source duplicate login, and PATCH/cleanup failure. This finding is based
on the exact upstream source and Avibe consumers, not on a live OAuth login.

### Evidence and limits

- All four upstream archive sizes and SHA-256 values match the release API and
  `checksums.txt`. Executable digests were recorded, and the release guard
  verified the nine locally materialized mirror assets.
- 444 focused runtime/release-guard/takeover/dependency tests and 631
  OAuth/retry/tool-name/usage/dependency tests passed against unchanged Avibe
  behavior.
- 45 candidate real-engine routing/unknown-model/stream/reload/cancellation
  cases passed; isolated install/hardening and credential-environment checks
  passed. The tool-continuation extension passed all 24 cases before adding
  the token-limit preservation assertion that correctly rejects the candidate.
  The final strengthened 24-case matrix passes on the shipped v7.2.149 binary;
  v7.3.16 fails both selected native Chat cases (buffered and streaming).
- The old full-app S3 fixture fails during Source creation with HTTP 422
  `observation_failed` on both engine versions, before the inference under
  test. It is not an upgrade regression and is not counted as passing evidence.
- Optional auth-file pagination remains backwards compatible when omitted;
  existing OAuth endpoints/provider identities and subscription Chat surfaces
  remain supported. The known capability-driven reasoning stripping gap
  remains present and is not repaired by the stock upgrade.

The partial, unpublished mirror draft was removed; the packaged manifest still
selects v7.2.149 and the public Avibe latest release was not changed.
The implementation stays on a task branch until the engine-maintenance scope
is decided. Do not publish a modified binary under the stock candidate's
checksums, silently select a different target version, or deploy the candidate.
