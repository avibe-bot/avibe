# Core release hot path

Owner-approved scope (2026-11-11): shorten tag-to-core publication without removing release checks. This work follows the RC-gate change in #3545; it does not change that gate's acceptance policy.

## Contract

- Select the newest complete six-platform canonical Show Runtime set from the allowed successful producer workflows. Stop discovery once it is found; keep all identity, toolchain and byte-digest checks. Missing/invalid reusable bytes select the existing native build matrix.
- A GitHub-only release can transfer and verify a reusable set on one Linux runner. It must not execute foreign-platform binaries or infer their toolchains from that Linux host. Native builders retain their native shell and publish fresh canonical sets.
- Keep all four final distribution assertions. Run the three independent full-dependency installation checks concurrently, each with a new process, temporary home and venv. No installed environment reuse or dependency omission.
- Reuse UI only from the already-proven full master backstop's exact SHA/run/attempt, with an authenticated artifact digest and successful producer-job association. No proof means no discovery requests; missing/expired/mismatched artifacts rebuild UI locally. Do not publish CI's test-version wheel.
- The publishing job starts after the unchanged gate, prepares notes, then waits for this workflow run/attempt's successful build. Missing/failed/cancelled/stale jobs or artifacts cannot publish. Only the publishing job receives write permissions.
- Preserve predecessor compatibility activation, release version pinning, final package checks, immutable assets, source binding and Stage 2 signing.

## Validation and acceptance

Focused tests cover discovery order and early exit; six-platform reuse and any-member rejection; archive digest/path safety and exact-attempt attribution; isolated parallel acceptance and failure propagation; publish wait success, failure, cancellation, reruns and timeout; workflow DAG/permissions and stable-path compatibility. Existing release contract tests remain authoritative.

Tests use fixtures and temporary state, not real releases or the owner's runtime. Workflow lint and review/CI must pass on the final head. Exact-head owner approval precedes merge by the lead. No real release is dispatched for validation.

Next two authorized RCs after merge must be measured against RC4 (8:10 workflow-created to published) and RC5 (9:23 tag to published; 9:21 workflow-created to published). Measure cache outcomes (including absent/pending full proof and partial-rerun inherited-job fallback), job scheduling gaps, install verification and publication separately. A ~4–5 minute hot-path estimate is not a cold-build or hosted-runner latency guarantee; RC UI fallback is expected while its backstop is running. Public download and mirror readiness are separate from publishedAt and desktop Stage 2.

## Pre-PR evidence

- Rebased independently after #3545 merged (`b53d330`); no stacked PR or gate-policy change.
- 443 focused release tests passed on Python 3.12; changed-file Ruff and Actionlint 1.7.12 passed.
- Read-only live API/byte checks: RC3 canonical Linux archive passed GitHub ZIP SHA256 and all identity/archive/router checks; RC5 backstop run 42947856231 attempt 1 supplied a verified UI artifact through the new consumer.
- Independent review found and prompted fixes for PR/fork producer trust, historical-source dispatch tooling, transient poll failures and publisher time budgeting. Partial-rerun inherited jobs are rejected using actual job start times, not GitHub's relabelled attempt number.
- No real release or workflow was dispatched. Hosted-runner scheduling, real parallel install speed, and end-to-end publication latency remain for the next two owner-authorized RCs.
