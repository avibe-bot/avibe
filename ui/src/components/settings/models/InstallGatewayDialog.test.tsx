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
    const installing = runtime('installing');
    const install = vi.spyOn(modelsApi, 'installRuntime').mockResolvedValueOnce(installing);
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
    expect(onRuntime).toHaveBeenCalledWith(installing);
  });
});
