# Remote Session Revocation

## Background

Remote sessions currently retain an instance role and Organization membership
claims until their scheduled OIDC refresh. A hosted access downgrade can
therefore leave an already signed editor session active on every hostname that
serves the paired Instance.

## Goal

Invalidate remote authorization promptly after any hosted change that can alter
the effective Instance decision, while preserving owner-equivalent trusted
local access.

## Revision Contract

- The control plane owns a monotonic, non-negative revision for the effective
  authorization state of each Instance.
- Every OIDC decision includes that value as
  `vibe_instance_authorization_revision`.
- A paired device reads the current value from
  `GET /api/v1/instances/{instance_id}/authorization-revision`, authenticated by
  `X-Vibe-Device-Secret`. The response is
  `{ "authorization_revision": <integer> }`.
- The control plane advances the revision after member removal, granting-group
  removal or archival, role downgrade, and Instance access-binding removal.
- The runtime persists only monotonically newer device snapshots. A remote
  session is current only when its signed revision exactly matches a fresh
  device snapshot. Missing, malformed, stale, or expired revision state fails
  closed for paired remote access.

The revision is Instance-wide. Advancing it can require unaffected remote users
to reauthorize, but it guarantees that the default and custom hostnames share
one decision and avoids retaining hosted membership data in the local runtime.

## Enforcement

- HTTP and newly opened pages validate the revision while parsing the session.
- Session renewal preserves the OIDC revision and cannot stamp stale claims
  with a newer device revision.
- Workbench and Show Page SSE streams re-check the revision while connected.
- Private Show Page and terminal WebSockets race their normal handler against
  revision loss, so resumed connections close and reconnect through current
  authorization.
- Trusted local requests never consult the hosted revision snapshot.

## Acceptance Evidence

- `I1057-AC1`: editor downgrade removes mutation capability.
- `I1057-AC2`: member and granting-group removal invalidate active sessions.
- `I1057-AC3`: sessions on default and custom hostnames share one watermark.
- `I1057-AC4`: HTTP, SSE, and WebSocket paths re-check the same state.
- `I1057-AC5`: trusted local access remains owner-equivalent offline.
- `I1057-AC6`: stale revisions, reconnects, and all revocation causes have
  automated coverage.
- `I1057-AC7`: staging E2E remains a deployment-level manual check after the
  matching control-plane revision contract is available.

## Todo

- [x] Persist and poll the paired-device revision.
- [x] Bind remote sessions to the signed OIDC revision.
- [x] Re-check revision state on HTTP, SSE, and WebSocket paths.
- [x] Add focused unit and contract tests.
