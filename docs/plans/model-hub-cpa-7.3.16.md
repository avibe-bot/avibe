# Model Hub CPA v7.3.16 Upgrade

Status: implementation ready for review, 2026-09-25. The packaged target is
now an Avibe-owned v7.3.16 build. The installed runtime was not upgraded or
restarted in this change.

## Change contract

Upgrade Model Hub from CLIProxyAPI v7.2.149 to v7.3.16 at source commit
`c404af96ebacedf8168b3c2bdbf4449a21cd1c1e`. Publish the four-platform
Avibe-owned assets as `model-hub-engine-v7.3.16-3` before adoption. Preserve
the existing platform matrix and the [dependency lifecycle](model-hub-cpa-dependency.md).

The upgrade must preserve:

- Exact Source, credential, and upstream model selection, including unlisted
  models. Avibe remains the owner of cross-Source fallback.
- Management API credential inventory and OAuth flow identities.
- Buffered and streaming Messages, Chat Completions, and Responses behavior,
  including tool calls, usage, cancellation, and active-stream reload.
- Verified atomic installation, previous-generation retention, and separation
  between installing the dependency and starting the engine.

## Compatibility ownership

### Claude credential filenames

CPA v7.3.16 may migrate a Claude credential from a legacy name such as
`claude-account.json` to a stable identity-hash name such as
`claude-00f765af-account.json`. Avibe reconciles the file identity from the
stable `email`, `organization_uuid`, and `account_uuid` fields before binding
or cleaning up OAuth material. The original credential ref, Source, and
prefix remain the owner, and ambiguous reconciliation fails closed.

Coverage includes same-Source reauthentication, cross-Source duplicate login,
renamed credentials, and binding or cleanup failures.

### Native Chat token fields

Stock v7.3.16 normalizes `max_completion_tokens` to `max_tokens` when the
per-model `use-max-completion-tokens` option is false. That breaks upstreams
which accept only the caller's native field. Avibe owns this boundary, so the
release assets carry the minimal patch in
`patches/model-hub-cpa-v7.3.16/openai-compat-native-token-field.patch`.

The patched default preserves whichever token-limit field the caller supplied
in both buffered and streaming Chat requests. An explicit true option retains
CPA's canonical `max_completion_tokens` conversion.

## Validation

- Build all four assets from the exact upstream source with the Avibe-owned
  patch, record archive and executable digests, and verify the complete
  manifest asset set with the release guard. The build metadata records only
  host-independent toolchain identity, and the builder validates against the
  checked-in manifest rather than self-verifying a generated one.
- Update the frozen packaged-manifest test together with the production pin.
- Run focused runtime, OAuth/source-identity, release-guard, dependency,
  release-helper, routing, lint, and diff checks.
- Exercise the patched engine with synthetic credentials and loopback
  upstreams through the isolated Model Hub wire harness.
- Bind review and CI evidence to the PR's current head. Do not merge, deploy,
  or restart the user's runtime as part of this change.

## Known by design

- The release assets are Avibe-owned patched builds, not byte-for-byte copies
  of the upstream v7.3.16 archives.
- No new provider entry points or changes to the supported platform matrix.
- No automatic tracking of upstream `latest`.
- The capability-driven reasoning intent gap remains a separate existing issue;
  this upgrade does not claim to repair it.
- Synthetic wire tests do not establish live vendor OAuth login/refresh or
  external provider availability.

## Evidence and limits

- The four patched archive sizes, archive SHA-256 values, and extracted binary
  SHA-256 values are recorded in the packaged manifest.
- The patch is limited to the helper shared by CPA's buffered and streaming
  OpenAI-compatible Chat paths. The Claude migration remains in Avibe's state
  layer rather than in the CPA binary.
- The existing old-version references in historical Model Hub investigations
  remain historical evidence and are not updated to imply those experiments
  ran against v7.3.16.
