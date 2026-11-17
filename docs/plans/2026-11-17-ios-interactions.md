# iOS instance status and image dismissal

## Change contract

The owner approved the status-dot placement and preview gesture on 2026-11-17.
Implementation starts at master `ad0f863729c0169cfe9bc4c3d5dae9314c1c2a57`.
The PR candidate is rebased onto master `3bba33b3e96acc534726dc2bc859d6d199e6e2fb`,
explicitly fetched on 2026-11-17 before its final local validation.
The approved source is the `design/ios-instance-status-dots-20261117` frame/kit
and its image-dismiss prototype. This change introduces no copy or API fields.

- The App's full page and sheet use the same instance rows. A fixed 8px dot
  and 8px gap precede the name; names wrap within their column. Green means
  `available`, gray means unavailable, amber means this row is opening.
  `waiting` only disables/dims another row while an instance opens; its dot
  keeps the availability color (mapping clarified by the orchestrator).
- Online is exposed to assistive technology with the existing translation.
  Opening/offline dots are decorative beside their existing text. Failures
  remain red with Retry; a failed refresh keeps its existing amber banner.
- The shared Runtime image viewer accepts single-finger vertical dismissal at
  exactly 1x. At 6px, vertical means `abs(dy) > abs(dx) * 1.2`; another direction
  stays yielded until lift. The outer capture listener yields pinches, zoomed
  pans, controls, and starts within the left/right 20px.
- Downward translation is 1:1. Scale reaches 0.85 and backdrop opacity 0.15 at
  20% viewport height. Controls/counter fade over 160ms above 8px. Release at
  20% height or 0.5px/ms dismisses, using the existing trailing 100ms velocity
  helper. Otherwise return takes 290ms with the move easing. Upward travel
  rubber-bands to at most 24px; cancellation never dismisses.
- Reduced motion retains finger tracking but no shrinking, instant return,
  and a 140ms opacity-only exit. Mouse, keyboard, focus return, pinch and
  double-click behavior remain owned by the existing modal/zoom components.
  Horizontal swipes do not page; arrows and keys do.

## Delivery boundaries

Status dots ship in the bundled mobile UI and need a new App build. Image
preview gestures ship in the selected instance's Runtime Web UI and need a
Runtime containing this change. No additional native capability or minimum
Runtime version is introduced. Updating only one carrier cannot deliver both.

Read-only route check on 2026-11-17: `ios-testflight.yml` still requires master;
PR builds do not sign/upload. Its signing team is `68B2SQ6YK3`. The two local
valid development identities belong to other teams; no physical device is
currently available to devicectl. This establishes no usable local installation
route, without changing credentials or testing signing against an Apple account.
At 17:10 on 2026-11-17 (Asia/Shanghai), the owner approved this change's
sequence exception: local validation → one PR through review/CI → separately
authorized merge and Internal TestFlight/Runtime delivery → device acceptance.
The current authorization ends at Merge-ready. Merge, TestFlight dispatch,
Runtime updates and installation still need separate owner authorization.

## Validation

Primary contracts are tested through `InstancesPage` / `InstanceSheetPage` and
`ImageViewerProvider` with the real zoom component. Existing onboarding and
viewer suites cover row actions, gallery scoping, retained surfaces, keyboard
ownership and focus. New tests first fail on the original implementation:
status semantics/accessibility were absent, and touch dismissal was absent.
Browser captures compare the real component composition against approved
light/dark frames at 402×874 and long names at 375×667. Chromium touch input
can establish event ownership and geometry; it is not iPhone feel or VoiceOver
acceptance. Device acceptance remains a separate owner gate.
