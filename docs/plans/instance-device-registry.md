# Instance device registry

Each authenticated session belongs to one instance binding and subject. Its signed session ID remains the unit of authorization and revocation. A reported installation ID or push device ID groups sessions for display **within** that binding and subject; neither the display name nor the reported kind selects a sign-out target. The host is a synthetic row and has no session.

| Actor / action | Boundary and failure to prevent |
| --- | --- |
| Remote viewer lists devices | Only their subject, unless the current authorization says instance owner. Never infer ownership from a label, login-time role, or email. |
| Direct loopback lists devices | Host first, owner-at-login rows grouped as mine, other accounts separate. No inferred local account. Setup LAN does not inherit device administration. |
| Member signs out a device | Match the opaque row ID in the caller's binding and subject before revoking any sessions; a guessed ID cannot sign the owner out. |
| Owner signs out a device | Match only the current binding. Revoke all active sessions of that logical device. Never target the host. |
| Authenticated caller signs out others | Fence only their subject, exempt their calling session. Loopback's distinct operation fences all remote subjects, with no exemption. |
| A request renews or arrives during sign-out | Never recreate an active session from a revoked cookie. Revocations outlive every renewal and become visible to listeners only after commit. |
| Client reports a name or kind | Treat as bounded display-only data. Never use it to join, authorize or select a row. |

Migration is additive. There is no way to enumerate unpresented, pre-registry cookies: enrollment is on the first successful use, but bulk revocation must cover them without waiting for them to return. The bulk transaction writes existing SID tombstones for enrolled sessions and an instance- or subject-level cutoff for unpresented cookies. This binary and successors enforce both. A predecessor can still enforce every enrolled SID tombstone. Its only rollback gap is an unpresented pre-upgrade cookie that was still within its idle lifetime at rollback (30 days Personal or 7 days Organization from last renewal). After one uninterrupted idle lifetime on the upgraded binary, no such usable cookie remains. If rolled back sooner, the old binary can renew an admitted cookie under its old rules, up to the absolute cap. This is not unconditional rollback enforcement.

The list returns `mine` (host first for owners), `others`, and `bulk_action` (`other_sessions` for remote subjects, `all_remote` for direct loopback, or null for an unpaired setup host). Timestamps are Unix seconds. The only public action identifier is `id`; raw session IDs, installation IDs, push endpoints and keys are not returned. Successful foreground Push subscription/sync binds its `device_id` to the authenticated session. Push capability is read live by `(remote:<subject>, device_id, enabled)`; neither a user agent, name, IP address nor an account's subscription count is evidence of device identity. Clients without a reported stable ID initially use session identity. No UI, cloud call, Health, alarms or transport telemetry is introduced.
