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

await i18n.changeLanguage('en');
declare global {
  interface Window {
    petLayoutHitRegions: Parameters<typeof petBridge.setHitRegions>[0];
    setPetLayoutVisible: (visible: boolean) => void;
  }
}
window.petLayoutHitRegions = [];
petBridge.setHitRegions = async (rects) => { window.petLayoutHitRegions = rects; };
const params = new URLSearchParams(location.search);
const hasMain = params.get('main') !== 'false';
const layers = Number(params.get('layers') ?? 3);
const title = 'A long unbroken title '.repeat(40);
const session = {
  id: 'pet-layout', scope_id: 'fixture', title, agent_backend: 'codex',
  status: 'active', visibility: 'foreground', agent_status: 'idle', metadata: {},
};
const messages = hasMain ? [{
  id: 'user', session_id: session.id, scope_id: session.scope_id,
  author: 'user', type: 'user', text: title, metadata: {}, content: {},
  created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z',
}] : [];
const activities = Array.from({ length: layers + (hasMain ? 0 : 1) }, (_, i) => ({
  id: `watch:fixture-${i}`, item_kind: 'watch', status: 'enabled', label: title,
  description: title, backend: 'codex', runtime_key: `fixture-${i}`,
  session_id: session.id, kind: 'watch', started_at: '2026-10-12T00:00:00Z',
}));
localStorage.setItem('avibe.pet.devBinding', JSON.stringify({ binding: session.id, revision: 1 }));

// Every API read is fixture-owned. Unexpected calls fail rather than reaching a
// user's runtime; the Vite proxy also points at an unused local port.
window.fetch = async (input) => {
  const path = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin).pathname;
  const payloads: Record<string, unknown> = {
    '/api/config': { mode: 'self_host', setup_state: { needs_setup: false } },
    '/api/session': { remote: false, instance_kind: 'personal', capabilities: OWNER_INSTANCE_CAPABILITIES },
    '/api/sessions/pet-layout': session,
    '/api/sessions/pet-layout/messages': { messages, next_after_id: null, next_before_id: null },
    '/api/sessions/pet-layout/turn-state': {
      foreground: 'idle', in_flight: false, native_turn_started: false, pending_input_count: 0,
      background_activities: activities, pending_activity_output_count: 0, connection: 'connected',
    },
    '/api/vault/requests': { ok: true, requests: [] },
    '/api/asr/status': { available: false },
    '/api/running-agents': { ok: true, agents: [], counts: { total: 0, active: 0, idle: 0, orphan: 0, by_backend: {} } },
    '/api/sessions': { sessions: [session], next_before_id: null },
    '/api/inbox': { ok: true, sessions: [], unread_by_session: {}, total_unread: 0, next_before_id: null },
  };
  if (!(path in payloads)) throw new Error(`Unexpected pet fixture request: ${path}`);
  return new Response(JSON.stringify(payloads[path]), { headers: { 'Content-Type': 'application/json' } });
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
