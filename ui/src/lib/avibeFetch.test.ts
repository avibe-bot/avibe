import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./apiFetch', () => ({
  apiFetch: vi.fn(),
}));

const loadModules = async () => {
  const { apiFetch } = await import('./apiFetch');
  const avibe = await import('./avibeFetch');
  return { ...avibe, apiFetch: vi.mocked(apiFetch) };
};

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('avibeFetch', () => {
  it('honors the caller deadline while a shared token request is pending', async () => {
    const { apiFetch, avibeFetch } = await loadModules();
    apiFetch.mockReset();
    apiFetch.mockImplementation(() => new Promise<Response>(() => undefined));
    const controller = new AbortController();
    const request = avibeFetch('/api/cloud/audio/transcriptions', {
      signal: controller.signal,
    });

    controller.abort(new DOMException('timed out', 'TimeoutError'));

    await expect(request).rejects.toMatchObject({ name: 'TimeoutError' });
    expect(apiFetch).toHaveBeenCalledWith('/api/cloud/token', {
      signal: expect.any(AbortSignal),
    });
  });

  it('evicts a stalled shared mint after its caller-independent deadline', async () => {
    vi.useFakeTimers();
    const {
      apiFetch,
      avibeFetch,
      CLOUD_TOKEN_MINT_TIMEOUT_MS,
      CloudUnavailableError,
    } = await loadModules();
    expect(CLOUD_TOKEN_MINT_TIMEOUT_MS).toBeLessThanOrEqual(10_000);
    apiFetch.mockReset();
    apiFetch
      // Intentionally ignore abort to prove the shared promise itself expires.
      .mockImplementationOnce(() => new Promise<Response>(() => undefined))
      .mockResolvedValueOnce(Response.json({
        token: 'recovered',
        base_url: 'https://example.test',
        expires_at: Math.floor(Date.now() / 1000) + 3600,
      }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ text: 'ok' })));

    const stalled = avibeFetch('/api/cloud/audio/transcriptions');
    const stalledResult = expect(stalled).rejects.toBeInstanceOf(CloudUnavailableError);
    await vi.advanceTimersByTimeAsync(CLOUD_TOKEN_MINT_TIMEOUT_MS);

    await stalledResult;
    await expect(avibeFetch('/api/cloud/audio/transcriptions')).resolves.toMatchObject({
      status: 200,
    });
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });

  it('does not let one waiter abort the shared token request', async () => {
    const { apiFetch, avibeFetch } = await loadModules();
    let resolveToken!: (response: Response) => void;
    apiFetch.mockReset();
    apiFetch.mockImplementation(
      () => new Promise<Response>((resolve) => {
        resolveToken = resolve;
      }),
    );
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ text: 'ok' })));
    const firstController = new AbortController();
    const secondController = new AbortController();

    const first = avibeFetch('/api/cloud/audio/transcriptions', {
      signal: firstController.signal,
    });
    const second = avibeFetch('/api/cloud/audio/transcriptions', {
      signal: secondController.signal,
    });
    firstController.abort(new DOMException('first timed out', 'TimeoutError'));
    await expect(first).rejects.toMatchObject({ name: 'TimeoutError' });

    resolveToken(Response.json({
      token: 'shared',
      base_url: 'https://example.test',
      expires_at: Math.floor(Date.now() / 1000) + 3600,
    }));

    await expect(second).resolves.toMatchObject({ status: 200 });
    expect(apiFetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('marks token refresh failure as post-upload unavailability', async () => {
    const { apiFetch, avibeFetch, CloudUnavailableError } = await loadModules();
    apiFetch.mockReset();
    apiFetch
      .mockResolvedValueOnce(Response.json({
        token: 'initial',
        base_url: 'https://example.test',
        expires_at: Math.floor(Date.now() / 1000) + 3600,
      }))
      .mockResolvedValueOnce(new Response(null, { status: 503 }));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(
      new Response(null, { status: 401 }),
    ));

    const error = await avibeFetch('/api/cloud/audio/transcriptions').catch((cause) => cause);

    expect(error).toBeInstanceOf(CloudUnavailableError);
    expect(error).toMatchObject({ uploadStarted: true });
    expect(apiFetch).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('exposes both HTTP attempts around an internal 401 refresh', async () => {
    const { apiFetch, avibeFetch } = await loadModules();
    const onAttempt = vi.fn();
    apiFetch.mockReset();
    apiFetch
      .mockResolvedValueOnce(Response.json({
        token: 'initial',
        base_url: 'https://example.test',
        expires_at: Math.floor(Date.now() / 1000) + 3600,
      }))
      .mockResolvedValueOnce(Response.json({
        token: 'refreshed',
        base_url: 'https://example.test',
        expires_at: Math.floor(Date.now() / 1000) + 3600,
      }));
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ text: 'ok' })));

    await expect(avibeFetch('/api/cloud/audio/transcriptions', {
      onAttempt,
    })).resolves.toMatchObject({ status: 200 });

    expect(onAttempt.mock.calls.map(([event]) => event)).toEqual([
      { phase: 'started', attempt: 1 },
      { phase: 'response', attempt: 1, status: 401, elapsedMs: expect.any(Number) },
      { phase: 'started', attempt: 2 },
      { phase: 'response', attempt: 2, status: 200, elapsedMs: expect.any(Number) },
    ]);
  });
});
