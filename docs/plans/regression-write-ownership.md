# Regression Write Ownership

## Problem

Every local Incus deployment recursively changes ownership of the service home,
source, and dependency environments. An observed master update spent about half
an hour walking existing runtime directories. The shared VM disk saturated and
another regression instance's Model Hub management health request timed out.

## Invariants

- Preparing an existing instance visits only its fixed service directories;
  existing descendants keep their contents, permissions, and ownership metadata.
- Source extraction, dependency installation, and state seeding create files as
  the service user. Ownership is established by the writer, not a later walk.
- New base images install backends as the service user. First boot alone repairs
  root-owned backend installation paths inherited from older images; it never
  recursively changes ownership of the home or unrelated runtime directories.
- Source sync preserves the existing exclusion and stale-file removal contract.
- A usable Python environment is reused. Creating a missing environment forces
  dependency installation regardless of prior fingerprints; creation failures
  stop deployment.
- Health failures identify the local endpoint, bounded failure category, HTTP
  status when available, and elapsed time. Logs contain no response bodies,
  credential values, or arbitrary upstream error text. Repeated identical
  failures do not create repeated warnings; recovery is recorded.

## Scope

Update the Incus runner and focused tests, plus Model Hub health diagnostics and
their tests. Preserve product state, reset semantics, runtime health values,
health deadlines, and deployment fingerprints. Do not move VM storage or change
machine resources as part of this change.

## Validation

- Execute generated shell commands against test-owned filesystem trees.
- Verify source payloads, modes, symlinks, and preserved descendants.
- Exercise fresh, existing, and missing Python environment paths.
- Test both health endpoints, failure classification, redaction, and recovery.
- Run focused pytest and Ruff checks, then the required PR review and CI gates.
- Verify the deployment path in local Incus with accumulated state preserved.
