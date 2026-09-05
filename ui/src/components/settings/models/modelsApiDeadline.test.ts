import { readFile } from 'node:fs/promises';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { classifyModelHubFailure } from './asyncLifetime';
import {
  apiFailure,
  ApiCallError,
  MODEL_HUB_REQUEST_DEADLINE_MS,
  MODEL_HUB_RPC_CEILING_MS,
  modelsApi,
} from './modelsApi';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Model Hub request deadlines', () => {
  it('keeps the browser backstop beyond the backend RPC ceiling', async () => {
    const backendSource = await readFile('../vibe/model_hub_client.py', 'utf8');
    const match = backendSource.match(/_RPC_TIMEOUT_SECONDS\s*=\s*([\d.]+)/);

    expect(match?.[1]).toBeDefined();
    expect(MODEL_HUB_RPC_CEILING_MS).toBe(Number(match![1]) * 1_000);
    expect(MODEL_HUB_REQUEST_DEADLINE_MS).toBeGreaterThan(MODEL_HUB_RPC_CEILING_MS);
  });

  it('bounds every request issued by every live client method', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test-token' });
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_, reject) => {
        const signal = init?.signal;
        signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
      }));
    vi.stubGlobal('fetch', fetchMock);
    // Narrowed to a call the compiler accepts for every member of the method
    // union: no parameters, and a result assignable to each method's own return.
    // The test calls each one with nothing — what it asserts is that a request
    // goes out under a deadline, not what any method resolves to.
    const methods = Object.entries(modelsApi).filter(
      (entry): entry is [string, () => Promise<never>] =>
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
      await vi.advanceTimersByTimeAsync(MODEL_HUB_REQUEST_DEADLINE_MS - 1);
      expect(calls.every(([, init]) => !init?.signal?.aborted)).toBe(true);
      await vi.advanceTimersByTimeAsync(1);
      expect(calls.every(([, init]) => init?.signal?.aborted)).toBe(true);
      await settled;
      expect(vi.getTimerCount()).toBe(0);
    }

    expect(issuedSignals).toHaveLength(fetchMock.mock.calls.length);
    expect(issuedSignals.every((signal) => signal.aborted)).toBe(true);
  });

  it('owns the deadline until response decoding settles', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test-token' });
    let issuedSignal: AbortSignal | undefined;
    let resolveBody!: (value: unknown) => void;
    const body = new Promise<unknown>((resolve) => {
      resolveBody = resolve;
    });
    const response = Response.json({ ok: true });
    vi.spyOn(response, 'json').mockImplementation(() => body);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        issuedSignal = init?.signal ?? undefined;
        return response;
      }),
    );
    const controller = new AbortController();
    const removeEventListener = vi.spyOn(controller.signal, 'removeEventListener');

    const operation = modelsApi.observeApiKeySource({
      vendor: 'openai',
      key: 'test-key',
    }, controller.signal);
    await vi.advanceTimersByTimeAsync(0);

    expect(vi.getTimerCount()).toBe(1);
    expect(issuedSignal?.aborted).toBe(false);

    resolveBody({ ok: true, observation: {} });
    await expect(operation).resolves.toEqual({});
    expect(vi.getTimerCount()).toBe(0);
    expect(removeEventListener).toHaveBeenCalledWith('abort', expect.any(Function));
    controller.abort(new DOMException('settled operation', 'AbortError'));
    expect(issuedSignal?.aborted).toBe(false);
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
    await vi.advanceTimersByTimeAsync(MODEL_HUB_REQUEST_DEADLINE_MS);

    const error = await failure;
    expect(error).toBeInstanceOf(ApiCallError);
    expect(error).toEqual(expect.objectContaining({
      code: 'bad_response',
      serverNamed: false,
    }));
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps a stalled response body inside the inconclusive deadline boundary', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test-token' });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        const signal = init?.signal;
        const response = Response.json({ ok: true });
        // Native fetch couples its response body to the request signal. This
        // mock wiring asserts our scope boundary, not browser implementation.
        vi.spyOn(response, 'json').mockImplementation(() =>
          new Promise<unknown>((_, reject) => {
            signal?.addEventListener('abort', () => reject(signal.reason), { once: true });
          }));
        return response;
      }),
    );

    const failure = modelsApi.refreshSource('src_body_stall').catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(MODEL_HUB_REQUEST_DEADLINE_MS);

    const error = await failure;
    expect(error).toBeInstanceOf(ApiCallError);
    expect(error).toEqual(expect.objectContaining({
      code: 'bad_response',
      serverNamed: false,
    }));
    expect(classifyModelHubFailure(apiFailure(error))).toBe('inconclusive');
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
