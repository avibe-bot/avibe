/** @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import type { TurnActivityGroupWire } from '../../lib/agentActivity';

const mocks = vi.hoisted(() => ({
  api: {
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
    mutateConfig: vi.fn(),
    waitForAgentActivityConfigMutations: vi.fn(),
    onSessionArchived: vi.fn(),
  },
  events: null as null | {
    onConnected: () => void;
    onAuthorizationChanged: (data: {
      resource_kinds?: string[];
      instance_authorization_revision?: number;
    }) => void;
    onMessageNew: (message: ReturnType<typeof projectedMessage>) => void;
    onTurnStart: (data: { session_id: string }) => void;
    onTurnEnd: (data: { session_id: string }) => void;
  },
  authorizationCapabilities: {
    can_chat: true,
    can_manage_instance: false,
    can_use_show_pages: false,
    can_use_system: false,
    can_use_vault_secrets: false,
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
    capabilities: mocks.authorizationCapabilities,
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
    handleShortcutKeyDown: vi.fn(),
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

const projectedMessage = (id: string, text: string) => ({
  id,
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
  projection: 'claimed_delivery' as const,
  text,
  content: {},
  metadata: {},
  created_at: '2026-08-15T00:00:00Z',
  updated_at: '2026-08-15T00:00:00Z',
  delivered_at: '2026-08-15T00:00:01Z',
  read_at: null,
});

const SessionSwitcher = ({ sessionId = 'session-running', label = 'switch chat' }: {
  sessionId?: string;
  label?: string;
}) => {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(`/chat/${sessionId}`)}>{label}</button>;
};

describe('ChatPage transcript hydration', () => {
  let sessionRow: Deferred<{ id: string }>;
  let bootstrap: Deferred<never>;

  beforeEach(() => {
    // The transcript drives its scroll anchor and its older-page trigger from
    // these two; jsdom implements neither, and nothing here depends on what they
    // would report.
    for (const name of ['ResizeObserver', 'IntersectionObserver']) {
      vi.stubGlobal(
        name,
        class {
          observe() {}
          unobserve() {}
          disconnect() {}
        },
      );
    }
    sessionRow = deferred();
    bootstrap = deferred();
    mocks.events = null;
    vi.clearAllMocks();
    Object.assign(mocks.authorizationCapabilities, {
      can_chat: true,
      can_manage_instance: false,
      can_use_show_pages: false,
      can_use_system: false,
      can_use_vault_secrets: false,
    });

    mocks.api.connectWorkbenchEvents.mockImplementation((events) => {
      mocks.events = events;
      return () => {};
    });
    mocks.api.getCachedSessionDraft.mockReturnValue(null);
    mocks.api.getSession.mockReturnValue(sessionRow.promise);
    mocks.api.getSessionActivity.mockResolvedValue({ groups: [] });
    mocks.api.getSessionActivityGroup.mockResolvedValue({});
    mocks.api.getSessionBootstrap.mockReturnValue(bootstrap.promise);
    mocks.api.getTurnState.mockResolvedValue(idleTurnState);
    mocks.api.getWorkbenchPrefs.mockResolvedValue({});
    mocks.api.listSessionMessages.mockResolvedValue({ messages: [] });
    mocks.api.listSessionQueue.mockResolvedValue([]);
    mocks.api.mutateConfig.mockResolvedValue({ ui: {} });
    mocks.api.waitForAgentActivityConfigMutations.mockResolvedValue(undefined);
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

  it('removes a retired claimed Delivery projection during reconnect recovery', async () => {
    const projected = projectedMessage('delivery-claimed', 'claimed input still visible');
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
    act(() => mocks.events?.onConnected());

    await waitFor(() => expect(screen.queryByText(projected.text)).toBeNull());
    expect(mocks.api.listSessionMessages).toHaveBeenCalledWith('session-new', {
      limit: 50,
      tail: true,
      cache: false,
    });
  });

  it('does not let an older recovery resurrect a projection after settlement', async () => {
    const projected = projectedMessage('delivery-racing-recovery', 'stale projected input');
    const staleRecovery = deferred<{ messages: typeof projected[] }>();
    const settledRecovery = deferred<{ messages: never[] }>();
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      messages: [projected],
    });
    mocks.api.listSessionMessages
      .mockReset()
      .mockReturnValueOnce(staleRecovery.promise)
      .mockReturnValueOnce(settledRecovery.promise);

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText(projected.text)).toBeTruthy());
    act(() => mocks.events?.onConnected());
    await waitFor(() => expect(mocks.api.listSessionMessages).toHaveBeenCalledTimes(1));
    act(() => mocks.events?.onTurnEnd({ session_id: 'session-new' }));
    await waitFor(() => expect(mocks.api.listSessionMessages).toHaveBeenCalledTimes(2));

    await act(async () => settledRecovery.resolve({ messages: [] }));
    await waitFor(() => expect(screen.queryByText(projected.text)).toBeNull());
    await act(async () => staleRecovery.resolve({ messages: [projected] }));

    expect(screen.queryByText(projected.text)).toBeNull();
  });

  it('does not let an older bootstrap resurrect a projection after recovery', async () => {
    const projected = projectedMessage('delivery-racing-bootstrap', 'stale bootstrap projection');
    const staleBootstrap = deferred<ReturnType<typeof bootstrapPayload> & { messages: typeof projected[] }>();
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockReset().mockReturnValue(staleBootstrap.promise);

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.events).not.toBeNull());
    act(() => mocks.events?.onConnected());
    await waitFor(() => expect(mocks.api.listSessionMessages).toHaveBeenCalledTimes(1));
    await act(async () => staleBootstrap.resolve({
      ...bootstrapPayload('session-new'),
      messages: [projected],
    }));

    await waitFor(() => expect(screen.getByText('chat.transcriptEmpty')).toBeTruthy());
    expect(screen.queryByText(projected.text)).toBeNull();
  });

  it('refreshes detached Activity without settling the current live generation', async () => {
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      config: { ui: { show_agent_activity: true } },
    });

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalled());
    mocks.api.getSessionActivity.mockClear();
    const detachedRefresh = deferred<{ groups: never[] }>();
    mocks.api.getSessionActivity.mockReturnValue(detachedRefresh.promise);

    act(() => {
      mocks.events?.onTurnStart({ session_id: 'session-new' });
      mocks.events?.onMessageNew({
        ...projectedMessage('active-step-1', 'first active step'),
        author: 'agent',
        type: 'assistant',
        source: 'agent',
      });
      mocks.events?.onMessageNew({
        ...projectedMessage('detached-result', 'background completed'),
        author: 'agent',
        type: 'result',
        source: 'agent',
        metadata: { detached: true, activity_id: 'background-1' },
      });
      mocks.events?.onMessageNew({
        ...projectedMessage('active-step-2', 'second active step'),
        author: 'agent',
        type: 'assistant',
        source: 'agent',
      });
    });

    await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalledTimes(1));
    expect(screen.getByText('first active step')).toBeTruthy();
    expect(screen.getByText('second active step')).toBeTruthy();
  });

  it('does not render an open activity group as interrupted before the switched chat turn state is known', async () => {
    const openGroup = {
      id: 'open-turn',
      anchor_message_id: null,
      anchor_position: 'after' as const,
      open: true,
      status: 'interrupted' as const,
      steps: 1,
      duration_ms: null,
    };
    const runningBootstrap = deferred<ReturnType<typeof bootstrapPayload>>();
    const unknownActivity = deferred<{ groups: typeof openGroup[] }>();
    const runningActivity = deferred<{ groups: typeof openGroup[] }>();
    mocks.api.getSession.mockResolvedValue({ id: 'session-running' });
    mocks.api.getSessionBootstrap
      .mockReset()
      .mockResolvedValueOnce({
        ...bootstrapPayload('session-old'),
        config: { ui: { show_agent_activity: true } },
      })
      .mockReturnValueOnce(runningBootstrap.promise);
    let runningActivityReads = 0;
    mocks.api.getSessionActivity.mockImplementation((sessionId: string) => {
      if (sessionId !== 'session-running') return Promise.resolve({ groups: [] });
      runningActivityReads += 1;
      return runningActivityReads === 1 ? unknownActivity.promise : runningActivity.promise;
    });
    mocks.api.getSessionActivityGroup.mockResolvedValue({
      ...openGroup,
      rows: [{
        id: 'active-step',
        kind: 'assistant',
        text: 'still working',
        created_at: '2026-08-21T00:00:00Z',
      }],
    });

    render(
      <MemoryRouter initialEntries={['/chat/session-old']}>
        <SessionSwitcher />
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalledWith('session-old'));
    act(() => screen.getByRole('button', { name: 'switch chat' }).click());
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(2));

    act(() => mocks.events?.onConnected());
    await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalledWith('session-running'));

    await act(async () => runningBootstrap.resolve({
      ...bootstrapPayload('session-running'),
      config: { ui: { show_agent_activity: true } },
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    }));

    await act(async () => unknownActivity.resolve({ groups: [openGroup] }));
    await waitFor(() => expect(runningActivityReads).toBe(2));
    expect(screen.queryByText('chat.agentActivity.interrupted')).toBeNull();

    await act(async () => runningActivity.resolve({ groups: [openGroup] }));

    await waitFor(() => expect(screen.getByText('chat.agentActivity.running')).toBeTruthy());
    expect(screen.queryByText('chat.agentActivity.interrupted')).toBeNull();
    act(() => mocks.events?.onMessageNew({
      ...projectedMessage('next-active-step', 'next live step'),
      session_id: 'session-running',
      author: 'agent',
      type: 'assistant',
      source: 'agent',
    }));
    expect(screen.getByText('still working')).toBeTruthy();
    expect(screen.getByText('next live step')).toBeTruthy();
  });

  it.each(['before summary', 'during detail'] as const)(
    'retains the full running history when live rows arrive %s on a session switch',
    async (arrival) => {
      const rows = Array.from({ length: 300 }, (_, index) => ({
        id: `step-${index}`,
        kind: 'assistant' as const,
        text: `activity step ${index}`,
        created_at: new Date(Date.UTC(2026, 8, 5, 0, 0, index)).toISOString(),
      }));
      const openGroup: TurnActivityGroupWire = {
        id: rows[0].id,
        anchor_message_id: null,
        anchor_position: 'after',
        open: true,
        status: 'interrupted',
        steps: rows.length,
        duration_ms: null,
      };
      const summary = deferred<{ groups: TurnActivityGroupWire[] }>();
      const detail = deferred<TurnActivityGroupWire>();
      const running = { ...idleTurnState, foreground: 'running', in_flight: true };
      mocks.api.getSession.mockImplementation((id: string) => Promise.resolve({ id }));
      mocks.api.getSessionBootstrap.mockImplementation((id: string) => Promise.resolve({
        ...bootstrapPayload(id),
        config: { ui: { show_agent_activity: true } },
        turn_state: id === 'session-running' ? running : idleTurnState,
      }));
      mocks.api.getTurnState.mockResolvedValue(running);
      mocks.api.getSessionActivity.mockImplementation((id: string) => (
        id === 'session-running' ? summary.promise : Promise.resolve({ groups: [] })
      ));
      mocks.api.getSessionActivityGroup.mockReturnValue(detail.promise);

      render(
        <MemoryRouter initialEntries={['/chat/session-old']}>
          <SessionSwitcher />
          <Routes>
            <Route path="/chat/:sessionId" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>,
      );

      await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalledWith('session-old'));
      act(() => screen.getByRole('button', { name: 'switch chat' }).click());
      await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalledWith('session-running'));

      if (arrival === 'during detail') {
        await act(async () => summary.resolve({ groups: [openGroup] }));
        await waitFor(() => expect(mocks.api.getSessionActivityGroup).toHaveBeenCalled());
      }
      const emit = (id: string, text: string) => mocks.events?.onMessageNew({
        ...projectedMessage(id, text),
        session_id: 'session-running',
        created_at: rows.find((row) => row.id === id)?.created_at
          ?? new Date(Date.UTC(2026, 8, 5, 0, 0, 300)).toISOString(),
        author: 'agent',
        type: 'assistant',
        source: 'agent',
      });
      act(() => {
        emit(rows[299].id, rows[299].text);
        emit('step-300', 'activity step 300');
      });
      if (arrival === 'before summary') {
        await act(async () => summary.resolve({ groups: [openGroup] }));
        await waitFor(() => expect(mocks.api.getSessionActivityGroup).toHaveBeenCalled());
      }
      await act(async () => detail.resolve({ ...openGroup, rows }));

      const expected = [...rows.map((row) => row.text), 'activity step 300'];
      const renderedRows = () => screen.getAllByText(/^activity step \d+$/).map((el) => el.textContent);
      await waitFor(() => expect(renderedRows()).toEqual(expected));
      act(() => emit(rows[298].id, rows[298].text));
      expect(renderedRows()).toEqual(expected);

      // Reconnect can re-read an older snapshot while the live tail is newer.
      const reads = mocks.api.getSessionActivityGroup.mock.calls.length;
      act(() => mocks.events?.onConnected());
      await waitFor(() => expect(mocks.api.getSessionActivityGroup.mock.calls.length).toBeGreaterThan(reads));
      expect(renderedRows()).toEqual(expected);
    },
  );

  it.each(['next turn', 'other session', 'same session again'] as const)(
    'rejects old running history after entering the %s',
    async (transition) => {
      const openGroup: TurnActivityGroupWire = {
        id: 'old-group', anchor_message_id: null, anchor_position: 'after',
        open: true, status: 'interrupted', steps: 1, duration_ms: null,
      };
      const oldDetail = deferred<TurnActivityGroupWire>();
      const currentDetail = deferred<TurnActivityGroupWire>();
      const running = { ...idleTurnState, foreground: 'running', in_flight: true };
      mocks.api.getSession.mockImplementation((id: string) => Promise.resolve({ id }));
      mocks.api.getSessionBootstrap.mockImplementation((id: string) => Promise.resolve({
        ...bootstrapPayload(id),
        config: { ui: { show_agent_activity: true } },
        turn_state: running,
      }));
      mocks.api.getTurnState.mockResolvedValue(running);
      mocks.api.getSessionActivity
        .mockResolvedValueOnce({ groups: [openGroup] })
        .mockResolvedValue({ groups: [{ ...openGroup, id: 'current-group' }] });
      mocks.api.getSessionActivityGroup.mockImplementation((_sid: string, groupId: string) => (
        groupId === 'old-group' ? oldDetail.promise : currentDetail.promise
      ));
      render(
        <MemoryRouter initialEntries={['/chat/session-new']}>
          <SessionSwitcher />
          <SessionSwitcher sessionId="session-new" label="return chat" />
          <Routes>
            <Route path="/chat/:sessionId" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>,
      );
      await waitFor(() => expect(mocks.api.getSessionActivityGroup).toHaveBeenCalledWith('session-new', 'old-group'));
      if (transition === 'next turn') {
        act(() => mocks.events?.onTurnStart({ session_id: 'session-new' }));
      } else {
        act(() => screen.getByRole('button', { name: 'switch chat' }).click());
        await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(2));
        if (transition === 'same session again') {
          act(() => screen.getByRole('button', { name: 'return chat' }).click());
          await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(3));
        }
      }
      act(() => mocks.events?.onMessageNew({
        ...projectedMessage('current-row', 'current live row'),
        session_id: transition === 'other session' ? 'session-running' : 'session-new',
        author: 'agent', type: 'assistant', source: 'agent',
      }));
      await act(async () => oldDetail.resolve({
        ...openGroup,
        rows: [{ id: 'old-row', kind: 'assistant', text: 'obsolete history row', created_at: '2026-09-05T00:00:00Z' }],
      }));
      expect(screen.queryByText('obsolete history row')).toBeNull();
      expect(screen.getByText('current live row')).toBeTruthy();
    },
  );

  it('recovers running history after a detail failure while live rows continue', async () => {
    const group: TurnActivityGroupWire = {
      id: 'history', anchor_message_id: null, anchor_position: 'after',
      open: true, status: 'interrupted', steps: 1, duration_ms: null,
    };
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      config: { ui: { show_agent_activity: true } },
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    });
    mocks.api.getSessionActivity.mockResolvedValue({ groups: [group] });
    mocks.api.getSessionActivityGroup
      .mockRejectedValueOnce(new Error('temporary detail failure'))
      .mockResolvedValue({
        ...group,
        rows: [{ id: 'history', kind: 'assistant', text: 'recovered history row', created_at: '2026-09-05T00:00:00Z' }],
      });
    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocks.api.getSessionActivityGroup).toHaveBeenCalledTimes(1));
    act(() => mocks.events?.onMessageNew({
      ...projectedMessage('live', 'live during recovery'),
      author: 'agent', type: 'assistant', source: 'agent',
    }));
    await waitFor(() => expect(screen.getByText('recovered history row')).toBeTruthy(), { timeout: 2500 });
    expect(screen.getByText('live during recovery')).toBeTruthy();
    expect(mocks.api.getSessionActivityGroup).toHaveBeenCalledTimes(2);
  });

  it.each(['before summary', 'during detail', 'after hydration'] as const)(
    'isolates Activity phases when output arrives %s without ending the Turn',
    async (arrival) => {
      const previous: TurnActivityGroupWire = {
        id: 'previous-phase', anchor_message_id: null, anchor_position: 'after',
        open: true, status: 'interrupted', steps: 1, duration_ms: null,
      };
      const settled: TurnActivityGroupWire = {
        ...previous, open: false, status: 'done',
        anchor_message_id: 'phase-output', anchor_position: 'before',
      };
      const current: TurnActivityGroupWire = {
        ...previous, id: 'current-phase', anchor_message_id: 'phase-output',
      };
      const oldSummary = deferred<{ groups: TurnActivityGroupWire[] }>();
      const oldDetail = deferred<TurnActivityGroupWire>();
      let phaseAdvanced = false;
      const previousRow = {
        id: previous.id, kind: 'assistant' as const, text: 'previous phase step',
        created_at: '2026-09-05T00:00:00Z',
      };
      const running = { ...idleTurnState, foreground: 'running', in_flight: true };
      mocks.api.getSessionBootstrap.mockResolvedValue({
        ...bootstrapPayload('session-new'),
        config: { ui: { show_agent_activity: true } }, turn_state: running,
      });
      mocks.api.getTurnState.mockResolvedValue(running);
      mocks.api.getSessionActivity.mockImplementation(() => (
        phaseAdvanced ? Promise.resolve({ groups: [settled, current] }) : oldSummary.promise
      ));
      mocks.api.getSessionActivityGroup.mockImplementation((_sid: string, groupId: string) => (
        groupId === previous.id ? oldDetail.promise : Promise.resolve({
          ...current,
          rows: [{ id: current.id, kind: 'assistant', text: 'current phase history', created_at: '2026-09-05T00:00:02Z' }],
        })
      ));
      render(
        <MemoryRouter initialEntries={['/chat/session-new']}>
          <Routes>
            <Route path="/chat/:sessionId" element={<ChatPage />} />
          </Routes>
        </MemoryRouter>,
      );
      await waitFor(() => expect(mocks.api.getSessionActivity).toHaveBeenCalled());
      if (arrival !== 'before summary') {
        await act(async () => oldSummary.resolve({ groups: [previous] }));
        await waitFor(() => expect(mocks.api.getSessionActivityGroup).toHaveBeenCalledWith('session-new', previous.id));
      }
      if (arrival === 'after hydration') {
        await act(async () => oldDetail.resolve({ ...previous, rows: [previousRow] }));
        await screen.findByText(previousRow.text);
      }
      phaseAdvanced = true;
      act(() => {
        mocks.events?.onMessageNew({
          ...projectedMessage('phase-output', 'phase result'),
          author: 'agent', type: 'output', source: 'agent', created_at: '2026-09-05T00:00:01Z',
        });
        mocks.events?.onMessageNew({
          ...projectedMessage('current-live', 'current phase live step'),
          author: 'agent', type: 'assistant', source: 'agent', created_at: '2026-09-05T00:00:03Z',
        });
      });
      await act(async () => {
        oldSummary.resolve({ groups: [previous] });
        oldDetail.resolve({ ...previous, rows: [previousRow] });
      });
      await screen.findByText('current phase history');
      expect(screen.getByText('current phase live step')).toBeTruthy();
      expect(screen.getByText('chat.agentActivity.running')).toBeTruthy();
      expect(screen.queryByText(previousRow.text)).toBeNull();
      // The old phase remains available only in its durable, settled chip.
      act(() => screen.getByTitle('chat.agentActivity.expand').click());
      await screen.findByText(previousRow.text);
      expect(screen.getAllByText(previousRow.text)).toHaveLength(1);
      expect(screen.getByText('current phase live step')).toBeTruthy();
    },
  );

  it('keeps completed Activity history lazy and separate from the running card', async () => {
    const group: TurnActivityGroupWire = {
      id: 'completed', anchor_message_id: null, anchor_position: 'before',
      open: false, status: 'done', steps: 1, duration_ms: 1000,
    };
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      config: { ui: { show_agent_activity: true } },
      messages: [projectedMessage('user-message', 'start completed task')],
    });
    mocks.api.getSessionActivity.mockResolvedValue({ groups: [group] });
    mocks.api.getSessionActivityGroup.mockResolvedValue({
      ...group,
      rows: [{ id: 'completed', kind: 'assistant', text: 'completed history row', created_at: '2026-09-05T00:00:00Z' }],
    });
    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );
    const expand = await screen.findByTitle('chat.agentActivity.expand');
    expect(mocks.api.getSessionActivityGroup).not.toHaveBeenCalled();
    act(() => expand.click());
    await screen.findByText('completed history row');
    expect(screen.queryByText('chat.agentActivity.running')).toBeNull();
  });

  it('uses the thinking dots and the Activity header as shortcuts for the global display setting', async () => {
    const openGroup = {
      id: 'current-turn',
      anchor_message_id: null,
      anchor_position: 'after' as const,
      open: true,
      status: 'running' as const,
      steps: 1,
      duration_ms: null,
    };
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    });
    mocks.api.getSessionActivity.mockResolvedValue({ groups: [openGroup] });
    mocks.api.getSessionActivityGroup.mockResolvedValue({
      ...openGroup,
      rows: [{
        id: 'current-step',
        kind: 'assistant',
        text: 'current work step',
        created_at: '2026-08-25T00:00:00Z',
      }],
    });

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const enable = await screen.findByRole('button', { name: 'chat.agentActivity.enable' });
    expect(enable.className).toContain('cursor-pointer');
    act(() => enable.click());

    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledWith([
      { kind: 'set', path: ['ui', 'show_agent_activity'], value: true },
    ]));
    await waitFor(() => expect(screen.getByText('current work step')).toBeTruthy());

    const disable = screen.getByRole('button', { name: 'chat.agentActivity.disable' });
    expect(disable.className).toContain('size-6');
    expect(disable.className).toContain('border-border');
    act(() => disable.click());

    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledWith([
      { kind: 'set', path: ['ui', 'show_agent_activity'], value: false },
    ]));
    await waitFor(() => expect(screen.queryByText('current work step')).toBeNull());
    expect(screen.getByRole('button', { name: 'chat.agentActivity.enable' })).toBeTruthy();
  });

  it('waits for a visibility write before bootstrapping a switched chat', async () => {
    const activityWrite = deferred<{ ui: { show_agent_activity: boolean } }>();
    mocks.api.getSession.mockImplementation((id: string) => Promise.resolve({ id }));
    mocks.api.getSessionBootstrap
      .mockResolvedValueOnce({
        ...bootstrapPayload('session-new'),
        turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
      })
      .mockResolvedValueOnce({
        ...bootstrapPayload('session-running'),
        config: { ui: { show_agent_activity: true } },
        turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
      });
    mocks.api.mutateConfig.mockImplementationOnce(() => activityWrite.promise);

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <SessionSwitcher />
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const enable = await screen.findByRole('button', { name: 'chat.agentActivity.enable' });
    mocks.api.waitForAgentActivityConfigMutations.mockImplementationOnce(
      () => activityWrite.promise.then(() => undefined),
    );
    act(() => enable.click());
    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledTimes(1));
    act(() => screen.getByRole('button', { name: 'switch chat' }).click());
    await act(async () => Promise.resolve());
    expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => activityWrite.resolve({ ui: { show_agent_activity: true } }));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(2));
    expect(mocks.api.getSessionBootstrap).toHaveBeenLastCalledWith('session-running');
    await waitFor(() => expect(screen.queryByRole('button', {
      name: 'chat.agentActivity.enable',
    })).toBeNull());
  });

  it('does not let an in-flight authorization bootstrap overwrite a newer visibility click', async () => {
    const authorizationBootstrap = deferred<ReturnType<typeof bootstrapPayload>>();
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap
      .mockResolvedValueOnce({
        ...bootstrapPayload('session-new'),
        turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
      })
      .mockImplementationOnce(() => authorizationBootstrap.promise);

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const enable = await screen.findByRole('button', { name: 'chat.agentActivity.enable' });
    act(() => mocks.events?.onAuthorizationChanged({ instance_authorization_revision: 2 }));
    await waitFor(() => expect(mocks.api.getSessionBootstrap).toHaveBeenCalledTimes(2));
    act(() => enable.click());
    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledTimes(1));

    await act(async () => authorizationBootstrap.resolve({
      ...bootstrapPayload('session-new'),
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    }));
    await waitFor(() => expect(screen.queryByRole('button', {
      name: 'chat.agentActivity.enable',
    })).toBeNull());
  });

  it('restores the enable shortcut when the visibility write fails', async () => {
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    });
    mocks.api.mutateConfig.mockRejectedValueOnce(new Error('save failed'));

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const enable = await screen.findByRole('button', { name: 'chat.agentActivity.enable' });
    act(() => enable.click());
    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole('button', {
      name: 'chat.agentActivity.enable',
    })).toBeTruthy());
  });

  it('restores the last confirmed visibility when the latest write fails', async () => {
    const openGroup = {
      id: 'current-turn',
      anchor_message_id: null,
      anchor_position: 'after' as const,
      open: true,
      status: 'running' as const,
      steps: 1,
      duration_ms: null,
    };
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    });
    mocks.api.getSessionActivity.mockResolvedValue({ groups: [openGroup] });
    mocks.api.getSessionActivityGroup.mockResolvedValue({
      ...openGroup,
      rows: [{
        id: 'current-step',
        kind: 'assistant',
        text: 'current work step',
        created_at: '2026-08-25T00:00:00Z',
      }],
    });
    mocks.api.mutateConfig
      .mockResolvedValueOnce({ ui: { show_agent_activity: true } })
      .mockRejectedValueOnce(new Error('save failed'));

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const enable = await screen.findByRole('button', { name: 'chat.agentActivity.enable' });
    act(() => enable.click());
    await screen.findByText('current work step');
    const disable = screen.getByRole('button', { name: 'chat.agentActivity.disable' });
    act(() => disable.click());
    await waitFor(() => expect(mocks.api.mutateConfig).toHaveBeenCalledTimes(2));
    await screen.findByText('current work step');
    await waitFor(() => expect(screen.getByRole('button', {
      name: 'chat.agentActivity.disable',
    })).toBeTruthy());
  });

  it('does not offer the global enable shortcut to a Viewer', async () => {
    mocks.authorizationCapabilities.can_chat = false;
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    });

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await screen.findByText('chat.thinking');
    expect(screen.queryByRole('button', { name: 'chat.agentActivity.enable' })).toBeNull();
    expect(mocks.api.mutateConfig).not.toHaveBeenCalled();
  });

  it('does not offer the global disable shortcut to a Viewer', async () => {
    mocks.authorizationCapabilities.can_chat = false;
    const openGroup = {
      id: 'current-turn',
      anchor_message_id: null,
      anchor_position: 'after' as const,
      open: true,
      status: 'running' as const,
      steps: 1,
      duration_ms: null,
    };
    mocks.api.getSession.mockResolvedValue({ id: 'session-new' });
    mocks.api.getSessionBootstrap.mockResolvedValue({
      ...bootstrapPayload('session-new'),
      config: { ui: { show_agent_activity: true } },
      turn_state: { ...idleTurnState, foreground: 'running', in_flight: true },
    });
    mocks.api.getSessionActivity.mockResolvedValue({ groups: [openGroup] });
    mocks.api.getSessionActivityGroup.mockResolvedValue({
      ...openGroup,
      rows: [{
        id: 'current-step',
        kind: 'assistant',
        text: 'viewer-visible work step',
        created_at: '2026-08-25T00:00:00Z',
      }],
    });

    render(
      <MemoryRouter initialEntries={['/chat/session-new']}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await screen.findByText('viewer-visible work step');
    expect(screen.queryByRole('button', { name: 'chat.agentActivity.disable' })).toBeNull();
    expect(mocks.api.mutateConfig).not.toHaveBeenCalled();
  });
});
