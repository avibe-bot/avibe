# Model Hub CPA v7.3.16 Upgrade

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
