import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createHashRouter, RouterProvider, Route, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ApiProvider } from '../../src/context/ApiContext';
import { WorkbenchProjectsProvider } from '../../src/context/WorkbenchProjectsProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { ToastProvider } from '../../src/context/ToastProvider';
import { SettingsOverlayRouteSurface } from '../../src/components/settings/SettingsOverlayRouteSurface';
import { Workbench } from '../../src/components/Workbench';
import { settingsOverlayNavigationState } from '../../src/lib/settingsOverlay';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import i18n from '../../src/i18n';
import '../../src/index.css';

const params = new URLSearchParams(location.search);
void i18n.changeLanguage(params.get('lang') ?? 'en');
document.documentElement.dataset.theme = params.get('theme') === 'light' ? 'light' : 'dark';
const projects = [{
  id: 'project-中文', scope_id: 'scope-中文', display_name: '中文项目', folder_path: '/fixture/中文项目',
  created_at: '2026-09-01T00:00:00Z', last_active_at: null, archived: false,
  capabilities: { can_chat: true, has_folder: true },
}];
const agents = ['codex', 'claude'].map((name) => ({ id: `agent-${name}`, name, display_name: name, backend: name, enabled: true, archived: false, model: null }));
type Write = { path: string; body: Record<string, unknown> };
const writes: Write[] = [];
const sessions: Array<Record<string, unknown>> = [];
const uploads = new Map<string, { sessionId: string; token: string }>();
const control = {
  writes,
  uploadFailures: 0,
  messageMode: 'success' as 'success' | 'rejected' | 'unknown' | 'network',
  holdUpload: false,
  releaseUpload: () => {},
  asrFailures: 0,
};
declare global { interface Window { homeMedia: typeof control } }
window.homeMedia = control;
class FixtureEvents extends EventTarget {
  readyState = 1;
  close() { this.readyState = 2; }
}
Object.defineProperty(window, 'EventSource', { value: FixtureEvents });
// All transport terminates here, including CSRF, upload bytes and ASR. No
// backend proxy, cloud account, native keychain or real browser profile is used.
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin);
  const path = url.pathname;
  const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const body = typeof init?.body === 'string' ? JSON.parse(init.body) : {};
  if (init?.method === 'POST') writes.push({ path, body });
  if (path === '/api/csrf-token') return reply({ csrf_token: 'fixture' });
  if (path === '/api/agents') return reply({ ok: true, agents, default_agent_name: 'codex' });
  if (path === '/api/projects' && init?.method === 'POST') {
    const project = { ...projects[0], id: `project-${projects.length}`, scope_id: `scope-${projects.length}`, folder_path: body.folder_path, display_name: body.display_name || body.folder_path.split('/').pop() };
    projects.push(project);
    return reply(project);
  }
  if (path === '/api/projects' || path === '/api/workbench/projects-bootstrap') return reply({ projects, sessions: {} });
  if (path === '/api/browse') return reply({ ok: true, path: body.path || '/fixture', parent: '/fixture', dirs: [{ name: '另一个项目', path: '/fixture/另一个项目' }] });
  if (path === '/api/browse/favorites') return reply({ ok: true, favorites: [{ key: 'home', path: '/fixture' }] });
  if (path === '/api/sessions' && init?.method === 'POST') {
    const session = { ...body, id: `ses-${sessions.length + 1}`, status: 'active', agent_status: 'idle', created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z', metadata: {} };
    sessions.push(session);
    return reply(session, 201);
  }
  if (path === '/api/sessions') return reply({ sessions, next_before_id: null });
  if (path.endsWith('/attachments')) {
    const form = init?.body as FormData;
    const file = form.get('file') as File;
    const uploadId = String(form.get('upload_id'));
    const sessionId = path.split('/')[3];
    writes.at(-1)!.body = { name: file.name, uploadId, sessionId, size: file.size };
    if (control.holdUpload) await new Promise<void>((resolve) => { control.releaseUpload = resolve; });
    if (control.uploadFailures-- > 0) return reply({ error: { code: 'upload_failed' } }, 503);
    const token = `${sessionId}-${uploadId}`;
    uploads.set(token, { sessionId, token });
    return reply({ token, name: file.name, mime: file.type, size: file.size, kind: 'file', url: `/api/media/${token}` });
  }
  if (path.endsWith('/messages')) {
    const attachments = (body.content?.attachments ?? []) as Array<{ token: string }>;
    if (attachments.some((attachment) => uploads.get(attachment.token)?.sessionId !== path.split('/')[3])) return reply({ error: 'scope mismatch' }, 400);
    if (control.messageMode === 'network') throw new TypeError('fixture connection lost after admission');
    if (control.messageMode === 'unknown') return reply({ state: 'reserved', dispatch_error: 'dispatch_pending' }, 504);
    if (control.messageMode === 'rejected') return reply({ state: 'retired', dispatch_error: 'dispatch_failed' }, 502);
    return reply({ id: 'message-1', type: 'user', ...body }, 201);
  }
  if (path === '/api/asr/status') return reply({ available: true, max_file_bytes: 25_000_000 });
  if (path === '/api/cloud/token') return reply({ error: 'fixture local ASR only' }, 503);
  if (path === '/api/asr/transcribe') {
    if (control.asrFailures-- > 0) return reply({ error: 'fixture failure' }, 503);
    return reply({ text: '请整理中文发布说明。', cleanup: 'success' });
  }
  if (path.endsWith('/connection')) return reply({ backend: path.split('/')[3], ready: true });
  if (path.endsWith('/models')) return reply({ ok: true, models: [] });
  return reply({});
};

export function SettingsEntry() {
  const location = useLocation();
  const navigate = useNavigate();
  return <button data-testid="settings-entry" onClick={() => navigate('/settings/general', { state: settingsOverlayNavigationState({ destinationPathname: '/settings/general', desktop: true, source: location, targetState: undefined }) })}>Settings</button>;
}
export function Settings() {
  const navigate = useNavigate();
  return <div className="h-full bg-background p-6"><h1>Fixture Settings</h1><button onClick={() => navigate(-1)}>Back to app</button></div>;
}
export function Conversation() {
  const { sessionId } = useParams();
  const location = useLocation();
  return <div data-testid="conversation">{sessionId}<pre data-testid="handoff">{JSON.stringify(location.state)}</pre></div>;
}
export function Fixture() {
  return <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}>
    <ToastProvider><ApiProvider><WorkbenchProjectsProvider>
      <main className="mx-auto min-h-dvh max-w-6xl bg-background p-4 text-foreground md:p-8">
        <SettingsOverlayRouteSurface fallbackElement={<div>Missing route</div>}>
          <Route path="/" element={<><SettingsEntry /><Workbench /></>} />
          <Route path="/chat/:sessionId" element={<Conversation />} />
          <Route path="/agents" element={<div>Agents destination</div>} />
          <Route path="/settings/general" element={<Settings />} />
        </SettingsOverlayRouteSurface>
      </main>
    </WorkbenchProjectsProvider></ApiProvider></ToastProvider>
  </InstanceAuthorizationContext.Provider>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><RouterProvider router={createHashRouter([{ path: '*', element: <Fixture /> }])} /></StrictMode>);
