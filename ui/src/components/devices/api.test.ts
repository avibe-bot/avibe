import { afterEach, describe, expect, it, vi } from 'vitest';

import { DevicesApiError, LostAnswer, devicesApi } from './api';

const fetched = vi.hoisted(() => vi.fn());
vi.mock('@/lib/apiFetch', () => ({ apiFetch: fetched }));

const answer = (status: number, body: unknown) => ({ ok: status >= 200 && status < 300, status, json: async () => body }) as unknown as Response;
afterEach(() => fetched.mockReset());

describe('devicesApi', () => {
  it('reads a refused route optimization as an answer with its reason, not as a failure', async () => {
    fetched.mockResolvedValue(answer(409, { ok: false, error: 'route_optimization_debounce', retry_after_seconds: 6, running: true }));
    await expect(devicesApi.optimizeRoute()).resolves.toMatchObject({ ok: false, error: 'route_optimization_debounce', retry_after_seconds: 6 });
    fetched.mockResolvedValue(answer(500, { error: 'boom' }));
    await expect(devicesApi.optimizeRoute()).rejects.toBeInstanceOf(DevicesApiError);
  });

  it('throws a refusal with the instance\'s code, and a request that never answered as a lost answer', async () => {
    fetched.mockResolvedValue(answer(409, { state: 'error', code: 'claim_in_progress', claim_id: 'c1' }));
    await expect(devicesApi.startClaim('zh')).rejects.toMatchObject({ status: 409, code: 'claim_in_progress', body: { claim_id: 'c1' } });
    fetched.mockRejectedValue(new TypeError('network'));
    await expect(devicesApi.changeConnection('protocol', 'http2')).rejects.toBeInstanceOf(LostAnswer);
    // A truncated answer to a change that was accepted says nothing about its outcome either.
    fetched.mockResolvedValue({ ok: true, status: 202, json: async () => { throw new SyntaxError('cut'); } } as unknown as Response);
    await expect(devicesApi.changeConnection('protocol', 'http2')).rejects.toBeInstanceOf(LostAnswer);
  });

  it('keeps the activity cursor, and asks for the next page with it', async () => {
    fetched.mockResolvedValue(answer(200, { items: [{ id: 'e1', at: '2026-11-21T01:00:00Z', kind: 'signedIn', device_id: 'd1' }], next_cursor: 'c/2 ' }));
    const page = await devicesApi.activity(() => 'Chrome', 'c 1');
    expect(page).toEqual({ items: [{ id: 'e1', deviceId: 'd1', at: '2026-11-21T01:00:00.000Z', device: 'Chrome', event: { kind: 'signedIn' } }], nextCursor: 'c/2 ' });
    expect(fetched.mock.calls[0][0]).toBe('/api/devices/activity?limit=50&cursor=c%201');
  });
});
