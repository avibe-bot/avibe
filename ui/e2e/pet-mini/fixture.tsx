import { createRoot } from 'react-dom/client';
import { PetPage } from '../../src/pet/PetPage';
import { PetWindow } from '../../src/pet/PetWindow';
import { petBridge } from '../../src/pet/petBridge';
import { ApiProvider } from '../../src/context/ApiContext';
import { InstanceAuthorizationProvider } from '../../src/context/InstanceAuthorizationProvider';
import { WorkbenchInboxProvider } from '../../src/context/WorkbenchInboxProvider';
import { ToastProvider } from '../../src/context/ToastProvider';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import i18n from '../../src/i18n';
import '../../src/index.css';

const params = new URLSearchParams(location.search);
await i18n.changeLanguage(params.get('lang') ?? 'en');
declare global {
  interface Window {
    petLayoutHitRegions: Parameters<typeof petBridge.setHitRegions>[0];
    setPetLayoutVisible: (visible: boolean) => void;
    petFixtureSends: { sessionId: string; payload: unknown }[];
    petFixtureOpens: string[];
    petFixtureStops: unknown[];
  }
}
window.petLayoutHitRegions = [];
petBridge.setHitRegions = async (rects) => { window.petLayoutHitRegions = rects; };
window.petFixtureSends = [];
window.petFixtureOpens = [];
window.petFixtureStops = [];
petBridge.open = async (link) => { window.petFixtureOpens.push(link); };
const hasMain = params.get('main') !== 'false';
const layers = Number(params.get('layers') ?? 3);
const sessionCount = Number(params.get('sessions') ?? 1);
const personal = params.get('scenario') === 'personal';
const title = personal ? '检查桌宠 mini 的交互' : 'A long unbroken title '.repeat(40);
const session = {
  id: 'pet-layout', scope_id: 'fixture', title, agent_backend: 'codex',
  capabilities: { can_chat: true },
  status: 'active', visibility: 'foreground', agent_status: 'idle', metadata: {},
};
const messages = hasMain ? [{
  id: 'user', session_id: session.id, scope_id: session.scope_id,
  author: 'user', type: 'user', text: title, metadata: {}, content: {},
  created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z',
}, {
  id: 'result', session_id: session.id, scope_id: session.scope_id,
  author: 'agent', type: 'result',
  text: personal ? '我会帮你跟进各个项目。需要授权的事已经排在前面。' : 'Ready to help with your next task.',
  metadata: {}, content: {}, read_at: '2026-10-12T00:00:01Z',
  created_at: '2026-10-12T00:00:01Z', updated_at: '2026-10-12T00:00:01Z',
}] : [];
const agents = Array.from({ length: layers + 1 }, (_, i) => ({
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
const unread = personal ? { ses_fixture_2: 1 } : {};
const switcherSessions = [session, ...Array.from({ length: sessionCount - 1 }, (_, i) => ({ ...session, id: `fixture-session-${i}`, title: `Session ${i + 1} ${title}` }))];
localStorage.setItem('avibe.pet.devBinding', JSON.stringify({ binding: session.id, revision: 1 }));

// Every API read is fixture-owned. Unexpected calls fail rather than reaching a
// user's runtime; the Vite proxy also points at an unused local port.
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin);
  const path = url.pathname;
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  if (init?.method === 'POST' && path.endsWith('/messages')) {
    const payload = JSON.parse(String(init.body));
    window.petFixtureSends.push({ sessionId: path.split('/')[3], payload });
    return json({ ...messages[0], id: 'sent', text: payload.text });
  }
  if (init?.method === 'POST' && path.endsWith('/cancel')) {
    window.petFixtureStops.push(path.split('/')[3]);
    return json({ ok: true });
  }
  if (path.endsWith('/turn-state') && !path.includes('pet-layout')) return json({ in_flight: true, foreground: 'running' });
  if (path.endsWith('/mark-read')) return json({ updated: 1, unread_by_session: unread });
  const payloads: Record<string, unknown> = {
    '/api/csrf-token': { csrf_token: 'fixture-only-token' },
    '/api/config': { mode: 'self_host', setup_state: { needs_setup: false } },
    '/api/session': { remote: false, instance_kind: 'personal', capabilities: OWNER_INSTANCE_CAPABILITIES },
    '/api/sessions/pet-layout': session,
    '/api/sessions/pet-layout/messages': { messages, next_after_id: null, next_before_id: null },
    '/api/sessions/pet-layout/turn-state': {
      foreground: 'idle', in_flight: false, native_turn_started: false, pending_input_count: 0,
      background_activities: [], pending_activity_output_count: 0, connection: 'connected',
    },
    '/api/vault/requests': { ok: true, requests: requests.filter((request) => !url.searchParams.get('session') || request.session.id === url.searchParams.get('session')) },
    '/api/asr/status': { available: false },
    '/api/running-agents': { ok: true, agents, counts: { total: agents.length, active: agents.length, idle: 0, orphan: 0, by_backend: {} } },
    '/api/sessions': { sessions: switcherSessions, next_before_id: null },
    '/api/inbox': { ok: true, sessions: [], unread_by_session: unread, total_unread: Object.values(unread).reduce((a, b) => a + b, 0), next_before_id: null },
  };
  if (!(path in payloads)) throw new Error(`Unexpected pet fixture request: ${path}`);
  return json(payloads[path]);
};
// A disconnected event stream is sufficient for this fixed layout fixture.
class FixtureEvents {
  static OPEN = 1;
  readyState = 1;
  addEventListener() {}
  removeEventListener() {}
  close() {}
}
window.EventSource = FixtureEvents as unknown as typeof EventSource;

const root = createRoot(document.getElementById('root')!);
window.setPetLayoutVisible = (visible) => root.render(
  <ToastProvider>
    <ApiProvider>
      <InstanceAuthorizationProvider session={{ remote: false, instance_kind: 'personal', capabilities: OWNER_INSTANCE_CAPABILITIES }}>
        <WorkbenchInboxProvider><PetWindow>{visible ? <PetPage /> : null}</PetWindow></WorkbenchInboxProvider>
      </InstanceAuthorizationProvider>
    </ApiProvider>
  </ToastProvider>,
);
window.setPetLayoutVisible(true);
