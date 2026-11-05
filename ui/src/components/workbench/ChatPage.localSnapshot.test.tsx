/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import type { SessionRuntimeState, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ComposerProps } from './Composer';
import {
  beginChatSnapshotRequest,
  chatSnapshotOwnerForAnswer,
  readChatSnapshot,
  writeChatSnapshot,
  type ChatSnapshot,
} from '../../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';
import { writeChatViewMode } from '../../lib/chatViewMemory';

// Opening a chat paints its last known window before the bootstrap answers
// (docs/plans/web-ui-local-cache.md, Phase 2), fully formed: the composer, its
// route picker, the actions under messages and in the header all render from
// the cache, usable, and the live answer may only narrow them. The hand-over is
// where it can go wrong: a cached row the server deleted surviving the live
// answer, a row the stream delivered or the reader sent meanwhile being thrown
// away with the cache, the composer remounting (and losing what was typed), or
// the cache being written from something other than what the server answered
// for this reader.

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
    probeShowPageAccess: vi.fn(),
    recoverSessionDraftAfterRejectedSend: vi.fn(),
    reconcileSessionDraftAfterSend: vi.fn(),
    cacheSessionDraft: vi.fn(),
    setSessionDraft: vi.fn(),
  },
  apiFetch: vi.fn(),
  capabilities: {
    can_chat: true,
    can_manage_instance: false,
    can_use_show_pages: true,
    can_use_system: false,
    can_use_vault_secrets: false,
  },
  composerMounts: 0,
  sessionActionsWritable: [] as boolean[],
  routePicker: null as null | { onChange: (changes: Record<string, unknown>) => void },
  events: null as null | {
    onConnected: () => void;
    onMessageNew: (message: WorkbenchMessage) => void;
    onMessageUpdated: (message: WorkbenchMessage) => void;
  },
  composer: null as ComposerProps | null,
  inbox: { unreadBySession: {} as Record<string, number>, markRead: vi.fn() },
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
  useWorkbenchInbox: () => mocks.inbox,
}));

vi.mock('../../context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({ capabilities: mocks.capabilities }),
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

// The session actions menu lists what the page says is writable.
vi.mock('./useSessionActions', () => ({
  useSessionActions: ({ writable }: { writable: boolean }) => (mocks.sessionActionsWritable.push(writable), {
    actions: writable ? [{ id: 'pin', group: 'organize', label: 'pin', onSelect: () => {} }] : [],
    archiveDialog: null,
    requestArchive: () => {},
    canArchive: false,
  }),
}));

vi.mock('./ShowPageLaunchControl', () => ({
  ShowPageLaunchControl: () => <button type="button" data-testid="show-page-launch">show page</button>,
}));

vi.mock('./AgentRoutePicker', () => ({
  AgentRoutePicker: (props: {
    value: { agent_name: string | null; model: string | null };
    agents: Array<{ name: string }>;
    disabled?: boolean;
    onChange: (changes: Record<string, unknown>) => void;
  }) => {
    mocks.routePicker = props;
    return (
      <div
        data-testid="route-picker"
        data-route={`${props.value.agent_name}/${props.value.model}`}
        data-agents={props.agents.map((choice) => choice.name).join(',')}
        data-disabled={String(Boolean(props.disabled))}
      />
    );
  },
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

// The real composer is covered on its own (Composer.draftReseed.test.tsx); here it
// records how often it mounted, which draft it was handed, and renders the
// actions row (the route picker) the page gives it.
vi.mock('./Composer', async () => {
  const { useState } = await import('react');
  return {
    Composer: (props: ComposerProps) => {
      mocks.composer = props;
      const [instance] = useState(() => {
        mocks.composerMounts += 1;
        return mocks.composerMounts;
      });
      return (
        <div
          data-testid="composer"
          data-disabled={String(Boolean(props.disabled))}
          data-instance={instance}
          data-draft={props.initialDraft ?? ''}
        >
          {props.actions}
        </div>
      );
    },
  };
});

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

const sessionRow = (title: string, extra: Partial<WorkbenchSession> = {}) => ({
  id: SESSION,
  title,
  status: 'active',
  agent_name: 'vibey',
  agent_backend: 'vibey',
  model: 'cached-model',
  ...extra,
} as WorkbenchSession);

const agent = { id: 'agent-vibey', name: 'vibey', backend: 'vibey', model: 'cached-model' } as VibeAgentBrief;

const bootstrap = (
  messages: WorkbenchMessage[],
  { canChat = true, title = 'Live title', session = {} as Partial<WorkbenchSession>, draft = '' } = {},
) => ({
  session: sessionRow(title, session),
  capabilities: { can_chat: canChat },
  agents: [agent],
  default_agent_name: 'vibey',
  config: { ui: {} },
  messages,
  next_after_id: null,
  next_before_id: null,
  turn_state: idleTurnState,
  queued: [],
  draft: { text: draft, updated_at: null },
});

const seed = async (messages: WorkbenchMessage[], { canChat = true, fontSize = null as number | null } = {}) => {
  setLocalSnapshotPrincipal('local');
  const value: ChatSnapshot = {
    session: sessionRow('Cached title'),
    messages,
    next_before_id: null,
    agents: [agent],
    default_agent_name: 'vibey',
    can_chat: canChat,
    chat_message_font_size: fontSize,
  };
  await writeChatSnapshot(chatSnapshotOwnerForAnswer(beginChatSnapshotRequest(SESSION))!, value);
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
// The write-back settles a second after the window does (plan section 4.3).
const waitForWriteBack = () => act(() => new Promise((resolve) => setTimeout(resolve, 1000 + 150)));

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
  mocks.composerMounts = 0;
  mocks.sessionActionsWritable = [];
  mocks.routePicker = null;
  mocks.inbox = { unreadBySession: {}, markRead: vi.fn(async () => true) };
  Object.assign(mocks.capabilities, { can_chat: true, can_use_show_pages: true });
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
  mocks.api.probeShowPageAccess.mockReturnValue(new Promise(() => {}));
  mocks.api.reconcileSessionDraftAfterSend.mockResolvedValue(undefined);
  mocks.api.recoverSessionDraftAfterRejectedSend.mockResolvedValue(undefined);
  mocks.api.setSessionDraft.mockResolvedValue({ ok: true });
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, 'hasFocus');
  window.localStorage.clear();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('ChatPage conversation snapshot', () => {
  it('shows the last known window at once, ready to use, and lets the bootstrap replace it', async () => {
    await seed([message('m1', 'cached question'), message('m2', 'cached answer')]);
    mount();

    // The server has not answered: the snapshot is on screen instead of a loader,
    // and the composer is there, enabled.
    await screen.findByText('cached answer');
    expect(screen.getByText('cached question')).toBeTruthy();
    expect(screen.queryByText('common.loading')).toBeNull();
    expect(composerState()).toBe('false');

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

  it('keeps a cached row the stream updated while the bootstrap was out, over the older bootstrap copy', async () => {
    await seed([message('m1', 'cached question'), message('m2', 'cached notice')]);
    mount();
    await screen.findByText('cached notice');

    // A retry admitted from another tab updates the notice in place.
    act(() => mocks.events!.onMessageUpdated(message('m2', 'notice after the retry')));
    await screen.findByText('notice after the retry');

    // The bootstrap was read before that update.
    await act(async () => live.resolve(bootstrap([message('m2', 'notice before the retry')])));

    await waitFor(() => expect(composerState()).toBe('false'));
    expect(screen.getByText('notice after the retry')).toBeTruthy();
    expect(screen.queryByText('notice before the retry')).toBeNull();
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
    await act(async () => live.resolve(bootstrap([message('m2', 'recovered answer')])));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalled());
    expect(screen.getByText('recovered answer')).toBeTruthy();
    expect(screen.queryByText('cached question')).toBeNull();
  });

  it('narrows what the cache enabled when the bootstrap says this reader cannot chat', async () => {
    await seed([message('m1', 'cached answer')], { canChat: true });
    mount();
    await screen.findByText('cached answer');
    expect(composerState()).toBe('false');

    await act(async () => live.resolve(bootstrap([message('m1', 'cached answer')], { canChat: false })));
    await waitFor(() => expect(composerState()).toBe('absent'));
    expect(screen.queryByRole('button', { name: 'workbench.sessionActions' })).toBeNull();
  });

  it('renders the route picker, the actions under messages, and the header actions at first paint, all enabled', async () => {
    await seed([message('m1', 'Pick one', { content: { quick_replies: ['Yes', 'No'] } })]);
    mount();
    await screen.findByText('Pick one');

    expect(screen.getByTestId('route-picker').getAttribute('data-route')).toBe('vibey/cached-model');
    expect(screen.getByTestId('route-picker').getAttribute('data-agents')).toBe('vibey');
    expect(screen.getByTestId('route-picker').getAttribute('data-disabled')).toBe('false');
    expect((screen.getByRole('button', { name: 'Yes' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId('show-page-launch')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'workbench.sessionActions' }).length).toBeGreaterThan(0);
    expect(composerState()).toBe('false');
    expect(mocks.api.getSessionBootstrap).toHaveBeenCalled();
    expect(screen.queryByText('common.loading')).toBeNull();
  });

  it('paints the cached messages at the reader\'s font size', async () => {
    await seed([message('m1', 'cached answer')], { fontSize: 18 });
    mount();
    const text = await screen.findByText('cached answer');
    expect(text.closest('[style*="font-size"]')?.getAttribute('style')).toContain('font-size: 18px');
  });

  it('shows the bootstrap\'s Session row over the painted one even when a newer row read is still out', async () => {
    await seed([message('m1', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');
    expect(screen.getByText('Cached title')).toBeTruthy();

    // A reconnect starts a newer row read (it never answers here) before the
    // bootstrap lands, so the bootstrap is no longer the newest row read.
    act(() => mocks.events!.onConnected());
    await act(async () => live.resolve(bootstrap([message('m1', 'cached answer')], { title: 'Live title' })));
    await screen.findByText('Live title');
    expect(screen.queryByText('Cached title')).toBeNull();
  });

  it('keeps one composer across the bootstrap, and hands it the bootstrap draft', async () => {
    mocks.api.getCachedSessionDraft.mockReturnValue('local draft');
    await seed([message('m1', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');
    const before = screen.getByTestId('composer');
    expect(before.getAttribute('data-instance')).toBe('1');
    expect(before.getAttribute('data-draft')).toBe('local draft');

    await act(async () => live.resolve(bootstrap([message('m1', 'live answer')], { draft: 'server draft' })));
    await screen.findByText('live answer');
    const after = screen.getByTestId('composer');
    expect(after).toBe(before);
    expect(after.getAttribute('data-instance')).toBe('1');
    expect(mocks.composerMounts).toBe(1);
    // Whether it may replace the box is the composer's call (untouched only).
    expect(after.getAttribute('data-draft')).toBe('server draft');
  });

  it('turns the composer and the actions read-only when the live answer says the chat is archived', async () => {
    await seed([message('m1', 'Pick one', { content: { quick_replies: ['Yes', 'No'] } })]);
    mount();
    await screen.findByText('Pick one');
    expect(composerState()).toBe('false');
    expect((screen.getByRole('button', { name: 'Yes' }) as HTMLButtonElement).disabled).toBe(false);

    await act(async () => live.resolve(bootstrap(
      [message('m1', 'Pick one', { content: { quick_replies: ['Yes', 'No'] } })],
      { session: { status: 'archived', archived_at: '2026-11-04T00:00:00Z' } },
    )));
    await waitFor(() => expect(composerState()).toBe('true'));
    expect((screen.getByRole('button', { name: 'Yes' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByTestId('route-picker')).toBeNull();
  });

  it('sends from the painted chat, and the sent row survives the live window replacing the painted ones', async () => {
    await seed([message('m1', 'cached question')]);
    mocks.apiFetch.mockResolvedValue(Response.json(
      message('d1', 'sent before the bootstrap', { type: 'user', author: 'user', state: 'claimed', delivery_id: 'd1' } as Partial<WorkbenchMessage>),
    ));
    mount();
    await screen.findByText('cached question');

    await act(async () => {
      await mocks.composer!.onSend('sent before the bootstrap');
    });
    expect(mocks.apiFetch).toHaveBeenCalledWith(
      `/api/sessions/${SESSION}/messages`,
      expect.objectContaining({ method: 'POST' }),
    );
    await screen.findByText('sent before the bootstrap');

    // The bootstrap was read before the send, and m1 is gone on the server.
    await act(async () => live.resolve(bootstrap([message('m2', 'live answer')], { draft: 'stale draft' })));
    await screen.findByText('live answer');
    expect(screen.getByText('sent before the bootstrap')).toBeTruthy();
    expect(screen.queryByText('cached question')).toBeNull();
  });

  it('sends the route change the reader picks while painted, onto the cached route', async () => {
    mocks.api.updateSession = vi.fn().mockResolvedValue(sessionRow('Cached title', { model: 'picked-model' }));
    await seed([message('m1', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');

    act(() => mocks.routePicker!.onChange({ model: 'picked-model' }));
    expect(screen.getByTestId('route-picker').getAttribute('data-route')).toBe('vibey/picked-model');
    await waitFor(() => expect(mocks.api.updateSession).toHaveBeenCalledWith(SESSION, { model: 'picked-model' }));
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

    expect(mocks.sessionActionsWritable.at(-1)).toBe(true);
    act(() => setLocalSnapshotPrincipal('remote:bob'));
    await waitFor(() => expect(screen.queryByText('alice cached row')).toBeNull());
    expect(screen.getByText('common.loading')).toBeTruthy();
    // Nothing the cache enabled keeps acting for the new reader.
    expect(mocks.sessionActionsWritable.at(-1)).toBe(false);
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

  // The cache may not hold the newest replies, so a painted chat is not a read
  // one: only the live window marks it read, on first open and when the page
  // switches to it from another chat alike.
  it.each(['opened first', 'switched to from another chat'])(
    'marks a chat read only once its bootstrap answers, never from the painted snapshot (%s)',
    async (entry) => {
      // jsdom never reports window focus, so no reading would count as presented.
      document.hasFocus = () => true;
      mocks.inbox.unreadBySession = { [SESSION]: 2 };
      const other = { ...bootstrap([]), session: sessionRow('Other chat', { id: 'ses_b' }) };
      mocks.api.getSessionBootstrap.mockImplementation((id: string) => (id === SESSION ? live.promise : Promise.resolve(other)));
      await seed([message('m1', 'cached answer')]);
      const Switch = () => {
        const navigate = useNavigate();
        return <button type="button" onClick={() => navigate(`/chat/${SESSION}`)}>switch chat</button>;
      };
      render(
        <MemoryRouter initialEntries={[entry === 'opened first' ? `/chat/${SESSION}` : '/chat/ses_b']}>
          <Switch />
          <Routes>
            <Route path="/chat/:sessionId" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>,
      );
      if (entry !== 'opened first') {
        await screen.findByTestId('composer');
        await act(async () => screen.getByText('switch chat').click());
      }

      await screen.findByText('cached answer');
      await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
      expect(mocks.inbox.markRead).not.toHaveBeenCalled();

      await act(async () => live.resolve(bootstrap([message('m1', 'live answer')])));
      await screen.findByText('live answer');
      await waitFor(() => expect(mocks.inbox.markRead).toHaveBeenCalledWith(SESSION));
      expect(mocks.inbox.markRead).toHaveBeenCalledTimes(1);
    },
  );

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
