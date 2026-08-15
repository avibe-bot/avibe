import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiCallError, modelsApi } from './modelsApi';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Model Hub request deadlines', () => {
  it('bounds every request issued by every live client method', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test-token' });
    const fetchMock = vi.fn(async () => Response.json({}));
    vi.stubGlobal('fetch', fetchMock);
    const methods = Object.entries(modelsApi).filter(
      (entry): entry is [string, (...args: never[]) => Promise<unknown>] =>
        typeof entry[1] === 'function',
    );
    const issuedSignals: AbortSignal[] = [];

    for (const [name, method] of methods) {
      const callsBefore = fetchMock.mock.calls.length;
      await method();
      const calls = fetchMock.mock.calls.slice(callsBefore);
      expect(calls.length, `${name} must issue a deadline-bound request`).toBeGreaterThan(0);
      for (const [, init] of calls) {
        expect(init?.signal, `${name} dropped its request deadline`).toBeInstanceOf(AbortSignal);
        issuedSignals.push(init!.signal!);
      }
    }

    expect(issuedSignals).toHaveLength(fetchMock.mock.calls.length);
    expect(issuedSignals.every((signal) => !signal.aborted)).toBe(true);
    await vi.runAllTimersAsync();
    expect(issuedSignals.every((signal) => signal.aborted)).toBe(true);
  });

  it('reports a deadline as an inconclusive API call failure', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test-token' });
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          const signal = init?.signal;
          signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
        })),
    );

    const request = modelsApi.refreshSource('src_deadline');
    const rejected = expect(request).rejects.toEqual(expect.objectContaining({
      code: 'bad_response',
      serverNamed: false,
    }));
    await vi.runAllTimersAsync();

    await rejected;
    await request.catch((error) => expect(error).toBeInstanceOf(ApiCallError));
  });
});
