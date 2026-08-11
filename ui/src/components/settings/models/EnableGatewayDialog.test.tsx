// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { EnableGatewayDialog } from './EnableGatewayDialog';
import { modelsApi } from './modelsApi';
import type { AgentSupply, RuntimeDependency } from './types';

const direct: AgentSupply = {
  backend: 'claude',
  mode: 'direct',
  menu_kind: 'fixed',
  sources: { order: [], eligibility: [] },
};

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  contract_version: 5,
  manifest: { name: 'cliproxyapi', version: '1', source_sha: 'a'.repeat(40), assets: [] },
  status: { installed_version: health === 'not_installed' ? null : '1', verified: health !== 'not_installed', health },
});

const renderDialog = (runtimeValue: RuntimeDependency, props: Partial<React.ComponentProps<typeof EnableGatewayDialog>> = {}) => render(
  <I18nextProvider i18n={i18n}>
    <EnableGatewayDialog
      agent={direct}
      runtime={runtimeValue}
      onClose={vi.fn()}
      onAdopted={vi.fn()}
      onRuntime={vi.fn()}
      trackWrite={async (work) => work()}
      {...props}
    />
  </I18nextProvider>,
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('EnableGatewayDialog', () => {
  it('names the dependency and changes the primary when runtime is missing', () => {
    renderDialog(runtime('not_installed'));

    expect(screen.getByText(/cliproxyapi/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Install and switch' })).toBeTruthy();
  });

  it('keeps the dialog open with cause-neutral failure copy and a retryable primary', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([direct]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime('ok'));
    vi.spyOn(modelsApi, 'setAgentMode').mockRejectedValue(new TypeError('offline'));
    const onClose = vi.fn();
    renderDialog(runtime('ok'), { onClose });

    await user.click(screen.getByRole('button', { name: 'Switch to gateway' }));

    expect(await screen.findByText('The switch to the gateway did not go through')).toBeTruthy();
    expect(screen.getByText(/PATCH \/api\/models\/agents\/claude\/mode · The gateway could not be reached/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Switch to gateway' })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });
});
