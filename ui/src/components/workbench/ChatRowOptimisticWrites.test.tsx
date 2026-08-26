/** @vitest-environment jsdom */

// The header's route edits apply within the click and persist behind them, which
// is the point of the optimistic path. These tests pin what that means at the two
// boundaries where it is observable — what reached the network, in which order
// (Enter is never held behind an in-flight route PATCH, the resulting admission
// gap being the server's to close; the picks made during one PATCH reach the row
// as a single merged follow-up) — and what the user is left looking at when the
// server refuses the burst.

import { useEffect } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';

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
    waitForAgentActivityConfigMutations: vi.fn(),
    onSessionArchived: vi.fn(),
    updateSession: vi.fn(),
    cancelSession: vi.fn(),
    recoverSessionDraftAfterRejectedSend: vi.fn(),
    reconcileSessionDraftAfterSend: vi.fn(),
    convergeSessionArchived: vi.fn(),
  },
  apiFetch: vi.fn(),
  // The live subscription's handlers, so a test can deliver the server's own
  // events — the arrival point that has no request to be ordered against.
  events: null as null | {
    onSessionActivity?: (data: { session_id: string; event: string; title?: string | null }) => void;
  },
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
import { resetOpenSessionRowWrites } from './sessionRowRefresh';

const SESSION_ID = 'session-route';
const OTHER_SESSION_ID = 'session-other';

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

// A second chat, for the cases about two rows being edited at once. Same route on
// purpose: only the id distinguishes whose rollback is whose.
const otherRow = { ...sessionRow, id: OTHER_SESSION_ID, title: 'The other chat' };

const rowsById: Record<string, typeof sessionRow> = {
  [SESSION_ID]: sessionRow,
  [OTHER_SESSION_ID]: otherRow,
};

// Switching chats keeps ChatPage mounted (same route, new param) — which is why a
// burst opened on the chat the user has left can still settle into this component.
let navigate: ((to: string) => void) | null = null;

function NavProbe() {
  const to = useNavigate();
  useEffect(() => {
    navigate = to;
  }, [to]);
  return null;
}

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

// The title is click-to-edit, so what the user is looking at is the label of the
// button it collapses to. The real field is driven here rather than synthesized:
// a rename and a route pick come from two different controls, which is the whole
// point of the cases below.
function renameFrom(current: string, next: string) {
  act(() => {
    fireEvent.click(screen.getByRole('button', { name: current }));
  });
  const input = screen.getByPlaceholderText('chat.titlePlaceholder');
  act(() => {
    fireEvent.change(input, { target: { value: next } });
    fireEvent.keyDown(input, { key: 'Enter' });
  });
}

async function mountChat() {
  render(
    <MemoryRouter initialEntries={[`/chat/${SESSION_ID}`]}>
      <NavProbe />
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
    mocks.events = null;
    navigate = null;
    vi.clearAllMocks();
    // The writer's store and the record of what an open write is holding are both
    // module state (a session row outlives the page that edits it), so each case
    // starts from an empty one.
    resetCoalescedWrites();
    resetOpenSessionRowWrites();

    mocks.api.connectWorkbenchEvents.mockImplementation((handlers: unknown) => {
      mocks.events = handlers as typeof mocks.events;
      return () => {};
    });
    mocks.api.getCachedSessionDraft.mockReturnValue(null);
    mocks.api.getSession.mockImplementation(async (id: string) => rowsById[id] ?? sessionRow);
    mocks.api.getSessionBootstrap.mockImplementation(async (id: string) => ({
      session: rowsById[id] ?? sessionRow,
      capabilities: { can_chat: true },
      agents: [],
      default_agent_name: null,
      config: { ui: {} },
      messages: [],
      next_before_id: null,
      turn_state: idleTurnState,
      queued: [],
      draft: { text: '' },
    }));
    mocks.api.getTurnState.mockResolvedValue(idleTurnState);
    mocks.api.getWorkbenchPrefs.mockResolvedValue({});
    mocks.api.listSessionMessages.mockResolvedValue({ messages: [] });
    mocks.api.listSessionQueue.mockResolvedValue([]);
    mocks.api.waitForAgentActivityConfigMutations.mockResolvedValue(undefined);
    mocks.api.onSessionArchived.mockReturnValue(() => {});
    // A turn started; no transcript row to graft in this harness.
    mocks.apiFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  });

  afterEach(() => {
    cleanup();
    resetCoalescedWrites();
    resetOpenSessionRowWrites();
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

  it('sends the pick waiting behind a refused write when it carries the whole route', async () => {
    const gates = [deferred<unknown>(), deferred<unknown>()];
    let call = 0;
    mocks.api.updateSession.mockImplementation(() => gates[call++].promise);
    await mountChat();

    // An effort click, then an Agent pick behind it while that request is still
    // deciding. The picker emits an Agent pick as the WHOLE route — the Agent, its
    // default model, its default effort — so it names every field it depends on.
    act(() => {
      mocks.onPatch!({ reasoning_effort: 'high' });
      mocks.onPatch!({
        agent_name: 'codex',
        agent_id: 'ag_codex',
        agent_backend: 'codex',
        agent_variant: 'codex',
        model: 'gpt-5',
        reasoning_effort: 'medium',
      });
    });
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', effort: 'medium', saving: 'yes' });

    await act(async () => {
      gates[0].reject(new Error('nope'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The refusal says nothing about the pick behind it: that pick replaces every
    // field the refused one carried, so it is coherent against the route the server
    // kept. Dropping it would revert the header to an Agent the user has moved off,
    // for a failure that never touched the Agent.
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, {
      agent_name: 'codex',
      agent_id: 'ag_codex',
      agent_backend: 'codex',
      agent_variant: 'codex',
      model: 'gpt-5',
      reasoning_effort: 'medium',
    });
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', effort: 'medium', saving: 'yes' });

    const committedRow = { ...sessionRow, agent_name: 'codex', model: 'gpt-5', reasoning_effort: 'medium' };
    mocks.api.getSession.mockResolvedValue(committedRow);
    await act(async () => {
      gates[1].resolve(committedRow);
      await gates[1].promise;
    });
    await settle();

    // One burst, one settle: the send that ENDED it committed, so nothing reverts.
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', effort: 'medium', saving: 'no' });
  });

  it('sends a second model pick behind a refused one, because it overwrites the field that was refused', async () => {
    const gates = [deferred<unknown>(), deferred<unknown>()];
    let call = 0;
    mocks.api.updateSession.mockImplementation(() => gates[call++].promise);
    await mountChat();

    // Two model clicks on a session that already has an explicit Agent. The picker
    // emits each as ``{model}`` ALONE — there is no default to pin — so neither
    // payload carries the whole route, and counting fields would call the second
    // one dependent on the first.
    act(() => {
      mocks.onPatch!({ model: 'gpt-5' });
      mocks.onPatch!({ model: 'gpt-5-codex' });
    });
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'gpt-5-codex', saving: 'yes' });

    await act(async () => {
      gates[0].reject(new Error('nope'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // What decides it is the RELATION: the pending patch restates every field the
    // refused one tried to write, so the refusal says nothing about it. The user's
    // newest model choice must reach the server, not be discarded because the
    // choice it replaced failed.
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, { model: 'gpt-5-codex' });

    const committedRow = { ...sessionRow, model: 'gpt-5-codex' };
    mocks.api.getSession.mockResolvedValue(committedRow);
    await act(async () => {
      gates[1].resolve(committedRow);
      await gates[1].promise;
    });
    await settle();
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'gpt-5-codex', saving: 'no' });
  });

  it('keeps the fields a partly committed burst persisted, reverting only the refused ones', async () => {
    const gates = [deferred<unknown>(), deferred<unknown>()];
    let call = 0;
    mocks.api.updateSession.mockImplementation(() => gates[call++].promise);
    await mountChat();

    act(() => {
      mocks.onPatch!({ agent_name: 'codex', agent_variant: 'codex', model: 'gpt-5' });
      mocks.onPatch!({ reasoning_effort: 'high' });
    });
    await act(async () => {
      gates[0].resolve({ ...sessionRow, agent_name: 'codex', model: 'gpt-5' });
      await gates[0].promise;
    });
    await settle();
    // The Agent switch is on the server; the effort follows it in one request.
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, { reasoning_effort: 'high' });

    // Offline for reads too, so the local revert is the only thing acting.
    mocks.api.getSession.mockRejectedValue(new Error('offline'));
    await act(async () => {
      gates[1].reject(new Error('nope'));
      await gates[1].promise.catch(() => undefined);
    });
    await settle();

    // Only the effort goes back. This burst committed in PARTS, and reverting to
    // the row it started from would undo an Agent switch the server is holding —
    // leaving the header showing a route that exists nowhere.
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', effort: '', saving: 'no' });
  });

  it('keeps a pick the server has not answered on the row the user comes back to', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    await mountChat();

    act(() => mocks.onPatch!({ agent_name: 'codex', agent_variant: 'codex', model: 'gpt-5' }));
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5' });

    // Away and back while that PATCH is still deciding. The chat reloads, and the
    // row it loads is the one the server still holds — legitimately, since the write
    // has not answered.
    await act(async () => {
      navigate!(`/chat/${OTHER_SESSION_ID}`);
    });
    await settle();
    await act(async () => {
      navigate!(`/chat/${SESSION_ID}`);
    });
    await settle();

    // The pick is still what the user is looking at. The read that reopened this
    // chat is older than the write in flight, and the per-session read fence was
    // replaced by the navigation, so the record of the open write is what defends it.
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', saving: 'yes' });

    // Which is what makes the next click safe: the picker emits an effort change as
    // ``{reasoning_effort}`` alone, so composing it against the reopened row would
    // fold an effort chosen for claude in behind the switch to codex — and persist
    // it on top of that switch.
    act(() => mocks.onPatch!({ reasoning_effort: 'high' }));
    expect(shownRoute()).toMatchObject({ agent: 'codex', model: 'gpt-5', effort: 'high' });

    await act(async () => {
      patchGate.resolve({ ...sessionRow, agent_name: 'codex', model: 'gpt-5' });
      await patchGate.promise;
    });
    await settle();
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, { reasoning_effort: 'high' });
    // And the defence ends with the write. This harness's row read still answers
    // with the route the chat started on, so the settle re-read is visible: an
    // overlay left standing would pin a pick nothing is writing any more.
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'sonnet', effort: '', saving: 'no' });
  });

  it('sends a route pick made behind a rename as its own request, and keeps it when the rename is refused', async () => {
    const gates: Deferred<unknown>[] = [];
    mocks.api.updateSession.mockImplementation(() => {
      const gate = deferred<unknown>();
      gates.push(gate);
      return gate.promise;
    });
    await mountChat();

    renameFrom('Route ordering', 'Renamed');
    expect(mocks.api.updateSession).toHaveBeenCalledWith(SESSION_ID, { title: 'Renamed' });

    // Two requests, not one merged patch and not one waiting behind the other.
    // The title and the route overwrite nothing of each other's, and the server
    // writes only the columns each PATCH names.
    act(() => mocks.onPatch!({ model: 'opus' }));
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, { model: 'opus' });

    // Offline for reads, so only the local rollback is acting.
    mocks.api.getSession.mockRejectedValue(new Error('offline'));
    await act(async () => {
      gates[0].reject(new Error('nope'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The rename goes back. The pick does not: it is a different write, still in
    // flight, and nothing about it was refused. While the two shared one writer
    // key, this rejection ENDED that key's burst — which dropped the pick before
    // it was ever sent AND reverted it on screen.
    expect(screen.getByRole('button', { name: 'Route ordering' })).toBeTruthy();
    expect(shownRoute()).toMatchObject({ model: 'opus', saving: 'yes' });

    await act(async () => {
      gates[1].resolve({ ...sessionRow, model: 'opus' });
      await gates[1].promise;
    });
    await settle();
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(shownRoute()).toMatchObject({ model: 'opus', saving: 'no' });
  });

  it('keeps a rename made behind a route pick when the pick is refused', async () => {
    const gates: Deferred<unknown>[] = [];
    mocks.api.updateSession.mockImplementation(() => {
      const gate = deferred<unknown>();
      gates.push(gate);
      return gate.promise;
    });
    await mountChat();

    act(() => mocks.onPatch!({ agent_name: 'codex', agent_variant: 'codex', model: 'gpt-5' }));
    renameFrom('Route ordering', 'Renamed');
    expect(mocks.api.updateSession).toHaveBeenCalledTimes(2);
    expect(mocks.api.updateSession).toHaveBeenLastCalledWith(SESSION_ID, { title: 'Renamed' });

    mocks.api.getSession.mockRejectedValue(new Error('offline'));
    await act(async () => {
      gates[0].reject(new Error('nope'));
      await gates[0].promise.catch(() => undefined);
    });
    await settle();

    // The route reverts and the rename stays — including its request, which the
    // refusal has no claim over. The indicator is still on, because the header
    // shows one for the row and the row is still being written.
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'sonnet', saving: 'yes' });
    expect(screen.getByRole('button', { name: 'Renamed' })).toBeTruthy();

    await act(async () => {
      gates[1].resolve({ ...sessionRow, title: 'Renamed' });
      await gates[1].promise;
    });
    await settle();
    expect(screen.getByRole('button', { name: 'Renamed' })).toBeTruthy();
    expect(shownRoute()).toMatchObject({ saving: 'no' });
  });

  it('keeps a pending rename when a session event carries the title the server still holds', async () => {
    const patchGate = deferred<unknown>();
    mocks.api.updateSession.mockReturnValue(patchGate.promise);
    await mountChat();

    renameFrom('Route ordering', 'Renamed');
    expect(mocks.api.updateSession).toHaveBeenCalledWith(SESSION_ID, { title: 'Renamed' });

    // The rename broadcast for THIS row, carrying the title as the server still
    // holds it. An event has no request of its own, so no read fence orders it
    // against the write — which is why the defence cannot live in the fence.
    await act(async () => {
      mocks.events!.onSessionActivity!({ session_id: SESSION_ID, event: 'updated', title: 'Route ordering' });
    });
    await settle();
    // Installing it would not just flicker: it re-seeds the header's editor from
    // the prop, so a user still typing loses what they typed.
    expect(screen.getByRole('button', { name: 'Renamed' })).toBeTruthy();

    await act(async () => {
      patchGate.resolve({ ...sessionRow, title: 'Renamed' });
      await patchGate.promise;
    });
    await settle();
    // And the defence ends with the write: this harness's row read still answers
    // with the stored title, so an overlay left standing would be visible here.
    expect(screen.getByRole('button', { name: 'Route ordering' })).toBeTruthy();
  });

  it('keeps each chat rollback to itself when two rows have a write in flight', async () => {
    const gates = new Map<string, Deferred<unknown>>();
    mocks.api.updateSession.mockImplementation((id: string) => {
      const gate = deferred<unknown>();
      gates.set(id, gate);
      return gate.promise;
    });
    await mountChat();

    // A pick on the first chat, left in flight.
    act(() => mocks.onPatch!({ model: 'opus' }));
    expect(gates.has(SESSION_ID)).toBe(true);

    // The user switches chats while it is still deciding. ChatPage stays mounted,
    // so both bursts are live in the same component.
    await act(async () => {
      navigate!(`/chat/${OTHER_SESSION_ID}`);
    });
    await settle();
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'sonnet' });

    act(() => mocks.onPatch!({ model: 'haiku' }));
    expect(shownRoute()).toMatchObject({ model: 'haiku', saving: 'yes' });
    expect(gates.has(OTHER_SESSION_ID)).toBe(true);

    mocks.api.getSession.mockRejectedValue(new Error('offline'));
    // The FIRST chat's write fails. Its row is not on screen, so there is nothing
    // to revert there — but its settle must not take the open chat's rollback with
    // it. One slot for both would do exactly that, and the failure below would then
    // leave a refused model on screen with the indicator already gone.
    await act(async () => {
      gates.get(SESSION_ID)!.reject(new Error('nope'));
      await gates.get(SESSION_ID)!.promise.catch(() => undefined);
    });
    await settle();
    expect(shownRoute()).toMatchObject({ model: 'haiku', saving: 'yes' });

    await act(async () => {
      gates.get(OTHER_SESSION_ID)!.reject(new Error('nope'));
      await gates.get(OTHER_SESSION_ID)!.promise.catch(() => undefined);
    });
    await settle();
    expect(shownRoute()).toMatchObject({ agent: 'claude', model: 'sonnet', saving: 'no' });
  });
});
