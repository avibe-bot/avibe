/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { setConfigField } from '../lib/configMutations';
import { ApiProvider, useApi } from './ApiContext';

let capturedApi: ReturnType<typeof useApi> | null = null;

const CaptureApi = () => {
  const api = useApi();
  useEffect(() => {
    capturedApi = api;
  }, [api]);
  return null;
};

const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

beforeEach(() => {
  capturedApi = null;
  apiFetch.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('ApiProvider config convergence', () => {
  it('publishes the saved config after a successful mutation', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());

    const changed = vi.fn();
    const stop = capturedApi!.onConfigChanged(changed);
    const savedConfig = { language: 'zh', platforms: { enabled: ['slack'] } };
    apiFetch.mockResolvedValueOnce(response(savedConfig));

    await expect(capturedApi!.mutateConfig([setConfigField(['language'], 'zh')]))
      .resolves.toEqual(savedConfig);
    expect(changed).toHaveBeenCalledOnce();
    expect(changed).toHaveBeenCalledWith(savedConfig);

    stop();
    apiFetch.mockResolvedValueOnce(response({ ...savedConfig, language: 'en' }));
    await capturedApi!.mutateConfig([setConfigField(['language'], 'en')]);
    expect(changed).toHaveBeenCalledOnce();
  });
});
