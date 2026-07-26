import type { HarnessRun, HarnessSessionSummary } from '../../context/ApiContext';

// Pure mappers behind the Harness run rows (plan §4.1/§4.2). They take ``t``
// rather than calling useTranslation, so the branching is unit-testable without
// a router or an i18n provider — same shape as lib/chatTrigger.ts.

// The run types the store writes today, in the order the type selector lists
// them. Membership is what decides whether a type has a translated label — an
// unknown or newly-added type still renders (as its raw value) rather than
// vanishing, which is also why the default filter is an exclusion, not an
// include-list. ``watch_runtime`` is hidden by default (plan D1): it is a
// watcher-process heartbeat with no session, agent, or message.
export const RUN_TYPES = [
  'agent_run',
  'watch',
  'scheduled',
  'task_run',
  'hook_send',
  'webhook',
  'watch_runtime',
] as const;
export const DEFAULT_HIDDEN_RUN_TYPES = ['watch_runtime'];
const KNOWN_RUN_TYPES = new Set<string>(RUN_TYPES);

// Options for the type selector: the types we have words for, then anything
// else the ledger actually holds.
//
// A hardcoded list alone strands rows. Search skips ``run_type`` on purpose (it
// is a translated chip, not the user's text), so a type with no option is a row
// visible under All and reachable by no filter at all — which is what happened
// to ``webhook``. Naming the known types *and* reading the rest from the server
// means a type invented later is filterable the day it first appears.
export function runTypeOptions(present: readonly string[] | undefined): string[] {
  const extras = (present ?? []).filter((type) => type && !KNOWN_RUN_TYPES.has(type));
  return [...RUN_TYPES, ...[...new Set(extras)].sort()];
}

// Human words for an internal run_type. Same map feeds the row chip and the
// type selector, so the two can never disagree.
export function runTypeLabel(runType: string | null | undefined, t: (k: string) => string): string {
  if (!runType) return t('harness.runType.unknown');
  return KNOWN_RUN_TYPES.has(runType) ? t(`harness.runType.${runType}`) : runType;
}

const RUN_STATUSES = ['queued', 'running', 'succeeded', 'failed', 'canceled'];

// Human words for a run status. Same map feeds the detail pill and the status
// segments; an unrecognised status still prints rather than blanking out.
export function runStatusLabel(status: string | null | undefined, t: (k: string) => string): string {
  if (!status) return '—';
  return RUN_STATUSES.includes(status) ? t(`harness.runStatus.${status}`) : status;
}

const WHITESPACE_RUN = /\s+/g;

// One uniform row title for every run type (plan §4.1): the message's first
// non-empty line, else the originating task/watch name, else the type label.
// Never slices — the row truncates with CSS so the detail panel and the
// server-side search index keep the full text.
export function runRowTitle(
  run: Pick<HarnessRun, 'message' | 'prompt' | 'definition_name'>,
  typeLabel: string,
): string {
  for (const line of (run.message || run.prompt || '').split('\n')) {
    const collapsed = line.trim().replace(WHITESPACE_RUN, ' ');
    if (collapsed) return collapsed;
  }
  return run.definition_name?.trim() || typeLabel;
}

export type HarnessSessionState = 'none' | 'workbench' | 'im' | 'deleted';

// The all-null summary the server sends for a session id that no longer
// resolves. Used when a payload carries the id but not its own summary object.
export const BLANK_SESSION_SUMMARY: HarnessSessionSummary = {
  session_title: null,
  session_platform: null,
  session_scope_kind: null,
  session_label: null,
  session_is_workbench: false,
};

// Which of the four states a bound session is in (plan §4.2).
//
// ``deleted`` is the state this surface used to get wrong: a run/task whose
// session row is gone resolves to the all-null summary, fell through to the IM
// branch, and printed a bare hash that opens nothing. Naming the state lets the
// UI say so instead.
export function harnessSessionState(
  summary: HarnessSessionSummary,
  sessionId: string | null | undefined,
): HarnessSessionState {
  if (summary.session_is_workbench) return 'workbench';
  if (summary.session_platform) return 'im';
  return sessionId ? 'deleted' : 'none';
}
