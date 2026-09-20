# Instance and Project Authorization

## Background

The hosted control plane now signs an effective `owner | editor | viewer`
instance role and exposes a versioned Project access-intent protocol. The local
runtime currently authenticates remote sessions but does not retain or enforce
that role, and it has no applied Project policy store.

## Goal

Make the local runtime the enforcement point for both layers:

1. the signed instance role is the maximum capability for a remote session;
2. an applied Project policy may only narrow that role;
3. independent Agent, Vault, Skill, and Show Page ACL checks remain additional
   gates and never grant Project visibility.

Trusted local requests remain owner-equivalent. Remote claims fail closed.

## Delivery

### Issue 981: instance role

- Validate the signed role and organization claim shape at OIDC exchange.
- Retain only validated claims in the signed local session cookie.
- Reauthorize instead of sliding an old role at the session refresh boundary.
- Build one request authorization context and capability projection.
- Apply a fail-closed HTTP route policy and equivalent WebSocket checks.
- Let the frontend hide or disable owner/editor actions from server capabilities.

### Issue 982: Project policy

- Add forward-only Project policy and binding tables.
- Evaluate email, domain, and organization-group bindings in one service.
- Publish safe Project metadata, pull newer intents, apply atomically, and ACK
  exact revisions with idempotent retry behavior.
- Filter Project/session lists and re-check direct Project, session, message,
  stream, and conversation actions.
- Keep paths, files, prompts, messages, and execution output off the control
  plane.

## Branching

Both PRs target `dev`. Issue 982 is implemented on top of issue 981 and must not
merge until the instance-role boundary is present in `dev`.
