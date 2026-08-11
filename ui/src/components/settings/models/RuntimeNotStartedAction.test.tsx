import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import zh from '../../../i18n/zh.json';
import { pollRuntimeStatus, startRuntimeWithStatusRefresh } from './runtimeLifecycle';
import { RuntimePill } from './SettingsModelsPage';
import type { RuntimeDependency } from './types';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  resources: { zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const runtime = (
  health: RuntimeDependency['status']['health'],
  withAsset = false,
  hostPlatform?: string,
): RuntimeDependency => ({
  contract_version: 5,
  // #1326 runtime-dependency shape: absent host keeps any server asset installable.
  ...(hostPlatform === undefined ? {} : { host_platform: hostPlatform }),
  manifest: {
    name: 'cliproxyapi',
    version: '1',
    source_sha: 'sha',
    assets: withAsset ? [{
      platform: 'darwin-arm64',
      url: 'https://example.invalid/runtime',
      size_bytes: 1,
      sha256: '0'.repeat(64),
    }] : [],
  },
  status: { installed_version: '1', verified: true, listening: null, health, last_check: null },
});

const renderPill = (
  health: RuntimeDependency['status']['health'],
  options: { withAsset?: boolean; hostPlatform?: string; statusUnread?: boolean; starting?: boolean } = {},
): string => renderToStaticMarkup(
  <I18nextProvider i18n={i18n}>
    <RuntimePill
      runtime={runtime(health, options.withAsset, options.hostPlatform)}
      statusUnread={options.statusUnread ?? false}
      starting={options.starting ?? false}
      onStart={vi.fn()}
      onInstall={vi.fn()}
    />
  </I18nextProvider>,
);

describe('Model Hub runtime pill', () => {
  it('maps every runtime health to its registered shell copy and activation', () => {
    expect(renderPill('ok')).toContain(zh.settings.models.shell.running);
    expect(renderPill('ok')).not.toContain('<button');
    expect(renderPill('degraded')).toContain(zh.settings.models.shell.degraded);
    expect(renderPill('degraded')).not.toContain('<button');
    expect(renderPill('down')).toContain(zh.settings.models.shell.stopped);
    expect(renderPill('down')).toContain('<button');
    expect(renderPill('not_started')).toContain(zh.settings.models.shell.notStarted);
    expect(renderPill('not_started')).toContain('<button');
  });

  it('uses server host evidence when present and keeps absent-host installation available', () => {
    expect(renderPill('not_installed', { withAsset: true })).toContain(zh.settings.models.shell.notInstalled);
    expect(renderPill('not_installed', { withAsset: true })).toContain('<button');
    expect(renderPill('not_installed')).toContain(zh.settings.models.shell.unsupported);
    expect(renderPill('not_installed')).not.toContain('<button');
    expect(renderPill('not_installed', { withAsset: true, hostPlatform: 'linux-amd64' })).toContain(zh.settings.models.shell.unsupported);
    expect(renderPill('not_installed', { withAsset: true, hostPlatform: 'linux-amd64' })).not.toContain('<button');
  });

  it('renders the pending and unread projections without claiming healthy state', () => {
    expect(renderPill('ok', { starting: true })).toContain(zh.settings.models.shell.starting);
    expect(renderPill('ok', { starting: true })).not.toContain('<button');
    expect(renderPill('ok', { statusUnread: true })).toContain(zh.settings.models.shell.stopped);
    expect(renderPill('ok', { statusUnread: true })).not.toContain(zh.settings.models.shell.running);
  });

  it('refreshes authoritative health after an explicit start fails', async () => {
    const api = {
      startRuntime: vi.fn().mockRejectedValue(new Error('start failed')),
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('down')),
    };

    await expect(startRuntimeWithStatusRefresh(api)).resolves.toEqual({ runtime: runtime('down'), failed: true });
    expect(api.startRuntime).toHaveBeenCalledOnce();
    expect(api.getRuntimeStatus).toHaveBeenCalledOnce();
  });

  it('drops the stale idle snapshot when post-failure health cannot be read', async () => {
    const api = {
      startRuntime: vi.fn().mockRejectedValue(new Error('start failed')),
      getRuntimeStatus: vi.fn().mockRejectedValue(new Error('status failed')),
    };

    await expect(startRuntimeWithStatusRefresh(api)).resolves.toEqual({ runtime: null, failed: true });
  });

  it('accepts healthy and degraded authoritative results', async () => {
    for (const health of ['ok', 'degraded'] as const) {
      const api = {
        startRuntime: vi.fn().mockResolvedValue(runtime(health)),
        getRuntimeStatus: vi.fn(),
      };

      await expect(startRuntimeWithStatusRefresh(api)).resolves.toEqual({ runtime: runtime(health), failed: false });
      expect(api.getRuntimeStatus).not.toHaveBeenCalled();
    }
  });

  it('accepts an authoritative healthy read after the start response is lost', async () => {
    const api = {
      startRuntime: vi.fn().mockRejectedValue(new Error('response lost')),
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('ok')),
    };

    await expect(startRuntimeWithStatusRefresh(api)).resolves.toEqual({ runtime: runtime('ok'), failed: false });
  });

  it('polls read-only health while idle and cancels stale async writes', async () => {
    vi.useFakeTimers();
    try {
      let resolveStatus: ((value: RuntimeDependency) => void) | undefined;
      const api = {
        getRuntimeStatus: vi.fn().mockImplementation(() => new Promise<RuntimeDependency>((resolve) => {
          resolveStatus = resolve;
        })),
      };
      const onRuntime = vi.fn();
      const stop = pollRuntimeStatus(api, onRuntime, 100);

      await vi.advanceTimersByTimeAsync(100);
      expect(api.getRuntimeStatus).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(500);
      expect(api.getRuntimeStatus).toHaveBeenCalledOnce();

      stop();
      resolveStatus?.(runtime('ok'));
      await Promise.resolve();
      expect(onRuntime).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
