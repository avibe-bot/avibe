# C-2 Provider adapter interface

One adapter per protocol in `core/agent_core/ai/`. Adapters speak HTTP to the endpoint they are given and know nothing
about Model Hub routing.

```python
class ProviderAdapter(Protocol):
    protocol: Literal["anthropic", "openai_chat", "openai_responses", "google"]

    def stream(self, request: ModelRequest, cancel: CancelToken) -> AsyncIterator[ProviderEvent]: ...


@dataclass(frozen=True)
class ModelEndpoint:            # built by the adapter layer from C-6 HopResolution
    protocol: str
    base_url: str
    token: str                  # never logged, never written to the transcript
    model_id: str               # the runtime model id to send
    request_headers: Mapping[str, str]


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
    cache: Literal["default", "none"]    # "none" for the compaction summarizer
    supports_images: bool                # resolved by the loop; unknown capability counts as False
```

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
`api`, `model`, before the first model byte. On the direct channel (no gateway) the endpoint is the served hop. When a gateway response carries no
served-hop report, the origin is unverified: the adapter commits the message with every opaque provider payload
removed (the `signature` of every block of any kind, and redacted thinking), keeping the endpoint as `origin`, so no
opaque payload of an unknown vendor can ever be replayed. A block without a signature is always sent without one
(`cross-provider.md`).

Error classification is part of the contract because the loop branches on it:

| `kind` | Meaning | Loop action |
| --- | --- | --- |
| `overflow` | the request exceeded the model's context (Pi's overflow patterns, HTTP 413, `context_length_exceeded`) | overflow recovery (plan §5.2) |
| `rate_limit`, `overloaded`, `network`, `server` | transient | retry with backoff, honoring `retry_after_s`, only when nothing was streamed (no deltas, no `partial`); after streamed output the error is terminal for the request, matching Model Hub's first-byte rule |
| `auth`, `invalid_request` | not retryable | end the run with the error |
| `aborted` | cancelled by the caller | end the run as aborted |
| `unknown` | anything else | end the run with the error |

The `ai` lane adds a conversion table per protocol (request fields, stream events, stop reasons, usage fields) to this
directory with its first PR; its tests enumerate the tables.
