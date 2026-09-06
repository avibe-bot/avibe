# Inbox Return Regression

Run `npm run test:inbox-return` from `ui/`.

This backend-free suite renders the production `InboxPage` with an in-memory
Inbox provider and a routed Chat placeholder. Its shell mirrors the production
mobile internal scroll owner and desktop document scrolling. Browser history
and the Chat Back button both return to the original Inbox entry.

The invariant is that returning from a conversation preserves the reader's
neighborhood within the already loaded Inbox window. A surviving visible row
keeps its offset; disappearing unread rows fall back to their nearest remaining
neighbors, subject to the new scroll limits. New activity and delayed layout or
read updates must not reclaim scrolling after the reader resumes input.

The suite covers repeated returns, bottom-up unread triage, removed anchors,
additional loaded pages, new activity, delayed mark-read updates, content reflow,
fresh navigation, and a later Search return without replaying a consumed Chat
snapshot. Component tests additionally cover empty intermediate
renders, all visible rows disappearing, every supported input cancellation,
expiration, and Strict Mode lifecycle replay. Consumption and cancellation both
prevent the shared snapshot from leaking into a later Inbox remount, while the
current visit retains its local copy for delayed layout corrections.

All API requests are blocked. No Avibe instance, credentials, or persisted
messages are used. Screenshots and failure traces are written under
`e2e/.artifacts/inbox-return/`.

For full-service verification, proxy the worktree's Vite server to the existing
local Incus regression instance with `VIBE_UI_BACKEND`. Use existing conversations
and confirm their IDs and loaded row counts survive a history return. Do not
repurpose a remote instance or restart the user's host service. Native iOS Safari
edge-swipe recognition is a separate device check; Chromium exercises its
history-POP navigation result, not Safari's gesture recognizer.
