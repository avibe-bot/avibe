# Folder picker delivery diagnosis

## Circuit-breaker decision — 2026-09-23

PR #2115 has 12 findings-bearing reviewed heads and no clean review. GitHub's
complete review/thread inventory contains 30 finding threads, 27 resolved and
three open. Reviewed heads are bound through the review's commit, not the
mutable inline-comment diff location.

| Reviewed head | Root causes in its findings |
| --- | --- |
| fe992a7aa6 | Modal keyboard ownership; listing vs operation validity; initial fallback readiness; pending search presentation; truncation |
| 001682fd51 | Failed favorites fallback; overlay containment; search vs creation lifetime |
| 6b3d1934f9 | Configured path compatibility; hidden-file refresh; search preservation |
| d3e26afef2 | Initial resolver lifetime; listing/search refresh consistency |
| 76742047da | StrictMode lifetime |
| 7399333512 | Stale initial completion; pending destination refresh; overlay containment; retained creation draft; project fallback failure |
| 69cc604168 | Upload drop-region ownership; arbitrary path entry; configured symlink compatibility |
| 6803e37ac0 | Stale manual submission |
| b902cfec5b | Retained path editor focus |
| ad888b312d | Stale initial error |
| abf628c72f | Repeated refresh/search race; favorite and fallback symlink compatibility |
| 09e7a3c1ea | Stale creation completion; lost navigation history; stale consuming fixture |

The repeated async-ownership class and the post-extraction head count both
trip the breaker. Further isolated callback patches are paused. Inspection of
the diff and the consuming `home-media/sheet-suspension.spec.ts` shows two causes:

1. Initial resolution, favorite resolution, manual submission and listing use
   independently invalidated identities. Starting a favorite does not invalidate
   an older listing; editing text can invalidate a favorite without releasing
   its busy state. Folder creation has no completion fence.
2. The shared UI extraction did not preserve the full picker interaction
   contract. History and caret retention disappeared, and the consuming browser
   fixture still mocks the removed listing transport. Passing focused unit tests
   did not exercise this contract.

## Scope decision

Keep the shared FileBrowser surface and Files API. No backend, installer,
permission model, global router or unrelated Files operation changes are needed.
Consolidate the picker's navigation into one latest-request owner covering
resolution, listing, errors, busy state and history commit. Reuse the previous
picker's retained-editor selection behavior. Keep creation identity separate
from navigation, but invalidate it on navigation, cancellation or a newer draft.
This is local, reversible and preserves the selection contract.

- Canonicalize external paths (initial, fallback, favorites and manual input);
  list already-canonical rows, breadcrumbs and history directly.
- Only the latest navigation can publish a listing, error or history entry.
  Refresh retains a pending destination, its history intent and the search.
  Successful navigation alone commits history; failed/stale requests do not.
  Search results/errors are scoped to directory, query, hidden setting and
  refresh revision. A local deferred-response test reproduced source-folder
  hits/errors surviving a destination commit; changing the search context must
  immediately withdraw those results while the destination search is pending.
- Unsubmitted text and caret are editor state, not navigation intent. Typing
  another draft does not cancel admitted navigation or let completion close the
  newer draft. Opening an editor does not cancel a pending favorite.
- Escape explicitly cancels an outstanding **manual** navigation, including its
  resolution/listing phases. Another navigation or submission also supersedes
  it. This reconciles review 4072117989 with the older consuming test: the
  Escape/reopen scenario must assert cancellation, while the newer-draft scenario
  must still assert successful navigation plus preserved draft/caret.
  If hidden-file settings changed during an abandoned/failed navigation, reload
  the retained source with those settings. A supplementary read-only review
  reproduced that mismatch; both cancellation and failure are covered.
- Route suspension retains state and requests, but withdraws modal effects;
  real unmount discards completions.
- A mkdir may finish on the server after cancellation/navigation. Its stale
  completion must not navigate, clear a newer editor or publish an error.
- Add optional history controls to the shared surface; do not rebuild a second
  toolbar. Restore the consuming fixture's actual Files list/search/mkdir
  contracts without permitting undeclared requests.

## Verification and delivery

Add adversarial deferred success/failure tests for the shared request owner and
creation, history/failure/refresh tests, and browser checks for retained Unicode
draft/caret and the actual Files transport. Run the home-media typecheck and full
Playwright suite, focused component tests, UI lint and build before pushing.
Do not delete failing race tests to obtain a green result.

The existing Windows smoke failure was an HTTP 500 downloading the released Show
Runtime after installation, outside this UI scope. Its single failed-job rerun
passed; no installer or workflow changes were needed.

Local verification passed: 47 picker tests, all 5,318 UI tests across 348 files,
all 57 home-media browser cases, the fixture typecheck, UI lint and production
build. Desktop/mobile picker screenshots were inspected. A supplementary
read-only review found the two search/source-option boundaries noted above;
after their fixes its six focused probes passed with no further findings.
The orchestrator also inspected the final production diff and consuming tests.
No failing race tests were removed.

Keep the existing durable PR/Actions Watch. Repository automation owns Codex
review pickup; do not manually trigger it. Close-out requires a current-version
review pass, green CI and no open actionable threads. Merge still requires owner
authorization. Targeted thread replies only; no round-summary PR comments.
