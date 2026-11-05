/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import type { SessionRuntimeState, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ComposerProps } from './Composer';
import { readChatSnapshot } from '../../lib/chatSnapshots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';

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
      <button type="button" onClick={() => navigate('/chat/ses_b')}>open chat b</button>
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
    if (init?.method === 'POST' && url === `/api/sessions/${SESSION}/messages`) {
      const reply = deferred<Response>();
      posts.push({ body: JSON.parse(String(init.body)), reply });
      return reply.promise;
    }
    return new Promise(() => {});
  });
});

afterEach(async () => {
  cleanup();
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
    // The jump into the window looks its row up by id (jsdom has no CSS.escape).
    vi.stubGlobal('CSS', { escape: (value: string) => value });
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

  it('never stores a pending row in the chat snapshot', async () => {
    await openChat();
    send('not yet accepted');

    // The write-back settles a second after the window does.
    await act(() => new Promise((resolve) => setTimeout(resolve, 1000 + 150)));
    const stored = await readChatSnapshot(SESSION);
    expect(stored?.messages.map((row) => row.text)).toEqual(['earlier answer']);
    expect(rowsOf('not yet accepted')).toHaveLength(1);
  });

  it('keeps a row sent from the painted chat through the bootstrap, and hands it to the server row once', async () => {
    // Paint from a stored snapshot: the first test's write-back leaves one.
    await openChat();
    await act(() => new Promise((resolve) => setTimeout(resolve, 1000 + 150)));
    cleanup();
    live = deferred();
    mocks.api.getSessionBootstrap.mockReturnValue(live.promise);
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
    const chatB = bootstrap([message('b1', 'chat b answer', { session_id: 'ses_b' })]);
    mocks.api.getSessionBootstrap.mockImplementation((id: string) => Promise.resolve(
      id === SESSION ? bootstrap([message('m1', 'earlier answer')]) : { ...chatB, session: { ...chatB.session, id: 'ses_b' } },
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
