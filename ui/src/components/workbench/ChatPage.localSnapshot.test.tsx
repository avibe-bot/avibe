/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import type { InboxSession, SessionRuntimeState, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ComposerProps } from './Composer';
import { readChatSnapshot, type ChatSnapshot } from '../../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';
import { beginLocalStoreRead, recordLocalStorePage } from '../../lib/localStore';
import { writeChatViewMode } from '../../lib/chatViewMemory';
import { WorkbenchProjectsContext, type WorkbenchProjectsTree } from '../../context/WorkbenchProjectsContext';

// Opening a chat paints its last known window before the bootstrap answers
// (docs/plans/local-sync-store.md section 4.3), fully formed: the composer, its
// route picker, the actions under messages and in the header all render from
// the cache, usable, and the live answer may only narrow them. The hand-over is
// where it can go wrong: a cached row the server deleted surviving the live
// answer, a row the stream delivered or the reader sent meanwhile being thrown
// away with the cache, or the composer remounting (and losing what was typed).
// What is painted comes from the local message store, which the API layer keeps
// current (its write rules are tested there, in ApiContext.localStore.test.tsx);
// here the store is filled directly.

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
  busyHistory: [] as boolean[],
  sessionActionsWritable: [] as boolean[],
  routePicker: null as null | { onChange: (changes: Record<string, unknown>) => void },
  events: null as null | {
    onConnected: () => void;
    onMessageNew: (message: WorkbenchMessage) => void;
    onMessageUpdated: (message: WorkbenchMessage) => void;
    onTurnEnd: (data: { session_id: string }) => void;
    onTurnStart: (data: { session_id: string }) => void;
    onAuthorizationChanged: (data: { resource_kinds?: string[] }) => void;
  },
  composer: null as ComposerProps | null,
  inbox: { unreadBySession: {} as Record<string, number>, markRead: vi.fn(), inboxSessions: [] as InboxSession[] },
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
      mocks.busyHistory.push(Boolean(props.busy));
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
          data-busy={String(Boolean(props.busy))}
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

// Store a conversation as a bootstrap of it would have, and let this document
// hold it in memory, as it would once it wrote or read it.
const storeChat = async (snapshot: ChatSnapshot) => {
  setLocalSnapshotPrincipal('local');
  await recordLocalStorePage(
    beginLocalStoreRead(snapshot.session.id),
    { kind: 'tail' },
    { messages: snapshot.messages, next_before_id: snapshot.next_before_id },
    snapshot.session,
    {
      agents: snapshot.agents,
      default_agent_name: snapshot.default_agent_name,
      can_chat: snapshot.can_chat,
      chat_message_font_size: snapshot.chat_message_font_size,
    },
  );
  await readChatSnapshot(snapshot.session.id);
};

const seed = (
  messages: WorkbenchMessage[],
  { canChat = true, fontSize = null as number | null, row = {} as Partial<WorkbenchSession> } = {},
) => storeChat({
  session: sessionRow('Cached title', row),
  messages,
  next_before_id: null,
  agents: [agent],
  default_agent_name: 'vibey',
  can_chat: canChat,
  chat_message_font_size: fontSize,
});

const PROJECT = 'proj_1';

// The Session list as the sidebar or Projects page loaded it: the row that
// drives the pulsing status dot.
const listed = (agentStatus: 'idle' | 'running' | 'failed') => ({
  sessionsOf: (projectId: string) => ({
    sessions: projectId === PROJECT ? [sessionRow('Listed title', { project_id: PROJECT, agent_status: agentStatus })] : null,
    loading: false,
    loadingMore: false,
    cursor: null,
    error: false,
  }),
}) as unknown as WorkbenchProjectsTree;

const mount = (list: WorkbenchProjectsTree | null = null, entry = `/chat/${SESSION}`) => render(
  <WorkbenchProjectsContext.Provider value={list}>
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/chat/:sessionId"
          element={(
            <>
              <SwitchTo sessionId="ses_other" />
              <ChatPage />
            </>
          )}
        />
      </Routes>
    </MemoryRouter>
  </WorkbenchProjectsContext.Provider>,
);

const SwitchTo = ({ sessionId }: { sessionId: string }) => {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(`/chat/${sessionId}`)}>switch chat</button>;
};

const busy = () => screen.queryByTestId('composer')?.getAttribute('data-busy') ?? 'absent';
const dots = () => document.querySelectorAll('.vr-typing-dot').length > 0;

// A later load of the page: the reader is named again, and only the store (not
// this document's memory) holds what an earlier load wrote, so the chat paints
// when IndexedDB answers.
const asALaterLoad = () => {
  setLocalSnapshotPrincipal(null);
  setLocalSnapshotPrincipal('local');
};

const composerState = () => screen.queryByTestId('composer')?.getAttribute('data-disabled') ?? 'absent';

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
  mocks.busyHistory = [];
  mocks.sessionActionsWritable = [];
  mocks.routePicker = null;
  mocks.inbox = { unreadBySession: {}, markRead: vi.fn(async () => true), inboxSessions: [] };
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
  it('paints a chat this document holds in its first render, never a loading view', async () => {
    await seed([message('m1', 'cached question'), message('m2', 'cached answer')]);
    mount();

    // No await: the chat is on screen in the render that mounted it.
    expect(screen.getByText('cached answer')).toBeTruthy();
    expect(screen.queryByText('common.loading')).toBeNull();
    expect(composerState()).toBe('false');
  });

  it('switches to another chat this document holds in the same render', async () => {
    mocks.api.getSessionBootstrap.mockImplementation((id: string) => (id === SESSION ? live.promise : new Promise(() => {})));
    await seed([message('m1', 'cached answer')]);
    await storeChat({
      session: sessionRow('Other chat', { id: 'ses_other' }),
      messages: [message('o1', 'the other chat', { session_id: 'ses_other' })],
      next_before_id: null,
      agents: [agent],
      default_agent_name: 'vibey',
      can_chat: true,
      chat_message_font_size: null,
    });
    mount();
    expect(screen.getByText('cached answer')).toBeTruthy();

    act(() => screen.getByText('switch chat').click());
    expect(screen.getByText('the other chat')).toBeTruthy();
    expect(screen.queryByText('cached answer')).toBeNull();
    expect(screen.queryByText('common.loading')).toBeNull();
  });

  it('paints a chat an earlier load stored once the store answers', async () => {
    await seed([message('m1', 'stored answer')]);
    asALaterLoad();
    mount();
    expect(screen.queryByText('stored answer')).toBeNull();

    await screen.findByText('stored answer');
    expect(screen.queryByText('common.loading')).toBeNull();
  });

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

  it('withdraws the painted chat when the bootstrap answers 404, and shows the failure', async () => {
    await seed([message('m1', 'cached answer')]);
    mount();
    await screen.findByText('cached answer');

    await act(async () => live.reject(Object.assign(new Error('Session not found'), { status: 404 })));

    await screen.findByText('Session not found');
    expect(screen.queryByText('cached answer')).toBeNull();
  });
});

describe('ChatPage working state on entry', () => {
  const running = { project_id: PROJECT, agent_status: 'running' as const };
  const idle = { project_id: PROJECT, agent_status: 'idle' as const };
  const turnState = (foreground: 'idle' | 'running' | 'unknown') => ({ ...idleTurnState, foreground, in_flight: foreground === 'running' });

  it('opens a run the list shows with the dots and Stop at first paint, before the bootstrap', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: idle });
    mount(listed('running'));
    await screen.findByText('question');
    expect(busy()).toBe('true');
    expect(dots()).toBe(true);
    expect(mocks.busyHistory.filter((value) => !value)).toEqual([]);
  });

  it('falls back to the snapshot row when the list has no row for the session', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount();
    await screen.findByText('question');
    expect(busy()).toBe('true');
  });

  it('opens idle a session the list shows idle, whatever the snapshot row says', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('idle'));
    await screen.findByText('question');
    expect(busy()).toBe('false');
    expect(dots()).toBe(false);
  });

  it('turns back to Send when the bootstrap says the run already ended', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('running'));
    await screen.findByText('question');
    expect(busy()).toBe('true');

    await act(async () => live.resolve({ ...bootstrap([message('m1', 'question', { type: 'user', author: 'user' })]), turn_state: turnState('idle') }));
    await waitFor(() => expect(busy()).toBe('false'));
    expect(dots()).toBe(false);
  });

  it('keeps the run without a flicker when the bootstrap says it is still running', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('running'));
    await screen.findByText('question');

    await act(async () => live.resolve({ ...bootstrap([message('m1', 'question', { type: 'user', author: 'user' })]), turn_state: turnState('running') }));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalled());
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(busy()).toBe('true');
    expect(mocks.busyHistory.filter((value) => !value)).toEqual([]);
  });

  it('does not keep a seeded run the bootstrap cannot confirm', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('running'));
    await screen.findByText('question');

    await act(async () => live.resolve({ ...bootstrap([message('m1', 'question', { type: 'user', author: 'user' })]), turn_state: turnState('unknown') }));
    await waitFor(() => expect(busy()).toBe('false'));
  });

  it('never downgrades a run the stream confirmed before the paint, even when an older bootstrap says idle', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    asALaterLoad();
    mount(listed('running'));
    // The bootstrap is out (its turn-state epoch taken) and the turn starts live
    // while the snapshot is still being read: the store answers in a later task.
    await act(async () => undefined);
    expect(mocks.api.getSessionBootstrap).toHaveBeenCalled();
    expect(screen.queryByText('question')).toBeNull();
    act(() => mocks.events!.onTurnStart({ session_id: SESSION }));
    await screen.findByText('question');
    expect(busy()).toBe('true');

    // The bootstrap was read before the turn started.
    await act(async () => live.resolve({ ...bootstrap([message('m1', 'question', { type: 'user', author: 'user' })]), turn_state: turnState('idle') }));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalled());
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
    expect(busy()).toBe('true');
  });

  it('seeds nothing once the stream said the run ended, whatever the list still shows', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    asALaterLoad();
    mount(listed('running'));
    expect(screen.queryByText('question')).toBeNull();
    act(() => mocks.events!.onTurnEnd({ session_id: SESSION }));
    await screen.findByText('question');
    expect(busy()).toBe('false');
  });

  it('ends a seeded run on turn.end', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('running'));
    await screen.findByText('question');
    expect(busy()).toBe('true');

    act(() => mocks.events!.onTurnEnd({ session_id: SESSION }));
    expect(busy()).toBe('false');
  });

  it('sends the stop request from a seeded run, and settles when nothing was running', async () => {
    mocks.api.cancelSession.mockResolvedValue({ ok: false, code: 'not_in_flight' });
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('running'));
    await screen.findByText('question');

    await act(async () => {
      await mocks.composer!.onStop?.();
    });
    expect(mocks.api.cancelSession).toHaveBeenCalledWith(SESSION);
    expect(busy()).toBe('false');
  });

  it('does not carry a seeded run into the next chat', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    // The next chat is painted from its own snapshot (idle) while its bootstrap is out.
    await storeChat({
      session: { ...sessionRow('Other title', idle), id: 'ses_other' },
      messages: [message('m9', 'other question', { type: 'user', author: 'user', session_id: 'ses_other' })],
      next_before_id: null,
      agents: [agent],
      default_agent_name: 'vibey',
      can_chat: true,
      chat_message_font_size: null,
    });
    mount(listed('running'));
    await screen.findByText('question');
    expect(busy()).toBe('true');

    await act(async () => {
      screen.getByRole('button', { name: 'switch chat' }).click();
    });
    await screen.findByText('other question');
    expect(busy()).toBe('false');
    expect(dots()).toBe(false);
  });

  it('does not carry a seeded run over to the next reader', async () => {
    await seed([message('m1', 'alice question', { type: 'user', author: 'user' })], { row: running });
    mount();
    await screen.findByText('alice question');
    expect(busy()).toBe('true');

    // Another reader is named: the store holds nothing of the last one for them.
    act(() => setLocalSnapshotPrincipal('remote:bob'));
    await waitFor(() => expect(screen.queryByText('alice question')).toBeNull());
    expect(busy()).not.toBe('true');
    expect(dots()).toBe(false);
  });

  it('does not keep a seeded run after the bootstrap fails', async () => {
    await seed([message('m1', 'question', { type: 'user', author: 'user' })], { row: running });
    mount(listed('running'));
    await screen.findByText('question');
    expect(busy()).toBe('true');

    // The server is unavailable: the saved chat stays readable, but the run only
    // the list showed is not one anything confirmed.
    await act(async () => live.reject(Object.assign(new Error('unavailable'), { status: 503 })));
    await screen.findByText('chat.offline.notice');
    expect(busy()).toBe('false');
    expect(dots()).toBe(false);
    // The page recovers through an authorization refresh whose turn state is unknown.
    mocks.api.getSession.mockResolvedValue(sessionRow('Live title'));
    mocks.api.getSessionBootstrap.mockResolvedValue({ ...bootstrap([]), turn_state: turnState('unknown') });
    await act(async () => mocks.events!.onAuthorizationChanged({ resource_kinds: [] }));
    await waitFor(() => expect(busy()).toBe('false'));
  });
});

describe('ChatPage opened with no cached copy', () => {
  // A Projects tree that has loaded this Session's row.
  const tree = (row: Partial<WorkbenchSession> = {}) => ({
    projects: [{ id: PROJECT }],
    sessionsOf: (projectId: string) => ({
      sessions: projectId === PROJECT ? [sessionRow('Listed title', { project_id: PROJECT, ...row })] : null,
      loading: false,
      loadingMore: false,
      cursor: null,
      error: false,
    }),
  }) as unknown as WorkbenchProjectsTree;

  // This Session's Inbox card: its newest message is the Agent's reply.
  const inboxRow = (extra: Partial<InboxSession> = {}): InboxSession => ({
    session_id: SESSION,
    scope_id: null,
    project_id: null,
    project_name: null,
    title: 'Inbox title',
    last_activity_at: '2026-11-01T08:00:09Z',
    last_message_author: 'agent',
    replied: true,
    preview_text: 'the latest reply',
    preview_at: '2026-11-01T08:00:09Z',
    unread_count: 1,
    unread: true,
    ...extra,
  });

  const skeleton = () => document.querySelector('[role="status"][aria-busy="true"]');
  const spinner = () => document.querySelector('.animate-spin');
  const rowsOf = (text: string) => screen.queryAllByText(text).map((node) => node.closest('[data-message-id]'));

  beforeEach(() => setLocalSnapshotPrincipal('local'));

  it('opens on what its list row says, the transcript a skeleton, and hands over to the live window', async () => {
    mount(tree());

    // No await: the header and composer are the chat's own in the first render;
    // only the transcript is blocks. No spinner, no empty state.
    expect(screen.getByText('Listed title')).toBeTruthy();
    expect(composerState()).toBe('false');
    expect(skeleton()).not.toBeNull();
    expect(spinner()).toBeNull();
    expect(screen.queryByText('chat.transcriptEmpty')).toBeNull();
    expect(screen.getByTestId('route-picker')).toBeTruthy();
    const composer = screen.getByTestId('composer');

    await act(async () => live.resolve(bootstrap([message('m1', 'live question', { type: 'user', author: 'user' }), message('m2', 'live answer')])));
    await screen.findByText('live answer');
    expect(skeleton()).toBeNull();
    // The rows fade in where the skeleton was, under the same composer.
    expect(rowsOf('live answer')[0]?.className).toContain('starting:opacity-0');
    expect(screen.getByTestId('composer')).toBe(composer);
    expect(composer.getAttribute('data-instance')).toBe('1');
  });

  it('opens a chat no list knows on a skeleton of the whole page, never a spinner', async () => {
    mount();

    expect(document.querySelector('[data-chat-skeleton="page"]')).not.toBeNull();
    expect(spinner()).toBeNull();

    await act(async () => live.resolve(bootstrap([message('m2', 'live answer')])));
    await screen.findByText('live answer');
    expect(document.querySelector('[data-chat-skeleton="page"]')).toBeNull();
  });

  it('draws the newest reply its Inbox card holds at the bottom, and one copy once the live window lands', async () => {
    mocks.inbox.inboxSessions = [inboxRow()];
    mount();

    expect(screen.getByText('Inbox title')).toBeTruthy();
    expect(rowsOf('the latest reply')).toHaveLength(1);
    expect(skeleton()).not.toBeNull();
    expect(screen.getByTestId('chat-transcript').firstElementChild?.className).toContain('justify-end');
    // The card does not say which Agent the chat runs on: its picker is a block.
    expect(document.querySelector('[data-route-picker-skeleton]')).not.toBeNull();
    expect(screen.queryByTestId('route-picker')).toBeNull();

    await act(async () => live.resolve(bootstrap([message('m1', 'question', { type: 'user', author: 'user' }), message('m9', 'the latest reply')])));
    await waitFor(() => expect(skeleton()).toBeNull());
    expect(rowsOf('the latest reply')).toHaveLength(1);
    expect(rowsOf('the latest reply')[0]?.getAttribute('data-message-id')).toBe('m9');
    expect(screen.getByTestId('route-picker')).toBeTruthy();
  });

  it('draws nothing more when the cached window already holds the newest reply', async () => {
    await seed([message('m1', 'cached question'), message('m9', 'the latest reply')]);
    mocks.inbox.inboxSessions = [inboxRow()];
    mount();

    expect(screen.getByText('cached question')).toBeTruthy();
    expect(rowsOf('the latest reply')).toHaveLength(1);
    expect(skeleton()).toBeNull();
  });

  it('draws the newest reply over a cached window older than it', async () => {
    await seed([message('m1', 'cached question'), message('m2', 'cached answer')]);
    mocks.inbox.inboxSessions = [inboxRow()];
    mount();

    expect(rowsOf('the latest reply')).toHaveLength(1);
    expect(skeleton()).not.toBeNull();
    expect(screen.queryByText('cached answer')).toBeNull();
  });

  it('withdraws what the Inbox said when another reader is named', async () => {
    mocks.inbox.inboxSessions = [inboxRow()];
    mount();
    expect(rowsOf('the latest reply')).toHaveLength(1);

    act(() => setLocalSnapshotPrincipal('someone-else'));
    expect(rowsOf('the latest reply')).toHaveLength(0);
    expect(screen.queryByText('Inbox title')).toBeNull();
  });

  it('gives way to a stored window that holds what the lists knew once IndexedDB answers', async () => {
    await seed([message('m1', 'stored question'), message('m9', 'the latest reply')]);
    asALaterLoad();
    mocks.inbox.inboxSessions = [inboxRow()];
    mount(tree());
    expect(skeleton()).not.toBeNull();
    expect(screen.queryByText('stored question')).toBeNull();

    await screen.findByText('stored question');
    expect(skeleton()).toBeNull();
    expect(rowsOf('the latest reply')).toHaveLength(1);
  });
});

describe('ChatPage history this device saved', () => {
  // docs/plans/local-sync-store.md section 4.3: older pages and deep links read
  // what this device saved first; online, the server's page for the same
  // stretch replaces it. Offline, a painted chat stays readable, and where its
  // saved history ends a marker says so (decision D4).
  const at = (n: number) => `2026-11-02T08:${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}Z`;
  const hist = (n: number) => message(`h${String(n).padStart(3, '0')}`, `history ${n}`, {
    created_at: at(n), updated_at: at(n), delivered_at: at(n),
  });
  const span = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => hist(from + i));
  const chrome = { agents: [agent], default_agent_name: 'vibey', can_chat: true, chat_message_font_size: null };
  // A page as the API layer stores it; with ``chrome``, a bootstrap of it.
  const storePage = async (kind: 'tail' | 'before', messages: WorkbenchMessage[], nextBeforeId: string | null, withChrome = false) => {
    setLocalSnapshotPrincipal('local');
    await recordLocalStorePage(
      beginLocalStoreRead(SESSION),
      { kind },
      { messages, next_before_id: nextBeforeId },
      sessionRow('Saved title'),
      withChrome ? chrome : undefined,
    );
    await readChatSnapshot(SESSION);
  };
  const liveWindow = (messages: WorkbenchMessage[], nextBeforeId: string | null) => ({ ...bootstrap(messages), next_before_id: nextBeforeId });
  const scrollUp = () => act(async () => {
    fireEvent.wheel(screen.getByTestId('chat-transcript'), { deltaY: -100 });
  });
  type Page = { messages: WorkbenchMessage[]; next_before_id?: string | null; next_after_id?: string | null };
  const serve = (pages: { older?: Promise<Page>; around?: Promise<Page>; tail?: Promise<Page> }) =>
    mocks.api.listSessionMessages.mockImplementation((_id: string, params: { beforeId?: string; aroundId?: string; tail?: boolean } = {}) => {
      if (params.beforeId) return pages.older ?? new Promise(() => {});
      if (params.aroundId) return pages.around ?? new Promise(() => {});
      if (params.tail) return pages.tail ?? new Promise(() => {});
      return new Promise(() => {});
    });

  it('shows saved older rows at once, and lets the server page for the same stretch replace them', async () => {
    await storePage('tail', span(1, 70), null, true);
    const older = deferred<Page>();
    serve({ older: older.promise });
    mount();
    await act(async () => live.resolve(liveWindow(span(21, 70), 'h021')));
    await screen.findByText('history 70');

    await scrollUp();
    // From this device, while the server's page is still out.
    await screen.findByText('history 1');
    expect(mocks.api.listSessionMessages).toHaveBeenCalledWith(SESSION, { limit: 50, beforeId: 'h021' });

    // The server no longer has row 1; the rows it answered keep their place.
    await act(async () => older.resolve({ messages: span(2, 20), next_before_id: null }));
    await waitFor(() => expect(screen.queryByText('history 1')).toBeNull());
    expect(screen.getByText('history 2')).toBeTruthy();
    expect(screen.getByText('history 20')).toBeTruthy();
  });

  it('keeps a saved chat readable when the server cannot be reached, pages its saved history, and marks where it ends', async () => {
    // Saved: rows 1 to 10 from the start of history, and rows 21 to 130 from a
    // later visit; nothing says what lies between them.
    await storePage('before', span(1, 10), null);
    await storePage('tail', span(21, 130), 'h021', true);
    serve({});
    mount();
    await screen.findByText('history 130');
    await act(async () => live.reject(new TypeError('Failed to fetch')));

    // Still the chat, with the offline notice, not the failure view.
    expect(screen.getByText('chat.offline.notice')).toBeTruthy();
    expect(screen.getByText('history 130')).toBeTruthy();
    expect(screen.queryByText('Failed to fetch')).toBeNull();

    // A full page of saved rows, and more saved below it: no marker yet.
    await scrollUp();
    await screen.findByText('history 31');
    expect(screen.queryByText('chat.offline.historyGap')).toBeNull();
    // The rest of what is saved joined to it, and then the marker where it ends.
    await scrollUp();
    await screen.findByText('history 21');
    expect(await screen.findByText('chat.offline.historyGap')).toBeTruthy();
    // Rows 1 to 10 are saved too, but not joined to row 21.
    expect(screen.queryByText('history 10')).toBeNull();
    expect(mocks.api.listSessionMessages).not.toHaveBeenCalledWith(SESSION, expect.objectContaining({ beforeId: expect.any(String) }));

    // Back in reach: the bootstrap runs again, and its window replaces everything saved.
    mocks.api.getSessionBootstrap.mockResolvedValue(liveWindow(span(111, 130), 'h111'));
    await act(async () => mocks.events!.onConnected());
    await waitFor(() => expect(screen.queryByText('chat.offline.notice')).toBeNull());
    await waitFor(() => expect(screen.queryByText('history 21')).toBeNull());
    expect(screen.queryByText('history 81')).toBeNull();
    expect(screen.queryByText('chat.offline.historyGap')).toBeNull();
    expect(screen.queryByText('chat.olderLoadFailed')).toBeNull();
    expect(screen.getByText('history 111')).toBeTruthy();
  });

  it('keeps a saved chat the server fails before the device has read it', async () => {
    await storePage('tail', span(1, 30), null, true);
    // A later load: only IndexedDB holds it, and the server fails first.
    asALaterLoad();
    serve({});
    mount();
    expect(screen.queryByText('history 30')).toBeNull();
    live.reject(new TypeError('Failed to fetch'));
    await screen.findByText('history 30');
    expect(screen.getByText('chat.offline.notice')).toBeTruthy();
    expect(screen.queryByText('Failed to fetch')).toBeNull();
  });

  it('asks the server again from the marker, and its answer replaces everything saved', async () => {
    await storePage('before', span(1, 10), null);
    await storePage('tail', span(21, 70), 'h021', true);
    serve({});
    mount();
    await screen.findByText('history 70');
    await act(async () => live.reject(new TypeError('Failed to fetch')));
    await scrollUp();
    const marker = await screen.findByText('chat.offline.historyGap');

    mocks.api.getSessionBootstrap.mockResolvedValue(liveWindow(span(51, 70), 'h051'));
    await act(async () => marker.click());
    await waitFor(() => expect(screen.queryByText('chat.offline.notice')).toBeNull());
    expect(screen.queryByText('history 21')).toBeNull();
    expect(screen.queryByText('chat.offline.historyGap')).toBeNull();
    expect(screen.getByText('history 51')).toBeTruthy();
  });

  it("shows the server's version of a saved row the page returns", async () => {
    await storePage('tail', span(1, 70), null, true);
    const older = deferred<Page>();
    serve({ older: older.promise });
    mount();
    await act(async () => live.resolve(liveWindow(span(21, 70), 'h021')));
    await scrollUp();
    await screen.findByText('history 5');
    const edited = { ...hist(5), text: 'history 5, edited' };
    await act(async () => older.resolve({ messages: [...span(1, 4), edited, ...span(6, 20)], next_before_id: null }));
    await screen.findByText('history 5, edited');
    expect(screen.queryByText('history 5')).toBeNull();
  });

  it('recovers a saved chat in a historical window through its bootstrap alone when the stream reconnects', async () => {
    vi.stubGlobal('CSS', { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
    await storePage('tail', span(1, 100), null, true);
    // A tail read of the stream's own catch-up would answer at once.
    serve({ tail: Promise.resolve({ messages: span(81, 100), next_before_id: 'h081' }) });
    mount(null, `/chat/${SESSION}?msg=h030`);
    await act(async () => live.reject(new TypeError('Failed to fetch')));
    // Offline, the deep link opened in saved history.
    await screen.findByText('history 30');
    expect(screen.queryByText('history 100')).toBeNull();

    const back = deferred<ReturnType<typeof liveWindow>>();
    mocks.api.getSessionBootstrap.mockReturnValue(back.promise);
    await act(async () => mocks.events!.onConnected());
    await act(async () => back.resolve(liveWindow(span(81, 100), 'h081')));
    await screen.findByText('history 100');
    expect(screen.queryByText('chat.offline.notice')).toBeNull();
    expect(screen.queryByText('history 30')).toBeNull();
  });

  it('withdraws the saved rows it showed when the server refuses the older page', async () => {
    await storePage('tail', span(1, 70), null, true);
    const older = deferred<Page>();
    serve({ older: older.promise });
    mount();
    await act(async () => live.resolve(liveWindow(span(21, 70), 'h021')));
    await scrollUp();
    await screen.findByText('history 1');
    await act(async () => older.reject(Object.assign(new Error('Session not found'), { status: 404 })));
    await waitFor(() => expect(screen.queryByText('history 1')).toBeNull());
    expect(screen.getByText('history 21')).toBeTruthy();
  });

  it('still withdraws a saved chat the server refuses', async () => {
    await storePage('tail', span(1, 20), null, true);
    serve({});
    mount();
    await screen.findByText('history 20');
    await act(async () => live.reject(Object.assign(new Error('Session not found'), { status: 404 })));
    await screen.findByText('Session not found');
    expect(screen.queryByText('history 20')).toBeNull();
    expect(screen.queryByText('chat.offline.notice')).toBeNull();
  });

  describe('a deep link', () => {
    // The jump looks its row up by id and centers it; jsdom has neither
    // CSS.escape nor scrollIntoView.
    beforeEach(() => {
      vi.stubGlobal('CSS', { escape: (value: string) => value });
      Element.prototype.scrollIntoView = vi.fn();
    });

    const openAt = async (around: Promise<Page>, tail?: Promise<Page>) => {
      await storePage('tail', span(1, 100), null, true);
      serve({ around, tail });
      mount(null, `/chat/${SESSION}?msg=h030`);
      await act(async () => live.resolve(liveWindow(span(51, 100), 'h051')));
      // From this device, while the server's window is still out.
      await screen.findByText('history 30');
      expect(screen.getByText('history 1')).toBeTruthy();
      expect(screen.queryByText('history 100')).toBeNull();
    };

    it("shows the saved window around its message first, and the server's replaces it", async () => {
      const around = deferred<Page>();
      await openAt(around.promise);
      await act(async () => around.resolve({ messages: span(20, 40), next_before_id: 'h020', next_after_id: 'h040' }));
      await waitFor(() => expect(screen.queryByText('history 1')).toBeNull());
      expect(screen.getByText('history 30')).toBeTruthy();
    });

    it('keeps the saved window when the server fails before the device answers', async () => {
      await storePage('tail', span(1, 100), null, true);
      const failed = Promise.reject(new TypeError('Failed to fetch'));
      failed.catch(() => undefined);
      serve({ around: failed });
      mount(null, `/chat/${SESSION}?msg=h030`);
      await act(async () => live.resolve(liveWindow(span(51, 100), 'h051')));
      await screen.findByText('history 30');
      expect(screen.getByText('history 1')).toBeTruthy();
    });

    it('withdraws the saved window when the server refuses it', async () => {
      const around = deferred<Page>();
      await openAt(around.promise, Promise.resolve({ messages: span(51, 100), next_before_id: 'h051' }));
      await act(async () => around.reject(Object.assign(new Error('Forbidden'), { status: 403 })));
      await screen.findByText('history 100');
      expect(screen.queryByText('history 1')).toBeNull();
    });

    it('keeps the saved window when the server cannot be reached', async () => {
      const around = deferred<Page>();
      await openAt(around.promise);
      await act(async () => around.reject(new TypeError('Failed to fetch')));
      expect(screen.getByText('history 30')).toBeTruthy();
      expect(screen.getByText('history 1')).toBeTruthy();
    });

    it('goes back to the live tail when the server no longer has the message', async () => {
      const around = deferred<Page>();
      await openAt(around.promise, Promise.resolve({ messages: span(51, 100), next_before_id: 'h051' }));
      await act(async () => around.resolve({ messages: [] }));
      await screen.findByText('history 100');
      expect(screen.queryByText('history 30')).toBeNull();
    });
  });
});
