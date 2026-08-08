/* @vitest-environment jsdom */

import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useBackendRuntime } from './useBackendRuntime';


const mocks = vi.hoisted(() => ({
  api: {
    getConfig: vi.fn(),
    detectCli: vi.fn(),
    installAgent: vi.fn(),
    saveConfig: vi.fn(),
  },
  showToast: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => mocks.api }));
vi.mock('@/context/ToastContext', () => ({
  useToast: () => ({ showToast: mocks.showToast }),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.api.getConfig.mockResolvedValue({
    agents: { claude: { enabled: true, cli_path: 'claude' } },
  });
  mocks.api.detectCli.mockImplementation(async (binary: string) => ({
    found: true,
    path: binary,
  }));
  mocks.api.installAgent.mockResolvedValue({
    ok: true,
    message: 'installed',
    path: '/private/backends/claude',
  });
  mocks.api.saveConfig.mockResolvedValue({});
});

const mountRuntime = async () => {
  const hook = renderHook(() =>
    useBackendRuntime({ backend: 'claude', defaultCli: 'claude' }),
  );
  await waitFor(() => expect(hook.result.current.loaded).toBe(true));
  await waitFor(() => expect(hook.result.current.detecting).toBe(false));
  return hook;
};

describe('persisted backend install paths', () => {
  it('does not mark a lifecycle install as an unsaved path edit', async () => {
    const hook = await mountRuntime();

    act(() => hook.result.current.setCliPath('/user/edit'));
    expect(hook.result.current.runtimeDirty).toBe(true);

    await act(async () => {
      await hook.result.current.handleLifecycleChanged({
        installedPath: '/private/backends/claude',
      });
    });

    expect(hook.result.current.cliPath).toBe('/private/backends/claude');
    expect(hook.result.current.runtimeDirty).toBe(false);
  });

  it('does not mark the direct install action as an unsaved path edit', async () => {
    const hook = await mountRuntime();

    await act(async () => {
      await hook.result.current.install();
    });

    expect(hook.result.current.cliPath).toBe('/private/backends/claude');
    expect(hook.result.current.runtimeDirty).toBe(false);
  });
});
