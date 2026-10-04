/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchEventHandlers, WorkbenchMessage, WorkbenchSession } from '@/context/ApiContext';

import { PET_BOUND_EVENT, PET_SUMMON_EVENT } from './petBridge';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const handlers = new Set<WorkbenchEventHandlers>();
const emit = (pick: (h: WorkbenchEventHandlers) => void) => act(() => handlers.forEach(pick));

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void };
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
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
let sessionReads: Record<string, () => Promise<{ status: number; session: WorkbenchSession | null }>> = {};
let tails: Record<string, WorkbenchMessage[]> = {};
let switcherSessions: WorkbenchSession[] = [];
let unreadBySession: Record<string, number> = {};
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
    : Promise.resolve({ status: 200, session: session(id) }))),
  listSessionMessages: vi.fn(async (id: string) => ({ messages: tails[id] ?? [], next_after_id: null, next_before_id: null })),
  getTurnState: vi.fn(async () => idleTurn),
  listSessions: vi.fn(async () => ({ sessions: switcherSessions, next_before_id: null })),
  getVaultRequests: vi.fn(async () => ({ requests: [] })),
  sendSessionMessage: vi.fn(async () => message('sent', 'S', { author: 'user', type: 'user' })),
  connectWorkbenchEvents: (h: WorkbenchEventHandlers) => {
    handlers.add(h);
    return () => handlers.delete(h);
  },
};

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
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

const devBind = (sessionId: string | null) => {
  if (sessionId) window.localStorage.setItem('avibe.pet.devBinding', sessionId);
  else window.localStorage.removeItem('avibe.pet.devBinding');
};
const bound = (sessionId: string | null) => act(() => {
  devBind(sessionId);
  window.dispatchEvent(new CustomEvent(PET_BOUND_EVENT, { detail: { session_id: sessionId } }));
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
    const lateA = deferred<{ status: number; session: WorkbenchSession | null }>();
    sessionReads.A = () => lateA.promise;
    devBind('A');
    render(<PetPage />);
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('A'));
    await bound('B');
    await waitFor(() => expect(api.getSessionResult).toHaveBeenCalledWith('B'));
    await act(async () => lateA.resolve({ status: 404, session: null }));
    summon('show');
    expect(await screen.findByText('Session B')).toBeTruthy();
    expect(window.localStorage.getItem('avibe.pet.devBinding')).toBe('B');
  });

  it('clears a persisted binding to a Runtime-owned session and opens the switcher', async () => {
    sessionReads['ses-workspace-notices'] = async () => ({
      status: 200,
      session: session('ses-workspace-notices', { visibility: 'system' }),
    });
    switcherSessions = [session('fresh', { title: 'Brand new' })];
    devBind('ses-workspace-notices');
    render(<PetPage />);
    await waitFor(() => expect(window.localStorage.getItem('avibe.pet.devBinding')).toBeNull());
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
    expect(window.localStorage.getItem('avibe.pet.devBinding')).toBe('new');
  });
});

describe('PetPage state', () => {
  it('shows Needs input for an open quick-reply group and clears it on message.updated', async () => {
    const asking = message('q', 'S', { content: { quick_replies: ['Yes', 'No'] } });
    tails.S = [message('u', 'S', { author: 'user', type: 'user' }), asking];
    devBind('S');
    render(<PetPage />);
    await waitFor(() => expect(pose()).toBe('needs_input'));
    await emit((h) => h.onMessageUpdated?.({ ...asking, content: { ...asking.content, quick_reply_chosen: 'Yes' } }));
    await waitFor(() => expect(pose()).toBe('idle'));
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
    await act(async () => ready.resolve({ binding: 'S', summon_pending: { intent: 'listen' } }));
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
    act(() => {
      window.dispatchEvent(new CustomEvent(PET_BOUND_EVENT, { detail: { session_id: 'B' } }));
    });
    await act(async () => ready.resolve({ binding: 'A', summon_pending: null }));
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
      if (command === 'pet_ready') return Promise.resolve({ binding: 'A', summon_pending: null });
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
