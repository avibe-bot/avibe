# ShowPage Email Access

## Goal

Allow a ShowPage owner to grant exact email addresses access to one ShowPage.
Organization ShowPages retain the same email audience and additionally support
the whole Organization or selected Organization groups.

Anonymous Public links remain an independent sharing switch.

## Authorization Contract

- A grant is keyed by `instance_id + show_page_id + normalized_email`.
- The OIDC request carries the target `show_page_id`.
- A user admitted only by that grant receives `vibe_instance_access_source =
  show_page_email`, `vibe_instance_role = viewer`, and a signed
  `vibe_show_page_id` claim.
- Existing Instance owner, email, domain, group, and public-instance access wins
  before the ShowPage-only grant so existing users keep their broader access.
- When a broader user also has the target ShowPage email grant, the broader
  source and role remain unchanged while the signed `vibe_show_page_id` is
  retained as an independent exact-page entitlement.
- The exact-page entitlement bypass applies only to ShowPage routes and
  ShowPage-specific APIs. Generic Session APIs still require their normal
  Project/session ACL so a page grant cannot expose the owner's transcript.
- A broader browser session that predates a page grant reauthorizes once when
  the requested ShowPage is otherwise forbidden, allowing the control plane to
  refresh the signed exact-page entitlement without requiring logout.
- A `show_page_email` browser session may serve only the exact
  `/show/<vibe_show_page_id>` route subtree and required public static assets.
  Workbench, Chat, other Sessions, Agents, files, Cloud capabilities, APIs, and
  other ShowPages fail closed.
- ShowPage resource authorization independently requires the requested resource
  ID to equal the signed `vibe_show_page_id`.
- Replacing an email audience bumps the paired Instance authorization revision
  when and only when the normalized set changes, invalidating existing sessions
  after the normal revision refresh.

## Management Contract

- The local Avibe service exposes owner-authorized ShowPage email GET/PUT APIs.
- Those handlers call a paired-device-authenticated control-plane endpoint; the
  device secret never reaches the browser.
- Personal and Organization instances use the same email-grant endpoint.
- Organization audience/group management continues through the existing
  Organization management flow and remains orthogonal to email grants.

## Verification

- Backend store, route, authorization, authorize-request, frozen-code, and ID
  token tests.
- App OIDC claim, cookie, exact-route, exact-resource, API, and UI tests.
- Regression coverage proving one email grant cannot open another ShowPage or
  any Workbench/API surface.
