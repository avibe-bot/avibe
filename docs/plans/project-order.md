# Stable Project Order

## Contract

Project navigation retains its order independently of activity, names, or
session state. Without a saved order, projects follow creation time ascending
with their stable id as the tie-breaker. Newly created projects append. Saved
positions survive archiving and restoration. Creation appends to the same saved
order in its write transaction, independently of timestamp precision or id values.

The existing project service owns the order in `state_meta` under
`workbench.project_order.v1`. `PUT /api/projects/order` accepts `order` and
`expected_order`, both arrays of visible active project ids. The new order is
a permutation of the baseline. A changed baseline returns 409. Instance
members and owners may reorder; hidden and archived projects keep their slots.
Responses contain the normal authorized project list. `projects.changed`
broadcasts only an invalidation, never private project ids.

Desktop and mobile share one sortable-list component. The existing project
header activates dragging; it has no additional handle or toolbar. Touch uses
a deliberate hold, allowing an ordinary swipe to scroll. Click/tap still
expands. The dragged project lifts, neighbors animate into their new slots,
and release animates into the optimistically updated order. Escape
and touch cancellation abandon the move. Keyboard access uses the same header.
Reduced-motion preferences disable decorative transitions.

## Verification

- [x] Storage: stable default, saved order, stale-write rejection, visibility.
- [x] Shared provider: optimistic save, failure recovery, incoming refreshes.
- [x] Desktop and mobile: click, scroll, drag, cancellation, animation.
- [x] Focused tests and production UI build.
- [x] Final lint gate; no new violations or suppressions.
- [ ] Installed-environment regression: local Incus daemon unavailable.

Run `npm run test:project-order` from `ui/` for the hermetic browser suite.
It mounts the real sidebar, mobile page, API client, and project provider with
fixture-only transport. Desktop and mobile-emulated Chromium cover persistence,
cross-page invalidation, expanded projects, keyboard cancellation, touch scroll,
touch cancellation, and reduced motion. Storage tests exercise the real HTTP
route and SQLite persistence independently of the browser fixture.

Implementation uses dnd-kit for touch activation, collision detection,
auto-scrolling, keyboard movement, and sortable transforms. It is recorded in
the UI manifest and lockfile; existing layout and visual tokens are reused.

## Review Inventory

Head `5c7ef040a3`, review `5128930144`: three findings, one reviewed head.
Root-cause classes are the second SSE visibility gate, timestamp-based insertion
ordering, and untranslated API fallback messages. No repeated-class threshold
has been reached. The scoped fixes use the existing global-invalidation path
with an empty-payload policy, the existing order record during creation, and
the backend translation catalogs. Regression tests cover the real SSE generator
for every non-owner instance role, creation under a fixed/backwards clock with
adversarial ids, and every error branch in both supported languages.
