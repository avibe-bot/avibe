# CI Toolchain and Upgrade Failure Evidence

## Ordered Scope

The owner approved stability diagnosis before further test preparation savings
and, only after complete multi-run evidence, shard balancing. This first change
owns CI tool installation and the missing evidence from the historical upgrade
fixture. It does not declare the full optimization request complete.

## Observations

- At `2f7d9a21c5`, run `33998201417` failed before Python tests: setup-uv
  could not fetch `astral-sh/versions/main/v1/uv.ndjson`.
- Inspection of setup-uv `11f9893b081a58869d3b5fccaea48c9e9e46f990`
  shows that exact versions bypass latest-version resolution but cold downloads
  still call `getArtifact`, which fetches that manifest. Pinning alone would not
  remove the observed dependency.
- At `0abad3c5b8`, run `33996154247` passed all 21 distribution contracts,
  then the real 3.0.13 upgrade fixture timed out waiting for split Memory.
  Core upgraded to 9999.0.0, but the observed launcher still pointed to an
  environment without avibe-memory. The fixture deleted the container without
  reporting its background install state or service logs.
- A local isolated probe used the SHA-256-verified published 3.0.13 wheel and
  its real config API, then loaded the resulting file with source `67cb0bc313`.
  The recovery warning concerns Model Hub; Memory remains enabled and required.
  This rules out disabled Memory from that recovery as the explanation. It does
  not establish why the failed CI installation did not complete.

## Smallest Current Change

- Install uv directly with Python's existing pip, exact versions and binary-only,
  dependency-free package selection. Keep the known green 0.12.10 for core CI and
  the existing 0.9.18 contract for the pinned Memory Runtime. This removes the
  additional remote versions-manifest lookup, not every network dependency.
- Replace setup-uv's implicit cache ownership with SHA-pinned actions/cache v5.
  Unit shards and migration history only restore; installer runners and the
  isolated Memory Runtime populate their respective caches. Keys retain OS,
  architecture, Python, uv and dependency identities. No new application dependency.
- On an upgrade fixture failure, print bounded tails of only its explicit runtime
  logs and automatic Memory repair state before Docker cleanup. Do not print its
  config or entire state directory, change the exit status, increase timeouts, or
  retry the upgrade. Preserve the real released-wheel and service-start assertions.

## Invariants and Validation

All 17 gates, per-file isolation, 300-second watchdog, 20-minute shard bound,
real migration and install coverage remain. Tool installation and required
dependencies must fail closed. Native contracts cover command execution and
failure evidence on success, failure and nonstandard exit codes.

The old upgrade failure remains unexplained until reproducible evidence or the
new runtime diagnostics identify its cause; a subsequent green run is not proof
of a fix. Docker is unavailable on the local workstation, so the real container
case must run in the unchanged installer CI gate. Further approved work will
profile resource-access/CLI/update-checker setup before changing fixtures, and
will not rebalance from runner variance alone.

Local evidence: 55 workflow/shard/fixture/diagnostic contracts and 188 original
installer/upgrade cases passed. Ruff 0.4.9, dependency consistency and whitespace
checks passed; the actual binary-only pip install produced uv 0.12.10 in the
private worktree environment. The Docker case is excluded locally, not removed
from CI. The first new cache key is necessarily cold and must not be compared as
though cache warmth were held constant.

Integration inspection: master `2f92bd009` adds optional Memory sender-name
propagation. Its producer, writer retry/downgrade consumer and tests were inspected;
it does not overlap this four-file scope or change package-install admission.
