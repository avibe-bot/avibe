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
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_, reject) => {
        const signal = init?.signal;
        signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
      }));
    vi.stubGlobal('fetch', fetchMock);
    const methods = Object.entries(modelsApi).filter(
      (entry): entry is [string, (...args: never[]) => Promise<unknown>] =>
        typeof entry[1] === 'function',
    );
    const issuedSignals: AbortSignal[] = [];

    for (const [name, method] of methods) {
      const callsBefore = fetchMock.mock.calls.length;
      const settled = method().then(() => undefined, () => undefined);
      await vi.advanceTimersByTimeAsync(0);
      const calls = fetchMock.mock.calls.slice(callsBefore);
      expect(calls.length, `${name} must issue a deadline-bound request`).toBeGreaterThan(0);
      for (const [, init] of calls) {
        expect(init?.signal, `${name} dropped its request deadline`).toBeInstanceOf(AbortSignal);
        issuedSignals.push(init!.signal!);
      }
      expect(calls.every(([, init]) => !init?.signal?.aborted)).toBe(true);
      await vi.advanceTimersByTimeAsync(299_999);
      expect(calls.every(([, init]) => !init?.signal?.aborted)).toBe(true);
      await vi.advanceTimersByTimeAsync(1);
      expect(calls.every(([, init]) => init?.signal?.aborted)).toBe(true);
      await settled;
      expect(vi.getTimerCount()).toBe(0);
    }

    expect(issuedSignals).toHaveLength(fetchMock.mock.calls.length);
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

    const failure = modelsApi.refreshSource('src_deadline').catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(300_000);

    const error = await failure;
    expect(error).toBeInstanceOf(ApiCallError);
    expect(error).toEqual(expect.objectContaining({
      code: 'bad_response',
      serverNamed: false,
    }));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('preserves a caller-owned timeout reason', async () => {
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
    const controller = new AbortController();
    const callerReason = new DOMException('caller deadline', 'TimeoutError');

    const request = modelsApi.observeApiKeySource({
      vendor: 'openai',
      key: 'test-key',
    }, controller.signal);
    const rejected = expect(request).rejects.toBe(callerReason);
    await vi.advanceTimersByTimeAsync(0);
    controller.abort(callerReason);

    await rejected;
    expect(vi.getTimerCount()).toBe(0);
  });
});
