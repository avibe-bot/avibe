import type { WorkbenchMessage } from '../context/ApiContext';

// One turn's activity, as rendered by the Chat Activity panel. Mirrors the
// backend ``storage/agent_activity_service.py`` shape (see the /activity endpoint).
export type ActivityStatus = 'running' | 'done' | 'failed' | 'interrupted';

export type ActivityRow = {
  id: string;
  kind: 'assistant' | 'tool_call';
  text: string;
  created_at: string;
};

// A group is positioned relative to a transcript message that is AT OR BEFORE the
// group's own end (never a future message): done/failed anchor to their terminal
// reply with ``anchorPosition: 'before'`` (the chip hugs the reply from above);
// interrupted anchor to the boundary before their activity (the turn's trigger)
// with ``anchorPosition: 'after'`` (the chip sits just below the trigger). ``open``
// marks the last un-terminated turn — the ONLY group the frontend may promote into
// the tail live card while it is still running; the transcript tail is otherwise
// reserved exclusively for that live card. ``anchorMessageId`` is null only in the
// degenerate no-prior-message case (rendered at the top, never the tail). ``rows``
// is present once loaded (live snapshot or lazy fetch); absent = summary only.
export type ActivityGroup = {
  id: string;
  anchorMessageId: string | null;
  anchorPosition: 'before' | 'after';
  open: boolean;
  status: ActivityStatus;
  steps: number;
  durationMs: number | null;
  startedAt?: string | null;
  rows?: ActivityRow[];
};

// Wire shape from GET /api/sessions/<id>/activity (summary group + optional rows).
export type TurnActivityGroupWire = {
  id: string;
  anchor_message_id: string | null;
  anchor_position: 'before' | 'after';
  open: boolean;
  status: ActivityStatus;
  steps: number;
  duration_ms: number | null;
  started_at?: string | null;
  ended_at?: string | null;
  rows?: Array<{ id: string; kind: 'assistant' | 'tool_call'; text: string; created_at: string }>;
};

export const groupFromWire = (wire: TurnActivityGroupWire): ActivityGroup => ({
  id: wire.id,
  anchorMessageId: wire.anchor_message_id ?? null,
  anchorPosition: wire.anchor_position === 'before' ? 'before' : 'after',
  open: Boolean(wire.open),
  status: wire.status,
  steps: wire.steps,
  durationMs: wire.duration_ms ?? null,
  startedAt: wire.started_at ?? null,
  rows: wire.rows?.map((r) => ({ id: r.id, kind: r.kind, text: r.text, created_at: r.created_at })),
});

// A live ``message.new`` of type assistant/tool_call → an activity row (the live
// stream only carries these when ``show_agent_activity`` is on, see message_mirror).
export const activityRowFromMessage = (msg: WorkbenchMessage): ActivityRow => ({
  id: msg.id,
  kind: msg.type === 'tool_call' ? 'tool_call' : 'assistant',
  text: msg.text ?? '',
  created_at: msg.created_at,
});

// ===== Live running-card buffer: a pure state machine (state, not timing) =====
// The live buffer drives ONLY the in-flight running card; all SETTLED groups come
// from the durable endpoint. Each turn is tagged with a monotonic GENERATION so
// that a stale buffer is invisible by construction and a late settle-refresh is a
// structural no-op for a newer turn:
//   - the running card renders only while ``working`` AND ``rows`` are non-empty,
//     and ``rows`` always belong to the current generation (cleared on every bump);
//   - a settle refresh is issued for a generation and only clears/rehydrates the
//     buffer when it resolves for that SAME generation (a newer turn bumped it → the
//     resolution is dropped). This subsumes the "stale/late buffer" class without
//     promise-cancellation or grace-timer bookkeeping.
export type LiveActivityState = {
  gen: number; // current turn generation (monotonic)
  settled: boolean; // the current generation has settled (terminal / turn.end seen)
  rows: ActivityRow[]; // current-generation buffer (empty ⇒ nothing to show)
  startedAt: number | null; // elapsed-clock start for the running card
};

export const initialLiveActivity = (): LiveActivityState => ({
  gen: 0,
  settled: false,
  rows: [],
  startedAt: null,
});

export type LiveActivityEvent =
  | { type: 'turn_start' }
  | { type: 'row'; row: ActivityRow; now: number }
  | { type: 'settle' }
  | { type: 'clear_for_gen'; gen: number }
  | { type: 'rehydrate_for_gen'; gen: number; rows: ActivityRow[]; startedAt: number };

// The running card is a PURE FUNCTION of (working, current-generation buffer): it
// shows only while a turn is in flight AND the buffer is non-empty. The buffer is
// always the CURRENT generation when non-empty (the reducer clears it on every
// bump), so a stale buffer left by a failed/late refresh is invisible by
// construction the moment ``working`` goes false — no separate generation check
// is needed at the render site.
export const shouldShowRunningCard = (
  enabled: boolean,
  working: boolean,
  liveRowCount: number,
): boolean => enabled && working && liveRowCount > 0;

export const liveActivityReducer = (
  state: LiveActivityState,
  event: LiveActivityEvent,
): LiveActivityState => {
  switch (event.type) {
    case 'turn_start':
      // New turn → new generation with a fresh empty buffer (any stale rows from the
      // previous generation are dropped by construction).
      return { gen: state.gen + 1, settled: false, rows: [], startedAt: null };
    case 'row':
      if (state.settled) {
        // First row after a settle with no turn.start = an agent-initiated new turn.
        return { gen: state.gen + 1, settled: false, rows: [event.row], startedAt: event.now };
      }
      return {
        ...state,
        rows: [...state.rows, event.row],
        startedAt: state.rows.length === 0 ? event.now : state.startedAt,
      };
    case 'settle':
      return state.settled ? state : { ...state, settled: true };
    case 'clear_for_gen':
      // A settle refresh resolved with no in-flight turn: clear the finished buffer
      // — but ONLY if still the same generation (a newer turn.start bumped gen, so
      // this resolution is a stale no-op and must not wipe the new turn's rows).
      return event.gen === state.gen ? { ...state, rows: [], startedAt: null } : state;
    case 'rehydrate_for_gen':
      // In-flight re-hydrate from storage, only if still the current generation and
      // the live stream hasn't already filled the buffer.
      return event.gen === state.gen && state.rows.length === 0
        ? { ...state, rows: event.rows, startedAt: event.startedAt }
        : state;
    default:
      return state;
  }
};

export const isActivityMessageType = (type: string): boolean =>
  type === 'assistant' || type === 'tool_call';

// ``format_toolcall`` stores "🔧 `ToolName` `{json params}`" (one string, backend
// formatter output). Parse the tool name (first backtick token, else first word
// after the wrench) and a one-line summary (the remainder of the first line).
const TOOL_GLYPH = /^\s*🔧\s*/u;

export const parseToolName = (text: string): string => {
  const firstLine = (text || '').split('\n')[0].replace(TOOL_GLYPH, '').trim();
  const backtick = firstLine.match(/^`([^`]+)`/);
  if (backtick) return backtick[1].trim();
  const word = firstLine.split(/\s+/)[0] || '';
  return word.replace(/[`:]/g, '').trim();
};

export const toolSummary = (text: string): string => {
  let firstLine = (text || '').split('\n')[0].replace(TOOL_GLYPH, '').trim();
  // Drop the leading tool-name token (backtick-wrapped or bare word).
  const backtick = firstLine.match(/^`[^`]+`\s*/);
  if (backtick) firstLine = firstLine.slice(backtick[0].length);
  else firstLine = firstLine.replace(/^\S+\s*/, '');
  // Unwrap a single surrounding backtick pair for readability.
  const wrapped = firstLine.match(/^`(.*)`$/);
  return (wrapped ? wrapped[1] : firstLine).trim();
};

// Icon category by tool-name prefix (spec: terminal/file-text/pencil/globe/bot,
// fallback wrench). Returns a stable KEY, not a component, so the renderer maps it
// through a static table (avoids creating a component during render).
export type ToolIconKind = 'terminal' | 'edit' | 'file' | 'web' | 'agent' | 'wrench';

export const toolIconKind = (toolName: string): ToolIconKind => {
  // Match by PREFIX, not substring: tool names lead with their category (``Bash``,
  // ``Read``, ``WebSearch``, ``file_change``…). Substring matching mis-fires — e.g.
  // ``ls`` inside "SomethingElse", ``run`` inside "current".
  const name = (toolName || '').trim().toLowerCase();
  const startsWithAny = (prefixes: string[]) => prefixes.some((p) => name.startsWith(p));
  if (startsWithAny(['bash', 'shell', 'terminal', 'exec', 'command', 'run', 'sh'])) return 'terminal';
  if (startsWithAny(['write', 'edit', 'patch', 'create', 'update', 'apply', 'notebook', 'todo', 'file'])) return 'edit';
  if (startsWithAny(['read', 'cat', 'grep', 'glob', 'ls', 'open', 'view', 'list', 'find'])) return 'file';
  if (startsWithAny(['web', 'fetch', 'http', 'browse', 'url'])) return 'web';
  if (startsWithAny(['task', 'agent', 'mcp', 'sub', 'delegate'])) return 'agent';
  return 'wrench';
};

// Duration as {minutes, seconds} (null when unavailable). The unit text is applied
// by the component through i18n (AGENTS.md: no hardcoded user-facing units), so the
// zh chip renders localized units rather than a hardcoded "1m 23s".
export const activityDurationParts = (
  ms: number | null | undefined,
): { minutes: number; seconds: number } | null => {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  const totalSeconds = Math.round(ms / 1000);
  return { minutes: Math.floor(totalSeconds / 60), seconds: totalSeconds % 60 };
};
