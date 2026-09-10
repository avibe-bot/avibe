# Desktop notification consumer implementation

## Change contract

Implement `desktop-notifications-sse.md` on the shell side without changing
Python, HTTP routes, loopback authentication, WebView IPC, or capabilities.
The orchestrator approved OS-default activation for v1 on 2026-09-11;
cross-platform explicit activation requires a separate native adapter.
The orchestrator also approved the existing locked chrono 0.4 dependency
for durable timestamp parsing (std only, no clock).

## Data flow and ownership

After readiness succeeds, the shell owns one cancellable SSE task for the
validated Runtime origin. Transport loss retries only that task with capped
backoff. Three readiness failures, Stop, Uninstall, and Quit cancel it.
Window visibility and the stored notification preference only suppress
delivery; they neither disconnect the stream nor queue events for later.
The shell retains bounded filter state across stream reconnections.

The Tauri-free runtime-host parses production SSE framing, allows only the
two frozen event classes, deduplicates each class at 512 keys/24h, and
computes duration from durable timestamps. Missing stamps trigger one
bounded detail GET; missing/invalid stamps then fail closed. Scheduled
and watch runs are the only duration-independent kinds. Generic localized
copy carries no untrusted Runtime text.

## Verification plan

- Fake SSE collaborators under a virtual clock prove allow-list filtering,
  suppression without replay, duration independent of arrival/attachment,
  refetch-once/fail-closed, bounded approval/run retention, and reconnect.
- A loopback fake HTTP server proves framing, bounded reads, redirect refusal,
  exact routes, and no replay cursor on reconnection.
- Shell tests prove lifecycle ownership, preference persistence against
  test-owned paths, and notification intentions without calling the OS.
- Boundary tests keep all notification privileges away from WebViews.
- Both native catalogs use the frozen keys and matching placeholders.
- Run desktop npm install/build/i18n, UI build, and Rust fmt/clippy/tests
  across the workspace/all features before PR handoff.

## Integration acceptance

Real OS permission, delivery, OS-default activation, hidden-to-tray behavior,
and menu interaction remain packaged macOS/Windows acceptance. The producer
PR #1982 adds terminal timestamp fields independently and must merge first.

## Local evidence

Desktop npm install/build/i18n, UI npm install/build, workspace all-features
Rust tests, and all-target/all-feature clippy with warnings denied pass.
The notification suites exercise the production HTTP adapter against an
isolated loopback server and drive filter/reconnect behavior with a virtual
clock. No test submits an OS notification or starts the user's Runtime.
Packaged OS acceptance is deliberately not claimed by these checks.
