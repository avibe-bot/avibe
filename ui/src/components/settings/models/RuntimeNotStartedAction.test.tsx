import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import zh from '../../../i18n/zh.json';
import { failRegionRead, readyRegion, unreadRegion } from './regionRead';
import { agentHasLiveChainProjection, freshRuntimeProjection, pollRuntimeStatus, resumeInstallAndStartRuntime, startRuntimeWithStatusRefresh } from './runtimeLifecycle';
import { RuntimePill } from './SettingsModelsPage';
import type { AgentSupply, RuntimeDependency } from './types';

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
    contract_version: 8,
    ...(hostPlatform === undefined ? {} : { host_platform: hostPlatform }),
    manifest: resolution === 'unresolved'
      ? { name: 'cliproxyapi', resolution, assets: [] }
      : { name: 'cliproxyapi', resolution, version: '1', source_sha: 'a'.repeat(40), assets },
    status: { installed_version: '1', verified: true, listening: null, health, last_check: null },
  };
};

const renderPill = (
  health: RuntimeDependency['status']['health'],
  options: { withAsset?: boolean; hostPlatform?: string; unread?: boolean; starting?: boolean } = {},
): string => renderToStaticMarkup(
  <I18nextProvider i18n={i18n}>
    <RuntimePill
      read={options.unread
        ? unreadRegion()
        : readyRegion(runtime(health, options.withAsset, options.hostPlatform))}
      starting={options.starting ?? false}
    />
  </I18nextProvider>,
);

describe('Model Hub runtime pill', () => {
  it('exposes chain projections only for running Hub backends', () => {
    const agent = { backend: 'claude', mode: 'hub' } as AgentSupply;
    for (const health of ['ok', 'degraded'] as const) {
      expect(agentHasLiveChainProjection(freshRuntimeProjection(readyRegion(runtime(health))), agent)).toBe(true);
    }
    for (const health of ['down', 'not_started', 'not_installed', 'installing'] as const) {
      expect(agentHasLiveChainProjection(freshRuntimeProjection(readyRegion(runtime(health))), agent)).toBe(false);
    }
    expect(agentHasLiveChainProjection(freshRuntimeProjection(readyRegion(runtime('ok'))), { ...agent, mode: 'direct' })).toBe(false);
    expect(agentHasLiveChainProjection(null, agent)).toBe(false);
  });

  it('does not promote a retained runtime snapshot into a fresh projection', () => {
    const retained = failRegionRead(readyRegion(runtime('ok')));

    expect(freshRuntimeProjection(retained)).toBeNull();
  });

  it('maps every runtime health to its registered shell copy without owning activation', () => {
    expect(renderPill('ok')).toContain(zh.settings.models.shell.running);
    expect(renderPill('ok')).not.toContain('<button');
    expect(renderPill('degraded')).toContain(zh.settings.models.shell.degraded);
    expect(renderPill('degraded')).not.toContain('<button');
    expect(renderPill('down')).toContain(zh.settings.models.shell.stopped);
    expect(renderPill('down')).not.toContain('<button');
    expect(renderPill('not_started')).toContain(zh.settings.models.shell.notStarted);
    expect(renderPill('not_started')).not.toContain('<button');
  });

  it('uses the server manifest resolution without re-deriving host support', () => {
    expect(renderPill('not_installed', { withAsset: true })).toContain(zh.settings.models.shell.notInstalled);
    expect(renderPill('not_installed', { withAsset: true })).not.toContain('<button');
    expect(renderPill('not_installed')).toContain(zh.settings.models.shell.notInstalled);
    expect(renderPill('not_installed')).not.toContain('<button');
    expect(renderPill('not_installed', { withAsset: true, hostPlatform: 'linux-amd64' })).toContain(zh.settings.models.shell.unsupported);
    expect(renderPill('not_installed', { withAsset: true, hostPlatform: 'linux-amd64' })).not.toContain('<button');
  });

  it('renders the pending and unread projections without claiming healthy state', () => {
    expect(renderPill('ok', { starting: true })).toContain(zh.settings.models.shell.starting);
    expect(renderPill('ok', { starting: true })).not.toContain('<button');
    expect(renderPill('ok', { unread: true })).toContain(zh.settings.models.shell.unread);
    expect(renderPill('ok', { unread: true })).not.toContain(zh.settings.models.shell.stopped);
    expect(renderPill('ok', { unread: true })).not.toContain('<button');
    const stale = renderToStaticMarkup(
      <I18nextProvider i18n={i18n}>
        <RuntimePill
          read={failRegionRead(readyRegion(runtime('ok')))}
          starting={false}
        />
      </I18nextProvider>,
    );
    expect(stale).toContain(zh.settings.models.shell.unread);
    expect(stale).not.toContain('<button');
  });

  it('refreshes authoritative health after an explicit start fails', async () => {
    const api = {
      startRuntime: vi.fn().mockRejectedValue(new Error('start failed')),
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('down')),
    };

    await expect(startRuntimeWithStatusRefresh(api)).resolves.toMatchObject({ runtime: runtime('down'), failed: true });
    expect(api.startRuntime).toHaveBeenCalledOnce();
    expect(api.getRuntimeStatus).toHaveBeenCalledOnce();
  });

  it('drops the stale idle snapshot when post-failure health cannot be read', async () => {
    const api = {
      startRuntime: vi.fn().mockRejectedValue(new Error('start failed')),
      getRuntimeStatus: vi.fn().mockRejectedValue(new Error('status failed')),
    };

    await expect(startRuntimeWithStatusRefresh(api)).resolves.toMatchObject({ runtime: null, failed: true });
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

    await expect(startRuntimeWithStatusRefresh(api)).resolves.toMatchObject({ runtime: runtime('ok'), failed: false });
  });

  it.each([
    ['not_installed', 1, 1, 'ok'],
    ['not_started', 0, 1, 'ok'],
    ['down', 0, 1, 'ok'],
    ['ok', 0, 0, 'ok'],
    ['degraded', 0, 0, 'degraded'],
  ] as const)(
    'resumes install-and-start from %s at the first unproven step',
    async (initialHealth, installCalls, startCalls, finalHealth) => {
      const api = {
        installRuntime: vi.fn().mockResolvedValue(runtime('not_started')),
        startRuntime: vi.fn().mockResolvedValue(runtime('ok')),
        getRuntimeStatus: vi.fn(),
      };

      const result = await resumeInstallAndStartRuntime(api, runtime(initialHealth), vi.fn(), 0);

      expect(api.installRuntime).toHaveBeenCalledTimes(installCalls);
      expect(api.startRuntime).toHaveBeenCalledTimes(startCalls);
      expect(result.failedStep).toBeNull();
      expect(result.runtime?.status.health).toBe(finalHealth);
    },
  );

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

  it('keeps polling after an unread status until an authoritative result arrives', async () => {
    vi.useFakeTimers();
    try {
      const api = {
        getRuntimeStatus: vi.fn()
          .mockRejectedValueOnce(new TypeError('unread'))
          .mockResolvedValueOnce(runtime('ok')),
      };
      const onRuntime = vi.fn();
      const stop = pollRuntimeStatus(api, onRuntime, 100);

      await vi.advanceTimersByTimeAsync(100);
      expect(api.getRuntimeStatus).toHaveBeenCalledOnce();
      expect(onRuntime).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(100);
      expect(api.getRuntimeStatus).toHaveBeenCalledTimes(2);
      expect(onRuntime).toHaveBeenCalledWith(expect.objectContaining({ status: expect.objectContaining({ health: 'ok' }) }));
      stop();
    } finally {
      vi.useRealTimers();
    }
  });
});
