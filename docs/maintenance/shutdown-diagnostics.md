# Late service shutdown diagnostics

## Change contract

A managed service stop currently allows five seconds before escalation. The
controller's existing `controller_cleanup_sync` completion record does not
prove the Python process exited: loop closure, diagnostic-thread joins, log
drain and interpreter executor joins can still follow it. This instrumentation
makes that tail observable without changing the stop deadline, signal policy,
service lease, recovery ownership or worker cleanup.

On the first handled SIGTERM/SIGINT, `main.py` arms `ShutdownDiagnostics` and
reports its destination in the application log. The diagnostic writer creates
one private, exclusively created `logs/shutdown-<pid>-<time_ns>.log` under the
configured Avibe home. It does not reuse an existing file or a logging handler.
Only its daemon thread opens/writes the sink; cleanup merely enqueues small
records. Failure or a blocked destination cannot hold up service shutdown.

The file records UTC timestamps, PID and milliseconds since the first signal:

- `shutdown_requested`
- `controller_run_return`: the controller's `run()` has left, after its cleanup
- `macos_monitor_stop_start` / `macos_monitor_stop_return`: around each existing
  monitor stop/join (the current main path calls its idempotent stop twice)
- `logging_drain_start` / `logging_drain_return`
- `main_return`: main's final exit boundary, after the existing conditional
  service-lock release; **not** proof that the process has exited

After four seconds, if the process is still alive, the same daemon writes one
`thread_snapshot`: all currently observed Python thread IDs/names and up to
30 innermost frame coordinates each, with explicit truncation markers. It
prints no locals, argument values, environment, or source lines. A snapshot
containing `main_return` followed by the main thread in an executor/interpreter
join distinguishes that tail from a blocked monitor stop or logging drain.
Duplicate signals do not rearm it. The four-second observation corresponds to
one second before `runtime.stop_service()`'s unchanged five-second deadline.

The observer remains armed after main returns so it can see interpreter joins;
it is a daemon and is never joined by cleanup. It continues accepting late
phase records after the one snapshot. The OS closes the private sink at process
exit. Files are retained as diagnostic evidence alongside other service logs;
this change introduces no retention/deletion policy. Fast exits can abandon
queued final records. Treat a missing record as missing evidence, not as proof
that the corresponding phase never completed. A GIL-holding native call or a
blocked diagnostic filesystem can also prevent a useful snapshot.

## Validation and use

`tests/test_shutdown_diagnostics.py` verifies the late snapshot in a temporary,
self-owned child process whose interpreter joins an executor after normal
logging shutdown. It checks timestamps, a single snapshot, thread names/frame
coordinates, absence of local/source values, and private file permissions on
POSIX. Separate child cases verify that a healthy, blocked or failing sink
never prevents process exit. Thread-start failure is also best effort.

`test_service_main_owns_monitor_shutdown` in
`tests/test_macos_session_diagnostics.py` checks the actual main-path phase
ordering, including after logging has drained and around service-lock release.
This is a distinct wiring contract from the diagnostic sink's lifetime.

For this change the tests run in CI only. Do not restart the owner's desktop or
run load to gather evidence. Read the new file after the next independently
authorized restart. Identify the actual waiter before changing cleanup or
sizing a new deadline; force-killed observations do not supply a completed
shutdown-time distribution.
