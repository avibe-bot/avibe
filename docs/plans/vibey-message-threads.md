# Vibey native Anthropic message threads

An upload optimization, never the source of conversation history. Only Vibey conversation requests may create a reusable response ID. Counted side requests, direct CLI backends, and Hub pass-through do not inherit this behavior.

## Acceptance contract

- Pin speculative sends to the selected Source/model. Keep a single-use, five-minute Session parent only after a completed, non-truncated response; compare the rendered history and the assistant reply, route identity, system, tools, settings and betas before omitting anything. If these differ, send full history.
- For an append, previous reported input (including cache read/write) plus output must be at least 20,000 and at least three times the budget for system, tools and the appended messages. Use the first `message_start` to require reported input of at least 90% of the parent total before releasing text, thinking or tool events. **Known by design:** if a relay drops the parent but injects or reports roughly an entire conversation's worth of unrelated input, its answer or tool call can pass the guard without the original context.
- On speculative refusal or failed admission discard the attempt; replay the ordinary full request, without the thread field or beta, once. Ordinary transport retry, terminal failure, cancellation and partial-stream semantics still belong to their existing owners. A failed speculative send does not cool the Source needed for replay.
- All routes try by default. A failed route uses ordinary requests for 24 hours. Creating `state/disable-message-threads` in the instance home disables new thread requests without restart; removing it permits them again. Request cache metadata records attempt mode, wire body bytes, corresponding ordinary full body bytes and the fallback reason.
- Verify usage and cache behavior independently on matched real requests. A mock does not establish provider billing behavior. The owner-route confirmation comes from the first real thread call after update; no historical Session is a test target.

Claude Code would require its native client and the Hub's native pass-through/error mapping to agree. Codex needs a separate Responses-protocol design. OpenCode would need native client support or a gateway-side history owner; none are changed here.
