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
//    reconciles that scope's window. `created` (foreground/undo) is reconciled
//    with minCount 1, so a restored row returns even from an emptied window;
//    `user_message` (background) drops it via the foreground-only re-read.
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
    {
      session_id: sessionId,
      scope_id: scopeId,
      event: visibility === 'background' ? 'user_message' : 'created',
    },
  ];
}
