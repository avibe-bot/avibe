/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import type { MemorySettings } from '../../../context/ApiContext';
import { MemorySettingsPanel } from './MemorySettingsPanel';

const api = vi.hoisted(() => ({
  saveMemorySettings: vi.fn(),
  rebuildMemoryRuntime: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../../../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('../../ui/confirm-dialog', () => ({
  ConfirmDialog: ({
    open,
    onConfirm,
  }: {
    open: boolean;
    onConfirm: () => void | Promise<void>;
  }) => (open ? <button type="button" onClick={() => void onConfirm()}>confirm-rebuild</button> : null),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { returnObjects?: boolean; deleted?: string }) => {
      if (options?.returnObjects) return [];
      return options?.deleted ? `${key}:${options.deleted}` : key;
    },
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
  im_attachment_capture_available: true,
  processing: {
    llm: endpoint('https://old.example.test/v1'),
    embedding: endpoint('https://embedding.example.test/v1'),
  },
};

const emptyEndpoint = {
  base_url: null,
  model: null,
  api_key: null,
  has_api_key: false,
};

const firstSetupSettings: MemorySettings = {
  status: 'ok',
  enabled: false,
  im_attachment_capture_available: true,
  processing: {
    llm: emptyEndpoint,
    embedding: emptyEndpoint,
  },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MemorySettingsPanel', () => {
  it('hides multimodal configuration until IM attachment capture is available', () => {
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, im_attachment_capture_available: false }}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.queryByText('memory.settings.multimodalTitle')).toBeNull();
    expect(screen.queryByText('memory.settings.disclosureAttachment')).toBeNull();
    expect(screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')).toHaveLength(3);
    expect(screen.getAllByPlaceholderText('memory.settings.modelPlaceholder')).toHaveLength(3);
  });

  it('renders optional multimodal configuration once IM capture is available', () => {
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.getByText('memory.settings.rerankTitle')).not.toBeNull();
    expect(screen.getByText('memory.settings.multimodalTitle')).not.toBeNull();
    expect(screen.getByText('memory.settings.disclosureAttachment')).not.toBeNull();
    expect(screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')).toHaveLength(4);
    expect(screen.getAllByPlaceholderText('memory.settings.modelPlaceholder')).toHaveLength(4);
  });

  it('submits a complete multimodal endpoint as the IM attachment opt-in', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...firstSetupSettings,
      processing: {
        ...firstSetupSettings.processing,
        multimodal: endpoint('https://vision.example.test/v1'),
      },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={firstSetupSettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const baseUrls = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder');
    const models = screen.getAllByPlaceholderText('memory.settings.modelPlaceholder');
    const keys = screen.getAllByPlaceholderText('memory.settings.apiKeyPlaceholder');
    await user.type(baseUrls[3], 'https://vision.example.test/v1');
    await user.type(models[3], 'vision-model');
    await user.type(keys[3], 'vision-secret');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: {
        multimodal: {
          base_url: 'https://vision.example.test/v1',
          model: 'vision-model',
          api_key: 'vision-secret',
        },
      },
    }));
  });

  it('clears the complete multimodal endpoint to disable IM attachment capture', async () => {
    const configured: MemorySettings = {
      ...legacySettings,
      processing: {
        ...legacySettings.processing,
        rerank: {
          ...endpoint('https://rerank.example.test/v1/inference'),
          model: 'rerank-model',
          has_api_key: true,
        },
        multimodal: {
          ...endpoint('https://vision.example.test/v1'),
          model: 'vision-model',
          has_api_key: true,
        },
      },
    };
    api.saveMemorySettings.mockResolvedValue(legacySettings);
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={configured}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.getByRole('checkbox', {
      name: 'memory.settings.rerankClearLabel',
    })).not.toBeNull();
    await user.click(screen.getByRole('checkbox', {
      name: 'memory.settings.multimodalClearLabel',
    }));
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: {
        multimodal: { api_key: null, base_url: null, model: null },
      },
    }));
  });

  it('locks settings mutations while the page owns another mutation', () => {
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={{ status: 'ok', data_exists: true, can_clear: true, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        mutationBusy
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );
    expect((screen.getByRole('button', { name: 'memory.settings.save' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'memory.clear.button' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('switch', { name: 'memory.settings.enableLabel' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('allows endpoint repair while hiding reinitialization controls', () => {
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, factory_reset_required: true }}
        maintenance={{ status: 'ok', data_exists: true, can_clear: true, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder').every((input) => !(input as HTMLInputElement).disabled)).toBe(true);
    expect((screen.getByRole('button', { name: 'memory.settings.save' }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('switch', { name: 'memory.settings.enableLabel' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'memory.clear.button' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'memory.factoryReset.retry' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'memory.factoryReset.button' })).toBeNull();
  });

  it('submits endpoint identity corrections directly while factory reset is pending', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      factory_reset_required: true,
      processing: {
        ...legacySettings.processing,
        embedding: endpoint('https://corrected-embedding.example.test/v1'),
      },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, factory_reset_required: true }}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText(
      'memory.settings.baseUrlPlaceholder',
    )[1];
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://corrected-embedding.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: { embedding: { base_url: 'https://corrected-embedding.example.test/v1' } },
    }));
    expect(screen.queryByRole('button', { name: 'confirm-rebuild' })).toBeNull();
  });

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
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={onSaved}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.getByRole('button', { name: 'memory.settings.llmHelpLabel' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'memory.settings.embeddingHelpLabel' })).toBeTruthy();
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

  it('keeps embedding identity editable when data exists and confirms rebuild on save', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      rebuild_required: true,
      processing: {
        ...legacySettings.processing,
        embedding: endpoint('https://new-embedding.example.test/v1'),
      },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={{ status: 'ok', data_exists: true, can_clear: true, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')[1] as HTMLInputElement;
    expect(embeddingBaseUrl.disabled).toBe(false);
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://new-embedding.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));
    expect(api.saveMemorySettings).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: { embedding: { base_url: 'https://new-embedding.example.test/v1' } },
      confirm_rebuild: true,
    }));
  });

  it('preserves endpoint drafts after confirmed preflight validation fails', async () => {
    api.saveMemorySettings.mockResolvedValue({
      status: 'failed',
      error: 'memory_embedding_unavailable',
      diagnostic: { message: 'provider_request_timed_out' },
    });
    const onReloadSettings = vi.fn();
    const onReloadMaintenance = vi.fn();
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={onReloadSettings}
        onReloadMaintenance={onReloadMaintenance}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText(
      'memory.settings.baseUrlPlaceholder',
    )[1] as HTMLInputElement;
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://candidate.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalled());
    expect(embeddingBaseUrl.value).toBe('https://candidate.example.test/v1');
    expect(onReloadSettings).not.toHaveBeenCalled();
    expect(onReloadMaintenance).not.toHaveBeenCalled();
    expect(
      screen.getByText(
        'errors.memory_embedding_unavailable: errors.provider_request_timed_out',
      ),
    ).toBeTruthy();
  });

  it('reloads durable rebuild state after post-save preflight fails', async () => {
    api.saveMemorySettings.mockResolvedValue({
      status: 'failed',
      error: 'memory_embedding_unavailable',
      diagnostic: { message: 'provider_request_timed_out' },
      rebuild_required: true,
    });
    const onReloadSettings = vi.fn();
    const onReloadMaintenance = vi.fn();
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={onReloadSettings}
        onReloadMaintenance={onReloadMaintenance}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText(
      'memory.settings.baseUrlPlaceholder',
    )[1] as HTMLInputElement;
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://candidate.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));

    await waitFor(() => expect(onReloadSettings).toHaveBeenCalledOnce());
    expect(onReloadMaintenance).toHaveBeenCalledOnce();
  });

  it('retains and replays the first embedding identity draft after confirmation', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...firstSetupSettings,
      processing: {
        ...firstSetupSettings.processing,
        embedding: endpoint('https://embedding.example.test/v1'),
      },
      runtime: { ok: true, result: 'completed_empty', state: 'disabled' },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={firstSetupSettings}
        maintenance={{ status: 'ok', data_exists: false, can_clear: true, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')[1];
    const embeddingModel = screen.getAllByPlaceholderText('memory.settings.modelPlaceholder')[1];
    await user.type(embeddingBaseUrl, 'https://embedding.example.test/v1');
    await user.type(embeddingModel, 'model-1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    expect(api.saveMemorySettings).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));
    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: {
        embedding: {
          base_url: 'https://embedding.example.test/v1',
          model: 'model-1',
        },
      },
      confirm_rebuild: true,
    }));
  });

  it('saves a first embedding API key without rebuild confirmation', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...firstSetupSettings,
      processing: {
        ...firstSetupSettings.processing,
        embedding: { ...emptyEndpoint, has_api_key: true },
      },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={firstSetupSettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingApiKey = screen.getAllByPlaceholderText('memory.settings.apiKeyPlaceholder')[1];
    await user.type(embeddingApiKey, 'embed-key');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: { embedding: { api_key: 'embed-key' } },
    }));
    expect(screen.queryByRole('button', { name: 'confirm-rebuild' })).toBeNull();
  });

  it('shows Retry rebuild under a pending marker', async () => {
    api.rebuildMemoryRuntime.mockResolvedValue({ ok: true, result: 'completed' });
    const onReloadSettings = vi.fn();
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, rebuild_required: true }}
        maintenance={{ status: 'ok', data_exists: true, can_clear: true, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={onReloadSettings}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(screen.getByText('memory.settings.rebuildRequiredTitle')).toBeTruthy();
    const inputs = screen.getAllByPlaceholderText(
      'memory.settings.baseUrlPlaceholder',
    ) as HTMLInputElement[];
    expect(inputs.every((input) => !input.disabled)).toBe(true);
    expect(
      (screen.getByRole('switch', { name: 'memory.settings.enableLabel' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: 'memory.settings.save' }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    const retry = screen.getByRole('button', {
      name: 'memory.settings.retryRebuild',
    }) as HTMLButtonElement;
    expect(retry.disabled).toBe(false);
    await user.click(retry);
    await waitFor(() => expect(api.rebuildMemoryRuntime).toHaveBeenCalled());
    expect(onReloadSettings).toHaveBeenCalled();
  });

  it('keeps a pending embedding identity editable and reconfirms its correction', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      rebuild_required: true,
      processing: {
        ...legacySettings.processing,
        embedding: endpoint('https://corrected-embedding.example.test/v1'),
      },
      runtime: { ok: false, error: 'memory_rebuild_failed', result: 'failed' },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, rebuild_required: true }}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText(
      'memory.settings.baseUrlPlaceholder',
    )[1] as HTMLInputElement;
    expect(embeddingBaseUrl.disabled).toBe(false);
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://corrected-embedding.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    expect(api.saveMemorySettings).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));
    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: {
        embedding: { base_url: 'https://corrected-embedding.example.test/v1' },
      },
      confirm_rebuild: true,
    }));
  });

  it('keeps a pending LLM identity editable so a failed probe can be corrected', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      rebuild_required: true,
      processing: {
        ...legacySettings.processing,
        llm: {
          ...legacySettings.processing.llm,
          base_url: 'https://corrected-llm.example.test/v1',
          model: 'corrected-model',
        },
      },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, rebuild_required: true }}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const llmBaseUrl = screen.getAllByPlaceholderText(
      'memory.settings.baseUrlPlaceholder',
    )[0] as HTMLInputElement;
    const llmModel = screen.getAllByPlaceholderText(
      'memory.settings.modelPlaceholder',
    )[0] as HTMLInputElement;
    expect(llmBaseUrl.disabled).toBe(false);
    expect(llmModel.disabled).toBe(false);
    await user.clear(llmBaseUrl);
    await user.type(llmBaseUrl, 'https://corrected-llm.example.test/v1');
    await user.clear(llmModel);
    await user.type(llmModel, 'corrected-model');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      processing: {
        llm: {
          base_url: 'https://corrected-llm.example.test/v1',
          model: 'corrected-model',
        },
      },
    }));
    expect(screen.queryByRole('button', { name: 'confirm-rebuild' })).toBeNull();
  });

  it('keeps API-key correction available under a pending marker', async () => {
    api.saveMemorySettings.mockResolvedValue({ ...legacySettings, rebuild_required: true });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, rebuild_required: true }}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const apiKeys = screen.getAllByPlaceholderText(
      'memory.settings.apiKeyPlaceholder',
    ) as HTMLInputElement[];
    expect(apiKeys.every((input) => !input.disabled)).toBe(true);
    await user.type(apiKeys[1], 'corrected-key');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() =>
      expect(api.saveMemorySettings).toHaveBeenCalledWith({
        processing: { embedding: { api_key: 'corrected-key' } },
      }),
    );
    expect(showToast).toHaveBeenCalledWith('memory.settings.saved', 'success');
  });

  it('disables settings and retry while a rebuild request is running', () => {
    render(
      <MemorySettingsPanel
        settings={{ ...legacySettings, rebuild_required: true }}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        rebuildBusy
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    expect(
      (screen.getByRole('button', { name: 'memory.settings.save' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole('button', {
        name: 'memory.settings.retryingRebuild',
      }) as HTMLButtonElement).disabled,
    ).toBe(true);
    const inputs = [
      ...screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder'),
      ...screen.getAllByPlaceholderText('memory.settings.modelPlaceholder'),
      ...screen.getAllByPlaceholderText('memory.settings.apiKeyPlaceholder'),
    ] as HTMLInputElement[];
    expect(inputs.every((input) => input.disabled)).toBe(true);
  });

  it.each([false, true])('disables Clear when authoritative maintenance refuses it with data_exists=%s', async (dataExists) => {
    const onClearAll = vi.fn();
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={{ status: 'ok', data_exists: dataExists, can_clear: false, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={onClearAll}
        clearing={false}
      />,
    );

    const clear = screen.getByRole('button', { name: 'memory.clear.button' }) as HTMLButtonElement;
    expect(clear.disabled).toBe(true);
    await user.click(clear);
    expect(onClearAll).not.toHaveBeenCalled();
  });

  it('keeps Clear available for an explicit retry after a failed marker', async () => {
    const onClearAll = vi.fn();
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={{
          status: 'ok',
          data_exists: true,
          can_clear: true,
          clear_in_progress: {
            operation_id: 'clear-1',
            state: 'failed',
            occurred_at: '2026-08-13T00:00:00Z',
            error_code: 'memory_clear_marker_unreadable',
          },
        }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={onClearAll}
        clearing={false}
      />,
    );

    const clear = screen.getByRole('button', { name: 'memory.clear.button' }) as HTMLButtonElement;
    expect(clear.disabled).toBe(false);
    await user.click(clear);
    expect(onClearAll).toHaveBeenCalledOnce();
  });

  it('does not announce rebuild completion for ordinary reconcile results', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      processing: {
        ...legacySettings.processing,
        llm: endpoint('https://new.example.test/v1'),
      },
      runtime: { ok: true, state: 'ready' },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const llmBaseUrl = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')[0];
    await user.clear(llmBaseUrl);
    await user.type(llmBaseUrl, 'https://new.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith('memory.settings.saved', 'success'));
    expect(showToast).not.toHaveBeenCalledWith('memory.settings.rebuildCompleted', 'success');
  });

  it('announces rebuild completion only for confirmed rebuild saves', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      rebuild_required: false,
      processing: {
        ...legacySettings.processing,
        embedding: endpoint('https://new-embedding.example.test/v1'),
      },
      runtime: { ok: true, result: 'completed' },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={{ status: 'ok', data_exists: true, can_clear: true, clear_in_progress: null }}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')[1];
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://new-embedding.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('memory.settings.rebuildCompleted', 'success'),
    );
  });

  it('surfaces the specific confirmed rebuild failure', async () => {
    api.saveMemorySettings.mockResolvedValue({
      ...legacySettings,
      rebuild_required: true,
      runtime: { ok: false, error: 'memory_rebuild_root_busy', result: 'root_busy' },
    });
    const user = userEvent.setup();
    render(
      <MemorySettingsPanel
        settings={legacySettings}
        maintenance={null}
        maintenanceError={null}
        dependencyReady
        onSaved={() => undefined}
        onReloadSettings={() => undefined}
        onReloadMaintenance={() => undefined}
        onClearAll={() => undefined}
        clearing={false}
      />,
    );

    const embeddingBaseUrl = screen.getAllByPlaceholderText('memory.settings.baseUrlPlaceholder')[1];
    await user.clear(embeddingBaseUrl);
    await user.type(embeddingBaseUrl, 'https://new-embedding.example.test/v1');
    await user.click(screen.getByRole('button', { name: 'memory.settings.save' }));
    await user.click(await screen.findByRole('button', { name: 'confirm-rebuild' }));

    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('errors.memory_rebuild_root_busy', 'error'),
    );
  });

});
