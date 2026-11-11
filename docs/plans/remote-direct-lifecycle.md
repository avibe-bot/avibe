# Instance direct lifecycle (I3)

`vibe/direct_lifecycle.py` owns the optional path in the Web UI process. It
never starts, stops or reconfigures Cloudflare Tunnel. The persistent setting
is `remote_access.vibe_cloud.direct`, additive and **false by default**.
`vibe remote direct on|off|status` changes/reads it; no service restart is
required. Web config saves notify the owner; a worker reads CLI/file changes
at 0.5-second intervals. A slow filesystem can delay observation, but cannot
extend a previously received permission past its independent expiry timer.

## Present product behavior

The production binary resolver deliberately returns no installed transport.
There is no environment variable or configuration field for an arbitrary
executable. With the setting off, observation does not bind a socket, spawn a
child, call the certificate client or create direct certificate files. With it
on, a managed, paired instance reports `transport-not-installed`: **it still
orders no certificate and starts neither ingress nor supervisor**. Only tests
inject a stand-in. Installation, the settings page and the real iroh/Incus
scenario are later work.

## Single owner and ordering

The Web UI loop owns configuration observations, permission receipts and
expiry, resource handles, publication and all supervisor operations. Config
observations are copied, not mutable references to a caller's object. Every
transition invalidates a local generation, so on–off–on cannot reuse a reply
from the first on. Every runtime-status request captures a ticket before HTTP;
only a reply to a request that actually carried `direct_paths_enabled: true`
can authorize the current generation. Permission is strictly less than 15
minutes from receipt, using a monotonic clock. Missing permission is denial.
A later denial cannot be undone by an older response.

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
certificate or TLS work. Certificate work has one outstanding task; TLS
preparation has one retained worker slot across starts, reloads and all
setting generations. Preparation constructs replacement TLS/Uvicorn data,
but never binds a socket or changes active context/path fields in its worker.
The loop checks generation, ingress identity and permission before committing
a prepared start or renewed context. Failure to prepare a replacement leaves
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

Before existing certificate retirement, an in-process pairing writer posts a
stop barrier to the owner loop. The barrier does not load config or await a
certificate worker: the writer already holds the config file lock. The owner
remains suspended until it observes the committed changed pairing. A failed
config commit deliberately leaves the optional path closed.

A CLI pairing writer uses `POST /api/remote-access/direct/suspend` on the
ordinary loopback listener. This stop-only endpoint requires the existing
local CLI token **and** a direct loopback connection, and refuses direct-ingress
and forwarded/proxied requests. Normal Owner authorization remains; the exact
local-token request is exempt from browser CSRF just like existing CLI routes.
The client disables proxy inheritance and redirects. It sends no pairing
credential and accepts only a confirmed stop. If the UI is gone, nothing can
serve; a 404 is compatibility with a pre-I3 UI that has no lifecycle. Other
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
the backend renewal design. No automatic key rotation, sidecar installation,
backend/iOS change, settings page, real public issuance or Incus scenario is
part of I3.
