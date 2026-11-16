/** @vitest-environment jsdom */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { cleanup, render, waitFor } from '@testing-library/react';
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
vi.mock('@/lib/apiFetch', () => ({ apiFetch }));
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
  vi.clearAllMocks();
});

describe('SystemScheduleForwarder', () => {
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
      'compensation',
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
