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
    found: true, status: 'ok' as const,
    path: binary,
  }));
  mocks.api.installAgent.mockResolvedValue({
    ok: true,
    message: 'installed',
    path: '/private/backends/claude',
  });
  mocks.api.saveConfig.mockResolvedValue({});
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

const mountRuntime = async () => {
  const hook = renderHook(() =>
    useBackendRuntime({ backend: 'claude', defaultCli: 'claude' }),
  );
  await waitFor(() => expect(hook.result.current.loaded).toBe(true));
  await waitFor(() => expect(hook.result.current.detecting).toBe(false));
  return hook;
};

describe('persisted backend install paths', () => {
  it('retains a missing saved path on initial observation and explicit retry', async () => {
    const path = '/saved/不存在/claude';
    mocks.api.getConfig.mockResolvedValue({ agents: { claude: { enabled: true, cli_path: path } } });
    mocks.api.detectCli.mockResolvedValue({ found: false, path: null, status: 'missing' });
    const hook = await mountRuntime();
    expect(hook.result.current.cliPath).toBe(path);
    expect(hook.result.current.runtimeDirty).toBe(false);
    await act(async () => hook.result.current.detect(path));
    expect(mocks.api.detectCli).toHaveBeenLastCalledWith(path, 'claude');
    expect(hook.result.current.cliPath).toBe(path);
  });

  it('does not overwrite a newer human edit with a missing initial-path completion', async () => {
    const path = '/saved/claude';
    const pending = deferred<{ found: boolean; path: null; status: 'missing' }>();
    mocks.api.getConfig.mockResolvedValue({ agents: { claude: { enabled: true, cli_path: path } } });
    mocks.api.detectCli.mockReturnValue(pending.promise);
    const hook = renderHook(() => useBackendRuntime({ backend: 'claude', showToast: mocks.showToast }));
    await waitFor(() => expect(hook.result.current.loaded).toBe(true));
    act(() => hook.result.current.setCliPath('/new-human/claude'));
    await act(async () => pending.resolve({ found: false, path: null, status: 'missing' }));
    expect(hook.result.current.cliPath).toBe('/new-human/claude');
    expect(hook.result.current.runtimeDirty).toBe(true);
    // The saved path is private hook state; returning to it clears the draft.
    act(() => hook.result.current.setCliPath(path));
    expect(hook.result.current.runtimeDirty).toBe(false);
  });

  it('reports a present but broken CLI without converting presence to health', async () => {
    mocks.api.detectCli.mockResolvedValue({ found: true, path: '/broken/claude', status: 'broken' });
    const hook = await mountRuntime();
    expect(hook.result.current.cliStatus).toBe('broken');
    expect(mocks.api.detectCli).toHaveBeenCalledWith('claude', 'claude');
  });

  it('does not install the saved CLI while the person is inspecting another unsaved path', async () => {
    const hook = await mountRuntime();
    act(() => hook.result.current.setCliPath('/draft/claude'));
    await act(async () => hook.result.current.install());
    expect(mocks.api.installAgent).not.toHaveBeenCalled();
    expect(hook.result.current.runtimeDirty).toBe(true);
  });

  it('does not mistake automatic discovery of the saved command for a human draft', async () => {
    mocks.api.detectCli.mockResolvedValue({ found: true, path: '/resolved/claude', status: 'ok' });
    const hook = await mountRuntime();
    expect(hook.result.current.cliPath).toBe('/resolved/claude');
    expect(hook.result.current.runtimeDirty).toBe(false);
  });

  it('reads the built-in backend as enabled without a config section and rejects CLI work at the hook boundary', async () => {
    const hook = renderHook(() => useBackendRuntime({ backend: 'vibey' }));
    await waitFor(() => expect(hook.result.current.loaded).toBe(true));
    expect(hook.result.current.enabled).toBe(true);
    expect(hook.result.current.cliPath).toBe('');
    await act(async () => {
      await hook.result.current.detect();
      await hook.result.current.install();
      await hook.result.current.onSaveRuntime();
    });
    expect(mocks.api.detectCli).not.toHaveBeenCalled();
    expect(mocks.api.installAgent).not.toHaveBeenCalled();
    expect(hook.result.current.runtimeDirty).toBe(false);
  });

  it('does not mark a lifecycle install as an unsaved path edit', async () => {
    const hook = await mountRuntime();

    await act(async () => {
      await hook.result.current.handleLifecycleChanged({
        installedPath: '/private/backends/claude',
      });
    });

    expect(hook.result.current.cliPath).toBe('/private/backends/claude');
    expect(hook.result.current.runtimeDirty).toBe(false);
  });

  it('preserves a newer draft while adopting the installed saved path', async () => {
    const hook = await mountRuntime();
    act(() => hook.result.current.setCliPath('/user/edit'));
    await act(async () => {
      await hook.result.current.handleLifecycleChanged({ installedPath: '/private/backends/claude' });
    });
    expect(hook.result.current.cliPath).toBe('/user/edit');
    expect(hook.result.current.runtimeDirty).toBe(true);
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
