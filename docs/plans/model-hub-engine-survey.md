# Model Hub Engine Survey: CLIProxyAPI and Managed Runtime Reuse

Status: S1 + S3 implementation spike, verified 2026-07-23

## Scope and evidence boundary

This survey evaluates CLIProxyAPI (CPA) as the first replaceable Model Hub
data-plane engine and audits Avibe's Show Runtime installer for reuse. It uses
the signed product spec at `docs/plans/model-hub.md` (SHA256
`0bf53910fd14c4afb8c18a10d0a8d5f5bb43c64516bcb81a8c0fcaf4743d13b8`) and
the implementation plan at `docs/plans/model-hub-implementation.md` (SHA256
`35f336d5d0b063e000dbe5dbf16e752d997ccf80dec03873a76ed7ec018b93d5`).
Those two inputs were untracked in the source checkout at survey time, so their
content hashes, rather than an Avibe commit, identify the reviewed snapshots.

CPA was cloned at release tag `v7.2.95`, exact commit
[`f71ec0eb6776854457892452cf28c47f0d658251`](https://github.com/router-for-me/CLIProxyAPI/commit/f71ec0eb6776854457892452cf28c47f0d658251).
All CPA source links below are pinned to that commit. The Show Runtime audit is
pinned to Avibe commit
[`e71890a78209f13ae8579d25800282e40469df38`](https://github.com/avibe-bot/avibe/commit/e71890a78209f13ae8579d25800282e40469df38).
The branch was fast-forwarded to `origin/master` commit
`8f9268c6afdcfa47658dced2194b9e7e2092902e` before commit; that intervening
change only touched an unrelated Show annotation plan.

No real vendor OAuth login or billable inference request was performed because
the spike had no credentials and must not create or expose them. OAuth and
upstream behavior claims are therefore source-verified, not live-account-
verified. Asset hashes, binary versions, archive layouts, targeted source tests,
and Avibe installer tests were verified locally as described in Verification.

## Executive decision

CPA is viable as a protocol and credential engine, but its built-in routing must
not be the Model Hub policy authority. CPA automatically advances to another
eligible credential for more error classes than the signed spec permits, and it
does not expose a safe, first-class selection/switch event stream. The Avibe
adapter should own mapping, ordered candidate selection, retry taxonomy, and the
resolution-event log. It can isolate one source per CPA call by assigning every
source a unique `prefix`, enabling `force-model-prefix`, and addressing the
candidate as `<private-prefix>/<model>`. CPA then owns credential refresh,
protocol translation, upstream execution, and per-source cooldown signals.

For installation, reuse the Show Runtime manifest/download/verification design
after extracting a generic managed-archive installer. Do not copy the existing
Show-specific class and do not make the engine a special case inside it. This is
a medium L1 change.

## S1. CLIProxyAPI capability re-verification

### 1. Official repository and pinned release

The repository is
[`router-for-me/CLIProxyAPI`](https://github.com/router-for-me/CLIProxyAPI).
Its Go module is `github.com/router-for-me/CLIProxyAPI/v7`, and its current
release series is v7.x, matching the project named in the prior survey. At
During the 2026-07-23 03:05-04:00 UTC+08 verification window, the latest
non-draft release was
[`v7.2.95`](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.95),
published 2026-07-22 14:47:25 UTC. The tag resolves to source commit
`f71ec0eb6776854457892452cf28c47f0d658251`. Sources:
[`go.mod`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/go.mod),
[`v7.2.95` release](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.95).

Pinned Model Hub L1 assets:

| Platform | Release asset | Download bytes | MiB | Extracted executable bytes | SHA256 |
| --- | --- | ---: | ---: | ---: | --- |
| macOS ARM64 | [`CLIProxyAPI_7.2.95_darwin_aarch64.tar.gz`](https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/CLIProxyAPI_7.2.95_darwin_aarch64.tar.gz) | 14,384,655 | 13.72 | 43,626,210 | `c7ccc28b7db5d1799999a9e22725ccc6bd0e36d9aa023da6b52b7c1a71aad978` |
| Linux AMD64 | [`CLIProxyAPI_7.2.95_linux_amd64.tar.gz`](https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/CLIProxyAPI_7.2.95_linux_amd64.tar.gz) | 15,401,775 | 14.69 | 46,901,896 | `826604e2dbf11913b0f373047f7bca1829eb2bab8a45d3a1916cc2534c7a9fd5` |

Both archives were downloaded from the release, hashed locally, and matched the
release's [`checksums.txt`](https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.95/checksums.txt).
The release also publishes Darwin AMD64, Linux ARM64, Windows ARM64/AMD64,
FreeBSD variants, and Linux `no-plugin` variants. Those additional assets were
not downloaded or independently hashed in this spike and are not part of this
pin.

### 2. OAuth vendors and connect-dialog forms

The built-in authentication manager registers Codex, Claude, Antigravity, Kimi,
and xAI. Vertex is a service-account import, not OAuth. The binary also parses
existing `gemini-cli` auth files, but it has no built-in Gemini login command or
Management API OAuth-start endpoint at this tag. Plugins can add providers, so
the contract must remain extensible. Sources:
[`internal/cmd/auth_manager.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/cmd/auth_manager.go),
[`cmd/server/main.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/cmd/server/main.go#L84-L105),
[`sdk/auth/filestore.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/filestore.go#L210-L265).

Form mapping is based on the actual Management API path that Avibe can call,
not on a human parsing the standalone CLI's stdout:

| Vendor / provider | Current flow shape | Spec form | Decision |
| --- | --- | --- | --- |
| Claude (`anthropic` session, `claude` auth) | PKCE URL redirects to `localhost:54545`; Management API callback replay accepts a full redirect URL or tracked `state` + pasted `code` | **C** natively; A can be adapter-compatible | The signed spec says A. Prefer C in the contract/UI; accepting a raw code in the same submit endpoint is a compatibility enhancement, not the primary flow. |
| Codex / ChatGPT (`codex`) | Management API uses PKCE localhost callback on port 1455 | **C** | The signed spec says B. A separate `-codex-device-login` CLI mode is B, but no Management API endpoint exposes that device flow. Do not parse CLI stdout in L1. |
| Google Antigravity (`antigravity`) | Browser OAuth redirects to `localhost:51121/oauth-callback`; callback replay is supported | **C** | This is the current built-in Google subscription path and supplies Gemini-family models. |
| Kimi (`kimi`) | Device authorization returns URL/user code and background polling self-completes | **B** | Direct fit. |
| xAI / Grok (`xai`) | Device authorization returns URL/user code and background polling self-completes | **B** | Direct fit. |
| Gemini CLI (`gemini-cli`) | Existing auth-file parsing only; no built-in start flow | **None** | Import/reuse may be possible, but acquisition is not implemented by CPA. Do not advertise it as a CPA connect flow. |
| Vertex (`vertex`) | Service-account JSON import | **None** | Model this as controlled credential import, not subscription OAuth. |

The common callback endpoint accepts `redirect_url` or `code` + `state`, which
is why Claude can tolerate a Form A-style raw-code submission even though its
native redirect flow is Form C. Kimi and xAI start responses explicitly return
`flow: "device"`; Codex device mode exists only in the standalone authenticator.
Sources:
[`Management OAuth handlers`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/auth_files.go#L1971-L2665),
[`oauth_callback.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/oauth_callback.go),
[`sdk/auth/claude.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/claude.go#L35-L205),
[`sdk/auth/codex.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/codex.go#L35-L220),
[`sdk/auth/codex_device.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/codex_device.go),
[`sdk/auth/kimi.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/kimi.go),
[`sdk/auth/xai.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/xai.go).

Live vendor consent screens, entitlement restrictions, and end-to-end token
exchange were not verified. S2 must still gate product defaults.

### 3. API-key upstreams and protocol conversion

CPA has first-class config sections for Gemini, Google Interactions, Claude,
Codex, xAI, Vertex-compatible, and named OpenAI-compatible providers. Each entry
can carry an API key, base URL where applicable, model list/aliases, excluded
models, custom headers, proxy, prefix, and priority according to provider type.
Sources:
[`config.example.yaml`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/config.example.yaml#L208-L405),
[`internal/config/config.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/config/config.go#L448-L688),
[`internal/config/vertex_compat.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/config/vertex_compat.go).

The relevant client-to-upstream conversion matrix is:

| Client request protocol | Anthropic Messages upstream | OpenAI Responses/Codex upstream | Gemini/Antigravity upstream | Generic OpenAI-compatible upstream |
| --- | --- | --- | --- | --- |
| Anthropic Messages | Native | Yes | Yes | Yes, converted to Chat Completions |
| OpenAI Chat Completions | Yes | Yes | Yes | Native |
| OpenAI Responses | Yes | Native for Codex/xAI | Yes | Yes, normally converted to Chat Completions |

CPA additionally translates Google Interactions and Gemini client forms. The
generic OpenAI-compatible executor targets `/chat/completions`; only the
non-streaming `responses/compact` alternate path targets `/responses/compact`.
The registry contains both streaming and non-streaming transforms for the
matrix above. Sources:
[`internal/translator/init.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/translator/init.go),
[`OpenAI Responses -> Claude registration`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/translator/claude/openai/responses/init.go),
[`Claude -> OpenAI registration`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/translator/openai/claude/init.go),
[`openai_compat_executor.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/runtime/executor/openai_compat_executor.go#L82-L160).

Streaming caveats:

- Generic OpenAI-compatible streaming expects SSE `data:` lines. A non-SSE
  error/body after a nominal stream start becomes a 502. The executor requests
  `stream_options.include_usage=true` and synthesizes `[DONE]` if a clean
  upstream close omits it.
- CPA buffers through the first meaningful stream payload. Before that payload,
  refresh/fallback can occur. After it returns the wrapped stream to the HTTP
  handler, later chunk errors are forwarded and are not transparently replayed,
  matching the signed no-retry-after-stream-start rule.
- Syntax conversion is implemented and heavily tested, but thinking,
  prompt-cache, tool, image/audio, service-tier, and provider-specific semantics
  are not capability-equivalence guarantees. Model Hub must retain the visible
  mapping warning from the spec.

Sources:
[`openai_compat_executor.go` stream path](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/runtime/executor/openai_compat_executor.go#L314-L460),
[`conductor.go` stream bootstrap](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L1840-L1965).

### 4. Model listing and discovery

CPA exposes a merged effective catalog at `GET /v1/models` and a Gemini-shaped
catalog at `GET /v1beta/models`. The Management API also exposes
`GET /auth-files/models?name=<auth-file-or-id>`, which reads models registered
for that specific auth, plus `GET /model-definitions/:channel` for static model
metadata. These are sufficient for an effective per-source supply list and the
"found N models" result after an OAuth auth record is registered. Sources:
[`internal/api/server.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/server.go#L510-L575),
[`GetAuthFileModels`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/auth_files.go#L398-L445),
[`model_definitions.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/model_definitions.go).

This is not a uniform live entitlement-discovery API. OAuth providers often
register a built-in or remotely maintained catalog; API-key and generic
OpenAI-compatible entries normally declare their `models` in configuration.
The broad `POST /v0/management/api-call` helper can attach an auth token and
call an arbitrary upstream `/models`-like URL, but it has no vendor-neutral
schema or host allowlist. Avibe must not expose that generic primitive to the UI.
For API-key source tests, L2 should implement vendor-specific, allowlisted
discovery adapters and record `declared`, `effective`, or `live` provenance and
observation time in the source model. Source:
[`api_tools.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/api_tools.go#L54-L175).

Actual account-specific model entitlement was not live-verified.

### 5. Credential layout, schema, relocation, and refresh

`auth-dir` defaults to `~/.cli-proxy-api`, supports `~`, and can be set to a
different absolute or relative path. Avibe must always pass a dedicated engine
credential directory under its own runtime/state root; it must never depend on
or mutate the user's default CPA directory. The file store scans JSON records
and uses the top-level `type` to select a provider. Sources:
[`config.example.yaml`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/config.example.yaml#L41-L48),
[`internal/util/util.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/util/util.go),
[`sdk/auth/filestore.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/filestore.go).

Current built-in persisted schemas, omitting optional adapter metadata such as
`disabled`, `priority`, `prefix`, `note`, headers, proxy, and cloak fields:

| Provider | Top-level credential fields |
| --- | --- |
| Claude | `type`, `id_token`, `access_token`, `refresh_token`, `last_refresh`, `email`, `expired` |
| Codex | Claude fields plus `account_id` |
| Antigravity | `type`, `access_token`, `refresh_token`, `expires_in`, `timestamp`, `expired`, optional `email`, `project_id` |
| Kimi | `type`, `access_token`, `refresh_token`, `token_type`, `scope`, `device_id`, `expired` |
| xAI | `type`, `access_token`, `refresh_token`, optional `id_token`, expiry/refresh fields, identity, base/token endpoints, `auth_kind` |
| Vertex import | `type`, embedded `service_account`, `project_id`, `email`, optional `location`, `prefix` |

Sources:
[`Claude token`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/auth/claude/token.go),
[`Codex token`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/auth/codex/token.go),
[`Antigravity auth`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/antigravity.go#L180-L215),
[`Kimi token`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/auth/kimi/token.go),
[`xAI token`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/auth/xai/token.go),
[`Vertex credential`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/auth/vertex/vertex_credentials.go).

CPA runs a background refresh scheduler (default check interval 5 seconds,
default concurrency 16) and persists refreshed credentials. Provider lead times
at this tag are Claude 4 hours, Codex 5 days, Antigravity 5 minutes, Kimi 5
minutes, and xAI 5 minutes. A request that receives 401 also attempts one
refresh-and-retry before normal error handling. Sources:
[`conductor.go` refresh constants](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L74-L90),
[`StartAutoRefresh`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L5820-L5850),
[`refresh registry`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/refresh_registry.go),
[`unauthorized stream path`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L1860-L1920).

Security gap: built-in provider `SaveTokenToFile` methods create the parent with
0700 but use `os.Create` for token JSON, so a new file's mode follows the process
umask rather than being explicitly fixed to 0600. The generic metadata-only
path does use 0600. L1 must create the engine directory as 0700 and atomically
enforce 0600 on every credential file after create/import/refresh, with a startup
audit that fails closed on broader permissions. Sources:
[`Claude SaveTokenToFile`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/auth/claude/token.go#L50-L93),
[`FileTokenStore`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/auth/filestore.go#L75-L150).

### 6. Management API, live configuration, and state

The Management API is enabled only when a management secret exists. Every
request, including loopback, requires `Authorization: Bearer <key>` or
`X-Management-Key`. Non-loopback access additionally requires remote management
permission. `MANAGEMENT_PASSWORD` acts as a runtime secret and also overrides
the remote-management gate, so Avibe must bind CPA explicitly to `127.0.0.1`
and never rely on the gate alone. Sources:
[`handler.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/handler.go#L65-L90),
[`management middleware`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/handler.go#L262-L315),
[`config.example.yaml`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/config.example.yaml#L1-L39).

The registered surface includes:

- whole-config read/write; debug/logging/retry/cooldown/routing/quota settings;
- proxy client API keys and API-key upstream CRUD for every built-in type;
- OpenAI-compatible providers, model aliases/exclusions, and model definitions;
- auth-file list, model list, upload, delete, disable, metadata-field patch, and
  Vertex import;
- OAuth start/status/cancel and callback replay;
- logs/request logs, usage queue/API-key usage, generic authenticated upstream
  calls, and plugin/store management.

Source: [`registerManagementRoutes`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/server.go#L850-L1028).

Management mutations save YAML and trigger an asynchronous config reload. The
reload path updates clients, retry/cooldown/routing, aliases, executors, plugins,
and config-derived auths without a process restart. Source inspection does not
show listener host/port/TLS or the watched auth-directory root being rebound;
L1 should treat those as startup-owned even though they are fields in the YAML.
That startup-only conclusion was not live-mutated in this spike. Sources:
[`persistLocked`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/handler.go#L395-L425),
[`service config reload`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/service.go#L1300-L1370).

`GET /auth-files` exposes useful auth-level projection fields: provider/type,
status/message, disabled/unavailable, success/failure counters, recent request
summary, priority, last refresh, and aggregate `next_retry_after`. It does not
include the internal per-model `ModelStates`. If `save-cooldown-status` is
enabled, CPA writes per-auth `.cds` files containing model, status, reason,
quota, last error, and retry time, but there is no Management API endpoint for
those files. Sources:
[`buildAuthFileEntry`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/auth_files.go#L520-L650),
[`cooldown_state.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/cooldown_state.go).

CPA multiplexes an authenticated Redis RESP protocol on the same listener.
`SUBSCRIBE errors` yields per-failure auth/model cooldown snapshots, and
`SUBSCRIBE usage` yields per-attempt usage when usage statistics are enabled.
There is still no first-class "source selected" or "source changed" event.
Worse, the usage payload includes the inbound proxy `api_key` verbatim. Model
Hub must not enable or consume that feed until CPA removes that field or L1
ships a reviewed redaction patch. The error feed is useful only after the
adapter strips bodies and projects an allowlisted reason/status shape. Sources:
[`redis_queue_protocol.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/redis_queue_protocol.go),
[`error_events.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/error_events.go),
[`usage queue payload`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/redisqueue/plugin.go#L20-L145).

### 7. Routing, fallback, and plugins

CPA supports `round-robin` and `fill-first`. All eligible auths are first
partitioned by integer `priority`, and the highest integer wins. `fill-first`
then chooses deterministically; equal-priority auths are ordered by auth ID,
not config-list order. Assigning unique descending priorities can project the
global list, but priority alone does not fix the error taxonomy below. Sources:
[`config.example.yaml` routing](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/config.example.yaml#L108-L188),
[`selector.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/selector.go#L190-L305).

The blocking mismatch is automatic failover. In one execution CPA keeps picking
untried eligible credentials until one succeeds or all are exhausted. It stops
early only for its request-invalid heuristics: selected 400 error strings,
request-scoped 404, 422, and a narrow 500 case. A failed 401 after refresh,
402/403, ordinary 404, many 400s, and other errors can move to the next source.
Its state machine also assigns 30-minute cooldowns to 401 and 402/403 and 12
hours to 404. This is broader than the signed spec, which allows fallback only
for explicit quota/429, transient 5xx, and network failure, and requires
parameter/protocol/tool errors to surface. Sources:
[`executeMixedOnce`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L2465-L2660),
[`isRequestInvalidError`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L4525-L4575),
[`MarkResult`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L3710-L3870).

The clean compensation is adapter-owned selection, not an expanding set of CPA
error overrides. Every source receives a stable private prefix; L1 sets
`force-model-prefix: true`, and the adapter sends a single candidate-specific
model name per attempt. Prefixes apply to both OAuth auth files and API-key
config auths, are removable before upstream execution, and can be updated for
OAuth files through the metadata-field API. The adapter can then apply the
signed taxonomy, know the exact attempted/selected source, and write the event
log without observing CPA secrets. Generated config must also avoid repeated
OpenAI-compatible aliases that create an internal upstream-model pool; one
private candidate name must resolve to one Model Hub source. Sources:
[`applyModelPrefixes`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/service.go#L2415-L2460),
[`rewriteModelForAuth`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/sdk/cliproxy/auth/conductor.go#L3175-L3210),
[`PatchAuthFileFields`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/api/handlers/management/auth_files.go#L1486-L1720).

CPA's plugin system is real but is not a declarative Model Hub fallback product:

- trusted in-process dynamic-library plugins are globally disabled by default;
- a `ModelRouter` can target a built-in provider or plugin executor and is
  ordered by plugin priority;
- a separate Scheduler capability can pick an auth ID or delegate to built-in
  fill-first/round-robin;
- examples and tests exist, but the only model-router example is specialized
  Claude web-search routing, and the scheduler is an example plugin;
- there is no shipped YAML cross-vendor fallback rule engine or native
  default-off cross-vendor policy flag.

Do not make plugins an L1 dependency. Enforce the experimental cross-vendor flag
when the adapter constructs candidates. Keep CPA plugins disabled for the
initial runtime. Sources:
[`config.example.yaml` plugins](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/config.example.yaml#L49-L80),
[`model_router.go`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/pluginhost/model_router.go),
[`plugin examples`](https://github.com/router-for-me/CLIProxyAPI/tree/f71ec0eb6776854457892452cf28c47f0d658251/examples/plugin),
[`scheduler example`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/examples/plugin/scheduler/README.md).

### 8. License, cadence, and executable posture

CPA is MIT licensed. The standard release archive contains one executable plus
LICENSE, README/README_CN, and `config.example.yaml`; no language runtime or
sidecar daemon is required. The standard binaries include dynamic-plugin
support. The inspected macOS ARM64 executable is Mach-O ARM64 and links only to
macOS system libraries/frameworks. The Linux AMD64 executable is a stripped
ELF, dynamically linked to glibc system libraries (`libdl`, `libresolv`,
`libpthread`, `libc`), so "single binary" does not mean a fully static Linux
artifact. Sources:
[`LICENSE`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/LICENSE),
[`release assets`](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.95),
[`plugin support`](https://github.com/router-for-me/CLIProxyAPI/blob/f71ec0eb6776854457892452cf28c47f0d658251/internal/pluginhost/support.go).

Release cadence is unusually high: the latest 30 non-draft releases at survey
time span v7.2.66 through v7.2.95 in about 11.4 days, often multiple releases
per day. That reinforces immutable version + SHA pinning and argues against
"latest" resolution at runtime. Maintainer signing, artifact notarization, and
whether GitHub release assets are operationally immutable were not verified;
SHA mismatch must fail closed regardless.

### 9. Risks and adapter compensations

| Priority | Gap against the signed spec | Compensation / owner |
| --- | --- | --- |
| P0 | CPA performs broader credential/source fallback than the spec's no-blind-fallback taxonomy. | L1/L2 adapter owns candidate loop. Use a unique forced prefix per source so each CPA call can hit only that source. Retry only quota/429, transient 5xx, or network failures; refresh+retry 401 once; stop after first emitted stream payload. |
| P0 | No safe selection/switch event. Error RESP has failures only; usage feed exposes the inbound proxy API key and is unacceptable. | Adapter writes `resolution-event` from its own candidate loop. Keep usage statistics/feed disabled. Optionally consume the error RESP feed through an allowlist projector that drops body/IDs not required by the contract. |
| P1 | OAuth form assumptions in the spec do not match CPA Management API: Claude is naturally C, and Codex B exists only as a CLI command. | Make OAuth flow form runtime-declared. Ship Claude and Codex as C for this engine; accept raw Claude code as a compatibility input; add B for Codex only after a Management API device-flow endpoint exists. |
| P1 | Per-source model listing is effective catalog data, not uniformly live entitlement discovery; generic OpenAI-compatible discovery is manual. | Store model provenance (`declared`, `effective`, `live`) and observation time. Implement allowlisted vendor discovery adapters; never expose generic `/api-call`. |
| P1 | `/auth-files` omits model-level cooldown state; `.cds` has it but has no API. | Project auth-level chips from `/auth-files`; consume sanitized error events for model-level changes; keep adapter state authoritative for current candidate/recovery events. Do not read undocumented `.cds` files as a contract. |
| P1 | Token JSON file modes depend on umask in provider save paths. | Dedicated 0700 directory; atomic 0600 enforcement after every write/import/refresh; startup permission audit and fail closed. |
| P1 | Management API includes high-risk primitives and credential-bearing endpoints. | Bind loopback, generate a runtime-only management key, implement a narrow internal client, never proxy Management API routes to browser/IM, and allowlist response fields. |
| P2 | Higher integer is higher priority; equal-priority fill-first uses auth ID order. | Adapter remains source of truth; if priorities are projected into CPA, assign unique descending integers and never rely on ties. |
| P2 | Cross-vendor fallback has no native default-off policy control. | Candidate construction enforces the flag and tags adapter-generated events. Leave CPA plugins disabled. |
| P2 | Very fast upstream release cadence and no verified signing/notarization policy. | Pin tag, commit, URL, byte size, SHA256, and license in Avibe's dependency manifest; upgrade only through reviewed PR + contract/scenario tests. |

## S3. Managed runtime-dependency reuse audit

### 10. What Show Runtime currently provides

The current implementation is `core/show_runtime.py`, not a
`vibe/show_runtime/` package. Its official `manifest-cache` path provides:

- schema v1 with `runtime_version`, `runtime_source`, `minimum_node`, and an
  `archives` map keyed by six Avibe platform tags; each archive has `name`,
  immutable URL, SHA256, and byte size;
- a packaged manifest embedded in the Avibe wheel; HTTPS/file downloads;
  size + SHA256 verification before cache promotion; safe tar extraction;
- content-addressed downloads at
  `${AVIBE_HOME:-~/.avibe}/runtime/show-runtime/downloads/<sha256>.tgz`;
- installs at
  `.../versions/<runtime-version>/<platform>/<manifest+archive fingerprint>/`,
  per-install `.vibe-show-runtime.json`, and `current.json`;
- verified-cache offline mode, status/probe diagnostics, a clean command, and
  retention of the current install plus one prior packaged-manifest install;
- `vibe runtime prepare --force`, which re-extracts/reinstalls even when install
  metadata matches, while still reusing a verified download cache;
- failure fallback to an already verified install where possible.

Sources:
[`core/show_runtime.py`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/core/show_runtime.py#L35-L970),
[`config/paths.py`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/config/paths.py#L1-L125),
[`generate_show_runtime_manifest.py`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/scripts/generate_show_runtime_manifest.py),
[`runtime CLI`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/vibe/cli.py#L11190-L11360).

The current parser validates schema version, runtime version, and the existence
of archives, but it does not retain or validate the generated `runtime_source`
object. The generic dependency schema must make upstream repository, source
commit, release tag, and license first-class verified metadata rather than an
ignored informational field. Source:
[`_load_runtime_manifest`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/core/show_runtime.py#L768-L835).

The release workflow resolves an exact Show Runtime commit, builds all six
platform archives, generates the manifest, embeds only the manifest in the
wheel, uploads archives beside the Avibe release, and refuses to overwrite a
same-tag runtime asset unless it is byte-identical. Source:
[`publish.yml`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/.github/workflows/publish.yml#L45-L310).

### 11. Reusable core and concrete coupling

The following behavior should be reused: manifest parsing, platform selection,
immutable URL/size/SHA checks, content-addressed cache, HTTPS/file download,
offline semantics, safe extraction, versioned install metadata/current pointer,
rollback retention, status/probe output, and force/clean lifecycle.

It cannot be reused by constructing `ShowRuntimeManager` with different values.
Concrete coupling points are:

1. The manager mixes installation with Show server start/health/request/prewarm,
   Node command discovery, workspace/cache flags, logs, and orphan-process
   sweeping.
2. Archive command resolution is fixed to
   `node_modules/@avibe/show-runtime/dist/cli.js` and requires the manifest's
   `minimum_node`; CPA needs a native executable locator and executable-mode
   check, not Node.
3. Constants, environment variables, metadata filename, diagnostic reasons,
   user agent, log messages, and cleanup patterns are all Show-named.
4. The extractor supports `.tar.gz` only. Current CPA target assets are tar.gz,
   but a future Windows target would require zip support.
5. Platform names and archive names differ (`darwin-arm64` vs
   `darwin_aarch64`, `linux-x64` vs `linux_amd64`), so manifest generation must
   accept an explicit mapping rather than infer only from a Show prefix.
6. `vibe runtime status/prepare/clean`, printed output, post-upgrade prepare,
   Doctor repair, and strict-success logic address Show and other dependencies
   through hand-written branches rather than a dependency registry.
7. The release workflow assumes six Show archives built from Show Runtime main;
   CPA should consume or mirror a reviewed upstream release, not run CPA's build
   as if it were first-party source.
8. Most installer regressions live inside `tests/test_ui_show_pages.py`, so the
   reusable cases must move to focused generic installer tests before adding
   engine-specific tests.

Sources:
[`core/show_runtime.py`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/core/show_runtime.py),
[`runtime CLI`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/vibe/cli.py#L11190-L11595),
[`Show manifest tests`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/tests/test_ui_show_pages.py#L3330-L4195),
[`manifest generator tests`](https://github.com/avibe-bot/avibe/blob/e71890a78209f13ae8579d25800282e40469df38/tests/test_show_runtime_manifest.py).

### 12. Recommendation for L1

**Yes, reuse the machinery by extraction; effort M (roughly 3-5 engineer-days
including workflow and focused tests).**

Extract a small generic component, for example
`core/managed_archive_dependency.py`, parameterized by:

- dependency ID, runtime root, manifest resource, supported schema/platform map;
- archive type and executable locator/validator;
- optional prerequisite validator (Node for Show, none for CPA);
- metadata/error namespace and rollback count;
- download user agent and optional post-install permissions hook.

Keep `ShowRuntimeManager` responsible for Show process behavior and make a new
Model Hub engine manager responsible for CPA config generation, loopback port,
management key injection, credential root, process supervision, health, and
shutdown. Both compose the generic installer. Extend runtime
status/prepare/clean through a dependency registry rather than adding another
top-level special-case branch.

For the engine manifest, pin at least dependency ID, schema version, upstream
repo, release tag, source commit, license, archive URL, platform, byte size, and
SHA256. The release workflow should either mirror the already-hashed upstream
assets into the Avibe release with the existing immutability gate or retain the
upstream immutable URL plus checksum; mirroring is preferred for availability
and release provenance. Never resolve CPA `latest` on the user's machine.

## Contract changes required before implementation lanes open

1. **`oauth-flow.schema.json`:** separate engine flow capability from UI
   presentation. Add `provider`, `engine_provider`, `flow_kind`
   (`pkce_callback`, `device_code`, `credential_import`), `submission`
   (`redirect_url`, `code`, or both), tracked opaque `state`, callback timeout,
   and extensible provider metadata. Freeze CPA v7.2.95 mappings as Claude C
   (raw-code compatible), Codex C, Antigravity C, Kimi B, xAI B; Gemini CLI and
   Vertex are not A/B/C connect flows. Keep product vendor identity distinct
   from the engine provider key, especially Gemini supply through CPA's
   `antigravity` provider.
2. **`resolution-event.schema.json`:** make this adapter-owned. Add
   `request_id`, `requested_model`, `resolved_model`, ordered `attempts[]`,
   final/selected source, precise reason taxonomy, `stream_committed`, recovery
   marker, and `cross_vendor`. Explicitly forbid tokens, API keys, auth-file
   paths, raw upstream bodies, and engine management identifiers not needed by
   the UI.
3. **`source.schema.json`:** split `models[]` into a projection that records
   provenance (`declared|effective|live`) and `observed_at`; represent auth-level
   and optional model-level cooldown separately. Add an opaque adapter-owned
   engine binding (`provider`, `auth_index` or stable equivalent) that is never
   returned by public/UI APIs.
4. **`priority.schema.json`:** keep the ordered source-ID list authoritative.
   State that CPA integer priority is only a projection and ties are forbidden.
   Candidate ordering and retry decisions remain adapter behavior.
5. **`api.md`:** specify that source test/discovery is vendor-allowlisted, that
   Management API is never a browser passthrough, and that submit accepts either
   a callback URL or raw code when the declared flow allows it.
6. **Add a managed dependency contract:** freeze the engine manifest/status
   payload (version, platform, asset SHA, install state, health, last error) so
   L1 and L2 do not couple directly to Show Runtime types or CPA's config schema.

The contracts should be committed before L1/L2/L4 start. At survey time the
signed spec and implementation plan were not present in `origin/master`; their
snapshot hashes are recorded at the top of this document, but a hash in a survey
is not a substitute for versioned contract files.

## Verification

Performed against the pinned sources and assets:

- `git show-ref --verify refs/tags/v7.2.95` resolved to
  `f71ec0eb6776854457892452cf28c47f0d658251`.
- Local SHA256 and byte-size checks passed for both pinned archives and matched
  CPA `checksums.txt`.
- The macOS binary reported `7.2.95`, commit `f71ec0eb`, build time
  `2026-07-22T14:48:18Z`; archive layout and platform executable types were
  inspected locally.
- CPA focused tests passed for Management API, SDK auth, auth conductor,
  plugin host, the entire translator tree, and runtime executors.
- Avibe UI build passed to provide required package data. Focused Show Runtime
  manifest/prepare/clean tests passed: 10 passed, 187 deselected. Pytest emitted
  the repository's known temporary locked-directory cleanup warnings; no test
  failed.

Not verified: live OAuth exchanges, live model entitlements, billable provider
requests, vendor ToS/billing, macOS notarization/code signing, maintainer release
signatures, or mutation behavior of startup-owned CPA config fields.
