# Single-copy mirror trace text

## Contract

New mirror-written `agent_events` rows with `event_type='tool_call'` and
`visibility='trace'` keep their rendered text in `content_text`, without an
automatically synthesized `content_json.text`. `content_json.kind`, hosted-tool
cards, metadata, event identity, provenance, ordering, and live delivery stay as
before. The serialization change belongs in `agent_events_service.append`, where
the duplicate was introduced, not in each backend or the mirror's callers.

Acceptance invariants:

- Current and released **3.2.4** readers display the same complete tool arguments,
  including quoting, newlines, Unicode, and hosted cards.
- Generic SQL still reads `content_text`. Existing dual-copy and JSON-only rows
  still load; a missing JSON text member does not suppress activity.
- Only new tool-call trace writes change. Existing rows are never updated;
  explicit caller-provided JSON, other event types, and other visibilities keep
  their existing append behavior.
- No schema migration, backfill, retention change, removal of trace rows, or
  change to Vibey's canonical model context.

This is independent of the separate proposal to stop redundant Vibey trace rows
and project activity from canonical messages, tracked by PR #3366
(`docs/plans/sqlite-upgrade-downtime.md` in that proposal). That unmerged design
is not an implementation dependency.

## Reader inventory

Audited against master `15c5a8533ea1d829a246a4989c6d80e61c027aab` and released
`v3.2.4`, commit `bc6cd410ccb26cacc5fbd422f8198371bd272892`. The text decoding
and activity/UI paths described below are the same in both versions; the
current mirror has additional retry/citation/provenance handling, none of which
changes the trace text reader contract.

| Reader / surface | Code path | Field and absence behavior (both versions) |
| --- | --- | --- |
| Event storage payload | `storage/agent_events_service.py`: `_row_to_payload`, `list_session_events` | Top-level `text = content_text or content_json.text or ''`. Missing JSON text is already supported, without a new reader shim. Structured `content` remains the parsed JSON object. |
| Web activity API | `vibe/ui_server.py`: `sessions_activity`; `storage/agent_activity_service.py`: `_timeline`, `_make_group`, `_build_groups`, `get_turn_group` | Detail rows take the event payload's top-level `text`; absent text becomes `''`. Summary counts/order do not depend on text. JSON is used for `hosted`, not tool-call display text. |
| Web activity UI | `ui/src/lib/agentActivity.ts`: `groupFromWire`, `activityRowFromMessage`, `parseToolCall`; activity components | Historical rows use `r.text`; live rows use `msg.text ?? ''`; tool names/arguments/raw fallback are parsed from that text. Hosted cards use `content.hosted`. No JSON-text requirement. |
| Live activity publication | `core/message_mirror.py`: `_publish_activity_event` | Publishes `event_row.get('text') or ''` from the append payload; forwards structured `content` separately. No re-read or JSON-text lookup. |
| Web conversation transcript / inbox | `sessions_messages`, `storage/messages_service.py` | Reads the `messages` table, not these trace rows. Tool-call traces are deliberately absent from conversation/inbox payloads. |
| `vibe data query` | `storage/read_only_query.py`: `run_read_only_query`, `RECALL_VIEWS` | Raw SQL can select `agent_events.content_text`, which stays populated. `transcript` and `turns` read messages/turns, not tool-call traces; `sessions` reads session metadata. Raw `json_extract(content_json, '$.text')` now returns SQL NULL for new traces; it is not a built-in rendering path. |
| Digests / recall | `storage/agent_transcript.py`: `_record_digest`; `storage/read_only_query.py`: `digests` | Checkpoint digest columns, or first-input/latest-reply `messages.content_text`. Mirror tool-call traces are not a digest source. |
| Search | `vibe/ui_server.py`: `search_messages_list`; `storage/messages_service.py`: `search_messages`; `core/chat_discovery.py` | Conversation search uses message/session fields, not these traces. Direct SQL search on the retained trace `content_text` remains possible. |
| Retention | `storage/agent_events_retention.py`: `eligible_filter`, `plan`, `_delete_batch`, `run_skill_retention` | Selection/deletion uses event type, visibility, timestamp, and identity, not text. Size estimates sum UTF-8 bytes in text/JSON/metadata with NULL treated as zero. Smaller new rows automatically reduce estimates. |
| IM delivery / mirroring | `core/message_dispatcher.py`, `core/message_mirror.py` | IM delivery uses the in-memory formatted text; the mirror is a persistence sink, not an IM replay source. Message receipt lookups use `messages`, not tool-call traces. |
| Context / fork recovery | `storage/agent_transcript.py`: context-row readers; `modules/agents/vibey/agent.py`: `_committed_tool_result`; `core/services/session_fork.py`: `_latest_source_message_anchor` | Canonical context reads non-null `context_seq`/context payloads, not mirror traces. Hosted tool-result readback parses its canonical `message`/`details`, not trace text. Fork boundary lookup reads silent-terminal identity/timestamp/metadata, not text. |
| Cache diagnostics | `core/handlers/cache_metrics.py`: `report`; `storage/cache_metrics.py`: `CACHE_VIEWS` | Reads JSON components/reason for `prompt.pending`/`prompt.applied`, or cache/usage for `model_attempt`/`fork_turn`. These filters do not read tool-call trace text. |
| Schema migrations / lifecycle | `0072_session_digests._backfill`, `0044_session_delivery_foundation`; `core/chat_discovery.py`, `storage/sessions_service.py` | Digest backfill reads context compactions; the delivery migration's text fallback reads legacy messages, not new trace rows. Lifecycle paths delete/check events by identity/type without rendering text. |
| Exports / diagnostics | SQL query output; backend transcripts, prompt/model-request exports; `core/show_git.py` | No dedicated mirror-trace text export reader. SQL exports preserve `content_text`; native/model-request exports use their own canonical sources. Show Git and delivery snapshots use messages, and Show-session events use their own table. |

A caller authorized to query raw events and choosing only the JSON text path
must switch to `content_text` (or
use the same column-first fallback as the released reader). Lower-privilege
roles cannot query the whole events table. This change does not
claim to preserve an intentionally removed raw duplicate field. No shipped
reader needs it.

## Other trace writers

The mirror writes `tool_call` and `silent_terminal`, not `skill.*`.
`silent_terminal` has no rendered text at all: `content_text=NULL`,
`content_json={}`, and terminal-outcome metadata. `storage/skill_observability.py`
writes catalog/load observations directly, with `content_text=NULL`; their JSON
contains structured observations and body-byte counts, not a duplicated loaded
skill body or display text. No optimization is needed for either family.

Hermetic samples (one catalog entry / successful load of the test `docs` skill):

| Type | `content_text` bytes | `content_json` bytes | `metadata_json` bytes | Change |
| --- | ---: | ---: | ---: | --- |
| `silent_terminal` | 0 (NULL) | 2 | 33 | None |
| `skill.load_result` | 0 (NULL) | 366 | 218 | None |
| `skill.catalog_result` | 0 (NULL) | 411 | 218 | None |

These are fixture sizes, not production averages; catalog size grows with its
entries. Prompt diagnostics and canonical audit/context writes also remain
outside this change.

## Byte evidence

Three synthetic but ordinary tool calls were rendered with the real
`AvibeFormatter.format_toolcall`, persisted through the new mirror, then fed to
the unchanged 3.2.4 append function to measure the old duplicate representation:

- `bash`: a multiline Python command inventorying activity tests and printing
  JSON, with timeout/watch arguments (696 UTF-8 bytes of rendered text).
- `read`: a source path with offset/limit (83 bytes).
- `write`: a Markdown note with English/Chinese text and newlines (338 bytes).

Payload bytes below are `length(CAST(content_text AS BLOB)) +
length(CAST(content_json AS BLOB)) + length(CAST(metadata_json AS BLOB))`, counting
NULL as zero, as in the retention estimator. No character-count approximation.

| New trace sample | Old JSON bytes | New JSON bytes | Old payload bytes | New payload bytes | Saved per row |
| --- | ---: | ---: | ---: | ---: | ---: |
| Multiline bash | 760 | 21 | 1,487 | 748 | 739 (49.7%) |
| Read | 132 | 21 | 246 | 135 | 111 (45.1%) |
| Unicode write | 480 | 21 | 849 | 390 | 459 (54.1%) |

Including every TEXT column in those same new rows gives 1,645 → 906, 404 → 293,
and 1,007 → 548 bytes respectively; the baseline changes only the serialized
JSON text member. SQLite record headers, B-tree/index/page overhead and WAL
write amplification are **not** included. These are per-row logical savings,
not a promised physical database shrink or a workload-average daily saving.
Old rows do not shrink, and normal retention is unchanged.

## Verification

- The existing mirror writer test now asserts the absence of JSON text. It
  failed against the pre-change writer specifically because the duplicate was
  present, then passed with the new serialization rule.
- API coverage parametrizes column-only, historical dual-copy, and JSON-only
  traces; all return identical tool text through activity detail and remain out
  of the conversation transcript. Live mirror publication and hosted-card tests
  still pass.
- Append-boundary tests protect explicit JSON/non-target event contracts and
  prove an already-persisted dual-copy row is unchanged by subsequent appends.
- Focused local runs: **131 passed** across event storage, mirror, activity,
  retention, and the activity HTTP endpoint; **168 passed** across canonical
  transcript, read-only queries/recall, skill observations, and mirror retries.
  Changed-file Ruff and `git diff --check` pass.
- A separate hermetic experiment exported actual new mirror rows and their
  API/live projections, imported those rows unchanged into a fresh database
  owned by an archive of **v3.2.4**, and executed that release's unmodified
  storage, activity HTTP handlers, conversation HTTP handler, SQL query reader,
  and `_publish_activity_event`. Full summary/detail/transcript responses and
  live text/kind projections matched the current responses exactly. The
  historical writer also produced the before-byte counts above. This checked
  the actual released paths, not a reimplemented fallback.
- The old/new UI activity projection module is byte-identical; its historical
  and live row conversion reads the top-level text exercised above. No frontend
  change or UI build is required. This is a stored-row compatibility check, not
  an attempt to bypass unrelated schema/runtime downgrade requirements.

All probes used temporary test-owned homes/databases. No service restart,
deployment, user-state access, or data rewrite was performed.
