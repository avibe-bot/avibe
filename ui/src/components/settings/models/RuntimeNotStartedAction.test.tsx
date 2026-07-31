import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it, vi } from 'vitest';

import zh from '../../../i18n/zh.json';
import {
  ModelsPageActions,
  pollRuntimeStatus,
  RuntimeNotStartedAction,
  startRuntimeWithStatusRefresh,
} from './SettingsModelsPage';
import type { RuntimeDependency } from './types';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'zh',
  resources: { zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

const render = (starting = false): string => renderToStaticMarkup(
  <I18nextProvider i18n={i18n}>
    <RuntimeNotStartedAction starting={starting} onStart={vi.fn()} />
  </I18nextProvider>,
);

const runtime = (health: RuntimeDependency['status']['health']): RuntimeDependency => ({
  contract_version: 4,
  manifest: { name: 'cliproxyapi', version: '1', source_sha: 'sha', assets: [] },
  status: { installed_version: '1', verified: true, listening: null, health, last_check: null },
});

describe('installed but not-started Model Hub runtime', () => {
  it('renders neutral lazy-start copy with a quiet explicit start affordance', () => {
    const html = render();

    expect(html).toContain(zh.settings.models.runtime.notStarted);
    expect(html).toContain(zh.settings.models.runtime.startNow);
    expect(html).toContain('text-muted');
    expect(html).not.toContain(zh.settings.models.modelStatus.runtime);
  });

  it('shows a disabled pending state while the explicit start request runs', () => {
    const html = render(true);

    expect(html).toContain(zh.settings.models.runtime.starting);
    expect(html).toContain('disabled=""');
  });

  it('refreshes authoritative health after an explicit start fails', async () => {
    const api = {
      startRuntime: vi.fn().mockRejectedValue(new Error('start failed')),
      getRuntimeStatus: vi.fn().mockResolvedValue(runtime('down')),
    };

    const result = await startRuntimeWithStatusRefresh(api);

    expect(result).toEqual({ runtime: runtime('down'), failed: true });
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

  it('surfaces an automatic runtime transition on the next idle poll', async () => {
    vi.useFakeTimers();
    try {
      const nextRuntime = runtime('ok');
      const api = { getRuntimeStatus: vi.fn().mockResolvedValue(nextRuntime) };
      const onRuntime = vi.fn();
      const stop = pollRuntimeStatus(api, onRuntime, 100);

      await vi.advanceTimersByTimeAsync(100);

      expect(onRuntime).toHaveBeenCalledOnce();
      expect(onRuntime).toHaveBeenCalledWith(nextRuntime);
      stop();
    } finally {
      vi.useRealTimers();
    }
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

      stop();
      resolveStatus?.(runtime('ok'));
      await Promise.resolve();
      expect(onRuntime).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(100);
      expect(api.getRuntimeStatus).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps real configuration issues visible beside lazy-start status', () => {
    const html = renderToStaticMarkup(
      <I18nextProvider i18n={i18n}>
        <ModelsPageActions
          runtimeNotStarted
          startingRuntime={false}
          issueCount={2}
          issuesOnly={false}
          onStartRuntime={vi.fn()}
          onFocusIssues={vi.fn()}
        />
      </I18nextProvider>,
    );

    expect(html).toContain(zh.settings.models.runtime.notStarted);
    expect(html).toContain(i18n.t('settings.models.status.needsAction', { count: 2 }));
  });

  it('wires only the not_started state to the POST-backed start action', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    const page = readFileSync(join(here, 'SettingsModelsPage.tsx'), 'utf8');
    const api = readFileSync(join(here, 'modelsApi.ts'), 'utf8');

    expect(page).toMatch(/runtime\?\.status\.health === 'not_started'/);
    expect(page).toMatch(/startRuntimeWithStatusRefresh\(modelsApi\)/);
    expect(api).toMatch(/'\/api\/models\/runtime\/start', jsonInit\('POST'\)/);
  });
});
