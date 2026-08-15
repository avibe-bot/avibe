// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastProvider } from '@/context/ToastProvider';
import i18n from '@/i18n';

vi.mock('./featureFlags', async (importOriginal) => ({
  ...await importOriginal<typeof import('./featureFlags')>(),
  MODELS_API_MODE: 'mock',
  loadMockModelsApiForMode: () => import('./mock-only/modelsApi.mockEntry')
    .then(({ loadMockModelsApi }) => loadMockModelsApi()),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Model Hub mock product substitution', () => {
  it('routes the complete live client surface and real page through the corpus', async () => {
    const fetch = vi.fn(() => Promise.reject(new Error('live network reached')));
    vi.stubGlobal('fetch', fetch);

    const [{ configureModelsApi, loadSettingsModelsPage }, apiModule] = await Promise.all([
      import('./modelsApiMode'),
      import('./modelsApi'),
    ]);
    const configured = await configureModelsApi();
    const client = configured as unknown as Record<string, (...args: unknown[]) => unknown>;
    const facade = apiModule.modelsApi as unknown as Record<string, (...args: unknown[]) => unknown>;

    expect(apiModule.MODEL_HUB_CLIENT_OPERATIONS.length).toBeGreaterThan(0);
    for (const operation of apiModule.MODEL_HUB_CLIENT_OPERATIONS) {
      expect(typeof client[operation], operation).toBe('function');
      expect(typeof facade[operation], operation).toBe('function');

      const ownDescriptor = Object.getOwnPropertyDescriptor(configured, operation);
      const marker = { operation };
      const implementation = vi.fn(async () => marker);
      Object.defineProperty(configured, operation, {
        configurable: true,
        writable: true,
        value: implementation,
      });
      await expect(facade[operation]()).resolves.toBe(marker);
      expect(implementation, operation).toHaveBeenCalledOnce();
      if (ownDescriptor) Object.defineProperty(configured, operation, ownDescriptor);
      else delete client[operation];
    }

    await expect(apiModule.modelsApi.listSources()).resolves.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ display_name: 'Claude Pro subscription' }),
      ]),
    );

    const { default: SettingsModelsPage } = await loadSettingsModelsPage();
    render(
      <ToastProvider>
        <I18nextProvider i18n={i18n}>
          <SettingsModelsPage />
        </I18nextProvider>
      </ToastProvider>,
    );

    await waitFor(() => {
      expect(screen.queryAllByText('Claude Pro subscription').length).toBeGreaterThan(0);
    });
    expect(fetch).not.toHaveBeenCalled();
  });
});
