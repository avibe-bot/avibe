# Show Page Safari system entry

## Contract

A reader who can open a Show can use Safari to add that exact route to the iPhone Home Screen or Mac Dock. Owner `/show/<sid>/` and recipient `/p/<share-id>/` routes retain their existing authorization. Manifest identity, start URL and scope use the route itself, with no credential or fallback to the instance home. Revocation remains enforced on the next online request. This does not add offline access, native privileges, an Avibe pin permission or automatic publication.

The sharing control offers platform guidance and a default-browser handoff/copy action inside the native shell. The runtime document receives a small Shadow DOM entry, suppressed in standalone mode. It remembers collapsed/expanded/dismissed preferences per route. While annotation is active, annotation owns the page and guidance is hidden.

## November 23 repair

The candidate from `83176d6b` incorporates remote master `56854d06f8b03e1b706f48edb1fcabef3211fa0b`; the native PDF/image sharing flow from master is retained. The former expanded fixed panel covered the last authored line despite an appended spacer. Expanded guidance now occupies document flow after authored content. Clicking the compact entry brings that footer into view and focuses its collapse button. Collapse/Escape returns focus to the compact entry; dismissal removes the entry and annotation subscription. Space below the panel keeps the annotation button separately reachable. No author nodes are wrapped or restyled.

Unavailable copy explains that the instance has no remote-access address and directs the reader to the instance owner and Remote Access → Avibe Cloud. `url_available` is derived from configured enabled cloud URL, not a live connectivity probe, so copy does not promise that toggling a switch alone guarantees reachability. Mac instructions include both Safari Share and File menu routes.

## Verification and limits

- Browser regression exercises the exact injected script, document-end clearance, independent annotation controls, dismissal and both standalone signals. The old fixed panel fails the clearance regression on both viewports.
- `ui/scripts/show-system-entry-shot.mjs` produces 16 actual-script screenshots: iPhone/Mac × English/Chinese × light/dark × collapsed/expanded, with geometry and console-error readbacks. Its authored page and annotation button are isolated fixtures, not a hosted Show or native Safari acceptance.
- Sharing-control design frames render the product React component. The runtime design kit is a proposed visual reference; actual-script screenshots are the behavior evidence.
- The limited-recipient handler fixture verifies manifest identity and script access before revocation and denial afterward. Identity verification is mocked; this does not establish two live accounts or installed Safari cookie behavior.
- Full HTTPS acceptance requires a reserved test instance, exact candidate deployment/rollback, separate owner and non-manager recipient identities, real iPhone/Mac install and cold launch, then test-share revocation. No PR before owner acceptance. No deployment, tunnel, account, real Show or device operation is included in local implementation.

Local visual reproduction: from `ui/`, run `node scripts/show-system-entry-shot.mjs`; component frames use `npm run design:shot -- show-pages/system-entry --lang en` (repeat for `zh`). Follow the workspace safe-browser policy; never attach to the owner's browser profile.
