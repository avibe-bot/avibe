// Pure helpers for the unified background-work banner (ChatPage ActivityStrip).
// The banner renders a union of backend activities and live-derived harness
// items (watches / scheduled tasks / delegated agent runs); these functions
// classify and label a row without pulling in the component.
import type { SessionActivityItemKind, SessionActivityState } from '../context/ApiContext';

const HARNESS_ITEM_KINDS: readonly SessionActivityItemKind[] = ['watch', 'task', 'agent_run'];

// Resolve the union discriminator. A missing or unknown (e.g. pre-union) value
// reads as a backend activity so the banner never drops a row.
export function activityItemKind(
  item: Pick<SessionActivityState, 'item_kind'>,
): SessionActivityItemKind {
  const kind = item.item_kind;
  return kind && HARNESS_ITEM_KINDS.includes(kind) ? kind : 'backend_activity';
}

// Task recurrence is an explicit durable field (`at` / `cron`). Keep this
// mapping independent of task names so display text can never misclassify a
// user-authored label that happens to mention scheduling words.
export function activityKindI18nKey(
  item: Pick<SessionActivityState, 'item_kind' | 'schedule_type'>,
): string {
  const kind = activityItemKind(item);
  if (kind === 'task') {
    if (item.schedule_type === 'at') return 'taskOneShot';
    if (item.schedule_type === 'cron') return 'taskRecurring';
  }
  if (kind === 'backend_activity') return 'backendActivity';
  if (kind === 'agent_run') return 'agentRun';
  return kind;
}

// Harness rows (watch / task / delegated run) navigate to the Harness surface;
// backend activities keep their current non-navigating behavior.
export function isHarnessActivity(item: Pick<SessionActivityState, 'item_kind'>): boolean {
  return activityItemKind(item) !== 'backend_activity';
}

// Prefer the unified label, then the legacy description, then a kind fallback so
// an unnamed watch/task still shows something meaningful.
export function resolveActivityLabel(
  item: Pick<SessionActivityState, 'label' | 'description'>,
  fallback: string,
): string {
  return (item.label || item.description || '').trim() || fallback;
}

// A harness item id is namespaced (`watch:<id>` / `task:<id>` / `agent_run:<id>`).
// Return the underlying entity id for deep-linking; backend ids pass through.
export function harnessItemNativeId(item: Pick<SessionActivityState, 'id'>): string {
  const id = item.id ?? '';
  const idx = id.indexOf(':');
  return idx >= 0 ? id.slice(idx + 1) : id;
}

// Status values that mean "actively executing" for banner ordering. ``running``
// is the normalized form; ``processing`` is the raw/legacy alias the durable
// store may still carry (see storage/background.RUN_STATUS_ALIASES) — both must
// rank as in-progress or a live delegated run could sort below pending items.
const RUNNING_STATUSES = new Set(['running', 'processing']);

// Popover ordering (spec req 5): in-progress items first, then start time
// descending. Backend activities and running/processing delegated runs are
// "in progress"; watches (enabled), tasks (scheduled), and queued runs are
// pending. Stable, pure copy — never mutates the input.
export function sortBackgroundActivities(items: SessionActivityState[]): SessionActivityState[] {
  const activeRank = (it: SessionActivityState) => (RUNNING_STATUSES.has(it.status) ? 0 : 1);
  const sinceOf = (it: SessionActivityState) => it.since ?? it.started_at ?? '';
  return [...items].sort((a, b) => {
    const rank = activeRank(a) - activeRank(b);
    if (rank !== 0) return rank;
    return sinceOf(b).localeCompare(sinceOf(a));
  });
}

// Where a harness banner row navigates (spec req 4). Watch/task rows open the
// matching Harness tab filtered to the originating session (route param, shown
// as a removable chip). A delegated run executes in another session, so it can't
// be session-filtered — it opens the runs tab anchored to that run instead.
// Backend activities never navigate (guarded by the caller); they fall back to
// the bare Harness page.
export function harnessNavPath(
  item: Pick<SessionActivityState, 'id' | 'item_kind'>,
  sessionId: string | null | undefined,
): string {
  const kind = activityItemKind(item);
  const params = new URLSearchParams();
  if (kind === 'watch' || kind === 'task') {
    params.set('tab', kind === 'watch' ? 'watches' : 'tasks');
    if (sessionId) params.set('session', sessionId);
  } else if (kind === 'agent_run') {
    params.set('tab', 'runs');
    const runId = harnessItemNativeId(item);
    if (runId) params.set('run', runId);
  }
  const qs = params.toString();
  return qs ? `/harness?${qs}` : '/harness';
}
