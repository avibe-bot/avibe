# C-2 Provider adapter interface

One adapter per protocol in `core/agent_core/ai/`. Adapters speak HTTP to the endpoint they are given and know nothing
about Model Hub routing.

```python
class ProviderAdapter(Protocol):
    protocol: Literal["anthropic", "openai_chat", "openai_responses"]

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ProviderEvent]: ...


@dataclass(frozen=True)
class ModelEndpoint:            # built by the adapter layer from C-6 HopResolution
    protocol: str
    base_url: str
    token: str                  # field(repr=False): never logged or rendered, never written to the transcript
    model_id: str               # the runtime model id to send
    request_headers: Mapping[str, str]
    provider: str               # vendor identity of the hop (HopResolution.provider); with protocol and model it
                                # forms the target origin for the cross-provider rules
    source_id: str | None       # HopResolution.source_id: the pin of a request with native hosted content
    hosted_tools: Mapping[str, HostedToolRoute]   # HopResolution.hosted_tools, in Model Hub's order: per enabled
                                # tool its mode, and for native its declaration and Responses include values,
                                # plus the turn's executor and replay (vibey-hosted-tools.md §3.5)


@dataclass(frozen=True)
class ModelCapabilities:      # from C-6 HopResolution; None means Model Hub does not know
    context_window: int | None
    input_limit: int | None
    max_output_tokens: int | None
    supports_tools: bool | None
    supports_images: bool | None
    supports_reasoning: bool | None
    reasoning_efforts: tuple[str, ...]


class MediaLoader(Protocol):   # supplied by the adapter; reads media_objects
    async def load(self, media_token: str) -> tuple[bytes, str]: ...   # bytes, mime type


@dataclass(frozen=True)
class ModelRequest:
    endpoint: ModelEndpoint
    system: str
    messages: Sequence[Message]          # canonical (C-1); the adapter applies cross-provider.md
    tools: Sequence[ToolSpec]            # C-7; may differ on every call
    max_tokens: int
    reasoning_effort: str | None         # one of the hop's declared efforts, or None
    cache: Literal["default", "none"]    # "default": the protocol's cache breakpoints and their lifetimes
                                         # (conversion-tables.md); "none" for the compaction summarizer
    supports_images: bool                # resolved by the loop; unknown capability counts as False
    tool_choice: str | None              # the one tool the response must call; set only by a hosted-tool probe
```

`tools` is the one owner of what a request declares. A hosted tool is its canonical `ToolSpec`
(`core/agent_core/tools/hosted.py`, `hosted_tool_spec`). On a hop whose route for it is native, the adapter sends the
route's `declaration` verbatim in the spec's place, merges a Responses route's `include` values into `include`, and
pins the request with `x-avibe-pin-source: <source_id>`; on any other hop the spec is an ordinary function. Vibey
lists its hosted tools first, so Anthropic's tools breakpoint, which goes on the last function tool, never moves.
`registry.build_wire_request` returns the URL, headers and body the adapter would send, without sending it; a
hosted-tool probe sends exactly that request (`vibey-hosted-tools.md` §3.2). On Responses and Chat a forced function
is named, and a native hosted tool is forced with `"required"`, which needs it to be the request's only tool. An
Anthropic request is never forced: current Claude models and extended thinking refuse a forced `tool` or `any`
choice.

History follows the same Source rule. A response's hosted calls go back verbatim only to the hop the request goes to,
when that hop's Source served them on the same origin (`conversion-tables.md`, Hosted tools): within their turn as a
continuation, later with the hop's current `replay`, and pinned to that Source either way. The gateway answers a pinned request it cannot serve from that Source with
`409 pin_unavailable`. That answer is not a `ProviderError`: the adapter sends the same request exactly once more in
portable form and unpinned, so the loop's retry never sees it.

The loop's `ModelRouter` returns the `ModelEndpoint` together with its `ModelCapabilities`. Every capability has a
defined effect, including when it is unknown:

| Capability | `true` / value | `false` | unknown (`null`) |
| --- | --- | --- | --- |
| `context_window` | W for C-9 | — | the configured default window (128,000) |
| `max_output_tokens` | upper bound for `max_tokens` | — | the configured default (8,192) |
| `supports_tools` | tools sent | the route is refused before the run with a clear error: the agent needs tools | tools sent; a provider rejection surfaces as an error |
| `supports_images` | images sent | placeholders | placeholders |
| `supports_reasoning` | `reasoning_effort` sent if it is one of `reasoning_efforts` | `reasoning_effort = None` | `reasoning_effort = None` | Adapters are constructed with a
`MediaLoader` and resolve each `ImageBlock`'s bytes at request time; the canonical message never carries bytes. The
token names an immutable snapshot (`message.schema.json`), so the bytes never change for a committed block.

`stream` yields `ProviderEvent`s (`provider-event.schema.json`) and always ends with exactly one `done` or one
`error`. `done.message` is the complete canonical `AssistantMessage`; its `origin` comes from the served-hop report
(C-6), which the gateway sends as the `x-avibe-served-hop` response header: compact JSON with exactly `provider`,
`api`, `model`, `source` (Model Hub contract v12), before the first model byte. The `origin` is `provider`, `api`,
`model`; `source` is the serving Source id, which is not part of a message's origin. A report of any other shape
counts as no report. On the direct channel (no gateway) the endpoint is the served hop, and its `source_id`, when it
names one, is the serving Source. When a gateway response carries no
served-hop report, the origin is unverified: the adapter commits the message with every opaque provider payload
removed (the `signature` of every block of any kind, and redacted thinking), keeping the endpoint as `origin`, so no
opaque payload of an unknown vendor can ever be replayed. A block without a signature is always sent without one
(`cross-provider.md`).

Error classification is part of the contract because the loop branches on it:

| `kind` | Meaning | Loop action |
| --- | --- | --- |
| `overflow` | the request exceeded the model's context (Pi's overflow patterns and Anthropic's refusal of input plus `max_tokens` over the window, HTTP 413, `context_length_exceeded`) | overflow recovery (plan §5.2) |
| `rate_limit`, `overloaded`, `network`, `stalled`, `server`, `interrupted` | transient (`network`: the provider could not be reached; `stalled`: a connected provider sent nothing for the silence bound; `interrupted`: the provider's stream ended after output without a terminal, as Model Hub reports with its own `modelHub.errors.stream_interrupted` frame) | retry with backoff, honoring `retry_after_s`, only when nothing was streamed (no deltas, no `partial`), at most 3 retries within 120 s; then the run ends with that error. After streamed text or a tool call the error is terminal for the request, matching Model Hub's first-byte rule; after thinking alone it is retried once (below) |
| `auth`, `invalid_request` | not retryable | end the run with the error |
| `aborted` | cancelled by the caller | end the run as aborted |
| `unknown` | anything else | end the run with the error; after thinking alone, retry once (below) |

**A stream that dropped after thinking alone** (owner decision, 2026-10-11). When a response fails after it
streamed thinking and nothing else (no text, no tool call), with a `network`, `stalled`, `server`, `overloaded`,
`interrupted`, or `unknown` error, the `ProviderError` has `retryable` and `after_thinking` set. The loop retries such an error at
most once per request, inside the same count and time budget; a second drop ends the run with that error, because
some prompts drop at the same point every time. The dropped attempt's thinking is never context: it stays in the
attempt ledger and its audit (C-9 `context.md` §10, invariant 4), and the retry resends the original request.
A refusal or safety stop is a response, not an error, and `auth`, `invalid_request`, `overflow`, and `rate_limit`
after thinking stay terminal.

A `ProviderError` whose `partial` is committed (a response that failed after output) carries the error's text as that
partial's `error_message`, redacted like the error itself and bounded to 1,000 characters, so the transcript row says
why the response ended. A Stop's partial carries none.

The `ai` lane adds a conversion table per protocol (request fields, stream events, stop reasons, usage fields) to this
directory with its first PR; its tests enumerate the tables.
