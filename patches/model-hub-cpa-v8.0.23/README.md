# Avibe-owned CPA v8.0.23 compatibility patches

This directory records the source patches carried by Avibe's
`model-hub-engine-v8.0.23-3` build. Each patch applies independently to the
pinned source; the build applies them in the order the manifest's
`build.patches` records.

Base:

- Repository: `router-for-me/CLIProxyAPI`
- Release: `v8.0.23`
- Source commit: `d318bcc3afb9ea8782862f5e8cdb541afdc9dfa5`

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
(`openai_compat_executor.go`, buffered and streaming). The helper is
byte-identical between v8.0.20 and v8.0.23 and differs from v7.3.16 only in
its module import path, outside the patched hunk.

The patch also updates the three `useMaxCompletionTokens=false` cases of
upstream's `TestNormalizeOpenAIMaxTokens`, which asserted the stock rewrite,
to assert this contract: the payload, including a `null` limit, is returned
unchanged. It updates the four `use-max-completion-tokens=false` chat cases of
upstream's executor-level `TestOpenAICompatExecutor_MaxTokensNormalization`
and `TestOpenAICompatExecutor_MaxTokensNormalizationStream` the same way: the
request keeps `max_completion_tokens`, including `null`, and gains no
`max_tokens`. With all patches applied, `go test ./internal/runtime/executor/`
and `go test ./internal/runtime/executor/helps/` pass. The test changes do not
alter the engine binary.

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
probe answers, then narrowed to the entitled set. With both patches applied,
`go test -run Antigravity ./sdk/cliproxy/` passes.

Drop the patch once router-for-me/CLIProxyAPI#6531 is fixed upstream.

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
45.0 s. A live stream silent for 120 s completed on both.

Upstream: router-for-me/CLIProxyAPI#6535. Upstream made the health check an
opt-in setting, `streaming.http2-health-check-seconds`
(`requests.streaming.http2-health-check-seconds` in the v8 layout), which
defaults to 0, off. This patch stays unconditional. Once a tagged CPA release
that Avibe adopts contains that setting, drop this patch and render
`http2-health-check-seconds: 30` in `vibe/model_hub_runtime/config.py`
alongside the other fixed keys, in the layout that config uses.

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
its own HTTP/1.1 pool. The injected context RoundTripper is untouched, as in
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
- protocol selection is unchanged.

Upstream: the same failure mode as router-for-me/CLIProxyAPI#6535, whose
opt-in `streaming.http2-health-check-seconds` covers only the uTLS
connection. The follow-up proposes applying that setting to the standard and
proxy transports too. Drop this patch once a tagged CPA release Avibe adopts
applies the setting there, and render it in `vibe/model_hub_runtime/config.py`.

## `codex-sse-per-event-parsing.patch`

The Codex executor read upstream SSE line by line and took every `data:` line
as a whole event. In SSE an event's data may span several `data:` lines,
joined with `\n`. On such a split, lenient `gjson` read `"type":
"response.completed"` from the first fragment, the executor took its
terminal path, and the rest of the event was never read. The handler's
validator then failed the stream with `invalid SSE data JSON (len=<first
fragment>)`, the error Avibe saw from an API-key relay after a turn's full
output had streamed.

The patch reads the stream through `codexSSEScanner`, which keeps
`bufio.Scanner`'s interface. It is used by both `ExecuteStream` and the
non-stream `Execute`. Every event on this path is one JSON value, so:

- A data line that is complete JSON, or cannot start JSON (an empty value,
  `[DONE]`), is delivered byte-for-byte as before, with no lookahead. This
  keeps upstreams that send no blank line between events working as stock
  does; upstream tests cover that framing.
- A data line that starts an object or array it does not complete collects
  the following data lines until:
  - their joined value closes its outermost object or array, tracked
    incrementally so validation stays linear;
  - a blank line ends the event;
  - the stream ends;
  - a data line that is a complete object of its own starts the next event;
  - or the collected bytes would exceed the scanner's 50 MB line limit.
- A completed multi-line event is delivered compacted onto one line.
- One that never becomes valid JSON is delivered as the same multi-line data
  field, so it still fails downstream as before. One warning then records its
  framing: the data line count, each line's length and whether it is valid on
  its own, the joined length, and the JSON error offset. It records no data
  bytes.
- Field and comment lines inside an event follow its data line.
- An event left unterminated at the end of the stream is still delivered.

The patch adds `codex_sse_events.go` and `codex_sse_events_test.go`. They
cover:

- the minimal two-line repro and CRLF;
- blank-less framing, also after a split;
- invalid fragments, and the warning's contents;
- the 50 MB bound, and a 2,000-line event under a time bound;
- `[DONE]` and empty data lines;
- equivalence with `bufio.Scanner` on well-formed streams;
- the split event through `ExecuteStream`, with and without bootstrap
  buffering, and through `Execute`.

Upstream: the line-based parsing is unchanged on CPA `main` (v8.0.27) and
`dev`. Drop this patch once a tagged CPA release Avibe adopts parses Codex
SSE per event.

## Rebuild

The repository-owned release path checks out `source_sha`, applies the
recorded patches, builds every supported target with `CGO_ENABLED=0`,
packages deterministic archives, and verifies the generated bytes against the
checked-in manifest before publication:

```sh
python3 scripts/model_hub_engine_release_guard.py fetch-source \
  --output-dir /tmp/model-hub-engine-patched
```

The same command is available from the manual Model Hub release-guard
workflow. Its uploaded artifact is the source-built publication input; the
runtime manifest is updated only after the generated bytes and release assets
have been reviewed.
