/** @vitest-environment jsdom */

// The header's route edits apply within the click and persist behind them, which
// is the point of the optimistic path — but it means a prompt sent right after a
// model pick could be admitted while that PATCH is still waiting to flush, and
// the turn would run on the PREVIOUS route while the header already shows the new
// one. These tests pin the ordering at the boundary where it is observable: what
// reached the network, in which order.

import { act, cleanup, render } from '@testing-library/react';
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
  AgentRoutePicker: ({ onChange }: { onChange: (patch: AgentRoutePatch) => void }) => {
    mocks.onPatch = onChange;
    return null;
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

describe('sending after an optimistic route pick', () => {
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

  it('holds the prompt until the route write lands', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    await mountChat();

    act(() => mocks.onPatch!({ model: 'opus' }));
    expect(mocks.api.updateSession).toHaveBeenCalledWith(SESSION_ID, { model: 'opus' });

    act(() => {
      void mocks.onSend!('run it on opus');
    });
    await settle();
    // The turn must not be admitted on the route the user has already clicked
    // past — the header is showing `opus` at this point.
    expect(messagePosts()).toHaveLength(0);

    await act(async () => {
      patchGate.resolve({ ...sessionRow, model: 'opus' });
      await patchGate.promise;
    });
    await settle();
    expect(messagePosts()).toHaveLength(1);
    expect(String(messagePosts()[0][0])).toContain(`/api/sessions/${SESSION_ID}/messages`);
  });

  it('waits behind every write of a burst, and folds the picks made during one into a single PATCH', async () => {
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

    act(() => {
      void mocks.onSend!('and now with high effort');
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
    // That follow-up is only now in flight; admitting the turn here would run it
    // at the previous effort.
    expect(messagePosts()).toHaveLength(0);

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
    expect(messagePosts()).toHaveLength(1);
  });

  it('still sends when the route write fails, rather than holding the prompt hostage', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    await mountChat();

    act(() => mocks.onPatch!({ model: 'opus' }));
    act(() => {
      void mocks.onSend!('send me anyway');
    });
    await settle();
    expect(messagePosts()).toHaveLength(0);

    // Settle means "landed or failed loudly": the failure is already on the error
    // banner and the row is re-read, so the composer must not stay stuck.
    await act(async () => {
      patchGate.reject(new Error('offline'));
      await patchGate.promise.catch(() => undefined);
    });
    await settle();
    expect(messagePosts()).toHaveLength(1);
  });

  it('abandons a prompt the user stopped while the route write was still in flight', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    // Nothing has been admitted yet, so the controller has no turn to interrupt —
    // Stop is answered by clearing this tab's indicator.
    mocks.api.cancelSession.mockResolvedValue({ ok: false, code: 'not_in_flight' });
    await mountChat();

    act(() => mocks.onPatch!({ model: 'opus' }));
    let submission: unknown = 'pending';
    act(() => {
      void Promise.resolve(mocks.onSend!('never mind')).then((result) => {
        submission = result;
      });
    });
    await settle();
    expect(messagePosts()).toHaveLength(0);

    // Stop is live during the wait — the indicator went up on submit — and it is a
    // real cancel, so the prompt must not be POSTed once the route finally lands.
    await act(async () => {
      await mocks.onStop!();
    });
    expect(mocks.api.cancelSession).toHaveBeenCalledWith(SESSION_ID);

    await act(async () => {
      patchGate.resolve({ ...sessionRow, model: 'opus' });
      await patchGate.promise;
    });
    await settle();
    expect(messagePosts()).toHaveLength(0);
    // ``false`` hands the text back to the Composer as a retryable submission,
    // rather than swallowing what the user typed.
    expect(submission).toBe(false);
  });
});
