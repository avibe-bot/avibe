/** @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

const mocks = vi.hoisted(() => ({
  api: {
    connectWorkbenchEvents: vi.fn(),
    getCachedSessionDraft: vi.fn(),
    getSession: vi.fn(),
    getSessionBootstrap: vi.fn(),
    getTurnState: vi.fn(),
    getWorkbenchPrefs: vi.fn(),
    listSessionMessages: vi.fn(),
    listSessionQueue: vi.fn(),
    onSessionArchived: vi.fn(),
  },
  events: null as null | {
    onConnected: () => void;
    onAuthorizationChanged: (data: { resource_kinds?: string[] }) => void;
    onTurnEnd: (data: { session_id: string }) => void;
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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
  useSessionActions: () => ({
    actions: [],
    archiveDialog: null,
    requestArchive: vi.fn(),
    canArchive: false,
  }),
}));

vi.mock('./useShowPageAnnotation', () => ({
  useShowPageAnnotation: () => ({
    state: { enabled: false, mode: 'smart' },
    setIframe: vi.fn(),
    handleIframeLoad: vi.fn(),
    enable: vi.fn(),
    disable: vi.fn(),
    setMode: vi.fn(),
  }),
}));

vi.mock('./Composer', () => ({
  Composer: () => null,
}));

import { ChatPage } from './ChatPage';

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
};

const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
};

const idleTurnState = {
  foreground: 'idle',
  native_turn_started: false,
  pending_input_count: 0,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'connected',
};

const bootstrapPayload = (sessionId: string) => ({
  session: { id: sessionId },
  capabilities: { can_chat: true },
  agents: [],
  default_agent_name: null,
  config: { ui: {} },
  messages: [],
  next_before_id: null,
  turn_state: idleTurnState,
  queued: [],
  draft: { text: '' },
});

describe('ChatPage transcript hydration', () => {
  let sessionRow: Deferred<{ id: string }>;
  let bootstrap: Deferred<never>;

  beforeEach(() => {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    sessionRow = deferred();
    bootstrap = deferred();
    mocks.events = null;
    vi.clearAllMocks();

    mocks.api.connectWorkbenchEvents.mockImplementation((events) => {
      mocks.events = events;
      return () => {};
    });
    mocks.api.getCachedSessionDraft.mockReturnValue(null);
    mocks.api.getSession.mockReturnValue(sessionRow.promise);
    mocks.api.getSessionBootstrap.mockReturnValue(bootstrap.promise);
    mocks.api.getTurnState.mockResolvedValue(idleTurnState);
    mocks.api.getWorkbenchPrefs.mockResolvedValue({});
    mocks.api.listSessionMessages.mockResolvedValue({ messages: [] });
    mocks.api.listSessionQueue.mockResolvedValue([]);
    mocks.api.onSessionArchived.mockReturnValue(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('keeps the loading view when SSE Session-row recovery beats transcript bootstrap', async () => {
    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await act(async () => Promise.resolve());
    expect(mocks.events).not.toBeNull();

    act(() => mocks.events?.onConnected());
    await act(async () => sessionRow.resolve({ id: 'session-new' }));

    expect(screen.getByText('common.loading')).toBeTruthy();
    expect(screen.queryByText('chat.transcriptEmpty')).toBeNull();
  });

  it('ignores a bootstrap failure after a newer same-route refresh starts', async () => {
    const staleBootstrap = deferred<never>();
    const currentBootstrap = deferred<never>();
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap
      .mockReset()
      .mockReturnValueOnce(staleBootstrap.promise)
      .mockReturnValueOnce(currentBootstrap.promise);

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(1));
    act(() => mocks.events?.onAuthorizationChanged({ resource_kinds: [] }));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(2));

    await act(async () => staleBootstrap.reject(new Error('stale bootstrap failed')));

    expect(screen.getByText('common.loading')).toBeTruthy();
    expect(screen.queryByText('stale bootstrap failed')).toBeNull();
  });

  it('ignores a bootstrap success after a newer same-route refresh starts', async () => {
    const staleBootstrap = deferred<ReturnType<typeof bootstrapPayload>>();
    const currentBootstrap = deferred<never>();
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap
      .mockReset()
      .mockReturnValueOnce(staleBootstrap.promise)
      .mockReturnValueOnce(currentBootstrap.promise);

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(1));
    act(() => mocks.events?.onAuthorizationChanged({ resource_kinds: [] }));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(2));

    await act(async () => staleBootstrap.resolve(bootstrapPayload('session-new')));

    expect(screen.getByText('common.loading')).toBeTruthy();
    expect(screen.queryByText('chat.transcriptEmpty')).toBeNull();
  });

  it('removes a retired claimed Delivery projection when the turn settles', async () => {
    const projected = {
      id: 'delivery-claimed',
      scope_id: 'scope-1',
      session_id: 'session-new',
      platform: 'avibe',
      author: 'user',
      type: 'user',
      source: 'user',
      author_id: null,
      author_name: null,
      native_message_id: null,
      parent_native_message_id: null,
      text: 'claimed input still visible',
      content: {},
      metadata: { workbench_claimed_delivery: true },
      created_at: '2026-08-15T00:00:00Z',
      updated_at: '2026-08-15T00:00:00Z',
      delivered_at: null,
      read_at: null,
    };
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      messages: [projected],
    });

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText(projected.text)).toBeTruthy());
    act(() => mocks.events?.onTurnEnd({ session_id: 'session-new' }));

    await waitFor(() => expect(screen.queryByText(projected.text)).toBeNull());
    expect(mocks.api.listSessionMessages).toHaveBeenCalledWith('session-new', {
      limit: 50,
      tail: true,
      cache: false,
    });
  });
});
