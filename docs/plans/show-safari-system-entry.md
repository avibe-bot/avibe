# Show Page Safari system entry

## Contract

A reader who can open a Show can use Safari to add that exact route to the iPhone Home Screen or Mac Dock. Owner `/show/<sid>/` and recipient `/p/<share-id>/` routes retain their existing authorization. Manifest identity, start URL and scope use the route itself, with no credential or fallback to the instance home. Revocation remains enforced on the next online request. This does not add offline access, native privileges, an Avibe pin permission or automatic publication.

The sharing control offers platform guidance and a default-browser handoff/copy action inside the native shell. Show documents carry only native install metadata (manifest link, touch icon and web-app meta tags); Avibe injects no install prompt, panel, spacer or script into authored pages, so Safari's own Share and File menus are the install path on every platform.

## November 23 repair

The candidate from `83176d6b` incorporates remote master `56854d06f8b03e1b706f48edb1fcabef3211fa0b`; the native PDF/image sharing flow from master is retained. The runtime panel described here was removed on November 27. The former expanded fixed panel covered the last authored line despite an appended spacer. Expanded guidance now occupies document flow after authored content. Clicking the compact entry brings that footer into view and focuses its collapse button. Collapse/Escape returns focus to the compact entry; dismissal removes the entry and annotation subscription. Space below the panel keeps the annotation button separately reachable. No author nodes are wrapped or restyled.

Unavailable copy states that this control has no share link to open in Safari and directs the reader to the instance owner and Remote Access → Avibe Cloud. It makes no claim about custom domains or other access methods. `url_available` is derived from configured enabled cloud URL, not a live connectivity probe, so copy does not promise that toggling a switch alone guarantees reachability. Mac instructions include both Safari Share and File menu routes.

## Verification and limits

- Browser regression invokes Chromium’s actual manifest loader with the product link markup and synthetic owner/recipient identities, checks denied metadata after revocation, and verifies that metadata injection preserves document mode and authored nodes and adds no script.
- Sharing-control design frames render the product React component.
- Handler fixtures verify that `/show/<sid>/` and public and limited `/p/<share-id>/` documents carry the manifest and touch-icon metadata without an injected prompt, and that the limited recipient's manifest and icon follow admission and revocation. Identity verification is mocked; this does not establish two live accounts or installed Safari cookie behavior.
- Full HTTPS acceptance requires a reserved test instance, exact candidate deployment/rollback, separate owner and non-manager recipient identities, real iPhone/Mac install and cold launch, then test-share revocation. On November 23, the owner authorized PR and merge with these residual real-device checks explicitly disclosed; merge does not complete that acceptance. No deployment, tunnel, account, real Show or device operation is included in local implementation.

Local visual reproduction: component frames use `npm run design:shot -- show-pages/system-entry --lang en` (repeat for `zh`). Follow the workspace safe-browser policy; never attach to the owner's browser profile.

## Delivery integration

The reviewed local copy candidate `040ed5d3141915d698526c0e0414cee7af08cc01` was integrated with remote master `56d35ffbc20760d9ac012a215f09f9f0eef254d8` before PR. Native sharing/export and newer unrelated master changes are retained. The isolated script, component and handler checks are rerun on this integrated tree. Real Mac Dock cold launch, iPhone Home Screen launch, a separate restricted recipient and revocation from the saved icon remain an incomplete end-to-end acceptance matrix; no HTTPS test deployment or real-account/device operation is part of this delivery.

## Review repairs

Generated manifest links are relative to the current Show route and use `crossorigin="use-credentials"` so the browser manifest loader sends its existing same-origin identity cookie. This does not change CORS, permissions or grant checks and introduces no credential URL. Workbench keeps its explicit Show handoff. The isolated browser loader test uses a synthetic cookie gate, while the Python handler fixture checks real admission/lease/revocation code. Neither is real Safari or two-account hosted acceptance.

## First-screen budget

The guide is already outside the home screen's static dependency graph. Making
only that component lazy did not reduce the home download and was discarded.
The approved fix moves the two Inbox-only card skeletons, unchanged, from the
eager shared skeleton module into the existing Inbox route. Shared row skeletons
and their sizing helpers retain one implementation; no new dynamic import,
language namespace or loading state is introduced.

The final authorized production build measured 614,230 bytes
for home (170 below the 600 KiB budget) and 915,468 bytes for chat (6,132 below
the 900 KiB budget), using the existing gzip level 6 measurement. The Inbox card
markup is in the Inbox route chunk, absent from the entry. A production browser
consumer loads that route, goes offline, and first shows the pending-read cards
without another script request. Another consumer checks the home sidebar's
first-read skeleton. Matching-state captures of original source and built
candidate were pixel-identical for both regions. Real Safari acceptance remains
separate from these synthetic browser checks.

## November 24 controls cleanup

Mobile session headers omit the compact session menu only while displaying a Show. Returning to chat restores the existing authorized menu. Narrow desktop windows keep their compact menu; wide desktop headers are unchanged. Annotation, back-to-chat and sharing retain their controls.
`show-controls-cleanup.spec.ts` exercises the real ChatPage with isolated API responses on iPhone, Android, a phone-native bridge, narrow desktop and wide desktop profiles. These checks do not constitute device installation acceptance.

## November 27 runtime prompt removal

The floating Add to Home Screen entry injected into Show documents is removed on every platform (the November 24 cleanup had already removed it on Mac). Its script, route, configuration payload, copy and browser/design fixtures are gone; a stale request for the old script path is an ordinary reserved `__show/` path. Manifest, icon and web-app metadata, Safari's own install menus and the sharing control's guidance are unchanged.
