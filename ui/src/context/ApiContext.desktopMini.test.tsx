/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ApiError, ApiProvider, useApi } from './ApiContext';

let capturedApi: ReturnType<typeof useApi> | null = null;
const CaptureApi = () => {
  const api = useApi();
  useEffect(() => { capturedApi = api; }, [api]);
  return null;
};
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

beforeEach(() => {
  capturedApi = null;
  apiFetch.mockReset();
  showToast.mockReset();
});
afterEach(cleanup);

describe('ensureDesktopMiniSession', () => {
  it('returns the session row without its capabilities', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());
    apiFetch.mockResolvedValueOnce(response({ id: 'V', title: 'Vibey', capabilities: { can_chat: true } }));
    const session = await capturedApi!.ensureDesktopMiniSession();
    expect(session).toMatchObject({ id: 'V', title: 'Vibey' });
    expect(session).not.toHaveProperty('capabilities');
    expect(apiFetch).toHaveBeenCalledWith('/api/desktop-mini/session', expect.objectContaining({ method: 'POST' }));
  });

  it('rejects with the structured code and no global toast', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());
    apiFetch.mockResolvedValueOnce(response({
      ok: false,
      error: { code: 'no_chat_project', message: 'Create a project first.' },
      code: 'no_chat_project',
      message: 'Create a project first.',
    }, 409));
    const error = await capturedApi!.ensureDesktopMiniSession().catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).code).toBe('no_chat_project');
    expect(showToast).not.toHaveBeenCalled();
  });
});
