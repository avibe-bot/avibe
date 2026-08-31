/* @vitest-environment jsdom */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
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

const apiContextSource = () => readFileSync(join(__dirname, 'ApiContext.tsx'), 'utf8');

describe('ApiProvider expected error codes', () => {
  it('suppresses only the expected refusal for the OpenCode picker POST', async () => {
    const api = await mountApi();

    apiFetch.mockResolvedValue(codedError('instance_access_forbidden', 403));
    await expect(api.readOpencodeOptionsForModelPicker()).rejects.toMatchObject({
      code: 'instance_access_forbidden',
    });
    expect(showToast).not.toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalled();
    expect(apiFetch).toHaveBeenLastCalledWith('/api/opencode/options', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ cwd: '~' }),
    }));

    apiFetch.mockResolvedValue(codedError('opencode_runtime_failed', 500));
    await expect(api.readOpencodeOptionsForModelPicker()).rejects.toMatchObject({
      code: 'opencode_runtime_failed',
    });
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(consoleError).toHaveBeenCalledTimes(1);
  });

  it('answers an absent Show Page silently on every read that shares the owner', async () => {
    // The members are DERIVED from the product source rather than restated here, so a read
    // added to `readShowPageJson` later is covered without editing this test — the panel
    // fires several of these in parallel and the reviewer found the second one exactly
    // because the property had been declared per call site instead of owned once.
    // Both directions are asserted per member: an absent page still REJECTS (no caller can
    // mistake an error body for a payload) yet is neither toasted nor logged, while any
    // other failure of the very same read still reaches the user. That pairing is what
    // stops the shared owner from quietly becoming "these reads never report anything".
    const reads = [...apiContextSource().matchAll(/(\w+): \(sessionId\) => readShowPageJson\(/g)]
      .map(([, name]) => name);
    expect(reads.length).toBeGreaterThan(1);

    const api = await mountApi();
    for (const name of reads) {
      const read = api[name as keyof typeof api] as (sessionId: string) => Promise<unknown>;

      apiFetch.mockResolvedValue(codedError('show_page_not_found', 404));
      await expect(read('ses1')).rejects.toMatchObject({ code: 'show_page_not_found' });
      expect(showToast, name).not.toHaveBeenCalled();
      expect(consoleError, name).not.toHaveBeenCalled();

      apiFetch.mockResolvedValue(codedError('resource_access_forbidden', 403));
      await expect(read('ses1')).rejects.toMatchObject({ code: 'resource_access_forbidden' });
      expect(showToast, name).toHaveBeenCalledTimes(1);
      expect(consoleError, name).toHaveBeenCalledTimes(1);

      showToast.mockClear();
      consoleError.mockClear();
    }
  });

  it('keeps every single-session Show Page GET on that owner', () => {
    // The behavioural test above can only exercise reads that already went through the
    // owner. This is the half that makes a NEW one fail: a GET of one session's page
    // written against raw `getJson` is a violation by construction, whatever it is named.
    // The list read (`/api/show-pages`, no session segment) cannot report a missing page
    // and is excluded by the same shape.
    const violations = [...apiContextSource().matchAll(/^.*\bgetJson\(`\/api\/show-pages\/\$\{.*$/gm)]
      .map(([line]) => line.trim());

    expect(violations).toEqual([]);
  });
});
