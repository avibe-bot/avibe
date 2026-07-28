# Agent Organization Onboarding

## Background

Organization Resource ACL publishes only resources that already have a local
policy. Agents created before an Instance became Organization-managed, including
built-in backend Agents, therefore remain absent from the hosted Resource Access
inventory. Missing policies fail closed for Organization members, but owners have
no explicit way to register those Agents for later group publication.

## Goal

Give the signed Instance owner an explicit, safe, and repeatable onboarding path:

- inventory every custom and built-in/system Agent from `core/vibe_agents.py`;
- register every missing Agent as Organization-private;
- preserve every existing policy and revision unchanged;
- publish through the shared metadata-only Resource ACL sync path; and
- direct owners to the hosted Resource Access console to grant selected Agents
  to Organization groups.

## Design

`VibeAgentStore` owns the inventory and registration transaction because the
Agent table is the source of truth. The service requires owner-equivalent Agent
management access and an active signed Organization context before writing.
Registration uses `ensure_resource_policy()`, whose insert-only contract prevents
late onboarding from replacing an already-applied control-plane revision.

The local API returns only Agent identity and ACL state: id, safe name, backend,
source, enabled state, access level, group ids, and revisions. It never returns a
prompt, Agent metadata/configuration, credentials, source paths, or execution
output. Publication delegates to the shared Resource ACL descriptor builder so
the display-name and metadata-revision behavior owned by issue #1054 remains a
single contract.

The Agents page renders the inventory only when onboarding is available. One
owner action registers all missing Agents privately. Group selection stays in
the existing hosted Resource Access console, which owns desired ACL revisions.

## Verification

- Upgrade path: legacy custom and built-in Agents become private, then an editor
  sees only the Agents selected by a newer control-plane ACL intent.
- Fresh install: built-in Agents and a newly created private Organization Agent
  produce the intended editor-visible set after publication.
- Idempotency: rerunning onboarding does not change existing access, groups, or
  policy/control-plane revisions.
- Redaction: published descriptors contain only the shared safe metadata fields.
- Lifecycle: create-then-delete rename and deletion converge through the next
  full resource-index snapshot.

