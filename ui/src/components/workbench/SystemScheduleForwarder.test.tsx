/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SystemScheduleForwarder } from './SystemScheduleForwarder';

const { apiFetch, native, activity } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  native: {
    hasNativeCapability: vi.fn(),
    nativeAppInfo: vi.fn(),
    nativeSystemScheduleRequest: vi.fn(),
    nativeSystemScheduleCancel: vi.fn(),
  },
  activity: { onPageReactivated: vi.fn() },
}));

const state = vi.hoisted(() => ({ canChat: true }));

vi.mock('@/context/InstanceAuthorizationContext', () => ({
  useInstanceAuthorization: () => ({
    capabilities: { can_chat: state.canChat },
  }),
}));
vi.mock('@/lib/apiFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/apiFetch')>(),
  apiFetch,
}));
vi.mock('@/lib/native', () => native);
vi.mock('@/lib/pageActivity', () => activity);

const contractVectors = JSON.parse(readFileSync(
  resolve(process.cwd(), '../tests/fixtures/system_schedule_contract_vectors.json'),
  'utf8',
));

const request = {
  requestId: 'request-1',
  operationId: 'operation-1',
  kind: 'alarm' as const,
  title: 'Take medicine',
  fireAt: '2026-11-18T01:00:00.000000Z',
  timeZone: 'America/Los_Angeles',
  recurrence: null,
  instanceId: 'instance-1',
  sessionId: 'session-1',
  expiresAt: '2026-11-17T23:05:00.000000Z',
};
const receipt = {
  contract: 1 as const,
  requestId: request.requestId,
  operationId: request.operationId,
  kind: request.kind,
  state: 'scheduled' as const,
  systemIdentifier: 'opaque-1',
  fireAt: request.fireAt,
  timeZone: request.timeZone,
  authorization: 'authorized' as const,
  error: null,
};

const createClaim = {
  action: 'create' as const,
  generation: 1,
  purpose: 'user' as const,
  leaseToken: 'lease-1',
  leaseExpiresAt: request.expiresAt,
  request,
};

function callsFor(path: string) {
  return apiFetch.mock.calls.filter(([calledPath]) => calledPath === path);
}

beforeEach(() => {
  state.canChat = true;
  native.hasNativeCapability.mockReturnValue(true);
  native.nativeAppInfo.mockResolvedValue({
    contract: 1,
    platform: 'ios',
    shellVersion: '0.1.0',
    capabilities: { systemSchedule: 1 },
    instance: { id: 'instance-1', name: 'Studio' },
    device: { id: 'device-1' },
  });
  native.nativeSystemScheduleRequest.mockResolvedValue(receipt);
  native.nativeSystemScheduleCancel.mockResolvedValue(receipt);
  activity.onPageReactivated.mockReturnValue(() => undefined);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('SystemScheduleForwarder', () => {
  it.each(['fetch', 'body'] as const)(
    'releases a stalled claim %s without overlapping owners or losing cancel and receipt progress',
    async (phase) => {
      vi.useFakeTimers();
      const cancelRequest = {
        requestId: 'cancel-after-stall',
        operationId: request.operationId,
        kind: request.kind,
        purpose: 'user' as const,
      };
      const cancelReceipt = {
        ...receipt,
        requestId: cancelRequest.requestId,
        state: 'cancelled' as const,
      };
      native.nativeSystemScheduleCancel.mockResolvedValue(cancelReceipt);
      let claimCalls = 0;
      let activeClaims = 0;
      let maxActiveClaims = 0;
      let abortedClaims = 0;
      const acceptedLeases = new Set<string>();
      const uploadedReceipts: (typeof receipt | typeof cancelReceipt)[] = [];
      let createReceiptAttempts = 0;
      apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
        const body = JSON.parse(init.body as string);
        if (path === '/api/system-schedule/receipt') {
          uploadedReceipts.push(body.receipt);
          if (body.lease_token === createClaim.leaseToken && ++createReceiptAttempts === 1) {
            throw new Error('receipt temporarily offline');
          }
          acceptedLeases.add(body.lease_token);
          return Response.json({ ok: true });
        }
        claimCalls += 1;
        activeClaims += 1;
        maxActiveClaims = Math.max(maxActiveClaims, activeClaims);
        if (claimCalls === 2) {
          const stalled = new Promise<Response>((_, reject) => {
            init.signal?.addEventListener('abort', () => {
              activeClaims -= 1;
              abortedClaims += 1;
              reject(init.signal?.reason);
            }, { once: true });
          });
          if (phase === 'fetch') return stalled;
          return { ok: true, json: () => stalled };
        }
        const claim = claimCalls === 1
          ? createClaim
          : !acceptedLeases.has('lease-cancel')
            && !body.exclude_lease_tokens.includes('lease-cancel')
            ? {
              ...createClaim,
              action: 'cancel',
              leaseToken: 'lease-cancel',
              request: cancelRequest,
            }
            : null;
        const response = Response.json({ ok: true, claim });
        const readBody = response.json.bind(response);
        response.json = async () => {
          try {
            return await readBody();
          } finally {
            activeClaims -= 1;
          }
        };
        return response;
      });

      render(<SystemScheduleForwarder />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(claimCalls).toBe(2);
      expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();
      expect(native.nativeSystemScheduleCancel).not.toHaveBeenCalled();
      expect(uploadedReceipts).toEqual([]);

      await act(async () => { await vi.advanceTimersByTimeAsync(14_999); });
      expect(claimCalls).toBe(2);
      expect(maxActiveClaims).toBe(1);
      expect(uploadedReceipts).toEqual([]);
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(abortedClaims).toBe(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(10_005); });

      expect(acceptedLeases).toEqual(new Set([createClaim.leaseToken, 'lease-cancel']));
      expect(createReceiptAttempts).toBe(2);
      expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();
      expect(native.nativeSystemScheduleCancel).toHaveBeenCalledOnce();
      expect(native.nativeSystemScheduleCancel).toHaveBeenCalledWith(cancelRequest);
      expect(maxActiveClaims).toBe(1);
      expect(activeClaims).toBe(0);
      expect(uploadedReceipts).toEqual([receipt, receipt, cancelReceipt]);
    },
  );

  it('shares the closed create, cancel, and terminal receipt vectors across layers', () => {
    expect(contractVectors.contract).toBe(1);
    expect(contractVectors.createRequests.map((item: { title: string }) => item.title)).toEqual([
      'Take medicine',
      '吃药 💊',
    ]);
    expect(contractVectors.createRequests.every((item: Record<string, unknown>) => (
      Object.keys(item).sort().join(',')
        === [
          'expiresAt',
          'fireAt',
          'instanceId',
          'kind',
          'operationId',
          'recurrence',
          'requestId',
          'sessionId',
          'timeZone',
          'title',
        ].join(',')
    ))).toBe(true);
    expect(contractVectors.cancelRequests.map((item: { purpose: string }) => item.purpose)).toEqual([
      'user',
      'user',
    ]);
    expect(new Set(
      contractVectors.receipts.map((item: { state: string }) => item.state),
    )).toEqual(new Set([
      'scheduled',
      'user_denied',
      'permission_denied',
      'unsupported',
      'failed',
      'expired',
      'stale_context',
      'cancelled',
    ]));
  });

  it('claims for the native identity, invokes the bridge once, and returns its receipt', async () => {
    let claimed = false;
    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/system-schedule/claim') {
        if (claimed) return Response.json({ ok: true, claim: null });
        claimed = true;
        return Response.json({ ok: true, claim: createClaim });
      }
      return Response.json({ ok: true });
    });

    render(<SystemScheduleForwarder />);

    await waitFor(() => expect(callsFor('/api/system-schedule/receipt')).toHaveLength(1));
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledWith(request);
    expect(native.nativeSystemScheduleCancel).not.toHaveBeenCalled();
    expect(JSON.parse(callsFor('/api/system-schedule/claim')[0][1].body)).toEqual({
      instance_id: 'instance-1',
      device_id: 'device-1',
      exclude_lease_tokens: [],
      allow_create: true,
    });
    expect(JSON.parse(callsFor('/api/system-schedule/receipt')[0][1].body)).toEqual({
      instance_id: 'instance-1',
      device_id: 'device-1',
      lease_token: 'lease-1',
      receipt,
    });
  });

  it('forwards cancellation claims through the cancellation bridge', async () => {
    const cancelRequest = {
      requestId: 'cancel-1',
      operationId: request.operationId,
      kind: request.kind,
      purpose: 'user' as const,
    };
    const cancelReceipt = {
      ...receipt,
      requestId: cancelRequest.requestId,
      state: 'cancelled' as const,
    };
    native.nativeSystemScheduleCancel.mockResolvedValue(cancelReceipt);
    let claimed = false;
    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/system-schedule/claim') {
        if (claimed) return Response.json({ ok: true, claim: null });
        claimed = true;
        return Response.json({
          ok: true,
          claim: {
            action: 'cancel',
            generation: 1,
            purpose: 'user',
            leaseToken: 'lease-cancel',
            leaseExpiresAt: request.expiresAt,
            request: cancelRequest,
          },
        });
      }
      return Response.json({ ok: true });
    });

    render(<SystemScheduleForwarder />);

    await waitFor(() => expect(callsFor('/api/system-schedule/receipt')).toHaveLength(1));
    expect(native.nativeSystemScheduleCancel).toHaveBeenCalledWith(cancelRequest);
    expect(native.nativeSystemScheduleRequest).not.toHaveBeenCalled();
    expect(JSON.parse(callsFor('/api/system-schedule/receipt')[0][1].body)).toEqual({
      instance_id: 'instance-1',
      device_id: 'device-1',
      lease_token: 'lease-cancel',
      receipt: cancelReceipt,
    });
  });

  it.each([
    { action: 'create', resume: 'poll' },
    { action: 'create', resume: 'reactivation' },
    { action: 'cancel', resume: 'poll' },
    { action: 'cancel', resume: 'reactivation' },
  ] as const)('backs off a rejected $action until $resume permits replay', async ({
    action, resume,
  }) => {
    vi.useFakeTimers();
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });
    const cancelRequest = {
      requestId: 'cancel-rejected',
      operationId: request.operationId,
      kind: request.kind,
      purpose: 'user' as const,
    };
    const claim = action === 'create'
      ? createClaim
      : { ...createClaim, action, request: cancelRequest };
    const recoveredReceipt = action === 'create'
      ? receipt
      : { ...receipt, requestId: cancelRequest.requestId, state: 'cancelled' as const };
    const nativeCall = action === 'create'
      ? native.nativeSystemScheduleRequest
      : native.nativeSystemScheduleCancel;
    let admitted = false;
    let rejectNative: ((reason: Error) => void) | undefined;
    nativeCall.mockImplementation(() => admitted
      ? Promise.resolve(recoveredReceipt)
      : new Promise((_, reject) => { rejectNative = reject; }));
    let accepted = false;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/claim') {
        const body = JSON.parse(init.body as string);
        return Response.json({
          ok: true,
          claim: accepted || body.exclude_lease_tokens.includes(claim.leaseToken)
            ? null
            : claim,
        });
      }
      accepted = true;
      return Response.json({ ok: true });
    });

    render(<SystemScheduleForwarder />);
    await act(async () => undefined);
    expect(nativeCall).toHaveBeenCalledOnce();
    expect(callsFor('/api/system-schedule/claim')).toHaveLength(2);

    await act(async () => { rejectNative?.(new Error('forbidden')); });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(resume === 'poll' ? 4_999 : 4_000);
    });

    expect(nativeCall).toHaveBeenCalledOnce();
    expect(callsFor('/api/system-schedule/claim')).toHaveLength(2);
    expect(callsFor('/api/system-schedule/receipt')).toHaveLength(0);

    admitted = true;
    await act(async () => {
      if (resume === 'reactivation') reactivate?.();
      await vi.advanceTimersByTimeAsync(resume === 'poll' ? 5 : 0);
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(5); });

    expect(nativeCall).toHaveBeenCalledTimes(2);
    expect(nativeCall).toHaveBeenNthCalledWith(2, claim.request);
    expect(callsFor('/api/system-schedule/receipt')).toHaveLength(1);
    expect(JSON.parse(callsFor('/api/system-schedule/receipt')[0][1].body)).toEqual({
      instance_id: 'instance-1',
      device_id: 'device-1',
      lease_token: claim.leaseToken,
      receipt: recoveredReceipt,
    });
  });

  it.each([
    { action: 'create', code: 'local_state_write_failed', systemIdentifier: receipt.systemIdentifier },
    { action: 'create', code: 'local_state_unavailable', systemIdentifier: null },
    { action: 'cancel', code: 'local_state_write_failed', systemIdentifier: receipt.systemIdentifier },
    { action: 'cancel', code: 'local_state_unavailable', systemIdentifier: null },
  ] as const)('keeps $action sticky across $code until Native recovery', async ({
    action, code, systemIdentifier,
  }) => {
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });
    const cancelRequest = {
      requestId: 'cancel-recovery',
      operationId: request.operationId,
      kind: request.kind,
      purpose: 'user' as const,
    };
    const claim = action === 'create'
      ? createClaim
      : { ...createClaim, action, request: cancelRequest };
    const recoveredReceipt = action === 'create'
      ? receipt
      : { ...receipt, requestId: cancelRequest.requestId, state: 'cancelled' as const };
    const localFailure = {
      ...recoveredReceipt,
      systemIdentifier,
      state: 'failed' as const,
      error: { code, message: 'Local alarm and reminder records are not available.' },
    };
    let recovered = false;
    const nativeCall = action === 'create'
      ? native.nativeSystemScheduleRequest
      : native.nativeSystemScheduleCancel;
    nativeCall.mockImplementation(async () => recovered ? recoveredReceipt : localFailure);
    let offered = false;
    let accepted = false;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/claim') {
        const body = JSON.parse(init.body as string);
        if (accepted || offered || body.exclude_lease_tokens.includes(claim.leaseToken)) {
          return Response.json({ ok: true, claim: null });
        }
        offered = true;
        return Response.json({ ok: true, claim });
      }
      accepted = true;
      return Response.json({ ok: true });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(callsFor('/api/system-schedule/claim').length).toBeGreaterThanOrEqual(2));
    expect(nativeCall).toHaveBeenCalledOnce();

    offered = false;
    reactivate?.();
    await waitFor(() => expect(callsFor('/api/system-schedule/claim').length).toBeGreaterThan(2));
    expect(callsFor('/api/system-schedule/receipt')).toHaveLength(0);
    expect(JSON.parse(callsFor('/api/system-schedule/claim')[2][1].body)
      .exclude_lease_tokens).not.toContain(claim.leaseToken);
    expect(nativeCall).toHaveBeenCalledTimes(2);

    recovered = true;
    offered = false;
    reactivate?.();
    await waitFor(() => expect(callsFor('/api/system-schedule/receipt')).toHaveLength(1));
    expect(JSON.parse(callsFor('/api/system-schedule/receipt')[0][1].body)).toEqual({
      instance_id: 'instance-1',
      device_id: 'device-1',
      lease_token: claim.leaseToken,
      receipt: recoveredReceipt,
    });
    expect(nativeCall).toHaveBeenCalledTimes(3);
  });

  it.each([
    { action: 'create', state: 'user_denied', code: 'user_denied' },
    { action: 'create', state: 'expired', code: 'request_expired' },
    { action: 'create', state: 'failed', code: 'operation_conflict' },
    { action: 'create', state: 'stale_context', code: 'stale_context' },
    { action: 'cancel', state: 'failed', code: 'item_not_found' },
  ] as const)('still uploads the ordinary $action terminal error $code', async ({
    action, state: terminalState, code,
  }) => {
    const claimRequest = action === 'create' ? request : {
      requestId: 'cancel-terminal',
      operationId: request.operationId,
      kind: request.kind,
      purpose: 'user' as const,
    };
    const terminalReceipt = {
      ...receipt,
      requestId: claimRequest.requestId,
      state: terminalState,
      systemIdentifier: null,
      error: { code, message: 'A terminal Native outcome.' },
    };
    native.nativeSystemScheduleRequest.mockResolvedValue(terminalReceipt);
    native.nativeSystemScheduleCancel.mockResolvedValue(terminalReceipt);
    let accepted = false;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/claim') {
        const body = JSON.parse(init.body as string);
        return Response.json({
          ok: true,
          claim: accepted || body.exclude_lease_tokens.includes(createClaim.leaseToken)
            ? null
            : { ...createClaim, action, request: claimRequest },
        });
      }
      accepted = true;
      return Response.json({ ok: true });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(callsFor('/api/system-schedule/receipt')).toHaveLength(1));
    expect(JSON.parse(callsFor('/api/system-schedule/receipt')[0][1].body).receipt)
      .toEqual(terminalReceipt);
  });

  it('retries an undelivered receipt without invoking the same native lease again', async () => {
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });
    let receiptAttempts = 0;
    let receiptAccepted = false;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/claim') {
        if (receiptAccepted) return Response.json({ ok: true, claim: null });
        const body = JSON.parse(init.body as string);
        if (body.exclude_lease_tokens.includes(createClaim.leaseToken)) {
          return Response.json({ ok: true, claim: null });
        }
        return Response.json({ ok: true, claim: createClaim });
      }
      receiptAttempts += 1;
      if (receiptAttempts === 1) throw new Error('offline');
      receiptAccepted = true;
      return Response.json({ ok: true });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(receiptAttempts).toBe(1));

    reactivate?.();
    await waitFor(() => expect(receiptAttempts).toBe(2));
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();
  });

  // I3 review E7: a hung receipt cannot hold the claim mutex or suppress another lease.
  it('does not let one hung receipt block a cancel claim or another receipt', async () => {
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });
    const cancelRequest = {
      requestId: 'cancel-after-hung-receipt',
      operationId: request.operationId,
      kind: request.kind,
      purpose: 'user' as const,
    };
    const cancelReceipt = {
      ...receipt,
      requestId: cancelRequest.requestId,
      state: 'cancelled' as const,
    };
    native.nativeSystemScheduleCancel.mockResolvedValue(cancelReceipt);

    let firstClaimed = false;
    let allowCancel = false;
    let cancelClaimed = false;
    let resolveHungReceipt: ((response: Response) => void) | undefined;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      if (path === '/api/system-schedule/receipt') {
        if (body.lease_token === createClaim.leaseToken) {
          return new Promise<Response>((resolve) => { resolveHungReceipt = resolve; });
        }
        return Response.json({ ok: true });
      }
      if (!firstClaimed) {
        firstClaimed = true;
        return Response.json({ ok: true, claim: createClaim });
      }
      if (allowCancel && !cancelClaimed) {
        cancelClaimed = true;
        return Response.json({
          ok: true,
          claim: {
            action: 'cancel',
            generation: 1,
            purpose: 'user',
            leaseToken: 'lease-cancel-after-hung',
            leaseExpiresAt: request.expiresAt,
            request: cancelRequest,
          },
        });
      }
      return Response.json({ ok: true, claim: null });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(callsFor('/api/system-schedule/receipt')).toHaveLength(1));
    expect(callsFor('/api/system-schedule/receipt')[0][1].signal).toBeInstanceOf(AbortSignal);

    allowCancel = true;
    reactivate?.();

    await waitFor(() => expect(native.nativeSystemScheduleCancel).toHaveBeenCalledWith(cancelRequest));
    await waitFor(() => expect(
      callsFor('/api/system-schedule/receipt').some(([, init]) => (
        JSON.parse(init.body).lease_token === 'lease-cancel-after-hung'
      )),
    ).toBe(true));

    resolveHungReceipt?.(Response.json({ ok: true }));
  });

  it('claims cancellation while creation awaits confirmation and keeps both receipts by lease', async () => {
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });
    let resolveCreate: ((value: typeof receipt) => void) | undefined;
    native.nativeSystemScheduleRequest.mockReturnValue(new Promise((resolve) => {
      resolveCreate = resolve;
    }));
    const cancelRequest = {
      requestId: 'cancel-1',
      operationId: request.operationId,
      kind: request.kind,
      purpose: 'user' as const,
    };
    const cancelReceipt = {
      ...receipt,
      requestId: cancelRequest.requestId,
      state: 'cancelled' as const,
    };
    const createCancelledReceipt = {
      ...receipt,
      state: 'cancelled' as const,
    };
    native.nativeSystemScheduleCancel.mockResolvedValue(cancelReceipt);

    let claimCalls = 0;
    let allowCancelReceipt = false;
    let createReceiptAccepted = false;
    let cancelReceiptAccepted = false;
    const receiptAttempts: string[] = [];
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/receipt') {
        const body = JSON.parse(init.body as string);
        receiptAttempts.push(body.lease_token);
        if (body.lease_token === 'lease-cancel' && !allowCancelReceipt) {
          throw new Error('cancel receipt offline');
        }
        if (body.lease_token === 'lease-create') createReceiptAccepted = true;
        if (body.lease_token === 'lease-cancel') cancelReceiptAccepted = true;
        return Response.json({ ok: true });
      }
      const body = JSON.parse(init.body as string);
      const excluded = new Set<string>(body.exclude_lease_tokens);
      claimCalls += 1;
      if (!excluded.has('lease-create') && claimCalls === 1) {
        return Response.json({
          ok: true,
          claim: { ...createClaim, leaseToken: 'lease-create' },
        });
      }
      if (!excluded.has('lease-cancel') && !cancelReceiptAccepted) {
        return Response.json({
          ok: true,
          claim: {
            action: 'cancel',
            generation: 1,
            purpose: 'user',
            leaseToken: 'lease-cancel',
            leaseExpiresAt: request.expiresAt,
            request: cancelRequest,
          },
        });
      }
      if (!excluded.has('lease-create') && !createReceiptAccepted) {
        return Response.json({
          ok: true,
          claim: { ...createClaim, leaseToken: 'lease-create' },
        });
      }
      return Response.json({ ok: true, claim: null });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce());

    reactivate?.();
    await waitFor(() => expect(native.nativeSystemScheduleCancel).toHaveBeenCalledOnce());
    expect(resolveCreate).toBeTypeOf('function');
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();

    resolveCreate?.(createCancelledReceipt);
    await waitFor(() => expect(createReceiptAccepted).toBe(true));
    expect(cancelReceiptAccepted).toBe(false);
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();
    expect(native.nativeSystemScheduleCancel).toHaveBeenCalledOnce();

    allowCancelReceipt = true;
    reactivate?.();
    await waitFor(() => expect(cancelReceiptAccepted).toBe(true));
    expect(receiptAttempts).toContain('lease-create');
    expect(receiptAttempts.filter((lease) => lease === 'lease-cancel').length).toBeGreaterThan(1);
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();
    expect(native.nativeSystemScheduleCancel).toHaveBeenCalledOnce();
  });

  it('does not present a second create while the first confirmation is pending', async () => {
    let resolveFirst: ((value: typeof receipt) => void) | undefined;
    const secondRequest = {
      ...request,
      requestId: 'request-2',
      operationId: 'operation-2',
      title: 'Second alarm',
    };
    const secondReceipt = {
      ...receipt,
      requestId: secondRequest.requestId,
      operationId: secondRequest.operationId,
    };
    native.nativeSystemScheduleRequest.mockImplementation((claimedRequest) => {
      if (claimedRequest.operationId === request.operationId) {
        return new Promise((resolve) => { resolveFirst = resolve; });
      }
      return Promise.resolve(secondReceipt);
    });

    let firstClaimed = false;
    let firstReceiptAccepted = false;
    let secondClaimed = false;
    const allowCreateValues: boolean[] = [];
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string);
      if (path === '/api/system-schedule/receipt') {
        if (body.lease_token === 'lease-1') firstReceiptAccepted = true;
        return Response.json({ ok: true });
      }
      allowCreateValues.push(body.allow_create);
      if (!firstClaimed) {
        firstClaimed = true;
        return Response.json({ ok: true, claim: createClaim });
      }
      if (!body.allow_create) return Response.json({ ok: true, claim: null });
      if (firstReceiptAccepted && !secondClaimed) {
        secondClaimed = true;
        return Response.json({
          ok: true,
          claim: {
            ...createClaim,
            leaseToken: 'lease-2',
            request: secondRequest,
          },
        });
      }
      return Response.json({ ok: true, claim: null });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce());
    await waitFor(() => expect(allowCreateValues).toContain(false));
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();

    resolveFirst?.(receipt);
    await waitFor(() => expect(native.nativeSystemScheduleRequest).toHaveBeenCalledTimes(2));
    expect(native.nativeSystemScheduleRequest).toHaveBeenNthCalledWith(2, secondRequest);
  });

  it('retries auth, server, and semantic failures but discards a permanent receipt mismatch', async () => {
    let reactivate: (() => void) | undefined;
    activity.onPageReactivated.mockImplementation((callback: () => void) => {
      reactivate = callback;
      return () => undefined;
    });
    let receiptAttempts = 0;
    let discarded = false;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/claim') {
        if (discarded) return Response.json({ ok: true, claim: null });
        const body = JSON.parse(init.body as string);
        if (body.exclude_lease_tokens.includes(createClaim.leaseToken)) {
          return Response.json({ ok: true, claim: null });
        }
        return Response.json({ ok: true, claim: createClaim });
      }
      receiptAttempts += 1;
      if (receiptAttempts === 1) return Response.json({}, { status: 401 });
      if (receiptAttempts === 2) return Response.json({}, { status: 503 });
      if (receiptAttempts === 3) {
        return Response.json({ error: 'temporary_conflict' }, { status: 409 });
      }
      discarded = true;
      return Response.json({ error: 'receipt_mismatch' }, { status: 409 });
    });

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(receiptAttempts).toBe(1));

    for (const expected of [2, 3, 4]) {
      reactivate?.();
      await waitFor(() => expect(receiptAttempts).toBe(expected));
    }
    reactivate?.();
    await waitFor(() => expect(discarded).toBe(true));
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledOnce();
  });

  it('reclaims the same sticky lease after remount and relies on Native idempotency', async () => {
    let receiptAttempts = 0;
    let receiptAccepted = false;
    apiFetch.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === '/api/system-schedule/claim') {
        if (receiptAccepted) return Response.json({ ok: true, claim: null });
        const body = JSON.parse(init.body as string);
        if (body.exclude_lease_tokens.includes(createClaim.leaseToken)) {
          return Response.json({ ok: true, claim: null });
        }
        return Response.json({ ok: true, claim: createClaim });
      }
      receiptAttempts += 1;
      if (receiptAttempts === 1) throw new Error('offline');
      receiptAccepted = true;
      return Response.json({ ok: true });
    });

    const first = render(<SystemScheduleForwarder />);
    await waitFor(() => expect(receiptAttempts).toBe(1));
    first.unmount();

    render(<SystemScheduleForwarder />);
    await waitFor(() => expect(receiptAttempts).toBe(2));
    expect(native.nativeSystemScheduleRequest).toHaveBeenCalledTimes(2);
    const unexcludedClaims = callsFor('/api/system-schedule/claim')
      .map(([, init]) => JSON.parse(init.body))
      .filter((body) => body.exclude_lease_tokens.length === 0);
    expect(unexcludedClaims.length).toBeGreaterThanOrEqual(2);
  });

  it('does nothing without Editor Agent use or the native capability', async () => {
    state.canChat = false;
    native.hasNativeCapability.mockReturnValue(false);

    render(<SystemScheduleForwarder />);
    await Promise.resolve();

    expect(native.nativeAppInfo).not.toHaveBeenCalled();
    expect(apiFetch).not.toHaveBeenCalled();
  });
});
