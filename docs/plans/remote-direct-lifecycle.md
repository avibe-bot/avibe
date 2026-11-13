# Instance direct lifecycle (I3)

`vibe/direct_lifecycle.py` owns the optional path in the Web UI process. It
never starts, stops or reconfigures Cloudflare Tunnel. The persistent setting
is `remote_access.vibe_cloud.direct`, additive and **false by default**.
`vibe remote direct on|off|status` changes/reads it; no service restart is
required. Web config saves notify the owner; a worker reads CLI/file changes
at 0.5-second intervals. A slow filesystem can delay observation, but cannot
extend a previously received permission past its independent expiry timer.

## Present product behavior

The production binary resolver consumes only a verified installation matching
Avibe's packaged published release pin. The pin still ships **pending**, so no
product binary is available yet. There is no environment variable or
configuration field for an arbitrary executable, and no automatic installation.
`vibe remote direct install [--offline] [--json]` is an Owner-only, explicit
installation command; it does not change the setting or pairing. An already
opted-in owner can start the installed binary after its ordinary gates pass.
See [managed runtime preparation](remote-direct-managed-runtime.md) for the
manifest and the gated regression handoff.

With the setting off, observation does not inspect a binary, bind a socket,
spawn a child, call the certificate client or create direct certificate files.
With it on and the pin pending or the matching binary absent, a managed paired
instance reports `transport-not-installed`: **it orders no certificate and
starts neither ingress nor supervisor**. Tests use inert archives and injected
transports. Native installation, the settings page and real iroh/Incus acceptance
remain separate operational work.

## Single owner and ordering

The Web UI loop owns configuration observations, permission receipts and
expiry, resource handles, publication and all supervisor operations. Config
observations are copied, not mutable references to a caller's object. Every
transition invalidates a local generation, so on–off–on cannot reuse a reply
from the first on. Every runtime-status request captures a ticket before HTTP;
only a reply to a request that actually carried `direct_paths_enabled: true`
can authorize the current generation. Permission is strictly less than 15
minutes from receipt, using a monotonic clock. Missing permission is denial.
A later denial cannot be undone by an older response. A certificate worker's
captured deadline can expire while a newer receipt refreshes the same owner
generation; that request-local refusal retries under the fresh grant without
discarding an installed certificate.

On/off changes enqueue reports immediately after observation rather than
waiting for the five-minute heartbeat. The existing single status-report
worker preserves these transition reports instead of coalescing away an
explicit false. Coalesced ordinary observations reload current configuration,
so an older queued heartbeat cannot undo the last transition's setting.
Network work is bounded by the existing report timeout and
never awaited by local stop. All runtime reports include the boolean choice.

Only HTTPS `*-app.avibe.bot` public hostnames, without credentials or a
nonstandard port, qualify. Custom domains fail closed as `hostname_not_direct`.
Eligibility is checked in this order: setting/pairing/hostname, installed
binary, supported subprocess loop, fresh permission, usable certificate.
In particular no `allowed=True` certificate call occurs before the binary
check. Certificate calls carry the pairing and permission fences in
[the certificate-client contract](remote-direct-cert-client.md).

Start is usable certificate → ingress → supervisor. The ingress is the same
CONNECT, TLS/SNI and ASGI/remote-auth boundary as before; no alternative stream
entry point is added. The supervisor publishes only after its `ready` message.
Its generation-local rendezvous view refuses obsolete publication, without
changing the shared rendezvous or control protocol. Stop is withdraw → stop
supervisor → stop ingress. Permission withdrawal or expiry stops serving, not
just certificate ordering. Renewals are polled at the client's returned
`next_poll_at`, including while a usable certificate is already installed.
One cancelled order is retried once; ordinary repeated polls do not turn it
into an unbounded retry loop.

## Blocking work and cancellation

Startup schedules observation without waiting for configuration, backend,
certificate or TLS work. Config remains observed at 0.5-second intervals by
its own retained worker. Binary verification has a **separate** retained worker
slot, scheduled/collected by the loop on a changed configuration and otherwise
every five seconds; binary hashes are never read by loop-side gates. A stalled
binary check cannot delay disk-only CLI opt-out observation or its false report.
Each successful binary observation expires 15 seconds from
its start on the loop's monotonic clock. A stalled worker cannot extend it;
expiry withdraws publication and cancels an awaiting start without waiting for
filesystem work. A later fresh observation may resume the normal gates.
Observations are generation-fenced, including off/on ABA and stop/start reuse;
replacing or losing a previously available binary retires its generation.
Every retired generation also drops its in-memory certificate observation and
requires a fresh client poll before a new start. A fenced renewal worker may
already have persisted its replacement and deleted the old chain, so reusing
cached file paths while that poll runs could otherwise latch startup failure.
This drops no on-disk key or certificate: the client revalidates its own state.
This is bounded observation, not atomicity against arbitrary local filesystem
mutation between verification and process execution.

Certificate work has one outstanding task; TLS
preparation has one retained worker slot across starts, reloads and all
setting generations. Preparation constructs replacement TLS/Uvicorn data,
but never binds a socket or changes active context/path fields in its worker.
The loop checks generation, ingress identity and permission before committing
a prepared start or renewed context. If the old ingress expires during renewal
preparation, the still-authorized certificate is retained for a fresh start,
not applied to the retired ingress or lost with its next polling deadline.
Failure to prepare a replacement leaves
the previous context intact. The existing synchronous ingress reload API is
retained for its previous callers.

Cancellation does not kill a Python worker thread. A permanently stuck
filesystem worker occupies its slot and can keep direct access unavailable;
it cannot accumulate more workers across toggles or publish a late result.
Local withdrawal and the ordinary Web UI/heartbeat do not wait for it. Python's
executor shutdown may itself wait for a permanently stuck thread; this change
does not claim to solve uninterruptible filesystem calls or guarantee process
exit in that case. An already admitted certificate HTTP request is likewise
not cancelled by cancelling its asyncio waiter.

## Pairing and process boundary

The stop-before-discard fence is conditional on the **persisted/base** direct
setting. When it is off (or there is no previous config), pairing saves do not
contact or wait for the UI owner: existing certificate retirement and pairing
save behavior are preserved. The candidate's setting is not the guard. A
previously-on pairing still stops before retirement even when the same save
turns direct off; a previously-off pairing does not acquire an IPC dependency
when a re-pairing candidate opts in.

For a previously-on pairing, an in-process writer posts a stop barrier to the
owner loop before existing certificate retirement. The barrier does not load
config or await a certificate worker: the writer already holds the config file
lock. The owner remains suspended until it observes the committed changed
pairing. A failed config commit deliberately leaves the optional path closed.

A CLI writer for a previously-on pairing uses
`POST /api/remote-access/direct/suspend` on the
ordinary loopback listener. This stop-only endpoint requires the existing
local CLI token **and** a direct loopback connection, and refuses direct-ingress
and forwarded/proxied requests. Normal Owner authorization remains; the exact
local-token request is exempt from browser CSRF just like existing CLI routes.
The client disables proxy inheritance and redirects. It sends no pairing
credential and accepts only a confirmed stop. This bounded, same-host IPC is
an explicit exception inside the existing config-file-lock transaction: it
serializes stop and certificate retirement against other pairing writers.
The handler takes no config lock, waits for no certificate worker and performs
no external HTTP. Its global authentication guard still reads the persisted
config before route admission; a separate-interpreter test proves that read
and the stop complete while another process holds the writer file lock, with
a failed lock-acquisition positive control to rule out reentrancy.

The connect/read and owner-stop timeouts are not a hard end-to-end 12-second
deadline. An unreachable or refusing UI owner can delay or fail a pairing save
only when the persisted/base direct setting was on; the off case makes no IPC
call. This is pairing-writer serialization,
not universal atomicity with every external event or already admitted HTTP.
If the UI is gone, nothing can serve; a 404 is compatibility with a pre-I3 UI
that has no lifecycle. Other
unreachable or refused stops fail the pairing save closed. An old CLI or manual
config-file edit cannot participate in this barrier; observation still
converges, but the strict pre-discard order requires an I3-aware writer.

CLI direct status reads the live UI's existing local status route when it can;
with no live UI it shows the static disabled/not-installed projection. A
setting change with the UI down still reports the choice from the CLI, but
never grants a nonexistent local owner permission to order.

## Status and platform limits

The closed state set is `disabled`, `waiting-permission`, `waiting-certificate`,
`transport-not-installed`, `starting`, `running`, `stopping`, `error`. The safe
projection contains only the setting, state/reason, certificate expiry,
transport-running flag and current permission boolean. It contains no address,
endpoint ID/key, token, or private-key file path. Transport running means the
child is spawned, not that the path is usable; `running`/`published` requires
the supervisor's published endpoint.

The main Web UI explicitly uses Uvicorn's asyncio loop; the locked Uvicorn
0.40.0 single-process factory selects Proactor on Windows. The owner fails
closed as `windows_proactor_required` if that is not the actual loop, before
ordering or opening listeners. Existing Windows CI jobs do **not** run the
direct-lifecycle/transport test files. Linux/macOS stand-in coverage is not a
claim of Windows subprocess or native iroh verification.

The sidecar contract requires exit when its control pipe reaches EOF. Tests
exercise both explicit pipe close and abrupt death of a real owner process.
Next start creates a new supervisor generation/control token and does not
adopt, enumerate or pattern-kill an unexplained survivor. A sidecar that ignores
EOF violates its contract; normal stop escalates only against the recorded
child PID. The actual managed sidecar and abnormal-survivor cleanup are not
proven by a Python stand-in.

## Explicit non-decisions

`rotate_key()` is an internal one-shot command, serialized against certificate
polling. It returns the certificate client's result, including
`certificate_order_in_progress` when active-key renewal causes the client to
abandon the pending rotation. **Automatic rotation timing remains open** with
the backend renewal design. No automatic key rotation, backend/iOS change,
settings page or real public issuance is added. The managed installer is a
later [I1/I3 consumer change](remote-direct-managed-runtime.md), not an
installation or enablement of any live instance.
