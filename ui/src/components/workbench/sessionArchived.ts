// Archive is terminal — the read-only derivations for the chat surface.
//
// A session whose lifecycle ``status`` is ``archived`` can never accept another
// write: the server refuses the messages POST, the sessions PATCH, the fork and
// every Show Page mutation (create / republish / re-share) with
// ``409 {"code": "session_archived"}``, and archive itself takes any existing
// Show Page offline. Search's "include archived" opt-in links straight into such
// a chat, so this is a normal entry path rather than an edge case, and the
// transcript must stay fully readable while every mutating affordance is
// withdrawn — hidden, not left clickable-and-erroring.
//
// Archive is no longer the ONLY read-only reason: a ``visibility === 'system'``
// session is a row the RUNTIME owns (today the workspace-notifications session
// that harness failure notices fall back to — "no backend and no turns"). It is
// admitted to the Inbox on purpose, so its card is a clickable chat, and
// ``POST /api/sessions/<id>/messages`` answers ``403 {"code":
// "reserved_session"}`` there. Same shape as archive, different sentence — hence
// a read-only REASON rather than a second boolean.
//
// These live beside ChatPage rather than inside it (the harnessRuns.ts pattern)
// so the decisions are unit-tested without mounting a 3000-line component — the
// page just reads the answers.
import type { WorkbenchSession } from '../../context/ApiContext';

/** Why the chat surface is read-only, or ``null`` when it is writable.
 *
 *  ``archived`` is checked FIRST: it is the terminal lifecycle state, so it wins over
 *  ownership if a system row is ever also archived (the reserved row heals itself back
 *  out of that, but only on the next notice). */
export type SessionReadOnlyReason = 'archived' | 'system';

export const sessionReadOnlyReason = (
  session: WorkbenchSession | null,
): SessionReadOnlyReason | null => {
  if (!session) return null;
  if (session.status === 'archived') return 'archived';
  if (session.visibility === 'system') return 'system';
  return null;
};

/** One predicate owns "this session can never accept another write", so the
 *  composer, header, transcript, queue strip and vault cards all derive the same
 *  fact instead of each site re-deriving it. Derived from the reason so a new reason
 *  locks every one of those affordances by construction, and only the COPY has to
 *  learn about it. */
export const isSessionReadOnly = (session: WorkbenchSession | null): boolean =>
  sessionReadOnlyReason(session) !== null;

/** A ``409 {"code": "session_archived"}`` from any session-scoped write is the
 *  server stating this row is archived. Same shape for the messages POST and the
 *  sessions PATCH, so the classifier is shared rather than inlined per caller. */
export const isSessionArchivedConflict = (status: number, body: unknown): boolean =>
  status === 409 && (body as { code?: unknown } | null | undefined)?.code === 'session_archived';

/** The same fact for a rejection that came back through the shared JSON helpers
 *  rather than a raw ``apiFetch``: ``handleApiError`` already parsed the body and
 *  threw an ``ApiError`` carrying its machine code. Duck-typed on ``code`` so this
 *  module stays free of the ApiContext import (and so a plain ``Error`` — a
 *  network failure — is correctly not an archive conflict). */
export const isSessionArchivedError = (err: unknown): boolean =>
  (err as { code?: unknown } | null | undefined)?.code === 'session_archived';

/** Apply that server truth to the loaded row.
 *
 *  Identity-stable when the row already says archived (so it cannot spin a
 *  re-render) or belongs to another session (so a late response cannot stamp one
 *  chat's archive onto the chat the user moved to). */
export const markSessionArchived = (
  prev: WorkbenchSession | null,
  sessionId: string,
): WorkbenchSession | null =>
  prev && prev.id === sessionId && prev.status !== 'archived' ? { ...prev, status: 'archived' } : prev;

/** Which text-selection actions the transcript offers.
 *
 *  "Quote" appends into the composer and "Ask in a new session" forks — a
 *  read-only session can do neither: its composer is disabled, and the fork
 *  endpoint refuses an archived source outright (archive is terminal, there is no
 *  resume or fork out of it). Fork also needs a bound native, the pre-existing
 *  gate. Both are hidden rather than offered just to fail. */
export const transcriptSelectionActions = (
  session: WorkbenchSession,
  readOnly: boolean,
): { quote: boolean; askInNew: boolean } => ({
  quote: !readOnly,
  askInNew: !readOnly && Boolean(session.native_session_id),
});

/** Whether the chat surface is replaced by the session's framed Show Page.
 *
 *  A read-only session withdraws the entire Show Page action cluster — including
 *  the back-to-chat button, which IS the Visualize toggle — so a tab that was
 *  already framing the page when the session went archived would be stranded on
 *  a page it cannot leave and that archive already forced offline. Answering
 *  ``false`` here puts it back on the transcript. ChatPage derives this rather
 *  than resetting ``showPageMode`` from an effect, so the fallback lands in the
 *  same render that flips ``readOnly``. */
export const isShowPageActive = (readOnly: boolean, showPageMode: boolean): boolean =>
  showPageMode && !readOnly;

/** Which Show Page controls the chat header offers.
 *
 *  Archive takes the Show Page with it, so ALL THREE go on a read-only session —
 *  there is no read-only page-serving path to fall back to:
 *   - ``archive_session`` forces every existing page to ``visibility="offline"``
 *     (``storage/workbench_sessions_service.py``), and ``ensure_active`` refuses
 *     to create a missing one (``409 session_archived``, ``core/show_pages.py``).
 *     So Visualize can only end in that 409, or frame a page that is offline —
 *     never a working view.
 *   - Share's mutations (``update_visibility``, ``set_share_id``, ``rotate_share``)
 *     are refused by the same guard, and its popover re-ensures the page on open,
 *     so it 409s before it can render anything.
 *   - Annotating enqueues an annotation *message* into the session, which the
 *     messages POST refuses.
 *
 *  ``visualize`` is the one member that does not also depend on Show Page mode,
 *  so it doubles as "the header's action cluster has anything left to draw".
 */
export const showPageControlActions = (
  readOnly: boolean,
  showPageMode: boolean,
): { visualize: boolean; share: boolean; annotate: boolean } => {
  // Share and the annotation control only exist while the page is framed, so
  // they follow the same fact that decides whether it is framed at all.
  const framed = isShowPageActive(readOnly, showPageMode);
  return { visualize: !readOnly, share: framed, annotate: framed };
};
