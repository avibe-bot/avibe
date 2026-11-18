# Computer Use dialog diagnostics and Dock guidance (#3931, first delivery)

## Change contract

Baseline: origin/master `d5547843a0f156420461e67d19ca61320b6d8fcb`, fetched
explicitly on November 18, 2026. Driver stays at 0.31.0, upstream commit
`5272e492d61b96caf08e3bf434d91126c1f3dccc`; the existing Avibe raw-click,
reopen and parent-liveness patch is retained.

When Shortcuts shows an introduction window which is visible but absent from
its normal AXWindows list, the agent must learn whether the exact window can
be observed. It must not send Return to the library window merely because
both windows share a PID. This delivery preserves the input refusal and
provides an actionable manual step.

- C1 records typed AXWindows diagnostics and follows bounded explicit
  AXChildren, AXSheets, focused-window and parent relationships for the
  requested PID/native window ID. Sheet own/root identities remain distinct.
  Supplemental recovery produces observation-only content: no action tokens
  or capture binding, and the previous target snapshot is invalidated.
- Normal action-window enumeration and input admission remain unchanged.
  A current-Space unresolved target reports `app_dialog_unresolved` with
  approved manual guidance. Unknown/off-Space cases keep their refusal.
- D rejects only exact Dock bundle selectors before app lookup and supplies
  `dock_unsupported` manual-removal guidance.
- Shared transport preserves both flat and nested structured refusals plus
  raw reason/diagnostics, and uses the backend i18n catalog for actionable
  text. It adds no automatic action replay or route escalation.
- A/B menu capabilities and C2 limited modal input are excluded. This branch
  includes no menu state, menu actuator, pointer-origin, selection-lifetime or
  menu fixture changes.

## Validation and limits

Pure tests exercise bounded resolver graphs, own/root scope, typed diagnostics,
non-publication of recovered action tokens, transport and Dock lookup. Tests
use owned data and fake transports. Compile checks never launch a GUI or
contact a production driver. The original action window enumerators are
compared byte-for-byte against the existing patch baseline.

Packaging must refresh the combined patch identity and Mac metadata snapshot,
retain previously approved snapshots in Runtime, and apply 0001 then the
unchanged Windows 0002 on a fresh archive. Canonical SDK inventory inspection
runs without an action runtime. The local arm64 helper is ad-hoc and cannot
substitute for a Developer ID signed/notarized Desktop package. Any local
build overrides must be recorded.

The real Shortcuts introduction's recoverability remains unverified. C1 is
successful when an exact surface is safely observable or the agent gives the
approved manual step; it does not promise automatic dismissal. Native Windows
build and security checks remain assigned to the existing PR CI job.

## Delivery sequence

1. Peel C1/D into a clean worktree and compile/test the isolated scope.
2. Have the original reviewer inspect the exact separated candidate, including
   the observation-only publication and unchanged input consumers.
3. Prepare PR material and candidate package evidence for main; do not open a
   PR, merge, install or operate a desktop without the coordinated next step.
4. Main coordinates either administrator approval of an exact signing branch,
   or Max's agreement to PR/CI then explicitly authorized merge and a normal
   signed TEST RC. Agent smoke and Max acceptance use that installed candidate.

The earlier combined menu/dialog candidate is a retained diagnostic checkpoint,
not an acceptance package. Its local review led to this scope split; A/B now
requires a reviewed design before further implementation.
