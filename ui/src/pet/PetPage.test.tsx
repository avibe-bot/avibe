/* @vitest-environment jsdom */

import type { VoiceTranscriptionError as VoiceTranscriptionErrorType } from '@/lib/voiceTranscription';
import { fireEvent, act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  RunningAgentsResult,
  InboxSession,
  VaultRequest,
  WorkbenchEventHandlers,
  WorkbenchMessage,
  WorkbenchSession,
  WorkbenchSessionReadResult,
} from '@/context/ApiContext';

import { ApiError } from '@/context/ApiContext';

import { PET_BOUND_EVENT, PET_HOVER_EVENT, PET_LAYOUT_EVENT, PET_SUMMON_EVENT } from './petBridge';
import { SEND_GRACE_MS } from './usePetSession';

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
let mainId: string | null = 'S';
let inboxSessions: InboxSession[] = [];
let unreadLoaded = true;
let inboxLoading = false;
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
  cancelSession: vi.fn(async (_payload: unknown) => ({ ok: true })),
  setHarnessWatchEnabled: vi.fn(async () => ({ ok: true })),
  setHarnessTaskEnabled: vi.fn(async () => ({ ok: true })),
  getMainSession: vi.fn((): Promise<WorkbenchSessionReadResult> => mainId ? api.getSessionResult(mainId) : Promise.resolve({ status: 403, session: null })),
  getConfig: vi.fn(async () => (setupDone
    ? { mode: 'self_host', setup_state: { needs_setup: false } }
    : { mode: 'self_host', setup_state: { needs_setup: true } })),
  getSessionResult: vi.fn((id: string) => (sessionReads[id]
    ? sessionReads[id]()
    : Promise.resolve({ status: 200, session: session(id), capabilities: { can_chat: true } }))),
  listSessionMessages: vi.fn(async (id: string) => ({ messages: tails[id] ?? [], next_after_id: null, next_before_id: null })),
  getTurnState: vi.fn(async (_id?: string, _options?: unknown) => idleTurn),
  listSessions: vi.fn(async () => ({ sessions: [], next_before_id: null })),
  getVaultRequests: vi.fn(async (_filter?: { session?: string }, _options?: unknown): Promise<{ requests: VaultRequest[] }> => ({ requests: [] })),
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
      return { unreadBySession: { ...unreadBySession }, inboxSessions, unreadLoaded, loading: inboxLoading, markRead };
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

// Configure the authoritative endpoint independently from legacy shell state.
const mainSession = (sessionId: string | null) => { mainId = sessionId; };
const resolveMainAs = (sessionId: string | null) => {
  mainId = sessionId;
  return emit((h) => h.onAuthorizationChanged?.({}));
};
const shellBound = (sessionId: string | null, revision: number) => act(() => {
  window.dispatchEvent(new CustomEvent(PET_BOUND_EVENT, { detail: { session_id: sessionId, revision } }));
});
const summon = (intent: 'listen' | 'show' | 'release') => act(() => {
  window.dispatchEvent(new CustomEvent(PET_SUMMON_EVENT, { detail: { intent } }));
});
// The speech bubble holds the exchange. Tests of the exchange explicitly
// open it; raw summon remains available for collapsed/detail visibility tests.
const showCards = async () => {
  summon('show');
  await act(async () => new Promise((resolve) => setTimeout(resolve, 15)));
};
const showDetail = async () => {
  await showCards();
  const main = document.querySelector('.pet-mini-reply-toggle');
  if (main && !document.querySelector('.pet-mini-detail.is-open')) fireEvent.click(main);
};
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
  tails = Object.fromEntries(['S', 'A', 'B', 'C'].map((id) => [id, [message(`u-${id}`, id, { author: 'user', type: 'user', text: `Session ${id}` })]]));
  mainId = 'S';
  inboxSessions = [];
  unreadLoaded = true;
  inboxLoading = false;
  unreadBySession = {};
  api.getVaultRequests.mockReset().mockResolvedValue({ requests: [] });
  markRead.mockClear();
  inboxVersion = 0;
  api.getSessionResult.mockClear();
  api.listSessionMessages.mockClear();
  api.listSessions.mockClear();
  api.getMainSession.mockReset();
  api.getMainSession.mockImplementation(() => mainId ? api.getSessionResult(mainId) : Promise.resolve({ status: 403, session: null }));
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
    await showDetail();
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


describe('main-only resolution', () => {
  const mainResult = (): WorkbenchSessionReadResult => ({ status: 200, session: session('S'), capabilities: { can_chat: true } });

  it('ignores legacy ready/bound state and never selects another session', async () => {
    const legacy = JSON.stringify({ binding: 'legacy', revision: 9 });
    localStorage.setItem('avibe.pet.devBinding', legacy);
    const invoke = vi.fn(async (command: string) => command === 'pet_ready'
      ? { binding: 'legacy', revision: 9, dismissed_ids: ['unrelated'], summon_pending: { intent: 'listen' } }
      : { panel_side: 'left', panel_edge: 'bottom' });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    await userEvent.type(await screen.findByRole('textbox'), 'main only{Enter}');
    await shellBound('another-session', 10);
    expect(api.sendSessionMessage).toHaveBeenCalledWith('S', { text: 'main only' });
    expect(api.getSessionResult).not.toHaveBeenCalledWith('legacy');
    expect(api.getSessionResult).not.toHaveBeenCalledWith('another-session');
    expect(invoke.mock.calls.some(([command]) => command === 'pet_bind' || command === 'pet_unbind')).toBe(false);
    expect(localStorage.getItem('avibe.pet.devBinding')).toBe(legacy);
    await showDetail();
    expect(screen.queryByText('pet.switchSession')).toBeNull();
    expect(api.listSessions).not.toHaveBeenCalled();
    const { petShell } = await import('./petShell');
    expect(petShell.dismissed()).not.toContain('unrelated');
  });

  it.each([null, 'legacy-session', 'ses_main'])('normalizes persisted binding %s to main while keeping closes document-local', async (persisted) => {
    mainSession('ses_main');
    canUseAgents = true;
    runningAgents = workingIn('T');
    const invoke = vi.fn(async (command: string) => {
      if (command === 'pet_ready') return { binding: persisted, revision: 4, dismissed_ids: [], summon_pending: null };
      if (command === 'pet_bind') return { binding: 'ses_main', revision: 5 };
      return { panel_side: 'left', panel_edge: 'bottom' };
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />); summon('show');
    const close = await screen.findByLabelText('pet.dismiss');
    if (persisted !== 'ses_main') await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_bind', { sessionId: 'ses_main' }));
    await userEvent.click(close);
    const { petShell } = await import('./petShell');
    await waitFor(() => expect(petShell.dismissed()).toEqual(['T']));
    expect(invoke.mock.calls.some(([command]) => command === 'pet_set_dismissed')).toBe(false);
    await emit((h) => h.onConnected?.());
    expect(invoke.mock.calls.filter(([command]) => command === 'pet_bind')).toHaveLength(persisted === 'ses_main' ? 0 : 1);
    expect(invoke.mock.calls.some(([command]) => command === 'pet_unbind')).toBe(false);
  });

  it('does not retry failed main normalization until the authorization epoch changes', async () => {
    mainSession('ses_main');
    const invoke = vi.fn(async (command: string) => {
      if (command === 'pet_ready') return { binding: 'legacy-session', revision: 4, dismissed_ids: [], summon_pending: null };
      if (command === 'pet_bind') throw new Error('disk unavailable');
      return { panel_side: 'left', panel_edge: 'bottom' };
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_bind', { sessionId: 'ses_main' }));
    await emit((h) => h.onConnected?.());
    await emit((h) => h.onConnected?.());
    expect(invoke.mock.calls.filter(([command]) => command === 'pet_bind')).toHaveLength(1);
    await emit((h) => h.onAuthorizationChanged?.({}));
    await waitFor(() => expect(invoke.mock.calls.filter(([command]) => command === 'pet_bind')).toHaveLength(2));
  });

  it.each(['reconnect', 'reactivation'])('recovers a failed main lookup on %s without a polling loop', async (trigger) => {
    api.getMainSession.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(mainResult());
    render(<PetPage />);
    await screen.findByText('pet.vibeyUnavailable');
    expect(api.listSessionMessages).not.toHaveBeenCalled();
    expect(api.getMainSession).toHaveBeenCalledTimes(1);
    if (trigger === 'reconnect') await emit((h) => h.onConnected?.());
    else {
      act(() => { windowFocused = false; window.dispatchEvent(new Event('blur')); });
      act(() => { windowFocused = true; window.dispatchEvent(new Event('focus')); });
    }
    summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'recovered{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledWith('S', { text: 'recovered' });
    expect(api.getMainSession).toHaveBeenCalledTimes(2);
  });

  it.each([
    [403, 'owner_only'], [409, 'main_session_invalid'], [409, 'main_session_archived'], [409, 'builtin_agent_unavailable'],
  ])('keeps main unavailable for %s %s and offers Avibe', async (status) => {
    api.getMainSession.mockResolvedValue({ status: Number(status), session: null });
    render(<PetPage />);
    await screen.findByText('pet.vibeyUnavailable');
    expect(screen.getByRole('button', { name: 'pet.openInAvibe' })).toBeTruthy();
    summon('listen');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(api.listSessionMessages).not.toHaveBeenCalled();
    expect(api.listSessions).not.toHaveBeenCalled();
  });

  it('discards main and transcript reads from the prior authorization before resolving again', async () => {
    const oldMain = deferred<WorkbenchSessionReadResult>();
    const freshMain = deferred<WorkbenchSessionReadResult>();
    api.getMainSession.mockReturnValueOnce(oldMain.promise).mockReturnValueOnce(freshMain.promise);
    render(<PetPage />);
    await waitFor(() => expect(api.getMainSession).toHaveBeenCalledTimes(1));
    await emit((h) => h.onAuthorizationChanged?.({}));
    await waitFor(() => expect(api.getMainSession).toHaveBeenCalledTimes(2));
    await act(async () => oldMain.resolve(mainResult()));
    expect(api.listSessionMessages).not.toHaveBeenCalled();
    await act(async () => freshMain.resolve({ status: 403, session: null }));
    await screen.findByText('pet.vibeyUnavailable');
    expect(api.listSessionMessages).not.toHaveBeenCalled();
  });

  it('keeps live updates working after main is reauthorized with the same id', async () => {
    render(<PetPage />); await showDetail();
    await emit((h) => h.onAuthorizationChanged?.({}));
    await showDetail();
    await emit((h) => h.onMessageNew?.(message('new-authorized', 'S', { text: 'New authorized reply' })));
    expect(await screen.findByText('New authorized reply')).toBeTruthy();
  });

  it('keeps a draft through a transient resolution failure and fences a late transcript after access is lost', async () => {
    render(<PetPage />); summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'keep my words');
    api.getMainSession.mockRejectedValueOnce(new Error('offline'));
    await emit((h) => h.onConnected?.());
    await screen.findByText('pet.vibeyUnavailable');
    await emit((h) => h.onConnected?.());
    summon('listen');
    expect((await screen.findByRole('textbox') as HTMLTextAreaElement).value).toBe('keep my words');
    const oldTail = deferred<{ messages: WorkbenchMessage[]; next_after_id: null; next_before_id: null }>();
    api.listSessionMessages.mockReturnValueOnce(oldTail.promise);
    await emit((h) => h.onConnected?.());
    api.getMainSession.mockResolvedValue({ status: 403, session: null });
    await emit((h) => h.onAuthorizationChanged?.({}));
    await act(async () => oldTail.resolve({ messages: [message('old-secret', 'S', { text: 'private stale text' })], next_after_id: null, next_before_id: null }));
    await screen.findByText('pet.vibeyUnavailable');
    expect(screen.queryByText('private stale text')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});

describe('PetPage Vibey conversation', () => {
  it('shows a turn the agent could not run, with the way to fix it', async () => {
    tails.S = [
      message('u', 'S', { author: 'user', type: 'user', text: 'hello' }),
      message('n', 'S', {
        type: 'notify',
        text: 'No model source yet.',
        metadata: { event: 'backend_failure', failure_id: 'f', turn_id: 't', failure_next_step: 'models' },
      }),
    ];
    mainSession('S');
    render(<PetPage />);
    expect(await screen.findByText('pet.failedPrefix No model source yet.')).toBeTruthy();
    await showDetail();
    expect(await screen.findByText('No model source yet.')).toBeTruthy();
    expect(screen.getByText('pet.fixInAvibe')).toBeTruthy();
  });
});

describe('PetPage state', () => {
  it('offers an open quick-reply group in the panel without asking for attention', async () => {
    const asking = message('q', 'S', { content: { quick_replies: ['Yes', 'No'] } });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), asking];
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    expect(await screen.findByText('Yes')).toBeTruthy();
    expect(pose()).toBe('idle');
  });

  it('asks for attention only for a pending vault request', async () => {
    api.getVaultRequests.mockImplementation(async () => ({ requests: [{ id: 'v1', request_type: 'access' }] } as never));
    try {
      mainSession('S');
      render(<PetPage />);
      await waitFor(() => expect(pose()).toBe('needs_input'));
    } finally {
      api.getVaultRequests.mockImplementation(async () => ({ requests: [] }));
    }
  });

  it('derives Blocked from the first session read, with no provider row', async () => {
    sessionReads.S = async () => ({ status: 200, session: session('S', { agent_status: 'failed' }), capabilities: { can_chat: true } });
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('blocked'));
  });

  it('re-reads turn state on any runs.updated, even for another executor session', async () => {
    mainSession('S');
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
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('ready'));
    await showDetail();
    await screen.findByText('reply r3');
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r3'));
  });

  it('does not mark read when unread rows may reach past the loaded tail', async () => {
    tails.S = [message('r2', 'S', { read_at: null })];
    api.listSessionMessages.mockImplementationOnce(async (id: string) => ({
      messages: tails[id] ?? [], next_after_id: null, next_before_id: 'older',
    }));
    unreadBySession = { S: 5 };
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    expect(await screen.findByText('pet.moreInAvibe')).toBeTruthy();
    expect(markRead).not.toHaveBeenCalled();
  });
});

describe('PetPage other conversations', () => {
  const dataState = () => document.querySelector('[data-state]')?.getAttribute('data-state');

  it('looks busy while another conversation\'s agent works, and the bound session stays idle', async () => {
    canUseAgents = true;
    runningAgents = workingIn('T');
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('team'));
    expect(dataState()).toBe('idle');
    runningAgents = { ...runningAgents, agents: [] };
    await emit((h) => h.onTurnEnd?.({ session_id: 'T' }));
    await waitFor(() => expect(pose()).toBe('idle'));
  });

  it('keeps re-reading a started turn until the backend registers it', async () => {
    canUseAgents = true;
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(api.getRunningAgents).toHaveBeenCalled());
    // turn.start lands before the agent is active in the snapshot.
    await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
    await waitFor(() => expect(api.getRunningAgents.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(pose()).toBe('idle');
    runningAgents = workingIn('T');
    await waitFor(() => expect(pose()).toBe('team'), { timeout: 5000 });
    // Registered: no more retries for it.
    const settled = api.getRunningAgents.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(api.getRunningAgents.mock.calls.length).toBe(settled);
  }, 15000);

  it('keeps re-reading a started turn after a failed read', async () => {
    canUseAgents = true;
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(api.getRunningAgents).toHaveBeenCalled());
    const before = api.getRunningAgents.mock.calls.length;
    // The read turn.start asks for fails, as during a short controller outage.
    api.getRunningAgents.mockRejectedValueOnce(new Error('controller restarting'));
    await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
    await waitFor(() => expect(api.getRunningAgents.mock.calls.length).toBe(before + 1));
    runningAgents = workingIn('T');
    await waitFor(() => expect(pose()).toBe('team'), { timeout: 5000 });
  }, 15000);

  it('re-reads on every event that can change who is working, and catches up on visibility', async () => {
    const { REFRESH_TRIGGERS } = await import('./useConversationAgentWorking');
    canUseAgents = true;
    runningAgents = workingIn('T');
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('team'));
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
    await waitFor(() => expect(pose()).toBe('team'));
  });

  it('never reads other conversations without the agents permission', async () => {
    runningAgents = workingIn('T');
    mainSession('S');
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
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    await screen.findByText('reply r1');
    await waitFor(() => expect(markRead).toHaveBeenCalled());
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(markRead).toHaveBeenCalledTimes(1);
  });

  it('does not put a reply for A into B when the binding changes mid-send', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    tails.B = [message('b1', 'B', { text: 'B only' })];
    mainSession('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'hello{Enter}');
    await resolveMainAs('B');
    summon('listen');
    await screen.findByRole('textbox');
    await waitFor(() => expect(api.listSessionMessages).toHaveBeenCalledWith('B', expect.anything()));
    await act(async () => pending.resolve(message('a-sent', 'A', { author: 'user', type: 'user', text: 'sent to A' })));
    // B neither shows A's row nor enters A's post-send Running grace.
    expect(screen.queryByText('sent to A')).toBeNull();
    expect(screen.queryByText('pet.state.running')).toBeNull();
    expect(pose()).toBe('idle');
  });

  it('acts on a pending summon while ignoring the legacy binding from pet_ready', async () => {
    const ready = deferred<unknown>();
    const invoke = vi.fn((command: string) => (command === 'pet_ready'
      ? ready.promise
      : Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' })));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    // The surface is mounted and subscribed before the shell answers.
    await screen.findByLabelText('pet.recentTurns');
    await act(async () => ready.resolve({ binding: 'legacy', revision: 1, summon_pending: { intent: 'listen' } }));
    // The summon opens the resolved main's input.
    expect(await screen.findByLabelText('pet.inputPlaceholder')).toBeTruthy();
    expect(api.listSessions).not.toHaveBeenCalled();
  });
});

describe('PetPage review fixes, round 2', () => {

  it('retries a failed mark-read the next time the reply is shown', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    markRead.mockImplementationOnce(async (): Promise<boolean> => {
      throw new Error('offline');
    });
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(1));
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await showDetail();
    await waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
    expect(markRead).toHaveBeenLastCalledWith('S', 'r1');
  });

  it('keeps a draft typed after submitting when the send completes', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('S');
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
    mainSession('S');
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
      mainSession('S');
      render(<PetPage />);
      await showDetail();
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

});

describe('PetPage independent sweep', () => {
  it('retries mark-read the server did not apply, without a network error', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    markRead.mockImplementationOnce(async () => false);
    let visibility: DocumentVisibilityState = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
    try {
      mainSession('S');
      render(<PetPage />);
      await showDetail();
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


  it('clears sent text even when the session changed while it was sending', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'deploy{Enter}');
    await resolveMainAs('B');
    summon('listen');
    await act(async () => pending.resolve(message('a-sent', 'A', { author: 'user', type: 'user' })));
    expect((screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement).value).toBe('');
  });

  it('does not mark read through rows trimmed from a long live tail', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r1', 'S', { read_at: null })];
    unreadBySession = { S: 2 };
    mainSession('S');
    render(<PetPage />);
    // The hidden history proves the initial read applied without marking it read.
    await screen.findByText('reply r1');
    // A long turn: many transcript rows arrive live, pushing r1 out of the tail.
    await emit((h) => {
      for (let index = 0; index < 70; index += 1) {
        h.onMessageNew?.(message(`n${index}`, 'S', { author: 'user', type: 'user', text: `note ${index}` }));
      }
      h.onMessageNew?.(message('r2', 'S', { read_at: null, text: 'reply r2' }));
    });
    await showDetail();
    expect(await screen.findByText('pet.moreInAvibe')).toBeTruthy();
    expect(markRead).not.toHaveBeenCalled();
  });
});



describe('PetPage review fixes, round 6', () => {
  it('accepts no input for a binding whose session has not been validated yet', async () => {
    const validation = deferred<WorkbenchSessionReadResult>();
    sessionReads.S = () => validation.promise;
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    await screen.findByLabelText('pet.panel');
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();
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
    mainSession('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'meant for A');
    await resolveMainAs('B');
    summon('listen');
    await screen.findByRole('textbox');
    expect((screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement).value).toBe('');
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
    await screen.findByLabelText('pet.recentTurns');
    // The live summon is newer than the pending one pet_ready still carries.
    await shellBound('S', 1);
    await showDetail();
    // The summon expanded the panel (Vibey's aria-expanded now speaks for the history).
    expect(screen.getByLabelText('pet.panel')).toBeTruthy();
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await act(async () => ready.resolve({ binding: 'S', revision: 1, summon_pending: { intent: 'listen' } }));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    // The stale pending summon did not reopen the panel.
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();
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
    mainSession('S');
    render(<PetPage />);
    await showDetail();
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
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    await userEvent.click(await screen.findByText('Yes'));
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'also this{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(message('sent', 'S', { author: 'user', type: 'user' })));
  });

  it('does not clear a draft typed in another session by an older send', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(input, 'continue{Enter}');
    await resolveMainAs('B');
    summon('listen');
    await screen.findByRole('textbox');
    const inputB = screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await userEvent.type(inputB, 'continue');
    await act(async () => pending.resolve(message('a-sent', 'A', { author: 'user', type: 'user' })));
    expect((screen.getByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement).value).toBe('continue');
  });
});

describe('PetPage review fixes, round 9', () => {
  it('does not turn background definitions into visible team activity', async () => {
    api.getTurnState.mockImplementation(async () => ({
      ...idleTurn,
      background_activities: [
        { id: 'r1', backend: 'codex', runtime_key: 'k', session_id: 'x', kind: 'agent_run', status: 'running', description: null, started_at: '', updated_at: '' },
        { id: 'w1', backend: 'codex', runtime_key: 'k', session_id: 'x', kind: 'watch', status: 'running', description: null, started_at: '', updated_at: '' },
      ],
    }) as never);
    try {
      mainSession('S');
      render(<PetPage />);
      await screen.findByLabelText('pet.recentTurns');
      await act(async () => {});
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
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    await screen.findByText('unseen');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(markRead).not.toHaveBeenCalled();
    windowFocused = true;
    act(() => {
      window.dispatchEvent(new Event('focus'));
    });
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r1'));
  });

});

describe('PetPage review fixes, round 12', () => {
  it('keeps the panel matching the acknowledged shell state when activation fails', async () => {
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded') return Promise.reject(new Error('no monitor'));
      return Promise.resolve(null);
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />);
    await userEvent.click(await screen.findByLabelText('pet.recentTurns'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
    await waitFor(() => expect(screen.queryByLabelText('pet.panel')).toBeNull());
    expect(screen.getByLabelText('pet.recentTurns').getAttribute('aria-expanded')).toBe('false');
  });
});

describe('PetPage review fixes, round 13', () => {
  it('offers no composer or quick replies without the chat capability', async () => {
    canChat = false;
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S', { content: { quick_replies: ['Yes'] } })];
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    await screen.findByLabelText('pet.panel');
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();
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
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    await screen.findByLabelText('pet.panel');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();
    expect(screen.queryByText('Yes')).toBeNull();
    await userEvent.keyboard('should not send{Enter}');
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
  });

  it('ends the post-send Running grace on an authoritative turn.end', async () => {
    mainSession('S');
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
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    await screen.findByText('private reply');
    sessionReads.S = async () => ({ status: 403, session: null });
    await emit((h) => h.onAuthorizationChanged?.({}));
    await waitFor(() => expect(screen.queryByText('private reply')).toBeNull());
    await screen.findByText('pet.vibeyUnavailable');
  });
});

describe('PetPage async closure', () => {

  // Read freshness: what was read under the old authorization goes at once.

  // Unknown outcomes are not treated as answers.
  it('does not mark read from live rows that land before the first tail read', async () => {
    const tail = deferred<{ messages: WorkbenchMessage[]; next_after_id: null; next_before_id: string | null }>();
    api.listSessionMessages.mockImplementationOnce(() => tail.promise);
    unreadBySession = { S: 3 };
    mainSession('S');
    render(<PetPage />);
    await showDetail();
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
    mainSession('S');
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
    summon('listen');
    await userEvent.type(await screen.findByRole('textbox', { name: 'pet.inputPlaceholder' }), '{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(2);
  });

  it('keeps input open when the server refused the send', async () => {
    api.sendSessionMessage.mockRejectedValueOnce(new ApiError('conflict', 409, null));
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await userEvent.type(input, 'again{Enter}');
    await waitFor(() => expect(api.sendSessionMessage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('newSession.sendUncertain')).toBeNull();
    summon('listen');
    await userEvent.type(await screen.findByRole('textbox', { name: 'pet.inputPlaceholder' }), '{Enter}');
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
    await showDetail();
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
    mainSession('A');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await settle();
    await userEvent.type(input, 'to A{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();

    await resolveMainAs('B');
    summon('listen');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('B'));
    const inputB = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await settle();
    expect(screen.queryByText('newSession.sendUncertain')).toBeNull();
    await userEvent.type(inputB, 'to B{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();

    await resolveMainAs('A');
    await settle();
    expect(screen.getByText('newSession.sendUncertain')).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();
    await userEvent.keyboard('again{Enter}');
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

    await showCards();
    await waitFor(() => expect(invoke.mock.calls.filter(([c]) => c === 'pet_set_expanded')).toHaveLength(2));
    await act(async () => second.reject(new Error('already expanded')));
    await settle();
    expect(screen.getByLabelText('pet.panel')).toBeTruthy();
  });

  it('shows the panel and marks replies read only once the shell acknowledges activation', async () => {
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

    await act(async () => grow.reject(new Error('panel activation failed')));
    await settle();
    expect(screen.queryByLabelText('pet.panel')).toBeNull();
    expect(markRead).not.toHaveBeenCalled();
  });

  it('resolves main on reconnect when the initial read failed', async () => {
    sessionReads.S = vi.fn()
      .mockResolvedValueOnce({ status: 500, session: null })
      .mockResolvedValue({ status: 200, session: session('S'), capabilities: { can_chat: true } });
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    await screen.findByLabelText('pet.panel');
    await settle();
    expect(sessionReads.S).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('pet.keyboard.open').hasAttribute('disabled')).toBe(true);

    await emit((h) => h.onConnected?.());
    await waitFor(() => expect(sessionReads.S).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByLabelText('pet.keyboard.open').hasAttribute('disabled')).toBe(false));
    summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'recovered{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', { text: 'recovered' });
  });

  it('reads the tail on a live row when no tail read has landed yet', async () => {
    api.listSessionMessages.mockRejectedValueOnce(new Error('network'));
    mainSession('S');
    render(<PetPage />);
    await showCards();
    await screen.findByLabelText('pet.panel');
    await settle();
    const reads = api.listSessionMessages.mock.calls.filter(([id]) => id === 'S').length;

    await emit((h) => h.onMessageNew?.(message('live', 'S')));
    await settle();
    expect(api.listSessionMessages.mock.calls.filter(([id]) => id === 'S')).toHaveLength(reads + 1);
  });

  it('re-reads the tail when the session is marked read in another window', async () => {
    mainSession('S');
    render(<PetPage />);
    await showCards();
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
    mainSession('S');
    render(<PetPage />);
    await showDetail();
    expect(await screen.findByText('pet.moreInAvibe')).toBeTruthy();
    expect(markRead).not.toHaveBeenCalled();
  });

  it('keeps an in-flight send locked across a page remount', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('S');
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
    mainSession('S');
    const first = render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await userEvent.type(input, 'hello{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    first.unmount();
    render(<PetPage />);
    summon('listen');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();
    await userEvent.keyboard('again{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
  });

  it('sends overlapping panel-state commands in the order they were asked', async () => {
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
    // A second panel command while the expand is still in flight: a Vibey
    // click, which opens the history (the panel is not confirmed open, so
    // nothing is open to close). It waits its turn behind the first.
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    expect(order).toEqual([true]);
    await act(async () => first.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
    await waitFor(() => expect(order).toEqual([true, true]));
    await act(async () => second.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
  });

  it('focuses the input after panel activation and the composer is on screen', async () => {
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
    expect(screen.queryByRole('textbox', { name: 'pet.inputPlaceholder' })).toBeNull();

    await act(async () => grow.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await waitFor(() => expect(document.activeElement).toBe(input));
  });
});



describe('desktop mini v2 interaction contract', () => {
  // Visible process rows replace background definitions; stopping keeps v2 timing.
  const activity = (id: string, item_kind: 'watch' | 'task' | 'agent_run', status = 'running') => ({
    id, item_kind, status, label: id, description: id, backend: 'codex',
    runtime_key: id, session_id: 'S', kind: item_kind, started_at: '', updated_at: '',
  });
  const turn = (items: ReturnType<typeof activity>[]) => {
    canUseAgents = true;
    api.getTurnState.mockImplementation(async (id: string) => ({ ...idleTurn, in_flight: items.some((item) => item.id === id && item.status === 'running') }));
    runningAgents = {
      ok: true,
      agents: items.map((item) => ({
        ...workingIn(item.id).agents[0], backend: 'codex', title: item.label,
        state: item.status === 'running' ? 'active' : 'idle',
        trigger_source: item.item_kind === 'agent_run' ? 'agent' : 'human',
      })),
      counts: { total: items.length, active: items.length, idle: 0, orphan: 0, by_backend: {} },
    };
  };
  beforeEach(() => {
    api.cancelSession.mockReset().mockResolvedValue({ ok: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    api.getTurnState.mockResolvedValue(idleTurn);
  });

  it.each([
    ['watch', 'success'], ['watch', 'failure'], ['task', 'success'], ['task', 'failure'],
  ] as const)('drops a retained %s on authorization change and fences late pause %s', async (kind, outcome) => {
    const id = `${kind}:private`;
    turn([activity(id, kind)]);
    const pending = deferred<{ ok: boolean }>();
    const pause = api.cancelSession;
    pause.mockImplementationOnce(() => pending.promise);
    mainSession('S'); render(<PetPage />); await showCards();
    const stop = await screen.findByLabelText('pet.stop.stop');
    vi.useFakeTimers();
    fireEvent.click(stop); fireEvent.click(stop);
    expect(screen.getByText('pet.stop.stopping')).toBeTruthy();
    turn([]);
    await emit((h) => h.onAuthorizationChanged?.({}));
    expect(screen.queryByText(id)).toBeNull();
    expect(screen.queryByText('pet.stop.stopping')).toBeNull();
    // An authorized new version with the same id must not inherit old phases.
    turn([{ ...activity(id, kind), label: 'Allowed replacement' }]);
    await emit((h) => h.onRunsUpdated?.({}));
    expect(screen.getByText('Allowed replacement')).toBeTruthy();
    const freshStop = screen.getByLabelText('pet.stop.stop');
    expect(freshStop.hasAttribute('disabled')).toBe(false);
    fireEvent.click(freshStop);
    expect(freshStop.classList.contains('is-confirm')).toBe(true);
    await act(async () => {
      if (outcome === 'success') pending.resolve({ ok: true });
      else pending.reject(new Error('old pause failed'));
    });
    expect(freshStop.classList.contains('is-confirm')).toBe(true);
    expect(screen.queryByText('pet.stop.stopped')).toBeNull();
    expect(screen.queryByText('pet.stop.failed')).toBeNull();
    await act(async () => vi.advanceTimersByTime(4000));
    expect(screen.getByText('Allowed replacement')).toBeTruthy();
    expect(document.querySelector('.is-leaving')).toBeNull();
    expect(pause).toHaveBeenCalledTimes(1);
  });

  it('drops an exiting card on authorization change and ignores its already-queued removal', async () => {
    const id = 'watch:private';
    turn([activity(id, 'watch')]);
    mainSession('S'); render(<PetPage />); await showCards();
    vi.useFakeTimers();
    const timeouts = vi.spyOn(window, 'setTimeout');
    fireEvent.click(screen.getByLabelText('pet.dismiss'));
    const remove = timeouts.mock.calls.find(([, delay]) => delay === 500)?.[0];
    timeouts.mockRestore();
    expect(typeof remove).toBe('function');
    expect(document.querySelector('.is-leaving')).toBeTruthy();
    turn([]);
    await emit((h) => h.onAuthorizationChanged?.({}));
    expect(screen.queryByText(id)).toBeNull();
    expect(document.querySelector('.is-leaving')).toBeNull();
    turn([{ ...activity(id, 'watch'), label: 'New authorized card' }]);
    await emit((h) => h.onRunsUpdated?.({}));
    await act(async () => { (remove as () => void)(); });
    await act(async () => vi.advanceTimersByTime(500));
    expect(screen.getByText('New authorized card')).toBeTruthy();
    const { petShell } = await import('./petShell');
    expect(petShell.dismissed()).not.toContain(id);
  });

  it('fences a pause after switching away and back to the same binding', async () => {
    const id = 'watch:private';
    turn([activity(id, 'watch')]);
    const pending = deferred<{ ok: boolean }>();
    api.cancelSession.mockImplementationOnce(() => pending.promise);
    mainSession('S'); render(<PetPage />); await showCards();
    const stop = await screen.findByLabelText('pet.stop.stop');
    fireEvent.click(stop); fireEvent.click(stop);
    turn([]);
    await resolveMainAs('B');
    await emit((h) => h.onRunsUpdated?.({}));
    expect(screen.queryByText(id)).toBeNull();
    turn([{ ...activity(id, 'watch'), label: 'Fresh S card' }]);
    await emit((h) => h.onRunsUpdated?.({}));
    await resolveMainAs('S');
    await showCards();
    expect(await screen.findByText('Fresh S card')).toBeTruthy();
    await act(async () => pending.resolve({ ok: true }));
    expect(screen.queryByText('pet.stop.stopped')).toBeNull();
    expect(screen.getByLabelText('pet.stop.stop').hasAttribute('disabled')).toBe(false);
  });

  it.each(['success', 'uncertain'])('preserves user text while fencing a late send %s after authorization changes', async (outcome) => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('S'); render(<PetPage />); summon('listen');
    const input = await screen.findByRole('textbox');
    await userEvent.type(input, 'my own words{Enter}');
    await emit((h) => h.onAuthorizationChanged?.({}));
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('my own words');
    await userEvent.keyboard('{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    await act(async () => {
      if (outcome === 'success') pending.resolve(message('late-private', 'S', { text: 'Old authorized response' }));
      else pending.reject(new Error('unknown admission'));
    });
    const { petComposer } = await import('./petComposer');
    expect(petComposer.isSending()).toBe(false);
    expect(petComposer.isUncertain('S')).toBe(outcome === 'uncertain');
    expect(screen.queryByText('Old authorized response')).toBeNull();
    // The async completion cannot clear the user's draft or open old detail.
    if (outcome === 'success') expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('my own words');
    else expect(petComposer.isUncertain('S')).toBe(true);
    expect(document.querySelector('.pet-mini-detail.is-open')).toBeNull();
  });

  it('keeps the document send ledger locked when an old page fails after remount', async () => {
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('S');
    const first = render(<PetPage />);
    summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'preserve this{Enter}');
    first.unmount();
    render(<PetPage />); summon('listen');
    const input = await screen.findByRole('textbox');
    expect((input as HTMLTextAreaElement).value).toBe('preserve this');
    await userEvent.keyboard('{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
    await act(async () => pending.reject(new Error('unknown outcome')));
    const { petComposer } = await import('./petComposer');
    expect(petComposer.isUncertain('S')).toBe(true);
    expect(petComposer.isSending()).toBe(false);
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(petComposer.isUncertain('S')).toBe(true);
    await userEvent.keyboard('{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
  });


  // A tapped (not held) hotkey opens the text input once voice status is known.
  const tapHotkey = () => { summon('listen'); summon('release'); };

  async function withVoiceEnv(asrAvailable: () => boolean, dictation: unknown, body: () => Promise<void>) {
    vi.doMock('@/lib/showPageVoiceDictation', () => ({ ShowPageVoiceDictation: dictation }));
    const locks = Object.getOwnPropertyDescriptor(navigator, 'locks');
    const media = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices');
    try {
      Object.defineProperty(navigator, 'locks', { configurable: true, value: {} });
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn() } });
      vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ available: asrAvailable() }) })));
      vi.resetModules();
      ({ PetPage } = await import('./PetPage'));
      await body();
    } finally {
      vi.doUnmock('@/lib/showPageVoiceDictation');
      vi.unstubAllGlobals();
      if (locks) Object.defineProperty(navigator, 'locks', locks);
      else Reflect.deleteProperty(navigator, 'locks');
      if (media) Object.defineProperty(navigator, 'mediaDevices', media);
      else Reflect.deleteProperty(navigator, 'mediaDevices');
    }
  }

  it.each([
    ['microphone start', 'pet.voice.startFailed', () => ({ start: Promise.reject(new DOMException('denied', 'NotAllowedError')), done: new Promise<string>(() => {}) })],
    ['transcription', 'pet.voice.failed', (TranscriptionError: typeof VoiceTranscriptionErrorType) => ({ start: Promise.resolve(), done: Promise.reject(new TranscriptionError('failed')) })],
  ])('falls back to text with a note when %s fails, keeping the typed draft', async (_label, note, outcome) => {
    // Resolved after the module reset so PetPage and the test share one class.
    let TranscriptionError!: typeof VoiceTranscriptionErrorType;
    class Dictation {
      private readonly outcome = outcome(TranscriptionError);
      done = this.outcome.done;
      start = () => this.outcome.start;
      finish = () => {};
      abort = () => {};
    }
    await withVoiceEnv(() => true, Dictation, async () => {
      ({ VoiceTranscriptionError: TranscriptionError } = await import('@/lib/voiceTranscription'));
      mainSession('S'); render(<PetPage />); tapHotkey();
      const input = await screen.findByRole('textbox');
      await userEvent.type(input, 'typed before voice');
      await userEvent.click(await screen.findByLabelText('pet.voice.open'));
      expect(await screen.findByText(note)).toBeTruthy();
      expect(pose()).not.toBe('listening');
      expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('typed before voice');
      expect(api.sendSessionMessage).not.toHaveBeenCalled();
    });
  });


  it('clears a previous ASR-available cache when the authorization epoch changes', async () => {
    const status = deferred<{ ok: boolean; json: () => Promise<unknown> }>();
    let asrCalls = 0;
    class Dictation { done = new Promise<string>(() => {}); start = async () => {}; finish = () => {}; abort = () => {}; }
    await withVoiceEnv(() => true, Dictation, async () => {
      vi.stubGlobal('fetch', vi.fn(() => {
        asrCalls += 1;
        return asrCalls === 1 ? Promise.resolve({ ok: true, json: async () => ({ available: true }) }) : status.promise;
      }));
      mainSession('S'); render(<PetPage />); tapHotkey();
      expect(await screen.findByLabelText('pet.voice.open')).toBeTruthy();
      await emit((h) => h.onAuthorizationChanged?.({}));
      await waitFor(() => expect(screen.queryByLabelText('pet.voice.open')).toBeNull());
      await act(async () => status.resolve({ ok: true, json: async () => ({ available: false }) }));
      await waitFor(() => expect(screen.queryByLabelText('pet.voice.open')).toBeNull());
      expect(pose()).not.toBe('listening');
    });
  });

  it('opens text while the first ASR status read is still pending, and does not start voice when it later succeeds', async () => {
    const status = deferred<{ ok: boolean; json: () => Promise<unknown> }>();
    const start = vi.fn(async () => {});
    class Dictation { done = new Promise<string>(() => {}); start = start; finish = () => {}; abort = () => {}; }
    await withVoiceEnv(() => true, Dictation, async () => {
      vi.stubGlobal('fetch', vi.fn(() => status.promise));
      mainSession('S'); render(<PetPage />); summon('listen');
      expect(await screen.findByRole('textbox')).toBeTruthy();
      expect(pose()).not.toBe('listening');
      await act(async () => status.resolve({ ok: true, json: async () => ({ available: true }) }));
      await waitFor(() => expect(screen.getByLabelText('pet.voice.open')).toBeTruthy());
      expect(pose()).not.toBe('listening');
      expect(start).not.toHaveBeenCalled();
    });
  });

  it('opens text from an avatar hold while ASR status is pending, and does not start voice when it later succeeds', async () => {
    const status = deferred<{ ok: boolean; json: () => Promise<unknown> }>();
    const start = vi.fn(async () => {});
    class Dictation { done = new Promise<string>(() => {}); start = start; finish = () => {}; abort = () => {}; }
    await withVoiceEnv(() => true, Dictation, async () => {
      vi.stubGlobal('fetch', vi.fn(() => status.promise));
      mainSession('S'); render(<PetPage />); await screen.findByText('Session S');
      const avatar = screen.getByLabelText('pet.recentTurns');
      vi.useFakeTimers();
      try {
        fireEvent.pointerDown(avatar, { button: 0, pointerId: 1 });
        await act(async () => vi.advanceTimersByTime(400));
      } finally {
        vi.useRealTimers();
      }
      expect(await screen.findByRole('textbox')).toBeTruthy();
      expect(pose()).not.toBe('listening');
      await act(async () => status.resolve({ ok: true, json: async () => ({ available: true }) }));
      await waitFor(() => expect(screen.getByLabelText('pet.voice.open')).toBeTruthy());
      expect(pose()).not.toBe('listening');
      expect(start).not.toHaveBeenCalled();
    });
  });

  it('discards voice preview and final text from the previous authorization epoch', async () => {
    const done = deferred<string>();
    let preview!: (text: string) => void;
    const abort = vi.fn();
    vi.doMock('@/lib/showPageVoiceDictation', () => ({
      ShowPageVoiceDictation: class {
        done = done.promise;
        constructor(options: { onPreview: (text: string) => void }) { preview = options.onPreview; }
        start = async () => {};
        finish = () => {};
        abort = abort;
      },
    }));
    const locks = Object.getOwnPropertyDescriptor(navigator, 'locks');
    const media = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices');
    try {
      Object.defineProperty(navigator, 'locks', { configurable: true, value: {} });
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn() } });
      vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ available: true }) })));
      vi.resetModules();
      ({ PetPage } = await import('./PetPage'));
      mainSession('S'); render(<PetPage />); tapHotkey();
      const input = await screen.findByRole('textbox');
      await userEvent.type(input, 'typed before voice');
      await userEvent.click(await screen.findByLabelText('pet.voice.open'));
      expect(pose()).toBe('listening');
      await emit((h) => h.onAuthorizationChanged?.({}));
      expect(abort).toHaveBeenCalled();
      await act(async () => { preview('stale voice preview'); done.resolve('stale voice transcript'); });
      expect(screen.queryByText('stale voice preview')).toBeNull();
      expect(pose()).not.toBe('listening');
      expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('typed before voice');
      expect(api.sendSessionMessage).not.toHaveBeenCalled();
    } finally {
      vi.doUnmock('@/lib/showPageVoiceDictation');
      vi.unstubAllGlobals();
      if (locks) Object.defineProperty(navigator, 'locks', locks);
      else Reflect.deleteProperty(navigator, 'locks');
      if (media) Object.defineProperty(navigator, 'mediaDevices', media);
      else Reflect.deleteProperty(navigator, 'mediaDevices');
    }
  });

  it('leaves listening as soon as stop is pressed, before the transcript arrives', async () => {
    const done = deferred<string>();
    const finish = vi.fn();
    let preview!: (text: string) => void;
    class Dictation {
      done = done.promise;
      constructor(options: { onPreview: (text: string) => void }) { preview = options.onPreview; }
      start = async () => {};
      finish = finish;
      abort = () => {};
    }
    await withVoiceEnv(() => true, Dictation, async () => {
      mainSession('S'); render(<PetPage />); tapHotkey();
      await userEvent.click(await screen.findByLabelText('pet.voice.open'));
      expect(pose()).toBe('listening');
      await act(async () => { preview('live words'); });
      expect(screen.getByText('live words')).toBeTruthy();
      await userEvent.click(screen.getByLabelText('pet.voice.stop'));
      expect(finish).toHaveBeenCalled();
      expect(pose()).not.toBe('listening');
      expect(screen.queryByLabelText('pet.voice.stop')).toBeNull();
      expect(screen.getByLabelText('pet.voice.open').hasAttribute('disabled')).toBe(true);
      expect(screen.getByText('pet.voice.finishing')).toBeTruthy();
      expect(screen.queryByText('live words')).toBeNull();
      expect(screen.queryByRole('textbox')).toBeNull();
      await act(async () => done.resolve('cleaned words'));
      expect(await screen.findByRole('textbox')).toBeTruthy();
      expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('cleaned words');
      expect(screen.queryByText('pet.voice.finishing')).toBeNull();
    });
  });

  it('cancels an in-flight finish so Escape and an epoch change do not stick on Finishing', async () => {
    const done = deferred<string>();
    const abort = vi.fn();
    class Dictation {
      done = done.promise;
      start = async () => {};
      finish = () => {};
      abort = abort;
    }
    await withVoiceEnv(() => true, Dictation, async () => {
      mainSession('S'); render(<PetPage />); tapHotkey();
      await userEvent.click(await screen.findByLabelText('pet.voice.open'));
      expect(pose()).toBe('listening');
      await userEvent.click(screen.getByLabelText('pet.voice.stop'));
      expect(screen.getByText('pet.voice.finishing')).toBeTruthy();
      await userEvent.keyboard('{Escape}');
      expect(abort).toHaveBeenCalled();
      expect(screen.queryByText('pet.voice.finishing')).toBeNull();
      expect(await screen.findByRole('textbox')).toBeTruthy();
      await act(async () => done.resolve('late cleaned words'));
      expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('');
    });
  });

  it('does not let a queued old animation report replace the new epochs hit regions', async () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => frames.push(callback)));
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    const { petBridge } = await import('./petBridge');
    const report = vi.spyOn(petBridge, 'setHitRegions').mockResolvedValue(undefined);
    try {
      mainSession('S'); render(<PetPage />);
      await screen.findByText('Session S');
      const oldFrames = [...frames];
      expect(oldFrames.length).toBeGreaterThan(0);
      await emit((h) => h.onAuthorizationChanged?.({}));
      expect(report).toHaveBeenCalledWith([]);
      const currentFrame = frames.at(-1)!;
      expect(oldFrames).not.toContain(currentFrame);
      act(() => currentFrame(performance.now()));
      report.mockClear();
      // Simulate callbacks already queued before cancellation. Neither an old
      // report nor its empty cleanup may overwrite the new surface's regions.
      act(() => oldFrames.forEach((callback) => callback(performance.now())));
      expect(report).not.toHaveBeenCalled();
    } finally {
      cleanup();
      report.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it('requires a fresh intersection after authorization and ignores old read completions', async () => {
    const intersections: ((entries: { isIntersecting: boolean }[]) => void)[] = [];
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: typeof intersections[number]) { intersections.push(callback); }
      observe() {}
      disconnect() {}
    });
    const pending = deferred<boolean>();
    markRead.mockImplementationOnce(() => pending.promise);
    try {
      tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('result', 'S', { read_at: null })];
      unreadBySession = { S: 1 };
      mainSession('S'); render(<PetPage />); await showDetail();
      const oldIntersection = intersections.at(-1)!;
      await act(async () => oldIntersection([{ isIntersecting: true }]));
      expect(markRead).toHaveBeenCalledTimes(1);
      await emit((h) => h.onAuthorizationChanged?.({}));
      await showDetail();
      const freshIntersection = intersections.at(-1)!;
      expect(freshIntersection).not.toBe(oldIntersection);
      const reads = api.listSessionMessages.mock.calls.length;
      await act(async () => {
        oldIntersection([{ isIntersecting: true }]);
        pending.resolve(true);
      });
      expect(markRead).toHaveBeenCalledTimes(1);
      expect(api.listSessionMessages.mock.calls).toHaveLength(reads);
      await act(async () => freshIntersection([{ isIntersecting: true }]));
      expect(markRead).toHaveBeenCalledTimes(2);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('fences a late quick reply across authorization without clearing the typed draft', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S', { content: { quick_replies: ['Yes'] } })];
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('S'); render(<PetPage />); await showDetail();
    await userEvent.click(screen.getByText('Yes'));
    summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'my draft');
    await emit((h) => h.onAuthorizationChanged?.({}));
    await userEvent.keyboard('{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', { text: 'Yes', metadata: { quick_reply_for: 'q' } });
    await act(async () => pending.resolve(message('late-choice', 'S', { author: 'user', type: 'user', text: 'Yes' })));
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('my draft');
    expect(document.querySelector('.pet-mini-title')?.textContent).not.toBe('Yes');
    expect(document.querySelector('.pet-mini-detail.is-open')).toBeNull();
  });

  it.each(['update-first', 'ack-first'])('keeps quick-reply ownership when message.updated and the send response arrive %s', async (order) => {
    const asking = message('q1', 'S', { content: { quick_replies: ['Yes', 'No'] } });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), asking];
    const pending = deferred<WorkbenchMessage>();
    api.sendSessionMessage.mockImplementationOnce(() => pending.promise);
    mainSession('S'); render(<PetPage />); await showDetail();
    await userEvent.click(await screen.findByText('Yes'));
    summon('listen');
    const input = await screen.findByRole('textbox');
    const chosen = { ...asking, content: { ...asking.content, quick_reply_chosen: 'Yes' } };
    const update = () => {
      tails.S = tails.S.map((row) => row.id === chosen.id ? chosen : row);
      return emit((h) => h.onMessageUpdated?.(chosen));
    };
    const acknowledge = () => act(async () => {
      const sent = message('sent', 'S', { author: 'user', type: 'user', text: 'Yes' });
      // The post-send tail read must reflect the acknowledged durable write.
      tails.S = [...tails.S, sent];
      pending.resolve(sent);
    });
    if (order === 'update-first') {
      await update();
      await userEvent.type(input, 'duplicate{Enter}');
      expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
      await acknowledge();
    } else {
      await userEvent.type(input, 'duplicate{Enter}');
      expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
      await acknowledge();
      await update();
    }
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', { text: 'Yes', metadata: { quick_reply_for: 'q1' } });
    expect(screen.queryByRole('button', { name: 'Yes', exact: true })).toBeNull();
    expect(screen.queryByRole('button', { name: 'No', exact: true })).toBeNull();
    // The history stayed open above the input; Escape closes it, then the input.
    await userEvent.keyboard('{Escape}');
    expect(screen.getByRole('textbox')).toBeTruthy();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('textbox')).toBeNull();
    await showDetail();
    // An older update must never reclaim a newer result's quick-reply group.
    await emit((h) => h.onMessageNew?.(message('q2', 'S', { content: { quick_replies: ['Continue', 'Wait'] } })));
    expect(await screen.findByRole('button', { name: 'Continue', exact: true })).toBeTruthy();
    await update();
    expect(screen.getByRole('button', { name: 'Continue', exact: true }).hasAttribute('disabled')).toBe(false);
    expect(api.sendSessionMessage).toHaveBeenCalledTimes(1);
  });

  it('keeps quick replies behind the same uncertain-send lock as typed input', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q1', 'S', { content: { quick_replies: ['Yes', 'No'] } })];
    api.sendSessionMessage.mockRejectedValueOnce(new Error('connection lost'));
    mainSession('S'); render(<PetPage />); await showDetail();
    await userEvent.click(await screen.findByText('Yes'));
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    summon('listen');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Yes', exact: true })).toBeNull();
    // A newer result cannot bypass a send whose admission is still unknown.
    await emit((h) => h.onMessageNew?.(message('q2', 'S', { content: { quick_replies: ['Continue', 'Wait'] } })));
    expect(screen.queryByRole('button', { name: 'Continue', exact: true })).toBeNull();
    await userEvent.keyboard('duplicate{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', { text: 'Yes', metadata: { quick_reply_for: 'q1' } });
  });

  it('does not label idle processes as working or completed, and hides unsupported run stops', async () => {
    turn([activity('watch:w1', 'watch', 'idle'), activity('task:t1', 'task', 'idle')]);
    mainSession('S'); render(<PetPage />); await showDetail();
    expect(document.querySelectorAll('.pet-mini-card')).toHaveLength(0);
    expect(pose()).toBe('idle');
    turn([activity('run:r1', 'agent_run')]);
    await emit((h) => h.onRunsUpdated?.({}));
    await waitFor(() => expect(pose()).toBe('team'));
    expect(screen.queryByLabelText('pet.stop.stop')).toBeNull();
  });





  it('shows the dock while the shell reports hover over Vibey, and reports that zone', async () => {
    mainSession('S'); render(<PetPage />);
    const wrap = (await screen.findByLabelText('pet.recentTurns')).closest('.pet-mini-avatar-wrap')!;
    expect(wrap.hasAttribute('data-pet-hover')).toBe(true);
    expect(wrap.classList.contains('is-hovered')).toBe(false);
    act(() => { window.dispatchEvent(new CustomEvent(PET_HOVER_EVENT, { detail: { hover: true } })); });
    expect(wrap.classList.contains('is-hovered')).toBe(true);
    act(() => { window.dispatchEvent(new CustomEvent(PET_HOVER_EVENT, { detail: { hover: false } })); });
    expect(wrap.classList.contains('is-hovered')).toBe(false);
  });

  it('keeps the unread count visible until the inbox confirms the result was read', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('r', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    mainSession('S'); render(<PetPage />);
    expect((await screen.findByLabelText('pet.unreadBadge')).textContent).toBe('1');
    summon('show');
    expect(screen.getByLabelText('pet.unreadBadge').textContent).toBe('1');
    expect(markRead).not.toHaveBeenCalled();
    await showDetail();
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r'));
    act(() => {
      unreadBySession = {};
      inboxVersion += 1;
      inboxListeners.forEach((listener) => listener());
    });
    await waitFor(() => expect(screen.queryByLabelText('pet.unreadBadge')).toBeNull());
  });

  it('does not mark the collapsed card read; detail is explicitly opened and closes on collapse', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user', text: 'Question' }), message('r', 'S', { read_at: null })];
    unreadBySession = { S: 1 };
    mainSession('S');
    render(<PetPage />);
    await screen.findByText('Question');
    expect(markRead).not.toHaveBeenCalled();
    summon('show');
    await waitFor(() => expect(document.querySelector('.pet-mini-detail')).toBeTruthy());
    expect(markRead).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector('.pet-mini-reply-toggle')!);
    await waitFor(() => expect(markRead).toHaveBeenCalledWith('S', 'r'));
    fireEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).toBeNull());
  });

  it('marks detail read only after its result intersects the fixed scroll viewport', async () => {
    let intersect: ((entries: { isIntersecting: boolean }[]) => void) | undefined;
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: typeof intersect) { intersect = callback; }
      observe() {}
      disconnect() {}
    });
    try {
      tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('unseen', 'S', { read_at: null })];
      unreadBySession = { S: 1 };
      mainSession('S'); render(<PetPage />); await showDetail();
      expect(intersect).toBeDefined();
      expect(markRead).not.toHaveBeenCalled();
      await act(async () => intersect!([{ isIntersecting: true }]));
      expect(markRead).toHaveBeenCalledWith('S', 'unseen');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('requires a second press, uses the native watch id, and retains the stopped row through linger and both exit beats', async () => {
    turn([activity('watch:w1', 'watch'), activity('run:r1', 'agent_run')]);
    mainSession('S'); render(<PetPage />); await showCards();
    const stop = await screen.findByLabelText('pet.stop.stop');
    expect(screen.getAllByLabelText('pet.stop.stop')).toHaveLength(1);
    const pending = deferred<{ ok: boolean }>();
    api.cancelSession.mockImplementationOnce(() => pending.promise);
    vi.useFakeTimers();
    fireEvent.click(stop);
    expect(api.cancelSession).not.toHaveBeenCalled();
    fireEvent.click(stop);
    expect(api.cancelSession).toHaveBeenCalledExactlyOnceWith('watch:w1');
    expect(stop.hasAttribute('disabled')).toBe(true);
    await act(async () => pending.resolve({ ok: true }));
    expect(screen.getByText('pet.stop.stopped')).toBeTruthy();
    turn([]);
    // Server removal during the linger cannot cut off the local exit.
    await act(async () => { handlers.forEach((h) => h.onRunsUpdated?.({})); });
    expect(screen.getByText('watch:w1')).toBeTruthy();
    await act(async () => vi.advanceTimersByTime(2499));
    expect(document.querySelector('.is-leaving')).toBeNull();
    await act(async () => vi.advanceTimersByTime(1));
    expect(document.querySelector('.is-leaving')).toBeTruthy();
    await act(async () => vi.advanceTimersByTime(499));
    expect(screen.getByText('watch:w1')).toBeTruthy();
    await act(async () => vi.advanceTimersByTime(1));
    expect(screen.queryByText('watch:w1')).toBeNull();
  });

  it('expires confirmation at 3s and lets a failed pause be retried', async () => {
    turn([activity('task:t1', 'task')]); mainSession('S'); render(<PetPage />); await showCards();
    const stop = await screen.findByLabelText('pet.stop.stop');
    vi.useFakeTimers(); fireEvent.click(stop);
    await act(async () => vi.advanceTimersByTime(3000));
    expect(stop.classList.contains('is-confirm')).toBe(false);
    api.cancelSession.mockRejectedValueOnce(new Error('offline'));
    fireEvent.click(stop); fireEvent.click(stop);
    await act(async () => {});
    expect(screen.getByText('pet.stop.failed')).toBeTruthy();
    fireEvent.click(stop); fireEvent.click(stop);
    await act(async () => {});
    expect(api.cancelSession).toHaveBeenNthCalledWith(2, 'task:t1');
    expect(screen.getByText('pet.stop.stopped')).toBeTruthy();
  });

  it('holds for exactly 320ms, cancels a departed press, and falls back to text without voice support', async () => {
    mainSession('S'); render(<PetPage />); await screen.findByText('Session S');
    const avatar = screen.getByLabelText('pet.recentTurns');
    vi.useFakeTimers();
    fireEvent.pointerDown(avatar, { button: 0, pointerId: 1 });
    fireEvent.pointerLeave(avatar);
    await act(async () => vi.advanceTimersByTime(400));
    expect(screen.queryByRole('textbox')).toBeNull();
    fireEvent.pointerDown(avatar, { button: 0, pointerId: 2 });
    await act(async () => vi.advanceTimersByTime(319));
    expect(screen.queryByRole('textbox')).toBeNull();
    await act(async () => vi.advanceTimersByTime(1));
    expect(screen.getByRole('textbox')).toBeTruthy();
    expect(screen.queryByLabelText('pet.voice.open')).toBeNull();
    fireEvent.pointerUp(avatar, { pointerId: 2 });
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
  });

  // Spec §5.1/§5.2: Vibey's history and the input are open together.
  const historyIsOpen = () => document.querySelector('.pet-mini-detail.is-open') !== null;
  const withHistory = () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user', text: 'hello' }), message('r', 'S', { text: 'Old reply' })];
  };

  it('opens the history above the typed input, keeping its draft, focus and caret, and Escape closes the history first', async () => {
    withHistory();
    mainSession('S'); render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Old reply'));
    await userEvent.click(screen.getByLabelText('pet.keyboard.open'));
    const input = await screen.findByRole('textbox') as HTMLTextAreaElement;
    await userEvent.type(input, 'half a thought');
    input.setSelectionRange(4, 4);
    const avatar = screen.getByLabelText('pet.recentTurns');
    await userEvent.click(avatar);
    await waitFor(() => expect(historyIsOpen()).toBe(true));
    expect(avatar.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('textbox')).toBe(input);
    expect(input.value).toBe('half a thought');
    expect(document.activeElement).toBe(input);
    expect([input.selectionStart, input.selectionEnd]).toEqual([4, 4]);
    // Drawn in the same slot, above the input.
    const bubble = document.querySelector('.pet-mini-reply-bubble')!;
    expect(bubble.closest('.pet-mini-slot')).toBe(input.closest('.pet-mini-slot'));
    expect(bubble.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // A second click closes only the history.
    await userEvent.click(avatar);
    await waitFor(() => expect(historyIsOpen()).toBe(false));
    expect(document.querySelector('.pet-mini-reply-bubble')).toBeNull();
    expect(screen.getByRole('textbox')).toBe(input);
    expect(input.value).toBe('half a thought');
    // Escape: the history, then the input.
    await userEvent.click(avatar);
    await waitFor(() => expect(historyIsOpen()).toBe(true));
    await userEvent.keyboard('{Escape}');
    expect(historyIsOpen()).toBe(false);
    expect(screen.getByRole('textbox')).toBe(input);
    expect(input.value).toBe('half a thought');
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
  });

  it('keeps an open history when the input opens, and sends from the input below it', async () => {
    withHistory();
    mainSession('S'); render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Old reply'));
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(historyIsOpen()).toBe(true));
    await userEvent.click(screen.getByLabelText('pet.keyboard.open'));
    const input = await screen.findByRole('textbox');
    expect(historyIsOpen()).toBe(true);
    await userEvent.type(input, 'next{Enter}');
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', { text: 'next' });
  });

  it('opens the history above dictation without ending it, and Escape closes the history before cancelling', async () => {
    const abort = vi.fn();
    const finish = vi.fn();
    class Dictation { done = new Promise<string>(() => {}); start = async () => {}; finish = finish; abort = abort; }
    await withVoiceEnv(() => true, Dictation, async () => {
      withHistory();
      mainSession('S'); render(<PetPage />);
      await userEvent.click(await screen.findByLabelText('pet.voice.open'));
      await waitFor(() => expect(pose()).toBe('listening'));
      expect(document.querySelector('.pet-mini-marquee')).not.toBeNull();
      await userEvent.click(screen.getByLabelText('pet.recentTurns'));
      await waitFor(() => expect(historyIsOpen()).toBe(true));
      expect(pose()).toBe('listening');
      expect(document.querySelector('.pet-mini-marquee')).not.toBeNull();
      const bubble = document.querySelector('.pet-mini-reply-bubble')!;
      expect(bubble.compareDocumentPosition(document.querySelector('.pet-mini-marquee')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
      expect(historyIsOpen()).toBe(false);
      expect(pose()).toBe('listening');
      expect(abort).not.toHaveBeenCalled();
      expect(finish).not.toHaveBeenCalled();
      act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
      expect(abort).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(pose()).not.toBe('listening'));
    });
  });

  it('a double-click opens the conversation in Avibe and leaves the history as before', async () => {
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_open') return Promise.resolve(null);
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    try {
      withHistory();
      mainSession('S'); render(<PetPage />);
      await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Old reply'));
      const avatar = screen.getByLabelText('pet.recentTurns');
      const opens = () => invoke.mock.calls.filter(([command]) => command === 'pet_open');
      // Closed before: closed after, with the conversation opened once.
      await userEvent.setup().dblClick(avatar);
      await waitFor(() => expect(opens()).toEqual([['pet_open', { link: 'avibe://session/S' }]]));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 15)));
      expect(historyIsOpen()).toBe(false);
      expect(avatar.getAttribute('aria-expanded')).toBe('false');
      // Open before: open after.
      await userEvent.click(avatar);
      await waitFor(() => expect(historyIsOpen()).toBe(true));
      await userEvent.setup().dblClick(avatar);
      await waitFor(() => expect(opens()).toHaveLength(2));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 15)));
      expect(historyIsOpen()).toBe(true);
      expect(avatar.getAttribute('aria-expanded')).toBe('true');
      // Keyboard activation (no click count) and single clicks never open it.
      fireEvent.click(avatar);
      await waitFor(() => expect(historyIsOpen()).toBe(false));
      await userEvent.click(avatar);
      await waitFor(() => expect(historyIsOpen()).toBe(true));
      expect(opens()).toHaveLength(2);
    } finally {
      Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
      Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
    }
  });

  it('a quick click after a hold is a single click, not a double-click', async () => {
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      return Promise.resolve(command === 'pet_open' ? null : { panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    withHistory();
    mainSession('S'); render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Old reply'));
    const avatar = screen.getByLabelText('pet.recentTurns');
    // A click, then a hold (which opens the input without voice), then a
    // press the platform counts as the second of a double-click.
    await userEvent.click(avatar);
    await waitFor(() => expect(historyIsOpen()).toBe(true));
    fireEvent.pointerDown(avatar, { button: 0, pointerId: 7 });
    fireEvent.mouseDown(avatar, { detail: 2 });
    await act(async () => new Promise((resolve) => setTimeout(resolve, 400)));
    fireEvent.pointerUp(avatar, { pointerId: 7 });
    expect(await screen.findByRole('textbox')).toBeTruthy();
    fireEvent.pointerDown(avatar, { button: 0, pointerId: 8 });
    fireEvent.mouseDown(avatar, { detail: 2 });
    fireEvent.pointerUp(avatar, { pointerId: 8 });
    await waitFor(() => expect(historyIsOpen()).toBe(false));
    expect(invoke.mock.calls.filter(([command]) => command === 'pet_open')).toEqual([]);
  });

  it('a double-click that first collapsed the card list puts the list back', async () => {
    canUseAgents = true;
    const row = workingIn('running').agents[0];
    runningAgents = { ...workingIn('running'), agents: [
      { ...row, session_id: 'A', title: 'Task A', visibility: 'foreground', openable_in_chat: true },
      { ...row, session_id: 'B', title: 'Task B', visibility: 'foreground', openable_in_chat: true },
    ] };
    withHistory();
    mainSession('S'); render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-collapsed')).not.toBeNull());
    await showCards();
    await waitFor(() => expect(document.querySelector('.pet-mini-scroll')).not.toBeNull());
    await userEvent.setup().dblClick(screen.getByLabelText('pet.recentTurns'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 15)));
    expect(document.querySelector('.pet-mini-scroll')).not.toBeNull();
    expect(historyIsOpen()).toBe(false);
  });
});


describe('Personal Agent mini', () => {
  it('uses the same visible sessions for cards and attention, excluding the main and unknown visibility', async () => {
    canUseAgents = true;
    const row = workingIn('running').agents[0];
    runningAgents = { ...workingIn('running'), agents: [
      { ...row, session_id: 'S' },
      { ...row, session_id: 'ses_main' },
      { ...row, session_id: 'hidden', visibility: 'background' },
      { ...row, session_id: 'system', visibility: 'system' },
      { ...row, session_id: 'unknown', visibility: null },
      { ...row, session_id: 'closed', openable_in_chat: false },
      { ...row, session_id: 'reply', state: 'idle' },
      { ...row, session_id: 'running', state: 'idle' },
      row,
      { ...row, session_id: 'approval', state: 'idle' },
    ] };
    unreadBySession = { reply: 1 };
    api.getVaultRequests.mockImplementation(async (filter) => ({ requests: filter?.session === 'approval' ? [{ id: 'request', request_type: 'access', session: { id: 'approval' } } as VaultRequest] : [] }));
    mainSession('S'); render(<PetPage />); await showCards();
    await waitFor(() => expect(pose()).toBe('needs_input'));
    expect(Array.from(document.querySelectorAll('.pet-mini-card'), (node) => node.getAttribute('data-session-id'))).toEqual(['approval', 'running', 'reply']);
    expect(document.querySelector('.pet-mini-card.is-main')).toBeNull();
    expect(api.getVaultRequests).not.toHaveBeenCalledWith(expect.objectContaining({ session: 'hidden' }), expect.anything());
    api.getVaultRequests.mockResolvedValue({ requests: [] });
    await emit((h) => h.onVaultsUpdated?.({}));
    await waitFor(() => expect(pose()).toBe('ready'));
    expect(Array.from(document.querySelectorAll('.pet-mini-card'), (node) => node.getAttribute('data-session-id'))).toEqual(['running', 'reply']);
  });

  it('keeps unread Inbox replies after their process ends and rechecks visibility', async () => {
    canUseAgents = true;
    inboxSessions = [{ session_id: 'reply', unread: true, project_name: 'Project' } as InboxSession];
    unreadBySession = { reply: 1 };
    mainSession('S'); render(<PetPage />); await showDetail();
    await waitFor(() => expect(document.querySelector('[data-session-id="reply"]')).not.toBeNull());
    expect(screen.getByText('pet.replied')).toBeTruthy();
    expect(pose()).toBe('ready');
    sessionReads.reply = async () => ({ status: 200, session: session('reply', { visibility: 'background' }) });
    await emit((h) => h.onSessionActivity?.({ session_id: 'reply', scope_id: null, event: 'visibility', visibility: 'background' }));
    await waitFor(() => expect(document.querySelector('[data-session-id="reply"]')).toBeNull());
    expect(pose()).toBe('idle');
  });

  it('appends a removable reference to the draft and sends it only to the main binding', async () => {
    canUseAgents = true;
    runningAgents = workingIn('other');
    api.sendSessionMessage.mockResolvedValueOnce(message('sent-ref', 'S', { author: 'user', type: 'user' }));
    mainSession('S'); render(<PetPage />); summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'Check this');
    await userEvent.click(await screen.findByRole('button', { name: 'pet.referenceSession' }));
    expect(screen.getByRole('textbox').getAttribute('placeholder')).toBe('pet.inputPlaceholder');
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'pet.send' }));
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', {
      text: 'Check this #<other>', content: { references: [{ kind: 'session', session_id: 'other', title: 'pet.untitledTask' }] },
    });
  });
});


describe('Personal Agent review invariants', () => {
  it.each(['ended', 'hidden', 'unreachable', 'remount'])('keeps selected reference metadata when the activity is %s', async (reason) => {
    canUseAgents = true;
    runningAgents = workingIn('other');
    api.sendSessionMessage.mockResolvedValueOnce(message('sent-ref', 'S', { author: 'user', type: 'user' }));
    mainSession('S'); const view = render(<PetPage />); summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'Check this');
    await userEvent.click(await screen.findByRole('button', { name: 'pet.referenceSession' }));
    runningAgents = reason === 'hidden'
      ? { ...workingIn('other'), agents: workingIn('other').agents.map((row) => ({ ...row, visibility: 'background' })) }
      : { ok: false, unreachable: true, agents: [], counts: {} };
    if (reason === 'ended' || reason === 'remount') runningAgents = { ...workingIn('other'), agents: [] };
    await emit((h) => h.onRunsUpdated?.({}));
    if (reason === 'remount') {
      view.unmount(); render(<PetPage />); summon('listen');
    }
    const input = await screen.findByRole('textbox');
    expect((input as HTMLTextAreaElement).value).toBe('Check this ');
    expect(screen.getByRole('button', { name: 'pet.removeReference' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'pet.send' }));
    expect(api.sendSessionMessage).toHaveBeenCalledExactlyOnceWith('S', {
      text: 'Check this #<other>', content: { references: [{ kind: 'session', session_id: 'other', title: 'pet.untitledTask' }] },
    });
    summon('listen');
    await screen.findByRole('textbox');
    expect(screen.queryByRole('button', { name: 'pet.removeReference' })).toBeNull();
  });

  it.each(['remove', 'escape', 'binding'])('clears draft references on %s', async (action) => {
    canUseAgents = true; runningAgents = workingIn('other');
    mainSession('S'); render(<PetPage />); summon('show');
    await userEvent.click(await screen.findByRole('button', { name: 'pet.referenceSession' }));
    if (action === 'remove') await userEvent.click(screen.getByRole('button', { name: 'pet.removeReference' }));
    else if (action === 'escape') await userEvent.keyboard('{Escape}');
    else await resolveMainAs('B');
    summon('listen');
    expect((await screen.findByRole('textbox') as HTMLTextAreaElement).value).toBe('');
    expect(screen.queryByRole('button', { name: 'pet.removeReference' })).toBeNull();
  });

  it('sleeps despite unread sessions excluded from the visible activity model', async () => {
    vi.useFakeTimers();
    try {
      canUseAgents = true;
      const row = workingIn('hidden').agents[0];
      runningAgents = { ...workingIn('hidden'), agents: [
        { ...row, session_id: 'hidden', visibility: 'background' },
        { ...row, session_id: 'system', visibility: 'system' },
        { ...row, session_id: 'unknown', visibility: null },
        { ...row, session_id: 'closed', openable_in_chat: false },
      ] };
      unreadBySession = { hidden: 1, system: 1, unknown: 1, closed: 1 };
      mainSession('S'); render(<PetPage />);
      await act(async () => {});
      expect(pose()).toBe('idle');
      await act(async () => vi.advanceTimersByTimeAsync(5 * 60 * 1000 + 1));
      expect(pose()).toBe('sleeping');
      runningAgents = workingIn('visible');
      await emit((h) => h.onRunsUpdated?.({}));
      expect(pose()).toBe('team');
    } finally { vi.useRealTimers(); }
  });

  it('reads each activity action source once on mount and when IDs change with a healthy bridge', async () => {
    canUseAgents = true; runningAgents = workingIn('other');
    api.getTurnState.mockClear();
    mainSession('S'); render(<PetPage />); await showDetail();
    await waitFor(() => expect(api.getTurnState).toHaveBeenCalledWith('other', expect.anything()));
    expect(api.getVaultRequests.mock.calls.filter(([filter]) => filter?.session === 'other')).toHaveLength(1);
    expect(api.getTurnState.mock.calls.filter(([id]) => id === 'other')).toHaveLength(1);
    await emit((h) => h.onEventBridgeStatus?.({ connected: true }));
    runningAgents = { ...workingIn('other'), agents: [...workingIn('other').agents, ...workingIn('new').agents] };
    await emit((h) => h.onRunsUpdated?.({}));
    await waitFor(() => expect(api.getTurnState).toHaveBeenCalledWith('new', expect.anything()));
    expect(api.getVaultRequests.mock.calls.filter(([filter]) => filter?.session === 'other')).toHaveLength(2);
    expect(api.getVaultRequests.mock.calls.filter(([filter]) => filter?.session === 'new')).toHaveLength(1);
    expect(api.getTurnState.mock.calls.filter(([id]) => id === 'new')).toHaveLength(1);
  });
});

describe('Personal Agent review: history and lifecycle', () => {
  it.each(['disappeared', 'new-turn', 'new-turn-during-outage'])('forgets a manual dismissal when %s', async (reason) => {
    mainSession('ses_main'); canUseAgents = true; runningAgents = workingIn('T');
    const invoke = vi.fn(async (command: string) => command === 'pet_ready'
      ? { binding: 'ses_main', revision: 1, dismissed_ids: [], summon_pending: null }
      : { panel_side: 'left', panel_edge: 'bottom' });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />); summon('show');
    await userEvent.click(await screen.findByLabelText('pet.dismiss'));
    const { petShell } = await import('./petShell');
    await waitFor(() => expect(petShell.dismissed()).toEqual(['T']));
    if (reason === 'disappeared') {
      runningAgents = { ...runningAgents, agents: [] };
      await emit((h) => h.onRunsUpdated?.({}));
    } else {
      if (reason === 'new-turn-during-outage') {
        api.getMainSession.mockRejectedValueOnce(new Error('offline'));
        await emit((h) => h.onConnected?.());
        await screen.findByText('pet.vibeyUnavailable');
      }
      await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
    }
    await waitFor(() => expect(petShell.dismissed()).toEqual([]));
    expect(invoke.mock.calls.some(([command]) => command === 'pet_set_dismissed')).toBe(false);
    runningAgents = workingIn('T');
    await emit((h) => h.onConnected?.());
    await waitFor(() => expect(document.querySelector('[data-session-id="T"]')).toBeTruthy());
  });

  it('cancels an old manual exit when the next turn starts before its animation ends', async () => {
    canUseAgents = true; runningAgents = workingIn('T');
    render(<PetPage />); summon('show');
    const close = await screen.findByLabelText('pet.dismiss');
    vi.useFakeTimers();
    try {
      fireEvent.click(close);
      expect(document.querySelector('.is-leaving')).toBeTruthy();
      await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
      await act(async () => vi.advanceTimersByTime(500));
      const { petShell } = await import('./petShell');
      expect(petShell.dismissed()).toEqual([]);
      expect(document.querySelector('[data-session-id="T"]')).toBeTruthy();
      expect(document.querySelector('.is-leaving')).toBeNull();
    } finally { vi.useRealTimers(); }
  });

  it('draws a promoted result after the input it answered and before a later input answered by silence', async () => {
    // Scenario: MESSAGE-DELIVERY-325.
    tails.S = [
      message('ask', 'S', { author: 'user', type: 'user', text: 'Check CI' }),
      message('report', 'S', { text: 'CI is green', read_at: null, metadata: { promoted_from: 'output' } }),
      message('steer', 'S', { author: 'user', type: 'user', text: 'Also the docs' }),
    ];
    render(<PetPage />); await showDetail();
    const text = document.querySelector('.pet-mini-detail')?.textContent ?? '';
    expect(text.indexOf('Check CI')).toBeGreaterThanOrEqual(0);
    expect(text.indexOf('Check CI')).toBeLessThan(text.indexOf('CI is green'));
    expect(text.indexOf('CI is green')).toBeLessThan(text.indexOf('Also the docs'));
  });

  it('renders reference sidecars in current and historical Markdown', async () => {
    const reference = (id: string, title: string) => ({ references: [{ kind: 'session', session_id: id, title }] });
    tails.S = [
      message('old-user', 'S', { author: 'user', type: 'user', text: 'Read #<older>', content: reference('older', '旧项目') }),
      message('old-result', 'S', { text: 'Done #<old-result-ref>', content: reference('old-result-ref', 'Old result') }),
      message('current-user', 'S', { author: 'user', type: 'user', text: 'Summarize #<current>', content: reference('current', '当前项目') }),
      message('current-result', 'S', { text: 'See #<result-ref>', content: reference('result-ref', 'Latest result') }),
    ];
    render(<PetPage />); await showDetail();
    const detail = within(document.querySelector('.pet-mini-detail') as HTMLElement);
    for (const title of ['#旧项目', '#Old result', '#当前项目', '#Latest result']) expect(detail.getByText(title)).toBeTruthy();
    expect(document.querySelector('.pet-mini-detail')?.textContent).not.toMatch(/#</);
  });

  it.each([false, true])('preserves document dismissals across an outage (native bind succeeds: %s)', async (persistable) => {
    mainSession('ses_main'); canUseAgents = true; runningAgents = workingIn('T');
    const invoke = vi.fn(async (command: string) => {
      if (command === 'pet_ready') return { binding: null, revision: 0, dismissed_ids: [], summon_pending: null };
      if (command === 'pet_bind') {
        if (!persistable) throw new Error('storage unavailable');
        return { binding: 'ses_main', revision: 1 };
      }
      return { panel_side: 'left', panel_edge: 'bottom' };
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    render(<PetPage />); summon('show');
    await userEvent.click(await screen.findByLabelText('pet.dismiss'));
    const { petShell } = await import('./petShell');
    await waitFor(() => expect(petShell.dismissed()).toEqual(['T']));
    api.getMainSession.mockRejectedValueOnce(new Error('offline'));
    await emit((h) => h.onConnected?.());
    await screen.findByText('pet.vibeyUnavailable');
    expect(petShell.dismissed()).toEqual(['T']);
    await emit((h) => h.onConnected?.());
    await waitFor(() => expect(petShell.currentBinding()).toBe('ses_main'));
    expect(petShell.dismissed()).toEqual(['T']);
    expect(document.querySelector('[data-session-id="T"]')).toBeNull();
  });

  it('shows loading during slow reauthorization while preserving the draft', async () => {
    render(<PetPage />); summon('listen');
    await userEvent.type(await screen.findByRole('textbox'), 'unsent draft');
    const pending = deferred<WorkbenchSessionReadResult>();
    api.getMainSession.mockReturnValueOnce(pending.promise);
    await emit((h) => h.onAuthorizationChanged?.({}));
    expect(screen.getByRole('status').textContent).toContain('common.loading');
    expect(screen.getByRole('button', { name: 'pet.openInAvibe' })).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    await act(async () => pending.resolve({ status: 200, session: session('S'), capabilities: { can_chat: true } }));
    expect((await screen.findByRole('textbox') as HTMLTextAreaElement).value).toBe('unsent draft');
    expect(screen.queryByText('common.loading')).toBeNull();
  });

  it.each(['idle-then-running', 'new-turn', 'unread'])('does not turn a stop exit into a manual dismissal before %s', async (next) => {
    canUseAgents = true; runningAgents = workingIn('T');
    api.getTurnState.mockImplementation(async (id) => ({ ...idleTurn, in_flight: id === 'T' }));
    api.cancelSession.mockResolvedValue({ ok: true });
    render(<PetPage />); summon('show');
    const stop = await screen.findByLabelText('pet.stop.stop');
    vi.useFakeTimers();
    try {
      fireEvent.click(stop); fireEvent.click(stop);
      await act(async () => {});
      await act(async () => vi.advanceTimersByTime(2500));
      await act(async () => vi.advanceTimersByTime(500));
      expect(document.querySelector('[data-session-id="T"]')).toBeNull();
      const { petShell } = await import('./petShell');
      expect(petShell.dismissed()).not.toContain('T');
      if (next === 'idle-then-running') {
        runningAgents = { ...runningAgents, agents: runningAgents.agents.map((row) => ({ ...row, state: 'idle' })) };
        await emit((h) => h.onRunsUpdated?.({}));
        runningAgents = workingIn('T');
        await emit((h) => h.onRunsUpdated?.({}));
      } else if (next === 'new-turn') {
        await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
      } else {
        runningAgents = { ...runningAgents, agents: runningAgents.agents.map((row) => ({ ...row, state: 'idle' })) };
        act(() => {
          unreadBySession = { T: 1 }; inboxVersion += 1; inboxListeners.forEach((listener) => listener());
        });
        await emit((h) => h.onRunsUpdated?.({}));
      }
      expect(document.querySelector('[data-session-id="T"]')).toBeTruthy();
      expect(petShell.dismissed()).not.toContain('T');
    } finally { vi.useRealTimers(); }
  });
});

describe('Personal Agent breaker: readiness and turn generations', () => {
  it('keeps closes only in this document and ignores legacy dismissals even after slow ready', async () => {
    mainSession('ses_main'); canUseAgents = true; runningAgents = workingIn('T');
    const ready = deferred<unknown>();
    const legacy = { binding: 'ses_main', revision: 5, dismissed_ids: ['T'], summon_pending: null };
    const invoke = vi.fn((command: string) => command === 'pet_ready' ? ready.promise : Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    const view = render(<PetPage />); await showCards();
    await userEvent.click(await screen.findByLabelText('pet.dismiss'));
    const { petShell } = await import('./petShell');
    await waitFor(() => expect(petShell.dismissed()).toEqual(['T']));
    await act(async () => ready.resolve(legacy));
    expect(petShell.dismissed()).toEqual(['T']);
    expect(document.querySelector('[data-session-id="T"]')).toBeNull();
    expect(invoke.mock.calls.some(([command]) => command === 'pet_set_dismissed')).toBe(false);
    view.unmount();
    // A new module graph represents reopening the document, with the same file.
    vi.resetModules();
    const { PetPage: ReopenedPet } = await import('./PetPage');
    render(<ReopenedPet />); await showCards();
    await waitFor(() => expect(document.querySelector('[data-session-id="T"]')).toBeTruthy());
    expect(invoke.mock.calls.some(([command]) => command === 'pet_set_dismissed')).toBe(false);
  });

  it.each(['unread', 'feed', 'running'])('does not forget a close while the %s source is unready', async (source) => {
    mainSession('ses_main'); canUseAgents = true;
    unreadLoaded = source !== 'unread'; inboxLoading = source === 'feed';
    if (source === 'running') runningAgents = { ok: false, agents: [], counts: {} };
    const { petShell } = await import('./petShell');
    petShell.addDismissed('reply');
    render(<PetPage />); await showCards();
    await waitFor(() => expect(petShell.currentBinding()).toBe('ses_main'));
    await waitFor(() => expect(api.getRunningAgents).toHaveBeenCalled());
    expect(petShell.dismissed()).toEqual(['reply']);
    act(() => {
      inboxSessions = [{ session_id: 'reply', unread: true } as InboxSession];
      unreadBySession = { reply: 1 }; unreadLoaded = true; inboxLoading = false;
      inboxVersion += 1; inboxListeners.forEach((listener) => listener());
    });
    runningAgents = { ok: true, agents: [], counts: {} };
    await emit((h) => h.onRunsUpdated?.({}));
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('reply'));
    expect(petShell.dismissed()).toEqual(['reply']);
    expect(document.querySelector('[data-session-id="reply"]')).toBeNull();
    // A loaded empty snapshot can now forget it.
    act(() => {
      inboxSessions = []; unreadBySession = {};
      inboxVersion += 1; inboxListeners.forEach((listener) => listener());
    });
    await waitFor(() => expect(petShell.dismissed()).toEqual([]));
  });

  it.each([false, true])('retains reply cards and their generation during revalidation (dismissed: %s)', async (dismissed) => {
    mainSession('ses_main'); canUseAgents = true;
    inboxSessions = [{ session_id: 'reply', unread: true } as InboxSession];
    unreadBySession = { reply: 1 };
    render(<PetPage />); await showCards();
    await waitFor(() => expect(document.querySelector('[data-session-id="reply"]')).toBeTruthy());
    const { petShell } = await import('./petShell');
    if (dismissed) {
      await userEvent.click(await screen.findByLabelText('pet.dismiss'));
      await waitFor(() => expect(petShell.dismissed()).toEqual(['reply']));
    }
    const first = deferred<WorkbenchSessionReadResult>();
    const latest = deferred<WorkbenchSessionReadResult>();
    sessionReads.reply = () => first.promise;
    await emit((h) => h.onConnected?.());
    const assertStable = () => {
      expect(petShell.dismissed()).toEqual(dismissed ? ['reply'] : []);
      expect(Boolean(document.querySelector('[data-session-id="reply"]'))).toBe(!dismissed);
      if (!dismissed) expect(pose()).toBe('ready');
    };
    assertStable();
    sessionReads.reply = () => latest.promise;
    await emit((h) => h.onSessionActivity?.({ session_id: 'unrelated', scope_id: null, event: 'updated' }));
    assertStable();
    await act(async () => latest.resolve({ status: 200, session: session('reply') }));
    assertStable();
    // The older read must not remove the newer validated row.
    await act(async () => first.resolve({ status: 404, session: null }));
    assertStable();
    sessionReads.reply = () => Promise.reject(new Error('offline'));
    await emit((h) => h.onConnected?.());
    assertStable();
  });

  it('records a new close against the retained main during a lookup outage', async () => {
    mainSession('ses_main'); canUseAgents = true; runningAgents = workingIn('T');
    render(<PetPage />); await showCards();
    await screen.findByLabelText('pet.dismiss');
    api.getMainSession.mockRejectedValueOnce(new Error('offline'));
    await emit((h) => h.onConnected?.());
    await screen.findByText('pet.vibeyUnavailable');
    await userEvent.click(await screen.findByLabelText('pet.dismiss'));
    const { petShell } = await import('./petShell');
    await waitFor(() => expect(petShell.dismissed()).toEqual(['T']));
    expect(document.querySelector('[data-session-id="T"]')).toBeNull();
    await emit((h) => h.onConnected?.());
    await waitFor(() => expect(petShell.currentBinding()).toBe('ses_main'));
    expect(petShell.dismissed()).toEqual(['T']);
    expect(document.querySelector('[data-session-id="T"]')).toBeNull();
  });

  it('requires two fresh stop presses after confirmation, manual exit and a new turn', async () => {
    canUseAgents = true; runningAgents = workingIn('T');
    api.getTurnState.mockImplementation(async (id) => ({ ...idleTurn, in_flight: id === 'T' }));
    api.cancelSession.mockReset().mockResolvedValue({ ok: true });
    render(<PetPage />); await showCards();
    const stop = await screen.findByLabelText('pet.stop.stop');
    vi.useFakeTimers();
    try {
      fireEvent.click(stop);
      fireEvent.click(screen.getByLabelText('pet.dismiss'));
      await act(async () => vi.advanceTimersByTime(500));
      expect(document.querySelector('[data-session-id="T"]')).toBeNull();
      await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
      const nextStop = screen.getByLabelText('pet.stop.stop');
      fireEvent.click(nextStop);
      expect(nextStop.getAttribute('aria-label')).toBe('pet.stop.confirmStop');
      expect(api.cancelSession).not.toHaveBeenCalled();
      // The old confirmation timer must not clear the new turn's confirmation.
      await act(async () => vi.advanceTimersByTime(2500));
      expect(nextStop.getAttribute('aria-label')).toBe('pet.stop.confirmStop');
      fireEvent.click(nextStop);
      expect(api.cancelSession).toHaveBeenCalledExactlyOnceWith('T');
      await act(async () => {});
    } finally { vi.useRealTimers(); }
  });

  it.each([
    ['event', 'success'], ['event', 'failure'], ['reappearance', 'success'], ['reappearance', 'failure'],
  ] as const)('fences a pending cancel across %s before late %s', async (boundary, outcome) => {
    canUseAgents = true; runningAgents = workingIn('T');
    api.getTurnState.mockImplementation(async (id) => ({ ...idleTurn, in_flight: id === 'T' }));
    const pending = deferred<{ ok: boolean }>();
    api.cancelSession.mockReset().mockImplementationOnce(() => pending.promise);
    render(<PetPage />); await showCards();
    const stop = await screen.findByLabelText('pet.stop.stop');
    fireEvent.click(stop); fireEvent.click(stop);
    expect(screen.getByText('pet.stop.stopping')).toBeTruthy();
    if (boundary === 'event') await emit((h) => h.onTurnStart?.({ session_id: 'T' }));
    else {
      runningAgents = { ...runningAgents, agents: [] };
      await emit((h) => h.onRunsUpdated?.({}));
      runningAgents = workingIn('T');
      await emit((h) => h.onRunsUpdated?.({}));
    }
    const nextStop = await screen.findByLabelText('pet.stop.stop');
    expect(nextStop.hasAttribute('disabled')).toBe(false);
    fireEvent.click(nextStop);
    await act(async () => {
      if (outcome === 'success') pending.resolve({ ok: true });
      else pending.reject(new Error('old cancellation failed'));
    });
    expect(nextStop.getAttribute('aria-label')).toBe('pet.stop.confirmStop');
    expect(screen.queryByText('pet.stop.stopped')).toBeNull();
    expect(screen.queryByText('pet.stop.failed')).toBeNull();
    expect(document.querySelector('.is-leaving')).toBeNull();
    expect(api.cancelSession).toHaveBeenCalledTimes(1);
  });

  it('offers stop for a scope-less Web turn but excludes IM and non-running turns', async () => {
    canUseAgents = true;
    runningAgents = { ...workingIn('web'), agents: [
      { ...workingIn('web').agents[0], platform: null, scope_type: null },
      { ...workingIn('idleweb').agents[0], platform: null, scope_type: null },
      { ...workingIn('im').agents[0], platform: 'feishu' },
    ] };
    api.getTurnState.mockReset().mockImplementation(async (id) => ({ ...idleTurn, in_flight: id === 'web' || id === 'im' }));
    render(<PetPage />); await showCards();
    await waitFor(() => expect(document.querySelector('[data-session-id="web"] .pet-mini-stop')).toBeTruthy());
    expect(document.querySelector('[data-session-id="idleweb"] .pet-mini-stop')).toBeNull();
    expect(document.querySelector('[data-session-id="im"] .pet-mini-stop')).toBeNull();
    expect(api.getTurnState).not.toHaveBeenCalledWith('im', expect.anything());
  });
});

describe('PetPage IME composition', () => {
  // WebKit (the macOS pet webview) fires compositionend before the Enter that
  // commits the candidate, so that keydown reads isComposing false with the
  // legacy keyCode 229.
  it('keeps the draft through IME Enters and sends once on a plain Enter', async () => {
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'hello' } });
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: false });
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13, isComposing: true });
    await act(async () => new Promise((resolve) => setTimeout(resolve, 15)));
    expect(api.sendSessionMessage).not.toHaveBeenCalled();
    expect(input.value).toBe('hello');
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 13 });
    await waitFor(() => expect(api.sendSessionMessage).toHaveBeenCalledTimes(1));
    expect(api.sendSessionMessage).toHaveBeenCalledWith('S', { text: 'hello' });
  });

  it('leaves the composer and its draft open when Escape dismisses an IME candidate', async () => {
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: 'hello' } });
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 229 }));
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', isComposing: true }));
    });
    expect(screen.getByLabelText('pet.inputPlaceholder')).toBe(input);
    expect(input.value).toBe('hello');
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    await waitFor(() => expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull());
  });
});

describe('Vibey bubble visibility (spec §4.2)', () => {
  // Vibey's own bubble (its toggle row), not the loading notice that shares the slot.
  const bubbleOpen = () => document.querySelector('.pet-mini-reply-toggle')?.closest('.pet-mini-slot')?.classList.contains('is-open') ?? false;

  it('starts hidden for an old, finished exchange, quick replies and unread included', async () => {
    tails.S = [
      message('u', 'S', { author: 'user', type: 'user', text: 'hello' }),
      message('r', 'S', { text: 'Done already', read_at: null, content: { quick_replies: ['Yes', 'No'] } }),
    ];
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Done already'));
    expect(bubbleOpen()).toBe(false);
  });

  // A turn that is running, then ends with `text` as its result; returns the
  // slot's class at every commit after the result lands.
  const runToResult = async (text = 'All done') => {
    // Fake time that still moves on its own, so waitFor keeps polling while the
    // linger's own timer is under the test's control from the start.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'], shouldAdvanceTime: true });
    tails.S = [message('u', 'S', { author: 'user', type: 'user', text: 'hello' })];
    let running = true;
    api.getTurnState.mockImplementation(async () => (running ? { ...idleTurn, in_flight: true, foreground: 'running' as const } : idleTurn));
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    const slot = document.querySelector('.pet-mini-reply-toggle')!.closest('.pet-mini-slot')!;
    const classes: string[] = [];
    const observer = new MutationObserver(() => classes.push(slot.className));
    observer.observe(slot, { attributes: true, attributeFilter: ['class'] });
    const result = message('r', 'S', { text, read_at: null });
    tails.S = [...tails.S, result];
    running = false;
    await emit((h) => {
      h.onMessageNew?.(result);
      h.onTurnEnd?.({ session_id: 'S' });
    });
    await waitFor(() => expect(api.getTurnState).toHaveBeenCalled());
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    observer.disconnect();
    return classes;
  };
  const bubbleWrap = () => document.querySelector('.pet-mini-reply-bubble')!.parentElement!;
  afterEach(() => {
    vi.useRealTimers();
    api.getTurnState.mockImplementation(async () => idleTurn);
  });

  // Spec §4.2: a status word alone sizes the bubble to its content.
  it('is content-sized while thinking, full width for the reply and whenever the history is open', async () => {
    const compact = () => document.querySelector('.pet-mini-reply-bubble')!.classList.contains('is-compact');
    tails.S = [message('u', 'S', { author: 'user', type: 'user', text: 'hello' })];
    let running = true;
    api.getTurnState.mockImplementation(async () => (running ? { ...idleTurn, in_flight: true, foreground: 'running' as const } : idleTurn));
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    expect(document.querySelector('.pet-mini-reply-toggle')?.textContent).toContain('pet.thinking');
    expect(compact()).toBe(true);
    fireEvent.click(document.querySelector('.pet-mini-reply-toggle')!);
    await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).not.toBeNull());
    expect(compact()).toBe(false);
    fireEvent.click(document.querySelector('.pet-mini-reply-toggle')!);
    await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).toBeNull());
    expect(compact()).toBe(true);
    const result = message('r', 'S', { text: 'All done', read_at: null });
    tails.S = [...tails.S, result];
    running = false;
    await emit((h) => {
      h.onMessageNew?.(result);
      h.onTurnEnd?.({ session_id: 'S' });
    });
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-toggle')?.textContent).toContain('pet.repliedPrefix'));
    expect(compact()).toBe(false);
    expect(bubbleOpen()).toBe(true);
  });

  it('keeps the bubble open from running to its result, then shows the reply for 10s', async () => {
    const classes = await runToResult();
    // No closed frame between the running bubble and the reply.
    expect(classes.every((value) => value.includes('is-open'))).toBe(true);
    expect(bubbleOpen()).toBe(true);
    expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('All done');
    act(() => { vi.advanceTimersByTime(9_900); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(200); });
    expect(bubbleOpen()).toBe(false);
  });

  it('spends nothing while the turn still reads as running, so a late turn end still gets the full 10s', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'], shouldAdvanceTime: true });
    tails.S = [message('u', 'S', { author: 'user', type: 'user', text: 'hello' })];
    let running = true;
    api.getTurnState.mockImplementation(async () => (running ? { ...idleTurn, in_flight: true, foreground: 'running' as const } : idleTurn));
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    // The result lands, but its turn end is late: the summary still says thinking.
    const result = message('r', 'S', { text: 'Late end', read_at: null });
    tails.S = [...tails.S, result];
    await emit((h) => h.onMessageNew?.(result));
    act(() => { vi.advanceTimersByTime(30_000); });
    expect(bubbleOpen()).toBe(true);
    running = false;
    await emit((h) => h.onTurnEnd?.({ session_id: 'S' }));
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-toggle .pet-mini-running-dot')).toBeNull());
    act(() => { vi.advanceTimersByTime(9_800); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(400); });
    expect(bubbleOpen()).toBe(false);
  });

  it('does not linger an old reply again when it is edited', async () => {
    const old = message('r', 'S', { text: 'Old reply', metadata: { promoted_from: 'output' } });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), old];
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Old reply'));
    await emit((h) => h.onMessageUpdated?.({ ...old, text: 'Old reply, edited' }));
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('edited'));
    expect(bubbleOpen()).toBe(false);
  });

  it('hands keyboard focus to Vibey when a loading notice replaces the bubble', async () => {
    await runToResult();
    const toggle = document.querySelector<HTMLButtonElement>('.pet-mini-reply-toggle')!;
    act(() => { toggle.focus(); });
    expect(document.activeElement).toBe(toggle);
    // A new authorization resolves slowly: the loading notice takes the slot.
    const pending = deferred<WorkbenchSessionReadResult>();
    api.getMainSession.mockReturnValueOnce(pending.promise);
    await emit((h) => h.onAuthorizationChanged?.({}));
    expect(document.querySelector('.pet-mini-reply-toggle')).toBeNull();
    expect(document.activeElement).toBe(screen.getByLabelText('pet.recentTurns'));
    await act(async () => pending.resolve({ status: 200, session: session('S'), capabilities: { can_chat: true } }));
  });

  it('hands keyboard focus to Vibey when dictation replaces the bubble', async () => {
    class Dictation { done = new Promise<string>(() => {}); start = async () => {}; finish = () => {}; abort = () => {}; }
    vi.doMock('@/lib/showPageVoiceDictation', () => ({ ShowPageVoiceDictation: Dictation }));
    const locks = Object.getOwnPropertyDescriptor(navigator, 'locks');
    const media = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices');
    try {
      Object.defineProperty(navigator, 'locks', { configurable: true, value: {} });
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn() } });
      vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ available: true }) })));
      vi.resetModules();
      ({ PetPage } = await import('./PetPage'));
      await runToResult();
      await waitFor(() => expect(screen.getByLabelText('pet.voice.open')).toBeTruthy());
      const toggle = document.querySelector<HTMLButtonElement>('.pet-mini-reply-toggle')!;
      act(() => { toggle.focus(); });
      // Holding the summon hotkey starts dictation: the marquee takes the slot.
      summon('listen');
      await waitFor(() => expect(document.querySelector('.pet-mini-reply-toggle')).toBeNull());
      expect(document.activeElement).toBe(screen.getByLabelText('pet.recentTurns'));
    } finally {
      vi.doUnmock('@/lib/showPageVoiceDictation');
      vi.unstubAllGlobals();
      if (locks) Object.defineProperty(navigator, 'locks', locks);
      else Reflect.deleteProperty(navigator, 'locks');
      if (media) Object.defineProperty(navigator, 'mediaDevices', media);
      else Reflect.deleteProperty(navigator, 'mediaDevices');
      vi.resetModules();
      ({ PetPage } = await import('./PetPage'));
    }
  });

  // Running, then the turn ends; the test supplies what the end delivers.
  const runThenEnd = async (deliver: (finish: () => void) => Promise<void>) => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'], shouldAdvanceTime: true });
    tails.S = [message('r0', 'S', { text: 'Earlier reply' }), message('u', 'S', { author: 'user', type: 'user', text: 'hello' })];
    let running = true;
    api.getTurnState.mockImplementation(async () => (running ? { ...idleTurn, in_flight: true, foreground: 'running' as const } : idleTurn));
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    await deliver(() => { running = false; });
  };

  it('does not linger after a stopped turn, whose result is empty', async () => {
    await runThenEnd(async (finish) => {
      const silent = message('stop', 'S', { text: '', read_at: null });
      tails.S = [...tails.S, silent];
      finish();
      await emit((h) => {
        h.onMessageNew?.(silent);
        h.onTurnEnd?.({ session_id: 'S' });
      });
    });
    await waitFor(() => expect(bubbleOpen()).toBe(false));
  });

  it('does not take a background completion for the ended turn\'s reply', async () => {
    await runThenEnd(async (finish) => {
      const detached = message('bg', 'S', { text: 'Background run done', read_at: null, metadata: { detached: true } });
      const silent = message('stop', 'S', { text: '', read_at: null });
      tails.S = [...tails.S, detached, silent];
      finish();
      await emit((h) => {
        h.onMessageNew?.(detached);
        h.onMessageNew?.(silent);
        h.onTurnEnd?.({ session_id: 'S' });
      });
    });
    await waitFor(() => expect(bubbleOpen()).toBe(false));
  });

  it('counts a turn that ends while the history is open as already seen', async () => {
    await runThenEnd(async (finish) => {
      // The history is open (expansion confirmed) when the turn ends with its reply.
      await userEvent.click(screen.getByLabelText('pet.recentTurns'));
      await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).not.toBeNull());
      const reply = message('r1', 'S', { text: 'Answered while open', read_at: null });
      tails.S = [...tails.S, reply];
      finish();
      await emit((h) => {
        h.onMessageNew?.(reply);
        h.onTurnEnd?.({ session_id: 'S' });
      });
      await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Answered while open'));
    });
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(bubbleOpen()).toBe(false));
  });

  it('lingers a reply that lands after its turn end, once the history it was not in has closed', async () => {
    await runThenEnd(async (finish) => {
      // History open, then the turn ends before its result.
      await userEvent.click(screen.getByLabelText('pet.recentTurns'));
      await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).not.toBeNull());
      finish();
      await emit((h) => h.onTurnEnd?.({ session_id: 'S' }));
      await waitFor(() => expect(api.getTurnState).toHaveBeenCalled());
      await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    });
    // Closed before the reply came: it was never seen there.
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(bubbleOpen()).toBe(false));
    const late = message('late', 'S', { text: 'Late reply', read_at: null });
    tails.S = [...tails.S, late];
    await emit((h) => h.onMessageNew?.(late));
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    act(() => { vi.advanceTimersByTime(9_800); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(400); });
    expect(bubbleOpen()).toBe(false);
  });

  it('lingers once the reply arrives when the turn end came first', async () => {
    await runThenEnd(async (finish) => {
      finish();
      await emit((h) => h.onTurnEnd?.({ session_id: 'S' }));
    });
    await waitFor(() => expect(bubbleOpen()).toBe(false));
    const late = message('late', 'S', { text: 'Late reply', read_at: null });
    tails.S = [...tails.S, late];
    await emit((h) => h.onMessageNew?.(late));
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    act(() => { vi.advanceTimersByTime(9_800); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(400); });
    expect(bubbleOpen()).toBe(false);
  });

  it('keeps the lingering reply while a history request waits on the shell, and after it is refused', async () => {
    const expand = deferred<unknown>();
    const invoke = vi.fn((command: string, args?: { expanded?: boolean }) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded' && args?.expanded) return expand.promise;
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    try {
      await runToResult();
      const toggle = document.querySelector<HTMLButtonElement>('.pet-mini-reply-toggle')!;
      act(() => { toggle.focus(); });
      fireEvent.click(screen.getByLabelText('pet.recentTurns'));
      await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
      // Not yet confirmed: the reply still holds the bubble, and focus stays put.
      expect(bubbleOpen()).toBe(true);
      expect(document.activeElement).toBe(toggle);
      await act(async () => expand.reject(new Error('no monitor')));
      // Refused: the history never opened, so the reply plays out its time.
      expect(bubbleOpen()).toBe(true);
      act(() => { vi.advanceTimersByTime(10_200); });
      expect(bubbleOpen()).toBe(false);
    } finally {
      Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
      Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
    }
  });

  it('holds a lingering reply while the shell is slower to confirm the history than its time left', async () => {
    const expand = deferred<unknown>();
    const invoke = vi.fn((command: string, args?: { expanded?: boolean }) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null });
      if (command === 'pet_set_expanded' && args?.expanded) return expand.promise;
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
    try {
      await runToResult();
      act(() => { vi.advanceTimersByTime(9_000); });
      fireEvent.click(screen.getByLabelText('pet.recentTurns'));
      await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_set_expanded', { expanded: true }));
      // Far longer than the second left: the request holds the bubble.
      act(() => { vi.advanceTimersByTime(5_000); });
      expect(bubbleOpen()).toBe(true);
      await act(async () => expand.resolve({ panel_side: 'left', panel_edge: 'bottom' }));
      await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).not.toBeNull());
      expect(bubbleOpen()).toBe(true);
    } finally {
      Reflect.deleteProperty(window, '__AVIBE_DESKTOP_SHELL__');
      Reflect.deleteProperty(window, '__TAURI_INTERNALS__');
    }
  });

  it('does not take an earlier input\'s promoted answer for a later turn that ends without a reply', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'], shouldAdvanceTime: true });
    tails.S = [
      message('u1', 'S', { author: 'user', type: 'user', text: 'first' }),
      message('p1', 'S', { text: 'Promoted answer', metadata: { promoted_from: 'output' } }),
      message('u2', 'S', { author: 'user', type: 'user', text: 'second' }),
    ];
    let running = true;
    api.getTurnState.mockImplementation(async () => (running ? { ...idleTurn, in_flight: true, foreground: 'running' as const } : idleTurn));
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    // The later turn is stopped: it reaches idle before its empty row lands.
    running = false;
    await emit((h) => h.onTurnEnd?.({ session_id: 'S' }));
    await waitFor(() => expect(bubbleOpen()).toBe(false));
  });

  it('does not linger when a send grace runs out with no turn registered', async () => {
    tails.S = [message('u0', 'S', { author: 'user', type: 'user', text: 'earlier' }), message('r0', 'S', { text: 'Earlier reply' })];
    // The send is accepted and echoed back, so nothing local still holds the bubble.
    api.sendSessionMessage.mockImplementationOnce(async () => message('sent', 'S', { author: 'user', type: 'user', text: 'quick' }));
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'], shouldAdvanceTime: true });
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await userEvent.type(input, 'quick{Enter}');
    await waitFor(() => expect(pose()).toBe('running'));
    // The server never registers the turn: the local grace simply runs out.
    act(() => { vi.advanceTimersByTime(SEND_GRACE_MS + 100); });
    await waitFor(() => expect(pose()).not.toBe('running'));
    act(() => { vi.advanceTimersByTime(500); });
    expect(bubbleOpen()).toBe(false);
  });

  it('drops a lingering reply when the epoch changes', async () => {
    await runToResult();
    expect(bubbleOpen()).toBe(true);
    // A new authorization is a new epoch: its first read is the baseline.
    await resolveMainAs('S');
    await waitFor(() => expect(bubbleOpen()).toBe(false));
  });

  it('pauses while typing takes the slot, then plays the rest', async () => {
    await runToResult();
    act(() => { vi.advanceTimersByTime(4_000); });
    summon('listen');
    await screen.findByLabelText('pet.inputPlaceholder');
    act(() => { vi.advanceTimersByTime(60_000); });
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(5_900); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(200); });
    expect(bubbleOpen()).toBe(false);
  });

  it('holds while the pointer is over it, and leaves at least 3s after', async () => {
    await runToResult();
    act(() => { vi.advanceTimersByTime(9_000); });
    fireEvent.pointerEnter(bubbleWrap());
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(bubbleOpen()).toBe(true);
    fireEvent.pointerLeave(bubbleWrap());
    act(() => { vi.advanceTimersByTime(2_900); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(200); });
    expect(bubbleOpen()).toBe(false);
  });

  it('drops the pointer hold when the input replaces the bubble under a still pointer', async () => {
    await runToResult();
    act(() => { vi.advanceTimersByTime(4_000); });
    fireEvent.pointerEnter(bubbleWrap());
    // The summon opens the input in the bubble's place; no pointer leave comes.
    summon('listen');
    await screen.findByLabelText('pet.inputPlaceholder');
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(screen.queryByLabelText('pet.inputPlaceholder')).toBeNull();
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(5_900); });
    expect(bubbleOpen()).toBe(true);
    act(() => { vi.advanceTimersByTime(200); });
    expect(bubbleOpen()).toBe(false);
  });

  it('does not linger again once its history has been opened and closed', async () => {
    await runToResult();
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(document.querySelector('.pet-mini-detail.is-open')).not.toBeNull());
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(bubbleOpen()).toBe(false));
  });

  it('stays shown while a failure needs the user', async () => {
    tails.S = [
      message('u', 'S', { author: 'user', type: 'user', text: 'hello' }),
      message('n', 'S', {
        type: 'notify',
        text: 'No model source yet.',
        metadata: { event: 'backend_failure', failure_id: 'f', turn_id: 't', failure_next_step: 'models' },
      }),
    ];
    mainSession('S');
    render(<PetPage />);
    expect(await screen.findByText('pet.failedPrefix No model source yet.')).toBeTruthy();
    expect(bubbleOpen()).toBe(true);
  });

  it('stays shown while a send may have been admitted', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), message('q', 'S')];
    api.sendSessionMessage.mockRejectedValueOnce(new ApiError('dispatch pending', 504, null));
    mainSession('S');
    render(<PetPage />);
    summon('listen');
    const input = await screen.findByLabelText('pet.inputPlaceholder') as HTMLTextAreaElement;
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('S'));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    await userEvent.type(input, 'deploy{Enter}');
    expect(await screen.findByText('newSession.sendUncertain')).toBeTruthy();
    expect(bubbleOpen()).toBe(true);
  });

  it('opens with a Vibey click and hides again when that history closes', async () => {
    tails.S = [message('u', 'S', { author: 'user', type: 'user', text: 'hello' }), message('r', 'S', { text: 'Old reply' })];
    mainSession('S');
    render(<PetPage />);
    await waitFor(() => expect(document.querySelector('.pet-mini-reply-bubble')?.textContent).toContain('Old reply'));
    expect(bubbleOpen()).toBe(false);
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(bubbleOpen()).toBe(true));
    expect(document.querySelector('.pet-mini-detail.is-open')).not.toBeNull();
    expect(screen.getByLabelText('pet.recentTurns').getAttribute('aria-expanded')).toBe('true');
    await userEvent.click(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(bubbleOpen()).toBe(false));
    expect(screen.getByLabelText('pet.recentTurns').getAttribute('aria-expanded')).toBe('false');
  });
});

describe('dragging the pet to the top of the display (spec §9.1)', () => {
  const shell = (invoke: (command: string, args?: unknown) => Promise<unknown>) => {
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true, configurable: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke }, configurable: true });
  };
  const edge = () => document.querySelector('.pet-mini-stage')?.getAttribute('data-edge');
  const drag = (avatar: HTMLElement) => {
    fireEvent.pointerDown(avatar, { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(avatar, { buttons: 1, pointerId: 1, clientX: 30, clientY: 10 });
  };

  it('draws the layout the shell chose at load', async () => {
    shell(vi.fn(async (command: string) => command === 'pet_ready'
      ? { binding: 'S', revision: 1, summon_pending: null, layout: { panel_side: 'left', panel_edge: 'top' } }
      : { panel_side: 'left', panel_edge: 'top' }));
    mainSession('S'); render(<PetPage />);
    await screen.findByText('Session S');
    await waitFor(() => expect(edge()).toBe('top'));
  });

  it('draws the layout a drop answers before the shell brings the full window back', async () => {
    const dropped = deferred<unknown>();
    const edgeAtSettle: (string | null | undefined)[] = [];
    const invoke = vi.fn((command: string) => {
      if (command === 'pet_ready') return Promise.resolve({ binding: 'S', revision: 1, summon_pending: null, layout: { panel_side: 'left', panel_edge: 'bottom' } });
      if (command === 'pet_drag') return dropped.promise;
      if (command === 'pet_settle') { edgeAtSettle.push(edge()); return Promise.resolve({ panel_side: 'left', panel_edge: 'top' }); }
      return Promise.resolve({ panel_side: 'left', panel_edge: 'bottom' });
    });
    shell(invoke);
    mainSession('S'); render(<PetPage />);
    await screen.findByText('Session S');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_ready', { layouts: true }));
    await act(async () => {});
    drag(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_drag'));
    expect(invoke.mock.calls.some(([command]) => command === 'plugin:window|start_dragging')).toBe(false);
    expect(edge()).toBe('bottom');
    expect(invoke.mock.calls.some(([command]) => command === 'pet_settle')).toBe(false);
    // The column waits out the drag: the window is only the strip around Vibey.
    expect(document.querySelector('.pet-mini-stage')?.hasAttribute('data-dragging')).toBe(true);
    await act(async () => dropped.resolve({ panel_side: 'left', panel_edge: 'top' }));
    await waitFor(() => expect(edgeAtSettle).toEqual(['top']));
    expect(edge()).toBe('top');
    await waitFor(() => expect(document.querySelector('.pet-mini-stage')?.hasAttribute('data-dragging')).toBe(false));
    // The drag was not a click: the history stays closed.
    expect(document.querySelector('.pet-mini-detail.is-open')).toBeNull();
  });

  it('drags with the window\'s own start_dragging under a shell that does not drag the pet', async () => {
    const invoke = vi.fn(async (command: string) => command === 'pet_ready'
      ? { binding: 'S', revision: 1, summon_pending: null }
      : { panel_side: 'left', panel_edge: 'bottom' });
    shell(invoke);
    mainSession('S'); render(<PetPage />);
    await screen.findByText('Session S');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_ready', { layouts: true }));
    await act(async () => {});
    drag(screen.getByLabelText('pet.recentTurns'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('plugin:window|start_dragging'));
    expect(invoke.mock.calls.some(([command]) => command === 'pet_drag')).toBe(false);
    expect(edge()).toBe('bottom');
    // The window keeps its size in this drag, so the column stays drawn.
    expect(document.querySelector('.pet-mini-stage')?.hasAttribute('data-dragging')).toBe(false);
  });

  it('draws a layout the shell moved it into before the shell brings the window that fits it', async () => {
    const edgeAtSettle: (string | null | undefined)[] = [];
    const invoke = vi.fn(async (command: string) => {
      if (command === 'pet_ready') return { binding: 'S', revision: 1, summon_pending: null, layout: { panel_side: 'left', panel_edge: 'bottom' } };
      if (command === 'pet_settle') { edgeAtSettle.push(edge()); return { panel_side: 'left', panel_edge: 'top' }; }
      return { panel_side: 'left', panel_edge: 'bottom' };
    });
    shell(invoke);
    mainSession('S'); render(<PetPage />);
    await screen.findByText('Session S');
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('pet_ready', { layouts: true }));
    act(() => { window.dispatchEvent(new CustomEvent(PET_LAYOUT_EVENT, { detail: { layout: { panel_side: 'left', panel_edge: 'top' } } })); });
    // Every settle (earlier tests' documents still listen in this window)
    // comes after the new layout is drawn.
    await waitFor(() => expect(edgeAtSettle.length).toBeGreaterThan(0));
    expect(new Set(edgeAtSettle)).toEqual(new Set(['top']));
    expect(edge()).toBe('top');
  });

  it('peeks the stack layers out toward Vibey in either layout', async () => {
    canUseAgents = true;
    const row = workingIn('running').agents[0];
    runningAgents = { ...workingIn('running'), agents: ['A', 'B', 'C'].map((id) => ({ ...row, session_id: id })) };
    const layers = () => [...document.querySelectorAll<HTMLElement>('.pet-mini-layer')].map((layer) => [layer.style.top, layer.style.bottom]);
    let reply: 'top' | 'bottom' = 'bottom';
    shell(vi.fn(async (command: string) => command === 'pet_ready'
      ? { binding: 'S', revision: 1, summon_pending: null, layout: { panel_side: 'left', panel_edge: reply } }
      : { panel_side: 'left', panel_edge: reply }));
    mainSession('S'); const view = render(<PetPage />);
    await waitFor(() => expect(document.querySelectorAll('.pet-mini-layer')).toHaveLength(2));
    // Below the front card: the nearest layer peeks 9px, the next 18px.
    expect(layers()).toEqual([['9px', '9px'], ['18px', '0px']]);
    expect(document.querySelector<HTMLElement>('.pet-mini-collapsed')!.style.paddingBottom).toBe('18px');
    view.unmount();
    reply = 'top';
    vi.resetModules();
    ({ PetPage } = await import('./PetPage'));
    render(<PetPage />);
    await waitFor(() => expect(edge()).toBe('top'));
    await waitFor(() => expect(document.querySelectorAll('.pet-mini-layer')).toHaveLength(2));
    expect(layers()).toEqual([['9px', '9px'], ['0px', '18px']]);
    expect(document.querySelector<HTMLElement>('.pet-mini-collapsed')!.style.paddingTop).toBe('18px');
  });
});
