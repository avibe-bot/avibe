# Vibey canonical tool activity (reader-first)

Contract: `sqlite-upgrade-downtime.md`, section C-alt. This change keeps all
compatibility trace INSERTs. Write suppression and the epoch/downgrade floor are
separate work, blocked on section A and the owner's activation instruction.

## Invariants

- Only newly committed, explicitly marked Vibey responses project calls. Backend
  changes on a Session never reinterpret its earlier messages. Unmarked history,
  unknown capabilities and other backends retain the existing trace reader.
- A call is identified by its response row and provider call id. Canonical block
  order is explicit; repeated arguments and reused ids in retries/forks never
  match heuristically. Transitional traces link to that identity and deduplicate.
- One projection supplies the activity summary/detail and live publication. The
  source commits before publication; no reconstruction executes tools or models.
- A request is not execution evidence. Only ToolStarted records a start; retained
  canonical results establish outcomes. Compact display metadata stores timing
  and source labels/state, never arguments, rendered tool text or result bodies.
- Canonical media registration supplies authorized image URLs independently of
  trace persistence. IM formatting/delivery, activity visibility gates, Session
  ranking, canonical context, retention and audit remain unchanged.
- Reads are bounded by retained response ids and each response's context interval;
  live work seeks one response. SQL diagnostics gain a read-only `tool_calls` view
  without changing the physical `agent_events` table or its access policy.

## Additive wire contract

Stable step id: `tool:<response-row-id>:<URL-encoded-call-id>`.

Activity rows carry `order_micros`, `order_index`, `status`, `started_at`,
`ended_at`, `duration_ms`. Live `message.new` carries the same fields under
`content.activity`. Existing hosted cards remain `content.hosted` live and
`hosted` in activity rows. Status vocabulary: `pending`, `running`, `completed`,
`failed`, `interrupted`, `unknown`. The frontend lane owns consuming explicit
order and upserting same-id state changes. No chat arguments become chat text.

Native calls sort just before their response; ordinary calls just after it;
canonical block index orders calls at the same anchor. Group/Turn attribution
uses the existing activity boundary projection. The current 500-message /
2000-trace window applies equally to summary and expansion; live steps use the
same identity/order calculation.

## Diagnostics and rollout

`vibe data query` exposes a connection-local, read-only `tool_calls` view:
`session_id`, `source` (`canonical` or `trace`), `source_id`, `response_id`,
`call_id`, `context_seq`, `id`, `order_micros`, `order_index`, `name`,
`arguments_json`, `status`, `started_at`, `ended_at`, `duration_ms`, `turn_id`,
`text`. Legacy traces do not invent call ids, arguments or execution evidence.
For example:

```sh
vibe data query --sql "select id, source, name, status from tool_calls where session_id = 'ses...' order by order_micros, order_index, id"
```

It retains the existing `agent_events` Member/Owner diagnostic access requirement;
it is not a way around the raw-table authorizer. `agent_events` stays physical,
and `transcript`, `turns`, `digests` and message search keep their meanings.

The additive UI consumer (#3369) has landed first. This reader-first change still
writes all compatibility traces, so trace-only rollback remains correct. It
adds response capability/source links, compact start/finish/source-display facts,
and exact canonical image registration tokens; it does not change model payloads
or `context_seq`. The event consumer binds call ids at `MessageCommitted`, not in
the producer's ahead-of-consumer store callback, so a queued event cannot be
attributed to a later response that reused the id. Live recovery publishes its
committed settlements before terminal delivery or automatic continuation; a
missing ToolFinished cannot leave the last live step running after Stop. No
storage savings are claimed while the compatibility writer is still enabled. The write-suppression PR owns
epoch activation, previous-release compatibility testing, upgrade documentation,
and measurements against #3367's single-copy baseline.

## Validation

Primary owner coverage: projection/service tests for tool-only responses,
parallel/reused ids, interval-bounded results, fork isolation, pending/cancelled
calls, hosted media/errors, legacy/marked mixed rows and read-only behavior.
Distinct integration risks: adapter commits/ToolStarted and mirror live gates,
trace compatibility/identity links, query ACL and query plans. Frontend tests
cover live upserts, explicit ordering and reload deduplication. No migration,
release, service restart, remote operation or bulk cleanup is authorized here.
