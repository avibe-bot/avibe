# SignPath test signing of the Windows installer

## Background

Avibe's Windows desktop package is an unsigned NSIS installer. Before any
production Windows signing decision, we need evidence that SignPath's trusted
GitHub connector can sign a real Avibe installer built on GitHub-hosted
runners, and that the result can be verified independently of SignPath.

SignPath organization `cd712383-10e4-47d1-8267-d395bd8104b1`, project `avibe`,
signing policy `test-signing`, artifact configuration `windows-installer-test`:

```xml
<artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
  <zip-file>
    <pe-file path="Avibe-test-setup.exe">
      <authenticode-sign hash-algorithm="sha256" />
    </pe-file>
  </zip-file>
</artifact-configuration>
```

The test certificate is self-signed, `CN=Test certificate for 'Avibe [OSS]'`,
valid until 2029-11-03, DER SHA-256
`38f8d097ed29ecd73394e347a401c0d099379e89e1c47a3843f442041829bc0a`.

## Prerequisites

Some setup lives outside this repository:

- **SignPath GitHub App.** The official SignPath GitHub App must be installed for
  the repository owner, with repository access limited to the selected
  repository. Without it, the submit step fails before any signing request
  exists, with `Failed to retrieve GitHub App token`. Operators must review and
  approve the permissions GitHub shows at installation. Limiting repository
  selection does not remove the organization-level permissions the App requests.
- **Test-only submitter.** The GitHub environment `signpath-test` holds
  `SIGNPATH_TEST_API_TOKEN`. It is the API token of a dedicated, test-only
  SignPath CI user that submits only to the `test-signing` policy. It is never a
  production submitter.
- **Branch and workflow access.** The environment's deployment branches are
  `master` and, while an approved test PR is open, its task branch. The
  `test-signing` policy requires the trusted GitHub build system and origin
  verification. Before dispatching the standalone entry, an authorized operator
  must migrate its build-definition allowlist from
  `.github/workflows/desktop-package.yml` to
  `.github/workflows/desktop-signpath-test.yml`, preserving branch restrictions
  and all production policies. This external migration is not performed by the
  workflow-isolation change; the new entry has not had a real signing acceptance
  run. Ordinary releases do not depend on that test-policy migration.

## Goal and contract

An explicit manual run of `desktop-signpath-test` with
`signpath_test=true` builds the self-contained Windows NSIS installer, has
SignPath test-sign its outer PE, and independently verifies the signed result.

- Only the outer installer PE is signed. NSIS is not a SignPath composite
  format, so the desktop and runtime executables embedded in the installer stay
  unsigned. This run is an integration test, not inside-out production signing.
- The test workflow has only `workflow_dispatch`, not `workflow_call`, push,
  schedule, or release triggers. It requires the default-false explicit opt-in.
  `desktop-package.yml` contains only its ordinary `package` job and keeps its
  manual, reusable, push and schedule triggers; release caller permissions do
  not increase.
- The standalone test workflow never runs the `package` job, never touches the
  `desktop-updater` environment, Apple or updater secrets, R2, releases, tags,
  updater feeds, or
  release manifests, and never records signing in the production manifest
  schema.
- Every job runs on a GitHub-hosted runner with read-only permissions. No job
  binds a production environment or inherits secrets. Only the sign job binds
  `signpath-test`, and its SignPath step is the only place that references a
  secret, `SIGNPATH_TEST_API_TOKEN`. The sign job never checks out or runs
  repository code. The build and verification jobs reference no secret.
- SignPath organization, project, policy, artifact configuration, connector URL,
  file name, and certificate pin are literals. The only dispatch value used is
  the version, which must be a SemVer prerelease.
- Artifacts are test-named and short-lived: unsigned and signed installers for
  3 days, verification report for 14 days.

Verification (`desktop/scripts/signpath_test_signing.py verify`) passes only
when:

1. the unsigned and signed artifacts each contain exactly one file,
   `Avibe-test-setup.exe`;
2. the signed bytes are the unsigned bytes with only an Authenticode signature
   added: the PE `CheckSum` and security directory entry may change, then up to
   7 zero padding bytes and a certificate table that ends the file;
3. the signer certificate's DER SHA-256 equals the pin, checked before anything
   is trusted;
4. with exactly that certificate added to the runner's `LocalMachine\Root` for
   the duration of the check, `Get-AuthenticodeSignature` reports `Valid`; the
   certificate is removed afterwards, and a pre-existing copy fails the run.

Two negative controls must fail for their intended reason, or the run fails:

- one flipped content byte is rejected by the payload check and reported
  `HashMismatch` by Authenticode under the same trust;
- a copy of the unsigned installer signed on the runner with a throwaway
  self-signed code-signing certificate passes the payload check and is rejected
  on certificate identity.

## Why a standalone dispatch-only workflow

The first implementation placed the test jobs in `desktop-package.yml`, guarded
by a dispatch-only boolean. That isolated execution but not startup validation:
GitHub checks every nested job's permissions before evaluating `if`. The test
sign job's `actions: read` exceeded the release caller's implicit `actions: none`,
so ordinary releases failed before any job ran.

Move the three test jobs unchanged into `desktop-signpath-test.yml`, with no
`workflow_call` trigger. Do not grant the release caller more permissions for an
unrelated test. The ordinary packaging job no longer needs a test-mode guard.
The standalone sign job binds `signpath-test` directly, without reusable-workflow
secret inheritance. A new dispatch workflow must exist on the default branch
before it can be dispatched, and its SignPath origin allowlist must match the
new path; these are prerequisites for a later test run, not for a release.

Repository-level secrets remain in the `secrets` context of every job. The
guarantee is what each job references: the SignPath step references only
`SIGNPATH_TEST_API_TOKEN`, and no other test job references any secret.

## Validation

- Focused pytest: payload checker on synthetic PE32 and PE32+ fixtures, and the
  workflow contract (guards, permissions, pins, secret placement, literals).
- Permission regression: parse every local reusable-workflow call edge and
  resolve workflow/job defaults through the chain, treating unlisted scopes as
  `none`. The actual release call graph fails before this fix with
  `desktop-package.yml / signpath-test-sign: actions:read exceeds caller actions:none`.
  Negative controls retain that failure for a disabled nested job and cover
  explicit empty maps, inheritance, job overrides and nested permission reduction.
- Real evidence: after the prerequisites above, an operator may dispatch
  `gh workflow run desktop-signpath-test.yml --ref <branch> -f version=0.0.0-rc.1 -f signpath_test=true`.
  The historical run below does not validate the new entry point. Its real
  signing acceptance and the next ordinary release are separate runtime gates.

## Todo

- [x] Contract note to the orchestrator
- [x] Verifier script and focused tests
- [x] Workflow jobs and contract tests
- [ ] PR, exact-head Codex review, CI
- [x] Real dispatched run evidence (orchestrator)

## Evidence

Workflow run 41685355395 ran on implementation commit `60d3c45`. Its second
attempt, after the GitHub App was installed, passed build, sign, and verify.
The first attempt failed before creating a signing request because the App was
missing. SignPath request `10b7075b-dc3e-4a87-93f4-c40357d0e094` completed.
The verification report showed:

- The signed installer is the 181,648,326-byte unsigned installer plus 2 zero
  padding bytes and an 8,360-byte certificate table.
- The signer certificate matched the pin. Authenticode reported `UnknownError`
  without trust and `Valid` with only the pinned certificate trusted.
- The tampered copy was rejected as `payload`, and Authenticode reported
  `HashMismatch`. The foreign-certificate copy was rejected as `certificate`.

This evidence covers the original `desktop-package.yml` entry on commit
`60d3c45` only; later documentation-only commits did not rebuild. The standalone
workflow migration has not had a real signing run. It shows test signing, not
production signing. Only the outer installer PE was signed, with the test
certificate. The embedded executables were not signed.
