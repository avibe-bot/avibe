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
class ModelRequest:
    endpoint: ModelEndpoint
    system: str
    messages: Sequence[Message]          # canonical (C-1); the adapter applies cross-provider.md
    tools: Sequence[ToolSpec]            # C-7; may differ on every call
    max_tokens: int
    reasoning_effort: str | None         # one of the hop's declared efforts, or None
    cache: Literal["default", "none"]    # "none" for the compaction summarizer
```

`stream` yields `ProviderEvent`s (`provider-event.schema.json`) and always ends with exactly one `done` or one
`error`. `done.message` is the complete canonical `AssistantMessage`; its `origin` comes from the served-hop report
(C-6), falling back to the endpoint only when no report exists.

Error classification is part of the contract because the loop branches on it:

| `kind` | Meaning | Loop action |
| --- | --- | --- |
| `overflow` | the request exceeded the model's context (Pi's overflow patterns, HTTP 413, `context_length_exceeded`) | overflow recovery (plan §5.2) |
| `rate_limit`, `overloaded`, `network`, `server` | transient | retry with backoff, honoring `retry_after_s` |
| `auth`, `invalid_request` | not retryable | end the run with the error |
| `aborted` | cancelled by the caller | end the run as aborted |
| `unknown` | anything else | end the run with the error |

The `ai` lane adds a conversion table per protocol (request fields, stream events, stop reasons, usage fields) to this
directory with its first PR; its tests enumerate the tables.
