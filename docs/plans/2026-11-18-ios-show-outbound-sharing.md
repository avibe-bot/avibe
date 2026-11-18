# iOS Show outbound sharing

## Goal and scope

Starting at fetched `origin/master` `8279eb7057afeffb6a04000dd3627843e82197cc`, let a reader share the Show currently displayed in the iPhone App as its existing link, a PDF, or an image through `UIActivityViewController`. This implements the outbound portion of accepted C1 v1. It does not change access, publish pages, add an inbound extension, change signing, distribute TestFlight, or merge. I3 and HealthKit branches remain separate.

## Contract

- The trusted instance main frame advertises `c1.share_draft: 1`. `share.capabilities` reports version 1, no inbound kinds, outbound `link/pdf/image`, outbound file limits (50 MiB each, one attachment), and no inbound pending recovery.
- `share.present({operationId, kind, sessionId})` takes a UUIDv4, one supported kind, and a bounded opaque session identifier. It accepts no URL, coordinates, HTML, filesystem path, credentials, or export bytes. The native host reads the current instance's existing read-only Show/access APIs using its WebView; the authoritative page response supplies the original link/title. No permission write occurs.
- Exports require a loaded, visible iframe for that exact session, still at its authenticated same-origin `/show/<session>/` route. The native host captures its intersection with the visible WebView/ancestor clipping rectangles; it never navigates to another resource or scrolls/loads hidden content. The sharing UI must close before capture. PNG and PDF contain only that visible region. Cross-origin/hidden/missing/changed views fail closed.
- Native authorization remains the existing trusted main-frame/origin policy. The host rechecks the source document and frame around asynchronous capture, and cancels when its WebView navigates or unregisters on account/instance teardown.
- A single active presentation owns its operation. Duplicate requests attach to that operation or replay its terminal result; conflicting reuse fails. Results are `completed/cancelled/failed/expired`. `completed` means only the system callback's successful handoff. Completion/cancellation have no extra success toast. Errors remain retryable with a new explicit user operation.
- PDF/image confirmation explains visible-content limits and independently retained copies. Reviewed English/Chinese copy is in the UI catalogs.
- Files live in a controlled temporary directory, under opaque operation directories. A successful callback retains them for five minutes; cancellation/failure removes them immediately. A fifteen-minute deadline from start forcibly cleans and expires an operation without a callback. Launch removes orphan directories older than 24 hours. No exported path/content/name is logged or persisted in drafts.
- Old shells retain the existing Web Share link path; their export choices explain the upgrade requirement. Browser sharing behavior remains available.

## Validation

Focused Web tests cover capability negotiation, user confirmation, cancellation/errors, and unchanged permissions. XCTest covers request validation, current-view/access checks, real WebKit capture, operation results/idempotency/timeout and temporary-file lifecycle. Simulator foreground captures cover English/Chinese and light/dark after the coordinator allocates a device window. Build the Workbench/mobile UI and unsigned iOS App in independent DerivedData. Open an English non-draft PR and use exact-head Codex review, CI and zero unresolved threads to reach Merge-ready; device and TestFlight checks remain explicit residual work.

## Work

- [x] Implement native capability, export/presentation owner and lifecycle cleanup.
- [x] Add reviewed sharing controls and locale entries, preserving read-only access behavior.
- [x] Add focused automated coverage and design frames.
- [x] Verify unsigned build, foreground simulator and four locale/theme captures.
- [x] Record read-only inbound Share Extension feasibility separately.
- [ ] Deliver Merge-ready PR; do not merge or release.
