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
3. **Hop resolution** for the `avibe` backend returns `HopResolution` (`hop-resolution.schema.json`): the primary
   hop's protocol, the gateway URL for that protocol, the token, the runtime model, and capabilities including the
   input limit and image support. The agent resolves once per run and again after a retryable error.
4. **Served-hop report.** For every response, the agent learns which hop actually served it (`provider`, `api`,
   `model`) before it commits the response. It records that as the message `origin` (C-1), so a signature is never
   replayed into a different vendor after a failover. The report comes from the same attempt Model Hub records as
   `TurnProvenance.served`. Delivery options, chosen by the Model Hub lane: response headers sent with the first
   output byte, or a lookup of the request's served attempt through the internal API by its correlation metadata.
5. **Conversion stays a fallback.** The agent speaks the primary hop's protocol. When failover reaches a hop with
   another protocol, the gateway converts (degraded, not broken) and the served-hop report says so.

## 3. Not required

Failover, credentials, quota, and Source health stay in Model Hub. The agent never contacts a vendor directly.
