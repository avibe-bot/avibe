import type { InboxSession, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { WorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';

// What the lists a chat is opened from already say about it, so a chat with no
// cached copy opens on what is known instead of a loading state
// (docs/plans/web-ui-local-cache.md section 4.2): its row from the Projects tree
// (or, short of that, what its Inbox row or its Bot says), and, from an Inbox
// row, its newest message, whose whole text the row carries as its preview.

/** The list knowledge a chat opens on until its bootstrap answers. */
export type ChatListSeed = {
  session: WorkbenchSession;
  /** The newest message, from the Inbox row; null when no list knows it. */
  latest: WorkbenchMessage | null;
};

/** The id a seeded newest message is drawn under: no server row has it, so the
 *  live window, which brings the real one, always replaces it. */
export const SEEDED_LATEST_ID_PREFIX = 'seed-latest:';

/** The Session's row as a loaded Projects list holds it. */
export function listedSession(
  tree: WorkbenchProjectsTree | null,
  sessionId: string,
  projectId: string | null,
): WorkbenchSession | null {
  if (!tree) return null;
  const projectIds = projectId ? [projectId] : (tree.projects ?? []).map((project) => project.id);
  for (const id of projectIds) {
    const row = tree.sessionsOf(id).sessions?.find((session) => session.id === sessionId);
    if (row) return row;
  }
  return null;
}

/** A row standing in for the Session from what its Inbox row says, and, for a
 *  Bot's conversation, the Agent the Bot runs on. Everything else is unknown
 *  until the bootstrap answers. */
export function sessionFromList(
  sessionId: string,
  inboxRow: InboxSession | null,
  botAgentName: string | null,
): WorkbenchSession {
  const at = inboxRow?.last_activity_at ?? '';
  return {
    id: sessionId,
    scope_id: inboxRow?.scope_id ?? null,
    project_id: inboxRow?.project_id ?? null,
    title: inboxRow?.title ?? null,
    agent_id: null,
    agent_name: botAgentName,
    agent_backend: null,
    agent_variant: null,
    model: null,
    reasoning_effort: null,
    status: 'active',
    pinned: false,
    agent_status: 'idle',
    workdir: null,
    native_session_id: null,
    created_at: at,
    updated_at: at,
    last_active_at: inboxRow?.last_activity_at ?? null,
    metadata: {},
  };
}

/** The newest message an Inbox row describes. Its preview is always the Agent's
 *  latest reply (the preview types are the Agent's: output, result, notify,
 *  vault, error), so it is the newest message only while the conversation's last
 *  author is the Agent; drawn as the reply bubble, with the row's whole text. */
export function latestFromInboxRow(row: InboxSession): WorkbenchMessage | null {
  if (row.last_message_author !== 'agent' || !row.preview_at || !row.preview_text.trim()) return null;
  return {
    id: `${SEEDED_LATEST_ID_PREFIX}${row.session_id}`,
    scope_id: row.scope_id,
    session_id: row.session_id,
    platform: 'avibe',
    author: 'agent',
    type: 'result',
    source: 'agent',
    author_id: null,
    author_name: null,
    native_message_id: null,
    parent_native_message_id: null,
    text: row.preview_text,
    content: {},
    metadata: {},
    created_at: row.preview_at,
    updated_at: row.preview_at,
    delivered_at: row.preview_at,
    read_at: null,
  };
}
