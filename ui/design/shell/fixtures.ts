import type { InboxSession, InstanceCapabilities, WorkbenchProject, WorkbenchSession } from '@/context/ApiContext';
import { OWNER_INSTANCE_CAPABILITIES } from '@/lib/sessionInfo';

import { DESIGN_NOW } from './clock';

/** What a frame can set. Everything is optional; omitted parts are empty, not loading. */
export interface WorkbenchFixtures {
  capabilities?: InstanceCapabilities;
  projects?: WorkbenchProject[];
  sessionsByProject?: Record<string, WorkbenchSession[]>;
  expandedProjects?: string[];
  inboxSessions?: InboxSession[];
  unreadBySession?: Record<string, number>;
  /** API responses by path (`/api/...`), for components that fetch while rendering. */
  api?: Record<string, unknown>;
}

export const owner = OWNER_INSTANCE_CAPABILITIES;

const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();

export const project = (id: string, name: string, extra: Partial<WorkbenchProject> = {}): WorkbenchProject => ({
  id,
  scope_id: `avibe::project::${id}`,
  display_name: name,
  folder_path: `/Users/demo/${name}`,
  created_at: at(60 * 24 * 30),
  last_active_at: at(30),
  archived: false,
  capabilities: { can_chat: true, has_folder: true },
  ...extra,
});

export const session = (id: string, projectId: string, title: string, extra: Partial<WorkbenchSession> = {}): WorkbenchSession => ({
  id,
  scope_id: `avibe::project::${projectId}`,
  project_id: projectId,
  title,
  agent_id: 'agent_vibey',
  agent_name: 'vibey',
  agent_backend: 'vibey',
  agent_variant: 'vibey',
  model: 'gpt-6-astra',
  reasoning_effort: 'high',
  status: 'active',
  visibility: 'foreground',
  pinned: false,
  agent_status: 'idle',
  workdir: null,
  native_session_id: null,
  created_at: at(600),
  updated_at: at(20),
  last_active_at: at(20),
  metadata: {},
  ...extra,
});

export const inboxRow = (
  sessionId: string,
  projectName: string | null,
  title: string,
  preview: string,
  minutesAgo: number,
  unread = 0,
): InboxSession => ({
  session_id: sessionId,
  scope_id: null,
  project_id: null,
  project_name: projectName,
  title,
  last_activity_at: at(minutesAgo),
  last_message_author: 'agent',
  replied: false,
  preview_text: preview,
  preview_at: at(minutesAgo),
  unread_count: unread,
  unread: unread > 0,
});

/** The project list the sidebar frames share. */
export const demoProjects = [
  project('proj_avibe', 'avibe'),
  project('proj_chainbot', 'chainbot'),
  project('proj_relay', 'ai-relay'),
  project('proj_work', 'ai-work'),
  project('proj_article', 'x-article'),
  project('proj_askill', 'askill-workspace'),
];

export const demoSessions: Record<string, WorkbenchSession[]> = {
  proj_avibe: [
    session('ses_demo_cache', 'proj_avibe', '修复中途断缓存', { agent_status: 'running' }),
    session('ses_demo_inbox', 'proj_avibe', '收件箱分类设计'),
    session('ses_demo_pr', 'proj_avibe', 'PR #3073 交付', { agent_status: 'failed' }),
  ],
};
