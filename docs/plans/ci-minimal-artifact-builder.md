# Minimal CI Artifact Builder

## Scope and Evidence

This is phase two of the approved order: stability, build/install preparation,
then repeated work inside test calls. Phase one (#1905) is merged and its exact
master source passed all 17 checks, 405 unit-file exits/metrics, 21 distribution
contracts and 196 installer cases. The remaining test-call investigation is not
claimed complete by this PR.

The artifact producer installed the whole application and its runtime dependency
graph before running isolated PEP 517 builds. Inspection of the manifest helper,
Hatch hook and exact-peer metadata hook found only standard-library operations
and declared build-system requirements. The producer does not run application
behavior; separate private installation jobs own that validation.

A fresh local builder containing only pip, build, packaging and pyproject-hooks
produced real core and Memory wheels and source distributions. Neither the app
nor its runtime dependencies were installed in that builder before or after.
The original manifest preparation validated the published runtime manifest. Both
distribution pairs used the exact 3.0.99rc1 peer contract. All 21 existing actual
distribution tests passed against those artifacts, including isolated source
installation. The local UI placeholder does not substitute for CI's production
UI build, Docker regressions or Windows installation.

## Smallest Complete Change

Install only the existing build frontend in the producer. Each isolated build
continues installing its declared backend requirements. Preserve the original
manifest preparation, core/Memory wheel and sdist builds, contract versions,
production UI, artifact identities and same-run consumers. No build isolation
is disabled. No runner, dependency declaration, timeout, test selection or
application behavior changes.

Workflow contracts pin the minimal command sequence and execute that sequence
with a controlled Python command to prove failure propagation at every boundary.
All existing unit/UI and distribution/install aggregates still fail closed.

## Verification and Delivery

Run workflow, shard and fixture contracts, source packaging contracts, lint and
package requirement checks. Require all 21 actual distribution contracts in the
installer CI with no skips; retain the real released-wheel upgrade and Windows
smoke. The full expected check set remains 17. Complete current-head bot review,
every exact-head lint run, whole-PR thread inventory and clean integration gate
are mandatory before guarded squash. Verify exact-source master CI afterward.

Baseline #1905 PR run 34008949861 took 416 seconds; producer 103 seconds and
installer 302 seconds. Merged master 34009487671 took 442 seconds, with a Python
shard as the tail. Prior master 34007354215 took 361 seconds with producer 122
seconds. These varying samples are not a stable speed guarantee. Compare actual
build-step and build-to-install timestamps after this PR; the successful local
build is capability evidence, not a claimed CI speedup.

Initial findings-bearing review-head inventory is zero. No allocated cross-lane
identifiers, production operations or global environment changes are involved.

Local workflow/shard/fixture/source contracts passed 70 cases. Seven artifact-only
cases skip when wheel paths are absent; all 21 distribution cases separately
passed with the minimal builder's real artifacts (136.41 seconds). Ruff 0.4.9,
all 84 private test-environment package requirements and whitespace checks pass.
