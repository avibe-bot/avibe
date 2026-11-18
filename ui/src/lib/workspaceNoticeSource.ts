import type { WorkbenchMessage } from '../context/ApiContext';

/** The Session a workspace-thread failure notice reports on, as the server names it
 *  (``content.failure_source``), and whether its failed Turn can be retried now. */
export type WorkspaceNoticeSource = { session_id: string; retry: boolean };

export function workspaceNoticeSource(message: WorkbenchMessage): WorkspaceNoticeSource | null {
  const source = (message.content as { failure_source?: Partial<WorkspaceNoticeSource> } | undefined)?.failure_source;
  return source && typeof source.session_id === 'string' && source.session_id
    ? { session_id: source.session_id, retry: source.retry === true }
    : null;
}
