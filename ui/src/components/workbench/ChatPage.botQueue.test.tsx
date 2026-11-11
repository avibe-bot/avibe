/** @vitest-environment jsdom */
import 'fake-indexeddb/auto';

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { SessionRuntimeState, VibeAgentBrief, WorkbenchMessage, WorkbenchSession } from '../../context/ApiContext';
import type { ComposerProps } from './Composer';
import type { Bot } from '../../lib/bots';
import { clearLocalSnapshots, setLocalSnapshotPrincipal } from '../../lib/localSnapshots';

// A Bot's "Hide details" (docs/plans/bots.md D5) hides Harness input waiting for
// the Bot's Turn, as it hides the delivered Harness rows. What the user sent
// stays in the strip: it has not reached the Bot yet (D4).

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
  bot: null as Bot | null,
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

vi.mock('../../context/BotsContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../context/BotsContext')>();
  return {
    ...actual,
    useBots: () => ({
      ...actual.useBots(),
      enabled: true,
      loaded: true,
      listed: true,
      bots: mocks.bot ? [mocks.bot] : [],
      botForSession: (id: string | null | undefined) => (mocks.bot && id === mocks.bot.session_id ? mocks.bot : undefined),
    }),
  };
});

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

const SESSION = 'ses_cso';
const bot = (hideDetails: boolean): Bot => ({
  handle: 'cso', display_name: 'CSO', avatar: null, role: '', agent_name: 'cso', session_id: SESSION,
  builtin: false, hide_details: hideDetails,
} as Bot);
const running: SessionRuntimeState = {
  in_flight: true,
  foreground: 'running',
  native_turn_started: true,
  pending_input_count: 2,
  background_activities: [],
  pending_activity_output_count: 0,
  connection: 'connected',
};
const queued = (id: string, source: 'user' | 'harness', text: string) => ({
  id, scope_id: null, session_id: SESSION, platform: 'avibe', author: 'user', type: source, source,
  author_id: null, author_name: null, native_message_id: null, parent_native_message_id: null, state: 'queued',
  text, content: {}, metadata: {}, created_at: '2026-11-01T08:00:01Z', updated_at: '2026-11-01T08:00:01Z',
  delivered_at: null, read_at: null,
} as WorkbenchMessage);
const mine = queued('q1', 'user', 'my next step');
const watchResult = queued('q2', 'harness', '[Harness: watch result]');

const mount = (queue: WorkbenchMessage[]) => {
  mocks.api.getSessionBootstrap.mockResolvedValue({
    session: { id: SESSION, title: 'CSO', status: 'active', agent_name: 'cso', agent_backend: 'vibey' } as WorkbenchSession,
    capabilities: { can_chat: true },
    agents: [{ id: 'agent-cso', name: 'cso', backend: 'vibey', model: 'm' } as VibeAgentBrief],
    default_agent_name: 'vibey',
    config: { ui: {} },
    messages: [],
    next_after_id: null,
    next_before_id: null,
    turn_state: running,
    queued: queue,
    draft: { text: '', updated_at: null },
  });
  mocks.api.listSessionQueue.mockResolvedValue({ queued: queue });
  return render(
    <MemoryRouter initialEntries={[`/chat/${SESSION}`]}>
      <Routes>
        <Route path="/chat/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
};

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
  mocks.api.getTurnState.mockResolvedValue(running);
  mocks.api.getWorkbenchPrefs.mockResolvedValue({});
  mocks.api.listSessionMessages.mockReturnValue(new Promise(() => {}));
  mocks.api.waitForAgentActivityConfigMutations.mockResolvedValue(undefined);
  mocks.api.onSessionArchived.mockReturnValue(() => {});
  mocks.api.probeShowPageAccess.mockReturnValue(new Promise(() => {}));
  mocks.api.sendQueuedNow.mockReturnValue(new Promise(() => {}));
  mocks.apiFetch.mockReturnValue(new Promise(() => {}));
});

afterEach(async () => {
  cleanup();
  vi.unstubAllGlobals();
  mocks.bot = null;
  setLocalSnapshotPrincipal(null);
  await clearLocalSnapshots();
});

describe("a Bot's queue strip", () => {
  it('shows only what the user sent while Hide details is on', async () => {
    mocks.bot = bot(true);
    mount([mine, watchResult]);

    await screen.findByText('my next step');
    expect(screen.queryByText('[Harness: watch result]')).toBeNull();
  });

  it('is not shown for queued Harness input alone while Hide details is on', async () => {
    mocks.bot = bot(true);
    mount([watchResult]);

    await screen.findByTestId('composer');
    // Let the queue read land before asserting what it drew.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('[Harness: watch result]')).toBeNull();
    expect(document.querySelector('[data-queue-batch]')).toBeNull();
  });

  it('shows queued Harness input while Hide details is off', async () => {
    mocks.bot = bot(false);
    mount([mine, watchResult]);

    await screen.findByText('my next step');
    expect(screen.getByText('[Harness: watch result]')).toBeTruthy();
  });

  // The queue is in the order it was sent, so a hidden Harness row can be its head.
  // Send now acts on that real head, never on the first row drawn.
  it('sends the real queue head now even when it is hidden', async () => {
    mocks.bot = bot(true);
    mount([watchResult, mine]);

    await screen.findByText('my next step');
    act(() => screen.getByRole('button', { name: 'chat.queue.sendNow' }).click());

    expect(mocks.api.sendQueuedNow).toHaveBeenCalledWith(SESSION, 'q2');
  });

  it('keeps Send now busy while a hidden head is being steered in', async () => {
    mocks.bot = bot(true);
    mount([{ ...watchResult, state: 'steering' } as WorkbenchMessage, mine]);

    await screen.findByText('my next step');
    expect((screen.getByRole('button', { name: /chat\.queue\.sendingNow/ }) as HTMLButtonElement).disabled).toBe(true);
  });
});
