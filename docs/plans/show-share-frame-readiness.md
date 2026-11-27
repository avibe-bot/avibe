# Show share frame readiness

## Change contract

Opening the share popover before its Show body mounts must not leave PDF/image
actions absent after a matching, visible frame becomes available. The same open
popover consumes the later frame without another user action.

The shared control retains the existing native capability, payload, session,
geometry, visibility and pointer-event gates. A successful pre-open sample
remains valid for that opening: some existing hosts deliberately disable frame
pointer events while sharing. Only an opening still waiting for an eligible frame
subscribes to DOM changes, frame load, scrolling and viewport resize. The
subscription ends when eligibility is found, the popover closes, or the control
unmounts.

No new copy, loading placeholder, export format, native command, permission,
network transport or connection-pool behavior is introduced. Desktop Workbench
remains link-only; Desktop exports remain limited to native Show windows.

## Evidence

- The isolated component regression fails on the baseline and passes with the
  subscription. It opens with no frame, introduces a pointer-blocked frame,
  then makes that frame eligible without closing the popover.
- Existing component coverage verifies native capability boundaries, annotation,
  unrelated frames, export confirmation, cancellation and read-only opening.
  A host-shield regression preserves the successful pre-open sample.
- The production browser case holds the actual lazy Show body module while
  opening the native title-bar entry, then releases it. Chromium and WebKit
  exercise both themes, the resulting actions and confirmation cancellation.
  API responses and shell capability markers are synthetic; browser state,
  clipboard and native IPC are isolated.
- Real signed Desktop package, AppKit share menu, PDF/image bytes and installed
  window acceptance remain untested by this change.

Base: `af091c6316` (including the deferred sharing settings from #4374). First-screen gzip measurements compare this base and the
change using the same dependency lock and build command; the PR records the
delta separately from the existing budget overrun. Budgets are unchanged.

After synchronization, Home JS is 624,609 → 624,615 B (+6 B), and Chat JS
is 922,303 → 922,490 B (+187 B), gzip level 6. Both routes pass the unchanged
first-screen budget. The lazy settings preload and boundary remain intact.
