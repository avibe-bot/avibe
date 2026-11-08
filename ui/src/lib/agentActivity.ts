import type { WorkbenchMessage } from '../context/ApiContext';
import type { CitationSource } from './citations';
import { hostedActivityFrom, type HostedActivity } from './hostedActivity';
import { specFor } from './messageTypes';
import { timestampOrderTimeMs } from './transcriptOrder';

// One turn's activity, as rendered by the Chat Activity panel. Mirrors the
// backend ``storage/agent_activity_service.py`` shape (see the /activity endpoint).
export type ActivityStatus = 'running' | 'done' | 'failed' | 'interrupted';

// One tool step's state, from Vibey's canonical projection (``core/tool_activity.py``).
// Legacy traces and other backends' rows carry none of these step fields.
export type ActivityStepStatus = 'pending' | 'running' | 'completed' | 'failed' | 'interrupted' | 'unknown';

export type ActivityRow = {
  id: string;
  kind: 'assistant' | 'tool_call';
  text: string;
  created_at: string;
  // Authoritative position: on every durable row, and on a live projected step.
  order_micros?: number;
  // Tiebreak at one ``order_micros`` (a response's block index). Present only on
  // projected steps, so it also marks a row whose position is explicit.
  order_index?: number;
  status?: ActivityStepStatus;
  started_at?: string | null;
  ended_at?: string | null;
  duration_ms?: number | null;
  // Source sidecar for an agent narration row whose text carries citation links
  // (see lib/citations). Absent on every other row, live or durable.
  citations?: CitationSource[];
  // A hosted tool call's card (lib/hostedActivity); its ``text`` is then the IM line.
  hosted?: HostedActivity;
};

type ActivityStepFields = Pick<
  ActivityRow,
  'order_micros' | 'order_index' | 'status' | 'started_at' | 'ended_at' | 'duration_ms'
>;

const STEP_RANK: Record<ActivityStepStatus, number> = {
  pending: 0,
  unknown: 0,
  running: 1,
  completed: 2,
  failed: 2,
  interrupted: 2,
};

const isStepStatus = (value: unknown): value is ActivityStepStatus =>
  typeof value === 'string' && Object.hasOwn(STEP_RANK, value);

// The step fields a row carries: ``content.activity`` on a live message, top-level
// on a durable row. A malformed value is dropped, so that field keeps today's behavior.
const stepFields = (raw: unknown): ActivityStepFields => {
  if (!raw || typeof raw !== 'object') return {};
  const value = raw as Record<string, unknown>;
  const fields: ActivityStepFields = {};
  if (Number.isSafeInteger(value.order_micros)) fields.order_micros = value.order_micros as number;
  if (Number.isSafeInteger(value.order_index)) fields.order_index = value.order_index as number;
  if (isStepStatus(value.status)) fields.status = value.status;
  for (const key of ['started_at', 'ended_at'] as const) {
    if (typeof value[key] === 'string' || value[key] === null) fields[key] = value[key] as string | null;
  }
  if (Number.isFinite(value.duration_ms) || value.duration_ms === null) fields.duration_ms = value.duration_ms as number | null;
  return fields;
};

// Storage owns persisted order, including clocks recovered from migration metadata.
// Live rows without a durable key still carry their emission clock in the id.
const activityRowMicros = (row: ActivityRow): number => {
  if (row.order_micros !== undefined) return row.order_micros;
  const clock = /^[^_]{3}_([0-9a-f]{15})/i.exec(row.id)?.[1];
  if (clock) {
    const micros = Number.parseInt(clock, 16);
    if (Number.isSafeInteger(micros)) return micros;
  }
  return timestampOrderTimeMs(row.created_at) * 1000;
};

const compareKeys = (a: number | string, b: number | string): number => (a < b ? -1 : a > b ? 1 : 0);

// Storage's order within a group: (emission microsecond, block index, id). A row
// without a block index sorts as 0, so rows that lack one keep (time, id).
const compareActivityRows = (a: ActivityRow, b: ActivityRow): number =>
  compareKeys(activityRowMicros(a), activityRowMicros(b))
  || compareKeys(a.order_index ?? 0, b.order_index ?? 0)
  || compareKeys(a.id, b.id);

// Insert a live row at its place. A row with explicit order is placed among all
// rows; one without it is appended as before, moving ahead only of explicitly
// ordered rows that sort after it.
const placeRow = (rows: ActivityRow[], row: ActivityRow): ActivityRow[] => {
  const explicit = row.order_index !== undefined;
  let at = rows.length;
  while (
    at > 0
    && (explicit || rows[at - 1].order_index !== undefined)
    && compareActivityRows(rows[at - 1], row) > 0
  ) at -= 1;
  return [...rows.slice(0, at), row, ...rows.slice(at)];
};

// A step only moves forward: pending (or unknown) → running → settled. A durable
// read and the live stream can deliver an older state of a step after a newer one.
const isOlderStep = (held: ActivityRow, other: ActivityRow): boolean =>
  held.status !== undefined && other.status !== undefined && STEP_RANK[other.status] < STEP_RANK[held.status];

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

export type ActivityForeground = 'idle' | 'running' | 'unknown';

/** Interpret durable groups only after the controller's foreground state is known.
 * An open group is not interrupted evidence by itself: while state is unknown it
 * stays hidden, while running it becomes the live card, and only authoritative
 * idle lets its stored interrupted status render as a settled chip. */
export const activityGroupsForForeground = (
  groups: ActivityGroup[],
  foreground: ActivityForeground,
): { settled: ActivityGroup[]; inflight: ActivityGroup | null } => {
  if (foreground === 'idle') return { settled: groups, inflight: null };

  let inflight: ActivityGroup | null = null;
  if (foreground === 'running') {
    for (let i = groups.length - 1; i >= 0; i -= 1) {
      if (groups[i].open) {
        inflight = groups[i];
        break;
      }
    }
  }
  return { settled: groups.filter((group) => !group.open), inflight };
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
  rows?: Array<{
    id: string;
    kind: 'assistant' | 'tool_call';
    text: string;
    created_at: string;
    order_micros?: number;
    order_index?: number;
    status?: string;
    started_at?: string | null;
    ended_at?: string | null;
    duration_ms?: number | null;
    citations?: CitationSource[];
    hosted?: unknown;
  }>;
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
  rows: wire.rows?.map((r) => ({
    id: r.id, kind: r.kind, text: r.text, created_at: r.created_at,
    ...stepFields(r),
    citations: r.citations,
    hosted: hostedActivityFrom(r.hosted),
  })),
});

// A live ``message.new`` of type assistant/tool_call → an activity row (the live
// stream only carries these when ``show_agent_activity`` is on, see message_mirror).
export const activityRowFromMessage = (msg: WorkbenchMessage): ActivityRow => ({
  id: msg.id,
  kind: msg.type === 'tool_call' ? 'tool_call' : 'assistant',
  text: msg.text ?? '',
  created_at: msg.created_at,
  ...stepFields((msg.content as { activity?: unknown } | null | undefined)?.activity),
  citations: (msg.content as { citations?: CitationSource[] } | null | undefined)?.citations,
  hosted: hostedActivityFrom((msg.content as { hosted?: unknown } | null | undefined)?.hosted),
});

// A retry is never inferred from SSE order. Ask the durable projection to refresh
// interrupted native cards when subsequent activity arrives; reload uses it too.
export const hasUnconfirmedHostedInterruption = (rows: ActivityRow[]): boolean =>
  rows.some((row) => row.hosted?.interruption !== undefined && !row.hosted.retried);

// ===== Live running-card buffer: a pure state machine (state, not timing) =====
// The live buffer drives ONLY the in-flight running card; all SETTLED groups come
// from the durable endpoint. Each Activity phase is tagged with a monotonic GENERATION so
// that a stale buffer is invisible by construction and a late settle-refresh is a
// structural no-op for a newer turn:
//   - the running card renders only while ``working`` AND ``rows`` are non-empty,
//     and ``rows`` always belong to the current generation (cleared on every bump);
//   - a settle refresh is issued for a generation and only clears/rehydrates the
//     buffer when it resolves for that SAME generation (a newer turn bumped it → the
//     resolution is dropped). This subsumes the "stale/late buffer" class without
//     promise-cancellation or grace-timer bookkeeping.
export type LiveActivityState = {
  gen: number; // current Activity phase generation (monotonic)
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
  | { type: 'reset' }
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
    case 'reset':
      // Visibility changes, navigation and output phase boundaries invalidate
      // every in-flight read from the previous visible group without settling work.
      return { gen: state.gen + 1, settled: false, rows: [], startedAt: null };
    case 'row': {
      const at = state.rows.findIndex((row) => row.id === event.row.id);
      if (at >= 0) {
        // A step's state changes arrive under its id: update it in place, in any
        // phase. A row without step state never changes, and storage hydration can
        // include it before its SSE envelope arrives, so its repeat is a no-op.
        const held = state.rows[at];
        if (event.row.status === undefined || isOlderStep(held, event.row)) return state;
        const rows = compareActivityRows(held, event.row) === 0
          ? state.rows.map((row, index) => (index === at ? event.row : row))
          : placeRow(state.rows.filter((_, index) => index !== at), event.row);
        return { ...state, rows };
      }
      if (state.settled) {
        // First row after a settle with no turn.start = an agent-initiated new turn.
        return { gen: state.gen + 1, settled: false, rows: [event.row], startedAt: event.now };
      }
      return {
        ...state,
        rows: placeRow(state.rows, event.row),
        startedAt: state.rows.length === 0 ? event.now : state.startedAt,
      };
    }
    case 'settle':
      return state.settled ? state : { ...state, settled: true };
    case 'clear_for_gen':
      // A settle refresh resolved with no in-flight turn: clear the finished buffer
      // — but ONLY if still the same generation (a newer turn.start bumped gen, so
      // this resolution is a stale no-op and must not wipe the new turn's rows).
      return event.gen === state.gen ? { ...state, rows: [], startedAt: null } : state;
    case 'rehydrate_for_gen': {
      if (event.gen !== state.gen || state.settled) return state;
      // Match storage's order even when its bounded window advances. Stable
      // timestamp-only sorting would move retained tied rows last. Durable rows win
      // overlap so SSE cannot erase storage-only ordering keys, unless the durable
      // read predates a step state the live stream already delivered.
      const rows = new Map(state.rows.map((row) => [row.id, row]));
      for (const row of event.rows) {
        const held = rows.get(row.id);
        if (!held || !isOlderStep(held, row)) rows.set(row.id, row);
      }
      return {
        ...state,
        rows: [...rows.values()].sort(compareActivityRows),
        startedAt: Math.min(state.startedAt ?? event.startedAt, event.startedAt),
      };
    }
    default:
      return state;
  }
};

// Process-log rows for the Activity panel. Assistant rows are real Messages and
// therefore use the catalog. Tool calls are synthetic live envelopes backed by
// ``agent_events``; they intentionally do not exist in the Message catalog.
export const isActivityMessageType = (type: string): boolean =>
  type === 'tool_call' || specFor(type).render === 'activity';

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
  if (startsWithAny(['bash', 'powershell', 'pwsh', 'shell', 'terminal', 'exec', 'command', 'run', 'sh'])) return 'terminal';
  if (startsWithAny(['write', 'edit', 'patch', 'create', 'update', 'apply', 'notebook', 'todo', 'file'])) return 'edit';
  if (startsWithAny(['read', 'cat', 'grep', 'glob', 'ls', 'open', 'view', 'list', 'find'])) return 'file';
  if (startsWithAny(['web', 'fetch', 'http', 'browse', 'url'])) return 'web';
  if (startsWithAny(['task', 'agent', 'mcp', 'sub', 'delegate'])) return 'agent';
  return 'wrench';
};

// Shared duration clock for live and completed activity. Keep the compact MM:SS
// shape below one hour, then expose hours and days instead of letting minutes
// grow into an increasingly hard-to-read total.
export const formatActivityElapsedClock = (ms: number, daySuffix: string): string => {
  const totalSeconds = Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 1000)) : 0;
  const days = Math.floor(totalSeconds / 86_400);
  const hours = Math.floor((totalSeconds % 86_400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (value: number) => String(value).padStart(2, '0');

  const clock = hours > 0 || days > 0
    ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
  return days > 0 ? `${days}${daySuffix} ${clock}` : clock;
};

// ===== Tool-call summary v2 (A/D): 3-tier degrade, frontend-only parse =====
// ``ActivityRow.text`` for a tool call is the backend ``format_toolcall`` STRING —
// ``🔧 `ToolName` `{compact json}` `` (base_formatter.py). Tool names and arg keys
// differ per backend (Claude Capitalized `file_path`/`command`; Codex `bash`/
// `file_change` with `file`+`type`; OpenCode lowercase `file_path||path`; an
// OpenCode restore path emits `` `name`: `arg` `` with no JSON), which is why we
// degrade: tier 1 known-tool recipes → tier 2 generic kv chips → tier 3 raw text.
// Everything here is a PURE function of the row text; any parse failure/oversize
// yields ``args: null`` (→ tier 3), and the component wraps calls in try/catch so
// an unexpected shape can never blank a row.

// A parsed params object is only trusted below this size (compact JSON is tiny;
// anything larger is treated as oversized → tier 3 raw).
const MAX_TOOL_PARAMS_BYTES = 20000;

export type ParsedToolCall = {
  name: string;
  args: Record<string, unknown> | null; // parsed JSON object, or null → tier 3
  raw: string; // full original row text (tier-3 render + JSON-dialog fallback)
};

// Extract the tool name + the embedded compact-JSON params object (when present and
// parseable) from a ``format_toolcall`` string. Never throws.
export const parseToolCall = (text: string): ParsedToolCall => {
  const raw = text || '';
  const name = parseToolName(raw);
  let args: Record<string, unknown> | null = null;
  try {
    let firstLine = raw.split('\n')[0].replace(TOOL_GLYPH, '').trim();
    // Drop the leading tool-name token (backtick-wrapped or bare word).
    const nameToken = firstLine.match(/^`[^`]+`\s*/);
    firstLine = nameToken ? firstLine.slice(nameToken[0].length) : firstLine.replace(/^\S+\s*/, '');
    firstLine = firstLine.trim();
    // Unwrap a single surrounding backtick pair (format_toolcall wraps the JSON).
    const wrapped = firstLine.match(/^`([\s\S]*)`$/);
    const candidate = (wrapped ? wrapped[1] : firstLine).trim();
    if (candidate.startsWith('{') && candidate.length <= MAX_TOOL_PARAMS_BYTES) {
      const parsed: unknown = JSON.parse(candidate);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        args = parsed as Record<string, unknown>;
      }
    }
  } catch {
    args = null;
  }
  return { name, args, raw };
};

const splitPath = (p: string): { dir: string; base: string } => {
  const clean = p.replace(/\/+$/, '');
  const idx = clean.lastIndexOf('/');
  return idx >= 0 ? { dir: clean.slice(0, idx + 1), base: clean.slice(idx + 1) } : { dir: '', base: clean };
};

export type FileOp = 'create' | 'modify' | 'delete';

// Tier-1 render intents (keyed on known tool-name prefix + expected arg presence).
export type ToolRecipe =
  | { kind: 'command'; command: string } // bash/shell → the command body
  | { kind: 'read'; dir: string; base: string } // read/list → dir muted + base bold
  | { kind: 'fileop'; dir: string; base: string; op: FileOp } // edit/write → base + op badge
  | { kind: 'query'; text: string } // web/search/fetch → quoted query / URL
  | { kind: 'text'; text: string }; // task/agent → description

const asStr = (v: unknown): string | undefined => (typeof v === 'string' && v.length > 0 ? v : undefined);

const summarizeShellCommand = (command: string): string => {
  const wrapper = /^(?:\/(?:[\w.+-]+\/)*)?(?:bash|zsh|sh)[ \t]+-(?:lc|c)[ \t]+/.exec(command);
  if (!wrapper) return command;

  // Decode one literal shell argument, not a shell program. Any expansion,
  // extra argument, or operator keeps the original invocation visible.
  const argument = command.slice(wrapper[0].length).replace(/[ \t]+$/, '');
  const parts = /'([^']*)'|"((?:[^"\\$`]|\\[^\r\n])*)"|\\([^\r\n])|([\w@%+=:,./-]+)/gy;
  let summary = '';
  while (parts.lastIndex < argument.length) {
    const part = parts.exec(argument);
    if (!part) return command;
    summary += part[1] ?? part[2]?.replace(/\\([$`"\\])/g, '$1') ?? part[3] ?? part[4];
  }
  return summary.trim() ? summary : command;
};

const fileOpFrom = (name: string, args: Record<string, unknown>): FileOp => {
  const type = (asStr(args.type) || '').toLowerCase();
  if (/^(creat|add)/.test(type)) return 'create';
  if (/^(delet|remov)/.test(type)) return 'delete';
  if (/^(modif|edit|updat|chang)/.test(type)) return 'modify';
  if (name.startsWith('write') || name.startsWith('create')) return 'create';
  if (name.startsWith('delete') || name.startsWith('remove')) return 'delete';
  return 'modify'; // edit/update/apply/patch/notebook default
};

// Known-tool recipe (tier 1). Returns null for unknown tools or a known tool whose
// expected primary arg is absent → the caller falls back to tier 2 (generic chips).
// Backend-agnostic: name matching is case-insensitive and by PREFIX, and paths probe
// ``file_path`` → ``path`` → ``file`` to cover Claude/OpenCode/Codex divergence.
export const toolRecipe = (name: string, args: Record<string, unknown>): ToolRecipe | null => {
  const n = (name || '').trim().toLowerCase();
  const starts = (prefixes: string[]) => prefixes.some((p) => n.startsWith(p));
  const path = asStr(args.file_path) ?? asStr(args.filePath) ?? asStr(args.path) ?? asStr(args.file);
  const command = asStr(args.command) ?? asStr(args.cmd);

  if (starts(['bash', 'powershell', 'pwsh', 'shell', 'exec', 'run', 'sh', 'zsh', 'terminal', 'command'])) {
    return command != null ? { kind: 'command', command: summarizeShellCommand(command) } : null;
  }
  if (starts(['write', 'edit', 'create', 'update', 'apply', 'patch', 'notebook', 'multiedit', 'file_change', 'filechange'])) {
    return path != null ? { kind: 'fileop', ...splitPath(path), op: fileOpFrom(n, args) } : null;
  }
  if (starts(['read', 'cat', 'open', 'view', 'list', 'ls', 'glob', 'find'])) {
    return path != null ? { kind: 'read', ...splitPath(path) } : null;
  }
  if (starts(['web', 'fetch', 'http', 'browse', 'url', 'search', 'grep'])) {
    const query = asStr(args.query) ?? asStr(args.url) ?? asStr(args.pattern);
    return query != null ? { kind: 'query', text: query } : null;
  }
  if (starts(['task', 'agent', 'mcp', 'sub', 'delegate'])) {
    const desc = asStr(args.description) ?? asStr(args.prompt);
    return desc != null ? { kind: 'text', text: desc } : null;
  }
  return null;
};

export type ToolChip = { key: string; value: string };

const truncateValue = (s: string, max = 48): string => (s.length > max ? `${s.slice(0, max - 1)}…` : s);

// A chip-worthy value is a single-line scalar (string/number/bool). Length does NOT
// disqualify — long values are shown truncated (per spec); only multiline strings and
// objects/arrays are excluded (they still count toward the overflow tally).
const isChipScalar = (v: unknown): boolean =>
  (typeof v === 'string' && !v.includes('\n')) || typeof v === 'number' || typeof v === 'boolean';

// Tier-2 generic chips: up to ``max`` scalar params (long values truncated) + an
// overflow count for every remaining param (including the non-chip-worthy ones).
export const genericChips = (
  args: Record<string, unknown>,
  max = 3,
): { chips: ToolChip[]; overflow: number } => {
  const entries = Object.entries(args);
  const chips: ToolChip[] = [];
  for (const [key, value] of entries) {
    if (chips.length >= max) break;
    if (isChipScalar(value)) chips.push({ key, value: truncateValue(String(value)) });
  }
  return { chips, overflow: Math.max(0, entries.length - chips.length) };
};

// B: tool-row visibility filter. Assistant narration ALWAYS shows; only tool_call
// rows are hidden. Pure so the component's placeholder logic is unit-testable and the
// step counts (which use the unfiltered length) stay independent of display.
export const filterActivityRows = (
  rows: ActivityRow[],
  showToolCalls: boolean,
): { visible: ActivityRow[]; hiddenTools: number } => {
  if (showToolCalls) return { visible: rows, hiddenTools: 0 };
  const visible = rows.filter((r) => r.kind !== 'tool_call');
  return { visible, hiddenTools: rows.length - visible.length };
};

export type ToolParam = { key: string; value: string; block: boolean };

// Full ordered kv params for the inline detail table (D). Long/multiline values are
// flagged ``block`` (rendered as a wrapping code block); ``timeout`` in ms is
// humanized inline (e.g. ``70000`` → ``70000 (70s)``) mirroring the design.
export const toolParams = (args: Record<string, unknown>): ToolParam[] =>
  Object.entries(args).map(([key, value]) => {
    let text = typeof value === 'string' ? value : JSON.stringify(value);
    if (key === 'timeout' && typeof value === 'number' && value >= 1000) {
      text = `${value} (${Math.round(value / 1000)}s)`;
    }
    return { key, value: text, block: text.includes('\n') || text.length > 60 };
  });
