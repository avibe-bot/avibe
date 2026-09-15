# Memory subprocess lifecycle — issue 1990

Owner-approved implementation summary, 2026-09-15. The full investigation and
review remain outside this PR. This summary preserves the approved product contract.

## Problem and decision

On macOS, psutil's public display creation time can shift while a child remains
the same execution. Avibe repeatedly compared that display value, rejected a
healthy Memory child, and exhausted bounded Wake retries without clearing it.

Use direct `asyncio` child waiting as lifecycle authority. Keep public retained
`psutil.Process` references for action-boundary reuse protection. Remove the
independent 0.2-second authentication loop and readiness timestamp gate; preserve
the one-second descendant/TCP safety inspection. A timestamp shift alone neither
marks the child down nor pauses claims or consumes restart attempts.

## Ownership and cleanup invariants

- `EverOSProcess._owned_processes` owns one map of references; `_ProcessHost`
  stays stateless. The probe and orphan reapers use execution-local maps.
- Capture before readiness. Preserve references across reparenting, group escape,
  and failed cleanup; do not discard a constructed reference on extra read failure.
- Gone/reused references stay terminal in that execution map. Never recapture
  their PIDs into another generation. `None` means unresolved presence, not exit.
- Use retained public references for individual signals. Group signals require
  every current member to match a confirmed owned reference and cannot target
  Avibe's own group. Also signal retained children outside the original group.
- TERM, bounded wait, KILL and final wait remain. Direct-child reaping alone is
  insufficient: known descendants and unknown group members block cleanup.
  Late helpers after leader exit enter each bounded round through existing
  root/socket/role group classification; foreign members block replacement.
- Existing locks, child-object identity and supervisor generations serialize
  stop, natural exit, Wake and close. No new lifecycle state machine is added.

## Compatibility and bounded recovery

Released JSON shapes and historical command/UID/role/interpreter/root/socket
classification remain unchanged. Capture a reference before classification reads
and validate it afterward; retain it through signals and exit waiting. Historical
stamps remain record fields, never live-child authentication authority.

Both root and companion distributions require `psutil>=7.1.0`, which provides
the public monotonic identity behavior needed on macOS and Linux. The lockfile
resolves 7.2.2. Built wheel metadata enforces the floor independently of the lock.

Automatic recovery keeps its existing bounded budget. After unrelated failures
exhaust it, `degraded` with `memory_wake_failed` requires an explicit **Retry
startup** (Wake) once cleanup is possible. Wake preserves existing data.

## Validation and reproduction

Scenario IDs: MEMORY-WAKE-001, MEMORY-WAKE-202, MEMORY-WAKE-204, MEMORY-WAKE-205.

- Focused process, lifecycle, supervisor, Wake and provider regression: 215 passed.
- Darwin arm64 psutil floor and resolved environments: 32 passed each, including
  real processing/recall. Boot-time injection is test-only and checks its hook.
- Native tests cover full scan cycles during a shift, probe capture before shift
  then timeout cleanup, a prohibited TCP listener, and leader/child/grandchild exit.
- Controlled distinct generations cover delayed exit callbacks, PID reuse,
  unknown/denied members, group escape, and failed-stop reference retention.
  Native orphan reaping covers classification, shift, waiting and record retirement.
- Actual production Wake, admission, claims, writer, EverOS 1.2.3 extraction,
  storage/indexing and keyword recall preserve old input, process another input
  during a shift without recovery, then process/recall new input after Wake.
- Root/companion wheel and sdist builds passed; four declaration/built-wheel
  dependency checks passed. Changed-file Ruff and normal UI asset build passed.

Old-test mapping: health-only pinned Wake -> full MEMORY-WAKE-205 (required in CI);
fake lifecycle self-tests -> supervisor callback/non-overlap consumers; generic
orphan success -> shift/retirement case covering all three released record shapes;
native orphan helper -> consuming reaper; duplicate capture-read dimension merged.
PM follow-up: ordinary exits skip classification but retain surviving-group cleanup;
missing record identity is explicit, and tautological legacy sync equality is removed.

```sh
uv sync --no-install-project --group dev
uv run --no-sync pytest tests/test_memory_process.py tests/test_memory_process_lifecycle.py tests/test_memory_supervisor.py tests/test_memory_wake.py tests/test_memory_everos.py -q
uv sync --frozen --project scripts/memory_runtime
AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT=1 uv run --frozen --project scripts/memory_runtime --with 'pytest==9.0.3' --with 'pytest-asyncio==1.4.0' python -m pytest tests/test_memory_process_lifecycle.py tests/scenarios/memory_repair/test_memory_lifecycle_runtime.py -q
AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT=1 uv run --no-sync --with 'everos==1.2.3' --with 'psutil==7.1.0' pytest tests/test_memory_process_lifecycle.py tests/scenarios/memory_repair/test_memory_lifecycle_runtime.py -q
```

## Evidence limits

Tests own their HOME/XDG/config/storage and every signaled child. The loopback
HTTP server replaces only external model/1024-dimensional embedding answers;
there are no runtime/storage doubles or direct DB inserts. This proves lifecycle
and data continuity, not model quality or real host PID-recycling stress.

Local Incus provisioning was blocked before target creation by required IM/model
seed configuration. Runner status/reconcile confirmed no task target remained.
The shared Lima VM was started and left running. The orchestrator approved native
full-pipeline evidence instead; four-platform Incus deployment remains unverified.
No production credentials or regression state were copied/reset.

If no initial reference is acquired, cleanup stays unknown (a probe may remain
unreaped); a later PID or delayed child callback cannot authorize recapture.
Public process checks are not an atomic macOS pidfd. No private identity API,
native ABI, new record schema, reconfirmation loop, service-manager integration,
IPC, writer-lock redesign, running-property change or expanded retries were added.
