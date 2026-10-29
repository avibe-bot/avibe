# Remote desktop update settlement

Follow-up to #3058, closing review residuals 4409191520, 4409191530 and
4409191539. The existing authorization, IPC preference, signed native installer,
Web upgrade route, target revalidation and acknowledgement timeout are unchanged.

## Contract

One accepted request owns its receipt and busy gate until settlement. Under the
updater state mutex, `settle` alone writes terminal receipts and calls `finish`.
Outcomes are completed, failed, declined, expired, shell_interrupted and
restart_version_mismatch. Startup reconciliation uses the same settlement path.
A busy rejection preserves the owner's receipt. An expired host prompt is settled
before a replacement claims ownership. Timers, pollers and host actions match both
request ID and target; an old callback cannot release a new owner's gate.

The accepted request retains its deadline in memory. A competing or stale request
file cannot change the current request's identity, deadline, receipt or gate.
The install path still revalidates the accepted request before download/install;
a matching expired file cancels it, while an unrelated file is ignored. The gate
remains held through installation and restart handoff until startup settlement.

Remote Web polling keeps an absolute 35-minute request deadline. A transient
connection outage starts bounded reconnect verification; a matching non-restarting
receipt restores the remaining original request deadline. Restart/version
verification retains its two-minute bound. Native refresh guards use the native
receipt, including Ignore and expiry events.

## Verification

- Rust state-machine tests: poll/timer ordering, competing claims after prompt
  expiry, startup settlement, stale IDs/targets, and installation ownership.
- VersionBadge fake-timer tests: connectivity recovery, bounded restart checking,
  native Ignore/expiry with a stale API receipt.
- Both Rust crates: fmt, clippy and tests. Related Python API/request/status tests.
  Focused UI tests, typecheck, lint and build.
- Exact-head Codex review, no unresolved threads, lint + desktop-shell CI and
  CLEAN merge state. Do not merge or release. Stop and report to ses_main if
  settlement/busy-gate findings recur on a second reviewed head.
