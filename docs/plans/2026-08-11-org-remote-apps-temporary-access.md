# Temporary Organization Remote Runtime Access

## Decision

The original Apps-only decision in this document was superseded by
[#1343](https://github.com/avibe-bot/avibe/issues/1343). Until granular runtime
capabilities and resource ACLs ship, every authenticated active Organization
member may use the complete Avibe runtime through remote access. This rollout
rule is not projected as a capability and does not grant durable resource
ownership.

## Open Surfaces

- Apps launcher, Dock, Window Layer, and the complete built-in App Library
- the explicit `/api/files/*` endpoints used by Files and Editor, plus Files favorites
- Terminal WebSocket connections and subject-scoped session deletion
- authenticated Show Page inventory, content, mutations, Dock pins, events,
  annotations, icons, and HMR
- Harness, Agent definitions, Skills, Vault, Settings, service control, Model
  Hub, Project administration, and the other explicit runtime routes listed in
  the temporary active-Organization-member policy

Vault access is intentional for this temporary rollout. Active Organization
members may reach secret, grant, approval, signing, VMK, and audit operations;
[#1343](https://github.com/avibe-bot/avibe/issues/1343) records this accepted
risk and the requirement to replace it with granular authorization.

## Retained Boundaries

- Organization management keeps its existing Cloud authorization boundary.
- Unknown routes and control-plane routes outside the explicit temporary
  runtime matrix remain fail-closed.
- Terminal requests still require an exact allowed Origin, a signed remote
  session, active Organization membership, authorization refresh, and
  subject-scoped session IDs.
- Anonymous `/p/` sharing still serves only public pages.
- `show_page_email` sessions remain confined to their signed page subtree
  (`AUTH-SETUP-401`).
- Persisted Project and Show Page ACL services remain intact; only qualifying
  remote Organization requests receive a request-scoped temporary bypass.

## Removal

Replace the exact temporary HTTP/WebSocket and resource-context bypasses as
[#1343](https://github.com/avibe-bot/avibe/issues/1343) defines and implements
granular capabilities and resource ACLs. The replacement must preserve
fail-closed handling for new or unknown endpoints.
