# Avibe Release Workflow

This document contains the release-specific details that are too operational
for `AGENTS.md`. The workflows are the source of truth for the final commands.

## Select the release commit

Fetch `origin/master` and select its latest commit when starting a release. Record
that exact SHA for all release-only validation and tag creation; do not switch the
candidate silently while checks run. New PRs may continue through the merge queue:
there is no routine release-time merge freeze or requirement to wait for duplicate
`push: master` lint, iOS-shell, or desktop-shell runs.

Require successful merge-group validation for the candidate's landing tree, traced
through the merged PR/queue timeline and its associated `merge_group` Actions runs.
A green PR head alone is insufficient, and a synthetic queue head is not the commit
to tag. If queue evidence is unavailable (for example, a pre-queue bootstrap commit),
run the applicable validation explicitly against the selected SHA rather than
assuming missing checks passed. Keep the post-merge cache and release-guard jobs;
the queue replaces duplicated pre-merge suites, not release-specific safeguards.

Before pushing a tag, fetch master again. If the release is required to include
newly landed work, select the new tip and repeat candidate-specific gates; otherwise
retain the recorded, validated candidate. Require the owner's release authorization
in either case. The stable-release E2E below and asset-complete publication gates
remain mandatory.

## Tags and notifications

- Tags advance the latest version number, for example `v1.0.1` to `v1.0.2`.
- For an official `v*` release, the annotated tag is the only release-state
  input. Put `<!-- avibe:update-notification=none -->` in the tag annotation
  when the release should suppress update and post-update notifications while
  automatic updates remain enabled.
- The marker also silences the desktop app: its automatic startup check does
  not prompt for a silent release, which still shows as available and is
  offered by an explicit check or a remote install request. The shell reads
  the marker from the mirror index's `update_notification` field, or from the
  release notes when it falls back to the GitHub API. Desktop apps built
  before this rule still prompt.
- Push the tag and let the official workflows create the release. Do not
  pre-create or manually edit a GitHub Release.
- The workflow emits both current and legacy `vibe-remote` update markers for
  installed-client compatibility.

## Workflow ownership and artifacts

The official workflows stage assets and generated notes in a Draft.
`Release (AI Notes)` may update notes but never publishes. `Publish to PyPI` is
the finalizer: it verifies the exact notes run, publishes the asset-complete
GitHub Release, and only then allows PyPI publication.

GitHub-only pre-releases use the `gh-vX.Y.ZrcN` format, such as
`gh-v2.2.8rc2`, so they remain distinct from PyPI-triggering `v*` tags. They
must include an installable wheel containing `ui/dist` and
`vibe/show_runtime/*.tgz`, plus the source distribution.

## Release E2E

Pull-request CI does not run the hermetic Model Hub E2E (`-m e2e_model_hub`).
`Model Hub release E2E` runs it nightly and on demand on a clean runner, with
the pinned engine and the latest agent CLIs. Before tagging a stable `v*`
release, dispatch it on the release commit and require a green run at that
exact SHA; a strict `xfail` names a known, tracked gap, not a pass to ignore.

## Managed-runtime manifests

Published managed-runtime manifests are availability contracts. Keep their
release URLs under the scheduled manifest-verified backup/recovery guard.
Publish replacement assets before changing a pinned manifest so the guard never
restores bytes from a different release. A manifest that pins a third-party
release that Avibe cannot republish (ripgrep, cloudflared) relies on the download mirror as
that guard: the mirror holds the pinned release, verified against the manifest.

## Download mirror

`Mirror releases to dl.avibe.bot` copies published release assets to
`https://dl.avibe.bot/releases/<tag>/<asset>` and writes the release index at
`https://dl.avibe.bot/index/releases.json`. It follows the publishing
workflows, so a renamed publishing workflow must also be renamed in its
trigger. It also keeps version-less links to the newest stable desktop
installers, such as
`https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg`. The contract and retention rules are in
`docs/plans/release-download-mirror.md`.
