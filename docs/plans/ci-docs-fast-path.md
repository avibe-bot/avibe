# Documentation-only CI fast path

## Contract

Owner request: avoid a full `lint` run for changes that are proven to affect
only narrative documentation, reducing contention for the organization's
20 hosted-runner slots. Base: `9b63a3009` (#2527). No branch-protection changes,
production behavior, deployment, regression update, or merge authorization.

- Always trigger the workflow; do not use workflow-level path filters, changed
  file API pagination, or the push event's possibly truncated `commits` array.
- A single lightweight `changes` job classifies the **complete Git tree diff**.
  PR: compare the event base to the checked-out synthetic merge tree, verifying
  its two parents match the event base/head. Push: compare `before` to `after`,
  verifying the checked-out SHA and rejecting new/deleted/forced pushes.
  Checkout depth two suffices for PR merge parents; fetch the validated prior
  SHA on a multi-commit push only if missing. Unknown/missing/unreadable evidence
  selects full CI; failure to execute classification leaves the final gates red.
- Disable rename detection so both old and new paths count. Require ordinary
  non-executable Git blobs (100644 / absent); symlinks, executable files,
  submodules, type changes, malformed/empty diffs, mixed code/docs and unsupported
  events select full CI. No user input can request a skip.
- Initial allowlist: root AGENTS.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md,
  SECURITY.md, VISION.md, VISION_ZH.md; `.github/PULL_REQUEST_TEMPLATE.md`;
  narrative `.md` files beneath `docs/`. Contract subtrees (`*-contracts`),
  docs/plans/model-hub.md, docs/plans/model-hub-shell-core-coverage.md and
  docs/INSTALL_FOR_AI{,_ZH}.md stay full because tests consume them. README.md
  and README_ZH.md remain full (distribution metadata/install instructions).
  Runtime Markdown in core/prompts, skills, UI fixtures and test assets is never
  inferred to be documentation by extension alone. New executable docs must be
  excluded when their consumer is introduced.
- `changes` emits an explicit lowercase `docs_only=true|false` and a human
  reason. All expensive root jobs require a successful classification with
  `docs_only=false`. Artifact consumers remain dependent on the artifact job.
- Preserve all nine required check names. The three existing aggregate jobs
  remain `always()`, also require `changes`, and accept skipped producers **only**
  for an explicit successful docs-only classification. Full-CI paths still
  reject failure, cancellation, skip, or missing results. Leaf jobs skipped by
  an intentional job-level condition are completed/skipped GitHub checks, not
  absent/pending workflow checks. No fake check-runs or extra write permissions.
- Documentation fast path uses four small hosted jobs (classifier + existing
  three aggregate checks), no package/dependency installs or test matrices.
  This audits change scope, not Markdown prose quality. Code PR adds only the
  classifier's small startup dependency, not full Git history downloads.

## Validation plan

Use existing pytest (no new test framework) with temporary real Git repositories
and event fixtures: complete multi-commit diffs, changes beyond 300 files,
renames across code/docs, non-ASCII names, mixed changes, merge parent/SHA
mismatches, forced/new pushes, absent revisions and ambiguous events. Verify
real workflow aggregate shell behavior for docs/full/error and each producer
result, and changed job selection/required contexts. Keep existing CI boundary
coverage rather than adding assertions of tool spelling.

First PR run must use full CI because workflow/classifier/tests change. Exercise
the classifier against a real historical docs-only PR's merge and event SHAs
with the new script, and execute the actual aggregate shell steps locally for
both paths. A real docs-only GitHub scheduling result remains a post-merge
acceptance observation: do not add a bypass input, create a stacked PR or alter
another Agent's PR just to manufacture it. No unrelated PRs are modified or
cancelled. Exact-head Codex, all expected CI results and zero open review
threads remain close-out gates.

## Ownership

Session sesp2bd67rp9c owns implementation, independent review decisions and the
single delivery PR. A read-only design reviewer may challenge the skip boundary;
no parallel mutable lane is needed for this tightly coupled workflow change.
