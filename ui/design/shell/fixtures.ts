import type { InboxSession, InstanceCapabilities, WorkbenchProject, WorkbenchSession } from '@/context/ApiContext';
import type { BotRuntimeStatus } from '@/context/BotsContext';
import type { RuntimeStatus } from '@/context/StatusContext';
import type { Bot, BotAvatar } from '@/lib/bots';
import { OWNER_INSTANCE_CAPABILITIES, type InstanceKind } from '@/lib/sessionInfo';

import { DESIGN_NOW } from './clock';

/** What a frame can set. Everything is optional; omitted parts are empty, not loading. */
export interface WorkbenchFixtures {
  capabilities?: InstanceCapabilities;
  /** An Organization instance (shared transcripts name their senders); a personal one when omitted. */
  instanceKind?: InstanceKind;
  /** The principal the viewer's own Chat rows are written as (`local`, `remote:<sub>`). */
  readerPrincipal?: string;
  /** The service status the shell polls (`GET /status`), as the Status provider serves it. */
  status?: RuntimeStatus;
  projects?: WorkbenchProject[];
  sessionsByProject?: Record<string, WorkbenchSession[]>;
  expandedProjects?: string[];
  inboxSessions?: InboxSession[];
  unreadBySession?: Record<string, number>;
  /** The Owner's Bots, served as `GET /api/bots` to the real Bots provider. */
  bots?: Bot[];
  /** Agent status of Bot Sessions, by Session id (the running dot). */
  botStatus?: Record<string, BotRuntimeStatus>;
  /** Web Storage entries the frame starts with (a remembered preference, such as a collapsed section). */
  storage?: Record<string, string>;
  /** API responses by path plus query (`/api/inbox?session=ses_pm&limit=1`) or by path, for anything else a frame fetches. */
  api?: Record<string, unknown>;
}

/** Everything the frame's providers fetch, as the network stub serves it. */
export function apiResponses(fixtures: WorkbenchFixtures = {}): Record<string, unknown> {
  // Any write first reads a CSRF token, so every frame can draw a component that saves.
  const responses: Record<string, unknown> = { '/api/csrf-token': { csrf_token: 'design' } };
  if (fixtures.bots) responses['/api/bots'] = { ok: true, bots: fixtures.bots };
  // Every Bot Session is read for its status dot; idle unless the frame says otherwise.
  for (const { session_id: sessionId } of fixtures.bots ?? []) {
    const status = fixtures.botStatus?.[sessionId] ?? 'idle';
    responses[`/api/sessions/${sessionId}`] = session(sessionId, '', '', { scope_id: null, project_id: null, agent_status: status });
  }
  return { ...responses, ...fixtures.api };
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

export const bot = (handle: string, displayName: string, role: string, avatar: BotAvatar | null, position: number): Bot => ({
  handle,
  display_name: displayName,
  avatar,
  role,
  agent_name: handle,
  session_id: `ses_${handle}`,
  position,
  builtin: handle === 'main',
  hide_details: true,
});

/** The main Bot as a new install has it: the mascot, and the localized default name. */
export const mainBot = bot('main', '', "The user's main Bot and first point of contact.", { kind: 'mascot' }, 0);
export const demoBots = [
  mainBot,
  bot('cso', '首席安全官', '依赖与权限的安全审计，每周汇报', { kind: 'emoji', emoji: '🛡️', hue: 200 }, 1),
  bot('pm', '产品经理', '需求池整理与优先级', { kind: 'emoji', emoji: '🧭', hue: 42 }, 2),
];
