# Live Activity Runtime and Web entry

Implements the frozen I5 contract in `2026-11-20-ios-live-activity-contract.md` and its shared vectors.

The controller owns registration, token rotation, stopping, phase observation and relay delivery. Public HTTP verifies session visibility and forwards operations over the existing authenticated internal socket. One async lock orders activity mutations and sends; no SQLite transaction spans relay IO. Rows survive controller restarts, and names/titles never enter the tracking store or publisher. The native bridge owns account/instance/logout cleanup.

Migration allocation: `20261120_0093`, provisionally following 0091 until D2's reserved 0092 lands. Recheck and rechain against master before opening the PR.

Validation: real migrated SQLite, public HTTP lifecycle and ownership, relay request vectors, publisher time/phase/retry/restart/stop behavior; native bridge UI states and production build. Production APNs and TestFlight acceptance belong to integration after authorized deployment.

## Delivery evidence

The initial real SQLite/public HTTP test failed before the implementation existed, then passed. Coverage now includes controller HTTP authentication, current Project authorization, all three pending-owner-request sources, registration and token rotation, terminal races, DELETE/send ordering, retry persistence, heartbeat failure, activity-token 410 isolation and eight-hour expiration. Migration tests compare every preexisting persisted row after upgrade and downgrade. The Web tests exercise the actual typed bridge, non-ASCII title capture, disabled Settings flow and all approved refusal messages. The production UI build and the English/Chinese, light/dark 390 px visual pass succeeded.

Publisher authorization reuses the existing signed Web Push context records and current delivery policy, stored in an internal `authorization_json` column; a changed Project ACL is checked before delivery. No SQLite transaction spans the HTTP relay request. Cancelled controller operations join their worker before releasing the serialization lock.

Delivery follows the revised contract: one durable `pending_state` holds the latest unsent state, with terminal states never overwritten. Transient failures receive at most three rapid retries, then one retry per 900 seconds, including terminal delivery. Every attempt reserves a fresh timestamp and refreshes confirmation time. Until an end is accepted, GET returns its terminal phase with tracking status so the phone can end it locally. Relay rejection stops only this activity. A dedicated outage test failed before this change, then passed alongside restart, replacement, terminal persistence and rejection coverage.
