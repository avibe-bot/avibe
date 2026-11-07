# Vault fetch in a Show API handler

Use this only in trusted `api/*.ts` server code on an Avibe service that supplies
`process.env.AVIBE_SHOW_SERVER_MODULE`. Never import it in `src/`, a browser
entry/barrel, or a shared module reachable from a browser bundle. Load
`use-avibe-vault` before performing credentialed work; refer to a secret by name
or `NAME.field`, not by value.

The helper calls the same resident service operation as `vibe vault fetch`,
without starting a Python CLI for each request. avault still owns credentials
and attaches them only at egress. Do not read transport snapshots, descriptors,
Vault storage or environment credentials yourself.

```ts
// api/provider.ts — server code only
export async function GET(request: Request, context) {
  const moduleURL = process.env.AVIBE_SHOW_SERVER_MODULE
  if (!moduleURL) return Response.json({ error: "server_adapter_unavailable" }, { status: 503 })
  const { vaultFetch } = await import(/* @vite-ignore */ moduleURL)
  const result = await vaultFetch({
    auth: "GITHUB_API.token",
    url: "https://api.github.com/user",
    method: "GET",
    noApprovalWait: true,
  }, {
    cwd: context.session.workspace,
    signal: request.signal,
  })
  if (result.payload.kind !== "vault_fetch") {
    // Handle approval_required separately in your page's application flow.
    // Never retry execution_unknown automatically.
    return Response.json({ error: result.payload.code }, { status: 503 })
  }
  return new Response(result.payload.body, { status: result.payload.status })
}
```

`headers` accepts a string map or an array of `"Name: value"` strings; `body` is
inline UTF-8 text. There are no file/output arguments. Upstream non-2xx responses
still have `kind: "vault_fetch"`, status and body, with `exit_code: 1`; they are
not transport failures. Other results carry a structured error. Do not forward
credential-bearing request headers or private runtime configuration to browsers.

Invocation context defaults to the server process's raw `AVIBE_*` context, just
as an inherited CLI subprocess did. If the replaced command explicitly supplied
an environment map, pass that same map as `callerEnv` in the second argument.
Never derive authority from incoming browser headers, grant the page owner's
identity to its visitors, or change `process.env`/cwd for individual requests.
A page Session ID is not, by itself, a new authorization mechanism.

Approval defaults to the CLI's 540-second wait; `approvalWaitSeconds` overrides
it, and `noApprovalWait` returns `approval_required` without waiting. Do not set
both. Ordinary Show API requests have their own configured timeout; registered
public server ingress has a shorter total bound. Prefer explicit no-wait calls
for handlers and handle approval through the existing browser/Session flow.
Pass a cancellation signal so the adapter closes its actual IPC connection when
that signal aborts. Do not assume `request.signal` observes a browser disconnect:
the currently pinned Show Runtime does not wire the incoming connection to that
signal. A handler-owned `AbortController` can enforce its own cancellation or
time budget; the adapter's signal test does not prove proxy-disconnect propagation.

A missing/older service returns `not_executed`; nothing starts a service or falls
back to SQLite. A lost/malformed response or interrupted post-send connection
returns `execution_unknown`; inspect the upstream state rather than replaying
it. If an approval completion response is lost, continuation ownership can be
unknown even though no fetch was sent: do not re-arm or retry automatically.

The Python Show owner refreshes private transport discovery before each admitted
API proxy request. A later request can therefore recover after the controller
starts or changes generation without restarting Show. An operation already sent
never rediscover-and-replays. This guarantee does not extend to detached timers
that run indefinitely without another proxied request; use the Harness for work
that must run in the background.

After migrating a handler, verify its response, non-2xx/error handling and
cancellation with test-owned state. Merely adding this helper does not rewrite
existing pages or authorize changing a live workspace.
