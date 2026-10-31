# Model request artifacts

When a Vibey model request does not succeed, Avibe keeps the exact bytes it sent, so the failure can be studied
later: for example, a provider 400 whose cause depends on the request's shape. Requests that succeed leave nothing
behind.

Source: `core/agent_core/ai/request_artifacts.py`. It is wired into the shared transport (`drive_sse_stream` in
`core/agent_core/ai/_common.py`) and into Vibey Turns (`modules/agents/vibey/agent.py`).

## What is kept, and when

Every model request a Vibey Turn sends goes through the shared transport. This covers all four protocols:
Anthropic Messages, OpenAI Responses, OpenAI Chat Completions, and Google.

1. **Before the request is sent**, its pending artifact is written: `request.bin` (the `httpx.Request.content` that
   is then sent, byte for byte) and `meta.json`.
2. **When it ends**, it is resolved in one of two ways:
   - A semantically successful request is deleted. That means one that produced a complete response (`Done`), or an
     Anthropic `pause_turn` sub-request that ended normally. Its continuation is a separate request with its own
     artifact.
   - Every other ending is kept with its outcome: an HTTP error (including the gateway's `pin_unavailable` 409 and
     an HTTP 400 that leads to a resend), a stream that returned HTTP 200 but ended with an error or a stall, a
     network failure, or a cancellation. HTTP 200 alone is not success.
3. **A request with no known ending** is marked `unknown`, never success:
   - its consumer stopped reading, or its task was cancelled before a terminal (the driver awaits a shielded write
     that marks it, visible to every reader at once);
   - it was cancelled while its pending write ran, before it was sent (`unknown`, outcome `not_sent`);
   - the process died, or was cancelled again during that write: the entry stays `pending` on disk, and
     `vibe model-requests` shows it as `unknown` once its owner process is gone.

Outside a Vibey Turn nothing is captured: the library default is off, so tests and other callers stay hermetic.

### Scope: client-to-Model-Hub bytes

Vibey sends its requests to the local Model Hub gateway. These are the bytes Vibey sent to the gateway. They are
**not** the bytes the gateway, a relay, or the provider finally received, because the Model Hub engine may rewrite
a request before forwarding it. To correlate one with the upstream hop, use its `x-request-id` / `request-id`
response header (kept in `meta.json`) and the Model Hub provenance of the same Turn.

## Layout

```
~/.avibe/state/agent_core/model-requests/
  mr_20261031T140102Z_0a1b2c3d4e5f/
    request.bin     # the request body exactly as sent
    response.bin    # the raw response bytes read, when there were any
    meta.json       # typed facts only
```

- **`meta.json`** holds:
  - the Session and Turn, and the Avibe version;
  - the hop: protocol, provider, model, Source, and the URL without its query or userinfo;
  - allowlisted request headers (`content-type`, `anthropic-version`, `anthropic-beta`, the Model Hub pin headers,
    and the like);
  - the body's size and sha256;
  - `outcome`: `kind`, `error_kind`, `http_status`, and `exception_type`;
  - for the response: its status, allowlisted headers (`content-type`, `request-id`, `x-request-id`, `retry-after`,
    `x-should-retry`, `x-avibe-served-hop`), and the bytes read and kept.

  It never holds a credential or any other header, and never an error message: a provider's message can echo the
  prompt, tools, or a credential. The response body is in `response.bin`.
- **`response.bin`**:
  - An HTTP error body is kept from its start, up to 4 MiB. Reading past the first 64 KiB, which the error's
    classification uses, is for the artifact only: each such read waits at most 10 s, and a stall, reset, or
    cancellation there only marks the capture incomplete (`read_error`). The error is classified exactly as without
    capture.
  - A stream (HTTP 2xx) keeps its first 64 KiB and its last 1 MiB, so the terminal error event of a long response
    survives.
  - Any cut is stated in `meta.json` (`truncated`, `truncation`, `omitted_bytes`, `read_to_end`, `read_error`).

The files can contain prompts, tool output, and attachments. Directories are `0700`, files `0600`. Files are
created with `O_EXCL | O_NOFOLLOW`, and a store path that is a symbolic link is refused.

## Retention

The defaults are 7 days and 256 MiB. Retention is applied when a failed request is kept, and once when a process
first records a request:

- Expired attempts are removed.
- Over the quota, the oldest finished or unknown attempts are removed first.
- An attempt still being sent by a live process is never removed. Liveness is the owner's pid together with its
  process start time, so a reused pid does not count.

Trade-offs:
- A week covers the time between noticing a failure and studying it.
- 256 MiB holds dozens of the largest real requests (several MiB each with long contexts or images) without
  growing without bound.
- Retention never blocks a request. A single request larger than the quota is still kept while older ones go.

## Runtime cost and failure isolation

All filesystem work runs on one private worker thread, never on the controller's event loop. Each request waits
only for its pending write before it is sent, and for its own removal or finalization when it ends.

If the store cannot be written (full disk, a non-directory in its place), the request proceeds exactly as it would
without capture. A warning names only the failed step and the exception type.

## CLI

```bash
vibe model-requests list [--session-id ID] [--state failed|cancelled|unknown|pending] [--page N] [--limit N]
vibe model-requests show <id>
vibe model-requests export <id> --to <dir>
```

- The commands run at the Owner role floor, since the attempts hold every Session's prompts.
- `list` and `show` print the JSON envelope with metadata only, never a body.
- `export` reads only from the store, by a validated id. It copies the attempt's files into a new private
  directory `<dir>/<id>/`, and refuses a symbolic-link destination or an existing export.
- Share exports with care: they hold the conversation.

## Testing

`tests/agent_core/ai/test_request_artifacts.py` drives the real adapters over `httpx.MockTransport`. It covers:

- exact bytes on HTTP 400 and 503 for all four protocols, and on a network error;
- a stream that returns HTTP 200 and then ends in an error;
- error-body truncation;
- success, and a successful `pause_turn` sub-request;
- cancellation, and a consumer that closes early;
- off-loop execution and failure isolation;
- retention around active and orphaned attempts;
- export safety, and concurrent requests.

`tests/test_vibey_agent.py::test_a_turns_failed_model_request_is_kept_with_its_session_and_turn` covers the Vibey
wiring, and `tests/test_cli_model_requests.py` covers the CLI. Every test writes under its own temporary home.
