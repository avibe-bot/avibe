# Model Hub CPA v7.3.16 Upgrade

Status: release assets published and verified; login-only compatibility in review,
2026-09-26. The packaged target is an Avibe-owned v7.3.16 build. The installed
runtime was not upgraded or restarted in this change.

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

CPA v7.3.16 changes a Claude filename only when saving a new OAuth login.
The email-only name `claude-owner@example.com.json` can become
`claude-00f765af-owner@example.com.json`; an account-hashed name can likewise
precede an organization-hashed name. Existing files remain usable without
relogin. Startup, restart, token refresh, upload and watcher loading do not
generate a new Claude filename.

The pinned upstream call graph at `c404af96ebacedf8168b3c2bdbf4449a21cd1c1e`
is the evidence for this boundary:

- `internal/api/handlers/management/auth_files_provider_oauth.go:169-180`
  computes the canonical name after login and calls `saveTokenRecord`.
- `internal/api/handlers/management/auth_files_fields.go:945-980` finds the
  legacy credential, merges its metadata, saves the new file, then deletes the
  predecessor. `sdk/auth/manager.go:90-110` does the same for CLI login.
  These are the only callers of `FindMatchingLegacyCredential`.
- `sdk/cliproxy/auth/metadata_merge.go:24-53` preserves non-token metadata,
  including Avibe's prefix; `internal/auth/claude/token.go:73-100` writes it.
- `sdk/auth/filestore.go:417-446` resolves persistence to the existing path or
  filename. `internal/watcher/synthesizer/file.go:97,199` loads the existing
  basename; `internal/api/handlers/management/auth_files_crud.go:261-275`
  uploads under the supplied name. None invokes Claude filename generation.

Avibe therefore applies one rule, only in `_complete_oauth`, after identifying
the login's resulting auth file and before binding or cleanup:

- A file carrying an existing Claude credential's exact Avibe prefix belongs
  to that credential. Update only its `auth_name`; preserve ref, Source and
  prefix. Account email/organization/account UUID fields are not ownership keys.
- The normal same-Source reauthentication and foreign-Source rejection then
  apply. A migrated file owned by another Source is retained as
  `FOREIGN_SOURCE_REF`, never deleted merely because its filename is new.
- Multiple credential owners or a still-present predecessor are ambiguous:
  completion fails with `binding_failed`, retains `UNKNOWN`, and changes or
  deletes neither grant. If CPA itself reports the failed deletion as an OAuth
  error, the existing `upstream_failed`/unknown-retention path applies.

#### Circuit-breaker diagnosis and scope decision

The earlier review loop assumed migration could happen during any lifecycle
operation. That premise created global reconciliation passes, startup retry
flags and account-identity matching, each with additional failure boundaries.
Direct inspection of the pinned upstream disproved the premise. The unpushed
activation/observation follow-up was withdrawn, not extended.

Remove the global pass, completeness/isolation state, `oauth_identity`
seeding/comparison, and reconciliation from startup, native activation,
revocation, validation, discovery, quota and labels. Those paths return to the
base behavior. Keep only the login-completion rule and its consuming tests.
The manifest, CPA patch, builder, workflow and release guard are frozen.

New findings must establish their trigger against the pinned upstream before
changing code. A proposal to reintroduce general reconciliation or identity
matching, or change the release pipeline, requires a new scope decision.

### Release publication and provenance boundary

Review through `df6fef95c` exposed another recurrence: both publication and
backup recovery implemented draft repair independently, and name-intersection
replacement could never recover an unexpected asset. Both jobs now invoke one
guard-owned draft operation. It verifies the local pinned set, requires an
unpublished release, downloads and verifies the remote set, replaces the entire
draft set when it is unreadable or mismatched, and re-downloads before publication.
This includes interrupted uploads whose listed assets cannot be downloaded;
an already-public release is never rewritten by this operation.

The consuming publication test covers valid, missing, extra and corrupt draft
assets, a corrupt replacement upload, transient and persistent download failures,
and refusal to modify a published release. The old expected-name-only replacement fails the
extra-asset case; the exact-set replacement passes it.

The builder resolves `build.patch` from the manifest, or accepts a local copy
only when its bytes match `build.patch_sha256`. It validates this input before
checkout/build, independently of output hashes: a provenance-only patch change
can leave all binary/archive hashes unchanged and must still be rejected.

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
- Avibe's random prefix is an ownership tag inside its managed auth directory,
  not a tamper-proof signature. Arbitrary external replacement of an account
  while copying another credential's prefix is outside this upgrade's scope;
  no account-identity registry or observational-read hardening is introduced.
- Ambiguous or interrupted logins are retained, not automatically repaired at
  startup. Ordinary legacy files do not need a migration pass to remain usable.
- The capability-driven reasoning intent gap remains a separate existing issue;
  this upgrade does not claim to repair it.
- Synthetic wire tests do not establish live vendor OAuth login/refresh or
  external provider availability.

## Evidence and limits

- The narrowed regressions exercise same-Source login, foreign-Source login,
  predecessor retention, PATCH failure after migration, and unchanged legacy
  use without relogin. The four login cases failed on the base implementation
  for new-prefix rebinding or deletion of the only surviving grant.
- Earlier global-reconciliation test counts are not evidence for this narrowed
  implementation. Run its focused suites and obtain fresh exact-head hosted
  review/CI before close-out.
- The narrowed implementation passed 1,368 tests across runtime, OAuth, native
  takeover, Source identity, quota, API, build, release guard and login migration,
  plus changed-file Ruff and diff checks. All state is test-owned and synthetic.
- Workflow run `36213312402` built and published `model-hub-engine-v7.3.16-3`
  from `df6fef95c078536c4406d1357ea676daaba7160b`. The first guard hit a
  transient public-download 404; failed-job rerun attempt 2 completed
  successfully, including the verified backup. An independent public download
  subsequently verified all nine assets against the unchanged packaged manifest.
- The repeated release workflow `36214619870` succeeded on `fbba8aa10` with
  all three jobs successful; that head's lint run `36214572839` also completed
  with all 18 checks successful. Neither result substitutes for review/CI on
  the next ownership-fix head.
- The four patched archive sizes, archive SHA-256 values, and extracted binary
  SHA-256 values are recorded in the packaged manifest.
- The patch is limited to the helper shared by CPA's buffered and streaming
  OpenAI-compatible Chat paths. The Claude migration remains in Avibe's state
  layer rather than in the CPA binary.
- The existing old-version references in historical Model Hub investigations
  remain historical evidence and are not updated to imply those experiments
  ran against v7.3.16.
