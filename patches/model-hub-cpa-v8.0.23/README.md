# Avibe-owned CPA v8.0.23 compatibility patches

This directory records the source patches carried by Avibe's
`model-hub-engine-v8.0.23-2` build. Each patch applies independently to the
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
unchanged. With all patches applied,
`go test ./internal/runtime/executor/helps/` passes. The test change does not
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
