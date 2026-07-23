// Synthesize the `session.activity` event sequence the backend emits for a
// visibility change (see _publish_session_update_activity in ui_server.py), so
// the client can replay a successful visibility PATCH through the SAME workbench-
// event pipeline the SSE stream feeds. That single chokepoint lets every
// visibility-keyed cache reconcile via its own existing reducer even when the SSE
// stream is down (remote/mobile) — instead of each cache being hand-synced at the
// call site. A real SSE event arriving later is an idempotent no-op.
//
// Two events, matching the backend, each consumed by a different listener:
//  - `updated` carries `visibility` → the Inbox listener drops the card on
//    background / reconciles it on foreground (sessionActivityInboxAction).
//  - the placement event is a REORDER event → the projects-tree listener
//    reconciles that scope's window. Foreground/undo emits a `created` placement
//    carrying `restored: true` (a marker a real backend SSE event never sets), so
//    the tree reconciles one past the loaded window and a row ranked past the page
//    still returns; `user_message` (background) drops it via the foreground-only
//    re-read.
//
// A pure visibility toggle never changes scope, so the placement event rides the
// same scope_id. Pure + exported so it can be unit-tested without the provider.

// Matches the WorkbenchEventHandlers.onSessionActivity payload shape in ApiContext.
export type SessionActivityEvent = {
  session_id: string;
  scope_id: string | null;
  event: string;
  title?: string | null;
  visibility?: 'foreground' | 'background';
  // Client-only marker: set on a synthesized foreground-restore `created` event
  // so the tree grows its window to bring a restored (previously-hidden) row back.
  // Real backend `created` events never carry it, so a genuine new session keeps
  // the original reconcile semantics and does not inflate the window.
  restored?: boolean;
};

export function visibilityActivityEvents(args: {
  sessionId: string;
  scopeId: string | null;
  title: string | null;
  visibility: 'foreground' | 'background';
}): SessionActivityEvent[] {
  const { sessionId, scopeId, title, visibility } = args;
  return [
    { session_id: sessionId, scope_id: scopeId, event: 'updated', title, visibility },
    visibility === 'background'
      ? { session_id: sessionId, scope_id: scopeId, event: 'user_message' }
      : { session_id: sessionId, scope_id: scopeId, event: 'created', restored: true },
  ];
}

// minCount for the tree's `created`-placement reconcile. A synthesized restore
// (`restored`) grows the window to `loaded + 1` so a row ranked just past the
// loaded page returns; a real `created` (new session, no marker) keeps the
// original `1` floor so repeated local creates don't inflate the window
// (targetCount stays `max(loaded, 1)`).
export function createdReconcileMinCount(restored: boolean, loaded: number): number {
  return restored ? loaded + 1 : 1;
}
