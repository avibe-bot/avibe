// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { InstallGatewayDialog } from './InstallGatewayDialog';
import { modelsApi } from './modelsApi';
import type { RuntimeDependency } from './types';

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  contract_version: 5,
  manifest: {
    name: 'cliproxyapi',
    version: '1',
    source_sha: 'sha',
    assets: [{ platform: 'darwin-arm64', url: 'https://example.invalid/runtime', size_bytes: 1, sha256: '0'.repeat(64) }],
  },
  status: { installed_version: null, verified: false, listening: null, health, last_check: null },
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('InstallGatewayDialog', () => {
  it('confirms through the dedicated install route and keeps start separate', async () => {
    const installed = runtime('not_started');
    const install = vi.spyOn(modelsApi, 'installRuntime').mockResolvedValueOnce(installed);
    const start = vi.spyOn(modelsApi, 'startRuntime');
    const onRuntime = vi.fn();
    render(
      <I18nextProvider i18n={i18n}>
        <InstallGatewayDialog runtime={runtime('not_installed')} onClose={vi.fn()} onRuntime={onRuntime} />
      </I18nextProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /Install and start|安装并启动/i }));

    await waitFor(() => expect(install).toHaveBeenCalledOnce());
    expect(start).not.toHaveBeenCalled();
    expect(onRuntime).toHaveBeenCalledWith(installed);
  });

  it('keeps the dialog open and offers retry when a background install fails', async () => {
    let settleInstall: ((value: RuntimeDependency) => void) | undefined;
    vi.spyOn(modelsApi, 'installRuntime').mockImplementation(() => new Promise((resolve) => { settleInstall = resolve; }));
    const onClose = vi.fn();
    const view = render(
      <I18nextProvider i18n={i18n}>
        <InstallGatewayDialog runtime={runtime('not_installed')} onClose={onClose} onRuntime={vi.fn()} />
      </I18nextProvider>,
    );
    await userEvent.click(screen.getByRole('button', { name: /Install and start|安装并启动/i }));
    view.rerender(<I18nextProvider i18n={i18n}><InstallGatewayDialog runtime={runtime('installing')} onClose={onClose} onRuntime={vi.fn()} /></I18nextProvider>);
    view.rerender(<I18nextProvider i18n={i18n}><InstallGatewayDialog runtime={{ ...runtime('not_installed'), status: { ...runtime('not_installed').status, error_key: 'settings.models.install.fail.detail' } }} onClose={onClose} onRuntime={vi.fn()} /></I18nextProvider>);

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Try again|重试/i })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    settleInstall?.(runtime('not_installed'));
  });
});
