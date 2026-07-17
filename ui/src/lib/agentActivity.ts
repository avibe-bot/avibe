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

// A group is positioned in the transcript by ``anchorMessageId`` (the chip renders
// directly above that message: the terminal reply for done/failed, the next turn's
// opening message for a history-interrupted turn). ``null`` = trails the transcript
// (the live running card, or an interrupted turn with no following message).
// ``rows`` is present once loaded (live snapshot or lazy fetch); absent = summary
// only (fetch on expand).
export type ActivityGroup = {
  id: string;
  anchorMessageId: string | null;
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

// "1m 23s" for ≥60s, "3.2s" below — matches the design chip metadata (mono).
export const formatActivityDuration = (ms: number | null | undefined): string => {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return '';
  const totalSeconds = ms / 1000;
  if (totalSeconds < 60) {
    // Whole seconds read cleaner once we're past a couple seconds.
    return totalSeconds < 10 ? `${totalSeconds.toFixed(1)}s` : `${Math.round(totalSeconds)}s`;
  }
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.round(totalSeconds % 60);
  return `${minutes}m ${seconds}s`;
};
