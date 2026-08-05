/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MemorySettings } from '../../../context/ApiContext';
import { MemorySettingsPanel } from './MemorySettingsPanel';

const api = vi.hoisted(() => ({ saveMemorySettings: vi.fn() }));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../../../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { returnObjects?: boolean }) => options?.returnObjects ? [] : key,
  }),
}));

const endpoint = (baseUrl: string) => ({
  base_url: baseUrl,
  model: 'model-1',
  api_key: null,
  has_api_key: false,
});

const legacySettings: MemorySettings = {
  status: 'ok',
  enabled: true,
  processing: {
    llm: endpoint('https://old.example.test/v1'),
    embedding: endpoint('https://embedding.example.test/v1'),
  },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MemorySettingsPanel', () => {
  it('does not expose a provider logging switch and omits diagnostics from saves', async () => {
    const saved = {
      ...legacySettings,
      processing: {
        ...legacySettings.processing,
        llm: endpoint('https://new.example.test/v1'),
      },
    };
    api.saveMemorySettings.mockResolvedValue(saved);
    const onSaved = vi.fn();
    const user = userEvent.setup();

    render(
      <MemorySettingsPanel
        settings={legacySettings}
        status={null}
        dependencyReady
        onSaved={onSaved}
        onReloadSettings={() => undefined}
        onReloadStatus={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.queryByRole('switch', { name: 'memory.settings.providerCallLoggingLabel' })).toBeNull();

    const llmBaseUrl = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')[0];
    await user.clear(llmBaseUrl);
    await user.type(llmBaseUrl, 'https://new.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: { llm: { base_url: 'https://new.example.test/v1' } },
    }));
    expect(onSaved).toHaveBeenCalledWith(saved);
  });

});
