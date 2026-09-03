// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { createAgentCollectionReadAuthority } from './collectionReadAuthority';
import { EnableGatewayDialog } from './EnableGatewayDialog';
import { modelsApi } from './modelsApi';
import { failRegionRead, readyRegion, unreadRegion, type RegionRead } from './regionRead';
import type { AgentSupply, RuntimeDependency } from './types';

const direct: AgentSupply = {
  backend: 'claude',
  cli_present: true,
  mode: 'direct',
  menu_kind: 'fixed',
  sources: { order: [], eligibility: [] },
};

const runtime = (
  health: RuntimeDependency['status']['health'],
  withAsset = false,
  hostPlatform?: string,
): RuntimeDependency => {
  const assets = withAsset ? [{
      platform: 'darwin-arm64',
      url: 'https://example.invalid/runtime',
      size_bytes: 1,
      sha256: '0'.repeat(64),
    } as const] : [];
  const resolution = !withAsset
    ? 'unresolved' as const
    : hostPlatform === 'linux-amd64' ? 'unsupported' as const : 'resolved' as const;
  return {
    contract_version: 7,
    ...(hostPlatform === undefined ? {} : { host_platform: hostPlatform }),
    manifest: resolution === 'unresolved'
      ? { name: 'cliproxyapi', resolution, assets: [] }
      : { name: 'cliproxyapi', resolution, version: '1', source_sha: 'a'.repeat(40), assets },
    status: { installed_version: health === 'not_installed' ? null : '1', verified: health !== 'not_installed', health },
  };
};

const renderDialog = (runtimeValue: RegionRead<RuntimeDependency>, props: Partial<React.ComponentProps<typeof EnableGatewayDialog>> = {}) => render(
  <I18nextProvider i18n={i18n}>
    <EnableGatewayDialog
      agent={direct}
      runtime={runtimeValue}
      agentReads={createAgentCollectionReadAuthority(modelsApi)}
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
    renderDialog(readyRegion(runtime('not_installed', true)));

    expect(screen.getByText(/cliproxyapi/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Install and switch' })).toBeTruthy();
  });

  it('keeps installation reachable while a remote manifest is uncached', () => {
    renderDialog(readyRegion(runtime('not_installed')));

    expect(screen.getByRole('button', { name: 'Install and switch' })).toBeTruthy();
  });

  it('shares the server manifest resolution with the runtime pill', () => {
    renderDialog(readyRegion(runtime('not_installed', true, 'linux-amd64')));

    expect(screen.queryByRole('button', { name: 'Install and switch' })).toBeNull();
    expect((screen.getByRole('button', { name: 'Switch to gateway' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps the dialog open with cause-neutral failure copy and a retryable primary', async () => {
    const user = userEvent.setup();
    vi.spyOn(modelsApi, 'listAgents').mockResolvedValue([direct]);
    vi.spyOn(modelsApi, 'getRuntimeStatus').mockResolvedValue(runtime('ok'));
    vi.spyOn(modelsApi, 'setAgentMode').mockRejectedValue(new TypeError('offline'));
    const onClose = vi.fn();
    renderDialog(readyRegion(runtime('ok')), { onClose });

    await user.click(screen.getByRole('button', { name: 'Switch to gateway' }));

    expect(await screen.findByText('The switch to the gateway did not go through')).toBeTruthy();
    expect(screen.getByText(/PATCH \/api\/models\/agents\/claude\/mode · The gateway could not be reached/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Switch to gateway' })).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps adoption disabled while runtime status is unread', async () => {
    const user = userEvent.setup();
    const setMode = vi.spyOn(modelsApi, 'setAgentMode');
    renderDialog(unreadRegion<RuntimeDependency>());

    const confirm = screen.getByRole('button', { name: 'Switch to gateway' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(screen.getByText('Gateway status unavailable')).toBeTruthy();

    await user.click(confirm);
    expect(setMode).not.toHaveBeenCalled();
  });

  it('does not use a retained runtime value to choose an adoption action', () => {
    renderDialog(failRegionRead(readyRegion(runtime('not_installed', true))));

    expect(screen.queryByRole('button', { name: 'Install and switch' })).toBeNull();
    expect((screen.getByRole('button', { name: 'Switch to gateway' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Gateway status unavailable')).toBeTruthy();
  });
});
