# Custom Hostname Migration Compatibility Design

## Problem

The custom-hostname heartbeat branch cannot start against the default Avibe state database. The database is stamped at `20260716_0030`, while the branch contains a different migration head, `20260720_0030`, descending directly from `20260707_0029`. Alembic therefore cannot resolve the installed database revision. The main service exits before it can send runtime-status heartbeats, so the Web UI never receives the backend's `active_hostnames` snapshot and rejects `max.fileguard.io`.

## Design

Preserve the existing database and move forward on one migration chain:

1. Add the released `20260716_0030_harness_input_index` migration.
2. Add the released `20260721_0031_silent_marker_inbox_index` migration, descending from `20260716_0030`.
3. Rename the unreleased resource ACL migration to `20260723_0032_resource_access_policies` and make it descend from `20260721_0031`.
4. Set the local schema head to `20260723_0032`.

This lets databases created by current releases upgrade normally while retaining the ACL branch's schema. It does not stamp, downgrade, reset, or directly edit user state.

## Verification

- A hermetic migration test starts at `20260716_0030`, upgrades to head, and verifies both the released `0031` index behavior and ACL tables.
- Existing migration, remote-access heartbeat, and OAuth host tests pass.
- The source install starts the main service with a new PID.
- A successful heartbeat persists `max.fileguard.io` as an active hostname.
- `https://max.fileguard.io/` returns an OIDC redirect instead of `remote_access_host_mismatch`.
- Browser authentication returns to `https://max.fileguard.io/auth/callback` and completes successfully.

## Safety

- Do not reset or downgrade `~/.avibe` state.
- Do not modify pairing, tunnel, or remote-access credentials.
- Do not merge unrelated current-master changes into this feature branch.
- Keep the existing independent UI process and tunnel configuration intact except for the required managed restart.
