# C-1 Cross-provider transform

Applied by `ai` when it builds a request for a target `(provider, api, model)` from canonical messages
(`message.schema.json`). Rules ported from Pi's `pi-ai` `transform-messages` (MIT); cases ported from tau's
cross-provider history tests (MIT).

| Input | Same origin as the target | Different origin |
| --- | --- | --- |
| Text block | kept | kept |
| Thinking block with `signature` | replayed verbatim with its signature | dropped |
| Thinking block, `redacted: true` | replayed verbatim | dropped |
| Thinking block without `signature` (unverified origin, C-2) | sent as plain assistant text | dropped |
| Tool call | kept; `id` and `signature` as stored | kept without `signature`; `id` normalized to the target's rules (below) |
| Tool result | kept | `tool_call_id` rewritten through the same id map |
| Image in a user or tool-result message | kept | kept if the target model accepts images, else `[image: <name or mime>]` |
| Hosted tool block (`hosted_tool`) | when the target hop is `native.source_id`, its origin equals the message's `origin`, and its current replay evidence is positive (`HopResolution.hosted_tools[tool].replay`): `native.payload` verbatim, in place; otherwise as a different origin. | a synthetic call to the same-named tool, plus its result, both rendered from the block's `action` and `record` by the one record-to-text function under the per-action rules of `../vibey-hosted-tools.md` §5. A `failed` or `incomplete` record is an error result (`is_error`, or a leading `[<tool> failed: <error>]` where the protocol has no flag). A `pending` record is the interrupted-call result below. The assistant message is split around the pair. |
| Text block `citations` | not sent | not sent |
| Tool result with `hosted` (a proxied hosted call) | kept, as any tool result | kept, as any tool result |

**Reader compatibility.** Deserialization does not require the current hosted-tool registry to read stored history.

- A native `HostedToolBlock` whose `tool` this build does not recognize becomes one portable `TextBlock` with any
  query or URL and its sources, or a one-line unavailable placeholder. Readable non-completed status and error are
  kept; a pending call explicitly has no result, never an implied success.
- Whenever any native block is degraded, response-wide counts, cited sources and cost are discarded because they
  cannot be partitioned between unknown and known calls. Decode the calls first and do not validate the discarded
  aggregate's contents against this build's summary/source schema. An existing summary becomes empty if recognized
  hosted blocks remain, and is omitted otherwise; a missing summary for a recognized block remains invalid.
- A proxied `ToolResultMessage.hosted` whose `tool_name` is unknown becomes an ordinary tool result with its stored
  `content` and `is_error`, and `hosted` is dropped.

This degradation applies only to unrecognized tool names: the message envelope and known tools remain strictly
validated, new writes remain strict, stored rows are not rewritten, and each degraded row logs one warning without
content.

Invariants:

- Thinking never goes to a different origin, in any form (owner decision, 2026-10-21; Pi converts it to text). It is
  the serving model's private reasoning, not something the assistant said: sent as assistant text it is attributed to
  the target model, and Anthropic refuses requests whose history carries another model's reasoning as text
  (`reasoning_extraction`), the same refusal on every retry, so a Session that switched models stays stuck. The
  stored transcript keeps it; only the request leaves it out. An assistant message whose only content was such
  thinking is left out of the request, as an aborted one is, never sent empty; the messages around it keep their
  order (consecutive user-role messages are already valid on every protocol).
- Same-origin unsigned thinking stays plain assistant text: a protocol that cannot take that reasoning back gets the
  model's own words, attributed to the model that wrote them.
- "Opaque provider payload" means any `signature` field on any block, and any redacted thinking. Every rule here
  about signatures applies to all of them. A signature is never synthesized and never sent to a different origin.
- Opaque payloads are also bound to the account that issued them, the identity of the Source that served the message
  (`AssistantMessage.source_identity`, from HopResolution `source_identity`), because a provider refuses another
  account's ciphertext (OpenAI and xAI encrypted reasoning). When the request names its hop (`endpoint`), a message
  takes the "same origin" column only if its origin equals the target's **and** its `source_identity` equals the
  hop's. A message another identity served (another Source, or the same Source after its key or relay changed), or
  one no known identity served (after a failover to another Source, or a row written before the field), takes the
  "different origin" column at a hop that names an identity. A message whose identity's provider refused an opaque
  payload of a kind in this Session (`ModelEndpoint.opaque_refused`) goes without every payload of that kind: its
  signatures take the "different origin" column (kind `signature`), or its hosted calls their portable form (kind
  `hosted`). A relay pooling upstream accounts behind one key changes account without changing identity, so the
  refusal, not the identity, is what reveals it.
  The gateway applies the same binding at failover (C-6 item 6).
- Tool-call id normalization is deterministic: the stored id when the target accepts it; otherwise
  `call_` + the first 24 hex characters of SHA-256 of the stored id. Anthropic accepts `^[a-zA-Z0-9_-]{1,64}$`; the
  other protocols' limits are recorded in the `ai` lane's conversion tables. The map is computed per request; the
  stored transcript is never rewritten.
- Every tool call in the request has exactly one result. A call without a stored result gets a synthetic result,
  `[tool call interrupted; no result recorded]`, with `is_error: true` (C-5 settles most such calls from the job handle
  first).
- Responses API requests use `store: false` and never `previous_response_id`.
- Hosted calls deferred behind Anthropic client tools are one logical call across two immutable responses
  (clarified with orchestrator approval, **2026-10-22**; persisted shape remains **v1**). Response 1 has the pending
  `hosted_tool` and a use-only `native.payload`; response 2 has the same id, tool and action, its completed/failed
  record, and a **result-only** `native.payload`. No prior row changes. Pair only the immediately continued mixed
  response, its client results, and the same origin, Source and Source identity. The later fragment makes the
  earlier use finished for replay eligibility. Both halves are native or neither is: native replay keeps the use,
  client results and server result in their original response boundaries; portable replay emits one synthetic
  call in the first response's parallel group, with its result among that group's client results, and removes the
  completion fragment from response 2. Usage and cost remain on the reporting response and are never inferred
  again from the completion. An unmatched pending use keeps the interrupted rule for model replay. On Web and IM,
  deferred use-only calls produce no card; response 2's result fragment uses the ordinary hosted-card path at its own
  position. There is no cross-response card merge, held-card state or special delivery guarantee. Failed `error`
  attempts on the same deferred Anthropic exchange are omitted together with every client-tool result owned by each
  failed response from the shared effective request history used by in-flight discovery, pairing and wire compilation;
  an authorized retry resends the last valid continuation, preserving failed-attempt usage separately (hosted-tools
  §§4.4/15). This is a C-1 interpretation of
  existing v1 fields, verified by round-tripping both fragments through the v3.2.2 and gh-v3.2.3rc2 readers; no new
  message discriminator or reader-breaking field is introduced. Provider contract, fetched **2026-10-22**:
  [Server tools, mixed client/server groups](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools.md).
- Hosted tool blocks ([`../vibey-hosted-tools.md`](../vibey-hosted-tools.md) §5): `native.payload` is an
  opaque provider payload under every rule above, and its replay is further limited to the recorded Source while
  that Source's current replay evidence is positive. The block never stores replay authorization. A different
  origin loses page text: Anthropic returns it only encrypted, and OpenAI and xAI never return it. Within one
  in-flight turn (`pause_turn`, or a client tool result after server tools in the same message), the request is
  pinned to the Source that produced the content, and the content goes back verbatim. This transform is the only
  converter of hosted content. The gateway never rewrites it: a pinned request it cannot serve comes back as
  `pin_unavailable`, and the loop renders the request again for a fresh hop (`provider.md`).
