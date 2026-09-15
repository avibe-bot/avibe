# Issue 1990 implementation acceptance

Implementation branch: `fix/memory-process-lifecycle-1990`, based on
`99cf95b2718a8b7dfe277de7942a547bf8f98c23`. No merge, release, installation into
the user's tool environment, or live Avibe/Memory restart is authorized.

## Change contract

Direct asyncio child waiting owns lifecycle. `EverOSProcess` retains public
psutil references; the process host stays stateless. Live safety scans preserve
one-second descendant/TCP inspection without creation-time equality. Signals,
orphan recovery and health-probe cleanup keep the same classified references.
Unknown members block unsafe group signals and cleanup proof. Dead/reused
references stay in the execution map; captured descendants remain owned after
reparenting or leaving the group. Failed cleanup retains newly discovered
references. Replacement requires direct-child reaping and tree/group clearance.
Released JSON fields and historical ownership classification remain compatible.
Both distributions require psutil >=7.1.0.

## Evidence layers

- **Native Darwin arm64, psutil 7.1.0 and 7.2.2:**
  `tests/test_memory_process_lifecycle.py` covers several unshortened scan cycles
  during a test-local boot-time shift, public-reference stop, probe capture before
  a shift followed by timeout cleanup, real prohibited TCP listener, and real
  child/grandchild cleanup after leader exit. The private boot-time hook exists
  only in tests and each use validates the observed one-second shift.
- **Controlled generations:** distinct process-generation objects model PID
  reuse with a delayed asyncio exit callback, group uncertainty, permission
  denial, escaped retained children, and preservation of newly discovered
  references after failed cleanup. No real host PID-recycling stress is claimed.
- **Consuming orphan contract:** the released ownership classifier, TERM/KILL,
  waiting and record retirement share one captured reference while diagnostic
  stamps shift. The record remains present during unsuccessful waiting and is
  retired only after the classified execution exits. Existing role/root/UID,
  released helper and missing-record tests remain passing.
- **Supervisor/Wake contracts:** existing race and bounded-budget tests pass;
  `MEMORY-WAKE-202` now also verifies explicit Wake after automatic exhaustion.
- **Actual pinned EverOS 1.2.3 pipeline:** `MEMORY-WAKE-205` runs production Wake,
  capture admission, claims, writer, sidecar, extraction, real native storage,
  indexing and keyword recall. It captures/recalls old input, shifts the Darwin
  display stamp for multiple scan cycles, processes/recalls another input without
  recovery, performs explicit Wake during the shift, then processes/recalls new
  input and confirms old input is still readable. Both psutil floor and resolved
  controller environments passed. The external model/embedding endpoint is a
  loopback HTTP double; no runtime/store substitution or direct database inserts
  are used. This is lifecycle/data continuity evidence, not model-quality evidence.
- **Packaging:** root and companion wheels and sdists were built. Four focused
  declaration/wheel metadata checks verify rejection of 7.0.0 and acceptance of
  7.1.0. UI assets were built solely to satisfy the normal wheel packaging contract.
- **Focused regression:** process, lifecycle, supervisor, Wake and provider tests:
  197 passed, one optional-runtime skip in the ordinary root environment. The
  skipped production-sidecar Wake test separately passed with the provisioned
  pinned runtime. GitHub CI and exact-head review remain delivery gates.

## Reproduction

```sh
uv sync --no-install-project --group dev
uv run --no-sync pytest tests/test_memory_process.py tests/test_memory_process_lifecycle.py tests/test_memory_supervisor.py tests/test_memory_wake.py tests/test_memory_everos.py -q
uv run --no-sync --with 'psutil==7.1.0' pytest tests/test_memory_process_lifecycle.py -q
uv sync --frozen --project scripts/memory_runtime
AVIBE_REQUIRE_MEMORY_RUNTIME_CONTRACT=1 uv run --frozen --project scripts/memory_runtime --with 'pytest==9.0.3' --with 'pytest-asyncio==1.4.0' python -m pytest tests/scenarios/memory_repair/test_memory_lifecycle_runtime.py -q
uv run --no-sync --with 'everos==1.2.3' --with 'psutil==7.1.0' pytest tests/scenarios/memory_repair/test_memory_lifecycle_runtime.py -q
```

Tests redirect HOME/XDG/config and Memory storage to test-owned directories.
Native signals target only child generations created by those tests.

## Explicit residual and known-by-design ledger

1. The local Incus VM was stopped and was started for the authorized regression
   attempt. The runner rejected a new temporary target before provisioning,
   because model and Slack/Discord/Feishu seed variables are required even with
   `--env-file /dev/null --reset-mode none`. No production credentials were read
   or copied and no fake live IM accounts were seeded. Runner `status` and
   `reconcile` confirmed no task project/instance or worktree environment remained.
   The shared VM remains running. The orchestrator approved the hermetic native
   full-pipeline alternative; four-platform Incus deployment remains unverified.
2. The prepared runner invocation, once approved seed configuration is available:
   `INCUS_CMD="limactl shell avibe-incus-regression -- sudo incus" python3 scripts/incus_regression.py up --target worktree --slug memory-lifecycle-1990 --env-file <test-owned-approved-env> --reset-mode none`.
3. No private psutil identity API, native ABI adapter, new persistent schema,
   reconfirmation loop, service-manager integration, IPC, writer-lock redesign,
   `running` projection change, or expanded automatic retry budget was added.
4. psutil checks reduce numeric signaling races; they are not an atomic macOS
   pidfd equivalent. After unrelated automatic recovery exhaustion, users can
   explicitly retry Wake. Installed processes require a separately authorized
   update/restart before receiving the change.
