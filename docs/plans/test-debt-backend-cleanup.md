# Backend Test-Debt Cleanup Plan

## Evidence and ownership

- The two `tests/test_platform_registry.py` failures are caused by
  `tests/test_slack_app_mention_empty.py`. Its collection-time loader removes
  `modules.im.slack` from `sys.modules` and executes the file again under the
  canonical module name. `PlatformDescriptor.create_client()` then imports the
  replacement class, while the registry tests retain the original `SlackBot`
  class object. The test loader owns this mutation and must reuse the canonical
  module instead of replacing process-global import state.
- The Show Runtime orphan-sweep failure is not leaked global state. The test
  calls the sweep immediately after `Popen`; the kernel can expose the child PID
  before the child completes `exec`, so `psutil` temporarily reports an empty or
  parent-shaped command line. An Incus stress probe missed 7 of 200 immediate
  command-line reads. The test helper owns child readiness and will use an
  explicit stdout-pipe byte from the executed child, with no sleep, polling,
  platform-specific file-descriptor passing, or production behavior change.
- The file-backed `TaskExecutionStore` owns its reload token. Directory
  `(mtime_ns, size, inode)` metadata can remain identical when a JSON entry is
  added or moved within one ext4 clock tick. The signature will instead be the
  sorted committed `*.json` entry set for each state directory, including every
  entry's `(mtime_ns, size, inode)`. This detects names, moves, and the store's
  atomic replacement writes while ignoring uncommitted `*.tmp` files. The
  SQLite branch remains unchanged.
- Pytest currently loads AnyIO but not `pytest-asyncio`: `asyncio_mode` is an
  unknown option and the five `@pytest.mark.asyncio` integration methods have an
  unknown marker. They are skipped earlier when credentials are absent, which
  hides that they cannot execute under their declared runner contract. Add
  `pytest-asyncio` explicitly to the dev dependency group and lockfile, and run
  the existing active-event-loop bridge test as a native async test so the
  ordinary suite enforces `asyncio_mode = "auto"` even without E2E credentials.
  The sharded unit-test workflow must install the declared dev group rather
  than maintaining a separate partial test-dependency list.
- The first post-fix Incus full suite exposed two additional same-tick
  assumptions. File Browser returns `mtime` as its optimistic-concurrency token,
  but an atomic replacement can inherit the saved file's token on ext4; the
  writer owns advancing a successful replacement's token beyond the accepted
  baseline, verifying the post-`utime` stat, using a coarse-filesystem fallback,
  and failing before replacement if no distinct token is observable. Contents
  and the advanced token are one durable publication unit, so the writer also
  keeps the temp descriptor open and flushes the token mutation before replace;
  a failed durability flush must leave the original file published.
  Processed-message retention orders only by `created_at`, while
  multiple SQLite inserts can receive the same wall-clock value; the SQLite
  store owns deterministic insertion order and will use the table rowid only as
  the tie-breaker for equal timestamps. Neither fix needs a sleep, retry, or
  schema change.

## Invariants

1. Tests never replace a canonical production module in `sys.modules` merely to
   load the same source again.
2. A real-process test does not inspect child argv until the child proves that
   `exec` completed.
3. Every committed file-backend queue transition changes the observed entry
   signature even when directory timestamps do not advance.
4. SQLite task execution behavior and intentional test skips are unchanged.
5. The configured async pytest mode and marker are provided by an explicit
   development dependency.
6. A successful conditional file replacement invalidates its accepted `mtime`
   token even when the filesystem initially timestamps both versions equally,
   and durably flushes that token before publishing the replacement.
7. Equal-timestamp processed-message claims retain and load in SQLite insertion
   order.

## Acceptance

1. Run the loader-isolation regression before the platform registry tests in one
   pytest process, then run the reverse order.
2. Run the Show Runtime orphan-sweep module repeatedly in the lane's Incus
   target and confirm the real child is immediately discoverable after the
   readiness handshake.
3. Run the file-backend reload test with directory signatures forced constant,
   followed by neighboring scheduled-task store tests.
4. Run Ruff on every changed Python file and `git diff --check`.
5. In the isolated Incus target, run the complete backend suite in one process
   with `VIBE_SHOW_RUNTIME_SOURCE` unset. Record exact pass, skip, fail, and
   warning counts; confirm `pytest-asyncio` is loaded and the previously failing
   order is clean.
6. Force a replacement temp file to the accepted `mtime` and force 205
   processed-message claims to one timestamp; confirm stale writes still
   conflict and retention keeps the newest 200 claims in insertion order. Pin
   the conditional-write order as token `utime`, temp-file `fsync`, then
   `os.replace`, and prove a failed token flush cannot publish.

## Verification results

- Deterministic red tests reproduced the canonical Slack module replacement,
  the unchanged directory-stat reload token, a same-`mtime` atomic replacement,
  a missing durability flush between token advancement and replacement,
  equal-timestamp SQLite retention, and the missing native async runner.
- The platform/Slack order passed in both directions (`19 passed` each), and the
  changed modules plus their neighboring suites passed with `482 passed, 1
  skipped, 379 warnings`.
- The isolated Incus full suite ran in one process with `pytest_asyncio.plugin`
  registered and `VIBE_SHOW_RUNTIME_SOURCE` unset: `7154 passed, 55 skipped,
  3904 warnings` in 870.92 seconds, with zero failures.
