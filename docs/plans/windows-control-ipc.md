# Windows Controller Control IPC

## Status

- Owner: Avibe Controller control IPC
- Scope: the existing internal HTTP routes and SSE streams
- POSIX transport: unchanged Unix-domain socket
- Windows transport: authenticated ephemeral IPv4 loopback TCP
- Contract version: 1

## Goal

The Controller and its local UI/CLI child processes need one HTTP transport on
native Windows without changing the routes, response bodies, or SSE event
ordering that already run over a Unix-domain socket on POSIX.

This contract does not introduce custom HTTP over named pipes. It does not
change Runtime process ownership, Tauri lifecycle, terminal, Vault credential
IPC, agent backends, Show Runtime, dependency installation, or packaging.

## Endpoint Selection

### POSIX

POSIX keeps the existing endpoint and behavior:

- default socket: `${AVIBE_HOME}/state/dispatch.sock`;
- override: `VIBE_INTERNAL_DISPATCH_SOCKET`;
- transport: `AF_UNIX` stream socket;
- socket mode: best-effort `0600` under a restrictive bind-time umask;
- HTTP base URL used by clients: `http://localhost`;
- no bearer header or endpoint descriptor.

An explicit `socket_path` passed by an existing caller remains an explicit UDS
endpoint. Existing POSIX callers do not read the Windows descriptor.

### Windows

The Controller pre-binds an `AF_INET` listener to `127.0.0.1:0`, calls
`listen`, and then publishes:

`%AVIBE_HOME%\runtime\control-ipc.json`

`AVIBE_HOME` retains its existing meaning. The default therefore lives below
the current user's Avibe home, not a machine-wide temporary directory.

The UTF-8 JSON descriptor has exactly this version-1 shape:

```json
{
  "schema_version": 1,
  "transport": "tcp",
  "host": "127.0.0.1",
  "port": 49152,
  "instance_id": "d69b9554dfb64cb6a1ca1df963fb3888",
  "bearer_token": "43-or-more-URL-safe-random-characters"
}
```

| Field | Producer | Consumer | Contract |
| --- | --- | --- | --- |
| `schema_version` | Controller host | internal clients | Integer `1`; any other value is unsupported |
| `transport` | Controller host | internal clients | Literal `tcp` |
| `host` | Controller host | internal clients | Literal `127.0.0.1`; hostnames, wildcard addresses, and non-loopback addresses are rejected |
| `port` | bound listener | internal clients | Integer `1..65535`; it is read from the pre-bound listener |
| `instance_id` | Controller host | server middleware and internal clients | 128-bit random lowercase hexadecimal identifier |
| `bearer_token` | Controller host | server middleware and internal clients | At least 256 bits from the OS CSPRNG, encoded as URL-safe text |

Unknown top-level fields are rejected in version 1. The descriptor is a local
secret because it contains the bearer token.

The Controller creates the runtime directory, lock file, temporary descriptor,
and stable descriptor with explicit private security:

- POSIX uses `0700` for the directory and `0600` for files;
- Windows uses a protected, non-inherited DACL with full access granted only to
  the current user SID and Local System, and records the current user SID as
  owner.

Windows creation passes this security descriptor to `CreateDirectoryW` and
`CreateFileW`; it does not expose an inherited-permission file and tighten it
afterward. Before consuming a descriptor, the client validates the opened
file's owner and exact protected DACL. A descriptor, lock, or runtime directory
with a different owner, an inherited DACL, or any additional allow entry is
rejected. A producer may tighten an existing current-user-owned artifact during
publication for upgrade compatibility, but it never changes or accepts an
artifact owned by another principal. Windows security API failures fail startup
or discovery closed.

Descriptor contents, authorization headers, and bearer tokens must not appear
in logs, exceptions, HTTP response bodies, or test failure output.

## Producer And Consumer Ownership

The Controller-side `ControlIpcHost` owns:

1. transport selection;
2. listener creation and pre-bind;
3. instance ID and bearer generation;
4. atomic descriptor publication;
5. request authentication and instance response headers on Windows;
6. owner-safe endpoint cleanup.

`vibe.internal_client` owns:

1. platform-specific endpoint discovery;
2. strict descriptor parsing;
3. attaching authentication to every Windows request and stream;
4. checking the responding instance on every Windows response and stream;
5. translating discovery, connect, authentication, and stale-instance failures
   into the existing unavailable/timeout behavior.

The UI-process Model Hub RPC client uses this same endpoint discovery,
transport, bearer header, and instance-response validation contract. It does
not inspect `dispatch.sock` or construct a UDS transport independently.

Any internal caller that opens `dispatch.sock` directly instead of using the
shared client connection contract remains POSIX-only until its owning lane
migrates it. The Windows server never provides an unauthenticated compatibility
path for such callers.

## Authentication And Instance Binding

Every Windows request, including `GET /internal/health`, both SSE routes, and
all mutating routes, carries:

```text
Authorization: Bearer <bearer_token>
```

The server compares the credential in constant time. Missing, malformed, or
incorrect credentials receive a generic `401` response. The response does not
echo the credential or explain which part failed.

Every authenticated Windows response carries:

```text
X-Avibe-Control-Instance: <instance_id>
```

This includes framework-generated `500 Internal Server Error` responses for
unhandled route exceptions. The Windows-only error handler preserves the
framework's status and plain-text body while adding the instance header.

The client compares that value with the descriptor's `instance_id` before it
accepts a response or consumes an SSE body. A missing or mismatched header is a
stale-instance failure. This prevents a stale descriptor from silently
connecting to an unrelated listener or a successor Controller that reused the
same port.

POSIX remains authenticated by possession and filesystem permissions of the
Unix socket and does not require these HTTP headers.

## Descriptor Validation

A Windows client rejects the descriptor before opening a connection when any
of the following is true:

- the path is absent, a symlink, not a regular file, unreadable, or larger than
  the bounded descriptor size;
- JSON is malformed, is not an object, has missing or unknown fields, or uses
  the wrong primitive types;
- `schema_version` or `transport` is unsupported;
- `host` is not exactly `127.0.0.1`;
- `port` is outside `1..65535`;
- `instance_id` is not the version-1 128-bit hexadecimal form;
- `bearer_token` is not the version-1 URL-safe minimum-strength form.

Errors identify only the descriptor category and path. They never include raw
descriptor data.

## Startup And Atomic Publication

Windows startup is ordered:

1. create the runtime directory and user-only lock file;
2. create a fresh instance ID and bearer token;
3. create a fresh TCP socket;
4. enable exclusive-address semantics where Windows supports them;
5. bind to `127.0.0.1:0`, call `listen`, and set non-blocking mode;
6. read the assigned port from `getsockname`;
7. write and `fsync` a user-only temporary descriptor in the runtime directory;
8. while holding the descriptor lock, atomically replace
   `control-ipc.json`;
9. hand the already-bound listener to uvicorn.

The stable descriptor never contains an unbound or requested port. Bind
failure publishes nothing. Publication failure closes the listener and leaves
the previous stable descriptor untouched.

An `EADDRINUSE` result while binding the ephemeral listener is retried with a
fresh socket up to three total bind attempts. Other bind errors fail startup
immediately. The OS still selects every candidate port; Avibe does not scan or
persist a preferred port.

## Shutdown And Stale Instances

Graceful shutdown first stops the server task and closes its listener. The
Windows host then acquires the same descriptor lock used by publication,
re-reads the stable descriptor, and removes it only when both `instance_id` and
`bearer_token` match the current host.

A stale Controller whose descriptor has already been atomically replaced by a
successor therefore leaves the successor descriptor untouched. Missing,
malformed, or non-owned descriptors are also left untouched.

A hard crash can leave a descriptor behind. Clients treat connect failure,
authentication failure, or instance-header mismatch as unavailable. A
successor binds a fresh listener and atomically replaces the stale descriptor
after bind succeeds. Each new host instance generates a new instance ID and
bearer credential; neither credential is reused across a successor publication.

POSIX keeps its existing stale socket replacement on startup and socket unlink
on graceful server shutdown.

## Retry And Reconnect

- Listener bind: only `EADDRINUSE`, at most three total attempts, always with a
  fresh socket.
- Descriptor publication: one atomic replace attempt while locked; a failure is
  a startup failure and does not publish partial JSON.
- Ordinary request: no hidden application-level retry. The existing caller
  decides whether unavailable/timeout is retryable.
- SSE connection: one descriptor snapshot per connection. The client does not
  switch instances inside a live stream.
- SSE reconnect: the existing subscriber reconnect loop calls the client again;
  that new call re-reads the descriptor, re-authenticates, and validates the new
  instance header.

This avoids replaying mutating requests while allowing a long-lived SSE
consumer to follow a restarted Controller safely.

## HTTP And SSE Compatibility

Windows exposes the same FastAPI application, methods, paths, status codes,
JSON bodies, and SSE payloads as POSIX. Authentication is transport middleware,
not route logic.

For both `POST /internal/dispatch` and `GET /internal/events`:

- uvicorn receives the pre-bound listener;
- SSE framing remains `event:` followed by JSON `data:` and one blank line;
- non-ASCII JSON content remains UTF-8 end to end;
- event order is unchanged;
- authentication and instance validation complete before the client consumes
  the first SSE event;
- a disconnect does not replay or reorder events; the existing route/caller
  semantics decide what a new request observes.

## Verification Contract

Focused automated evidence must cover:

- unchanged POSIX UDS selection and explicit UDS override;
- Windows TCP descriptor selection and strict validation;
- Windows current-user/SYSTEM-only owner and DACL enforcement for the runtime
  directory, lock, temporary descriptor, and stable descriptor;
- rejection of widened or non-owner Windows artifacts, plus upgrade-time repair
  only for current-user-owned artifacts;
- real loopback HTTP/SSE with non-ASCII dispatch data and preserved event order;
- missing and incorrect bearer rejection;
- stale descriptor and mismatched instance rejection;
- Model Hub sync and async RPCs through the shared Windows endpoint, bearer,
  and instance validation;
- instance headers on authenticated framework-generated 500 responses;
- atomic descriptor replacement without partial reads;
- owner-safe cleanup after successor replacement;
- occupied-port rebind behavior with a fresh socket;
- SSE reconnect through a successor descriptor;
- Ruff on every changed Python file.

A focused `windows-latest` CI job is required until an existing Windows job
installs the test dependencies and exercises this contract.
