/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { SessionRuntimeState, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ComposerProps } from './Composer';
import {
  chatSnapshotOwner,
  readChatSnapshot,
  writeChatSnapshot,
  type ChatSnapshot,
} from '../../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';
import { writeChatViewMode } from '../../lib/chatViewMemory';
import { CHAT_SNAPSHOT_WRITE_DELAY_MS } from './useChatSnapshot';

// Opening a chat paints its last known window before the bootstrap answers
// (docs/plans/web-ui-local-cache.md, Phase 2). The hand-over is where it can go
// wrong: a cached row the server deleted surviving the live answer, a row the
// stream delivered meanwhile being thrown away with the cache, a cached
// capability letting someone act before the server agreed, or the cache being
// written from something other than what the server answered for this reader.

const mocks = vi.hoisted(() => ({
  api: {
    cancelSession: vi.fn(),
    connectWorkbenchEvents: vi.fn(),
    getCachedSessionDraft: vi.fn(),
    getSession: vi.fn(),
    getSessionActivity: vi.fn(),
    getSessionActivityGroup: vi.fn(),
    getSessionBootstrap: vi.fn(),
    getTurnState: vi.fn(),
    getWorkbenchPrefs: vi.fn(),
    listSessionMessages: vi.fn(),
    listSessionQueue: vi.fn(),
    waitForAgentActivityConfigMutations: vi.fn(),
    onSessionArchived: vi.fn(),
  },
  apiFetch: vi.fn(),
  events: null as null | {
    onConnected: () => void;
    onMessageNew: (message: WorkbenchMessage) => void;
  },
  composer: null as ComposerProps | null,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../../lib/apiFetch', () => ({
  apiFetch: (...args: unknown[]) => mocks.apiFetch(...args),
}));

vi.mock('../../context/ApiContext', () => ({
  useApi: () => mocks.api,
}));

vi.mock('../../context/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../context/WorkbenchInboxContext', () => ({
  useWorkbenchInbox: () => ({ unreadBySession: {}, markRead: vi.fn() }),
}));

vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: {
      can_chat: true,
      can_manage_instance: false,
      can_use_show_pages: false,
      can_use_system: false,
      can_use_vault_secrets: false,
    },
  }),
}));

vi.mock('../../context/ComposerBridgeContext', () => ({
  useRegisterComposerTarget: vi.fn(),
}));

vi.mock('../../context/WindowManagerContext', () => ({
  useWindowManager: () => ({ focusedId: null, focusCanvas: vi.fn() }),
}));

vi.mock('../../lib/useIsDesktop', () => ({
  isDesktopViewport: () => false,
  useIsDesktop: () => false,
}));

vi.mock('../../lib/useIosKeyboardInset', () => ({
  useIosKeyboardInset: vi.fn(),
}));

vi.mock('../../lib/useFileDrop', () => ({
  useFileDrop: () => ({ dragging: false, handlers: {} }),
}));

vi.mock('../../lib/usePendingVaultRequests', () => ({
  usePendingVaultRequests: () => ({ requests: [], refresh: vi.fn() }),
}));

vi.mock('./useSessionActions', () => ({
  useSessionActions: () => ({ actions: [], archiveDialog: null, requestArchive: vi.fn(), canArchive: false }),
}));

vi.mock('./useShowPageAnnotation', () => ({
  useShowPageAnnotation: () => ({
    state: { enabled: false, mode: 'smart' },
    setIframe: vi.fn(),
    handleIframeLoad: vi.fn(),
    handleShortcutKeyDown: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
    setMode: vi.fn(),
  }),
}));

vi.mock('./Composer', () => ({
  Composer: (props: ComposerProps) => {
    mocks.composer = props;
    return <div data-testid="composer" data-disabled={String(Boolean(props.disabled))} />;
  },
}));

import { ChatPage } from './ChatPage';

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void };
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
};

const SESSION = 'ses_a';

const idleTurnState: SessionRuntimeState = {
  in_flight: false,
  foreground: 'idle',
  native_turn_started: false,
  pending_input_count: 0,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'connected',
};

const message = (id: string, text: string, extra: Partial<WorkbenchMessage> = {}): WorkbenchMessage => ({
  id,
  scope_id: null,
  session_id: SESSION,
  platform: 'avibe',
  author: 'agent',
  type: 'result',
  source: 'agent',
  author_id: null,
  author_name: null,
  native_message_id: null,
  parent_native_message_id: null,
  text,
  content: {},
  metadata: {},
  created_at: `2026-11-01T08:00:0${id.slice(-1)}Z`,
  updated_at: `2026-11-01T08:00:0${id.slice(-1)}Z`,
  delivered_at: `2026-11-01T08:00:0${id.slice(-1)}Z`,
  read_at: null,
  ...extra,
} as WorkbenchMessage);

const sessionRow = (title: string) => ({ id: SESSION, title, status: 'active' } as WorkbenchSession);

const bootstrap = (messages: WorkbenchMessage[], { canChat = true, title = 'Live title' } = {}) => ({
  session: sessionRow(title),
  capabilities: { can_chat: canChat },
  agents: [],
  default_agent_name: null,
  config: { ui: {} },
  messages,
  next_after_id: null,
  next_before_id: null,
  turn_state: idleTurnState,
  queued: [],
  draft: { text: '', updated_at: null },
});

const seed = async (messages: WorkbenchMessage[], { canChat = true } = {}) => {
  setLocalSnapshotPrincipal('local');
  const value: ChatSnapshot = {
    session: sessionRow('Cached title'),
    messages,
    next_before_id: null,
    agents: [],
    default_agent_name: null,
    can_chat: canChat,
  };
  await writeChatSnapshot(chatSnapshotOwner(SESSION)!, value);
};

const mount = () => render(
  <MemoryRouter initialEntries={[`/chat/${SESSION}`]}>
    <Routes>
      <Route path="/chat/:sessionId" element={<ChatPage />} />
    </Routes>
  </MemoryRouter>,
);

// File a copy of one reader's record under another reader, as another tab of the
// same origin could have, without going through the current reader's store.
const copyRecordToReader = async (from: string, to: string, edit: (value: ChatSnapshot) => ChatSnapshot) => {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('avibe-local-snapshots', 1);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const tx = db.transaction('snapshots', 'readwrite');
  const store = tx.objectStore('snapshots');
  const record = await new Promise<{ value: ChatSnapshot }>((resolve) => {
    const request = store.get(`${from}\nchat:${SESSION}`);
    request.onsuccess = () => resolve(request.result);
  });
  store.put({ ...record, id: `${to}\nchat:${SESSION}`, principal: to, value: edit(record.value) });
  await new Promise((resolve) => { tx.oncomplete = resolve; });
  db.close();
};

const composerState = () => screen.queryByTestId('composer')?.getAttribute('data-disabled') ?? 'absent';
const waitForWriteBack = () => act(() => new Promise((resolve) => setTimeout(resolve, CHAT_SNAPSHOT_WRITE_DELAY_MS + 150)));

let live: Deferred<ReturnType<typeof bootstrap>>;

beforeEach(() => {
  for (const name of ['ResizeObserver', 'IntersectionObserver']) {
    vi.stubGlobal(name, class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  }
  live = deferred();
  mocks.events = null;
  mocks.composer = null;
  vi.clearAllMocks();
  mocks.api.connectWorkbenchEvents.mockImplementation((events) => {
    mocks.events = events;
    return () => {};
  });
  mocks.api.cancelSession.mockResolvedValue({ ok: true });
  mocks.api.getCachedSessionDraft.mockReturnValue(null);
  mocks.api.getSession.mockReturnValue(new Promise(() => {}));
  mocks.api.getSessionActivity.mockResolvedValue({ groups: [] });
  mocks.api.getSessionActivityGroup.mockResolvedValue({});
  mocks.api.getSessionBootstrap.mockReturnValue(live.promise);
  mocks.api.getTurnState.mockResolvedValue(idleTurnState);
  mocks.api.getWorkbenchPrefs.mockResolvedValue({});
  mocks.api.listSessionMessages.mockReturnValue(new Promise(() => {}));
  mocks.api.listSessionQueue.mockResolvedValue({ queued: [] });
  mocks.api.waitForAgentActivityConfigMutations.mockResolvedValue(undefined);
  mocks.api.onSessionArchived.mockReturnValue(() => {});
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('ChatPage conversation snapshot', () => {
  it('shows the last known window at once, with an inert composer, and lets the bootstrap replace it', async () => {
    await seed([message('m1', 'cached question'), message('m2', 'cached answer')]);
    mount();

    // The server has not answered: the snapshot is on screen instead of a loader,
    // and the composer is there but cannot send.
    await screen.findByText('cached answer');
    expect(screen.getByText('cached question')).toBeTruthy();
    expect(screen.queryByText('common.loading')).toBeNull();
    expect(composerState()).toBe('true');
    expect(await mocks.composer?.onSend('sent from the snapshot')).toBe(false);
    expect(mocks.apiFetch).not.toHaveBeenCalled();

    // m1 was deleted on the server, m2 changed, m3 is new.
    await act(async () => live.resolve(bootstrap([message('m2', 'live answer'), message('m3', 'live follow-up')])));

    await screen.findByText('live follow-up');
    expect(screen.queryByText('cached question')).toBeNull();
    expect(screen.queryByText('cached answer')).toBeNull();
    expect(screen.getByText('live answer')).toBeTruthy();
    expect(composerState()).toBe('false');
  });

  it('keeps a row that arrived over the stream while the bootstrap was out', async () => {
    await seed([message('m1', 'cached question'), message('m2', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');
    expect(mocks.events).not.toBeNull();

    act(() => mocks.events!.onMessageNew(message('m4', 'streamed during the load')));
    await screen.findByText('streamed during the load');

    // The bootstrap was read before m4 existed.
    await act(async () => live.resolve(bootstrap([message('m2', 'live answer')])));

    await screen.findByText('live answer');
    expect(screen.getByText('streamed during the load')).toBeTruthy();
    expect(screen.queryByText('cached question')).toBeNull();
  });

  it('lets a recovery read that lands before the bootstrap replace the painted window too', async () => {
    const recovery = deferred<{ messages: WorkbenchMessage[]; next_after_id: null; next_before_id: null }>();
    mocks.api.listSessionMessages.mockReturnValue(recovery.promise);
    await seed([message('m1', 'cached question'), message('m2', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');

    act(() => mocks.events!.onConnected());
    await act(async () => recovery.resolve({
      messages: [message('m2', 'recovered answer')],
      next_after_id: null,
      next_before_id: null,
    }));

    await screen.findByText('recovered answer');
    expect(screen.queryByText('cached question')).toBeNull();
    // The bootstrap still decides that the chat is live.
    expect(composerState()).toBe('true');
    await act(async () => live.resolve(bootstrap([message('m2', 'recovered answer')])));
    await waitFor(() => expect(composerState()).toBe('false'));
    expect(screen.queryByText('cached question')).toBeNull();
  });

  it('never lets a cached can_chat open the composer when the bootstrap refuses it', async () => {
    await seed([message('m1', 'cached answer')], { canChat: true });
    mount();
    await screen.findByText('cached answer');
    expect(composerState()).toBe('true');

    await act(async () => live.resolve(bootstrap([message('m1', 'cached answer')], { canChat: false })));
    await waitFor(() => expect(composerState()).toBe('absent'));
  });

  it('does not flash the transcript of a chat that reopens on its Show Page', async () => {
    writeChatViewMode(SESSION, 'show-page');
    await seed([message('m1', 'cached answer')]);
    mount();
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.queryByText('cached answer')).toBeNull();
    expect(screen.getByText('common.loading')).toBeTruthy();
  });

  it('shows the loader as before when there is no snapshot', async () => {
    setLocalSnapshotPrincipal('local');
    mount();
    await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    expect(screen.getByText('common.loading')).toBeTruthy();
    expect(composerState()).toBe('absent');
  });

  it('stores the live window after it settles, without the painted rows or a send still pending', async () => {
    await seed([message('m1', 'cached question')]);
    mount();
    await screen.findByText('cached question');

    // Painting alone stores nothing.
    await waitForWriteBack();
    expect((await readChatSnapshot(SESSION))?.messages.map((m) => m.id)).toEqual(['m1']);

    await act(async () => live.resolve(bootstrap([
      message('m2', 'live answer'),
      message('m3', 'claimed input', { type: 'user', author: 'user', projection: 'claimed_delivery' }),
    ])));
    await screen.findByText('live answer');
    act(() => mocks.events!.onMessageNew(message('m5', 'streamed reply')));
    act(() => mocks.events!.onMessageNew(
      message('m6', 'pending send', { type: 'user', author: 'user', state: 'claimed' }),
    ));
    await screen.findByText('streamed reply');
    await waitForWriteBack();

    const stored = await readChatSnapshot(SESSION);
    expect(stored?.session.title).toBe('Live title');
    expect(stored?.messages.map((m) => m.id)).toEqual(['m2', 'm5']);
  });

  it('drops painted rows the moment another reader is named', async () => {
    await seed([message('m1', 'alice cached row')]);
    mount();
    await screen.findByText('alice cached row');

    act(() => setLocalSnapshotPrincipal('remote:bob'));
    await waitFor(() => expect(screen.queryByText('alice cached row')).toBeNull());
    expect(screen.getByText('common.loading')).toBeTruthy();
  });

  it('drops painted rows when the store is cleared, as an authorization change does', async () => {
    await seed([message('m1', 'cached before the change')]);
    mount();
    await screen.findByText('cached before the change');

    await act(async () => clearLocalSnapshots());
    expect(screen.queryByText('cached before the change')).toBeNull();
    expect(screen.getByText('common.loading')).toBeTruthy();

    await act(async () => live.resolve(bootstrap([message('m2', 'live after the change')])));
    await screen.findByText('live after the change');
  });

  it("paints the next reader's own record, never under the last reader's rows, and stores nothing for a bootstrap requested before the switch", async () => {
    await seed([message('m1', 'alice cached row')]);
    await copyRecordToReader('local', 'remote:bob', (value) => ({
      ...value,
      messages: [message('m2', 'bob cached row')],
    }));
    mount();
    await screen.findByText('alice cached row');

    act(() => setLocalSnapshotPrincipal('remote:bob'));
    await screen.findByText('bob cached row');
    expect(screen.queryByText('alice cached row')).toBeNull();

    // The bootstrap was requested for the last reader and answers after the switch.
    await act(async () => live.resolve(bootstrap([message('m3', 'answered before the switch')])));
    await screen.findByText('answered before the switch');
    await waitForWriteBack();
    expect((await readChatSnapshot(SESSION))?.messages.map((m) => m.id)).toEqual(['m2']);
  });

  it('stores a chat opened on a cold start, whose bootstrap went out before the reader was named', async () => {
    // The authorization boundary names the reader in an effect that runs after
    // the chat page's own, so the bootstrap is already out.
    mount();
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalled());
    act(() => setLocalSnapshotPrincipal('local'));

    await act(async () => live.resolve(bootstrap([message('m1', 'live answer')])));
    await screen.findByText('live answer');
    await waitForWriteBack();
    expect((await readChatSnapshot(SESSION))?.messages.map((m) => m.id)).toEqual(['m1']);
  });

  it('stores nothing after a reader change until that reader\'s own bootstrap, even once the first reader is back', async () => {
    setLocalSnapshotPrincipal('local');
    mount();
    await act(async () => live.resolve(bootstrap([message('m1', 'live answer')])));
    await screen.findByText('live answer');
    await waitForWriteBack();
    expect((await readChatSnapshot(SESSION))?.messages.map((m) => m.id)).toEqual(['m1']);

    // Another reader is named and the first one comes back (each deletes the
    // other's records). The rows on screen were never read for this naming.
    act(() => setLocalSnapshotPrincipal('remote:bob'));
    act(() => setLocalSnapshotPrincipal('local'));
    act(() => mocks.events!.onMessageNew(message('m2', 'streamed after the switch')));
    await screen.findByText('streamed after the switch');
    await waitForWriteBack();
    expect(await readChatSnapshot(SESSION)).toBeNull();
  });

  it('does not put back a conversation the store was cleared of while a write was owed', async () => {
    setLocalSnapshotPrincipal('local');
    mount();
    await act(async () => live.resolve(bootstrap([message('m1', 'live answer')])));
    await screen.findByText('live answer');

    // An authorization change clears the store while the write-back is pending.
    await act(async () => clearLocalSnapshots());
    await waitForWriteBack();
    expect(await readChatSnapshot(SESSION)).toBeNull();
  });

  it('deletes the snapshot when the bootstrap answers 404, and shows the failure', async () => {
    await seed([message('m1', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');

    await act(async () => live.reject(Object.assign(new Error('Session not found'), { status: 404 })));

    await screen.findByText('Session not found');
    expect(screen.queryByText('cached answer')).toBeNull();
    await waitFor(async () => expect(await readChatSnapshot(SESSION)).toBeNull());
  });
});
