# Live Activity Runtime and Web entry

Implements the frozen I5 contract in `2026-11-20-ios-live-activity-contract.md` and its shared vectors.

The controller owns registration, token rotation, stopping, phase observation and relay delivery. Public HTTP verifies session visibility and forwards operations over the existing authenticated internal socket. One async lock orders activity mutations and sends; no SQLite transaction spans relay IO. Rows survive controller restarts, and names/titles never enter the tracking store or publisher. The native bridge owns account/instance/logout cleanup.

Migration allocation: `20261120_0093`, following D2's `20261120_0092`, merged in `1da3fb05d7`. Table and index creation are replay-safe for unversioned databases already at the current schema. The migration uses literal additive operations that the compatibility guard can prove; the controller's lifecycle service owns status transitions.

Validation: real migrated SQLite, public HTTP lifecycle and ownership, relay request vectors, publisher time/phase/retry/restart/stop behavior; native bridge UI states and production build. Production APNs and TestFlight acceptance belong to integration after authorized deployment.

## Delivery evidence

The initial real SQLite/public HTTP test failed before the implementation existed, then passed. Coverage now includes controller HTTP authentication, current Project authorization, all three pending-owner-request sources, registration and token rotation, terminal races, DELETE/send ordering, retry persistence, heartbeat failure, activity-token 410 isolation and eight-hour expiration. Migration tests compare every preexisting persisted row after upgrade and downgrade. The Web tests exercise the actual typed bridge, non-ASCII title capture, disabled Settings flow and all approved refusal messages. The production UI build and the English/Chinese, light/dark 390 px visual pass succeeded.

Publisher authorization reuses the existing signed Web Push context records and current delivery policy, stored in an internal `authorization_json` column; a changed Project ACL is checked before delivery. No SQLite transaction spans the HTTP relay request. Cancelled controller operations join their worker before releasing the serialization lock.

Delivery follows the revised contract: one durable `pending_state` holds the latest unsent state, with terminal states never overwritten. Transient failures receive at most three rapid retries, then one retry per 900 seconds, including terminal delivery. Every attempt reserves a fresh timestamp and refreshes confirmation time. Until an end is accepted, GET returns its terminal phase with tracking status so the phone can end it locally. Relay rejection stops only this activity. A dedicated outage test failed before this change, then passed alongside restart, replacement, terminal persistence and rejection coverage.

The row lifecycle follows §4.4: immutable `requested_at` anchors expiry to the phone's Activity request, including delayed registration; expired PUTs receive `activity_expired`. A `token_gone` rejection preserves pending state and permits a different token to resume a still-live Turn, while all other stopped rows stay stopped. A PUT observing a terminal Turn settles an existing tracking row locally and clears its pending end. Real HTTP and SQLite tests cover immutable timestamps, clock-skew validation, expiry, token replacement and local terminal settlement.

Loss of a source also stops delivery, even with an end pending: removed/disabled registration, confirmed loss of session access, or a deleted Turn. Tests distinguish confirmed revocation from temporary authorization-service unavailability; the latter defers delivery without claiming that access was revoked.
