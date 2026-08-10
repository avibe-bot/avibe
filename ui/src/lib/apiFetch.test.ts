import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const remoteAuth = vi.hoisted(() => ({
  deferRemoteAuthRedirect: vi.fn(),
  remoteLoginPath: vi.fn((target: string) => `/auth/login?next=${encodeURIComponent(target)}`),
}));

vi.mock('./remoteAuth', () => remoteAuth);

import {
  apiFetch,
} from './apiFetch';

describe('apiFetch remote auth recovery', () => {
  beforeEach(() => {
    remoteAuth.deferRemoteAuthRedirect.mockReturnValue(true);
    vi.stubGlobal('window', {
      location: { pathname: '/inbox', search: '?filter=open', assign: vi.fn() },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('hands an expired remote session to the PWA auth gate', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ error: 'remote_access_login_required' }, { status: 401 }),
      ),
    );

    const response = await apiFetch('/api/inbox');

    expect(response.status).toBe(401);
    await vi.waitFor(() => expect(remoteAuth.deferRemoteAuthRedirect).toHaveBeenCalledOnce());
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  it('uses the dedicated login endpoint outside an iOS standalone PWA', async () => {
    remoteAuth.deferRemoteAuthRedirect.mockReturnValue(false);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ error: 'remote_access_login_required' }, { status: 401 }),
      ),
    );

    await apiFetch('/api/inbox');

    await vi.waitFor(() => expect(window.location.assign).toHaveBeenCalledWith(
      '/auth/login?next=%2Finbox%3Ffilter%3Dopen',
    ));
    expect(remoteAuth.remoteLoginPath).toHaveBeenCalledWith('/inbox?filter=open');
  });

  it('does not start remote auth for an unrelated 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ error: 'not_allowed' }, { status: 401 })),
    );

    await apiFetch('/api/inbox');

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(remoteAuth.deferRemoteAuthRedirect).not.toHaveBeenCalled();
  });

  it('refreshes a rejected CSRF token and safely replays the guarded mutation once', async () => {
    let cookie = 'vibe_csrf_token=stale-token';
    vi.stubGlobal('document', {
      get cookie() {
        return cookie;
      },
      set cookie(value: string) {
        cookie = value.includes('Max-Age=0') ? '' : value.split(';', 1)[0];
      },
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/csrf-token') {
        document.cookie = 'vibe_csrf_token=fresh-token; path=/';
        return Response.json({ csrf_token: 'fresh-token' });
      }
      const token = new Headers(init?.headers).get('X-Vibe-CSRF-Token');
      if (token === 'stale-token') {
        return Response.json({ ok: false, message: 'Forbidden: invalid csrf token' }, { status: 403 });
      }
      return Response.json({ ok: true }, { status: 201 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const response = await apiFetch('/api/sessions/ses-1/attachments', {
      method: 'POST',
      body: new FormData(),
    });

    expect(response.status).toBe(201);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get('X-Vibe-CSRF-Token')).toBe('fresh-token');
  });

  it('does not retry an unrelated forbidden mutation', async () => {
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=token' });
    const fetchMock = vi.fn(async () =>
      Response.json({ ok: false, message: 'Forbidden' }, { status: 403 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const response = await apiFetch('/api/settings', { method: 'POST' });

    expect(response.status).toBe(403);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('honors the request signal while a shared CSRF request is pending', async () => {
    let resolveCsrf!: (response: Response) => void;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL) =>
        new Promise<Response>((resolve) => {
          expect(input).toBe('/api/csrf-token');
          resolveCsrf = resolve;
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    const request = apiFetch('/api/asr/transcribe', {
      method: 'POST',
      signal: controller.signal,
    });

    controller.abort(new DOMException('transcription timed out', 'TimeoutError'));

    await expect(request).rejects.toMatchObject({ name: 'TimeoutError' });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Let the shared mint settle so it cannot leak into later tests.
    resolveCsrf(Response.json({ csrf_token: 'token' }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  it('does not impose the voice deadline on callers without a signal', async () => {
    vi.useFakeTimers();
    let resolveCsrf!: (response: Response) => void;
    const fetchMock = vi.fn()
      .mockImplementationOnce(
        () => new Promise<Response>((resolve) => {
          resolveCsrf = resolve;
        }),
      )
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    const request = apiFetch('/api/settings', { method: 'POST' });
    await vi.advanceTimersByTimeAsync(4_001);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveCsrf(Response.json({ csrf_token: 'token' }));
    await expect(request).resolves.toMatchObject({ status: 200 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('evicts a stalled shared CSRF fetch after a deadline-bound caller aborts', async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>(() => undefined))
      .mockResolvedValueOnce(Response.json({ csrf_token: 'fresh-token' }))
      .mockResolvedValueOnce(Response.json({ text: 'ok' }));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    const stalled = apiFetch('/api/asr/transcribe', {
      method: 'POST',
      signal: controller.signal,
    });
    const stalledResult = expect(stalled).rejects.toMatchObject({ name: 'TimeoutError' });
    controller.abort(new DOMException('transcription timed out', 'TimeoutError'));

    await stalledResult;
    await expect(apiFetch('/api/asr/transcribe', { method: 'POST' })).resolves.toMatchObject({
      status: 200,
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
