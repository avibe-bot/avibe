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
