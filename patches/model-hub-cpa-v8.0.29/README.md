# Avibe-owned CPA v8.0.29 compatibility patches

This directory records the source patches for Avibe's planned
`model-hub-engine-v8.0.29-1` build. Apply the five patches in the order listed
in `docs/plans/model-hub-cpa-v8.0.29.md`; adoption will record that order in the
runtime manifest's `build.patches`. The packaged manifest is unchanged.
The config-write/reload atomicity fix tracked in issue #2686 is planned for
3.2.2 but is not implemented here; its 3.2.1-stable-tag implementation hold,
Go regressions and upstream handoff are recorded in the upgrade plan.

Base:

- Repository: `router-for-me/CLIProxyAPI`
- Release: `v8.0.29`
- Source commit: `5fc580b1d72d87a7bcfe5cfb6f479c1aea5625b3`

Carried over from `model-hub-cpa-v8.0.23/`: patches 1 to 5. Patches 1 to 4 rebase
unchanged (only `index` lines and hunk offsets differ; patch 1 is byte-identical).
Patch 5 is restructured for v8.0.28's request-scoped transport decorator and
continues to apply cleanly to v8.0.29. Patch 6
(`codex-sse-per-event-parsing.patch`) is dropped: v8.0.28 parses Codex SSE per
event (see the end of this file).

## `openai-compat-native-token-field.patch`

Avibe owns the OpenAI-compatible Source boundary. When an incoming Chat
request contains `max_completion_tokens`, or `max_tokens`, the engine must
preserve that caller-selected field unless Avibe explicitly selects the
`use-max-completion-tokens` compatibility mode for a model. The stock
default rewrites the former field to the latter, which breaks upstreams that
accept only the native field.

The patch therefore makes the stock `false` path a preservation path. The
existing explicit `true` path still canonicalizes to
`max_completion_tokens`. Avibe does not set the option today, so both
buffered and streaming requests retain their native token-limit field.

The patch is limited to the helper used by both OpenAI Chat execution paths
(`openai_compat_executor.go`, buffered and streaming). The helper and its call
sites are unchanged from v8.0.23 through v8.0.29; source inspection shows that
stock v8.0.29 still rewrites the field, as asserted by upstream's
`TestNormalizeOpenAIMaxTokens`.

The patch also updates the three `useMaxCompletionTokens=false` cases of
upstream's `TestNormalizeOpenAIMaxTokens`, which asserted the stock rewrite,
to assert this contract: the payload, including a `null` limit, is returned
unchanged. It updates the four `use-max-completion-tokens=false` chat cases of
upstream's executor-level `TestOpenAICompatExecutor_MaxTokensNormalization`
and `TestOpenAICompatExecutor_MaxTokensNormalizationStream` the same way: the
request keeps `max_completion_tokens`, including `null`, and gains no
`max_tokens`. On v8.0.29 with all five patches applied and Go 1.26.9,
`go test ./internal/runtime/executor/` and
`go test ./internal/runtime/executor/helps/` both pass. The test changes do
not alter the engine binary.

## `oauth-callback-forwarder-loopback.patch`

Avibe starts every redirect-based subscription login (Claude, Codex,
Antigravity) with `is_webui=true`. The engine then starts a callback forwarder
on the vendor's fixed redirect port (54545, 1455 or 51121) that relays the
browser's redirect to the management callback. Stock CPA binds that forwarder
on `0.0.0.0` and aborts the login with a 500 when the bind fails.

The patch binds the forwarder on `127.0.0.1` only, because the redirect always
comes from a browser on the same machine. A wildcard bind exposed the port to
the network and, on Windows, raised a Defender Firewall prompt for the engine.
A failed bind (the port is in use, or reserved by Hyper-V, WSL or Docker) is
logged and no longer aborts the login: Avibe's Web UI still accepts the pasted
callback URL, which reaches the same session through `POST /oauth-callback`.

A container deployment that publishes the callback ports to reach a browser
outside the container loses the automatic callback, since the forwarder no
longer listens on the container's external interface. The pasted callback URL
completes the login there.

Go's wildcard listen is dual-stack, so the stock forwarder also accepted
`[::1]`; the patched one accepts 127.0.0.1 only. The vendors' redirect URIs
name `localhost`, and a browser that tries `::1` first is refused at once and
falls back to 127.0.0.1, as mainstream browsers do. Where one does not, the
pasted callback URL still completes the login. Revisit this if anyone reports
a refused connection to `localhost` at the OAuth callback.

## `antigravity-static-catalog-until-probe.patch`

Avibe ships Gemini subscriptions through Antigravity OAuth grants and routes
to them as soon as the engine has loaded a grant: right after every engine
start and right after a new login. Since v8.0.14 the stock engine registers no
Antigravity models until its per-account entitlement probe
(`/v1internal:fetchAvailableModels`) answers. Until then a request gets
`model_not_found` and model discovery sees an empty catalog; when the probe
fails, the account stays unroutable until a backed-off retry succeeds
(1-2 minutes at first, up to 30 minutes).

The patch registers the static Antigravity catalog while no entitlement hints
are cached. The probe that registration already queues narrows the catalog
through the fenced `ReplaceClientModels` when it answers; a failed probe leaves
the static catalog in place. This is the pre-v8.0.14 startup behavior plus
the later narrowing. The patch also rewrites upstream's
`TestAntigravityModelRegistration_AsyncNonBlocking`, which asserted the stock
behavior, to assert this contract: the static catalog is registered before the
probe answers, then narrowed to the entitled set. On v8.0.29 with all five
patches and Go 1.26.9, `go test -run Antigravity ./sdk/cliproxy/` passes.
The full SDK suite has a separate intermittent registration-concurrency
failure, also reproduced without Avibe's patches; see the upgrade plan's known
risk and owner-selected acceptance policy.

Upstream closed router-for-me/CLIProxyAPI#6531 with v8.0.24 (091ff27f), which
persists the probe's entitlement hints in the auth file and seeds them at the
next start. That covers a restart after one successful probe only: a new login,
and every grant written by an older engine on its first start, still register
nothing until the probe answers, and a failed probe leaves them unroutable for
the backoff. Measured on stock v8.0.28 with a synthetic grant: `model_not_found`
from the first request and an empty listing, nothing at all while the probe
fails, and an immediate Source refresh refused. This remains the last completed
synthetic-grant measurement, not v8.0.29 evidence. Keep this patch
until upstream registers a catalog while no hints exist; where persisted hints
exist the patch never runs, because `hints.ModelIDs` is set.

## `codex-utls-h2-ping-health-check.patch`

Codex subscription requests to `https://chatgpt.com` go through a dedicated
Chrome uTLS connection per request. When ALPN selects HTTP/2, stock CPA wraps
that connection in a zero-value `http2.Transport{}`: no PING health check, no
client timeout, nothing that bounds the body read. A path that stops
delivering packets without a FIN or RST reaching the engine, which a NAT or a
TCP-terminating proxy can leave behind and TCP keepalive cannot see past,
therefore blocks the stream until the caller gives up. Avibe's turns then
waited out their 600-second bound with no byte and no error. The same code is
in v7.3.16, so this is not a v8 regression.

The patch sets `ReadIdleTimeout` 30 s and `PingTimeout` 15 s on that
connection's `http2.Transport` only. After 30 s without a frame the transport
sends one PING and closes the connection when no ACK arrives within 15 s, so
the stream fails with an in-stream `request_timeout` error at about 45 s.
Model Hub's recovery then retries on a new connection. A live upstream that
is silent, for example during a long reasoning phase, acknowledges the PING
and is not cut; an active stream sends no PING. The shared transports, the
proxy transports, the HTTP/1.1 Claude round tripper and the WebSocket path
are untouched. The patch adds `utls_client_health_test.go`: a dead path fails
the stream, and a silent healthy stream survives ten idle periods.

Measured on the shipped v8.0.23 engine on the uTLS HTTP/2 path through a
black-holing CONNECT proxy: stock sent no byte and no error for 9 min 38 s,
until it was killed; patched sent `event: error` (`request_timeout`) at
45.0 s. A live stream silent for 120 s completed on both. Re-measured on
v8.0.28 through the same kind of path: stock silent until the client's 120 s
bound, patched `request_timeout` at 45.06 s, and a live stream silent for 90 s
completed on both. These remain historical v8.0.28 measurements; the owner
selected a single local service E2E rather than requiring repeat timing probes.

Upstream declined the health check. router-for-me/CLIProxyAPI#6534 was
closed as not planned: by the project's design policy CPA sets no timeout
once an upstream connection is established, HTTP/2 health checks and stream
read deadlines included, not even as an opt-in setting, and leaves timeouts
and cancellation to the downstream client. The opt-in pull request,
router-for-me/CLIProxyAPI#6535, was closed unmerged. Avibe therefore carries
this patch by design as its client-side transport bound for this connection.
No drop is planned; re-evaluate only if upstream changes its post-connect
timeout policy. The cost is that every CPA upgrade must rebase this patch and
`fallback-transport-h2-ping-health-check.patch`.

## `fallback-transport-h2-ping-health-check.patch`

Patch 4 covers only the dedicated uTLS connection to `https://chatgpt.com`.
Every other upstream request on an HTTP client from
`internal/runtime/executor/helps` ran on `http.DefaultTransport`, or on a
per-request proxy transport, without an HTTP/2 health check. That covers
API-key relays and OpenAI-compatible providers. The same dead-path failure
therefore reached Avibe through API-key relays: a turn on an HTTP/2 relay
whose path went dead waited out the 600-second bound with no byte and no
error.

The patch gives those requests one shared clone of `http.DefaultTransport`,
created on first use, with `HTTP2Config{SendPingTimeout: 30s, PingTimeout:
15s}`, the same timings as patch 4, as constants. It is used:

- as `NewUtlsHTTPClient`'s standard fallback for hosts other than chatgpt.com
  and api.anthropic.com (the Codex and Claude executors);
- by the OpenAI-compatible executor, through `WithUpstreamFallbackTransport`,
  which sets it only when `NewProxyAwareHTTPClient` left the transport nil.

`buildProxyTransport` sets the same health check on proxy transports.
`http.DefaultTransport` itself is not modified, and `NewProxyAwareHTTPClient`
still returns a nil transport when no proxy and no injected RoundTripper
apply, because the Antigravity executor reads that as "direct" and installs
its own HTTP/1.1 pool.

v8.0.28 added a request-scoped transport decorator (`sdk/httptransport`):
`NewProxyAwareHTTPClient` now ends by wrapping its transport, or
`http.DefaultTransport` when it has none, in any decorator the context carries.
Wrapping the helper's client with the fallback afterwards would then find a
non-nil transport and skip the health check. The OpenAI-compatible executor
therefore builds its client through `NewUpstreamHTTPClient`: the helper runs
with `WithoutDecorator`, the shared transport fills the empty slot, and
`Decorate` is applied once at the end, the same order upstream uses for the
Antigravity executor's own pool. Stock CPA sets no decorator, so this changes
nothing today; it keeps the contract if one is ever set. The injected context RoundTripper is untouched, as in
patch 4. The health check only sets HTTP/2 timings: protocol selection
(`ForceAttemptHTTP2`, `Protocols`, `TLSNextProto`, ALPN) is unchanged, and a
transport that did not opt into HTTP/2 still speaks HTTP/1.1.

Avibe renders `proxy-url: ""` and no per-Source proxy, so its Sources take the
shared clone. Coverage by Avibe Source kind:

| Source | CPA path | Covered |
| --- | --- | --- |
| `openai_responses` API key (OpenAI and xAI relays) | `codex-api-key`, `NewUtlsHTTPClient` fallback | yes |
| `anthropic` API key on a non-Anthropic host | `claude-api-key`, `NewUtlsHTTPClient` fallback | yes |
| `openai_chat` API key | `openai-compatibility`, `WithUpstreamFallbackTransport` | yes |
| `anthropic` API key on `api.anthropic.com` | HTTP/1.1 Claude round tripper | no: PING is HTTP/2 only |
| ChatGPT subscription | patch 4's uTLS connection | patch 4 |

An HTTP/1.1 upstream has no PING, so a silent HTTP/1.1 body is still bounded
only by the caller. The patch adds `upstream_transport_test.go`:

- a dead path fails the stream;
- a silent healthy stream survives ten idle periods;
- the shared transport is wired into both clients and the proxy transports;
- a request-scoped decorator wraps the health-checked transport, not
  `http.DefaultTransport`;
- protocol selection is unchanged.

Measured on v8.0.28 with a black-holing middlebox in front of an HTTP/2
relay: stock stayed silent until the client's 120 s bound on both the
`codex-api-key` and the `openai-compatibility` path; patched failed the stream
at 45.04 s and 45.05 s. A live stream silent for 90 s completed on both.
These are historical v8.0.28 measurements, not additional adoption gates.

Upstream: the same failure mode as router-for-me/CLIProxyAPI#6534, declined
under the same policy (see `codex-utls-h2-ping-health-check.patch`), so no
upstream follow-up is pending. Avibe carries this patch by design as its
client-side transport bound for these transports. No drop is planned;
re-evaluate only if upstream changes its post-connect timeout policy. Every
CPA upgrade must rebase it together with that patch.

## Dropped: `codex-sse-per-event-parsing.patch`

v8.0.29 retains the native `CodexSSEScanner` introduced in v8.0.28, so the
patch remains dropped. On stock v8.0.28 the two-line split of `response.completed`,
a split at an output-element boundary, the CRLF split, blank-less framing, a
well-formed stream, and an event left unterminated at EOF all complete as on
v8.0.23-3, where v8.0.23-2 failed the split cases with `invalid SSE data JSON`.
Differences from the dropped patch, none of which Avibe's upstreams produce: a
field or comment line inside a split event is emitted before the merged data line
rather than after it; an unclosed fragment followed by a complete object with no
blank line is one invalid event rather than two; an invalid event logs one
warning per stream without the patch's per-line framing details.

## Trial status and rebuild

The exact v8.0.29 source plus these patches builds with Go 1.26.9. The executor,
helper, management and focused Antigravity tests pass, as do the five hosted
passthrough cases on the native darwin-arm64 trial binary. The engine contract
and wire suites report 26 passed and 1 skipped; the fixed OAuth callback-port
case was not run on the host. The full SDK suite is **not green**: its
registration-concurrency test also fails intermittently on stock v8.0.29 and
remains a known risk, not a separate blocker under the owner's 2026-10-15
acceptance decision. The v8.0.28 timing measurements above remain historical,
not v8.0.29 measurements. See `docs/plans/model-hub-cpa-v8.0.29.md` for evidence
and the single local Incus service E2E acceptance path.

The repository-owned release path checks out the manifest's `source_sha`,
applies `build.patches`, builds every supported target with `CGO_ENABLED=0`,
and packages deterministic archives. The packaged manifest still selects
v8.0.23, so the default command below rebuilds **v8.0.23**, not this trial:

```sh
python3 scripts/model_hub_engine_release_guard.py fetch-source \
  --output-dir /tmp/model-hub-engine-patched
```

A v8.0.29 build needs a separate trial manifest pinning the source above and
these five patches; do not edit the packaged manifest to run the trial. The
manual Model Hub release-guard workflow likewise uses its selected manifest,
not this directory automatically. Adoption must publish the reviewed build
assets and record their archive and binary digests before changing the
packaged runtime pin.
