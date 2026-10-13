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
| Hosted tool block (`hosted_tool`) | when the target hop is `native.source_id` and that hop's current replay evidence is positive (`HopResolution.hosted_tools[tool].replay`): `native.payload` verbatim, in place; otherwise as a different origin | a synthetic call to the same-named tool with the block's `arguments`, plus its result: the `record`'s numbered sources (title, URL, snippet). The assistant message is split around the pair. |
| Text block `citations` | not sent | not sent |
| Tool result with `hosted` (a proxied hosted call) | kept, as any tool result | kept, as any tool result |

Invariants:

- "Opaque provider payload" means any `signature` field on any block, and any redacted thinking. Every rule here
  about signatures applies to all of them. A signature is never synthesized and never sent to a different origin.
- Tool-call id normalization is deterministic: the stored id when the target accepts it; otherwise
  `call_` + the first 24 hex characters of SHA-256 of the stored id. Anthropic accepts `^[a-zA-Z0-9_-]{1,64}$`; the
  other protocols' limits are recorded in the `ai` lane's conversion tables. The map is computed per request; the
  stored transcript is never rewritten.
- Every tool call in the request has exactly one result. A call without a stored result gets a synthetic result,
  `[tool call interrupted; no result recorded]`, with `is_error: true` (C-5 settles most such calls from the job handle
  first).
- Responses API requests use `store: false` and never `previous_response_id`.
- Hosted tool blocks (planned, [`../vibey-hosted-tools.md`](../vibey-hosted-tools.md) §5): `native.payload` is an
  opaque provider payload under every rule above, and its replay is further limited to the recorded Source while
  that Source's current replay evidence is positive. The block never stores replay authorization. A different
  origin loses page text: Anthropic returns it only encrypted, and OpenAI and xAI never return it. Within one
  in-flight turn (`pause_turn`, or a client tool result after server tools in the same message), the request is
  pinned to the Source that produced the content, and the content goes back verbatim.
