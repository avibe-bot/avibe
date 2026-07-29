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
    expect(apiFetch).toHaveBeenCalledWith('/api/cloud/token', { signal: controller.signal });
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
});
