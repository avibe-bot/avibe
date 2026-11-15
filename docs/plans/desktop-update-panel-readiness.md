# Desktop update panel readiness delivery

## Contract

After startup-only #3797, remember one explicit native update-menu or
`avibe://updates` request until the local Workbench can open the existing panel.
Actual readiness means its listener is installed and native update-state probing
has succeeded, not merely that a page finished loading. Delivery never changes
installation authority. Only the originating user action may restore/focus the
window; readiness and acknowledgement callbacks must not do so.

No timed retries, new settings, or startup presentation are added. Existing
remote/account presentation continues through the guarded native route; this
change does not expand its receiver or bypass unsaved-work approval.

## Ownership

- The shell owns one pending request, coalescing repeated requests while pending.
- The Workbench sends a narrow ready notification after installing the existing
  cancelable update-event listener and confirming updater availability.
- Native sends that fixed event and clears its mailbox only when its evaluation
  callback reports actual synchronous handling. Submission or page completion is
  not consumption; unhandled events and evaluation failure retain the request.
- A request id binds acknowledgement to its pending request. The event script
  remembers the last handled id in the current document so overlapping readiness
  notifications cannot produce duplicate panel checks before the first callback
  returns. This is still one mailbox, not a retry queue or readiness state machine.
- A ready signal with no pending request does nothing. It cannot create an update
  action, request installation, route a remote page, or focus a window.

## Compatibility and limits

A new Workbench ignores an unsupported readiness command on an older shell and
keeps its existing event handling. A ready older Workbench that understands the
legacy event can acknowledge it as before. Reliable delayed delivery requires a
Workbench that sends the new readiness notification; an incompatible adopted
page cannot become compatible by polling. Its existing explicit fallback remains
unchanged. No timing guarantee is made for such older receivers.

An event accepted by the live listener is delivery; this is not a promise to
reopen the panel after the user deliberately navigates away or after a process
crash. It must not refocus after the original request if the user switches apps.

## Validation plan

- Rust owner regression: unhandled completion retains intent; later handled
  completion consumes it; repeated pending requests coalesce; stale completion
  cannot consume a newer request; no request means no delivery.
- Execute the actual native event script against the real VersionBadge listener
  in the existing simulated-DOM tests. Hold the updater probe unresolved, then
  resolve it and observe ready notification, panel opening, acknowledgement, and
  duplicate-notification suppression. No fixture supplies the expected panel
  state. Old behavior loses the request before this listener is ready.
- Test unknown-command compatibility and readiness rejection outside the local
  main Workbench; keep guarded remote/account routing unchanged.
- Keep the native acceptance fixture's real show/focus lifecycle validation in
  sync with the explicit-action owner. Source adaptation is not signed evidence.
- Run focused Rust and UI tests, native fixture-adaptation tests, UI build/lint,
  and exact-head Codex/CI. Signed product observations remain a separate release
  gate; no owner's desktop automation is part of this implementation.
