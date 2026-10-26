import { createRoot } from 'react-dom/client';
import { PetAvatar, type PetPose } from '../../src/pet/PetAvatar';
import { PetPage } from '../../src/pet/PetPage';
import { PetWindow } from '../../src/pet/PetWindow';
import { petBridge } from '../../src/pet/petBridge';
import { petPanel } from '../../src/pet/petPanel';
import { ApiProvider } from '../../src/context/ApiContext';
import { InstanceAuthorizationProvider } from '../../src/context/InstanceAuthorizationProvider';
import { WorkbenchInboxProvider } from '../../src/context/WorkbenchInboxProvider';
import { ToastProvider } from '../../src/context/ToastProvider';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import i18n from '../../src/i18n';
import '../../src/index.css';

const params = new URLSearchParams(location.search);
await i18n.changeLanguage(params.get('lang') ?? 'en');
const PINNED_POSES: readonly PetPose[] = [
  'idle',
  'sleeping',
  'listening',
  'running',
  'team',
  'ready',
  'needs_input',
  'blocked',
];
const requestedPose = params.get('pose');
const pinnedPose = requestedPose && PINNED_POSES.includes(requestedPose as PetPose)
  ? requestedPose as PetPose
  : null;
declare global {
  interface Window {
    petLayoutHitRegions: Parameters<typeof petBridge.setHitRegions>[0];
    petHitReports: number;
    petFixtureLayout: (revision: number, edge: 'top' | 'bottom') => void;
    setPetLayoutVisible: (visible: boolean) => void;
    petFixtureSends: { sessionId: string; payload: unknown }[];
    petFixtureOpens: string[];
    petFixtureStops: unknown[];
    petFixtureMarkReads: string[];
    petFixtureMainReads: number;
    setPetMainAvailable: (available: boolean) => void;
    petFixtureConnection: (connected: boolean) => void;
    petFixtureMessage: (message: Record<string, unknown>) => void;
    petFixtureAddActivity: () => void;
    petFixtureFinishTurn: (result: Record<string, unknown>) => void;
  }
}
window.petLayoutHitRegions = [];
window.petHitReports = 0;
petBridge.setHitRegions = async (rects) => { window.petLayoutHitRegions = rects; window.petHitReports += 1; };
// Apply a layout snapshot the way the shell's event does.
window.petFixtureLayout = (revision, edge) => { petPanel.applySnapshot({ revision, layout: { panel_side: 'left', panel_edge: edge } }); };
window.petFixtureSends = [];
window.petFixtureOpens = [];
window.petFixtureStops = [];
window.petFixtureMarkReads = [];
petBridge.open = async (link) => { window.petFixtureOpens.push(link); };
// `edge=top`: the layout a shell chooses near the top of the display (§9.1).
if (params.get('edge') === 'top') petPanel.applySnapshot({ revision: 1, layout: { panel_side: 'left', panel_edge: 'top' } });
const hasMain = params.get('main') !== 'false';
const layers = Number(params.get('layers') ?? 3);
const personal = params.get('scenario') === 'personal';
const historyActions = params.get('history') === 'actions';
// Earlier turns that overflow the history box, with the latest reply unread
// unless `unread=0`.
const historyLong = params.get('history') === 'long';
// A latest reply too tall for the history box on its own.
const replyLong = params.get('reply') === 'long';
const title = personal ? '检查桌宠 mini 的交互' : 'A long unbroken title '.repeat(40);
const session = {
  id: 'ses_main', scope_id: 'fixture', title, agent_backend: 'codex',
  capabilities: { can_chat: true },
  status: 'active', visibility: 'foreground', agent_status: 'idle', metadata: {},
};
const earlier = historyLong ? Array.from({ length: 6 }, (_, i) => ({
  id: `earlier_${i}`, session_id: session.id, scope_id: session.scope_id,
  author: i % 2 ? 'agent' : 'user', type: i % 2 ? 'result' : 'user',
  text: i % 2 ? `第 ${i} 轮的回复。`.repeat(6) : `第 ${i} 轮的问题`, metadata: {}, content: {},
  created_at: `2026-10-11T00:00:0${i}Z`, updated_at: `2026-10-11T00:00:0${i}Z`, read_at: '2026-10-11T00:00:09Z',
})) : [];
const messages = hasMain ? [...earlier, {
  id: 'user', session_id: session.id, scope_id: session.scope_id,
  author: 'user', type: 'user', text: title, metadata: {}, content: {},
  created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z',
}, {
  id: 'result', session_id: session.id, scope_id: session.scope_id,
  author: 'agent', type: 'result',
  text: replyLong ? '这是一条很长的回复。'.repeat(60) : personal ? '我会帮你跟进各个项目。需要授权的事已经排在前面。' : 'Ready to help with your next task.',
  metadata: {}, content: historyActions ? { quick_replies: ['继续', '稍后'] } : {}, read_at: historyLong && params.get('unread') !== '0' ? null : '2026-10-12T00:00:01Z',
  created_at: '2026-10-12T00:00:01Z', updated_at: '2026-10-12T00:00:01Z',
}] : [];
const agents = Array.from({ length: Number(params.get('activities') ?? layers + 1) }, (_, i) => ({
  backend: i === 1 ? 'claude' : 'codex', state: personal && i === 2 ? 'idle' : 'active',
  session_id: `ses_fixture_${i}`, base_session_id: `native_${i}`, composite_key: null,
  pid: null, pid_shared: false, workdir: null, native_session_id: null, model: null, elapsed_seconds: 10,
  title: personal ? ['修复桌宠语音输入', '整理团队本周进展', '梳理 Personal Agent 交互', '检查项目文档'][i % 4] : title,
  scope_display_name: personal && i === 1 ? '产品频道' : i === 2 ? 'avibe-docs' : 'avibe-app',
  agent_name: i === 1 ? 'claude' : 'codex', platform: personal && i === 1 ? 'feishu' : 'avibe',
  scope_type: 'project', visibility: 'foreground', trigger_source: 'human', openable_in_chat: true,
}));
const requests = personal ? [{
  id: 'vr_fixture', request_type: 'access', status: 'pending', card: { request_type: 'access' },
  session: { id: 'ses_fixture_0' }, expires_at: null,
}] : [];
if (historyActions) requests.push({
  id: 'vr_main_fixture', request_type: 'access', status: 'pending', card: { request_type: 'access' },
  session: { id: 'ses_main' }, expires_at: null,
});
const unread = personal ? { ses_fixture_2: 1 } : {};
// A legacy file must not steer reads or sends away from main.
localStorage.setItem('avibe.pet.devBinding', JSON.stringify({ binding: 'legacy-session', revision: 1 }));
let mainAvailable = params.get('offline') !== 'true';
let mainRunning = params.get('running') === '1';
window.petFixtureMainReads = 0;
window.setPetMainAvailable = (available) => { mainAvailable = available; };

// Every API read is fixture-owned. Unexpected calls fail rather than reaching a
// user's runtime; the Vite proxy also points at an unused local port.
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin);
  const path = url.pathname;
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  if (path === '/api/sessions/main') {
    window.petFixtureMainReads += 1;
    if (!mainAvailable) return new Response(JSON.stringify({ error: 'temporarily_unavailable' }), { status: 503, headers: { 'Content-Type': 'application/json' } });
    return json({ session, capabilities: { can_chat: true } });
  }
  if (init?.method === 'POST' && path.endsWith('/messages')) {
    const payload = JSON.parse(String(init.body));
    window.petFixtureSends.push({ sessionId: path.split('/')[3], payload });
    return json({ ...messages[0], id: 'sent', text: payload.text });
  }
  if (init?.method === 'POST' && path.endsWith('/cancel')) {
    window.petFixtureStops.push(path.split('/')[3]);
    return json({ ok: true });
  }
  if (path.endsWith('/turn-state') && !path.includes('ses_main')) return json({ in_flight: true, foreground: 'running' });
  if (path.endsWith('/mark-read')) {
    window.petFixtureMarkReads.push(path.split('/')[3]);
    return json({ updated: 1, unread_by_session: unread });
  }
  const payloads: Record<string, unknown> = {
    '/api/csrf-token': { csrf_token: 'fixture-only-token' },
    '/api/config': { mode: 'self_host', setup_state: { needs_setup: false } },
    '/api/session': { remote: false, instance_kind: 'personal', capabilities: OWNER_INSTANCE_CAPABILITIES },
    '/api/sessions/ses_main': session,
    '/api/sessions/ses_main/messages': { messages, next_after_id: null, next_before_id: null },
    '/api/sessions/ses_main/turn-state': {
      // `running=1`: Vibey's own turn is in progress until `petFixtureFinishTurn`.
      foreground: mainRunning ? 'running' : 'idle', in_flight: mainRunning, native_turn_started: false,
      // `queued=1`: an input waits behind the turn, which keeps Vibey's bubble
      // shown with the latest reply's summary (the state layout tests draw in).
      pending_input_count: params.get('queued') === '1' ? 1 : 0,
      background_activities: [], pending_activity_output_count: 0, connection: 'connected',
    },
    '/api/vault/requests': { ok: true, requests: requests.filter((request) => !url.searchParams.get('session') || request.session.id === url.searchParams.get('session')) },
    // `asr=1`: voice input is available, so the dock shows its mic.
    '/api/asr/status': { available: params.get('asr') === '1' },
    '/api/running-agents': { ok: true, agents, counts: { total: agents.length, active: agents.length, idle: 0, orphan: 0, by_backend: {} } },
    '/api/sessions': { sessions: [session], next_before_id: null },
    '/api/inbox': { ok: true, sessions: [], unread_by_session: unread, total_unread: Object.values(unread).reduce((a, b) => a + b, 0), next_before_id: null },
  };
  if (!(path in payloads)) throw new Error(`Unexpected pet fixture request: ${path}`);
  return json(payloads[path]);
};
// Fixture-owned real EventTarget: exercise the provider's reconnect path.
const streams = new Set<FixtureEvents>();
class FixtureEvents extends EventTarget {
  static OPEN = 1;
  readyState = 1;
  constructor() { super(); streams.add(this); }
  close() { streams.delete(this); }
}
window.EventSource = FixtureEvents as unknown as typeof EventSource;
window.petFixtureConnection = (connected) => {
  for (const stream of streams) {
    stream.dispatchEvent(new MessageEvent('workbench.events.bridge.status', {
      data: JSON.stringify({ type: 'workbench.events.bridge.status', data: { connected } }),
    }));
  }
};
// Deliver a live `message.new` for the main session, as the event stream would.
window.petFixtureMessage = (message) => {
  const data = {
    session_id: session.id, scope_id: session.scope_id, metadata: {}, content: {},
    created_at: '2026-10-12T00:00:02Z', updated_at: '2026-10-12T00:00:02Z', read_at: '2026-10-12T00:00:03Z',
    ...message,
  };
  for (const stream of streams) {
    stream.dispatchEvent(new MessageEvent('message.new', { data: JSON.stringify({ type: 'message.new', data }) }));
  }
};
// Start one more visible activity, announced the way a run change is.
window.petFixtureAddActivity = () => {
  agents.push({ ...agents[0], session_id: `ses_fixture_${agents.length}`, base_session_id: `native_${agents.length}`, title: 'Another task' });
  for (const stream of streams) {
    stream.dispatchEvent(new MessageEvent('runs.updated', { data: JSON.stringify({ type: 'runs.updated', data: { run_id: `run_${agents.length}`, status: 'running' } }) }));
  }
};
// End Vibey's running turn with its result, as the event stream reports it.
window.petFixtureFinishTurn = (result) => {
  const data = {
    session_id: session.id, scope_id: session.scope_id, metadata: {}, content: {},
    created_at: '2026-10-12T00:00:02Z', updated_at: '2026-10-12T00:00:02Z', read_at: '2026-10-12T00:00:03Z',
    ...result,
  };
  mainRunning = false;
  messages.push(data as unknown as (typeof messages)[number]);
  for (const stream of streams) {
    stream.dispatchEvent(new MessageEvent('message.new', { data: JSON.stringify({ type: 'message.new', data }) }));
    stream.dispatchEvent(new MessageEvent('turn.end', { data: JSON.stringify({ type: 'turn.end', data: { session_id: session.id } }) }));
  }
};

const root = createRoot(document.getElementById('root')!);
window.setPetLayoutVisible = (visible) => root.render(
  <ToastProvider>
    <ApiProvider>
      <InstanceAuthorizationProvider session={{ remote: false, instance_kind: 'personal', capabilities: OWNER_INSTANCE_CAPABILITIES }}>
        {pinnedPose ? (
          visible ? (
            <PetWindow>
              <div className="pet-mini-avatar-wrap" style={{ position: 'relative', width: 115, height: 115 }}>
                <PetAvatar pose={pinnedPose} />
              </div>
            </PetWindow>
          ) : null
        ) : (
          <WorkbenchInboxProvider><PetWindow>{visible ? <PetPage /> : null}</PetWindow></WorkbenchInboxProvider>
        )}
      </InstanceAuthorizationProvider>
    </ApiProvider>
  </ToastProvider>,
);
window.setPetLayoutVisible(true);
