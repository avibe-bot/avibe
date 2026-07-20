# Resource ACL Sync Foundation

## Background

Organization membership arrives at the local runtime in a signed OIDC ID token,
while resource content remains local. The runtime therefore needs a local ACL
projection that can be updated only by versioned control-plane intents.

## Goal

Add the SQLite policy state, signed-session organization context, and paired
device sync needed by later resource-specific enforcement work. This change
does not add enforcement to agents, vaults, skills, or Show Pages.

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

## Verification

- Unit test private, public, scoped, and missing-group policy evaluation.
- Unit test newer-only intent application, exact ACK behavior, and offline
  retention of the prior local policy.
- Run the repository's unit, static syntax, and lint commands before commit.
