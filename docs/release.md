# Avibe Release Workflow

This document contains the release-specific details that are too operational
for `AGENTS.md`. The workflows are the source of truth for the final commands.

## Select the release commit

Fetch `origin/master` and record the candidate's exact SHA for all release-only
validation and tag creation; do not switch it silently while checks run. New PRs
may continue to merge: there is no routine release-time merge freeze.

Require a successful **full master backstop at the exact candidate SHA** for
every release, stable included. `lint.yml` runs that full pass after master
pushes, including iOS, desktop, browser, installer, web-fetch and Windows
checks. A running pass finishes; only the newest pending master pass waits.
`release_ai.yml`, `publish.yml`, and `desktop-release.yml` enforce the full
master backstop before their respective build, signing, or publication paths.
See [CI scope and routing](plans/ci-affected-routing.md). The PR-only iOS
NavigationPolicyTests quarantine (#3418) does not relax this release gate:
iOS must pass in the exact full master run, even while its PR check is advisory.

A green PR head, another SHA's full run, or a missing/coalesced run is not
proof. Select a full-green master SHA; wait for its current full pass, or rerun
its existing master backstop. Manual full validation is available on master.
Historical runs with no explicit full-backstop certificate are not assumed
equivalent.

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

The official workflows stage notes and release assets in one immutable tag/release.
`Release (AI Notes)` generates notes and publishes the core/runtime assets for
GitHub-only pre-releases; for official stable tags it leaves a Draft for
`Publish to PyPI`, which remains the stable core/PyPI finalizer after exact
notes verification. `Desktop Release (Stage 2)` runs independently after the
core stage, preserves the exact tag/source binding, and attaches the verified
signed desktop set to the same release. The updater and mirror each treat a
partial desktop set as unavailable until Stage 2 completes.

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
`https://dl.avibe.bot/index/releases.json`. It follows `Publish to PyPI`,
`Release (AI Notes)`, and `Desktop Release (Stage 2)`, so either stage can
reconcile the same release without overwriting immutable bytes. It also keeps
version-less links to the newest stable desktop installers, such as
`https://dl.avibe.bot/latest/Avibe_aarch64-apple-darwin.dmg`. The contract and retention rules are in
`docs/plans/release-download-mirror.md`.
