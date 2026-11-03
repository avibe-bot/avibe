/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  getMainSession: vi.fn(),
  listVibeAgents: vi.fn(),
  getBackendConnection: vi.fn(),
  updateSession: vi.fn(),
  setFastModeDefault: vi.fn(),
}));
const posts = vi.hoisted(() => ({
  calls: [] as Array<{ path: string; body: Record<string, unknown> }>,
  answer: (() => Promise.resolve(new Response('{}', { status: 200 }))) as () => Promise<Response>,
}));
const newSession = vi.hoisted(() => ({
  projects: [] as { id: string; display_name: string; folder_path: string }[],
  target: undefined as { id: string; display_name: string; folder_path: string } | undefined,
  setSelected: vi.fn(),
  sending: false,
  loaded: true,
  uncertainSessionId: null,
  agents: [],
  agentRoute: {},
  setAgentRoute: vi.fn(),
  effectiveDefaultAgentName: 'codex',
  needsProject: false,
  error: null as string | null,
  upsertSelectProject: vi.fn(),
  send: vi.fn(),
  speed: null,
  setSpeed: vi.fn(),
}));
const authorization = vi.hoisted(() => ({
  capabilities: { can_chat: true, can_manage_projects: true, can_manage_instance: true, is_instance_owner: true },
}));
const viewport = vi.hoisted(() => ({ desktop: true }));
vi.hoisted(() => {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
});

vi.mock('../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../context/ApiContext')>()),
  useApi: () => api,
}));
vi.mock('../lib/apiFetch', () => ({
  apiFetch: (path: string, init?: RequestInit) => {
    if (init?.method !== 'POST') return Promise.resolve(new Response(JSON.stringify({ available: false })));
    posts.calls.push({ path, body: JSON.parse(String(init.body)) });
    return posts.answer();
  },
}));
vi.mock('../lib/useNewSession', () => ({ useNewSession: () => newSession }));
vi.mock('../lib/useIsDesktop', () => ({ useIsDesktop: () => viewport.desktop }));
vi.mock('../context/InstanceAuthorizationContext', () => ({ useInstanceAuthorization: () => authorization }));
// The picker's own tests own its menu; here it only says which route it was handed.
vi.mock('./workbench/AgentRoutePicker', () => ({
  AgentRoutePicker: ({ value, agentLocked, onChange }: {
    value: { model?: string | null }; agentLocked?: boolean; onChange: (patch: Record<string, unknown>) => void;
  }) => (
    <>
      <span data-testid="route">{`${agentLocked ? 'locked' : 'open'}:${value.model ?? ''}`}</span>
      <button type="button" onClick={() => onChange({ model: 'model-a' })}>pick-a</button>
      <button type="button" onClick={() => onChange({ model: 'model-b' })}>pick-b</button>
    </>
  ),
}));
vi.mock('./workbench/NewProjectDialog', () => ({ NewProjectDialog: () => <div role="dialog" aria-label="new-project" /> }));

import { BotsContext, type BotsState } from '../context/BotsContext';
import { ToastProvider } from '../context/ToastProvider';
import { WorkbenchInboxContext, type InboxState } from '../context/WorkbenchInboxContext';
import { WorkbenchProjectsContext, type WorkbenchProjectsTree } from '../context/WorkbenchProjectsContext';
import en from '../i18n/en.json';
import { resetCoalescedWrites } from '../lib/useCoalescedWrite';
import { resetOpenSessionRowWrites } from './workbench/sessionRowRefresh';
import type { Bot } from '../lib/bots';
import { Workbench } from './Workbench';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const bot = (handle: string, display_name: string, builtin = false): Bot => ({
  handle, display_name, avatar: { kind: 'mascot' }, role: '', agent_name: handle, session_id: `ses_${handle}`,
  position: builtin ? 0 : 1, builtin, hide_details: true,
});
const main = bot('main', 'Vibey', true);
const pm = bot('pm', 'PM');

const botsState = (overrides: Partial<BotsState> = {}): BotsState => ({
  enabled: true, bots: [main, pm], loaded: true, listed: true, mainBot: main, limit: null, atLimit: false,
  statusBySession: {},
  botForSession: () => undefined, botForAgent: () => undefined, botForHandle: () => undefined,
  displayName: (each) => each.display_name,
  afterWrite: async () => undefined, moveBot: async () => true, setHideDetails: async () => true,
  openProfile: () => undefined, profileRequest: null, closeProfile: () => undefined,
  ...overrides,
});
const memberBots = (): BotsState => botsState({ enabled: false, bots: [], mainBot: null });

const inbox = { unreadBySession: {}, totalUnread: 0, inboxSessions: [] } as unknown as InboxState;
const project = (id: string, name: string, lastActive: string) => ({
  id, display_name: name, folder_path: `/work/${name}`, last_active_at: lastActive, created_at: '2026-01-01T00:00:00Z',
});
const projectsTree = (projects: unknown[]) => ({ projects, activate: () => () => undefined }) as unknown as WorkbenchProjectsTree;

const Where = () => <div data-testid="where">{useLocation().pathname}</div>;

function renderHome({ bots = botsState(), projects = [] as unknown[] } = {}) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ToastProvider>
        <BotsContext.Provider value={bots}>
          <WorkbenchInboxContext.Provider value={inbox}>
            <WorkbenchProjectsContext.Provider value={projectsTree(projects)}>
              <MemoryRouter initialEntries={['/']}>
                <Routes>
                  <Route path="*" element={<Where />} />
                </Routes>
                <Routes>
                  <Route path="/" element={<Workbench />} />
                </Routes>
              </MemoryRouter>
            </WorkbenchProjectsContext.Provider>
          </WorkbenchInboxContext.Provider>
        </BotsContext.Provider>
      </ToastProvider>
    </I18nextProvider>,
  );
}

const connection = (auth: string) => ({
  ok: true, backend: 'vibey', installed: true, enabled: true, auth, application: 'applied', ready: auth !== 'none', entry_eligible: auth !== 'none',
});
const where = () => screen.getByTestId('where').textContent;
const textbox = () => screen.getByRole('textbox') as HTMLTextAreaElement;
const sendButton = () => screen.getByRole('button', { name: en.chat.compose.send });

beforeEach(() => {
  viewport.desktop = true;
  authorization.capabilities.can_manage_instance = true;
  posts.calls = [];
  posts.answer = () => Promise.resolve(new Response(JSON.stringify({ id: 'msg_1' }), { status: 200 }));
  api.getMainSession.mockResolvedValue({
    status: 200,
    session: { id: 'ses_main', agent_name: 'main', agent_backend: 'vibey', model: 'gpt-6-astra', reasoning_effort: 'high' },
  });
  api.listVibeAgents.mockResolvedValue({ ok: true, agents: [{ name: 'main', backend: 'vibey', model: 'gpt-6-astra' }], default_agent_name: 'vibey' });
  api.getBackendConnection.mockResolvedValue(connection('api_key'));
  api.updateSession.mockResolvedValue({});
  api.setFastModeDefault.mockResolvedValue({});
  newSession.projects = [];
  newSession.target = undefined;
  newSession.send.mockResolvedValue({ sessionId: 'ses_new' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // The Session writers are module state shared with the conversation; each case starts clean.
  resetCoalescedWrites();
  resetOpenSessionRowWrites();
});

describe('the Owner home (ui/design: home/home/c-ready-desktop)', () => {
  it('talks to the main Bot: the route is its own, and a message lands in its conversation, which opens', async () => {
    const user = userEvent.setup();
    renderHome();
    expect(await screen.findByTestId('route')).toHaveProperty('textContent', 'locked:gpt-6-astra');
    expect(api.getBackendConnection).toHaveBeenCalledWith('vibey');
    expect(screen.getByRole('heading').textContent).toMatch(/What’s next\?$/);
    await user.type(textbox(), 'Plan my week');
    await user.click(sendButton());
    await waitFor(() => expect(where()).toBe('/chat/ses_main'));
    expect(posts.calls).toEqual([{ path: '/api/sessions/ses_main/messages', body: { text: 'Plan my week' } }]);
    expect(newSession.send).not.toHaveBeenCalled();
  });

  it('keeps the draft and the page when the main Bot refuses the message', async () => {
    posts.answer = () => Promise.resolve(new Response(JSON.stringify({ error: 'Nope' }), { status: 400 }));
    const user = userEvent.setup();
    renderHome();
    await user.type(textbox(), 'Plan my week');
    await user.click(sendButton());
    expect(await screen.findByText('Nope')).toBeTruthy();
    expect(where()).toBe('/');
    expect(textbox().value).toBe('Plan my week');
  });

  it.each([
    ['the response is lost', () => Promise.reject(new TypeError('network'))],
    ['the dispatch timed out after the controller took it', () => Promise.resolve(new Response(JSON.stringify({ dispatch_error: 'dispatch_pending' }), { status: 504 }))],
    ['the gateway failed', () => Promise.resolve(new Response('bad gateway', { status: 502 }))],
  ])('does not offer a second send when %s', async (_case, answer) => {
    posts.answer = answer;
    const user = userEvent.setup();
    renderHome();
    await user.type(textbox(), 'Plan my week');
    await user.click(sendButton());
    const inspect = await screen.findByRole('link', { name: en.newSession.inspectSession });
    expect(inspect.getAttribute('href')).toBe('/chat/ses_main');
    expect(textbox().value).toBe('Plan my week');
    expect(sendButton()).toHaveProperty('disabled', true);
    expect(posts.calls).toHaveLength(1);
    expect(where()).toBe('/');
  });

  it('saves route picks one at a time to the main Bot, folding the ones made meanwhile into the next', async () => {
    let release: () => void = () => {};
    api.updateSession.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve({}); }));
    const user = userEvent.setup();
    renderHome();
    await screen.findByTestId('route');
    await user.click(screen.getByText('pick-a'));
    await user.click(screen.getByText('pick-b'));
    // The newest pick shows at once; its request waits behind the one in flight.
    expect(screen.getByTestId('route').textContent).toBe('locked:model-b');
    expect(api.updateSession.mock.calls).toEqual([['ses_main', { model: 'model-a' }]]);
    act(() => release());
    await waitFor(() => expect(api.updateSession).toHaveBeenCalledTimes(2));
    expect(api.updateSession.mock.calls[1]).toEqual(['ses_main', { model: 'model-b' }]);
  });

  it('puts each whole ask in the composer without sending, and starts the project ask in the latest project', async () => {
    const user = userEvent.setup();
    renderHome({ projects: [project('p1', 'older', '2026-10-01T00:00:00Z'), project('p2', 'avibe', '2026-11-01T00:00:00Z')] });
    await user.click(screen.getByRole('button', { name: en.workbench.home.asks.weeklyReport }));
    expect(textbox().value).toBe(en.workbench.home.asks.weeklyReportAsk);
    await user.click(screen.getByRole('button', { name: en.workbench.home.asks.startInProject }));
    expect(textbox().value).toBe('In avibe, ');
    expect(posts.calls).toEqual([]);
  });

  it('asks for a new project when there is none to start in', async () => {
    const user = userEvent.setup();
    renderHome();
    await user.click(screen.getByRole('button', { name: en.workbench.home.asks.startInProject }));
    expect(textbox().value).toBe(en.workbench.home.asks.startNewProjectAsk);
  });
});

describe('the Owner home without a model (ui/design: home/home/a-new-desktop)', () => {
  it('disables the composer and the asks, introduces the Bot, and offers the way to add a model', async () => {
    api.getBackendConnection.mockResolvedValue(connection('none'));
    const user = userEvent.setup();
    renderHome();
    const addModel = await screen.findByRole('button', { name: en.workbench.home.addModel });
    expect(textbox()).toHaveProperty('disabled', true);
    expect(textbox().placeholder).toBe(en.workbench.home.noModelPlaceholder);
    expect(screen.getByRole('button', { name: en.workbench.home.asks.addBot })).toHaveProperty('disabled', true);
    expect(screen.getByRole('heading').textContent).toMatch(/I’m Vibey\.$/);
    expect(screen.queryByTestId('route')).toBeNull();
    await user.click(addModel);
    expect(where()).toBe('/settings/models');
  });

  it('unlocks once the backend reports a model, read again when the window comes back', async () => {
    api.getBackendConnection.mockResolvedValue(connection('none'));
    renderHome();
    await screen.findByRole('button', { name: en.workbench.home.addModel });
    api.getBackendConnection.mockResolvedValue(connection('subscription'));
    act(() => { window.dispatchEvent(new Event('focus')); });
    await waitFor(() => expect(textbox()).toHaveProperty('disabled', false));
    expect(screen.queryByRole('button', { name: en.workbench.home.addModel })).toBeNull();
  });

  it.each([
    ['a failed read', () => api.getBackendConnection.mockRejectedValue(new Error('down'))],
    ['an answer about another backend', () => api.getBackendConnection.mockResolvedValue({ ...connection('none'), backend: 'codex' })],
  ])('is not "no model" on %s', async (_case, arrange) => {
    arrange();
    renderHome();
    await screen.findByTestId('route');
    await waitFor(() => expect(api.getBackendConnection).toHaveBeenCalled());
    expect(textbox()).toHaveProperty('disabled', false);
  });

  it('asks the backend of the main Bot’s own Agent', async () => {
    api.listVibeAgents.mockResolvedValue({ ok: true, agents: [{ name: 'main', backend: 'claude' }], default_agent_name: 'vibey' });
    renderHome();
    await waitFor(() => expect(api.getBackendConnection).toHaveBeenCalledWith('claude'));
  });
});

describe('who is working, beside the greeting', () => {
  it('opens the one working Bot directly', async () => {
    renderHome({ bots: botsState({ statusBySession: { ses_pm: 'running' } }) });
    const chip = screen.getByRole('link', { name: /1 working/ });
    expect(chip.getAttribute('href')).toBe('/chat/ses_pm');
  });

  it('lists several working Bots, and opens the one picked', async () => {
    const user = userEvent.setup();
    renderHome({ bots: botsState({ statusBySession: { ses_main: 'running', ses_pm: 'running' } }) });
    await user.click(screen.getByRole('button', { name: /2 working/ }));
    await user.click(await screen.findByRole('button', { name: /^PM/ }));
    expect(where()).toBe('/chat/ses_pm');
  });
});

describe('a member without Bots (ui/design: home/home/d-member-desktop)', () => {
  it('keeps the project chips and starts a Session in the chosen project', async () => {
    const chosen = project('p1', 'avibe', '2026-11-01T00:00:00Z');
    newSession.projects = [chosen];
    newSession.target = chosen;
    authorization.capabilities.can_manage_instance = false;
    const user = userEvent.setup();
    renderHome({ bots: memberBots(), projects: [chosen] });
    expect(screen.getByRole('button', { name: 'avibe' })).toBeTruthy();
    expect(screen.getByTestId('route').textContent).toBe('open:');
    expect(screen.getByRole('button', { name: en.workbench.home.asks.remember })).toBeTruthy();
    await user.type(textbox(), 'Fix the build');
    await user.click(sendButton());
    await waitFor(() => expect(where()).toBe('/chat/ses_new'));
    expect(newSession.send).toHaveBeenCalledWith('Fix the build', [], undefined);
    expect(api.getBackendConnection).not.toHaveBeenCalled();
    expect(posts.calls).toEqual([]);
  });
});

describe('the soft keyboard (ui/design: home/home/z-keyboard-phone)', () => {
  it('holds the page at its keyboard-free height while a field has focus on a touch device', async () => {
    viewport.desktop = false;
    Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, value: 5 });
    vi.resetModules();
    try {
      const { HomeLayout } = await import('./home/HomeLayout');
      const { container } = render(<HomeLayout><textarea aria-label="draft" /></HomeLayout>);
      const page = container.firstElementChild as HTMLElement;
      page.getBoundingClientRect = () => ({ height: 700 }) as DOMRect;
      fireEvent.focusIn(screen.getByLabelText('draft'));
      expect(page.style.minHeight).toBe('700px');
      // The keyboard shrinks the shell; the page keeps its height, so the group does not re-center.
      page.getBoundingClientRect = () => ({ height: 400 }) as DOMRect;
      fireEvent.focusIn(screen.getByLabelText('draft'));
      expect(page.style.minHeight).toBe('700px');
      fireEvent.focusOut(screen.getByLabelText('draft'));
      expect(page.style.minHeight).toBe('');
    } finally {
      Reflect.deleteProperty(navigator, 'maxTouchPoints');
      vi.resetModules();
    }
  });
});
