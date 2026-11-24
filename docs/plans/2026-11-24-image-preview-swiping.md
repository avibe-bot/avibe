# Touch paging in the shared image preview

## Change contract

On 2026-11-24 the owner requested horizontal paging in the iPhone App's multi-image
preview. This supersedes the horizontal-swipe exclusion in the November 17 plan.
The implementation starts from explicitly fetched master
`9e72198ac033c005bee29fbbc540048ab96df888` and changes the shared Runtime viewer,
not the native App. Chat, draft and queued previews retain their existing gallery
ownership; an isolated image stays isolated.

- One finger at exactly 1x can page. Controls, pinches, zoomed pans and starts in
  the existing left/right 20px edge zones remain owned by their current handlers.
- At 6px movement, a 1.2:1 dominant horizontal axis owns paging; the dominant
  vertical axis keeps the existing dismissal. Ambiguous starts yield until lift.
- Current and eagerly loaded adjacent images track horizontally at 1:1, one
  viewport apart. Release commits at 25% viewport width (capped at 120px), or
  a trailing-100ms velocity of at least 0.5px/ms in the drag direction.
- Swipe paging stops at the first/last image. Outward drag uses exponential
  resistance capped at 64px. Short/paused drags return in 290ms. Commit settles
  in 140–290ms using the existing move easing, then updates the displayed URL.
  Reduced motion tracks the finger but commits/returns without autonomous motion.
- Counter, download and arrow state read the same displayed URL and gallery.
  Existing arrow/keyboard wraparound, mouse behavior, pinch zoom, vertical
  dismissal, focus and modal ownership are preserved. No style or copy changes.

## Validation and delivery

Component tests exercise thresholds, velocity, direction ownership, cancellation,
first/last boundaries, single images, real zoom, reduced motion and caller-owned
galleries. Chromium CDP touch checks exercise rendered geometry and actions in
both the built Runtime UI and its byte-identical packaged copy. Shipped design
frames retain initial, last-image, single-image and reduced-motion compositions.

The local Desktop acceptance package uses the dedicated Avibe Local Acceptance
identity and embeds the Runtime UI from the same commit. Package verification
checks source identity, resources, signatures and archive readback. Installation
requires separate owner authorization. iPhone WKWebView feel is accepted by the
owner after installation; Chromium is supporting evidence, not device acceptance.
No push or PR occurs before that acceptance. No native iOS build is required.
