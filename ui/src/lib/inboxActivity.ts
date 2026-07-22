// Decide what the Inbox does with a `session.activity` SSE event.
//
// Contract A6: the backend carries the session's current `visibility` on a
// visibility/scope change. Background hides the card (like an archive);
// foreground re-pulls the feed so a restored session reappears. A pre-M1
// backend never sends `visibility`, so an ordinary activity event without it
// stays a no-op (only an explicit `archived` still drops the card).
//
// Pure so it can be unit-tested without rendering the provider.

export type InboxActivityEvent = {
  event: string;
  visibility?: 'foreground' | 'background';
};

export type InboxActivityAction = 'drop' | 'reconcile' | 'ignore';

export function sessionActivityInboxAction(data: InboxActivityEvent): InboxActivityAction {
  if (data.visibility === 'foreground') return 'reconcile';
  if (data.visibility === 'background' || data.event === 'archived') return 'drop';
  return 'ignore';
}
