/** @vitest-environment jsdom */

// The header's route edits apply within the click and persist behind them, which
// is the point of the optimistic path. These tests pin what that means at the two
// boundaries where it is observable — what reached the network, in which order
// (Enter is never held behind an in-flight route PATCH, the resulting admission
// gap being the server's to close; the picks made during one PATCH reach the row
// as a single merged follow-up) — and what the user is left looking at when the
// server refuses the burst.

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import type { AgentRoutePatch } from './AgentRoutePicker';

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
    updateSession: vi.fn(),
    cancelSession: vi.fn(),
    recoverSessionDraftAfterRejectedSend: vi.fn(),
    reconcileSessionDraftAfterSend: vi.fn(),
    convergeSessionArchived: vi.fn(),
  },
  apiFetch: vi.fn(),
  // Captured from the mocked leaf components: the route pick and the send are
  // both props ChatPage hands down, so driving them needs no DOM choreography.
  onPatch: null as null | ((patch: AgentRoutePatch) => void),
  onSend: null as null | ((text: string) => void | Promise<unknown>),
  onStop: null as null | (() => void | Promise<unknown>),
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
  Composer: ({
    onSend,
    onStop,
  }: {
    onSend: (text: string) => void | Promise<unknown>;
    onStop: () => void | Promise<unknown>;
  }) => {
    mocks.onSend = onSend;
    mocks.onStop = onStop;
    return null;
  },
}));

vi.mock('./AgentRoutePicker', () => ({
  AgentRoutePicker: ({
    onChange,
    value,
    saving,
  }: {
    onChange: (patch: AgentRoutePatch) => void;
    value: { agent_name?: string | null; model?: string | null; reasoning_effort?: string | null } | null;
    saving?: boolean;
  }) => {
    mocks.onPatch = onChange;
    // The picker holds no selection of its own — the highlight IS this value — so
    // rendering it is how a test reads what the user is looking at.
    return (
      <div
        data-testid="route"
        data-agent={value?.agent_name ?? ''}
        data-model={value?.model ?? ''}
        data-effort={value?.reasoning_effort ?? ''}
        data-saving={saving ? 'yes' : 'no'}
      />
    );
  },
}));

import { ChatPage } from './ChatPage';
import { resetCoalescedWrites } from '../../lib/useCoalescedWrite';

const SESSION_ID = 'session-route';

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

const sessionRow = {
  id: SESSION_ID,
  title: 'Route ordering',
  agent_name: 'claude',
  agent_backend: 'claude',
  model: 'sonnet',
  reasoning_effort: null,
};

function settle() {
  return act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function messagePosts() {
  return mocks.apiFetch.mock.calls.filter(([url]) => String(url).includes('/messages'));
}

// What the header's picker is showing right now.
function shownRoute() {
  return screen.getByTestId('route').dataset;
}

async function mountChat() {
  render(
    <MemoryRouter initialEntries={[`/chat/${SESSION_ID}`]}>
      <Routes>
        <Route path="/chat/:sessionId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  await settle();
  // The live header only renders once the row is loaded, and the picker is where
  // a route pick comes from.
  if (!mocks.onPatch || !mocks.onSend || !mocks.onStop) {
    throw new Error('ChatPage did not mount its header/composer');
  }
}

describe('the chat row under an optimistic write', () => {
  beforeEach(() => {
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
    mocks.onPatch = null;
    mocks.onSend = null;
    mocks.onStop = null;
    vi.clearAllMocks();
    // The writer's store is module state (a session row outlives the page that
    // edits it), so each case starts from an empty one.
    resetCoalescedWrites();

    mocks.api.connectWorkbenchEvents.mockImplementation(() => () => {});
    mocks.api.getCachedSessionDraft.mockReturnValue(null);
    mocks.api.getSession.mockResolvedValue(sessionRow);
    mocks.api.getSessionBootstrap.mockResolvedValue({
      session: sessionRow,
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
    mocks.api.getTurnState.mockResolvedValue(idleTurnState);
    mocks.api.getWorkbenchPrefs.mockResolvedValue({});
    mocks.api.listSessionMessages.mockResolvedValue({ messages: [] });
    mocks.api.listSessionQueue.mockResolvedValue([]);
    mocks.api.onSessionArchived.mockReturnValue(() => {});
    // A turn started; no transcript row to graft in this harness.
    mocks.apiFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  });

  afterEach(() => {
    cleanup();
    resetCoalescedWrites();
    vi.unstubAllGlobals();
  });

  it('sends without waiting for the route write to land', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    await mountChat();

    act(() => mocks.onPatch!({ model: 'opus' }));
    expect(mocks.api.updateSession).toHaveBeenCalledWith(SESSION_ID, { model: 'opus' });

    act(() => {
      void mocks.onSend!('run it on opus');
    });
    await settle();
    // Enter stays live: the POST goes out while the PATCH is still deciding. That
    // leaves a real gap — the turn can be admitted on the route the row still
    // holds while the header already shows the new one — and the client cannot
    // close it, because routing a turn and sending it are separate requests
    // (``POST /messages`` accepts text, content and metadata only). Gating the
    // send would trade the gap for the very latency this path removes. Atomic
    // admission belongs to the server.
    expect(messagePosts()).toHaveLength(1);
    expect(String(messagePosts()[0][0])).toContain(`/api/sessions/${SESSION_ID}/messages`);
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      patchGate.resolve({ ...sessionRow, model: 'opus' });
      await patchGate.promise;
    });
    await settle();
    // And the send did not disturb the write: one PATCH for the pick, one row
    // re-read on settle.
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(1);
  });

  it('folds the picks made during a route write into a single follow-up PATCH', async () => {
    const gates = [deferred<unknown>(), deferred<unknown>()];
    let call = 0;
    mocks.api.updateSession.mockImplementation(() => gates[call++].promise);
    await mountChat();

    act(() => {
      mocks.onPatch!({ agent_name: 'codex', agent_variant: 'codex', model: 'gpt-5' });
      mocks.onPatch!({ reasoning_effort: 'high' });
      mocks.onPatch!({ model: 'gpt-5-codex' });
    });
    // One request per row: a second PATCH beside this one could land first and
    // undo it.
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(1);
    expect(mocks.api.updateSession).toHaveBeenCalledWith(SESSION_ID, {
      agent_name: 'codex',
      agent_variant: 'codex',
      model: 'gpt-5',
    });

    await act(async () => {
      gates[0].resolve({ ...sessionRow, agent_name: 'codex', model: 'gpt-5' });
      await gates[0].promise;
    });
    await settle();
    // Two picks, ONE follow-up PATCH carrying the union of their fields: they are
    // independent fields of one row, so neither takes the other hostage and
    // neither is dropped as "superseded".
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, {
      reasoning_effort: 'high',
      model: 'gpt-5-codex',
    });

    await act(async () => {
      gates[1].resolve({
        ...sessionRow,
        agent_name: 'codex',
        model: 'gpt-5-codex',
        reasoning_effort: 'high',
      });
      await gates[1].promise;
    });
    await settle();
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
  });

  it('puts the row back when the write is refused and the re-read fails', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    // The tab is offline for reads too — the case that decides whether the
    // rollback may be delegated to the re-read.
    mocks.api.getSession.mockRejectedValue(new Error('offline'));
    await mountChat();
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'sonnet', effort: '' });

    act(() => {
      mocks.onPatch!({ agent_name: 'codex', agent_variant: 'codex', model: 'gpt-5' });
      mocks.onPatch!({ reasoning_effort: 'high' });
    });
    // Both picks are on screen at once, ahead of the single request carrying the
    // first of them.
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', effort: 'high', saving: 'yes' });

    await act(async () => {
      patchGate.reject(new Error('nope'));
      await patchGate.promise.catch(() => undefined);
    });
    await settle();

    // The whole burst goes back to what the row held before it — the pick that
    // was refused AND the one dropped behind it, since neither is on the server.
    // ``refreshSessionRow`` is best-effort by contract (it swallows its own
    // failure), so a rollback delegated to it would leave this tab showing a
    // route the server refused with the indicator already gone.
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'sonnet', effort: '', saving: 'no' });
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(1);
  });
});
