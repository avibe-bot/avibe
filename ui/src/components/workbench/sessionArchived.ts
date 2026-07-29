// Archive is terminal — the read-only derivations for the chat surface.
//
// A session whose lifecycle ``status`` is ``archived`` can never accept another
// write: the server refuses the messages POST, the sessions PATCH, the fork and
// the Show Page create with ``409 {"code": "session_archived"}``. Search's
// "include archived" opt-in links straight into such a chat, so this is a normal
// entry path rather than an edge case, and the transcript must stay fully
// readable while every mutating affordance is withdrawn.
//
// These live beside ChatPage rather than inside it (the harnessRuns.ts pattern)
// so the decisions are unit-tested without mounting a 3000-line component — the
// page just reads the answers.
import type { WorkbenchSession } from '../../context/ApiContext';

/** One predicate owns "this session can never accept another write", so the
 *  composer, header, transcript, queue strip and vault cards all derive the same
 *  fact instead of each site re-deriving it. */
export const isSessionReadOnly = (session: WorkbenchSession | null): boolean =>
  session?.status === 'archived';

/** A ``409 {"code": "session_archived"}`` from any session-scoped write is the
 *  server stating this row is archived. Same shape for the messages POST and the
 *  sessions PATCH, so the classifier is shared rather than inlined per caller. */
export const isSessionArchivedConflict = (status: number, body: unknown): boolean =>
  status === 409 && (body as { code?: unknown } | null | undefined)?.code === 'session_archived';

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
