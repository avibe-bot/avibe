/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';
import './ChatPage.duplicateKeyGuard';

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

import type { SessionRuntimeState, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import type { ComposerProps } from './Composer';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';

// "Send now" on the queue strip ends the typing on a phone like Send does: the
// soft keyboard closes. A desktop keeps the field focused. The device is set up
// before the modules load, as the app meets it.

const device = vi.hoisted(() => {
  class Viewport extends EventTarget {
    height = 844;
    width = 390;
    offsetTop = 0;
    offsetLeft = 0;
    pageTop = 0;
    scale = 1;
  }
  const viewport = new Viewport();
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
  Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, value: 5 });
  return { viewport };
});

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
    sendQueuedNow: vi.fn(),
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

vi.mock('../../context/WindowManagerContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/WindowManagerContext')>()),
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
          <textarea aria-label="draft" />
          {props.actions}
        </div>
      );
    },
  };
});

import { ChatPage } from './ChatPage';

const SESSION = 'session-a';
const running: SessionRuntimeState = {
  in_flight: true,
  foreground: 'running',
  native_turn_started: true,
  pending_input_count: 1,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'connected',
};
const queued = {
  id: 'q1', scope_id: null, session_id: SESSION, platform: 'avibe', author: 'user', type: 'queued', source: 'user',
  author_id: null, author_name: null, native_message_id: null, parent_native_message_id: null, state: 'queued',
  text: 'next step', content: {}, metadata: {}, created_at: '2026-11-01T08:00:01Z', updated_at: '2026-11-01T08:00:01Z',
  delivered_at: null, read_at: null,
} as WorkbenchMessage;
const bootstrap = {
  session: { id: SESSION, title: 'Chat', status: 'active', agent_name: 'vibey', agent_backend: 'vibey' } as WorkbenchSession,
  capabilities: { can_chat: true },
  agents: [{ id: 'agent-vibey', name: 'vibey', backend: 'vibey', model: 'm' } as VibeAgentBrief],
  default_agent_name: 'vibey',
  config: { ui: {} },
  messages: [],
  next_after_id: null,
  next_before_id: null,
  turn_state: running,
  queued: [queued],
  draft: { text: '', updated_at: null },
};

const mount = () => render(
  <MemoryRouter initialEntries={[`/chat/${SESSION}`]}>
    <Routes>
      <Route path="/chat/:sessionId" element={<ChatPage />} />
    </Routes>
  </MemoryRouter>,
);

beforeEach(() => {
  for (const name of ['ResizeObserver', 'IntersectionObserver']) {
    vi.stubGlobal(name, class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
  }
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
  mocks.api.getSessionBootstrap.mockResolvedValue(bootstrap);
  mocks.api.getTurnState.mockResolvedValue(running);
  mocks.api.getWorkbenchPrefs.mockResolvedValue({});
  mocks.api.listSessionMessages.mockReturnValue(new Promise(() => {}));
  mocks.api.listSessionQueue.mockResolvedValue({ queued: [queued] });
  mocks.api.waitForAgentActivityConfigMutations.mockResolvedValue(undefined);
  mocks.api.onSessionArchived.mockReturnValue(() => {});
  mocks.api.probeShowPageAccess.mockReturnValue(new Promise(() => {}));
  mocks.api.sendQueuedNow.mockReturnValue(new Promise(() => {}));
  mocks.apiFetch.mockReturnValue(new Promise(() => {}));
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  device.viewport.height = 844;
  device.viewport.dispatchEvent(new Event('resize'));
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

const typing = async (keyboardOpen: boolean) => {
  mount();
  await screen.findByText('next step');
  const draft = screen.getByLabelText('draft');
  act(() => draft.focus());
  if (keyboardOpen) {
    act(() => {
      device.viewport.height = 500;
      device.viewport.dispatchEvent(new Event('resize'));
    });
  }
  return draft;
};

describe('Send now on the queue strip', () => {
  it('closes the soft keyboard on a phone', async () => {
    const draft = await typing(true);

    act(() => screen.getByRole('button', { name: 'chat.queue.sendNow' }).click());

    expect(mocks.api.sendQueuedNow).toHaveBeenCalledWith(SESSION, 'q1');
    expect(document.activeElement).not.toBe(draft);
  });

  it('keeps the field focused when no soft keyboard is open', async () => {
    const draft = await typing(false);

    act(() => screen.getByRole('button', { name: 'chat.queue.sendNow' }).click());

    expect(mocks.api.sendQueuedNow).toHaveBeenCalledWith(SESSION, 'q1');
    expect(document.activeElement).toBe(draft);
  });
});

describe('the Lock Screen progress entry', () => {
  it('is not on the chat page, even in a shell that advertises Live Activities', async () => {
    const call = vi.fn(async () => ({ enabled: true, tracking: false }));
    vi.stubGlobal('avibeNative', {
      contract: 1, platform: 'ios', shellVersion: 'test',
      capabilities: { liveActivities: 1 }, call, on: () => () => {},
    });

    mount();
    await screen.findByText('next step');

    expect(screen.queryByTestId('live-activity-entry')).toBeNull();
    expect(screen.queryByText(/liveActivity\./)).toBeNull();
    expect(call.mock.calls.filter(([method]) => String(method).startsWith('liveActivity.'))).toEqual([]);
  });
});

// Regresses the lead's duplicateKey.repro: reuse the real ChatPage through repeated
// session changes with the queue strip present. No orphan nodes remain.
function SessionLinks() {
  const navigate = useNavigate();
  return <button onClick={() => navigate(`/chat/${screen.getByLabelText('destination').getAttribute('data-session')}`)}>switch</button>;
}
it.each([
  { version: 0, prefix: 'session-' }, { version: 1, prefix: 'session-' },
  { version: 2, prefix: 'session-' }, { version: 2, prefix: 'ses_' },
])('leaves no chat nodes behind when switching $prefix sessions on shell capability $version', async ({ version, prefix }) => {
  const call = vi.fn(async (_method: string, _params?: Record<string, unknown>) => ({ ok: true }));
  vi.stubGlobal('avibeNative', {
    contract: 1, platform: 'ios', shellVersion: 'test',
    capabilities: { liveActivities: version }, call, on: () => () => {},
  });
  mocks.api.getSessionBootstrap.mockImplementation(async (id: string) => ({
    ...bootstrap, session: { ...bootstrap.session, id, title: `Title ${id}` },
    queued: [{ ...queued, id: `queue-${id}`, session_id: id, text: `next ${id}` }],
  }));
  const view = render(<MemoryRouter initialEntries={[`/chat/${prefix}a`]}>
    <SessionLinks /><span aria-label="destination" />
    <Routes><Route path="/chat/:sessionId" element={<ChatPage />} /></Routes>
  </MemoryRouter>);
  await screen.findByText(`next ${prefix}a`);
  const nodeCount = view.container.querySelectorAll('*').length;
  for (const suffix of ['b', 'c', 'd', 'a', 'b']) {
    const id = `${prefix}${suffix}`;
    screen.getByLabelText('destination').setAttribute('data-session', id);
    await act(async () => screen.getByRole('button', { name: 'switch' }).click());
    await screen.findByText(`next ${id}`);
    expect(screen.getAllByTestId('composer')).toHaveLength(1);
    expect(screen.getAllByLabelText('draft')).toHaveLength(1);
    expect(screen.queryByText(/liveActivity\./)).toBeNull();
    expect(view.container.querySelectorAll('*')).toHaveLength(nodeCount);
  }
  if (version < 2) expect(call).not.toHaveBeenCalled();
  else expect(call).toHaveBeenLastCalledWith('liveActivity.observe', {
    sessionId: `${prefix}b`, agentName: prefix === 'ses_' ? `Title ${prefix}b` : 'vibey',
    sessionTitle: prefix === 'ses_' ? null : `Title ${prefix}b`, running: true,
  });
  view.unmount();
  if (version >= 2) expect(call).toHaveBeenLastCalledWith('liveActivity.observe', { sessionId: null });
});

// Settings keeps the chat mounted underneath its route surface. Leaving that
// conversation must clear the native observation even without an unmount.
it('clears the observation while Settings covers the retained chat and restores it on return', async () => {
  const call = vi.fn(async (_method: string, _params?: Record<string, unknown>) => ({ ok: true }));
  vi.stubGlobal('avibeNative', {
    contract: 1, platform: 'ios', shellVersion: 'test',
    capabilities: { liveActivities: 2 }, call, on: () => () => {},
  });
  const page = (active: boolean) => <MemoryRouter initialEntries={[`/chat/${SESSION}`]}>
    <RouteSurfaceActiveContext.Provider value={active}>
      <Routes><Route path="/chat/:sessionId" element={<ChatPage />} /></Routes>
    </RouteSurfaceActiveContext.Provider>
  </MemoryRouter>;
  const view = render(page(true));
  await screen.findByText('next step');
  expect(call).toHaveBeenLastCalledWith('liveActivity.observe', expect.objectContaining({ sessionId: SESSION, running: true }));
  view.rerender(page(false));
  expect(call).toHaveBeenLastCalledWith('liveActivity.observe', { sessionId: null });
  const count = call.mock.calls.length;
  view.rerender(page(false));
  expect(call).toHaveBeenCalledTimes(count);
  view.rerender(page(true));
  expect(call).toHaveBeenLastCalledWith('liveActivity.observe', expect.objectContaining({ sessionId: SESSION, running: true }));
});
