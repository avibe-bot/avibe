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

// Popover ordering (spec req 5): in-progress items first, then start time
// descending. Backend activities and running delegated runs are "in progress"
// (status === 'running'); watches (enabled), tasks (scheduled), and queued runs
// are pending. Stable, pure copy — never mutates the input.
export function sortBackgroundActivities(items: SessionActivityState[]): SessionActivityState[] {
  const activeRank = (it: SessionActivityState) => (it.status === 'running' ? 0 : 1);
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
