# Preview Install Convergence

> Status: implemented (scenario MEMORY-INDEP-027)
>
> Scope: a `gh-v*` preview install or preview-to-preview upgrade is one
> copyable `uv tool install` command for the core wheel; the Memory companion
> converges automatically afterward. No CLI surface changes. The official
> `v*` / PyPI publication and upgrade contract is untouched.
>
> Replaces the earlier `memory-preview-bundle` and `preview-upgrade` drafts.
> Deliberately dropped from those drafts: bundle manifest asset, digest
> pipeline, GitHub API discovery, new CLI flags, and Web UI channel control.

## Problem

A `gh-v*` release attaches the `avibe-os` and `avibe-memory` wheels as GitHub
Release assets and publishes nothing to PyPI. Installing a preview therefore
means assembling `uv tool install <core-url> --with <memory-url> --force` from
two long asset URLs, and the operator must remember the Memory wheel even when
Memory is already installed. Installing only the core is worse than
inconvenient: the post-ready Memory package repair treats the rc version as
published, pins `avibe-memory==<rc>` against PyPI where it does not exist, and
burns its bounded three-attempt budget on installs that can never succeed.

## Design

The single user action, for first install and for every preview-to-preview
upgrade, is the one command printed in the `gh-v*` release notes:

```bash
uv tool install https://github.com/avibe-bot/avibe/releases/download/<tag>/avibe_os-<version>-py3-none-any.whl --force
```

`uv tool install --force` rebuilds the tool environment from that single spec,
so the previously installed `avibe-memory` is dropped — intentionally. After
the service restarts and reaches readiness, the **existing** reconciler
(`reconcile_memory_package_on_startup` → `_prepare_memory_package_job`)
observes that Memory is required and the companion is missing or
version-mismatched, and repairs it. The only change is where the repair points
its package specs when the running version is a preview.

### Preview-aware repair specs

When the running version is a clean PEP 440 prerelease (`is_prerelease`, not a
dev release, no local segment), the repair plan's two specs are derived — not
discovered — from the fixed release convention:

- tag: `gh-v<version>` (the normalized running version)
- core: `https://github.com/avibe-bot/avibe/releases/download/gh-v<version>/avibe_os-<version>-py3-none-any.whl`
- memory: `https://github.com/avibe-bot/avibe/releases/download/gh-v<version>/avibe_memory-<version>-py3-none-any.whl`

No GitHub API call is made; the URLs are pure functions of the running
version. Non-prerelease versions keep today's exact PyPI pins. Dev and local
versions keep today's refusal (`memory_package_unpublished_build`).

The wheel filename embeds the normalized version, so tag and filename
derivation share one normalization; the released artifacts' reciprocal exact
pins (`avibe-os==X ⇄ avibe-memory==X` in published METADATA) remain the
pairing integrity gate — uv fails resolution if the pair does not match.

### Plan builder extension

`build_upgrade_plan` gains two optional parameters honored only in
exact-version mode: `core_spec` and `memory_spec`. When set, they replace the
`avibe-os==<version>` / `avibe-memory==<version>` pins in the install and
preflight commands. Defaults are `None`, and every existing call site passes
neither, so all current plans — stable CLI upgrade, stable Web upgrade, stable
repair — are byte-identical to today. Exact-version semantics (install into
the current tool dir, `--force`, no `--upgrade`, service-scope restart) are
unchanged.

### Surfaces that benefit without changes

- the automatic startup reconciler;
- the manual "install memory-package" job on the Web UI Dependencies page
  (same `_prepare_memory_package_job` implementation);
- the bounded three-attempt budget, shared upgrade lock, integrity
  verification, and `memory-package-repair` service restart, all reused as-is.

## Behavior

| Situation | Result |
| --- | --- |
| preview core installed, Memory enabled or previously installed | repair installs the exact-version wheel pair from the GitHub release, then service restart |
| preview core installed, Memory disabled and absent | reconciler skips; zero download, zero install |
| preview release or asset missing (404), network failure | structured `memory_package_install_failed`; attempt budget consumed; stays repairable from the Dependencies page |
| dev / local / source build | refused before any network access, as today |
| stable version running | exact PyPI pins, byte-identical to today |
| preview → newer preview | same single `uv tool install` command from the new tag's notes |
| preview → stable | ordinary `vibe upgrade` once PyPI has a newer official version; the stable path carries Memory automatically |

## Official upgrade path — invariants

This change must not alter any official `v*` behavior:

1. `vibe upgrade` (CLI) and Web `do_upgrade` never pass the new parameters;
   their plans are unchanged for every input.
2. The stable repair/reconcile branch is selected by `is_prerelease == False`
   and keeps today's exact PyPI pins.
3. `publish.yml`, PyPI ordering (Memory before core), and release asset
   contracts are untouched.
4. Regression tests assert the stable plan shapes byte-for-byte (extending the
   existing exact-plan contract test in `tests/test_upgrade_flow.py`).

## Failure and security bounds

- Fixed repository, HTTPS only, derived URLs; no redirect-following logic of
  our own — uv performs the download inside the existing install step.
- All failures surface through the existing structured repair results; no new
  failure lifecycle, no rollback, no reservation, no quarantine.
- Error output follows the existing truncation and never adds credentials or
  URLs beyond what the install command already contains.

## Testing

- URL/tag derivation unit tests: rc/a/b versions, normalization, dev/local
  refusal, stable versions bypass;
- plan-shape tests: `core_spec`/`memory_spec` rendering in uv and pip exact
  plans and their preflights; omitted parameters produce today's commands
  byte-for-byte;
- repair tests: prerelease running version produces derived URL specs;
  attempt budget, lock, and restart behavior unchanged; 404-style install
  failure yields the existing structured result;
- reconciler tests: required+missing converges, disabled skips, mismatch at a
  preview version converges to the running version's pair.

## Delivery

One PR: derivation helper + `core_spec`/`memory_spec` + preview branch in
`_prepare_memory_package_job` + tests. No publish workflow asset changes.

### As shipped

- `preview_release_specs` in `vibe/upgrade.py` derives the pair, and
  `build_upgrade_plan` renders `memory_spec` as the PEP 508 direct reference
  `avibe-memory @ <url>` so every installer still reads it as a named
  distribution rather than an anonymous artifact.
- Sources without an exact `version` raise `ValueError` instead of being
  ignored. A forward upgrade resolves the newest release and has no known pair
  to name, so a silently dropped spec would read as applied — this is a
  decision beyond the original draft, and the invariant-1 tests assert it.
- The optional `gh-v*` release-notes template line is **not** in this PR. The
  notes are produced by the shared `release_ai.yml` generator, which also
  writes official `v*` notes, so changing it would put invariant 3 at risk for
  a text-only convenience. It stays a separate workflow-side change.
