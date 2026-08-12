# Memory Repair / Factory Reset Integration

Do not apply these edits until PR #1332 lands with its current review fixes.
The final integration worker must preserve both operations' retained-task and
closed-response contracts while adding the gates below.

Reference inspected: PR #1332 head `9d20ce6b` on 2026-08-11. Re-read the merged
head before applying this map because the six open P1 fixes may move these
locations. This branch intentionally does not integrate any #1332 commit.

## Bidirectional Admission Gate

The gate must cover both request orders, not only the shared operation lease:

1. In `vibe/ui_memory_routes.py`, include the loop-local Repair task in the
   mutation-running predicate. The Repair route must reject a running factory
   reset both before and inside `_memory_settings_write_lock()`. The factory
   reset route must symmetrically reject a running Repair at those same two
   points. Keep each existing retained task shielded and preserve the exact
   `{ok, error, result}` conflict envelope.
2. In `core/internal_server.py`, make `/internal/memory/repair` reject while
   `controller._memory_factory_reset_task` is live. Make
   `/internal/memory/factory-reset` reject when the selected Runtime reports
   either `_rebuild_running()` or `_repair_running()`. Keep the signed Memory UI
   proof check and exact confirmation body ahead of all mutation work.
3. In `core/memory/runtime.py`, carry Repair into PR #1332's retirement model:
   `repair()` must reject `_retired`, `_closing`, and a `factory_reset` recovery
   intent. Preserve Repair in all existing Runtime mutation conflict predicates.
   Repair's `MemoryOperationLease` and factory reset's lease acquisition before
   `runtime.retire()` are the cross-controller/process serialization boundary;
   neither operation may wait through the other's lease.
4. Add race tests for both directions: a retained Repair makes factory reset
   return `memory_operation_in_progress` without retiring the Runtime, and a
   retained factory reset makes Repair return the same closed conflict without
   launching `EverOSSyncProcess`. Also cover the checks immediately before and
   after the UI settings lock is acquired.

## Controller Shutdown Order

PR #1332 adds `_join_memory_factory_reset_task()`, because reset can replace
`controller.memory_runtime`. Preserve this exact shutdown order in
`Controller.stop()`:

1. Stop the internal dispatch server so no new Repair or reset can enter.
2. Settle startup Memory reconciliation.
3. Join `_memory_factory_reset_task` with no reporting timeout.
4. Drain Memory capture tasks.
5. Only then read the current `self.memory_runtime` and close that aggregate.

Within `MemoryRuntime.close()`, cancel and join `_repair_task` before the rebuild
join and before `_close_after_rebuild()` starts ordinary worker, retention, and
sidecar shutdown. Never cache the Runtime before the Controller joins factory
reset: the task may publish a fresh aggregate, and closing the old value would
leave the fresh sidecar and worker alive after Controller shutdown.

Required close-order tests should hold a reset immediately before fresh Runtime
publication and hold a Repair in its owned-child cleanup. Controller shutdown
must wait for reset publication, then close the published Runtime; Runtime close
must not release its operation lease until the Repair child and ownership record
are fully retired.
