# Release pipeline recovery

## Evidence and contract

The first repaired v3.1.0 push built every Show/Memory platform successfully,
but the official publish build job failed one of 321 installer regressions:
the real uv provenance test had no uv executable. PR CI supplied the tool;
the release consumer did not. Upload and PyPI jobs never ran. The notes
workflow separately rebuilt all assets although official publication never
consumed those artifacts.

Meanwhile, local/CI wheel preparation treated the repository-wide GitHub
Latest release as an Avibe release. After the incomplete v3.1.0 was withdrawn,
Latest named an engine release without a Show manifest. This also blocks a
workflow repair PR before it can safely restore publication.

The owner approved directed recovery of existing v3.1.0 on September 16, 2026,
and requested a complete audit of these connected release boundaries.
The product tag/source remains fixed; repaired automation is reviewed and
merged separately before explicitly targeting that tag.

## Smallest complete repair

- Supply the existing CI-pinned uv in the official release regression job.
- Build official artifacts only in Publish to PyPI. AI Notes skips its build
  chain for official tags, but preserves the complete GitHub-only preview
  path. A failed/cancelled preview build must never publish.
- Reuse the canonical tag producer to identify/order stable official Avibe
  releases. Paginate before choosing the newest official supplier; ignore
  unrelated engine releases, drafts, and prereleases. Select first, validate
  second: never silently fall back from a corrupt newest official release.
- Let the existing finalizer select Latest for stable official publication,
  including recovery dispatches, without displacing a newer official version.
  Preserve explicit false/preserve modes and all read-back checks.

No runtime code, new production dependency, asset overwrite bypass, test skip,
native engine, production configuration, installation, or service change.
No broad workflow rewrite or new publication mechanism: existing official
build/finalization and exact-source notes ownership remains authoritative.

## Acceptance

- Test actual workflow dependency order and official/preview job conditions.
- Exercise supplier pagination, version ordering, engine/draft/prerelease
  exclusions, explicit tags, invalid newest assets and byte-digest rejection.
- Exercise finalizer decisions against a newer official release and unrelated
  Latest, including exact remote command/read-back behavior.
- Retain package, all-platform/Windows shell, runtime guard, real pip/uv and
  Docker installer, notes/source, immutable upload, and public companion gates.
- Run focused consuming tests and changed-file Ruff, then exact-head Codex
  review, all lint jobs and zero unresolved threads before merging.
- Only then dispatch each official workflow once at one fixed automation
  revision, targeting existing v3.1.0/source bb70e063. Independently verify all
  resulting public assets and core PyPI bytes; Memory stays GitHub-only.

Historical failed runs and old release backups are evidence, not gates to
erase. Publication cannot be reported complete from workflow success alone.

## Local validation evidence

- Release/version/supplier/finalizer/workflow consumers: 340 passed, zero skips.
- Native runtime and migration release guards: 698 passed, zero skips.
- Actual pip and workflow-pinned uv 0.12.10 HTTPS provenance: 2 passed
  (28 unrelated cases explicitly deselected).
- A real two-page release inventory selected official v3.0.14 despite the
  engine Latest release. Its public six-platform manifest bytes passed the
  actual repository/tag/digest validator. API reads used the existing opaque
  GitHub client; the manifest itself was fetched anonymously over HTTPS.
- Fresh core and Memory 3.1.0 sdists and wheels built in isolated environments,
  including wheels rebuilt from those sdists. Core's newly shared build helper
  therefore works outside the source checkout.
- Artifact-bound distribution matrix: 36 passed, zero skips, including actual
  wheel/sdist installation, resolver behavior, exact peer metadata, and builtin
  skill packaging. No package-contract version override was used.
- All seven changed Python files pass Ruff 0.4.9; whitespace checks pass.
- Local UI bytes are accepted rc11 fixtures, not future assets. No runtime
  archives were prepared or executed. Actual Docker and complete current-head
  CI/review remain external gates; no local Docker pass is claimed.
