// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchEventHandlers } from '@/context/ApiContext';

// Contract: the desktop app comes forward only for a step this window handed to the browser, once,
// when that step has finished there, within the hand-off window, and never while the user is
// already in the app. In-memory state is per module instance, so every case loads a fresh one.

const T0 = Date.parse('2026-11-16T10:00:00Z');
const MINUTE = 60_000;

type Request = { id: string; status: string; expires_at: string | null };

function setup() {
  let handlers: WorkbenchEventHandlers = {};
  const requests = new Map<string, Request>();
  let secrets: Array<{ name: string; created_at: string }> = [];
  const api = {
    connectWorkbenchEvents: vi.fn((next: WorkbenchEventHandlers) => { handlers = next; return () => undefined; }),
    getVaultRequest: vi.fn(async (id: string) => {
      const request = requests.get(id);
      return request ? { ok: true, request } : { ok: false, code: 'request_not_found' };
    }),
    listVaultSecrets: vi.fn(async () => ({ ok: true, secrets })),
  };
  const bringForward = vi.fn();
  return {
    api, bringForward, requests,
    setSecrets: (next: typeof secrets) => { secrets = next; },
    vaults: (data: Parameters<NonNullable<WorkbenchEventHandlers['onVaultsUpdated']>>[0]) => handlers.onVaultsUpdated?.(data),
    connected: () => handlers.onConnected?.(),
  };
}

async function load() {
  vi.resetModules();
  return import('./vaultHandoffReturn');
}

const settle = () => vi.advanceTimersByTimeAsync(0);
const iso = (ms: number) => new Date(ms).toISOString();

beforeEach(() => {
  vi.useFakeTimers({ now: T0 });
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  Object.defineProperty(window, '__AVIBE_DESKTOP_FOCUS__', { configurable: true, value: true });
  (window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = { invoke: vi.fn() };
  vi.spyOn(document, 'hasFocus').mockReturnValue(false);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_FOCUS__?: true }).__AVIBE_DESKTOP_FOCUS__;
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe('bringing the desktop app back after a browser hand-off', () => {
  it.each([
    { status: 'approved', returns: true },
    { status: 'denied', returns: true },
    { status: 'fulfilled', returns: true },
    { status: 'completed', returns: true },
    { status: 'failed', returns: true },
    // Still the browser's turn: a sign request is producing the signature it was just allowed.
    { status: 'signing', returns: false },
    { status: 'pending', returns: false },
  ])('a handed-off request now $status brings the app back: $returns', async ({ status, returns }) => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'pending', expires_at: iso(T0 + 30 * MINUTE) });
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_1' });
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();

    s.requests.set('vrq_1', { id: 'vrq_1', status, expires_at: iso(T0 + 30 * MINUTE) });
    s.vaults({ scope: 'request', request_id: 'vrq_1', request_status: status });
    await settle();
    expect(s.bringForward).toHaveBeenCalledTimes(returns ? 1 : 0);

    // Once each: the same request settling again, or a later event about it, does nothing more.
    s.vaults({ scope: 'grant', request_id: 'vrq_1', grant_status: 'expired' });
    await settle();
    expect(s.bringForward).toHaveBeenCalledTimes(returns ? 1 : 0);
  });

  it('ignores requests this window did not hand off', async () => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.requests.set('vrq_other', { id: 'vrq_other', status: 'approved', expires_at: null });
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_mine' });
    s.vaults({ scope: 'request', request_id: 'vrq_other', request_status: 'approved' });
    s.connected();
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();
    expect(s.api.getVaultRequest).not.toHaveBeenCalledWith('vrq_other');
  });

  it('forgets the step without stealing focus when the user is already in the app', async () => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'pending', expires_at: null });
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_1' });
    await settle();
    vi.mocked(document.hasFocus).mockReturnValue(true);
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'denied', expires_at: null });
    s.vaults({ scope: 'request', request_id: 'vrq_1', request_status: 'denied' });
    await settle();
    vi.mocked(document.hasFocus).mockReturnValue(false);
    s.vaults({ scope: 'request', request_id: 'vrq_1', request_status: 'denied' });
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();
  });

  it('brings the app back when a handed-off request expires, though expiry sends no event', async () => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    // The server sweeps lazily: still `pending` on read, but past its expiry.
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'pending', expires_at: iso(T0 + 10 * MINUTE) });
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_1' });
    await settle();
    await vi.advanceTimersByTimeAsync(10 * MINUTE - 1);
    expect(s.bringForward).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(s.bringForward).toHaveBeenCalledTimes(1);
  });

  it('lets a step go once the hand-off window has passed', async () => {
    const { HANDOFF_RETURN_WINDOW_MS, noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'pending', expires_at: null });
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_1' });
    await settle();
    await vi.advanceTimersByTimeAsync(HANDOFF_RETURN_WINDOW_MS + 1);
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'approved', expires_at: null });
    s.vaults({ scope: 'request', request_id: 'vrq_1', request_status: 'approved' });
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();
  });

  it('catches up on a request that finished while the stream was down', async () => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'pending', expires_at: null });
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_1' });
    await settle();
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'approved', expires_at: null });
    s.connected();
    await settle();
    expect(s.bringForward).toHaveBeenCalledTimes(1);
  });

  it.each([
    { added: 'API_KEY', handedOff: 'API_KEY', returns: true },
    { added: 'OTHER_KEY', handedOff: 'API_KEY', returns: false },
    { added: 'ANY_NEW_KEY', handedOff: undefined, returns: true },
  ])('an Add handed off for $handedOff and $added created brings the app back: $returns', async ({ added, handedOff, returns }) => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    // A secret that already existed is not the Add's result, whatever event mentions it.
    s.setSecrets([{ name: 'OLD_KEY', created_at: iso(T0 - 60 * MINUTE) }]);
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff({ kind: 'add', name: handedOff });
    s.vaults({ scope: 'secret', secret_name: 'OLD_KEY' });
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(2 * MINUTE);
    s.setSecrets([{ name: 'OLD_KEY', created_at: iso(T0 - 60 * MINUTE) }, { name: added, created_at: iso(Date.now()) }]);
    s.vaults({ scope: 'secret', secret_name: added });
    await settle();
    expect(s.bringForward).toHaveBeenCalledTimes(returns ? 1 : 0);
  });

  it.each([
    { step: { kind: 'edit' as const, secretName: 'API_KEY' } },
    { step: { kind: 'reveal' as const, secretName: 'API_KEY' } },
  ])('does not follow a $step.kind hand-off', async ({ step }) => {
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.setSecrets([{ name: 'API_KEY', created_at: iso(T0 + MINUTE) }]);
    watchVaultHandoffs(s.api, s.bringForward);
    noteVaultHandoff(step);
    s.vaults({ scope: 'secret', secret_name: 'API_KEY' });
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();
  });

  it('remembers nothing in a shell that cannot bring its window forward', async () => {
    delete (window as { __AVIBE_DESKTOP_FOCUS__?: true }).__AVIBE_DESKTOP_FOCUS__;
    const { noteVaultHandoff, watchVaultHandoffs } = await load();
    const s = setup();
    s.requests.set('vrq_1', { id: 'vrq_1', status: 'approved', expires_at: null });
    noteVaultHandoff({ kind: 'request', requestId: 'vrq_1' });
    watchVaultHandoffs(s.api, s.bringForward);
    s.vaults({ scope: 'request', request_id: 'vrq_1', request_status: 'approved' });
    await settle();
    expect(s.bringForward).not.toHaveBeenCalled();
  });
});
