/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import type { SessionRuntimeState, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ComposerProps } from './Composer';
import { chatChromeFromBootstrap, readChatSnapshot } from '../../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';
import { beginLocalStoreRead, recordLocalStorePage } from '../../lib/localStore';

// A sent message is on screen the moment it is sent, as the row it will be, and
// the server's row takes its place without a flicker, whichever of the POST
// answer and the stream brings it first (usePendingSends). What can go wrong: the
// message showing twice or not at all, the row remounting at the hand-over, an
// outcome that leaves a row the server never accepted, a pending row leaking into
// the stored snapshot, or one surviving the reader who sent it.

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
    convergeSessionArchived: vi.fn(),
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
    onQueueUpdated: (data: { session_id: string }) => void;
    onConnected: () => void;
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

// An ordinary Session: `ses_<handle>` ids are Bot conversations, which place a send differently.
const SESSION = 'sesa';
const BOT_SESSION = 'ses_main';

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

const agent = { id: 'agent-vibey', name: 'vibey', backend: 'vibey', model: 'm' } as VibeAgentBrief;

const bootstrap = (messages: WorkbenchMessage[]) => ({
  session: { id: SESSION, title: 'Chat', status: 'active', agent_name: 'vibey', agent_backend: 'vibey' } as WorkbenchSession,
  capabilities: { can_chat: true },
  agents: [agent],
  default_agent_name: 'vibey',
  config: { ui: {} },
  messages,
  next_after_id: null,
  next_before_id: null,
  turn_state: idleTurnState,
  queued: [],
  draft: { text: '', updated_at: null },
});

// The server's row for a send: the Delivery the POST answers with, or the
// Message the stream publishes. Both carry the send's metadata.
const serverRow = (post: Post, id = 'd1', extra: Partial<WorkbenchMessage> = {}) => message(id, post.body.text, {
  type: 'user',
  author: 'user',
  source: 'user',
  metadata: post.body.metadata,
  created_at: '2026-11-01T08:00:09Z',
  ...extra,
});

type Post = { body: { text: string; metadata: Record<string, unknown> }; reply: Deferred<Response> };
let posts: Post[];
let live: Deferred<ReturnType<typeof bootstrap>>;

// Links to the two chats, as the sidebar would offer them.
const ChatLinks = () => {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate(`/chat/${SESSION}`)}>open chat a</button>
      <button type="button" onClick={() => navigate('/chat/sesb')}>open chat b</button>
    </>
  );
};

const mount = (path = `/chat/${SESSION}`) => render(
  <MemoryRouter initialEntries={[path]}>
    <ChatLinks />
    <Routes>
      <Route path="/chat/:sessionId" element={<ChatPage />} />
    </Routes>
  </MemoryRouter>,
);

// Store a conversation as the API layer stores a bootstrap of it, and let this
// document hold it in memory, so the chat paints from it.
const storeChat = async (answer: ReturnType<typeof bootstrap>) => {
  await recordLocalStorePage(
    beginLocalStoreRead(answer.session.id),
    { kind: 'tail' },
    answer,
    answer.session,
    chatChromeFromBootstrap(answer),
  );
  await readChatSnapshot(answer.session.id);
};

const openChat = async (messages: WorkbenchMessage[] = [message('m1', 'earlier answer')]) => {
  mount();
  await act(async () => live.resolve(bootstrap(messages)));
  await screen.findByText(messages[0].text);
};

// Send through the page's composer callback without waiting for the server: the
// row must be on screen when the synchronous part of the send has run.
const send = (text: string) => {
  let result!: Promise<boolean | void>;
  act(() => {
    result = Promise.resolve(mocks.composer!.onSend(text));
  });
  return { post: posts[posts.length - 1], result };
};

const rowsOf = (text: string) => screen.queryAllByText(text).map((node) => node.closest('[data-message-id]'));
// The rows a quick-reply answer draws, apart from the agent message its buttons sit in.
const answerRows = (text: string) => rowsOf(text).filter((row) => row && row.getAttribute('data-message-id') !== 'm1');
const sendingRows = () => document.querySelectorAll('[aria-busy="true"][data-message-id]');

beforeEach(() => {
  for (const name of ['ResizeObserver', 'IntersectionObserver']) {
    vi.stubGlobal(name, class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  }
  live = deferred();
  posts = [];
  mocks.events = null;
  mocks.composer = null;
  vi.clearAllMocks();
  setLocalSnapshotPrincipal('local');
  mocks.inbox = { unreadBySession: {}, markRead: vi.fn(async () => true) };
  mocks.api.connectWorkbenchEvents.mockImplementation((events) => {
    mocks.events = events;
    return () => {};
  });
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
  mocks.apiFetch.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method === 'POST' && (url === `/api/sessions/${SESSION}/messages` || url === `/api/sessions/${BOT_SESSION}/messages`)) {
      const reply = deferred<Response>();
      posts.push({ body: JSON.parse(String(init.body)), reply });
      return reply.promise;
    }
    return new Promise(() => {});
  });
});

const originalScrollIntoView = Element.prototype.scrollIntoView;

afterEach(async () => {
  cleanup();
  Element.prototype.scrollIntoView = originalScrollIntoView;
  vi.unstubAllGlobals();
  window.localStorage.clear();
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe('ChatPage optimistic send', () => {
  it('shows the sent message at once, as sending, before the server answers', async () => {
    await openChat();

    const { post } = send('hello there');

    // No await: the row is in the render the send made.
    expect(rowsOf('hello there')).toHaveLength(1);
    expect(rowsOf('hello there')[0]?.getAttribute('aria-busy')).toBe('true');
    expect(post.body.metadata.client_message_id).toEqual(expect.any(String));
  });

  it('lets the POST answer take the row over in place when it lands first, and ignores the stream copy after', async () => {
    await openChat();
    const { post, result } = send('hello there');
    const bubble = screen.getByText('hello there');

    await act(async () => post.reply.resolve(Response.json(serverRow(post, 'd1', { projection: 'claimed_delivery' }), { status: 201 })));
    await act(async () => result);

    expect(rowsOf('hello there')).toHaveLength(1);
    expect(rowsOf('hello there')[0]?.getAttribute('data-message-id')).toBe('d1');
    expect(sendingRows()).toHaveLength(0);
    // The same element: the server row took the pending row's place, no remount.
    expect(screen.getByText('hello there')).toBe(bubble);

    act(() => mocks.events!.onMessageNew(serverRow(post)));
    expect(rowsOf('hello there')).toHaveLength(1);
  });

  it('lets the stream copy take the row over in place when it lands first, and the POST answer after changes nothing', async () => {
    await openChat();
    const { post, result } = send('hello there');
    const bubble = screen.getByText('hello there');

    act(() => mocks.events!.onMessageNew(serverRow(post)));
    expect(rowsOf('hello there')).toHaveLength(1);
    expect(rowsOf('hello there')[0]?.getAttribute('data-message-id')).toBe('d1');
    expect(sendingRows()).toHaveLength(0);
    expect(screen.getByText('hello there')).toBe(bubble);

    await act(async () => post.reply.resolve(Response.json(serverRow(post), { status: 201 })));
    await act(async () => result);
    expect(rowsOf('hello there')).toHaveLength(1);
  });

  it('withdraws the row when the send was queued, once the queue shows it', async () => {
    await openChat();
    const queueRead = deferred<{ queued: WorkbenchMessage[] }>();
    mocks.api.listSessionQueue.mockReturnValue(queueRead.promise);
    const { post, result } = send('do this next');
    let answered = false;
    void result.then(() => {
      answered = true;
    });

    await act(async () => post.reply.resolve(Response.json(
      { ...serverRow(post, 'q1', { type: 'queued', state: 'queued' }), queued: true },
      { status: 201 },
    )));
    // The composer is free again, but the queue has not been read yet: the
    // message stays where it is.
    expect(answered).toBe(true);
    await expect(result).resolves.toBeUndefined();
    expect(rowsOf('do this next')).toHaveLength(1);

    await act(async () => queueRead.resolve({ queued: [serverRow(post, 'q1', { type: 'queued', state: 'queued' })] }));
    // Now it is in the queue strip only, not in the transcript.
    await waitFor(() => expect(sendingRows()).toHaveLength(0));
    expect(screen.queryAllByText('do this next')).toHaveLength(1);
    expect(rowsOf('do this next')[0]).toBeNull();
  });

  it('keeps a queued row while the queue cannot be read, until a later read shows it', async () => {
    await openChat();
    mocks.api.listSessionQueue.mockRejectedValueOnce(new Error('offline'));
    const { post, result } = send('do this next');
    const queued = serverRow(post, 'q1', { type: 'queued', state: 'queued' });

    await act(async () => post.reply.resolve(Response.json({ ...queued, queued: true }, { status: 201 })));
    await act(async () => result);
    // The queue read failed: the strip does not have it, so the row stays.
    expect(rowsOf('do this next')).toHaveLength(1);

    mocks.api.listSessionQueue.mockResolvedValue({ queued: [queued] });
    await act(async () => mocks.events!.onQueueUpdated({ session_id: SESSION }));
    await waitFor(() => expect(sendingRows()).toHaveLength(0));
    expect(screen.queryAllByText('do this next')).toHaveLength(1);
    expect(rowsOf('do this next')[0]).toBeNull();
  });

  it('lets the bootstrap of a chat opened again show where a queued send went', async () => {
    await openChat();
    mocks.api.listSessionQueue.mockRejectedValueOnce(new Error('offline'));
    const { post, result } = send('do this next');
    const queued = serverRow(post, 'q1', { type: 'queued', state: 'queued' });
    await act(async () => post.reply.resolve(Response.json({ ...queued, queued: true }, { status: 201 })));
    await act(async () => result);
    expect(rowsOf('do this next')).toHaveLength(1);

    const chatB = bootstrap([message('b1', 'chat b answer', { session_id: 'sesb' })]);
    mocks.api.getSessionBootstrap.mockImplementation((id: string) => Promise.resolve(
      id === SESSION
        ? { ...bootstrap([message('m1', 'earlier answer')]), queued: [queued] }
        : { ...chatB, session: { ...chatB.session, id: 'sesb' } },
    ));
    act(() => screen.getByRole('button', { name: 'open chat b' }).click());
    await screen.findByText('chat b answer');
    act(() => screen.getByRole('button', { name: 'open chat a' }).click());
    await screen.findByText('earlier answer');

    await waitFor(() => expect(sendingRows()).toHaveLength(0));
    expect(screen.queryAllByText('do this next')).toHaveLength(1);
    expect(rowsOf('do this next')[0]).toBeNull();
  });

  it('does not take a queue read begun before the send was queued as showing it', async () => {
    await openChat();
    const earlier = deferred<{ queued: WorkbenchMessage[] }>();
    const later = deferred<{ queued: WorkbenchMessage[] }>();
    mocks.api.listSessionQueue.mockReturnValueOnce(earlier.promise).mockReturnValueOnce(later.promise);
    const { post, result } = send('do this next');
    const queued = serverRow(post, 'q1', { type: 'queued', state: 'queued' });
    // Another queue change is being read while the POST is out.
    act(() => mocks.events!.onQueueUpdated({ session_id: SESSION }));

    await act(async () => post.reply.resolve(Response.json({ ...queued, queued: true }, { status: 201 })));
    await act(async () => result);
    await act(async () => earlier.resolve({ queued: [] }));
    expect(rowsOf('do this next')).toHaveLength(1);

    await act(async () => later.resolve({ queued: [queued] }));
    await waitFor(() => expect(sendingRows()).toHaveLength(0));
    expect(screen.queryAllByText('do this next')).toHaveLength(1);
  });

  it('keeps a queued row a turn took before the queue was read, until the transcript shows it', async () => {
    await openChat();
    const queueRead = deferred<{ queued: WorkbenchMessage[] }>();
    const tail = deferred<unknown>();
    mocks.api.listSessionQueue.mockReturnValue(queueRead.promise);
    mocks.api.listSessionMessages.mockReturnValue(tail.promise);
    const { post, result } = send('do this next');

    await act(async () => post.reply.resolve(Response.json(
      { ...serverRow(post, 'q1', { type: 'queued', state: 'queued' }), queued: true },
      { status: 201 },
    )));
    await act(async () => result);
    // The turn claimed it already: the queue no longer lists it, and its row is
    // not in the transcript yet.
    await act(async () => queueRead.resolve({ queued: [] }));
    expect(rowsOf('do this next')).toHaveLength(1);
    expect(mocks.api.listSessionMessages).toHaveBeenCalledWith(SESSION, { limit: 50, tail: true, cache: false });

    // The transcript shows it, merged with the input the turn took with it.
    const claimed = message('p1', 'the earlier input, and do this next', {
      type: 'user', author: 'user', source: 'user', projection: 'claimed_delivery', state: 'claimed',
    });
    await act(async () => tail.resolve({ messages: [message('m1', 'earlier answer'), claimed], next_before_id: null }));
    await waitFor(() => expect(sendingRows()).toHaveLength(0));
    expect(rowsOf('do this next')).toHaveLength(0);
    expect(document.querySelector('[data-message-id="p1"]')).not.toBeNull();
  });

  it('does not take a transcript read begun before the queue stopped listing the send as showing it', async () => {
    await openChat();
    const queueAfterSend = deferred<{ queued: WorkbenchMessage[] }>();
    const tailBefore = deferred<unknown>();
    const tailAfter = deferred<unknown>();
    mocks.api.listSessionQueue.mockReturnValueOnce(queueAfterSend.promise).mockReturnValue(new Promise(() => {}));
    mocks.api.listSessionMessages.mockReturnValueOnce(tailBefore.promise).mockReturnValueOnce(tailAfter.promise);
    const { post, result } = send('do this next');
    await act(async () => post.reply.resolve(Response.json(
      { ...serverRow(post, 'q1', { type: 'queued', state: 'queued' }), queued: true },
      { status: 201 },
    )));
    await act(async () => result);
    // A reconnect starts its transcript read before the queue read lands.
    act(() => mocks.events!.onConnected());
    await act(async () => queueAfterSend.resolve({ queued: [] }));

    await act(async () => tailBefore.resolve({ messages: [message('m1', 'earlier answer')], next_before_id: null }));
    expect(rowsOf('do this next')).toHaveLength(1);

    const claimed = message('p1', 'do this next, as the turn took it', {
      type: 'user', author: 'user', source: 'user', projection: 'claimed_delivery', state: 'claimed',
    });
    await act(async () => tailAfter.resolve({ messages: [message('m1', 'earlier answer'), claimed], next_before_id: null }));
    await waitFor(() => expect(sendingRows()).toHaveLength(0));
  });

  it('withdraws the row and shows the archived error when the chat was archived', async () => {
    await openChat();
    const { post, result } = send('one more thing');

    await act(async () => post.reply.resolve(Response.json(
      { error: 'archived', code: 'session_archived' },
      { status: 409 },
    )));

    await expect(result).resolves.toBeUndefined();
    expect(rowsOf('one more thing')).toHaveLength(0);
    expect(screen.getByText('chat.archived.sendBlocked')).toBeTruthy();
    expect(mocks.api.convergeSessionArchived).toHaveBeenCalledWith(SESSION);
  });

  it('withdraws the row, shows the error, and gives the draft back when the server refuses the send', async () => {
    await openChat();
    const { post, result } = send('please refuse me');

    await act(async () => post.reply.resolve(Response.json({ error: { code: 'agent_unavailable', message: 'Agent is unavailable' } }, { status: 500 })));

    await expect(result).resolves.toBe(false);
    expect(rowsOf('please refuse me')).toHaveLength(0);
    expect(screen.getByText('errors.agent_unavailable')).toBeTruthy();
    expect(mocks.api.recoverSessionDraftAfterRejectedSend).toHaveBeenCalledWith(SESSION);
  });

  it('withdraws the row, shows the error, and gives the draft back when the network fails', async () => {
    await openChat();
    const { post, result } = send('lost in transit');

    await act(async () => post.reply.reject(new Error('Failed to fetch')));

    await expect(result).resolves.toBe(false);
    expect(rowsOf('lost in transit')).toHaveLength(0);
    expect(screen.getByText('Failed to fetch')).toBeTruthy();
    expect(mocks.api.recoverSessionDraftAfterRejectedSend).toHaveBeenCalledWith(SESSION);
  });

  it('locks a quick reply at once and gives it back when the server already had the answer', async () => {
    await openChat([message('m1', 'Pick one', { content: { quick_replies: ['Yes', 'No'] } })]);

    act(() => screen.getByRole('button', { name: 'Yes' }).click());
    // The answer is on screen and the group is inert, before the server answers.
    expect(answerRows('Yes')).toHaveLength(1);
    expect((screen.getByRole('button', { name: 'No' }) as HTMLButtonElement).disabled).toBe(true);
    const [post] = posts;
    expect(post.body.metadata).toEqual({ quick_reply_for: 'm1', client_message_id: expect.any(String) });

    await act(async () => post.reply.resolve(Response.json({ already_answered: true })));

    await waitFor(() => expect((screen.getByRole('button', { name: 'No' }) as HTMLButtonElement).disabled).toBe(false));
    expect(answerRows('Yes')).toHaveLength(0);
  });

  it('withdraws a refused quick reply and gives the buttons back, without touching the draft', async () => {
    await openChat([message('m1', 'Pick one', { content: { quick_replies: ['Yes', 'No'] } })]);

    act(() => screen.getByRole('button', { name: 'No' }).click());
    expect(answerRows('No')).toHaveLength(1);

    await act(async () => posts[0].reply.resolve(Response.json({ error: { code: 'agent_unavailable', message: 'Agent is unavailable' } }, { status: 500 })));

    await waitFor(() => expect((screen.getByRole('button', { name: 'Yes' }) as HTMLButtonElement).disabled).toBe(false));
    expect(answerRows('No')).toHaveLength(0);
    expect(screen.getByText('errors.agent_unavailable')).toBeTruthy();
    expect(mocks.api.recoverSessionDraftAfterRejectedSend).not.toHaveBeenCalled();
  });

  it('brings a send made while scrolled up into view', async () => {
    await openChat();
    // Opening the chat lands at the bottom on a later frame; let it, then scroll up.
    const frames = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await frames();
    const scroller = screen.getByTestId('chat-transcript');
    Object.defineProperties(scroller, {
      scrollHeight: { value: 10_000, configurable: true },
      clientHeight: { value: 800, configurable: true },
      scrollTop: { value: 4_000, writable: true, configurable: true },
    });
    fireEvent.scroll(scroller);
    await frames();
    expect(scroller.scrollTop).toBe(4_000);

    send('look at this');

    await waitFor(() => expect(scroller.scrollTop).toBe(10_000));
  });

  it('moves a historical window to the live tail at once, and lands the sent row under it', async () => {
    // The jump into the window looks its row up by id and centers it; jsdom has
    // neither CSS.escape nor scrollIntoView.
    vi.stubGlobal('CSS', { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
    const around = deferred<unknown>();
    const tail = deferred<unknown>();
    mocks.api.listSessionMessages.mockImplementation((_id: string, options: { aroundId?: string; tail?: boolean }) => (
      options.aroundId ? around.promise : options.tail ? tail.promise : new Promise(() => {})
    ));
    mount(`/chat/${SESSION}?msg=m1`);
    await act(async () => live.resolve(bootstrap([message('m5', 'latest answer')])));
    await act(async () => around.resolve({ messages: [message('m1', 'old context')], next_after_id: 'm2', next_before_id: null }));
    await screen.findByText('old context');

    const { post, result } = send('a new question');
    expect(rowsOf('a new question')).toHaveLength(1);
    // The live tail is asked for now, not after the server answers.
    expect(mocks.api.listSessionMessages).toHaveBeenCalledWith(SESSION, { limit: 50, tail: true, cache: false });

    // The POST answers first; the tail was read before the message existed.
    await act(async () => post.reply.resolve(Response.json(serverRow(post), { status: 201 })));
    await act(async () => tail.resolve({ messages: [message('m5', 'latest answer')], next_before_id: null }));
    await act(async () => result);

    expect(screen.queryByText('old context')).toBeNull();
    expect(screen.getByText('latest answer')).toBeTruthy();
    expect(rowsOf('a new question')).toHaveLength(1);
    expect(rowsOf('a new question')[0]?.getAttribute('data-message-id')).toBe('d1');
  });

  it('keeps a row sent from the painted chat through the bootstrap, and hands it to the server row once', async () => {
    // Paint from the stored conversation.
    await storeChat(bootstrap([message('m1', 'earlier answer')]));
    mount();
    await screen.findByText('earlier answer');

    const { post, result } = send('sent while painted');
    // The bootstrap was read before the send.
    await act(async () => live.resolve(bootstrap([message('m1', 'earlier answer'), message('m2', 'live answer')])));
    await screen.findByText('live answer');
    expect(rowsOf('sent while painted')).toHaveLength(1);

    await act(async () => post.reply.resolve(Response.json(serverRow(post), { status: 201 })));
    await act(async () => result);
    expect(rowsOf('sent while painted')).toHaveLength(1);
    expect(sendingRows()).toHaveLength(0);
  });

  it('keeps a pending row with the chat it was sent in', async () => {
    await openChat();
    send('for chat a');
    const chatB = bootstrap([message('b1', 'chat b answer', { session_id: 'sesb' })]);
    mocks.api.getSessionBootstrap.mockImplementation((id: string) => Promise.resolve(
      id === SESSION ? bootstrap([message('m1', 'earlier answer')]) : { ...chatB, session: { ...chatB.session, id: 'sesb' } },
    ));

    act(() => screen.getByRole('button', { name: 'open chat b' }).click());
    await screen.findByText('chat b answer');
    expect(rowsOf('for chat a')).toHaveLength(0);

    // Back while the POST is still out: the row is still on its way.
    act(() => screen.getByRole('button', { name: 'open chat a' }).click());
    await screen.findByText('earlier answer');
    expect(rowsOf('for chat a')).toHaveLength(1);
  });

  it('drops a pending row when another reader is named', async () => {
    await openChat();
    send('from the last reader');
    expect(rowsOf('from the last reader')).toHaveLength(1);

    act(() => setLocalSnapshotPrincipal('someone-else'));
    expect(rowsOf('from the last reader')).toHaveLength(0);
  });

  it('keeps a row sent before the reader was named for the reader named next', async () => {
    setLocalSnapshotPrincipal(null);
    await openChat();
    send('sent on a cold start');

    act(() => setLocalSnapshotPrincipal('local'));
    expect(rowsOf('sent on a cold start')).toHaveLength(1);
  });

  it('drops a pending row when the store is cleared', async () => {
    await openChat();
    send('before sign-out');

    await act(async () => clearLocalSnapshots());
    expect(rowsOf('before sign-out')).toHaveLength(0);
  });
});

describe('ChatPage send while the agent is busy', () => {
  const running: SessionRuntimeState = { ...idleTurnState, in_flight: true, foreground: 'running', native_turn_started: true };

  const openBusyChat = async (messages: WorkbenchMessage[] = [message('m1', 'earlier answer')]) => {
    mocks.api.getTurnState.mockResolvedValue(running);
    mount();
    await act(async () => live.resolve({ ...bootstrap(messages), turn_state: running }));
    await screen.findByText(messages[0].text);
  };

  const queueRowsOf = (text: string) => screen.queryAllByText(text)
    .map((node) => node.closest('[data-queue-row]'))
    .filter((row): row is HTMLElement => row !== null);
  const transcriptRowsOf = (text: string) => rowsOf(text).filter((row) => row !== null);
  const queuedAnswer = (post: Post) => ({ ...serverRow(post, 'q1', { type: 'queued', state: 'queued' }), queued: true });

  it('puts a message sent while a turn runs straight into the queue, as sending', async () => {
    await openBusyChat();

    send('do this next');

    // No await: the row is in the queue strip in the render the send made, and
    // never in the transcript.
    expect(queueRowsOf('do this next')).toHaveLength(1);
    expect(queueRowsOf('do this next')[0].getAttribute('aria-busy')).toBe('true');
    expect(transcriptRowsOf('do this next')).toHaveLength(0);
    // Nothing of it is the server's yet: no removing it, no sending it now.
    const remove = screen.getByRole('button', { name: /^(chat\.queue\.remove|bots\.conversation\.cancelSend)$/ });
    expect((remove as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'chat.queue.sendNow' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('replaces it in place with the server queue row when the POST answers queued', async () => {
    await openBusyChat();
    const queueRead = deferred<{ queued: WorkbenchMessage[] }>();
    mocks.api.listSessionQueue.mockReturnValue(queueRead.promise);
    const { post, result } = send('do this next');
    const row = queueRowsOf('do this next')[0];

    await act(async () => post.reply.resolve(Response.json(queuedAnswer(post), { status: 201 })));
    await act(async () => result);
    expect(queueRowsOf('do this next')).toEqual([row]);

    await act(async () => queueRead.resolve({ queued: [serverRow(post, 'q1', { type: 'queued', state: 'queued' })] }));
    // The same element, now the server's row: no remount, no reorder, one row.
    expect(queueRowsOf('do this next')).toEqual([row]);
    expect(row.getAttribute('aria-busy')).toBe('false');
    expect(transcriptRowsOf('do this next')).toHaveLength(0);
  });

  it('shows one row when a queue read lists the send before the POST answers', async () => {
    await openBusyChat();
    const queueRead = deferred<{ queued: WorkbenchMessage[] }>();
    mocks.api.listSessionQueue.mockReturnValue(queueRead.promise);
    const { post } = send('do this next');

    // queue.updated reached this tab before the POST answer did.
    act(() => mocks.events!.onQueueUpdated({ session_id: SESSION }));
    await act(async () => queueRead.resolve({ queued: [serverRow(post, 'q1', { type: 'queued', state: 'queued' })] }));

    expect(queueRowsOf('do this next')).toHaveLength(1);
    expect(queueRowsOf('do this next')[0].getAttribute('aria-busy')).toBe('false');
  });

  it('moves it into the transcript when the server started a turn after all, answer first', async () => {
    await openBusyChat();
    const { post, result } = send('do this next');

    // The turn ended as the message left: the server started a new one with it.
    await act(async () => post.reply.resolve(Response.json(serverRow(post), { status: 201 })));
    await act(async () => result);

    expect(queueRowsOf('do this next')).toHaveLength(0);
    expect(transcriptRowsOf('do this next')).toHaveLength(1);
    expect(transcriptRowsOf('do this next')[0].getAttribute('data-message-id')).toBe('d1');
  });

  it('moves it into the transcript when the server started a turn after all, stream first', async () => {
    await openBusyChat();
    const { post } = send('do this next');

    act(() => mocks.events!.onMessageNew(serverRow(post)));

    // Out of the queue and into the transcript in the same render: one row.
    expect(queueRowsOf('do this next')).toHaveLength(0);
    expect(transcriptRowsOf('do this next')).toHaveLength(1);
  });

  it('withdraws it from the queue and gives the draft back when the server refuses it', async () => {
    await openBusyChat();
    const { post, result } = send('please refuse me');

    await act(async () => post.reply.resolve(Response.json(
      { error: { code: 'agent_unavailable', message: 'Agent is unavailable' } },
      { status: 500 },
    )));

    await expect(result).resolves.toBe(false);
    expect(queueRowsOf('please refuse me')).toHaveLength(0);
    expect(transcriptRowsOf('please refuse me')).toHaveLength(0);
    expect(screen.getByText('errors.agent_unavailable')).toBeTruthy();
    expect(mocks.api.recoverSessionDraftAfterRejectedSend).toHaveBeenCalledWith(SESSION);
  });

  it('follows the transcript to a message drawn in the queue that started a turn after all', async () => {
    await openBusyChat();
    const frames = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await frames();
    const scroller = screen.getByTestId('chat-transcript');
    Object.defineProperties(scroller, {
      scrollHeight: { value: 10_000, configurable: true },
      clientHeight: { value: 800, configurable: true },
      scrollTop: { value: 4_000, writable: true, configurable: true },
    });
    fireEvent.scroll(scroller);
    const { post, result } = send('do this next');
    await frames();
    // Into the queue: the transcript stays where the reader is.
    expect(scroller.scrollTop).toBe(4_000);

    await act(async () => post.reply.resolve(Response.json(serverRow(post), { status: 201 })));
    await act(async () => result);
    await waitFor(() => expect(scroller.scrollTop).toBe(10_000));
  });

  it('leaves a historical window where it is when the message goes to the queue', async () => {
    vi.stubGlobal('CSS', { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
    mocks.api.getTurnState.mockResolvedValue(running);
    const around = deferred<unknown>();
    mocks.api.listSessionMessages.mockImplementation((_id: string, options: { aroundId?: string }) => (
      options.aroundId ? around.promise : new Promise(() => {})
    ));
    mount(`/chat/${SESSION}?msg=m1`);
    await act(async () => live.resolve({ ...bootstrap([message('m5', 'latest answer')]), turn_state: running }));
    await act(async () => around.resolve({ messages: [message('m1', 'old context')], next_after_id: 'm2', next_before_id: null }));
    await screen.findByText('old context');

    send('do this next');

    expect(queueRowsOf('do this next')).toHaveLength(1);
    expect(mocks.api.listSessionMessages).not.toHaveBeenCalledWith(SESSION, { limit: 50, tail: true, cache: false });
    expect(screen.getByText('old context')).toBeTruthy();
  });

  it('sends to the transcript as before when no turn runs', async () => {
    await openChat();

    send('hello there');

    expect(transcriptRowsOf('hello there')).toHaveLength(1);
    expect(queueRowsOf('hello there')).toHaveLength(0);
  });

  it('keeps a quick reply under the question it answers, even while a turn runs', async () => {
    await openBusyChat([message('m1', 'Pick one', { content: { quick_replies: ['Yes', 'No'] } })]);

    act(() => screen.getByRole('button', { name: 'Yes' }).click());

    expect(answerRows('Yes')).toHaveLength(1);
    expect(queueRowsOf('Yes')).toHaveLength(0);
  });

  // Owner, 2026-11-12: a message must appear once, in its final place. While only the list's seed
  // says a turn runs, the page cannot know where the send goes, so the row waits beside the composer
  // and the server's answer draws it, once, where it went.
  const seededChat = async () => {
    const snapshotSession = { id: SESSION, title: 'Chat', status: 'active', agent_name: 'vibey', agent_backend: 'vibey', agent_status: 'running' } as WorkbenchSession;
    await storeChat({ ...bootstrap([message('m1', 'cached answer')]), session: snapshotSession });
    mount();
    await screen.findByText('cached answer');
  };
  const sendingLineOf = (text: string) => screen.queryAllByText(text).filter((node) => node.closest('[role="status"]'));

  it('holds a send made before the turn state is known beside the composer, then queues it once', async () => {
    await seededChat();
    const queueRead = deferred<{ queued: WorkbenchMessage[] }>();
    mocks.api.listSessionQueue.mockReturnValue(queueRead.promise);
    const { post, result } = send('sent while painted');

    expect(sendingLineOf('sent while painted')).toHaveLength(1);
    expect(transcriptRowsOf('sent while painted')).toHaveLength(0);
    expect(queueRowsOf('sent while painted')).toHaveLength(0);

    await act(async () => post.reply.resolve(Response.json(queuedAnswer(post), { status: 201 })));
    await act(async () => result);
    expect(sendingLineOf('sent while painted')).toHaveLength(0);
    expect(queueRowsOf('sent while painted')).toHaveLength(1);
    expect(transcriptRowsOf('sent while painted')).toHaveLength(0);
  });

  it('holds a send made before the turn state is known, then draws it once in the transcript when it started a turn', async () => {
    await seededChat();
    const { post, result } = send('sent while painted');
    expect(transcriptRowsOf('sent while painted')).toHaveLength(0);

    await act(async () => post.reply.resolve(Response.json(serverRow(post), { status: 201 })));
    await act(async () => result);
    expect(sendingLineOf('sent while painted')).toHaveLength(0);
    expect(queueRowsOf('sent while painted')).toHaveLength(0);
    expect(transcriptRowsOf('sent while painted')).toHaveLength(1);
  });
});

describe("ChatPage send in a Bot's conversation", () => {
  const running: SessionRuntimeState = { ...idleTurnState, in_flight: true, foreground: 'running', native_turn_started: true };
  const openBusyBot = async () => {
    mocks.api.getTurnState.mockResolvedValue(running);
    mount(`/chat/${BOT_SESSION}`);
    const answer = bootstrap([message('m1', 'earlier answer', { session_id: BOT_SESSION })]);
    await act(async () => live.resolve({ ...answer, session: { ...answer.session, id: BOT_SESSION }, turn_state: running }));
    await screen.findByText('earlier answer');
  };
  const queueRowsOf = (text: string) => screen.queryAllByText(text)
    .map((node) => node.closest('[data-queue-row]'))
    .filter((row): row is HTMLElement => row !== null);
  const transcriptRowsOf = (text: string) => rowsOf(text).filter((row) => row !== null);

  // The user's message is P1 there: a working Bot takes it into its Turn. It goes to the transcript
  // at once, and the queue row the server holds while steering it is drawn there too, not in the strip.
  it.each(['answer first', 'queue read first'] as const)(
    'draws a message sent while the Bot works in the transcript, through the steer, never in the queue strip (%s)',
    async (order) => {
      await openBusyBot();
      const queueRead = deferred<{ queued: WorkbenchMessage[] }>();
      mocks.api.listSessionQueue.mockReturnValue(queueRead.promise);
      const { post, result } = send('also check the tests');
      const row = transcriptRowsOf('also check the tests')[0];
      expect(row).toBeTruthy();
      expect(queueRowsOf('also check the tests')).toHaveLength(0);

      // The server answers a steer as queued (core/internal_server.py), in a steering state.
      const steering = serverRow(post, 'q1', { type: 'queued', state: 'pending_steer', session_id: BOT_SESSION });
      const answer = async () => {
        await act(async () => post.reply.resolve(Response.json({ ...steering, queued: true }, { status: 201 })));
        await act(async () => result);
      };
      const read = async () => {
        await act(async () => queueRead.resolve({ queued: [steering] }));
        await act(async () => mocks.events!.onQueueUpdated({ session_id: BOT_SESSION }));
      };
      mocks.api.listSessionQueue.mockResolvedValue({ queued: [steering] });
      if (order === 'answer first') { await answer(); await read(); } else { await read(); await answer(); }
      expect(transcriptRowsOf('also check the tests')).toEqual([row]);
      expect(row.getAttribute('aria-busy')).toBe('true');
      expect(queueRowsOf('also check the tests')).toHaveLength(0);

      // The Turn took it: its Message keeps the Delivery's id.
      act(() => mocks.events!.onMessageNew(serverRow(post, 'q1', { session_id: BOT_SESSION })));
      mocks.api.listSessionQueue.mockResolvedValue({ queued: [] });
      await act(async () => mocks.events!.onQueueUpdated({ session_id: BOT_SESSION }));
      expect(transcriptRowsOf('also check the tests')).toHaveLength(1);
      expect(queueRowsOf('also check the tests')).toHaveLength(0);
    },
  );

  it('keeps a steer whose receipt is unknown in the queue strip, which says it could not be confirmed', async () => {
    await openBusyBot();
    const { post } = send('also check the tests');
    const reconciling = serverRow(post, 'q1', { type: 'queued', state: 'reconciling_steer', session_id: BOT_SESSION });
    mocks.api.listSessionQueue.mockResolvedValue({ queued: [reconciling] });
    await act(async () => mocks.events!.onQueueUpdated({ session_id: BOT_SESSION }));
    expect(queueRowsOf('also check the tests')).toHaveLength(1);
    expect(transcriptRowsOf('also check the tests')).toHaveLength(0);
    expect(screen.getByText('chat.queue.sendReconciling')).toBeTruthy();
  });

  it('keeps Send now off while the queue head is a steer drawn in the transcript', async () => {
    await openBusyBot();
    const { post } = send('also check the tests');
    const steering = serverRow(post, 'q1', { type: 'queued', state: 'steering', session_id: BOT_SESSION });
    const harness = message('q2', 'a later note', { type: 'queued', state: 'queued', author: 'user', source: 'user', session_id: BOT_SESSION });
    mocks.api.listSessionQueue.mockResolvedValue({ queued: [steering, harness] });
    await act(async () => mocks.events!.onQueueUpdated({ session_id: BOT_SESSION }));
    expect(queueRowsOf('a later note')).toHaveLength(1);
    // The strip reads the real head: it is fenced, so there is nothing Send now could do.
    const sendNow = screen.getByRole('button', { name: /chat\.queue\.(sendNow|sendingNow)/ }) as HTMLButtonElement;
    expect(sendNow.disabled).toBe(true);
  });

});
