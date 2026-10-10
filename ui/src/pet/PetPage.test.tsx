/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  RunningAgentsResult,
  WorkbenchEventHandlers,
  WorkbenchMessage,
  WorkbenchSession,
  WorkbenchSessionReadResult,
} from '@/context/ApiContext';

import { ApiError } from '@/context/ApiContext';

import { PET_BOUND_EVENT, PET_SUMMON_EVENT } from './petBridge';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const handlers = new Set<WorkbenchEventHandlers>();
const emit = (pick: (h: WorkbenchEventHandlers) => void) => act(() => handlers.forEach(pick));

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void };
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const session = (id: string, overrides: Partial<WorkbenchSession> = {}): WorkbenchSession => ({
  id,
  scope_id: 'scope',
  project_id: 'project',
  title: `Session ${id}`,
  agent_id: null,
  agent_name: null,
  agent_backend: 'claude',
  agent_variant: null,
  model: null,
  reasoning_effort: null,
  status: 'active',
  visibility: 'foreground',
  pinned: false,
  agent_status: 'idle',
  workdir: null,
  native_session_id: null,
  created_at: '2026-10-04T00:00:00Z',
  updated_at: '2026-10-04T00:00:00Z',
  last_active_at: null,
  metadata: {},
  ...overrides,
});

const message = (id: string, sessionId: string, overrides: Partial<WorkbenchMessage> = {}): WorkbenchMessage => ({
  id,
  scope_id: 'scope',
  session_id: sessionId,
  platform: 'avibe',
  author: 'agent',
  type: 'result',
  source: 'agent',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text: `reply ${id}`,
  content: {},
  metadata: {},
  created_at: '2026-10-04T00:00:00Z',
  updated_at: '2026-10-04T00:00:00Z',
  delivered_at: null,
  read_at: '2026-10-04T00:00:01Z',
  ...overrides,
});

const idleTurn = {
  in_flight: false,
  foreground: 'idle' as const,
  native_turn_started: false,
  pending_input_count: 0,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'connected' as const,
};

let setupDone = true;
let sessionReads: Record<string, () => Promise<WorkbenchSessionReadResult>> = {};
let tails: Record<string, WorkbenchMessage[]> = {};
let switcherSessions: WorkbenchSession[] = [];
let unreadBySession: Record<string, number> = {};
let runningAgents: RunningAgentsResult = { ok: true, agents: [], counts: { total: 0, active: 0, idle: 0, orphan: 0, by_backend: {} } };
const workingIn = (sessionId: string): RunningAgentsResult => ({
  ok: true,
  agents: [{
    backend: 'claude', state: 'active', base_session_id: sessionId, composite_key: null, workdir: null, pid: null,
    pid_shared: false, native_session_id: null, model: null, elapsed_seconds: 1, session_id: sessionId, title: null,
    platform: 'avibe', scope_type: 'project', scope_display_name: null, visibility: 'foreground',
    trigger_source: 'human', agent_name: 'claude', openable_in_chat: true,
  }],
  counts: { total: 1, active: 1, idle: 0, orphan: 0, by_backend: { claude: 1 } },
});
// Like the provider, a mark-read response installs a new unread map, so every
// consumer re-renders with a new inbox object.
let inboxVersion = 0;
const inboxListeners = new Set<() => void>();
const markRead = vi.fn(async (): Promise<boolean> => {
  inboxVersion += 1;
  inboxListeners.forEach((listener) => listener());
  return true;
});

const api = {
  getConfig: vi.fn(async () => (setupDone
    ? { mode: 'self_host', setup_state: { needs_setup: false } }
    : { mode: 'self_host', setup_state: { needs_setup: true } })),
  getSessionResult: vi.fn((id: string) => (sessionReads[id]
    ? sessionReads[id]()
    : Promise.resolve({ status: 200, session: session(id), capabilities: { can_chat: true } }))),
  listSessionMessages: vi.fn(async (id: string) => ({ messages: tails[id] ?? [], next_after_id: null, next_before_id: null })),
  getTurnState: vi.fn(async () => idleTurn),
  listSessions: vi.fn(async () => ({ sessions: switcherSessions, next_before_id: null })),
  getVaultRequests: vi.fn(async () => ({ requests: [] })),
  getRunningAgents: vi.fn(async (): Promise<RunningAgentsResult> => runningAgents),
  sendSessionMessage: vi.fn(async () => message('sent', 'S', { author: 'user', type: 'user' })),
  connectWorkbenchEvents: (h: WorkbenchEventHandlers) => {
    handlers.add(h);
    return () => handlers.delete(h);
  },
};

vi.mock('@/context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/context/ApiContext')>()),
  useApi: () => api,
}));
let canChat = true;
let canUseAgents = false;
vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: { can_chat: canChat, can_use_agents: canUseAgents } }),
}));
vi.mock('@/context/WorkbenchInboxContext', async () => {
  const { useSyncExternalStore } = await import('react');
  return {
    useWorkbenchInbox: () => {
      useSyncExternalStore((listener) => {
        inboxListeners.add(listener);
        return () => inboxListeners.delete(listener);
      }, () => inboxVersion);
      return { unreadBySession: { ...unreadBySession }, markRead };
    },
  };
});

// An in-memory Storage, so the test does not depend on the runtime's own
// localStorage (Node 25+ shadows jsdom's without a backing file).
const memoryStorage = (): Storage => {
  const items = new Map<string, string>();
  return {
    get length() { return items.size; },
    clear: () => items.clear(),
    getItem: (key) => items.get(key) ?? null,
    key: (index) => Array.from(items.keys())[index] ?? null,
    removeItem: (key) => { items.delete(key); },
    setItem: (key, value) => { items.set(key, String(value)); },
  };
};
Object.defineProperty(window, 'localStorage', { value: memoryStorage(), configurable: true });
// The window has focus unless a test says otherwise (jsdom reports none).
let windowFocused = true;
Object.defineProperty(document, 'hasFocus', { value: () => windowFocused, configurable: true });

// The dev bridge's stand-in for the shell: the binding and its revision.
const DEV_BINDING_KEY = 'avibe.pet.devBinding';
const devBinding = (): { binding: string | null; revision: number } => {
  const stored = window.localStorage.getItem(DEV_BINDING_KEY);
  return stored ? JSON.parse(stored) : { binding: null, revision: 0 };
};
const devBind = (sessionId: string | null) => {
  const next = { binding: sessionId, revision: devBinding().revision + 1 };
  window.localStorage.setItem(DEV_BINDING_KEY, JSON.stringify(next));
  return next;
};
// A change made elsewhere (the main window's "Show in pet"), reported by event.
const bound = (sessionId: string | null) => act(() => {
  const next = devBind(sessionId);
  window.dispatchEvent(new CustomEvent(PET_BOUND_EVENT, { detail: { session_id: sessionId, revision: next.revision } }));
});
const shellBound = (sessionId: string | null, revision: number) => act(() => {
  window.dispatchEvent(new CustomEvent(PET_BOUND_EVENT, { detail: { session_id: sessionId, revision } }));
});
const summon = (intent: 'listen' | 'show') => act(() => {
  window.dispatchEvent(new CustomEvent(PET_SUMMON_EVENT, { detail: { intent } }));
});
const pose = () => document.querySelector('.pet-avatar')?.getAttribute('data-pose');

// The shell store lives for one document, so each test loads a fresh module
// graph instead of resetting it.
let PetPage: typeof import('./PetPage').PetPage;

beforeEach(async () => {
  vi.resetModules();
  ({ PetPage } = await import('./PetPage'));
  setupDone = true;
  windowFocused = true;
  canChat = true;
  canUseAgents = false;
  runningAgents = { ok: true, agents: [], counts: { total: 0, active: 0, idle: 0, orphan: 0, by_backend: {} } };
  api.getRunningAgents.mockClear();
  sessionReads = {};
  tails = {};
  switcherSessions = [];
  unreadBySession = {};
  markRead.mockClear();
  inboxVersion = 0;
  api.getSessionResult.mockClear();
  api.listSessions.mockClear();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  handlers.clear();
  api.sendSessionMessage.mockClear();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: unknown }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('PetPage setup', () => {
  it('stays on its own setup-pending state instead of redirecting to setup', async () => {
    setupDone = false;
    render(<PetPage />);
    expect(await screen.findByText('pet.setupPending')).toBeTruthy();
    expect(window.location.pathname).not.toBe('/setup');
  });

  it('leaves setup-pending on the next summon once setup finished elsewhere', async () => {
    setupDone = false;
    render(<PetPage />);
    await screen.findByText('pet.setupPending');
    setupDone = true;
    summon('show');
    await waitFor(() => expect(screen.queryByText('pet.setupPending')).toBeNull());
    expect(api.getConfig).toHaveBeenLastCalledWith({ cache: false });
  });

  it('also re-reads setup when the window regains focus', async () => {
    setupDone = false;
    render(<PetPage />);
    await screen.findByText('pet.setupPending');
    setupDone = true;
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await waitFor(() => expect(screen.queryByText('pet.setupPending')).toBeNull());
  });
});

describe('PetPage binding', () => {
  it('keeps B bound when a late 404 for A arrives after switching', async () => {
    const lateA = deferred<WorkbenchSessionReadResult>();
    sessionReads.A = () => lateA.promise;
    devBind('A');
    render(<PetPage />);
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('A'));
    await bound('B');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('B'));
    await act(async () => lateA.resolve({ status: 404, session: null }));
    summon('show');
    expect(await screen.findByText('Session B')).toBeTruthy();
    expect(devBinding().binding).toBe('B');
  });

  it('clears a persisted binding to a Runtime-owned session and opens the switcher', async () => {
    sessionReads['ses-workspace-notices'] = async () => ({
      status: 200,
      session: session('ses-workspace-notices', { visibility: 'system' }),
    });
    switcherSessions = [session('fresh', { title: 'Brand new' })];
    devBind('ses-workspace-notices');
    render(<PetPage />);
    await waitFor(() => expect(devBinding().binding).toBeNull());
    summon('listen');
    expect(await screen.findByText('Brand new')).toBeTruthy();
  });

  it('lists new sessions in the switcher and leaves out read-only ones', async () => {
    switcherSessions = [
      session('new', { title: 'No reply yet' }),
      session('old', { title: 'Archived', status: 'archived' }),
    ];
    render(<PetPage />);
    summon('listen');
    expect(await screen.findByText('No reply yet')).toBeTruthy();
    expect(screen.queryByText('Archived')).toBeNull();
    await userEvent.click(screen.getByText('No reply yet'));
    expect(devBinding().binding).toBe('new');
  });
});

describe('PetPage state', () => {
  it('offers an open quick-reply group in the panel without asking for attention', async () => {
    const asking = message('q', 'S', { content: { quick_replies: ['Yes', 'No'] } });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), asking];
    devBind('S');
    render(<PetPage />);
    summon('show');
    expect(await screen.findByText('Yes')).toBeTruthy();
    expect(pose()).toBe('idle');
  });

  it('asks for attention only for a pending vault request', async () => {
    api.getVaultRequests.mockImplementation(async () => ({ requests: [{ id: 'v1', request_type: 'access' }] } as never));
    try {
      devBind('S');
      render(<PetPage />);
      await waitFor(() => expect(pose()).toBe('needs_input'));
    } finally {
      api.getVaultRequests.mockImplementation(async () => ({ requests: [] }));
    }
  });

  it('derives Blocked from the first session read, with no provider row', async () => {
    sessionReads.S = async () => ({ status: 200, session: session('S', { agent_status: 'failed' }) });
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('blocked'));
  });

  it('re-reads turn state on any runs.updated, even for another executor session', async () => {
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(api.getTurnState).toHaveBeenCalled());
    const before = api.getTurnState.mock.calls.length;
    await emit((h) => h.onRunsUpdated?.({ run_id: 'r', status: 'running', session_id: 'executor' } as never));
    await waitFor(() => expect(api.getTurnState.mock.calls.length).toBeGreaterThan(before));
  });

  it('marks read through the last rendered unread result only when all unread rows are loaded', async () => {
    tails.S = [
      message('r1', 'S'),
      message('u', 'S', { author: 'user', type: 'user' }),
      message('r2', 'S', { read_at: null }),
      message('r3', 'S', { read_at: null }),
    ];
    unreadBySession = { S: 2 };
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('ready'));
    summon('show');
    await screen.findByText('reply r3');
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r3'));
  });

  it('does not mark read when unread rows may reach past the loaded tail', async () => {
    tails.S = [message('r2', 'S', { read_at: null })];
    api.listSessionMessages.mockImplementationOnce(async (id: string) => ({
      messages: tails[id] ?? [], next_after_id: null, next_before_id: 'older',
    }));
    unreadBySession = { S: 5 };
    devBind('S');
    render(<PetPage />);
    summon('show');
    expect(await screen.findByText('pet.moreInAvibe')).toBeTruthy();
    expect(markRead).not.toHaveBeenCalled();
  });
});

describe('PetPage other conversations', () => {
  const dataState = () => document.querySelector('[data-state]')?.getAttribute('data-state');

  it('looks busy while another conversation\'s agent works, and the bound session stays idle', async () => {
    canUseAgents = true;
    runningAgents = workingIn('T');
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('running'));
    expect(dataState()).toBe('idle');
    runningAgents = { ...runningAgents, agents: [] };
    await emit((h) => h.onTurnEnd?.({ session_id: 'T' }));
    await waitFor(() => expect(pose()).toBe('idle'));
  });

  it('keeps re-reading a started turn until the backend registers it', async () => {
    canUseAgents = true;
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(api.getRunningAgents).toHaveBeenCalled());
    // turn.start lands before the agent is active in the snapshot.
    await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
    await waitFor(() => expect(api.getRunningAgents.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(pose()).toBe('idle');
    runningAgents = workingIn('T');
    await waitFor(() => expect(pose()).toBe('running'), { timeout: 5000 });
    // Registered: no more retries for it.
    const settled = api.getRunningAgents.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(api.getRunningAgents.mock.calls.length).toBe(settled);
  }, 15000);

  it('re-reads on every event that can change who is working, and catches up on visibility', async () => {
    const { REFRESH_TRIGGERS } = await import('./useConversationAgentWorking');
    canUseAgents = true;
    runningAgents = workingIn('T');
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('running'));
    for (const trigger of REFRESH_TRIGGERS) {
      const before = api.getRunningAgents.mock.calls.length;
      await emit((h) => (h[trigger] as ((data: unknown) => void) | undefined)?.({ session_id: 'T', scope_id: null, event: 'updated' }));
      await waitFor(() => expect(api.getRunningAgents.mock.calls.length, trigger).toBeGreaterThan(before));
    }
    // T moved to the background: its agent no longer counts.
    runningAgents = { ...runningAgents, agents: runningAgents.agents.map((agent) => ({ ...agent, visibility: 'background' })) };
    await emit((h) => h.onSessionActivity?.({ session_id: 'T', scope_id: null, event: 'visibility', visibility: 'background' }));
    await waitFor(() => expect(pose()).toBe('idle'));
  });

  it('also looks busy with no binding at all', async () => {
    canUseAgents = true;
    runningAgents = workingIn('T');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('running'));
  });

  it('never reads other conversations without the agents permission', async () => {
    runningAgents = workingIn('T');
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(api.getTurnState).toHaveBeenCalled());
    expect(pose()).toBe('idle');
    expect(api.getRunningAgents).not.toHaveBeenCalled();
  });
});

describe('PetPage review fixes', () => {
  it('marks a rendered row read once, however often the inbox state changes', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByText('reply r1');
    await waitFor(() => expect(markRead).toHaveBeenCalled());
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(markRead).toHaveBeenCalledTimes(1);
  });

  it('does not put a reply for A into B when the binding changes mid-send', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    tails.B = [message('b1', 'B', { text: 'B only' })];
    devBind('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'hello{Enter}');
    await bound('B');
    await screen.findByText('B only');
    await act(async () => pending.resolve(message('a-sent', 'A', { author: 'user', type: 'user', text: 'sent to A' })));
    // B neither shows A's row nor enters A's post-send Running grace.
    expect(screen.queryByText('sent to A')).toBeNull();
    expect(screen.queryByText('pet.state.running')).toBeNull();
    expect(pose()).toBe('idle');
  });

  it('acts on the binding pet_ready returns together with a pending summon', async () => {
    const ready = deferred<unknown>();
    const invoke = vi.fn((command: string) => (command === 'pet_ready'
      ? ready.promise
      : Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' })));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    // The surface is mounted and subscribed before the shell answers.
    await screen.findByLabelText('pet.toggle');
    await act(async () => ready.resolve({ binding: 'S', revision: 1, summon_pending: { intent: 'listen' } }));
    // The summon opens the bound session's input, not the switcher.
    expect(await screen.findByLabelText('pet.inputPlaceholder')).toBeTruthy();
    expect(api.listSessions).not.toHaveBeenCalled();
  });
});

describe('PetPage review fixes, round 2', () => {
  it('does not mark replies read while the switcher hides them', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    switcherSessions = [session('S'), session('T')];
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByText('reply r1');
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
    // A newer reply arrives while the user is looking at the switcher.
    await userEvent.click(screen.getByLabelText('pet.switchSession'));
    await screen.findByText('Session T');
    await emit((h) => h.onMessageNew?.(message('r2', 'S', { read_at: null })));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(markRead).toHaveBeenCalledTimes(1);
  });

  it('retries a failed mark-read the next time the reply is shown', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    markRead.mockImplementationOnce(async (): Promise<boolean> => {
      throw new Error('offline');
    });
    devBind('S');
    render(<PetPage />);
    summon('show');
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    summon('show');
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
    expect(markRead).toHaveBeenLastCalledWith('S', 'r1');
  });

  it('keeps a draft typed after submitting when the send completes', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    devBind('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'first{Enter}');
    await userEvent.type(input, ' and more');
    await act(async () => pending.resolve(message('sent', 'S', { author: 'user', type: 'user' })));
    expect(input.value).toBe('first and more');
  });
});

describe('PetPage review fixes, round 3', () => {
  it('sends a draft once however often Enter is pressed while the POST is pending', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    devBind('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'once{Enter}{Enter}{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(message('sent', 'S', { author: 'user', type: 'user' })));
  });

  it('marks a reply read when a hidden, still-open pet is shown again', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    let visibility: DocumentVisibilityState = 'hidden';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
    try {
      devBind('S');
      render(<PetPage />);
      summon('show');
      await screen.findByText('reply r1');
      await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
      expect(markRead).not.toHaveBeenCalled();
      visibility = 'visible';
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r1'));
    } finally {
      delete (document as { visibilityState?: unknown }).visibilityState;
    }
  });

  it('keeps a bind that lands while pet_ready is still answering', async () => {
    const ready = deferred<unknown>();
    const invoke = vi.fn((command: string) => (command === 'pet_ready'
      ? ready.promise
      : Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' })));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    await screen.findByLabelText('pet.toggle');
    // The shell moved to B (revision 2) after answering pet_ready at revision 1.
    await shellBound('B', 2);
    await act(async () => ready.resolve({ binding: 'A', revision: 1, summon_pending: null }));
    summon('show');
    expect(await screen.findByText('Session B')).toBeTruthy();
  });
});

describe('PetPage independent sweep', () => {
  it('retries mark-read the server did not apply, without a network error', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    markRead.mockImplementationOnce(async () => false);
    let visibility: DocumentVisibilityState = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
    try {
      devBind('S');
      render(<PetPage />);
      summon('show');
      await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
      // The panel stays open; the window is hidden and shown again.
      visibility = 'hidden';
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      visibility = 'visible';
      act(() => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
    } finally {
      delete (document as { visibilityState?: unknown }).visibilityState;
    }
  });

  it('lands a bound summon on the session even if the switcher was left open', async () => {
    switcherSessions = [session('S')];
    render(<PetPage />);
    summon('show');
    expect(await screen.findByText('Session S')).toBeTruthy();
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await bound('S');
    summon('listen');
    expect(await screen.findByLabelText('pet.inputPlaceholder')).toBeTruthy();
  });

  it('clears sent text even when the session changed while it was sending', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    devBind('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'deploy{Enter}');
    await bound('B');
    await act(async () => pending.resolve(message('a-sent', 'A', { author: 'user', type: 'user' })));
    expect((screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement).value).toBe('');
  });

  it('does not mark read through rows trimmed from a long live tail', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 2 };
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(api.listSessionMessages).toHaveBeenCalled());
    // A long turn: many transcript rows arrive live, pushing r1 out of the tail.
    await emit((h) => {
      for (let index = 0; index < 70; index += 1) {
        h.onMessageNew?.(message(`n${index}`, 'S', { author: 'user', type: 'user', text: `note ${index}` }));
      }
      h.onMessageNew?.(message('r2', 'S', { read_at: null, text: 'reply r2' }));
    });
    summon('show');
    expect(await screen.findByText('pet.moreInAvibe')).toBeTruthy();
    expect(markRead).not.toHaveBeenCalled();
  });
});

describe('PetPage review fixes, round 4', () => {
  it('goes back to the binding the shell still holds when a pick cannot be persisted', async () => {
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'A', revision: 1, summon_pending: null });
      if (command === 'pet_bind') return Promise.reject(new Error('disk full'));
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    switcherSessions = [session('A'), session('B')];
    render(<PetPage />);
    summon('show');
    expect(await screen.findByText('Session A')).toBeTruthy();
    await userEvent.click(screen.getByLabelText('pet.switchSession'));
    await userEvent.click(await screen.findByText('Session B'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_bind', { sessionId: 'B' }));
    // The header shows the session the shell still holds.
    await waitFor(() => expect(api.getSessionResult).toHaveBeenLastCalledWith('A'));
  });
});

describe('PetPage review fixes, round 5', () => {
  it('stays unbound, without a validation loop, when the shell cannot persist a clear', async () => {
    sessionReads.A = async () => ({ status: 404, session: null });
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'A', revision: 1, summon_pending: null });
      if (command === 'pet_unbind') return Promise.reject(new Error('disk full'));
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    switcherSessions = [session('B')];
    render(<PetPage />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_unbind', { sessionId: 'A' }));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    summon('show');
    // The invalid session is never offered as usable: the switcher shows.
    expect(await screen.findByText('Session B')).toBeTruthy();
    expect(api.getSessionResult.mock.calls.filter(([id]) => id === 'A')).toHaveLength(1);
    expect(invoke.mock.calls.filter(([command]) => command === 'pet_unbind')).toHaveLength(1);
  });
});

describe('PetPage review fixes, round 6', () => {
  it('accepts no input for a binding whose session has not been validated yet', async () => {
    const validation = deferred<WorkbenchSessionReadResult>();
    sessionReads.S = () => validation.promise;
    devBind('S');
    render(<PetPage />);
    summon('listen');
    await screen.findByLabelText('pet.panel');
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
    await act(async () => validation.resolve({
      status: 200,
      session: session('S'),
      capabilities: { can_chat: true },
    }));
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'hello{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
  });

  it('starts an empty draft when the binding changes', async () => {
    devBind('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'meant for A');
    await bound('B');
    await screen.findByText('Session B');
    expect((screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement).value).toBe('');
  });

  it('keeps a pick made before pet_ready answers over the older answer', async () => {
    const ready = deferred<unknown>();
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return ready.promise;
      if (command === 'pet_bind') return Promise.resolve({ binding: 'B', revision: 2, shown: false });
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    switcherSessions = [session('B')];
    render(<PetPage />);
    await userEvent.click(await screen.findByLabelText('pet.toggle'));
    await userEvent.click(await screen.findByText('Session B'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_bind', { sessionId: 'B' }));
    // The shell answered pet_ready at revision 1, before it applied the pick.
    await act(async () => ready.resolve({ binding: 'A', revision: 1, summon_pending: null }));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(api.getSessionResult).not.toHaveBeenCalledWith('A');
    expect(api.getSessionResult).toHaveBeenLastCalledWith('B');
  });
});

describe('PetPage review fixes, round 7', () => {
  it('keeps a live summon that lands before the pet_ready answer', async () => {
    const ready = deferred<unknown>();
    const invoke = vi.fn((command: string) => (command === 'pet_ready'
      ? ready.promise
      : Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' })));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    tails.S = [];
    render(<PetPage />);
    await screen.findByLabelText('pet.toggle');
    // The live summon is newer than the pending one pet_ready still carries.
    await shellBound('S', 1);
    summon('show');
    await screen.findByText('Session S');
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await act(async () => ready.resolve({ binding: 'S', revision: 1, summon_pending: { intent: 'listen' } }));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    // The stale pending summon did not reopen the panel.
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();
  });
});

describe('PetPage review fixes, round 8', () => {
  it('does not replay a reply it already marked read when a newer one arrives', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null, text: 'first reply' })];
    unreadBySession = { S: 1 };
    markRead.mockImplementationOnce(async () => {
      // The server applied the read: the tail now reports r1 read.
      tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { text: 'first reply' })];
      return true;
    });
    devBind('S');
    render(<PetPage />);
    summon('show');
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r1'));
    await waitFor(() => expect(api.listSessionMessages.mock.calls.length).toBeGreaterThan(1));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    const r2 = message('r2', 'S', { read_at: null, text: 'second reply' });
    tails.S = [...tails.S, r2];
    await emit((h) => h.onMessageNew?.(r2));
    await screen.findByText('second reply');
    expect(screen.queryByText('first reply')).toBeNull();
  });

  it('keeps one message in flight across text and quick replies', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S', { content: { quick_replies: ['Yes', 'No'] } })];
    devBind('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.click(await screen.findByText('Yes'));
    await userEvent.type(input, 'also this{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(message('sent', 'S', { author: 'user', type: 'user' })));
  });

  it('does not clear a draft typed in another session by an older send', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    devBind('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'continue{Enter}');
    await bound('B');
    await screen.findByText('Session B');
    const inputB = screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(inputB, 'continue');
    await act(async () => pending.resolve(message('a-sent', 'A', { author: 'user', type: 'user' })));
    expect((screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement).value).toBe('continue');
  });
});

describe('PetPage review fixes, round 9', () => {
  it('shows a background-work count on the collapsed pet without changing its pose', async () => {
    api.getTurnState.mockImplementation(async () => ({
      ...idleTurn,
      background_activities: [
        { id: 'r1', backend: 'codex', runtime_key: 'k', session_id: 'x', kind: 'agent_run', status: 'running', description: null, started_at: '', updated_at: '' },
        { id: 'w1', backend: 'codex', runtime_key: 'k', session_id: 'x', kind: 'watch', status: 'running', description: null, started_at: '', updated_at: '' },
      ],
    }) as never);
    try {
      devBind('S');
      render(<PetPage />);
      expect(await screen.findByLabelText('chat.activities.running')).toBeTruthy();
      expect(screen.getByLabelText('chat.activities.running').textContent).toBe('2');
      expect(pose()).toBe('idle');
    } finally {
      api.getTurnState.mockImplementation(async () => idleTurn);
    }
  });
});

describe('PetPage review fixes, round 10', () => {
  it('does not mark a reply read while another app has focus', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null, text: 'unseen' })];
    unreadBySession = { S: 1 };
    windowFocused = false;
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByText('unseen');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(markRead).not.toHaveBeenCalled();
    windowFocused = true;
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r1'));
  });

  it('reverts a failed pick to the binding the shell confirmed, not an earlier pick', async () => {
    const binds: Record<string, ReturnType<typeof deferred<unknown>>> = { B: deferred(), C: deferred() };
    const invoke = vi.fn((command: string, args?: { sessionId?: string }) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'A', revision: 1, summon_pending: null });
      if (command === 'pet_bind') return binds[args?.sessionId ?? ''].promise;
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    switcherSessions = [session('A'), session('B'), session('C')];
    render(<PetPage />);
    summon('show');
    await screen.findByText('Session A');
    await userEvent.click(screen.getByLabelText('pet.switchSession'));
    await userEvent.click(await screen.findByText('Session B'));
    await userEvent.click(await screen.findByLabelText('pet.switchSession'));
    await userEvent.click(await screen.findByText('Session C'));
    await act(async () => binds.B.reject(new Error('disk full')));
    await act(async () => binds.C.reject(new Error('disk full')));
    await waitFor(() => expect(api.getSessionResult).toHaveBeenLastCalledWith('A'));
  });
});

describe('PetPage review fixes, round 12', () => {
  it('keeps the panel matching the native frame when a resize fails', async () => {
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') return Promise.reject(new Error('no monitor'));
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    await userEvent.click(await screen.findByLabelText('pet.toggle'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    await waitFor(() => expect(screen.queryByLabelText('pet.panel')).toBeNull());
    expect(screen.getByLabelText('pet.toggle').getAttribute('aria-expanded')).toBe('false');
  });
});

describe('PetPage review fixes, round 13', () => {
  it('offers no composer or quick replies without the chat capability', async () => {
    canChat = false;
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S', { content: { quick_replies: ['Yes'] } })];
    devBind('S');
    render(<PetPage />);
    summon('listen');
    await screen.findByLabelText('pet.panel');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();
    expect(screen.queryByText('Yes')).toBeNull();
  });

  it('offers no composer or quick replies when the bound session is project-viewer', async () => {
    canChat = true;
    sessionReads.S = async () => ({
      status: 200,
      session: session('S'),
      capabilities: { can_chat: false },
    });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S', { content: { quick_replies: ['Yes'] } })];
    devBind('S');
    render(<PetPage />);
    summon('listen');
    await screen.findByLabelText('pet.panel');
    await screen.findByText('Session S');
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();
    expect(screen.queryByText('Yes')).toBeNull();
    await userEvent.keyboard('should not send{Enter}');
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
  });

  it('ends the post-send Running grace on an authoritative turn.end', async () => {
    devBind('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await userEvent.type(input, 'quick{Enter}');
    await waitFor(() => expect(pose()).toBe('running'));
    await emit((h) => h.onTurnEnd?.({ session_id: 'S' }));
    await waitFor(() => expect(pose()).toBe('idle'));
  });

  it('drops what it read and unbinds when authorization to the session is lost', async () => {
    tails.S = [message('r1', 'S', { text: 'private reply' })];
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByText('private reply');
    sessionReads.S = async () => ({ status: 403, session: null });
    await emit((h) => h.onAuthorizationChanged?.({}));
    await waitFor(() => expect(screen.queryByText('private reply')).toBeNull());
    await waitFor(() => expect(devBinding().binding).toBeNull());
  });
});

describe('PetPage async closure', () => {
  // Binding order: the shell's revision decides, and picks reach it in order.
  it('sends picks one at a time and ignores a report older than the newest answer', async () => {
    const binds: Record<string, Deferred<unknown>> = { B: deferred(), C: deferred() };
    const invoke = vi.fn((command: string, args?: { sessionId?: string }) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'A', revision: 1, summon_pending: null });
      if (command === 'pet_bind') return binds[args?.sessionId ?? ''].promise;
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    switcherSessions = [session('A'), session('B'), session('C')];
    render(<PetPage />);
    summon('show');
    await screen.findByText('Session A');
    await userEvent.click(screen.getByLabelText('pet.switchSession'));
    await userEvent.click(await screen.findByText('Session B'));
    await userEvent.click(await screen.findByLabelText('pet.switchSession'));
    await userEvent.click(await screen.findByText('Session C'));
    // C waits for B's answer, so the shell sees the picks in the user's order.
    expect(invoke.mock.calls.filter(([command]) => command === 'pet_bind')).toEqual([['pet_bind', { sessionId: 'B' }]]);
    await act(async () => binds.B.resolve({ binding: 'B', revision: 2, shown: false }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_bind', { sessionId: 'C' }));
    await act(async () => binds.C.resolve({ binding: 'C', revision: 3, shown: false }));
    // B's own pet:bound arrives late, after the answer for C.
    await shellBound('B', 2);
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(api.getSessionResult).toHaveBeenLastCalledWith('C');
    expect(await screen.findByText('Session C')).toBeTruthy();
  });

  // Read freshness: what was read under the old authorization goes at once.
  it('drops switcher rows at once when authorization changes, then re-reads', async () => {
    switcherSessions = [session('X', { title: 'Revoked project' })];
    render(<PetPage />);
    summon('show');
    await screen.findByText('Revoked project');
    const reread = deferred<{ sessions: WorkbenchSession[]; next_before_id: null }>();
    api.listSessions.mockImplementationOnce(() => reread.promise);
    await emit((h) => h.onAuthorizationChanged?.({}));
    expect(screen.queryByText('Revoked project')).toBeNull();
    await act(async () => reread.resolve({ sessions: [session('Y', { title: 'Still allowed' })], next_before_id: null }));
    expect(await screen.findByText('Still allowed')).toBeTruthy();
  });

  // Unknown outcomes are not treated as answers.
  it('does not mark read from live rows that land before the first tail read', async () => {
    const tail = deferred<{ messages: WorkbenchMessage[]; next_after_id: null; next_before_id: string | null }>();
    api.listSessionMessages.mockImplementationOnce(() => tail.promise);
    unreadBySession = { S: 3 };
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByLabelText('pet.panel');
    await emit((h) => h.onMessageNew?.(message('live', 'S', { read_at: null })));
    await screen.findByText('reply live');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(markRead).not.toHaveBeenCalled();
    // The snapshot shows older unread rows past the tail: still not marked.
    await act(async () => tail.resolve({
      messages: [message('older', 'S', { read_at: null }), message('live', 'S', { read_at: null })],
      next_after_id: null,
      next_before_id: 'earlier',
    }));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(markRead).not.toHaveBeenCalled();
  });

  it('keeps the draft and closes input when a send may have been admitted', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S', { content: { quick_replies: ['Yes'] } })];
    api.sendSessionMessage.mockRejectedValueOnce(new ApiError('dispatch pending', 504, null));
    devBind('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await userEvent.type(input, 'deploy{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    expect(input.value).toBe('deploy');
    expect(screen.queryByText('Yes')).toBeNull();
    await userEvent.type(input, '{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    // Looking at the conversation in Avibe lifts it.
    const notice = screen.getByRole('status');
    await userEvent.click(within(notice).getByRole('button', { name: 'pet.openInAvibe' }));
    expect(screen.queryByText('newSession.sendUncertain')).toBeNull();
    await userEvent.type(input, '{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(2);
  });

  it('keeps input open when the server refused the send', async () => {
    api.sendSessionMessage.mockRejectedValueOnce(new ApiError('conflict', 409, null));
    devBind('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await userEvent.type(input, 'again{Enter}');
    await waitFor(() => expect(api.sendSessionMessage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('newSession.sendUncertain')).toBeNull();
    await userEvent.type(input, '{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(2);
  });

  it('leaves setup-pending when one of two overlapping checks sees setup finished', async () => {
    setupDone = false;
    render(<PetPage />);
    await screen.findByText('pet.setupPending');
    const seesDone = deferred<unknown>();
    api.getConfig
      .mockImplementationOnce(() => seesDone.promise as never)
      .mockImplementationOnce(() => Promise.reject(new Error('network')));
    // One wake: focus, then the summon, each re-reading setup.
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    summon('show');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await act(async () => seesDone.resolve({ mode: 'self_host', setup_state: { needs_setup: false } }));
    await waitFor(() => expect(screen.queryByText('pet.setupPending')).toBeNull());
  });
});

describe('PetPage live convergence', () => {
  const settle = () => act(async () => new Promise((resolve) => setTimeout(resolve, 20)));

  it('keeps the send lock when opening the conversation fails', async () => {
    api.sendSessionMessage.mockRejectedValueOnce(new ApiError('dispatch pending', 504, null));
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
      if (command === 'pet_open') return Promise.reject(new Error('main window gone'));
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' })];
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await settle();
    await userEvent.type(input, 'to S{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    const notice = screen.getByRole('status');
    await userEvent.click(within(notice).getByRole('button', { name: 'pet.openInAvibe' }));
    await settle();
    expect(screen.getByText('newSession.sendUncertain')).toBeTruthy();
    await userEvent.type(input, '{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
  });

  it('keeps each session closed after its own uncertain send', async () => {
    api.sendSessionMessage
      .mockRejectedValueOnce(new ApiError('dispatch pending', 504, null))
      .mockRejectedValueOnce(new ApiError('dispatch pending', 504, null));
    devBind('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await settle();
    await userEvent.type(input, 'to A{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();

    await bound('B');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('B'));
    const inputB = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await settle();
    expect(screen.queryByText('newSession.sendUncertain')).toBeNull();
    await userEvent.type(inputB, 'to B{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();

    await bound('A');
    await settle();
    expect(screen.getByText('newSession.sendUncertain')).toBeTruthy();
    await userEvent.type(screen.getByLabelText('pet.inputPlaceholder'), 'again{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(2);
  });

  it('keeps an already-open panel when a later expand request fails', async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    let expands = 0;
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') {
        expands += 1;
        return expands === 1 ? first.promise : second.promise;
      }
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' })];
    render(<PetPage />);
    summon('show');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    await act(async () => first.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
    await screen.findByLabelText('pet.panel');

    summon('show');
    await waitFor(() => expect(invoke.mock.calls.filter(([c]) => c === 'pet_set_expanded')).toHaveLength(2));
    await act(async () => second.reject(new Error('already expanded')));
    await settle();
    expect(screen.getByLabelText('pet.panel')).toBeTruthy();
  });

  it('shows the panel and marks replies read only once the shell has grown the frame', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    const grow = deferred<unknown>();
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') return grow.promise;
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    summon('show');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    await settle();
    expect(screen.queryByLabelText('pet.panel')).toBeNull();
    expect(markRead).not.toHaveBeenCalled();

    await act(async () => grow.reject(new Error('resize failed')));
    await settle();
    expect(screen.queryByLabelText('pet.panel')).toBeNull();
    expect(markRead).not.toHaveBeenCalled();
  });

  it('reads the session on a status event when no read has supplied it yet', async () => {
    sessionReads.S = vi.fn()
      .mockResolvedValueOnce({ status: 500, session: null })
      .mockResolvedValue({ status: 200, session: session('S') });
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByLabelText('pet.panel');
    await settle();
    expect(screen.queryByText('Session S')).toBeNull();

    await emit((h) => h.onSessionStatus?.({ session_id: 'S', agent_status: 'idle' }));
    expect(await screen.findByText('Session S')).toBeTruthy();
  });

  it('reads the tail on a live row when no tail read has landed yet', async () => {
    api.listSessionMessages.mockRejectedValueOnce(new Error('network'));
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByLabelText('pet.panel');
    await settle();
    const reads = api.listSessionMessages.mock.calls.filter(([id]) => id === 'S').length;

    await emit((h) => h.onMessageNew?.(message('live', 'S')));
    await settle();
    expect(api.listSessionMessages.mock.calls.filter(([id]) => id === 'S')).toHaveLength(reads + 1);
  });

  it('re-reads the tail when the session is marked read in another window', async () => {
    devBind('S');
    render(<PetPage />);
    summon('show');
    await screen.findByLabelText('pet.panel');
    await settle();
    const reads = api.listSessionMessages.mock.calls.filter(([id]) => id === 'S').length;

    await emit((h) => h.onInboxUnreadChanged?.({ session_id: 'S', delta: -1, unread_counts: {} }));
    await settle();
    expect(api.listSessionMessages.mock.calls.filter(([id]) => id === 'S')).toHaveLength(reads + 1);
  });
});

describe('PetPage review fixes, round 14', () => {
  it('does not mark read when the inbox count is ahead of the loaded unread results', async () => {
    tails.S = [message('r1', 'S')];
    unreadBySession = { S: 2 };
    api.listSessionMessages.mockImplementationOnce(async (id: string) => ({
      messages: tails[id] ?? [], next_after_id: null, next_before_id: 'older',
    }));
    devBind('S');
    render(<PetPage />);
    summon('show');
    expect(await screen.findByText('pet.moreInAvibe')).toBeTruthy();
    expect(markRead).not.toHaveBeenCalled();
  });

  it('keeps an in-flight send locked across a page remount', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    devBind('S');
    const first = render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'hello{Enter}');
    await waitFor(() => expect(api.sendSessionMessage).toHaveBeenCalledTimes(1));
    first.unmount();
    render(<PetPage />);
    summon('listen');
    const again = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(again, 'again{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(message('sent', 'S', { author: 'user', type: 'user' })));
  });

  it('keeps an uncertain send locked across a page recheck', async () => {
    api.sendSessionMessage.mockRejectedValueOnce(new ApiError('dispatch pending', 504, null));
    devBind('S');
    const first = render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'hello{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    first.unmount();
    render(<PetPage />);
    summon('listen');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    const again = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(again, 'again{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
  });

  it('sends overlapping native resizes in the order they were asked', async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    const order: boolean[] = [];
    const invoke = vi.fn((command: string, args?: { expanded?: boolean }) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') {
        order.push(Boolean(args?.expanded));
        return order.length === 1 ? first.promise : second.promise;
      }
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' })];
    render(<PetPage />);
    summon('show');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    expect(order).toEqual([true]);
    // Collapse while expand is still in flight: Esc is not listening yet
    // (the panel is hidden until the frame grows), so the avatar toggle is
    // the overlapping command. The native collapse waits its turn.
    await userEvent.click(screen.getByLabelText('pet.toggle'));
    expect(order).toEqual([true]);
    await act(async () => first.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
    await waitFor(() => expect(order).toEqual([true, false]));
    await act(async () => second.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
  });

  it('focuses the input only after the native panel has grown and the composer is on screen', async () => {
    const grow = deferred<unknown>();
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') return grow.promise;
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' })];
    render(<PetPage />);
    summon('listen');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();

    await act(async () => grow.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await waitFor(() => expect(document.activeElement).toBe(input));
  });
});

