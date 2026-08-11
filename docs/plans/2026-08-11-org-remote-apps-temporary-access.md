# Temporary Organization Remote App Access

## Decision

Until the authorization model in [#1313](https://github.com/avibe-bot/avibe/issues/1313)
ships, every authenticated active Organization member may use Apps, Files,
Editor, Terminal, and Show Pages through remote access. This rollout rule is
not projected as a capability and does not add an App ACL.

## Open Surfaces

- Apps launcher, Dock, Window Layer, and the complete built-in App Library
- the explicit `/api/files/*` endpoints used by Files and Editor, plus Files favorites
- Terminal WebSocket connections and subject-scoped session deletion
- authenticated Show Page inventory, content, mutations, Dock pins, events,
  annotations, icons, and HMR

## Retained Boundaries

- Organization management keeps its existing Cloud authorization boundary.
- Config and service control, Harness, Vault, and unknown local-only routes
  remain fail-closed.
- Terminal requests still require an exact allowed Origin, a signed remote
  session, active Organization membership, authorization refresh, and
  subject-scoped session IDs.
- Anonymous `/p/` sharing still serves only public pages.
- `show_page_email` sessions remain confined to their signed page subtree
  (`AUTH-SETUP-401`).
- Persisted Project and Show Page ACL services remain intact; only qualifying
  remote Organization requests receive a request-scoped temporary bypass.

## Removal

Replace the exact temporary HTTP/WebSocket policy and Show Page resource-context
bypass when #1313 defines and implements per-App capabilities and resource ACLs.
The replacement must preserve fail-closed handling for new or unknown endpoints.
