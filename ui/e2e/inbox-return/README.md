# Inbox Return Regression

Install the browser engines with `npx playwright install --with-deps chromium webkit`,
then run `npm run test:inbox-return` from `ui/`.

This backend-free suite renders the production `InboxPage` and `SearchPage` with
an in-memory Inbox provider and a routed Chat placeholder. Like production, a
data router hosts descendant declarative routes. Its shell mirrors the production
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

Search-entry taps must mount and focus the search input before the click finishes
bubbling, so focus stays inside the user gesture required by mobile keyboards.
Chromium and mobile WebKit checks cover this timing, typing without another tap,
clearing, repeat visits, and query restoration from a direct URL. Browser automation
does not prove that the native iOS soft keyboard appears; that remains a device check.

All API requests are blocked. No Avibe instance, credentials, or persisted
messages are used. Screenshots and failure traces are written under
`e2e/.artifacts/inbox-return/`.

For full-service verification, proxy the worktree's Vite server to the existing
local Incus regression instance with `VIBE_UI_BACKEND`. Use existing conversations
and confirm their IDs and loaded row counts survive a history return. Do not
repurpose a remote instance or restart the user's host service. Native iOS Safari
edge-swipe recognition is a separate device check; Chromium exercises its
history-POP navigation result, not Safari's gesture recognizer.

`motion.spec.ts` loads the fixture with `?motion`, which installs the production
navigation motion (`src/lib/navigationMotion.ts`, docs/plans/web-ui-motion.md M5
and M8). In Chromium and mobile WebKit it checks that opening a conversation on a
phone pushes it and Back pops it, with the Inbox already back at the reader's
position in the snapshot the pop reveals; that reduced motion cross-fades instead;
that a touch during a push finishes it at once; that a desktop navigation runs no
transition; and that a pressed control sinks and steps its colour, keeping only
the colour under reduced motion. Frame rate on an iPhone is a device check.

`swipe-read.spec.ts` checks marking a card read: 「Mark read」 sits at the card's
bottom right on desktop and phone, and on a phone (Chromium's touch emulation) a
swipe either way past 30% of the card marks it read without opening it, a short
swipe springs back, and a touch that goes down still scrolls the list with the
card left in place. The gesture's thresholds, flicks, the left-edge rule and
pointer types are covered by `InboxCard.swipe.test.tsx`; how the swipe feels on
an iPhone is a device check.

`load-more.spec.ts` loads `load-more.html`, which mounts the production
`WorkbenchInboxProvider` and `InboxPage` over a routed `GET /api/inbox` that pages
the way the server does (newest first, keyset cursor, a cursor only when another
row exists). It checks that "Load more" belongs to the current view: the Unread
tab offers it only while an unread conversation is not loaded yet, and one click
reads on until that conversation shows; the All tab loads the next page and drops
the button at the end of the feed; a click while the feed is being refreshed shows
its loading card at once and its page follows. That the server sends no cursor
for a page the feed fills exactly is covered in `tests/test_messages_service.py`.
