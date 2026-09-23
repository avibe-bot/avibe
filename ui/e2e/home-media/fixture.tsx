import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createHashRouter, RouterProvider, Route, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ApiProvider } from '../../src/context/ApiContext';
import { WorkbenchProjectsProvider } from '../../src/context/WorkbenchProjectsProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { ToastProvider } from '../../src/context/ToastProvider';
import { SettingsOverlayRouteSurface } from '../../src/components/settings/SettingsOverlayRouteSurface';
import { RouteSurfaceActivityBoundary } from '../../src/components/RouteSurfaceActivityBoundary';
import { NewSessionSheet } from '../../src/components/workbench/NewSessionSheet';
import { UnsavedChangesProvider } from '../../src/context/UnsavedChangesProvider';
import { Workbench } from '../../src/components/Workbench';
import { settingsOverlayNavigationState } from '../../src/lib/settingsOverlay';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import i18n from '../../src/i18n';
import type { FsEntry } from '../../src/lib/filesApi';
import '../../src/index.css';

const params = new URLSearchParams(location.search);
void i18n.changeLanguage(params.get('lang') ?? 'en');
document.documentElement.dataset.theme = params.get('theme') === 'light' ? 'light' : 'dark';
const firstProject = {
  id: 'project-中文', scope_id: 'scope-中文', display_name: '中文项目', folder_path: '/fixture/中文项目',
  created_at: '2026-09-01T00:00:00Z', last_active_at: null, archived: false,
  capabilities: { can_chat: true, has_folder: true },
};
const projects = params.has('empty') ? [] : [firstProject];
if (params.has('twoProjects')) projects.push({ ...firstProject, id: 'project-second', scope_id: 'scope-second', display_name: '第二个项目', folder_path: '/fixture/第二个项目' });
const agents = ['codex', 'claude'].map((name) => ({ id: `agent-${name}`, name, display_name: name, backend: name, enabled: true, archived: false, model: null }));
type Write = { path: string; body: Record<string, unknown> };
const writes: Write[] = [];
const sessions: Array<Record<string, unknown>> = [];
const uploads = new Map<string, { sessionId: string; token: string }>();
const createdFolders = new Map<string, FsEntry[]>();
const directoryEntries = (path: string): FsEntry[] => [
  { name: '另一个项目', kind: 'dir', size: null, mtime: null, ext: '' },
  { name: '.hidden', kind: 'dir', size: null, mtime: null, ext: '' },
  ...(createdFolders.get(path) ?? []),
];
const control = {
  writes,
  unexpectedRequests: [] as string[],
  uploadFailures: 0,
  uploadTerminal: false,
  messageTerminal: 0,
  messageMode: 'success' as 'success' | 'rejected' | 'unknown' | 'network',
  holdUpload: false,
  releaseUpload: () => {},
  holdMessage: false,
  releaseMessage: () => {},
  asrFailures: 0,
  holdCreate: false,
  releaseCreate: () => {},
  holdProject: false,
  projectFailure: false,
  releaseProject: () => {},
  messageCompletions: 0,
  projectCompletions: 0,
  heldBrowsePaths: [] as string[],
  pendingBrowses: [] as Array<{ path: string; release: () => void }>,
  browseCompletions: [] as string[],
  listRequests: [] as Array<{ path: string; showHidden: boolean }>,
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
    if (control.holdProject) await new Promise<void>((resolve) => { control.releaseProject = resolve; });
    control.projectCompletions++;
    if (control.projectFailure) return reply({ error: 'fixture project rejected' }, 500);
    const project = { ...firstProject, id: `project-${projects.length}`, scope_id: `scope-${projects.length}`, folder_path: body.folder_path, display_name: body.display_name || body.folder_path.split('/').pop() };
    projects.push(project);
    return reply(project);
  }
  if (path === '/api/projects' || path === '/api/workbench/projects-bootstrap') return reply({ projects, sessions: {} });
  if (path === '/api/browse') {
    // Compatibility resolution is separate from the Files listing. Held
    // navigation requests below intentionally block the listing phase only.
    return reply({ ok: true, path: body.path === '~' ? '/fixture' : body.path || '/fixture', parent: '/fixture', dirs: [{ name: '另一个项目', path: '/fixture/另一个项目' }] });
  }
  if (path === '/api/browse/favorites') return reply({ ok: true, favorites: [{ key: 'home', path: '/fixture' }] });
  if (path === '/api/files/list') {
    const directory = url.searchParams.get('path')!;
    const showHidden = url.searchParams.get('show_hidden') === '1';
    control.listRequests.push({ path: directory, showHidden });
    if (control.heldBrowsePaths.includes(directory)) {
      await new Promise<void>((resolve) => { control.pendingBrowses.push({ path: directory, release: resolve }); });
    }
    control.browseCompletions.push(directory);
    return reply({
      ok: true, path: directory, parent: directory.slice(0, directory.lastIndexOf('/')) || '/',
      entries: directoryEntries(directory).filter((entry) => showHidden || !entry.name.startsWith('.')),
      truncated: false, limit: 2000,
    });
  }
  if (path === '/api/files/search_names') {
    const root = url.searchParams.get('root')!;
    const query = url.searchParams.get('query')!;
    const showHidden = url.searchParams.get('show_hidden') === '1';
    return reply({
      ok: true, root, query, truncated: false, limit: 1000,
      results: directoryEntries(root)
        .filter((entry) => (showHidden || !entry.name.startsWith('.')) && entry.name.includes(query))
        .map((entry) => ({ ...entry, path: `${root}/${entry.name}`, rel: entry.name })),
    });
  }
  if (path === '/api/files/mkdir' && init?.method === 'POST') {
    const target = String(body.path);
    const parent = target.slice(0, target.lastIndexOf('/'));
    const name = target.slice(target.lastIndexOf('/') + 1);
    if (directoryEntries(parent).some((entry) => entry.name === name)) {
      return reply({ ok: false, error: { code: 'exists', message: 'Entry already exists' } }, 409);
    }
    createdFolders.set(parent, [...(createdFolders.get(parent) ?? []), { name, kind: 'dir', size: null, mtime: null, ext: '' }]);
    return reply({ ok: true });
  }
  if (path === '/api/sessions' && init?.method === 'POST') {
    if (control.holdCreate) await new Promise<void>((resolve) => { control.releaseCreate = resolve; });
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
    if (control.uploadTerminal) return reply({ error: { code: 'session_not_found' } }, 404);
    if (control.uploadFailures-- > 0) return reply({ error: { code: 'upload_failed' } }, 503);
    const token = `${sessionId}-${uploadId}`;
    uploads.set(token, { sessionId, token });
    return reply({ token, name: file.name, mime: file.type, size: file.size, kind: 'file', url: `/api/media/${token}` });
  }
  if (path.endsWith('/messages')) {
    const attachments = (body.content?.attachments ?? []) as Array<{ token: string }>;
    if (attachments.some((attachment) => uploads.get(attachment.token)?.sessionId !== path.split('/')[3])) return reply({ error: 'scope mismatch' }, 400);
    if (control.holdMessage) await new Promise<void>((resolve) => { control.releaseMessage = resolve; });
    control.messageCompletions++;
    if (control.messageTerminal) return reply({ error: 'session unavailable' }, control.messageTerminal);
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
  control.unexpectedRequests.push(`${init?.method ?? 'GET'} ${path}`);
  throw new Error(`Undeclared fixture request: ${path}`);
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
  const { sessionId: routeSessionId } = useParams();
  const location = useLocation();
  const sessionId = routeSessionId ?? location.pathname.split('/').at(-1);
  return <div data-testid="conversation">{sessionId}<pre data-testid="handoff">{JSON.stringify(location.state)}</pre></div>;
}
// Test-owned producer for C's frozen shell contract: sheet state lives outside
// route content, logical open stays true during Settings, activity alone changes.
function SheetHarness() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const settings = location.pathname.startsWith('/settings');
  useEffect(() => {
    const entry = (event: KeyboardEvent) => {
      if (event.altKey && event.code === 'KeyS') {
        event.preventDefault();
        navigate('/settings/general');
      }
    };
    window.addEventListener('keydown', entry);
    return () => window.removeEventListener('keydown', entry);
  }, [navigate]);
  return <>
    {settings ? <section data-testid="settings-foreground">
      <h1>Fixture Settings</h1><input aria-label="Settings value" />
      <button onClick={() => navigate(-1)}>Back to app</button>
      <button onClick={() => setOpen(false)}>Discard suspended sheet</button>
    </section> : location.pathname.startsWith('/chat/') ? <Conversation /> :
      <button onClick={() => setOpen(true)}>Open new session</button>}
    <RouteSurfaceActivityBoundary active={!settings}>
      <NewSessionSheet open={open} onOpen={() => setOpen(true)} onClose={() => setOpen(false)} />
    </RouteSurfaceActivityBoundary>
    <output data-testid="sheet-logical-open">{String(open)}</output>
  </>;
}
export function Fixture() {
  return <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}>
    <ToastProvider><ApiProvider><UnsavedChangesProvider><WorkbenchProjectsProvider>
      <main className="mx-auto min-h-dvh max-w-6xl bg-background p-4 text-foreground md:p-8">
        {params.get('surface') === 'sheet' ? <SheetHarness /> : <SettingsOverlayRouteSurface fallbackElement={<div>Missing route</div>}>
          <Route path="/" element={<><SettingsEntry /><Workbench /></>} />
          <Route path="/chat/:sessionId" element={<Conversation />} />
          <Route path="/agents" element={<div>Agents destination</div>} />
          <Route path="/settings/general" element={<Settings />} />
        </SettingsOverlayRouteSurface>}
      </main>
    </WorkbenchProjectsProvider></UnsavedChangesProvider></ApiProvider></ToastProvider>
  </InstanceAuthorizationContext.Provider>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><RouterProvider router={createHashRouter([{ path: '*', element: <Fixture /> }])} /></StrictMode>);
