# Show Page Safari system entry

## Contract

Avibe does not offer an “Add to Home Screen” or “Add to Dock” control for Show Pages. Neither an owner page (`/show/<sid>/`) nor a shared page (`/p/<share-id>/`) injects a button, prompt script, guide, or spacer into the authored document. The Workbench sharing panel has no installation section either. Its ordinary open, copy, share, and export actions and the separately permissioned Avibe Dock pin are unchanged.

Safari's own menus belong to the browser. The Show document still supplies a manifest link, touch icon, and web-app metadata. Manifest identity, start URL, and scope identify the exact Show route without embedding a credential or falling back to the instance home. The manifest link uses `crossorigin="use-credentials"` for existing same-origin cookies. Metadata does not grant access: the owner and recipient routes keep their admission checks, and revocation applies to the next online request. There is no offline-access feature.

## History

- #4217 introduced the runtime prompt and sharing-panel installation guide alongside the per-route metadata.
- #4250 removed the Mac runtime prompt and the mobile session-actions menu while viewing a Show. Annotation, back-to-chat, and ordinary sharing remained available.
- The remaining runtime prompt and all sharing-panel installation guidance were subsequently removed. No stored page or existing shortcut needs to be rewritten to remove these controls.

## Verification and limits

- HTTP handler fixtures cover owner, public, and limited documents and limited-recipient admission and revocation for metadata requests.
- The isolated browser tests exercise document mode, authored-node preservation, absence of an injected install script, and manifest loading with synthetic owner and recipient identities.
- The sharing-control tests open the actual popover in browser and native-shell modes and check that the ordinary link actions remain without an installation guide.
- These fixtures do not establish real Safari install/cold-launch behavior or a two-account HTTPS revocation flow. Those require separate real-device acceptance.
