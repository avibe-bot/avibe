// A chat as the shipped ChatPage reads it: one bootstrap payload (the Session row, its transcript, queue,
// draft, and turn state), the durable execution-process groups, and the few reads around them. Desktop
// frames draw the page in AppShell's content column; phone frames inside the shipped AppShell.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import { ChatPage } from '@/components/workbench/ChatPage';
import type { SessionRuntimeState, WorkbenchMessage, WorkbenchSession } from '@/context/ApiContext';
import { DockProvider } from '@/context/DockProvider';
import { ShowPageDragProvider } from '@/context/ShowPageDragProvider';
import type { TurnActivityGroupWire } from '@/lib/agentActivity';

import { DESIGN_NOW } from '../../shell/clock';
import { session, type WorkbenchFixtures } from '../../shell/fixtures';
import { demoAgents, modelMenusApi } from '../agents/fixtures';
import { agent, backendModel, chatgptPro, ok, relayClaude, relayGpt } from '../models/fixtures';
import { settingsConfig } from '../settings/fixtures';
import { consolePage, phoneShell, workbenchFixtures } from '../workbench/fixtures';

export const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();

/** A Session id as the server mints them. `ses_<handle>` is a Bot's conversation, so ordinary chats never use it. */
export const CHAT_ID = 'sesm0b1ux7d2q';

let seq = 0;
/** One transcript row as `GET /api/sessions/<id>/messages` serves it. */
export function message(
  author: 'user' | 'agent' | 'system' | 'harness',
  type: string,
  text: string,
  minutesAgo: number,
  extra: Partial<WorkbenchMessage> = {},
): WorkbenchMessage {
  seq += 1;
  return {
    id: `msg_${seq.toString(36).padStart(6, '0')}`,
    scope_id: 'avibe::project::proj_avibe',
    session_id: CHAT_ID,
    platform: 'avibe',
    author,
    type,
    source: author === 'system' ? 'agent' : author,
    author_id: author === 'user' ? 'local' : null,
    author_name: null,
    native_message_id: null,
    parent_native_message_id: null,
    text,
    content: {},
    metadata: {},
    created_at: at(minutesAgo),
    updated_at: at(minutesAgo),
    delivered_at: at(minutesAgo),
    read_at: at(minutesAgo),
    ...extra,
  };
}

export const userSays = (text: string, minutesAgo: number, extra: Partial<WorkbenchMessage> = {}) =>
  message('user', 'user', text, minutesAgo, extra);
/** The agent's reply that ends a turn (`result`). */
export const agentSays = (text: string, minutesAgo: number, extra: Partial<WorkbenchMessage> = {}) =>
  message('agent', 'result', text, minutesAgo, extra);

export const idleTurn: SessionRuntimeState = {
  in_flight: false,
  foreground: 'idle',
  native_turn_started: false,
  pending_input_count: 0,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'connected',
};
/** A turn the agent is working on. */
export const runningTurn: SessionRuntimeState = { ...idleTurn, in_flight: true, foreground: 'running', native_turn_started: true };

const efforts = ['low', 'medium', 'high', 'xhigh'];
/** Vibey's model menu: Model Hub serves Vibey, so its picker reads the gateway's catalog. */
const vibeyCatalog = agent({
  backend: 'vibey',
  cliPresent: false,
  order: [chatgptPro, relayGpt, relayClaude],
  routes: {
    'gpt-6-astra': { hops: [[chatgptPro, 'gpt-6-astra'], [relayGpt, 'gpt-6-astra']] },
    'gpt-5.6-sol': { hops: [[relayGpt, 'gpt-5.6-sol']] },
    'claude-opus-5': { hops: [[relayClaude, 'claude-opus-5']] },
  },
  catalog: [
    backendModel('gpt-6-astra', { display_name: 'GPT-6 Astra', supports_reasoning: true, reasoning_efforts: efforts }),
    backendModel('gpt-5.6-sol', { display_name: 'GPT-5.6 Sol', supports_reasoning: true, reasoning_efforts: efforts }),
    backendModel('claude-opus-5', { display_name: 'Claude Opus 5', supports_reasoning: true, reasoning_efforts: ['low', 'medium', 'high', 'max'] }),
  ],
});

/** A failure notice that ends a turn, offering Retry. */
export const failureNotice = (turnId: string, minutesAgo: number) =>
  message('agent', 'notify', '模型服务暂时不可用，这一轮没有完成。', minutesAgo, {
    source: 'agent',
    metadata: { event: 'backend_failure', backend: 'vibey', failure_id: `turn:${turnId}`, turn_id: turnId },
  });

/** What Model Hub recorded for that turn (the notice's 查看详情): the relay refused every attempt. */
export const failureApi = (turnId: string) => ({
  [`/api/models/turns/${turnId}/provenance`]: ok({
    provenance: {
      contract_version: 23,
      turn_id: turnId,
      ts: at(5),
      agent: 'vibey',
      requested_model_id: 'gpt-6-astra',
      outcome: 'exhausted',
      failed_attempts: [
        {
          source_id: 'src_relaygpt',
          configured_model_id: 'gpt-6-astra',
          channel: 'hub',
          reason: 'rate_limited',
          http_status: 429,
          upstream_detail: 'Rate limit reached for gpt-6-astra: 60 requests per minute.',
        },
      ],
      served: null,
      canceled_attempt: null,
      terminal_error: null,
      model_supply_state: null,
      blockers: [],
    },
  }),
  '/api/models/sources': ok({ sources: [{ id: 'src_relaygpt', display_name: 'relay-gpt' }] }),
});

// ----- The execution process (执行过程) -----

type ActivityRowWire = NonNullable<TurnActivityGroupWire['rows']>[number];

let rowSeq = 0;
const activityRow = (kind: ActivityRowWire['kind'], text: string, minutesAgo: number, extra: Partial<ActivityRowWire> = {}): ActivityRowWire => {
  rowSeq += 1;
  return { id: `evt_${rowSeq.toString(36).padStart(5, '0')}`, kind, text, created_at: at(minutesAgo), ...extra };
};

/** The agent's words between tool calls. */
export const narration = (text: string, minutesAgo: number) => activityRow('assistant', text, minutesAgo);
/** A tool call as the backend formats it: the tool's name and its arguments. */
export const toolCall = (tool: string, args: Record<string, unknown>, minutesAgo: number) =>
  activityRow('tool_call', `🔧 \`${tool}\` \`${JSON.stringify(args)}\``, minutesAgo);
/** A hosted tool call (web search, web fetch, image generation): its card record rides on the row. */
export const hostedCall = (hosted: unknown, minutesAgo: number) => activityRow('tool_call', '', minutesAgo, { hosted });

/** One turn's execution process. A settled group sits just before the reply that ended it; an interrupted one
 *  just after the message that started it; `open` marks the running turn's. */
export function activityGroup(
  id: string,
  anchor: WorkbenchMessage,
  rows: ActivityRowWire[],
  extra: Partial<TurnActivityGroupWire> = {},
): TurnActivityGroupWire {
  const status = extra.status ?? 'done';
  return {
    id,
    anchor_message_id: anchor.id,
    anchor_position: status === 'interrupted' || status === 'running' ? 'after' : 'before',
    open: status === 'running',
    status,
    steps: rows.length,
    duration_ms: null,
    started_at: rows[0]?.created_at ?? null,
    rows,
    ...extra,
  };
}

export interface ChatFixture {
  /** The Session row; a Vibey chat in the avibe project unless the frame says otherwise. */
  session?: Partial<WorkbenchSession>;
  messages?: WorkbenchMessage[];
  /** Messages sent while the agent was busy, waiting their turn. */
  queued?: WorkbenchMessage[];
  draft?: string;
  turnState?: SessionRuntimeState;
  /** `config.ui` the bootstrap carries: the execution-process panel and its tool rows are switched here. */
  ui?: Record<string, unknown>;
  /** The execution-process groups, with their rows: the summary read lists them, and each group's rows
   *  load when it opens (`GET .../activity?group_id=`). Drawn when `ui.show_agent_activity` is on. */
  activity?: TurnActivityGroupWire[];
  /** Voice input is offered (Avibe Cloud paired). */
  voice?: boolean;
  /** Anything else the frame reads, such as agent media by `/api/media/<token>`. */
  api?: Record<string, unknown>;
  extra?: WorkbenchFixtures;
}

/** The chat Session the frames open: a Vibey Session in the avibe project. */
export const chatSession = (extra: Partial<WorkbenchSession> = {}): WorkbenchSession =>
  session(extra.id ?? CHAT_ID, 'proj_avibe', '移动端交互设计', {
    workdir: '/Users/demo/avibe',
    native_session_id: 'thread_01jb7k2v',
    ...extra,
  });

const showPageAccess = {
  ok: true,
  mode: 'unmanaged',
  ownership_status: 'unmanaged',
  instance_id: null,
  organization_id: null,
  policy_organization_id: null,
  access_level: 'private',
  group_ids: [],
  policy_revision: null,
  last_applied_control_plane_revision: null,
  can_use: true,
  can_manage: true,
  can_publish_public: true,
};

/** Everything ChatPage reads for one Session, served from fixtures. */
export function chatFixtures(chat: ChatFixture = {}): WorkbenchFixtures {
  const row = chatSession(chat.session);
  const base = `/api/sessions/${row.id}`;
  const turn = chat.turnState ?? idleTurn;
  const groups = chat.activity ?? [];
  const ofSession = (rows: WorkbenchMessage[] = []) => rows.map((m) => ({ ...m, session_id: row.id }));
  return workbenchFixtures(
    {
      [`${base}/bootstrap`]: {
        session: row,
        capabilities: { can_chat: true },
        agents: demoAgents,
        default_agent_name: 'vibey',
        config: { ...settingsConfig, ui: { ...settingsConfig.ui, ...chat.ui } },
        messages: ofSession(chat.messages),
        next_after_id: null,
        next_before_id: null,
        queued: ofSession(chat.queued),
        draft: { text: chat.draft ?? '', updated_at: chat.draft ? at(5) : null },
        turn_state: turn,
      },
      [base]: { ...row, capabilities: { can_chat: true } },
      [`${base}/turn-state`]: turn,
      [`${base}/activity`]: { groups: groups.map(({ rows: _rows, ...summary }) => summary) },
      ...Object.fromEntries(groups.map((group) => [`${base}/activity?group_id=${group.id}`, group])),
      [`/api/show-pages/${row.id}/access`]: showPageAccess,
      '/api/vault/requests': { ok: true, requests: [] },
      '/api/workbench/prefs': { background_work_banner_enabled: true },
      '/api/asr/status': { available: chat.voice === true },
      // Offering voice warms the Avibe Cloud token the recording uploads with.
      ...(chat.voice ? { '/api/cloud/token': { token: 'design', base_url: 'https://avibe.bot', expires_at: DESIGN_NOW / 1000 + 3600 } } : {}),
      ...modelMenusApi,
      '/api/models/agents/vibey/models': ok({ agent: vibeyCatalog }),
      ...chat.api,
    },
    chat.extra,
  );
}

/** Inside the Dock and the Show Page drag the header's Show Page controls read; AppShell serves them in the app. */
export const withChatProviders = (node: ReactNode) => (
  <DockProvider enabled>
    <ShowPageDragProvider>{node}</ShowPageDragProvider>
  </DockProvider>
);

const page = withChatProviders(
  <Routes>
    <Route path="/chat/:sessionId" element={<ChatPage />} />
  </Routes>,
);

export const chatRoute = (sessionId = CHAT_ID) => `/chat/${sessionId}`;
/** The chat in AppShell's desktop content column (beside the sidebar, which has its own frames). */
export const chatDesktop = consolePage(page);
/** The chat as a phone shows it: full screen inside the shipped AppShell. */
export const chatPhone = phoneShell('/chat/:sessionId', <ChatPage />);
