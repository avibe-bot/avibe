# C-1 Cross-provider transform

Applied by `ai` when it builds a request for a target `(provider, api, model)` from canonical messages
(`message.schema.json`). Rules ported from Pi's `pi-ai` `transform-messages` (MIT); cases ported from tau's
cross-provider history tests (MIT).

| Input | Same origin as the target | Different origin |
| --- | --- | --- |
| Text block | kept | kept |
| Thinking block with `signature` | replayed verbatim with its signature | converted to plain assistant text; the signature is dropped |
| Thinking block, `redacted: true` | replayed verbatim | dropped |
| Thinking block without `signature` (unverified origin, C-2) | sent as plain assistant text | sent as plain assistant text |
| Tool call | kept; `id` and `signature` as stored | kept without `signature`; `id` normalized to the target's rules (below) |
| Tool result | kept | `tool_call_id` rewritten through the same id map |
| Image in a user or tool-result message | kept | kept if the target model accepts images, else `[image: <name or mime>]` |

Invariants:

- A signature is never synthesized and never sent to a different origin.
- Tool-call id normalization is deterministic: the stored id when the target accepts it; otherwise
  `call_` + the first 24 hex characters of SHA-256 of the stored id. Anthropic accepts `^[a-zA-Z0-9_-]{1,64}$`; the
  other protocols' limits are recorded in the `ai` lane's conversion tables. The map is computed per request; the
  stored transcript is never rewritten.
- Every tool call in the request has exactly one result. A call without a stored result gets a synthetic result,
  `[tool call interrupted; no result recorded]`, with `is_error: true` (C-5 settles most such calls from the job handle
  first).
- Responses API requests use `store: false` and never `previous_response_id`.
