# Next-request preview and debug replay

## Contract

The Agent's next-request preparation is shared by its live loop and a read-only
`dry_tick`. A preview at a committed context cut uses current code, route, tools,
and system instructions. It is explicitly a reconstruction, not a claim about
historical wire bytes. Transport failure artifacts are the separate source of
historical actual bytes.

- Preview never enters a run, executes a tool, calls a generation model, invokes
  user hooks, writes transcript/state, or performs context relief/compaction.
- Preparation returns `needs_context_transition` when the live loop would first
  clear results, compact or relieve overflow. It does not advertise that candidate
  as the next send. Pending tools, active runs and arbitrary hooks are explicit
  blockers, not silently ignored work.
- Canonical preparation, refusal projection, budgeting and provider transforms
  reuse the live owners. All four wire protocols use their existing wire builders
  and the same HTTPX JSON serializer as transport.
- Snapshot metadata identifies session/cut, route, compiler/package version,
  configuration and request hash. A context sequence alone is not a frozen request.
- Protected local export contains complete bytes; ordinary CLI output contains
  summaries only. Credentials are never exported. Replay is opt-in, one HTTP
  request outside the Agent Loop, no client tool executor or automatic follow-up.
  Native server tools require an explicit acknowledgement; exact replay never
  silently removes a tool or rewrites a signed body.

## Ownership

This PR owns Agent preparation/preview, protected request export and local
single-request debugging. A separate transport-artifact PR owns pending/error
capture and retention, with no dependency on this PR's unmerged interfaces.
Neither changes historical rendering or claims to repair the investigated 400.

## Verification

Primary tests compare preview bytes against requests captured by a real HTTPX
MockTransport for every provider. State/store/tool/model spies enforce dry-run
boundaries. Context planning tests cover ready vs clear/checkpoint/overflow and
run the existing context suite to protect normal loop semantics. Historical cuts
must not include later refusals/checkpoints. CLI tests cover protected output,
local authorization and no generation on preview; replay tests prove one send,
no client tools and explicit native-tool consent.

## Delivery

Implement, focused checks, independent review, exact-head Codex review and CI,
merge. Then publish one GitHub-only pre-release, install its wheel on the host,
restart with durable continuation and verify the running version. Continue the
original investigation using this capability; do not resume the original task.
