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

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
};

beforeEach(() => {
  capturedApi = null;
  apiFetch.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('ApiProvider config convergence', () => {
  it('invalidates a cached config read after a successful mutation', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());

    apiFetch
      .mockResolvedValueOnce(response({ language: 'en' }))
      .mockResolvedValueOnce(response({ language: 'zh' }))
      .mockResolvedValueOnce(response({ language: 'zh' }));

    await expect(capturedApi!.getConfig()).resolves.toEqual({ language: 'en' });
    await capturedApi!.mutateConfig([setConfigField(['language'], 'zh')]);
    await expect(capturedApi!.getConfig()).resolves.toEqual({ language: 'zh' });

    expect(apiFetch.mock.calls.map(([path]) => path)).toEqual([
      '/api/config',
      '/api/config',
      '/api/config',
    ]);
  });

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

  it('serializes mutations and preserves the fence across consumer remounts', async () => {
    const firstResponse = deferred<Response>();
    const view = render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());
    apiFetch
      .mockImplementationOnce(() => firstResponse.promise)
      .mockResolvedValueOnce(response({ language: 'en' }));

    const first = capturedApi!.mutateConfig([setConfigField(['language'], 'zh')]);
    const second = capturedApi!.mutateConfig([setConfigField(['language'], 'en')]);
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));

    view.rerender(<ApiProvider><div /></ApiProvider>);
    capturedApi = null;
    view.rerender(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());
    let fenceSettled = false;
    const fence = capturedApi!.waitForConfigMutations().then(() => {
      fenceSettled = true;
    });
    await Promise.resolve();
    expect(fenceSettled).toBe(false);

    firstResponse.resolve(response({ language: 'zh' }));
    await first;
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
    await second;
    await fence;
    expect(fenceSettled).toBe(true);
  });

  it('continues the mutation queue after a failed save', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());
    apiFetch
      .mockResolvedValueOnce(response({ detail: 'save failed' }, 500))
      .mockResolvedValueOnce(response({ language: 'en' }));

    const failed = capturedApi!.mutateConfig([setConfigField(['language'], 'zh')]);
    const recovered = capturedApi!.mutateConfig([setConfigField(['language'], 'en')]);
    await expect(failed).rejects.toThrow();
    await expect(recovered).resolves.toEqual({ language: 'en' });
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });
});
