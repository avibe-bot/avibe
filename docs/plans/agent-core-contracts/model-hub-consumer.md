# C-6 Model Hub consumer extension

What the Avibe Agent needs from Model Hub. Owned by Model Hub (`../model-hub-contracts/`); this file states the
consumer's requirements, and the Model Hub lane lands the change in both places.

## 1. Today

- Source `protocol` and backend `native_protocol` know `anthropic`, `openai_responses`, and `openai_chat`.
- `ModelHubLaunch` (`modules/agents/model_hub.py`) gives a consumer `gateway_base_url`, `gateway_token`,
  `gateway_request_metadata`, `runtime_model`, `context_window`, `max_output_tokens`, `supports_tools`,
  `supports_reasoning`, and `reasoning_efforts`, but not the protocol to speak.
- The gateway exposes `/<backend>/v1/{messages | chat/completions | responses}` and converts when the frontend protocol
  differs from the hop's. It fails over only before the first user-visible model output byte; after that a failure is
  terminal.

## 2. Required

1. **`google` protocol.** Gemini `generateContent` / `streamGenerateContent` joins the protocol vocabulary and gets a
   gateway frontend under the backend prefix.
2. **Backend id `avibe`** in every Model Hub enum that lists backends (C-8).
3. **Hop resolution** for the `avibe` backend returns `HopResolution` (`hop-resolution.schema.json`) through the Model
   Hub runtime router: the primary hop's protocol, the gateway URL for that protocol, the token, the runtime model,
   and capabilities. A capability Model Hub does not know is `null`, never guessed; `input_limit` is `null` until Model
   Hub stores one. The agent treats unknown values conservatively (C-2). It resolves once per run and again after a
   retryable error.
4. **Served-hop report.** Every gateway response for `avibe` carries `x-avibe-served-hop`: compact, ASCII-escaped JSON
   with exactly `provider`, `api`, `model`, taken from the winning attempt (Source vendor, Source protocol, upstream
   target) and sent before the first model byte. The gateway does not commit early keepalive headers for `avibe`;
   other backends are unchanged. The same origin is recorded on `TurnProvenance.served`. An `api` different from the
   frontend protocol means the gateway converted. The agent records it as the message `origin` (C-1). The header value is at most 4096 bytes; a hop whose origin cannot be represented within that limit is
   refused before invocation with a controlled local error (no model call, no header). Identifiers are never truncated.
5. **Conversion stays a fallback.** The agent speaks the primary hop's protocol. When failover reaches a hop with
   another protocol, the gateway converts (degraded, not broken) and the served-hop report says so.
6. **No opaque payload crosses origins at failover.** The agent builds the request for the primary hop's origin, so it
   may carry that origin's opaque payloads (C-1: any block's signature, redacted thinking). When the gateway fails
   over to a hop whose origin `(provider, api, model)` differs from the primary's, it removes every such payload
   before sending, applying the same rule as `cross-provider.md`, whether or not it converts protocols. Known limit:
   the next request targets the primary origin again, so payloads returned by a fallback origin are not replayed if a
   later request fails over to that same fallback; a provider that then rejects the call surfaces as a provider error.

## 3. Not required

Failover, credentials, quota, and Source health stay in Model Hub. The agent never contacts a vendor directly.
