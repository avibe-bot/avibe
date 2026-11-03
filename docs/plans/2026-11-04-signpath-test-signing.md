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

## Goal and contract

An explicit manual run of `desktop-self-contained-package` with
`signpath_test=true` builds the self-contained Windows NSIS installer, has
SignPath test-sign its outer PE, and independently verifies the signed result.

- Only the outer installer PE is signed. NSIS is not a SignPath composite
  format, so the desktop and runtime executables embedded in the installer stay
  unsigned. This run is an integration test, not inside-out production signing.
- Test mode exists only on `workflow_dispatch`. `workflow_call` does not declare
  the input, so release invocations cannot opt in; push, schedule, release, and
  default manual runs keep the existing `package` job unchanged.
- Test mode never runs the `package` job, never touches the `desktop-updater`
  environment, Apple or updater secrets, R2, releases, tags, updater feeds, or
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

## Why inline jobs instead of a reusable workflow

A called workflow's environment secrets resolve empty unless the caller passes
`secrets: inherit` (actions/runner#4453). Jobs inside `desktop-package.yml` bind
`signpath-test` directly, without that plumbing. Because the workflow is already
registered, they can also be dispatched from the task branch before merge.
Repository-level secrets remain in the `secrets` context of every job either
way. The guarantee is what each job references: the SignPath step references
only `SIGNPATH_TEST_API_TOKEN`, and no other test job references any secret.

## Validation

- Focused pytest: payload checker on synthetic PE32 and PE32+ fixtures, and the
  workflow contract (guards, permissions, pins, secret placement, literals).
- Real evidence: the orchestrator dispatches
  `gh workflow run desktop-package.yml --ref <branch> -f version=0.0.0-rc.1 -f signpath_test=true`
  after review. End-to-end success is claimed only from that run's report.

## Todo

- [x] Contract note to the orchestrator
- [x] Verifier script and focused tests
- [x] Workflow jobs and contract tests
- [ ] PR, exact-head Codex review, CI
- [ ] Real dispatched run evidence (orchestrator)
