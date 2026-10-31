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
2. **When it ends**, it is resolved in one of these ways:
   - A semantically successful request is deleted. That means one that produced a complete response (`Done`), or an
     Anthropic `pause_turn` sub-request that ended normally. Its continuation is a separate request with its own
     artifact.
   - One that ended in an error is kept as `failed`: an HTTP error (including the gateway's `pin_unavailable` 409
     and an HTTP 400 that leads to a resend), a stream that returned HTTP 200 but ended with an error or a stall,
     or a network failure. HTTP 200 alone is not success. A turn stopped mid-request is `cancelled`.
   - One stopped before it was sent is `cancelled` with outcome `not_sent`. A Stop that arrives during the
     pending write is checked before the send, so it never causes provider work.
   - One closed with no known outcome is `unknown`: its consumer stopped reading, or its task was cancelled before
     a terminal.
3. **Every ending goes through one path.** The attempt leaves this process's active set and the write of its final
   state is queued on the worker in the same synchronous step, so no later cancellation can skip it. The driver
   then awaits that write, shielded: cancelling the await leaves the write to run, and a failure of the write is
   logged, never raised into the request. Whichever comes second, a pending write landing or a cancellation that
   interrupted it, settles the attempt as `cancelled` / `not_sent`.
4. **An attempt whose final state was never written** stays `pending` on disk: the process died first.
   `vibe model-requests` shows it as `unknown` once its owner process is gone (pid and process start time). It is
   never treated as a success.

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
  The capture only sees bytes the transport reads anyway. It never reads on its own, so the error's classification
  and timing are what they are without it.
  - An HTTP error body is kept as far as it is read to classify the error: its first 64 KiB, plus the rest of the
    last chunk read, up to 1 MiB. A larger body is **expected to be incomplete**, with `read_to_end: false`.
  - A stream (HTTP 2xx) keeps its first 64 KiB and its last 1 MiB, so the terminal error event of a long response
    survives.
  - `meta.json` states completeness and any cut: `read_to_end`, `truncated`, `truncation`, `omitted_bytes`. The
    request body, by contrast, is always complete.

The files can contain prompts, tool output, and attachments. The store root and every export directory are made
owner-private by `core.private_fs`:
- on POSIX, `0700` directories and `0600` files;
- on Windows, a protected DACL that grants only this user and SYSTEM, set when the directory is created and
  inherited by everything created inside.

Files are created with `O_EXCL | O_NOFOLLOW`, and a store or export path that is a symbolic link is refused. The
Windows path is exercised by `core.private_fs`'s own Windows tests; this feature's tests check the same
`private_fs.is_private` contract on the platform they run on.

## Retention

The defaults are 7 days and 256 MiB. Retention runs at the store's own writes, never on a timer:

- after every attempt that is kept, whether `failed`, `cancelled`, or `unknown`;
- when a process first records a request;
- at any other write (a new request, or the removal of a successful one) once 10 minutes have passed since the last
  run.

An attempt that expires is therefore removed at the first write after it does. A process that sends nothing
removes nothing; the next one to write does.

Each run removes expired attempts, and then, while the store is over its quota, the oldest finished or unknown
attempts. The quota is strict for those: a single kept attempt larger than the quota is removed as soon as it is
kept. An attempt still being sent by a live process is never removed (liveness is the owner's pid together with its
process start time, so a reused pid does not count). While requests are in flight, the store can therefore exceed
the quota by their size.

Trade-offs:
- A week covers the time between noticing a failure and studying it.
- 256 MiB holds dozens of the largest real requests (several MiB each with long contexts or images) without
  growing without bound.
- Retention is part of the write it runs in, on the artifact worker. A run scans the store, which the quota keeps
  small, so it can add a few milliseconds to that write. It never changes a request's outcome.

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
