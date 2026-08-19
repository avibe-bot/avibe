/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { defaultValue?: string }) => options?.defaultValue ?? key,
  }),
}));

import { ApiProvider, useApi } from './ApiContext';

let capturedApi: ReturnType<typeof useApi> | null = null;

const CaptureApi = () => {
  const api = useApi();
  useEffect(() => {
    capturedApi = api;
  }, [api]);
  return null;
};

// The nested-``error`` body every coded route answers with (``_coded_error_response``
// in ``vibe/ui_server.py``), so these tests exercise the same parse the real routes do.
const codedError = (code: string, status: number) => new Response(
  JSON.stringify({ ok: false, error: { code, message: `${code} message` }, code, message: `${code} message` }),
  { status, headers: { 'Content-Type': 'application/json' } },
);

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  capturedApi = null;
  window.localStorage.clear();
  apiFetch.mockReset();
  showToast.mockReset();
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  apiFetch.mockResolvedValue(new Response('{}', {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));
});

afterEach(() => {
  cleanup();
  consoleError.mockRestore();
  window.localStorage.clear();
});

// Mount, then forget whatever the provider's own bootstrap did: these tests are about
// the single call each one makes afterwards.
const mountApi = async () => {
  render(<ApiProvider><CaptureApi /></ApiProvider>);
  await waitFor(() => expect(capturedApi).not.toBeNull());
  showToast.mockClear();
  consoleError.mockClear();
  return capturedApi!;
};

describe('ApiProvider expected error codes', () => {
  it('rejects a declared expected code without announcing it', async () => {
    // Property: a code the call site declared expected still REJECTS — so no caller can
    // mistake an error body for a payload — but is neither toasted nor logged as an
    // error. "This session has no Show Page" is `getShowPage`'s normal answer, which the
    // share panel renders as an empty link; a toast there would be a regression the
    // read-only endpoint introduced rather than fixed.
    const api = await mountApi();
    apiFetch.mockResolvedValue(codedError('show_page_not_found', 404));

    await expect(api.getShowPage('ses1')).rejects.toMatchObject({ code: 'show_page_not_found' });
    expect(showToast).not.toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('still announces any other failure of the same read', async () => {
    // The other direction, which is what keeps the suppression narrow: only the DECLARED
    // code is silent. A real fault on the very same call still reaches the user, so
    // "expected" cannot quietly grow into "this read never reports anything".
    const api = await mountApi();
    apiFetch.mockResolvedValue(codedError('resource_access_forbidden', 403));

    await expect(api.getShowPage('ses1')).rejects.toMatchObject({ code: 'resource_access_forbidden' });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(consoleError).toHaveBeenCalledTimes(1);
  });
});
