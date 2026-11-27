# Dependency inspection recovery

Base: a66e8070cd. This change is independent of the event transport fix.

## Problem and evidence

On November 27, 2026, the signed 3.2.9rc2 desktop package queued five read-only
inspection requests behind long-lived HTTP/1.1 event streams. WebKit cancelled
all five at the 15-second deadline; the page retained failed rows after the
runtime was healthy. Evidence is recorded in the owner's acceptance findings
(`2026-11-27-desktop-full-acceptance/findings/diagnosis.md`).

An isolated browser probe also demonstrates that WebKit 27.2 rejects fetch and
response-body cancellation with a new AbortError, while Chromium 159 preserves
the supplied TimeoutError. Both preserve the original reason on the signal.

## Contract

- Each explicit inspection permits one delayed read retry (1.5 seconds) for
  failed groups. An automatic retry cannot schedule another failure retry.
- Returning to the page or recovering the network/event connection re-reads
  only failed, idle groups once. These signals neither cancel an ongoing read
  nor start a repeating failure timer. A later genuine recovery edge may retry
  again, including after the one delayed retry was exhausted.
- Successful inspection and its existing startup reconciliation semantics stay
  intact. Inspection errors retain prior evidence and existing localized copy.
- A deadline is recognized from the signal that actually won cancellation plus
  an abort-shaped exception; caller cancellation and unrelated late errors are
  not deadlines. Preserve the original thrown error object.
- Superseded requests and unmounted/replaced readers cannot publish state or
  schedule retries. No installation, repair, or other write is triggered.

## Validation

Focused hook/deadline tests cover finite retries, recovery edges, request
ownership, cancellation and error taxonomy. Production-build browser tests run
in isolated Chromium and WebKit with fixture reads; no installed app is touched.
Signed Desktop package acceptance remains a separate integration step.

Results: 39 focused unit tests pass (the unchanged base fails six new assertions);
production build, test types, and browser-suite types pass. All six production
page scenarios pass in Chromium 159 and WebKit 27.2. Run the browser scenarios
after `npm run build` with `npx playwright test --config
playwright.dependencies.config.ts`. `PLAYWRIGHT_WEBKIT_EXECUTABLE_PATH` may select
an existing compatible WebKit installation; the default uses Playwright's pinned
browser. The fixtures only answer reads and reject production/external traffic.
