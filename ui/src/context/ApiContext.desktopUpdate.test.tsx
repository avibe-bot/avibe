/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());
vi.mock('../lib/apiFetch', async (importOriginal) => ({
  ...await importOriginal<typeof import('../lib/apiFetch')>(),
  apiFetch,
}));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ApiError, ApiProvider, useApi } from './ApiContext';

let api: ReturnType<typeof useApi>;
function CaptureApi() {
  const value = useApi();
  useEffect(() => { api = value; }, [value]);
  return null;
}
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status, headers: { 'Content-Type': 'application/json' },
});
beforeEach(() => {
  apiFetch.mockReset();
  showToast.mockReset();
  render(<ApiProvider><CaptureApi /></ApiProvider>);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// Component mocks cannot protect the HTTP boundary: requestJson must retain
// the 409 body, and new clients must send false rather than omit confirmation.
describe('update HTTP contracts', () => {
  it.each(['desktop', 'runtime'] as const)('preserves %s nested 409 counts and sends false/true', async (kind) => {
    const confirmation = {
      ok: false, error: 'confirmation_required',
      counts: { agents: 2, watches: 3, tasks: 4, total: 9, unknown: true },
    };
    const accepted = kind === 'desktop'
      ? { ok: true, request_id: 'request-1', delivery: 'prompt' }
      : { ok: true, message: 'Restarting', output: null, restarting: true };
    apiFetch.mockResolvedValueOnce(response(confirmation, 409)).mockResolvedValueOnce(response(accepted));
    const submit = (confirmInterrupt: boolean) => kind === 'desktop'
      ? api.requestDesktopUpdate('3.2.4-rc.1', { confirmInterrupt })
      : api.doUpgrade({ confirmInterrupt });
    expect(await submit(false)).toEqual(confirmation);
    expect(await submit(true)).toEqual(accepted);
    expect(apiFetch.mock.calls.map(([path, init]) => [path, JSON.parse(init.body)])).toEqual(
      [false, true].map((confirm_interrupt) => [
        kind === 'desktop' ? '/api/desktop/update/request' : '/api/upgrade',
        kind === 'desktop' ? { target_version: '3.2.4-rc.1', confirm_interrupt } : { confirm_interrupt },
      ]),
    );
    expect(showToast).not.toHaveBeenCalled();
  });

  it('defaults desktop requests to false while retaining the legacy Runtime omission', async () => {
    apiFetch.mockResolvedValueOnce(response({ ok: true, request_id: 'request-1', delivery: 'install' }))
      .mockResolvedValueOnce(response({ ok: true, restarting: true }));
    await api.requestDesktopUpdate('3.2.4');
    await api.doUpgrade();
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ target_version: '3.2.4', confirm_interrupt: false });
    expect(JSON.parse(apiFetch.mock.calls[1][1].body)).toEqual({});
  });

  it.each(['not_desktop_managed', 'no_update_available', 'update_in_progress', 'desktop_shell_unavailable'])(
    'returns stable error %s without losing the error field', async (error) => {
      apiFetch.mockResolvedValueOnce(response({ ok: false, error }, 409));
      expect(await api.requestDesktopUpdate('3.2.4')).toEqual({ ok: false, error });
      expect(showToast).not.toHaveBeenCalled();
    },
  );

  it('rejects an authorization failure instead of treating it as an accepted receipt', async () => {
    apiFetch.mockResolvedValueOnce(response({ ok: false, error: 'forbidden' }, 403));
    await expect(api.requestDesktopUpdate('3.2.4')).rejects.toMatchObject({ status: 403, code: 'forbidden' });
    expect(showToast).not.toHaveBeenCalled();
  });

  it('bounds a hung reconnect read without displaying a global error toast', async () => {
    vi.useFakeTimers();
    apiFetch.mockImplementationOnce((_path, { signal }: RequestInit) => new Promise((_resolve, reject) => {
      signal!.addEventListener('abort', () => reject(signal!.reason), { once: true });
    }));
    const read = api.getVersion({ background: true, cache: false });
    const rejection = expect(read).rejects.toBeTruthy();
    await vi.advanceTimersByTimeAsync(10_000);
    await rejection;
    expect(showToast).not.toHaveBeenCalled();
  });

  it('rejects failed reconnect responses quietly', async () => {
    apiFetch.mockResolvedValueOnce(response({}, 503));
    await expect(api.getVersion({ background: true })).rejects.toBeInstanceOf(ApiError);
    expect(showToast).not.toHaveBeenCalled();
  });
});
