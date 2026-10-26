# Avibe-owned CPA v8.0.30 compatibility patches

This directory records the source patches carried by Avibe's
`model-hub-engine-v8.0.30-*` builds: `-1` carries patches 1 to 9, and `-2`
adds patch 10, the Go module security refresh, built with Go 1.26.10. The
build applies them with `git apply --whitespace=error`, in the order the
manifest's `build.patches` records; each one applies on top of the ones before
it. The packaged manifest names the build Avibe installs.

Base:

- Repository: `router-for-me/CLIProxyAPI`
- Release: `v8.0.30`
- Source commit: `5be2cdab0869cbaa3d401891f740aebf7e15aa99`

Carried over from the v8.0.28 set: patches 1 to 5, byte-identical; they apply
to v8.0.30 with hunk offsets only. New in this set: patch 6, the xAI over-limit
item IDs; patches 7 and 8, private copies of router-for-me/CLIProxyAPI#6701
and #6700, carried until upstream releases them; and patch 9, upstream's own
fix a43026d7 from v8.0.33, backported. Patches 7, 8 and 9 are
`git format-patch` output; `git apply` reads them as the diffs they contain.

Each contract was re-measured on v8.0.30: the stock engine fails the check the
patch exists for, and the patched engine passes it.

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
sites are unchanged from v8.0.23 to v8.0.30; stock v8.0.30 still rewrites the
field (upstream's `TestNormalizeOpenAIMaxTokens` passes unpatched).

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

Measured on stock v8.0.30 with
`test_oauth_callback_forwarder_is_loopback_only_and_optional`
(`AVIBE_E2E_ISOLATED_NETWORK=1`): the Codex forwarder listens on `::`; with the
patch it listens on `127.0.0.1` and a taken Anthropic port is non-fatal.

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

Upstream closed router-for-me/CLIProxyAPI#6531 with v8.0.24 (091ff27f), which
persists the probe's entitlement hints in the auth file and seeds them at the
next start. That covers a restart after one successful probe only: a new login,
and every grant written by an older engine on its first start, still register
nothing until the probe answers, and a failed probe leaves them unroutable for
the backoff. Measured on stock v8.0.28, and again on stock v8.0.30, with a synthetic grant: `model_not_found`
from the first request and an empty listing, nothing at all while the probe
fails, and an immediate Source refresh refused. Keep this patch until upstream
registers a catalog while no hints exist; where persisted hints exist the patch
never runs, because `hints.ModelIDs` is set.

v8.0.29 (08e5762a) fences model registration with epochs; the static catalog
this patch registers goes through the same fenced path, and the probe still
narrows it.

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
45.0 s. A live stream silent for 120 s completed on both. Re-measured on v8.0.28
through the same kind of path: stock silent until the client's 120 s bound,
patched `request_timeout` at 45.06 s, and a live stream silent for 90 s
completed on both. Re-measured on v8.0.30: stock silent until the client's 120 s
bound, patched `request_timeout` at 45.02 s, a 90 s silent stream completed.
v8.0.29 (345cc9d5, b3dc6c3f) added an opt-in uTLS handshake timeout
(`tls-handshake-timeout-seconds`, default off); it bounds the handshake only,
not an established connection, so this patch stays.

Upstream declined the health check. router-for-me/CLIProxyAPI#6534 was
closed as not planned: by the project's design policy CPA sets no timeout
once an upstream connection is established, HTTP/2 health checks and stream
read deadlines included, not even as an opt-in setting, and leaves timeouts
and cancellation to the downstream client. The opt-in pull request,
router-for-me/CLIProxyAPI#6535, was closed unmerged. Avibe therefore carries
this patch by design as its client-side transport bound for this connection.
No drop is planned; re-evaluate only if upstream changes its post-connect
timeout policy. The cost is that every CPA upgrade must rebase this patch and
`fallback-transport-h2-ping-health-check.patch`. A dead path is this patch's
case; a live relay whose own upstream is stuck answers PINGs, so Avibe's
gateway bounds that case instead (the 120 s stream idle bound,
`docs/plans/model-hub-inference-deadlines.md`, "Stream idle bound", #2538).

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
Re-measured on v8.0.30: stock silent until 120 s on both paths; patched failed
at 45.02 s (`request_timeout`, `codex-api-key`) and 45.01 s (`http2: client
connection lost`, `openai-compatibility`); a 90 s silent stream completed.

Upstream: the same failure mode as router-for-me/CLIProxyAPI#6534, declined
under the same policy (see `codex-utls-h2-ping-health-check.patch`), so no
upstream follow-up is pending. Avibe carries this patch by design as its
client-side transport bound for these transports. No drop is planned;
re-evaluate only if upstream changes its post-connect timeout policy. Every
CPA upgrade must rebase it together with that patch. A live relay that answers
PINGs while its own upstream is stuck is bounded by Avibe's gateway instead
(the 120 s stream idle bound, `docs/plans/model-hub-inference-deadlines.md`,
"Stream idle bound", #2538).

## `codex-compat-upstream-item-ids.patch`

Avibe renders `is-compat: true` on the `codex-api-key` models of an xAI Source (#2620), so the codex
executor stops deleting xAI's envelope-less reasoning `encrypted_content` and `id` on replay. A second
rule still rewrote xAI's own item IDs. `SanitizeCodexInputItemIDs` enforces OpenAI's 64-rune input-ID
limit: it removes an encrypted reasoning item whose ID is longer, and shortens any other long ID to
47 runes plus a hash. xAI issues longer IDs. In the 2026-10-13 spike, tool-call reasoning `tco_…` and
custom tool calls `ctc_…` are 84 runes and share a `<turn>_<call>` suffix, and web search calls `ws_…`
are 83. Its own reasoning `rs_…` (39) and messages `msg_…` (40) are within the limit.

With `is-compat`, an input item ID over the limit is the upstream's own and is kept verbatim: it is
neither prefixed nor shortened, and an encrypted reasoning item carrying one is not removed.
- The rule is the ID's length, so it covers every item type.
- IDs within the limit are normalized exactly as before.
- Without `is-compat` nothing changes. That includes today's removal of over-limit encrypted
  reasoning and the shortening of every other over-limit ID.
- The HTTP executor gets the flag through `cacheHelperWithCompat`; the two WebSocket paths pass it
  directly.

Avibe renders `is-compat` for xAI Sources (#2767), so this patch changes the xAI replay wire: on stock
v8.0.30 under that config the 84-rune `tco_` reasoning is dropped whole and the `ctc_` ID is shortened
(`MH-ENGINE-HOSTED-007` is an expected failure); with the patch both arrive verbatim. For every other
Source the patch changes no byte.

Tests:
- `helps/codex_input_ids_test.go`: one subtest per item type (reasoning, custom_tool_call,
  web_search_call, function_call, message), each with an over-limit ID. With compat the item is kept
  byte for byte. Without compat the result equals `SanitizeCodexInputItemIDs`: encrypted reasoning
  removed, other IDs shortened to 64 runes.
- `codex_executor_signature_test.go`: `ExecuteStream` with a real 84-rune `tco_` reasoning item and
  its 84-rune `ctc_` custom tool call. An `is-compat` model sends both verbatim. A non-compat model
  strips the reasoning and shortens the call ID, as before.
- Mutation checks: disabling the verbatim rule fails both tests. Wiring `ExecuteStream` back to
  `cacheHelper` fails the executor test.

Drop the patch if xAI rejects verbatim long IDs in the live A/B, or once upstream exempts
compatibility models from the Codex ID limit.

## `config-locked-reads.patch`

A private copy of router-for-me/CLIProxyAPI#6701 (Avibe #2686), rebased on
v8.0.30. Avibe hot-reloads the engine with `PUT /v0/management/config.yaml`.
The engine rewrites `config.yaml` in place (the management write, and its own
rewrite of the management secret to bcrypt on every load), while its watcher,
loaders and management reads used a bare `os.ReadFile`, so a reader could see a
truncated or half-written file.

v8.0.30 (1631875b, #6631) already serializes reloads and records the hash of
the bytes each reload parsed. This patch carries only the torn-read fix that
release lacks: `config.ReadConfigFile` and `config.WriteConfigFile` hold one
RWMutex, through which the management write and its GET and v8 reads,
`SaveConfigPreserveComments*`, `PrepareConfigFileForUpload`, the `LoadConfig`
reads and cleanup write, the watcher's reload read, and the management
stale-snapshot check that 1631875b added all go. Writes stay in
place with `fsync`, so the inode of a single-file bind mount is kept. The
watcher is otherwise stock: Avibe writes `config.yaml` only before it starts
the engine and reloads through the management API, never by renaming the file
under a running engine, so the stock file watch is enough (upstream's watcher
work is in #6710, not carried).

Tests: `TestConfigFileReadsAndWritesExcludeEachOther` (a named pipe parks a
read or a write inside the file I/O, so mutual exclusion is checked without
timing), `TestConfigFileCallSitesHoldTheLock` (the same pipe parks each call
site, the watcher reload, the management GET, v8 read, stale-snapshot check
and write, inside its own file I/O while the test checks it holds the lock;
reverting any one to raw file I/O fails its subtest) and
`TestWriteConfigFileKeepsInode`. `fsync` has no unit test.

Drop when upstream merges #6701 and Avibe adopts a release that contains it.

## `claude-retry-headers-on-error-responses.patch`

A private copy of router-for-me/CLIProxyAPI#6700 (Avibe #2666) at its head
860a2f442, rebased on v8.0.30. When Anthropic refuses a request with a rate
limit, its response carries `Retry-After`, `Retry-After-Ms` and the
`Anthropic-Ratelimit-Unified-*` headers. Stock CPA drops them from the error it
returns, so a client cannot tell when to retry.

The Claude executor keeps those headers on its error, the fast-mode decode
path included. `executionErrorMessage`, the only producer of an error
message's headers, owns the policy for every consumer (HTTP writers, the
Responses WebSocket error event, plugin callbacks): it finds the header
carrier through every wrapper (`errors.As`), so retry and Home rounds no
longer lose them; it filters them with `FilterUpstreamHeaders`, as success
responses are; and when CPA has its own `Retry-After` (a Home round delay or
a local cooldown), that value replaces the upstream `Retry-After` and
`Retry-After-Ms`. The headers reach a client only with
`passthrough-headers: true`.

Tests: `TestClaudeExecutor_ErrorCarriesRetryHeaders`,
`TestClaudeExecutorFastHTTPDecodeErrorKeepsRetryHeaders`,
`TestExecutionErrorMessageFindsUpstreamHeadersThroughWrappers`,
`TestExecutionErrorMessageFiltersUpstreamHeadersLikeSuccess` and
`TestExecutionErrorMessageKeepsCPARetryTimingForEveryConsumer`; seven
mutants, all caught. Avibe's D6 (a 5xx's `Retry-After: 600` reaches the
Source cooldown through the real engine) fails on stock v8.0.30 and passes
with the patch.

Drop when upstream merges #6700 and Avibe adopts a release that contains it.

## `gemini-schema-content-keywords.patch`

Upstream's a43026d7 (router-for-me/CLIProxyAPI#6699, first in v8.0.33),
applied as published. Since v8.0.28 (a0fd8448) the Gemini and Antigravity
tool-schema cleaners keep a multi-branch `anyOf`/`oneOf` union instead of
collapsing it to one branch. A `contentMediaType` or `contentEncoding`
annotation that the collapse used to drop now reaches the backend, which
rejects the whole request with 400 `Unknown name "contentMediaType"`. Avibe's
Gemini subscription is an Antigravity grant, and MCP tool schemas carry these
annotations. The patch adds both keywords to the cleaners' unsupported list.

Upstream's `TestCleanJSONSchema_RemovesContentMediaTypeKeyword_Issue6699`
covers every cleaner (Gemini, Gemini JSON schema, Antigravity, its tool,
Claude-tool and response variants): on stock v8.0.30 each keeps both keywords
at the root, in `anyOf[1]` and in a property; with the patch they are
stripped, and properties named after the keywords survive. Dropping
`contentMediaType` from the list fails the test.

Drop at the next bump to v8.0.33 or later, which contains a43026d7.

## `go-module-security-refresh.patch`

Patch 10, new in `model-hub-engine-v8.0.30-2`. `govulncheck -mode binary`
reports eleven reachable advisories in the `-1` engine: GO-2026-6809 in the
standard library's `crypto/x509` (Go 1.26.9) and ten in Go modules. The
standard-library fix is the toolchain, Go 1.26.10, which the manifest records
as `build.go_version`. This patch moves the modules to their first fixed
releases:

| Module | v8.0.30 | Patched | Advisories |
| --- | --- | --- | --- |
| `golang.org/x/net` | v0.57.0 | v0.60.0 | GO-2026-6603, -6610, -6611, -6612, -6617 (HTTP/2) |
| `golang.org/x/crypto` | v0.54.0 | v0.57.0 | GO-2026-6354, -6355 (ssh) |
| `golang.org/x/text` | v0.40.0 | v0.42.0 | GO-2026-6629 |
| `github.com/go-git/go-git/v6` | v6.0.0-alpha.4.0.20260520124234-0860a7d8a164 | v6.0.0-alpha.5 | GO-2026-6213, -6214 |

Only `x/net` v0.60.0 and `go-git` v6.0.0-alpha.5 are requested. Minimal
version selection raises the rest: `x/net` v0.60.0 requires `x/crypto`
v0.57.0, `x/text` v0.42.0 and `x/sys` v0.48.0, `x/text` v0.42.0 requires
`x/sync` v0.23.0, and `go-git` v6.0.0-alpha.5 requires `go-billy`
v6.0.0-alpha.2. `go mod tidy` then removes the go.sum lines of the replaced
versions; no other requirement changes. Upstream v8.0.38, the latest release
when this patch was made, and upstream `main` still require the vulnerable
versions.

`go-git` v6.0.0-alpha.5 rewrites pack file access (`internal/packhandle`,
`x/fdpool`), and a repository handle can keep a pack open after it is deleted.
Upstream's `corruptGitRepository` test helper removed the packs and then
checked the corruption through the handle that had just repacked them, which
still reads them. The patch makes the helper check through a new handle, as
the store's own operations open one. With that change, every
`internal/store` test passes unchanged on v6.0.0-alpha.5, and the helper still
fails when the packs are not removed. The test change does not alter the
engine binary. `internal/store`'s git token store is the engine's only
`go-git` user. Avibe never enables it: it needs `GITSTORE_GIT_URL`, which
the engine's allow-listed environment (`vibe/model_hub_runtime/environment.py`)
does not pass and no `.env` in the engine's Avibe-owned working directory sets.

Measured with Go 1.26.10. With all ten patches applied, `go test ./...`
passes: 102 packages, as on the unrefreshed tree. Patches 4 and 5's HTTP/2
health-check tests also pass on `x/net` v0.60.0. Two candidate builds from
empty Go caches were byte-identical. `govulncheck -mode binary` reports no
reachable advisory on any of the six binaries. It still lists two advisories
it does not find called: GO-2026-5841 (`klauspost/compress/s2`, imported but
not called) and GO-2026-5932 (`x/crypto/openpgp`, deprecated with no fix).

Drop it once a CPA release that Avibe adopts requires these versions or later.
At every bump, rebase it onto the new `go.mod` and keep only the lines upstream
does not already cover, then re-run `govulncheck -mode binary` on the
candidate.

## Known risk: model registration superseded mid-flight (#6718)

v8.0.29 (08e5762a) fences model registration with epochs. A registration that
is superseded mid-flight by another update of the same auth's attributes, where
that update does not register models itself, leaves the auth with no models
(router-for-me/CLIProxyAPI#6718; reproduced with a synthetic openai-compat
auth). Avibe does not take that path: its prefix PATCH re-registers
synchronously, and config reloads and watcher events each register. Upstream
fixed it in v8.0.33 (f75ea073), a +556/-60 change that does not apply to this
tree; the next bump resolves it. Revisit if Model Hub reports models vanishing
right after a Source edit or an OAuth login.

## Dropped earlier: `codex-sse-per-event-parsing.patch`

Dropped with the v8.0.28 set: upstream's `CodexSSEScanner` (7795de19, v8.0.28)
covers it, and v8.0.30 does not change it.

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
