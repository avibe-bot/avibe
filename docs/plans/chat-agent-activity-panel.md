# Chat Agent Activity Panel (intermediate progress in Web Chat)

Status: approved design, implementation dispatched (2026-07-17)
Owner decisions locked: global toggle in Settings › Messaging; history turns included;
assistant interim text shown in full; tool calls summary-first with expandable detail;
no token-level streaming; no IM-side changes.

## Background

Research-desk users want to watch what the agent is doing during a turn:
intermediate assistant messages and tool calls. Today the Web Chat shows only a
three-dot ThinkingBubble while `working`, and the reply arrives atomically.

Key fact (verified 2026-07-17): the data pipeline already exists end-to-end.
All three backends emit intermediate `assistant` / `toolcall` via
`controller.emit_agent_message` → `core/message_mirror.py persist_agent_message`
persists them (`assistant` → `messages` table; `toolcall` → `agent_events` table
with `turn_id`/`sequence`/`content_json`) → `message.new` is published on the bus
→ SSE `/api/events` reaches the browser. The frontend currently **drops** these
rows in `isTranscriptMessage` (`ui/src/components/workbench/ChatPage.tsx`).
This feature is therefore: group-and-render what is already flowing + one
settings toggle + a small history read endpoint. Default off = exact current UI.

## Design source of truth

- design.pen frames (avibe-docs `design.pen`): `BoX4o` "Chat · Agent 执行过程 Activity Panel (P1)"
  spec board (states A–F + anatomy + interaction rules) and `s3SZt` full-page
  in-context mock. Exported PNGs for implementation reference:
  `~/vibe-remote-project/_tmp/activity-design/BoX4o.png` and `s3SZt.png`.
- Match the existing chat visual language: message avatar block, `bg-mint/[0.09]`-family
  tints, muted borders, JetBrains Mono for timings/metadata. Map to existing
  tokens/classes; no hardcoded one-off colors.

## UX specification

One new transcript row kind: **ActivityGroup** — one per agent turn, positioned
where that turn's reply renders (directly above the terminal message).

States:

1. **Running · compact (default)** — replaces ThinkingBubble when the toggle is
   on and the running turn has ≥1 activity row (falls back to ThinkingBubble
   while empty). Card = header + fixed-height viewport (~3 rows, ≈110px):
   header: spinner + "Running" + step count + elapsed + chevron;
   body: newest rows scroll in from the bottom, older rows fade upward;
   auto-follows the tail. The card itself never grows: no transcript reflow,
   existing scroll anchoring untouched.
2. **Running · expanded** — click header: max-height ≈40vh, internal scroll,
   auto-follow latest; manual scroll-up pauses follow and shows a "jump to
   latest" pill (reuse Transcript's existing jump logic/pattern). Stays
   expanded for the rest of the turn until collapsed.
3. **Done · collapsed (default)** — on terminal `result`: collapse to a single
   chip "✓ Activity · N steps · 1m 23s" hugging the final reply from above.
   Click to expand/collapse anytime. Expansion state is not persisted.
4. **History turns** — same chip on past turns (including after refresh).
   Detail is lazy-loaded on first expand; never loaded for the initial
   transcript render.
5. **Interrupted / failed** — chip variants: "Interrupted · stopped at step N"
   (gold icon) when the turn ends without a terminal result (Stop/override);
   "Failed · N steps" (destructive icon) on error result. Error details keep
   flowing through the existing error bubble — never into the activity panel.

Event rows:

- **Tool row**: icon (by tool-name prefix: terminal/file-text/pencil/globe/bot,
  fallback wrench) + tool name (mono) + one-line summary + duration when
  available. Summary = first line of the already-formatted toolcall text
  (`format_toolcall` output) — never raw tool input/output dumps.
  **Click row → expand/collapse inline detail block** showing the full stored
  toolcall text (mono, from `agent_events.content_json`). v1 does not read
  native transcripts; if only the summary line was stored, that is what shows.
- **Assistant interim row**: sparkles icon + **full text** (owner decision —
  users read the content). Wrapped, rendered with the existing `Markdown`
  component in a compact style (smaller font); no truncation. In the compact
  running viewport the fixed window simply follows the tail.

Grouping rules:

- Anchor by turn boundaries, not by user messages: a group collects
  `assistant`/`tool_call` rows between the previous terminal row and the next
  terminal (`result`/error) row; live turn = rows arriving while `working`.
  This also covers agent-initiated turns (no inbound user message) and queued
  turns (each turn groups independently).
- Reconnect/refresh mid-turn: persisted interim rows of the running turn
  re-hydrate the group; empty group falls back to ThinkingBubble.

Settings:

- `config.ui.show_agent_activity` (bool, default `false`), server-persisted via
  the existing `config.ui.*` path (same as `chat_message_font_size`), edited in
  Settings › Messaging with `SettingsRow` + `ToggleSwitch`.
- Independent from IM `agent_progress_style` / status bubble — no IM changes.

Out of scope (v1): token-level streaming (all three backends' delta handlers
are unimplemented), drill-down beyond the stored toolcall text, IM platforms,
per-chat override toggle, persistence of expand/collapse state.

## Implementation notes (grounded pointers, verify before relying)

Frontend (`ui/src`):

- `components/workbench/ChatPage.tsx`: `isTranscriptMessage` currently filters
  `assistant`/`tool_call`; `Transcript`/`MessageRow`/`ThinkingBubble`;
  `working` state; jump-to-latest + scroll-anchor logic; `MAX_RETAINED_MESSAGES = 300`
  (mind the retained-window interplay — activity rows must not silently evict
  visible conversation rows; group or cap accordingly).
- SSE: `context/ApiContext.tsx` `connectWorkbenchEvents` (`message.new`,
  `turn.start`, `turn.end`, `session.status`).
- Settings page: `components/settings/SettingsMessagingPage.tsx`,
  primitives in `components/settings/SettingsPrimitives.tsx`.
- i18n: every new string in both `ui/src/i18n/en.json` and `zh.json`.
- Collapse pattern precedent: the harness chip branch in `MessageRow`.

Backend:

- **Verify early (evidence-first, run it, don't assume)**: whether `tool_call`
  rows currently arrive at the browser via `message.new` and/or the transcript
  fetch, and what `content_json` carries for each backend. Adapt: live path
  should reuse `message.new`; if tool_call rows are absent from any needed
  path, extend at the chokepoint (`message_mirror` publish / transcript API),
  not per-backend.
- History lazy-load: a small read endpoint on `vibe/ui_server.py` (native
  FastAPI async, no per-request `asyncio.run()`) serving a turn's activity rows
  from `messages` + `agent_events` (`agent_events_service`). Endpoint shape is
  the lane's choice; document it in the PR description.
- Config default + plumbing for `ui.show_agent_activity` through bootstrap
  config like `chat_message_font_size`.

## Acceptance criteria (owner ~10 min)

1. Toggle exists in Settings › Messaging, default off; off = pixel-identical
   current behavior (ThinkingBubble and all).
2. On: sending a message shows the compact card in the reply position, live
   tool summaries + full interim assistant text, fixed height, no layout jump.
3. Header click expands (~40vh, internal scroll); scroll-up pauses follow and
   shows jump-to-latest; tool row click toggles full call text.
4. On completion the card collapses to the one-line chip above the final reply;
   chip toggles open/closed.
5. Past turns (after refresh) show chips; first expand lazy-loads detail.
6. Stop mid-run → "Interrupted" chip; failing turn → "Failed" chip with error
   bubble unchanged.
7. IM platforms behave exactly as before; `npm run build` green; touched
   backend files' per-file pytest green; ruff clean.

## As-built implementation notes (PR #934)

Implemented on branch `feat/chat-agent-activity-panel`. The three items below
record where the build diverged from or sharpened the plan above.

### (a) The `message.new` live-push gap → gated publish at the chokepoint

The plan's "Key fact" assumed intermediate `assistant` / `tool_call` rows already
reach the browser via `message.new`. **Verified false.** `persist_agent_message`
(`core/message_mirror.py`) persists them but publishes **no** `message.new` — the
live stream deliberately carries only transcript types
(`TRANSCRIPT_TYPES = user/harness/result/notify/error`). Proven by the persist
contract tests `test_persist_agent_intermediate_persisted_but_not_streamed` and
`test_persist_agent_toolcall_avibe_writes_event_without_streaming`.

Per the plan's own "verify early / extend at the chokepoint, not per-backend"
guidance, the live path is extended **only** in `core/message_mirror.py`, gated on
`config.ui.show_agent_activity`:

- **off (default) → byte-identical no-op.** No extra publish; `tool_call` still
  writes only to `agent_events`, `assistant` still stays out of the stream; the
  existing mirror contract tests are unchanged.
- **on →** interim `assistant` rows publish `message.new` (the persisted row), and
  `tool_call` events (which live in `agent_events`, never `messages`) publish a
  **synthesized** `message.new` payload shaped like a `messages` row
  (`type='tool_call'`), so the one existing browser consumer (ChatPage) can route
  it into the activity store. `inbox.session.updated` and web-push are **not**
  emitted for these rows (still process log, not a reply).

The gate reads `settings.load_config_or_default().ui.show_agent_activity` behind a
short TTL cache keyed by config path (no per-emit disk parse; the path key keeps a
fresh `AVIBE_HOME` — e.g. per test — from reading a stale value). No IM /
`core/message_dispatcher.py` / per-adapter edits.

Config plumbing mirrors `chat_message_font_size`: `UiConfig.show_agent_activity:
bool = False` in `config/v2_config.py` (+ a `bool(...)` coercion in `from_payload`),
surfaced automatically through `to_dict` / `api.config_to_payload`, saved via the
deep-merged `POST /api/config` (a partial `{"ui": {...}}` patch preserves siblings),
read by ChatPage from `bootstrap.config.ui.show_agent_activity`.

### (b) History read endpoint

`GET /api/sessions/<id>/activity` (native async FastAPI in `vibe/ui_server.py`,
reads `messages` + `agent_events` via storage services; 404 on unknown session):

- **summary** (no params) — one entry per turn with ≥1 activity row, no row text:
  `{"groups": [{"id", "anchor_message_id", "anchor_position", "open", "status",
  "steps", "started_at", "ended_at", "duration_ms"}]}`. Loaded once on chat open so
  past turns show a chip.
- **detail** (`?group_id=<id>`) — that one group plus `"rows": [{"id", "kind"
  (`assistant`|`tool_call`), "text", "created_at"}]`. The lazy expand; 404 on an
  unknown group id.

`status ∈ done | failed | interrupted`. `id` is the group's first-activity-row id
(stable key across summary/detail).

**Anchoring invariant (corrected — was a P1 rendering bug, fixed post-#934).** A
group is positioned relative to a transcript message that is **at or before the
group's own end — never a future message**:

- **done / failed** → `anchor_message_id` = the turn's terminal reply (`result` /
  `error` / backend-failure `notify`), `anchor_position = "before"` (the chip hugs
  the reply from above).
- **interrupted** → `anchor_message_id` = the boundary immediately *before* the
  turn's activity (its triggering user/harness message, tracked as
  `last_boundary_id`), `anchor_position = "after"` (the chip sits just below the
  trigger). It is NEVER anchored to the next turn's opener (a future message) and
  NEVER to the transcript tail.
- `open` = true only for the last un-terminated turn. The frontend promotes the
  `open` group into the tail **live running card** while the turn is running;
  otherwise it renders as an interrupted chip after its trigger. **The transcript
  tail is reserved exclusively for the live card** — a settled/interrupted chip is
  never placed there. `anchor_message_id` is `null` only in the degenerate
  no-prior-message case (rendered at the top, never the tail).

Original bug: interrupted turns anchored *forward* to the next turn's opening
message; while that message did not yet exist (or was minted much later) the
frontend fell back to the tail slot, so the "Interrupted" chip rendered below newer
messages and even below the next turn's live card. The backward-anchor invariant
fixes it at source. Grouping logic lives in `storage/agent_activity_service.py`; the
low-level read is `agent_events_service.list_session_events`.

### (c) Timestamp-parsed turn-boundary grouping

`messages` has **no `turn_id`** column (only `agent_events` does), so a turn cannot
be reconstructed by id. Instead a turn is bounded by transcript markers: it ends at
the agent's terminal reply (`result` / `error` / backend-failure `notify`), or — if
the user opened a new turn without one — is reported `interrupted`. Activity rows
between boundaries form the group.

Both tables write **whole-second** `...Z` timestamps, so a fast turn's tool call
and the terminal reply that followed it tie on time. The two sources are merged
into one timeline sorted by `(parsed_timestamp, phase)` where phase orders
turn-start < activity < terminal — this keeps a same-second tool call inside its
completed turn instead of sorting it after the terminal (which would orphan it to
the next turn or a spurious interrupted chip). Timestamps are **parsed**, not
string-compared, so the ordering stays correct if a writer ever changes precision.
Show-Page `assistant` marks (`metadata.source == 'show_page'`) are excluded from
activity (they belong to the transcript). The scan is bounded to the recent tail
(most-recent 500 messages / 2000 tool-call events, cap documented not silent), and
tool-call events that predate the oldest scanned message are dropped — otherwise an
event whose turn boundary was cut off would anchor a bogus chip to the first
visible turn.

Frontend (single-source-of-truth model — the durable endpoint owns all SETTLED
groups; the live SSE buffer drives ONLY the in-flight running card). The running
card is a pure function of ``working`` AND the current-generation buffer
(`shouldShowRunningCard`) — so a stale buffer is invisible the moment ``working``
goes false. The live buffer never leaves that card: there is deliberately no
client-side group reconstruction. On every settle signal — a terminal
`message.new`, `turn.end`, SSE reconnect, visibilitychange, OR the `/turn-state`
idle poll recovering a dropped terminal (the fifth signal, same contract) —
`refreshActivity` rebuilds `activityGroups` from `GET /activity` and, when no
in-flight turn remains, clears the live buffer so the finished card swaps to the
storage-derived chip (and its rows can't leak into the next turn). When a turn IS in flight, refresh re-hydrates the running card's rows
from storage only if the live stream hasn't already filled them. Settle bursts are
coalesced to one in-flight + at most one trailing fetch (`scheduleActivityRefresh`),
a transient fetch failure schedules exactly one bounded retry, and nothing fetches
when the toggle is off. The live buffer is a pure **generation** state machine
(`liveActivityReducer`, unit-tested): a monotonic generation bumps on every
turn.start (and on the first agent-initiated row after a settle), so a stale buffer
is invisible by construction and a late settle-refresh only clears/rehydrates its
OWN generation — a newer turn's resolution is a structural no-op (no
promise-cancellation or grace-timer bookkeeping). This structure closes the whole
class of live-state edge cases (lossy/stale/partial buffer, interrupted anchoring,
duration source, settle-vs-next-turn races) that iterative patching kept surfacing:
the chip's steps, status, anchor, and duration always come from the well-tested
backend grouping. Activity rows still live in a **separate store** (never the
`messages` array), so they never count against `MAX_RETAINED_MESSAGES`. Show-Page
transcript rows (`user` AND `assistant`, `metadata.source='show_page'`) are excluded
from both the backend grouping (never turn openers / activity) and live ingestion.
A lazy detail-fetch failure surfaces a retry affordance rather than a false "no
activity". The activity-streaming flag cache is reset on config save so the toggle
takes effect immediately. Presentation is `ActivityCard` / `ActivityChip` in
`ui/src/components/workbench/AgentActivityGroup.tsx`; pure helpers + the live
generation reducer + wire mapping in `ui/src/lib/agentActivity.ts`.

**Compact running-card height (refined UX, design.pen State A updated).** The
compact viewport height is **min(content, 3-row cap ≈ 110px)** — NOT a constant
reserved height. Below the cap the body is exactly content-tall and grows downward
as rows arrive (natural, like any new message at the transcript tail), so a 1–2 row
turn shows no blank space above the content. Only once content reaches the cap does
it become the constant-height viewport: clamped height, newest rows pinned to the
bottom (`justify-end` + `overflow-hidden` clipping the top), older rows fading up
(the top gradient renders ONLY at the cap). The cap is a CSS `max-height`; a small
layout measurement (`offsetHeight >= cap`) gates the fade, so the height behavior
itself is CSS-driven — not unit-testable under jsdom, which does no layout. Expanded
mode is unchanged: `max-h-[40vh]` + internal scroll already fits content up to its
cap. Tail auto-follow (the Transcript scroller) is untouched.

## As-built implementation notes (P2 — items A–E)

Owner-approved iteration package (design signed off 2026-07-20). Frontend-only
except one new display-config field. NO-TOUCH held: IM adapters,
`message_dispatcher`, mirror publish logic, endpoint grouping.

### A — Tool-row summary v2 (3-tier degrade, frontend-only parse)

`ActivityRow.text` for a tool call is the backend `format_toolcall` STRING
(`modules/im/formatters/base_formatter.py`): `` 🔧 `ToolName` `{compact json}` ``.
Names/arg-keys differ per backend (Claude Capitalized `file_path`/`command`; Codex
`bash`/`file_change` with `file`+`type`; OpenCode lowercase `file_path||path`; a
restore path emits `` `name`: `arg` `` with no JSON) — hence the degrade. Pure
helpers in `ui/src/lib/agentActivity.ts`: `parseToolCall` (name + parsed JSON args
or `null`), `toolRecipe` (tier-1 known-tool recipe: command → `$ cmd`; read → dir
muted + basename; edit/write → basename + op badge, op from name or Codex `type`;
web/search/grep → quoted query/URL; task → description — case-insensitive prefix,
paths probe `file_path`→`path`→`file`), `genericChips` (tier-2 ≤3 scalar chips +
overflow), `toolSummary` (tier-3 raw, unchanged). Any parse failure / non-object /
oversize (>20k) → `args: null` → tier 3; `ActivityToolRow` also wraps the parse in
try/catch so nothing can blank a row. Rendered by `ToolSummary` in
`AgentActivityGroup.tsx`. Unit-tested per tier + exception fallback in
`agentActivity.test.ts`.

### B — Tool-row visibility eye toggle + `config.ui.show_tool_calls`

New bool `UiConfig.show_tool_calls` (default **true**), coerced in
`V2Config.from_payload` like `show_agent_activity`. Both runtime serializers are
`__dict__`-based so it rides along automatically and the #939 serializer-coverage
guard is satisfied with no serializer edits (it derives fields from
`fields(UiConfig)`). Display-only → NO `message_mirror` cache / reset hook. The eye
pill (`ToolsEyePill`, eye/eye-off + "Tools"/"工具", icon-only < `sm`) sits in the
running-card header and the expanded-panel header (never the collapsed chip); it
flips the same config the Settings toggle writes (global, cross-device) via
`api.saveConfig({ ui: { show_tool_calls } })`, threaded through the `activity` prop
in `ChatPage`. Filter is the pure `filterActivityRows` (assistant narration ALWAYS
shows; step counts use the unfiltered length). All-filtered placeholders:
expanded → "已隐藏 N 条工具调用"; compact LIVE → "工具调用已隐藏 · 进行中" (never
falls back to ThinkingBubble). Settings row added in `SettingsMessagingPage.tsx`
(`dashboard.showToolCalls*`). i18n en+zh.

### C — Done/history expanded height cap

The expanded settled panel (`ActivityChip`, done/interrupted/failed incl. history)
body is `max-h-[60vh] overflow-y-auto` → height = min(content, ~60vh), opening at
the TOP (natural `scrollTop=0`, no tail-follow). The LIVE running card's expanded
scroller is unchanged (`max-h-[40vh]`, follows tail). Page-level scroll anchoring
untouched.

### D — Inline detail v2 + full-JSON dialog

Row-click detail (`ToolDetail`) renders the SAME parse as A: a kv table (key mono
muted; long/multiline values as wrapping code blocks; `timeout` ms humanized);
parse failure → raw text (unchanged fallback). A "{ } JSON" button opens
`ToolJsonDialog` — the shared `components/ui/dialog` + the lazy `preview-json`
viewer FileViewer uses (zero new deps), scrollable, copy button
(`copyTextToClipboard(JSON.stringify(args,null,2))`), ESC/backdrop close. Parse
failure → dialog shows raw text, still copyable.

### E — Settings copy

`dashboard.showAgentActivityHint` retrimmed per owner dictation (dropped the
Web-only / default-off tail) in en + zh.

### Verification & real-browser residual

`ui` build green; `vitest` full suite green (A/D parse tiers + B filter logic +
existing reducer/wire tests); backend `pytest` green (new
`test_show_tool_calls_defaults_on_and_round_trips` + the #939 serializer-coverage
guard auto-covering the new field); `ruff` clean; changed-file `eslint` clean.
**jsdom does no layout**, so these need the owner's Incus/real-browser pass: the C
60vh scroll feel + top-open, the compact fade-at-cap, the eye-pill live toggle +
cross-device persistence, the D JSON dialog open/copy/close, and the tier-1/2/3
rendering against real per-backend `format_toolcall` output.
