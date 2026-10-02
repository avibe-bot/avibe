# Recovery model (C-5, C-7)

One rule for every effect the Avibe Agent has outside its own process: **record the intent durably, perform the
effect, record the outcome durably.** Recovery after a crash reconciles intents without outcomes. An effect on an
external system with no idempotency key is **at-least-once**; this file says where that applies.

| Effect | Intent | Outcome | Recovery |
| --- | --- | --- | --- |
| Start a command (job) | `meta.json` before the spawn | `exit` file | the launch handshake below decides whether the command can still start |
| Settle a tool call | the committed tool call in the response row | a committed `tool_result` row | at resume, before any projection, append exactly one `tool_result` per open call, chosen from its job state |
| Deliver a response to a surface | the response row with `metadata_json.delivery.state = "pending"` | `"delivered"` with the receipt | re-deliver pending rows in `context_seq` order |

## Launch handshake

The parent and the wrapper agree through one file, `decision`, created with exclusive create (`O_CREAT | O_EXCL`), so
exactly one writer wins:

1. The parent writes `meta.json`, then spawns the wrapper.
2. The wrapper writes `pid` atomically, then waits for `decision`, at most 30 s.
3. The parent waits for `pid`, then creates `decision` containing `go`.
4. The wrapper runs the command only if `decision` says `go`. If the wait expires, it tries to create `decision`
   containing `abandon` itself and exits without running anything; if that create fails, it re-reads `decision`.

Recovery for a job without `exit`:

- `decision` is `go`: the command may be running; use `pid` and the process identity (running or gone).
- `decision` is `abandon`: the command never ran.
- `decision` is absent: recovery creates it with `abandon`. If that wins, the command never ran and never will; if it
  loses, the wrapper's own decision is read and used.

A temporarily missing `pid` file therefore never proves anything; only the `decision` file does.

## Tool-call settlement

`project()` reads only committed rows and stays pure. Before the first projection after a resume, the adapter settles
every tool call without a `tool_result`:

- job `exited` → the final output, formatted as `bash` would;
- job running → hand it over to Watch and commit the handover result (`tools.md` §5);
- job gone, or never ran, or no job for that call → `[tool call interrupted; no result recorded]`, `is_error: true`.

`project()` still answers any call that has no result with that same synthetic text, so an unsettled transcript always
yields a valid request; settlement makes the answer durable and identical across retries and forks.

## Delivery

- Workbench reads the committed row itself, so a re-delivery is a no-op: exactly once.
- IM platforms have no idempotency key at the `BaseIMClient.send_message` boundary, so a crash after the platform
  accepted a message but before the receipt was committed sends it again on recovery: at-least-once. The duplicate is
  limited to that window.
