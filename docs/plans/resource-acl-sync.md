# Organization Resource ACL

## Background

Organization membership arrives at the local runtime in a signed OIDC ID token,
while resource content remains local. The runtime therefore needs a local ACL
projection that can be updated only by versioned control-plane intents.

## Goal

Keep resource content local while enforcing organization-managed access for
Agents, Vault secrets, Skills, and Show Pages. The hosted control plane owns
desired ACL policy; the paired local runtime stores and enforces a transactional
projection of that policy.

## Design

- Store one policy per `(resource_kind, resource_id)` and group bindings in a
  separate table with a cascading composite foreign key.
- Keep OIDC organization claims in the signed local remote-access cookie. A
  membership-bearing cookie re-enters OIDC at its refresh boundary rather than
  extending stale group claims indefinitely.
- Publish safe resource metadata and the currently applied ACL revision over
  the paired-instance device channel. Pull a newer intent, apply it in one
  SQLite transaction, then acknowledge that exact revision.
- Do not mutate an organization policy through a local standalone revision.
  The hosted organization resource API remains the writer of desired intents.
- Treat a missing policy as local-private. Trusted local callers and the signed
  instance owner retain access to legacy resources; other remote callers fail
  closed.
- Register resources created by an active organization member as private and
  owned by that member. External instance guests cannot create organization
  resources.

## Authorization model

- `private`: only the resource owner can use the resource.
- `public`: any active member of the resource's organization can use it.
- `scope`: active organization members can use it when at least one signed
  group claim matches a policy group binding.
- Resource owners and organization owners/admins can manage an existing
  resource. Public or scoped use does not imply management access.
- Trusted local requests bypass organization ACLs. Remote identity, instance
  role, organization membership, and group claims must come from the validated
  OIDC exchange and signed local session cookie.

## Enforcement

- Agents: filter listing and lookup, gate use, and require management access
  for update/removal.
- Vault secrets: filter metadata, gate secret use and request creation, and
  require management access for metadata, rotation, pin, and deletion changes.
- Skills: filter backend-specific global/project resources, gate updates and
  removals, and register remotely created Skills.
- Show Pages: filter listing and lookup, gate session access, and require
  management access for visibility, share, icon, and archive mutations. Public
  share URLs remain governed by Show Page visibility rather than organization
  ACL.

Instance-wide endpoint roles and project ownership are separate authorization
layers. They are intentionally not derived from organization resource ACL and
are tracked in avibe-bot/avibe#981 and avibe-bot/avibe#982.

## Verification

- Unit test private, public, scoped, and missing-group policy evaluation.
- Unit test newer-only intent application, exact ACK behavior, and offline
  retention of the prior local policy.
- Unit test list filtering, use checks, management checks, private policy
  registration, and fail-closed request context resolution for every resource
  kind.
- Run the repository's unit, static syntax, and lint commands before commit.
