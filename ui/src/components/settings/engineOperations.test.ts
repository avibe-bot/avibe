/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EngineOperationReceipt } from '@/context/ApiContext';
import { runEngineOperation } from './engineOperations';

type Action = 'check' | 'update' | { job: string };

const scripted = (answers: EngineOperationReceipt[]) => {
  const asked: string[] = [];
  const request = vi.fn(async (action: Action) => {
    asked.push(typeof action === 'string' ? action : `job ${action.job}`);
    return answers.length > 1 ? answers.shift()! : answers[0];
  });
  return { request, asked };
};

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('runEngineOperation', () => {
  it('polls the receipt until it settles', async () => {
    const { request, asked } = scripted([
      { ok: true, job_id: 'engine-1', kind: 'check', status: 'running', update_state: 'checking' },
      { ok: true, job_id: 'engine-1', kind: 'check', status: 'running', update_state: 'checking' },
      { ok: true, job_id: 'engine-1', kind: 'check', status: 'succeeded', update_state: 'available' },
    ]);
    const outcome = runEngineOperation(request, 'check');
    await vi.advanceTimersByTimeAsync(2_000);
    expect(await outcome).toMatchObject({ status: 'succeeded', update_state: 'available', outcome: 'settled' });
    expect(asked).toEqual(['check', 'job engine-1', 'job engine-1']);
  });

  it('returns a refusal as a result, with nothing polled', async () => {
    const { request, asked } = scripted([{ ok: false, error: 'controller_unavailable', update_state: 'waiting_for_controller' }]);
    expect(await runEngineOperation(request, 'update')).toEqual({
      ok: false, error: 'controller_unavailable', update_state: 'waiting_for_controller', outcome: 'refused',
    });
    expect(asked).toEqual(['update']);
  });

  it('waits out a running check and never sends the update a second time by itself', async () => {
    const { request, asked } = scripted([
      { ok: false, error: 'operation_busy', operation_id: 'engine-check' },
      { ok: true, job_id: 'engine-check', kind: 'check', status: 'succeeded', update_state: 'available' },
    ]);
    const outcome = runEngineOperation(request, 'update');
    await vi.advanceTimersByTimeAsync(1_000);
    expect(await outcome).toMatchObject({ job_id: 'engine-check', kind: 'check', status: 'succeeded', outcome: 'settled' });
    expect(asked).toEqual(['update', 'job engine-check']);
  });

  it('reports a receipt the owner no longer has as unknown, not as a failure', async () => {
    const { request, asked } = scripted([
      { ok: true, job_id: 'engine-9', kind: 'update', status: 'running' },
      { ok: false, error: 'job_not_found' },
    ]);
    const outcome = runEngineOperation(request, 'update');
    await vi.advanceTimersByTimeAsync(1_000);
    expect(await outcome).toEqual({ ok: false, error: 'job_not_found', outcome: 'unknown' });
    expect(asked).toEqual(['update', 'job engine-9']);
  });

  it('reports an operation still running when the wait runs out as unknown', async () => {
    const { request } = scripted([{ ok: true, job_id: 'engine-slow', kind: 'update', status: 'running', update_state: 'updating' }]);
    const outcome = runEngineOperation(request, 'update');
    await vi.advanceTimersByTimeAsync(431_000);
    expect(await outcome).toMatchObject({ status: 'running', outcome: 'unknown' });
    const asked = request.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(request.mock.calls.length).toBe(asked);
  });

  it('lets a failed read reach the caller, which says nothing and reads the row', async () => {
    const request = vi.fn(async (action: Action) => {
      if (typeof action === 'string') return { ok: true, job_id: 'engine-2', kind: 'update', status: 'running' } as EngineOperationReceipt;
      throw new Error('network');
    });
    const outcome = runEngineOperation(request, 'update').catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(await outcome).toBeInstanceOf(Error);
  });
});
