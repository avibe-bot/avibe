# HTTP ingress request diagnostics

## Contract

`POST /api/remote-access/diagnostics` accepts `{"operation":"snapshot"}` under
its existing Member-or-Owner, normal authentication and CSRF policy. It returns
`{"ok":true,"request_counts":{...}}` without performing connectivity probes.
The existing empty `{}` operation continues to run connectivity diagnostics
unchanged. There is no new URL, UI toggle, authorization scheme, start/clear
operation, persistent setting, log sink or public status/heartbeat field.

The snapshot contains `enabled`, `scope: "ui_service_lifetime"`, `counts`, and
`saturated`. `counts` is exactly six integer cells:

- Ingress: `direct_ingress` or `main_listener`.
- Method: `get_head_options`, `post_put_patch_delete`, or `other`.

Each cell saturates at 2^63−1; `saturated` warns that deltas may undercount.
Method classes are syntactic, not claims that reads cannot have side effects or
that a mutation committed. No request bodies, paths, query strings, headers,
peer/account/session values or request IDs are stored in the aggregate.

## Ownership and enablement

The UI loop owns one fixed-size in-memory table. HTTP admissions increment it
only while the existing direct lifecycle observes the instance's direct setting
as enabled, even if the optional transport is unavailable. It reuses the existing
configuration observer: disk-only changes take effect on the next successful
observation, not atomically with the filesystem write. No request loads config,
locks state, starts a task, reads a clock or writes a log for these counters.
Disabled requests only check the scope type and in-memory enablement gate; they
do no classification or counter work. WebSocket and lifespan scopes bypass it.

OFF pauses counting but retains the service-lifetime totals; ON resumes them.
UI service restart resets the table. There is no expiry, rolling window, sealed
capture or per-request retained record. Authorized snapshots copy fixed values;
they cannot mutate the table. When enabled, the snapshot HTTP request itself is
included in `main_listener/post_put_patch_delete` if it arrives on that listener.

## What the evidence means

The observer counts once at shared UI application admission, outside UI
host/auth/CSRF middleware, after the strict `DirectIngressGuard` on the direct
listener. The direct class comes only from the server's ASGI scope marker,
never a client header. Strict-ingress rejections, CONNECT/TLS/HTTP parser failures
before this application boundary are excluded. UI auth/CSRF rejection, 404/405,
exceptions and disconnects after admission still count.

One keep-alive connection may carry multiple counted requests. SSE is one HTTP
admission regardless of chunks or connection lifetime. WebSocket handshakes and
frames are excluded. Receive/send, streaming, cancellation and errors pass
through unchanged; there are no response-start/status or completed-body counters.
These are request admissions, not delivered responses or committed effects.

`main_listener` includes desktop/local/LAN/health/status and tunnel traffic; it
is **not** a proof of Cloudflare routing. Concurrent clients contaminate
per-client inference. Neither client fallback nor device path-loss has a
trustworthy event source in these instance diagnostics; both are unavailable,
not zero. Supervisor stream/refusal totals and lifecycle stops cannot establish
either event and are not relabeled as such.

The snapshot does not prove replay absence, exactly-once effects, App-session
attribution or physical route choice. A trial may compare two snapshots while
accounting for its own diagnostic requests; resets/restarts and saturation
invalidate naive deltas. Marked-message committed-effect checks remain separate.
Any exported test evidence has its own retention policy; the server persists
none of this aggregate.

## Validation and operational boundary

Hermetic coverage protects classification/privacy/cardinality/saturation,
disabled and non-HTTP bypass, repeated requests, streaming/error propagation,
existing role/auth/CSRF protection, unchanged connectivity diagnostics, setting
observation and service reset. Installed/native/device/Incus qualification is
separate. Merging this source does not authorize deployment, installation,
certificate issuance, opt-in or general enablement.
