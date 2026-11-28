import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const remoteAuth = vi.hoisted(() => ({
  deferRemoteAuthRedirect: vi.fn(),
  reportRemoteAuthorizationState: vi.fn(),
  remoteLoginPath: vi.fn((target: string) => `/auth/login?next=${encodeURIComponent(target)}`),
}));

vi.mock('./remoteAuth', () => remoteAuth);

import { setPageWhyReader } from './pageReport';
import {
  apiFetch,
  isApiFetchDeadlineAbort,
  recoverRemoteAuthFromSessionProbe,
  withApiDeadline,
} from './apiFetch';

describe('apiFetch and the page report', () => {
  let stopReport: () => void = () => {};
  const sent = () => {
    const [, init] = vi.mocked(fetch).mock.calls.at(-1) as [unknown, RequestInit];
    return { word: new Headers(init.headers).get('X-Avibe-Page'), mode: init.mode };
  };

  beforeEach(() => {
    vi.stubGlobal('window', {
      location: { href: 'https://instance.example/chat', origin: 'https://instance.example', pathname: '/chat', search: '' },
    });
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=token' });
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ok: true })));
  });

  afterEach(() => {
    stopReport();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('sends the word on the page\'s own session read, which it makes same-origin', async () => {
    stopReport = setPageWhyReader(() => 'quiet_request');
    await apiFetch('/api/session');
    expect(sent()).toEqual({ word: 'quiet_request', mode: 'same-origin' });
    // Not due again: the read goes as it always did.
    await apiFetch(new URL('https://instance.example/api/session'), { cache: 'no-store' });
    expect(sent()).toEqual({ word: null, mode: undefined });
  });

  it('sends it on no other request', async () => {
    stopReport = setPageWhyReader(() => 'idle');
    // Another read, a write beside the CSRF header, another origin's session
    // read, a Request object, and a session read whose mode the caller chose.
    await apiFetch('/api/inbox');
    expect(sent()).toEqual({ word: null, mode: undefined });
    await apiFetch('/api/sessions/s1/mark-read', { method: 'POST' });
    expect(sent()).toEqual({ word: null, mode: undefined });
    await apiFetch('/api/session', { method: 'POST' });
    expect(sent()).toEqual({ word: null, mode: undefined });
    await apiFetch('https://elsewhere.example/api/session');
    expect(sent()).toEqual({ word: null, mode: undefined });
    await apiFetch('/api/session', { mode: 'cors' });
    expect(sent()).toEqual({ word: null, mode: 'cors' });
    // The word was not spent on any of them.
    await apiFetch('/api/session?fresh=1');
    expect(sent()).toEqual({ word: 'idle', mode: 'same-origin' });
  });

  it('changes nothing on a page that has no word', async () => {
    await apiFetch('/api/session');
    expect(sent()).toEqual({ word: null, mode: undefined });
  });
});

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

  it.each([
    'remote_access_login_required',
    'remote_access_authorization_refresh_required',
  ])(
    'hands remote auth recovery error %s to the PWA auth gate',
    async (error) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => Response.json({ error }, { status: 401 })),
      );

      const response = await apiFetch('/api/inbox');

      expect(response.status).toBe(401);
      await vi.waitFor(() => expect(remoteAuth.reportRemoteAuthorizationState).toHaveBeenCalledWith(
        'login_required',
      ));
      expect(window.location.assign).not.toHaveBeenCalled();
    },
  );

  it('does not start remote auth for an unrelated 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ error: 'not_allowed' }, { status: 401 })),
    );

    await apiFetch('/api/inbox');

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(remoteAuth.reportRemoteAuthorizationState).not.toHaveBeenCalled();
  });

  it('replays a rejected mutation with the cookie that replaced its header token', async () => {
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
      const token = new Headers(init?.headers).get('X-Vibe-CSRF-Token');
      if (token === 'stale-token') {
        document.cookie = 'vibe_csrf_token=fresh-token; path=/';
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
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('X-Vibe-CSRF-Token')).toBe('fresh-token');
  });

  it('shares a CSRF refresh with mutations that start while the cookie is cleared', async () => {
    let cookie = 'vibe_csrf_token=stale-token';
    let resolveToken!: (response: Response) => void;
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
        return new Promise<Response>((resolve) => {
          resolveToken = resolve;
        });
      }
      const token = new Headers(init?.headers).get('X-Vibe-CSRF-Token');
      if (token === 'stale-token') {
        cookie = '';
        return Response.json({ ok: false, message: 'Forbidden: invalid csrf token' }, { status: 403 });
      }
      return Response.json({ ok: true }, { status: 201 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const retrying = apiFetch('/api/attachments', { method: 'POST', body: new FormData() });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const concurrent = apiFetch('/api/settings', { method: 'POST' });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchMock.mock.calls.filter(([input]) => input === '/api/csrf-token')).toHaveLength(1);

    document.cookie = 'vibe_csrf_token=fresh-token; path=/';
    resolveToken(Response.json({ csrf_token: 'fresh-token' }));

    await expect(Promise.all([retrying, concurrent])).resolves.toEqual([
      expect.objectContaining({ status: 201 }),
      expect.objectContaining({ status: 201 }),
    ]);
    expect(fetchMock.mock.calls.filter(([input]) => input === '/api/csrf-token')).toHaveLength(1);
  });

  it('rechecks a token cookie overwritten by another tab before replaying', async () => {
    let cookie = 'vibe_csrf_token=stale-token';
    let resolveToken!: (response: Response) => void;
    let mutationCalls = 0;
    const currentToken = () => cookie.startsWith('vibe_csrf_token=')
      ? cookie.slice('vibe_csrf_token='.length)
      : '';
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
        return new Promise<Response>((resolve) => {
          resolveToken = resolve;
        });
      }
      mutationCalls += 1;
      const token = new Headers(init?.headers).get('X-Vibe-CSRF-Token');
      if (mutationCalls === 1) {
        cookie = '';
        return Response.json({ ok: false, message: 'Forbidden: invalid csrf token' }, { status: 403 });
      }
      return Response.json({ ok: token === currentToken() }, {
        status: token === currentToken() ? 201 : 403,
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const request = apiFetch('/api/attachments', { method: 'POST', body: new FormData() });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    document.cookie = 'vibe_csrf_token=tab-a-token; path=/';
    resolveToken(Response.json({ csrf_token: 'tab-a-token' }));
    document.cookie = 'vibe_csrf_token=tab-b-token; path=/';

    await expect(request).resolves.toEqual(expect.objectContaining({ status: 201 }));
    expect(new Headers(fetchMock.mock.calls.at(-1)?.[1]?.headers).get('X-Vibe-CSRF-Token'))
      .toBe('tab-b-token');
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

  it('aborts a deadline-bound request without a caller signal', async () => {
    vi.useFakeTimers();
    let issuedSignal: AbortSignal | undefined;
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          expect(input).toBe('/api/models/sources');
          issuedSignal = init?.signal ?? undefined;
          issuedSignal?.addEventListener('abort', () => reject(issuedSignal?.reason), { once: true });
        })),
    );

    const request = withApiDeadline(
      1_000,
      undefined,
      (signal) => apiFetch('/api/models/sources', { signal }),
    );
    const failure = request.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(999);
    expect(issuedSignal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    const error = await failure;
    expect(error).toMatchObject({ name: 'TimeoutError' });
    expect(isApiFetchDeadlineAbort(error)).toBe(true);
  });

  it('lets a caller abort before the deadline', async () => {
    vi.useFakeTimers();
    let issuedSignal: AbortSignal | undefined;
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_, reject) => {
          issuedSignal = init?.signal ?? undefined;
          issuedSignal?.addEventListener('abort', () => reject(issuedSignal?.reason), { once: true });
        })),
    );
    const controller = new AbortController();
    const callerReason = new DOMException('caller stopped waiting', 'TimeoutError');

    const request = withApiDeadline(
      1_000,
      controller.signal,
      (signal) => apiFetch('/api/models/sources', { signal }),
    );
    const rejected = expect(request).rejects.toBe(callerReason);
    controller.abort(callerReason);

    await rejected;
    expect(issuedSignal).not.toBe(controller.signal);
    expect(issuedSignal?.reason).toBe(callerReason);
    expect(isApiFetchDeadlineAbort(callerReason)).toBe(false);
  });

  it.each([false, true])('recognizes WebKit abort copies with caller signal=%s', async (withCaller) => {
    vi.useFakeTimers();
    const failure = new DOMException('Fetch is aborted', 'AbortError');
    const operation = withApiDeadline(100, withCaller ? new AbortController().signal : undefined,
      (signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(failure))));
    const caught = operation.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(100);
    expect(await caught).toBe(failure);
    expect(isApiFetchDeadlineAbort(failure)).toBe(true);
  });

  it('does not misclassify caller cancellation or an unrelated failure after the deadline', async () => {
    vi.useFakeTimers();
    const caller = new AbortController();
    const cancelled = new DOMException('Fetch is aborted', 'AbortError');
    const pending = withApiDeadline(100, caller.signal,
      (signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(cancelled))));
    const caught = pending.catch((error: unknown) => error);
    caller.abort(new DOMException('caller timed out', 'TimeoutError'));
    await vi.advanceTimersByTimeAsync(200);
    expect(await caught).toBe(cancelled);
    expect(isApiFetchDeadlineAbort(cancelled)).toBe(false);
    const unrelated = new Error('parse failed');
    const late = withApiDeadline(100, undefined,
      () => new Promise((_, reject) => setTimeout(() => reject(unrelated), 200)));
    const lateCaught = late.catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(200);
    expect(await lateCaught).toBe(unrelated);
    expect(isApiFetchDeadlineAbort(unrelated)).toBe(false);
  });

  it('fires the deadline while a shared CSRF fetch is in flight', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>(() => undefined))
      .mockResolvedValueOnce(Response.json({ csrf_token: 'fresh-token' }))
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    const stalled = withApiDeadline(
      1_000,
      undefined,
      (signal) => apiFetch('/api/models/sources', { method: 'POST', signal }),
    );
    const rejected = expect(stalled).rejects.toMatchObject({ name: 'TimeoutError' });
    await vi.advanceTimersByTimeAsync(1_000);

    await rejected;
    await expect(apiFetch('/api/models/sources', { method: 'POST' })).resolves.toMatchObject({
      status: 200,
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([
    ['success', false, false],
    ['throw', false, true],
    ['success with a caller signal', true, false],
    ['throw with a caller signal', true, true],
  ])('disposes deadline resources after request %s', async (_label, withCaller, shouldThrow) => {
    vi.useFakeTimers();
    let issuedSignal: AbortSignal | undefined;
    const fetchError = new Error('request failed');
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        issuedSignal = init?.signal ?? undefined;
        if (shouldThrow) throw fetchError;
        return Response.json({ ok: true });
      }),
    );
    const controller = withCaller ? new AbortController() : null;
    const removeEventListener = controller
      ? vi.spyOn(controller.signal, 'removeEventListener')
      : null;

    const request = withApiDeadline(
      1_000,
      controller?.signal,
      (signal) => apiFetch('/api/models/sources', { signal }),
    );
    if (shouldThrow) {
      await expect(request).rejects.toBe(fetchError);
    } else {
      await expect(request).resolves.toMatchObject({ status: 200 });
    }

    expect(vi.getTimerCount()).toBe(0);
    expect(issuedSignal?.aborted).toBe(false);
    if (controller) {
      expect(issuedSignal).not.toBe(controller.signal);
      expect(removeEventListener).toHaveBeenCalledWith('abort', expect.any(Function));
      controller.abort(new DOMException('settled request', 'AbortError'));
      expect(issuedSignal?.aborted).toBe(false);
    }
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

  it('sends a session probe answering an ended session to sign-in', async () => {
    // What vibe/ui_server.py api_session answers for a missing or expired session.
    await recoverRemoteAuthFromSessionProbe(Response.json({
      remote: true,
      authenticated: false,
    }));

    expect(remoteAuth.reportRemoteAuthorizationState).toHaveBeenCalledWith('login_required');
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  it('reports login-required responses to the shared recovery owner', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ error: 'remote_access_login_required' }, { status: 401 }),
      ),
    );

    await apiFetch('/api/inbox');

    await vi.waitFor(() => expect(remoteAuth.reportRemoteAuthorizationState).toHaveBeenCalledWith(
      'login_required',
    ));
  });

  it('evicts a stalled shared CSRF refresh after a rejected mutation aborts', async () => {
    let cookie = 'vibe_csrf_token=stale-token';
    let tokenFetches = 0;
    let mutationCalls = 0;
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
        tokenFetches += 1;
        if (tokenFetches === 1) {
          return new Promise<Response>(() => undefined);
        }
        document.cookie = 'vibe_csrf_token=fresh-token; path=/';
        return Response.json({ csrf_token: 'fresh-token' });
      }

      mutationCalls += 1;
      if (mutationCalls === 1) {
        cookie = '';
        return Response.json({ ok: false, message: 'Forbidden: invalid csrf token' }, { status: 403 });
      }
      const token = new Headers(init?.headers).get('X-Vibe-CSRF-Token');
      return Response.json({ ok: token === 'fresh-token' }, {
        status: token === 'fresh-token' ? 200 : 403,
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    const stalled = apiFetch('/api/attachments', {
      method: 'POST',
      body: new FormData(),
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const stalledResult = expect(stalled).rejects.toMatchObject({ name: 'TimeoutError' });
    controller.abort(new DOMException('upload timed out', 'TimeoutError'));

    await stalledResult;
    await expect(apiFetch('/api/settings', { method: 'POST' })).resolves.toMatchObject({
      status: 200,
    });
    expect(tokenFetches).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});

describe('message terminal hint at the HTTP boundary', () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    ['browser', {}],
    ['mac_app', { __AVIBE_DESKTOP_SHELL__: true }],
    ['iphone_app', { avibeNative: { contract: 1, platform: 'ios', capabilities: {}, call() {}, on() {} } }],
  ])('sends %s with an unchanged message body', async (terminal, bridge) => {
    vi.stubGlobal('window', { ...bridge, location: { pathname: '/inbox' } });
    vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test' });
    const fetch = vi.fn(async () => Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetch);
    const body = JSON.stringify({ text: '看看发布', metadata: { input: 'voice' } });
    await apiFetch('/api/sessions/ses_a/messages', { method: 'POST', body });
    const init = (fetch.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get('X-Avibe-Input-Source')).toBe(terminal);
    expect(init.body).toBe(body);
    await apiFetch('/api/settings', { method: 'POST', body: '{}' });
    expect(new Headers((fetch.mock.calls[1] as unknown as [string, RequestInit])[1].headers).has('X-Avibe-Input-Source')).toBe(false);
  });
});
