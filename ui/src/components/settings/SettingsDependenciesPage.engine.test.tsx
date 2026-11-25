/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsDependenciesPage } from './SettingsDependenciesPage';
import type { DependencyItem, DependencyReadOptions } from '@/context/ApiContext';
import type { EngineOperationResult } from './engineOperations';

const api = vi.hoisted(() => ({
  installDependency: vi.fn(),
  listDependencies: vi.fn(),
  engineOperationRequest: vi.fn(),
  setEngineAutoUpdate: vi.fn(),
  // The request-to-outcome wait has its own tests (engineOperations.test.ts); here it is one call.
  runEngineOperation: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('@/context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('@/context/ApiContext')>();
  return { ...original, useApi: () => api };
});
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('./engineOperations', () => ({
  runEngineOperation: (_request: unknown, kind: 'check' | 'update') => api.runEngineOperation(kind),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values?.version ? `${key}:${String(values.version)}` : key),
  }),
  Trans: ({ i18nKey, components }: { i18nKey: string; components: Record<string, React.ReactElement> }) => (
    <span>{i18nKey}{components.project}</span>
  ),
}));
vi.mock('./SettingsPageShell', () => ({
  SettingsPageShell: ({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) => <>{actions}{children}</>,
}));

const RUNNING = 'model-hub-engine-v8.0.63-2';
const NEXT = 'model-hub-engine-v8.0.70-1';
const KEY = 'settings.dependencies.engineUpdates';

let engineRow: DependencyItem;
const engine = (extra: Partial<DependencyItem> = {}): DependencyItem => ({
  id: 'model-hub-engine',
  kind: 'tool',
  required: true,
  installed: true,
  version: '8.0.63',
  latest_version: '8.0.63',
  status: 'ready',
  action_class: 'repairable',
  active_release: RUNNING,
  available_release: null,
  auto_update: true,
  update_state: 'up_to_date',
  last_checked_at: '2026-11-01T07:55:00+00:00',
  last_result: null,
  ...extra,
});

const strip = () => document.querySelector<HTMLElement>('[data-engine-updates]');
const card = () => screen.getByText('settings.dependencies.items.model-hub-engine.label').closest<HTMLElement>('.rounded-xl')!;
const stripButton = (name: string) => within(strip()!).getByRole('button', { name });
const renderPage = async () => {
  render(<MemoryRouter><SettingsDependenciesPage /></MemoryRouter>);
  await screen.findByText('settings.dependencies.items.model-hub-engine.label');
  await waitFor(() => expect(api.listDependencies).toHaveBeenCalled());
};

beforeEach(() => {
  engineRow = engine();
  api.listDependencies.mockImplementation(async ({ ids }: DependencyReadOptions = {}) => ({
    ok: true,
    deps: (ids ?? ['model-hub-engine']).map((id) => (id === 'model-hub-engine'
      ? engineRow
      : { id, kind: 'tool', required: false, installed: true, version: '1.0.0', status: 'ready' })),
  }));
  api.runEngineOperation.mockResolvedValue({ ok: true, job_id: 'engine-1', kind: 'check', status: 'succeeded', update_state: 'up_to_date', outcome: 'settled' } satisfies EngineOperationResult);
  api.setEngineAutoUpdate.mockResolvedValue({ ok: true, auto_update: false });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('SettingsDependenciesPage · Model Hub engine updates', () => {
  it('names the running build and says it is up to date, with repair still Reinstall', async () => {
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('current'));
    expect(screen.getByText('settings.dependencies.statusReady · v8.0.63-2')).toBeTruthy();
    // The repair target line gives way to the strip; Reinstall stays what it was.
    expect(screen.queryByText('settings.dependencies.targetVersion:8.0.63')).toBeNull();
    expect(screen.getAllByRole('button', { name: 'settings.dependencies.reinstall' }).length).toBeGreaterThan(0);
    expect(stripButton('update.action.check')).toBeTruthy();
  });

  it('keeps the row as it was in a build with no update channel', async () => {
    engineRow = engine({ update_state: 'uncommissioned', active_release: null });
    await renderPage();
    await screen.findByText('settings.dependencies.targetVersion:8.0.63');
    expect(strip()).toBeNull();
    expect(screen.getByText('settings.dependencies.statusReady · v8.0.63')).toBeTruthy();
  });

  it('never offers an update from the repair fields', async () => {
    engineRow = engine({ latest_version: '8.0.70', has_update: true });
    await renderPage();
    await waitFor(() => expect(strip()).not.toBeNull());
    expect(within(strip()!).queryByRole('button', { name: 'update.action.update' })).toBeNull();
  });

  it('checks in one click and reads the row again', async () => {
    await renderPage();
    await waitFor(() => expect(strip()).not.toBeNull());
    const reads = api.listDependencies.mock.calls.length;
    await userEvent.click(stripButton('update.action.check'));
    await waitFor(() => expect(api.runEngineOperation).toHaveBeenCalledWith('check'));
    await waitFor(() => expect(api.listDependencies.mock.calls.length).toBeGreaterThan(reads));
    expect(showToast).not.toHaveBeenCalled();
  });

  it('updates in one click with no confirmation, and the row itself shows the result', async () => {
    engineRow = engine({ update_state: 'available', available_release: NEXT });
    api.runEngineOperation.mockImplementation(async () => {
      engineRow = engine({ active_release: NEXT, update_state: 'applied', last_result: 'applied' });
      return { ok: true, job_id: 'engine-2', kind: 'update', status: 'succeeded', update_state: 'applied', outcome: 'settled' };
    });
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('available'));
    expect(strip()!.textContent).toContain(`${KEY}.available:8.0.70-1`);
    await userEvent.click(stripButton('update.action.update'));
    await waitFor(() => expect(api.runEngineOperation).toHaveBeenCalledWith('update'));
    await waitFor(() => expect(screen.getByText('settings.dependencies.statusReady · v8.0.70-1')).toBeTruthy());
    expect(strip()?.dataset.engineUpdates).toBe('current');
    // The new build in the badge is the confirmation; an "updated" toast could be wrong after a withdrawal.
    expect(showToast).not.toHaveBeenCalled();
  });

  it('shows the work in the button while this page waits for it', async () => {
    engineRow = engine({ update_state: 'available', available_release: NEXT });
    let finish: (result: EngineOperationResult) => void = () => undefined;
    api.runEngineOperation.mockReturnValue(new Promise<EngineOperationResult>((resolve) => { finish = resolve; }));
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('available'));
    await userEvent.click(stripButton('update.action.update'));
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('updating'));
    expect((stripButton(`${KEY}.updatingButton`) as HTMLButtonElement).disabled).toBe(true);
    // Repair waits for the update too.
    const reinstall = within(card()).getByRole('button', { name: 'settings.dependencies.reinstall' }) as HTMLButtonElement;
    expect(reinstall.disabled).toBe(true);
    engineRow = engine({ update_state: 'deferred', update_reason: 'waiting_for_idle', available_release: NEXT });
    finish({ ok: true, job_id: 'engine-3', kind: 'update', status: 'succeeded', update_state: 'deferred', update_reason: 'waiting_for_idle', outcome: 'settled' });
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('waitingForIdle'));
    // Which build a later try installs is decided then: the sentence names none.
    expect(strip()!.textContent).toContain(`${KEY}.waitingForIdle`);
    expect(strip()!.textContent).not.toContain('8.0.70-1');
    // Nothing failed, so the button is still Update and nothing is toasted.
    expect(stripButton('update.action.update')).toBeTruthy();
    expect(showToast).not.toHaveBeenCalled();
  });

  it('offers Try again after an update this page saw fail, and a check after a fresh read', async () => {
    engineRow = engine({ update_state: 'available', available_release: NEXT });
    api.runEngineOperation.mockImplementation(async () => {
      engineRow = engine({ update_state: 'failed', update_reason: 'engine_operation_failed' });
      return { ok: false, job_id: 'engine-4', kind: 'update', status: 'failed', update_state: 'failed', outcome: 'settled' };
    });
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('available'));
    await userEvent.click(stripButton('update.action.update'));
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('unfinished'));
    expect(stripButton('update.action.retry')).toBeTruthy();
    cleanup();
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('attemptUnfinished'));
    expect(stripButton('update.action.check')).toBeTruthy();
  });

  it('says a refused request in a toast, because the row may not change', async () => {
    api.runEngineOperation.mockResolvedValue({ ok: false, error: 'controller_unavailable', update_state: 'waiting_for_controller', outcome: 'refused' });
    await renderPage();
    await waitFor(() => expect(strip()).not.toBeNull());
    await userEvent.click(stripButton('update.action.check'));
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(`${KEY}.cannotCheck`, 'error'));
  });

  it('claims nothing about an outcome it could not confirm, and reads the row again', async () => {
    engineRow = engine({ update_state: 'available', available_release: NEXT });
    api.runEngineOperation.mockResolvedValue({ ok: false, error: 'job_not_found', outcome: 'unknown' });
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('available'));
    const reads = api.listDependencies.mock.calls.length;
    await userEvent.click(stripButton('update.action.update'));
    await waitFor(() => expect(api.listDependencies.mock.calls.length).toBeGreaterThan(reads));
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('available'));
    expect(showToast).not.toHaveBeenCalled();
  });

  it('does not take a failed check for a failed update', async () => {
    // A request can be answered with the receipt of the other operation, so the receipt says what failed.
    api.runEngineOperation.mockImplementation(async () => {
      engineRow = engine({ update_state: 'failed', update_reason: 'engine_operation_failed' });
      return { ok: false, job_id: 'engine-5', kind: 'check', status: 'failed', update_state: 'failed', outcome: 'settled' };
    });
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('current'));
    await userEvent.click(stripButton('update.action.check'));
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('cannotCheck'));
    expect(stripButton('update.action.retry')).toBeTruthy();
    // A later visit knows only that something did not finish, and names neither.
    cleanup();
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('attemptUnfinished'));
    expect(stripButton('update.action.check')).toBeTruthy();
  });

  it('keeps Check usable while the engine owner recovers after a start', async () => {
    engineRow = engine({ update_state: 'recovering', update_reason: 'engine_journal_unavailable', active_release: null });
    await renderPage();
    // An earlier check may be on record, and none is running: the row claims neither.
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('recovering'));
    expect((stripButton('update.action.check') as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps the repair target line on a channel build whose engine needs repair', async () => {
    engineRow = engine({ status: 'upgrade_required', update_state: 'up_to_date' });
    await renderPage();
    await screen.findByText('settings.dependencies.targetVersion:8.0.63');
    expect(strip()).toBeNull();
  });

  it('reads the row again by itself while an update waits for idle', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      engineRow = engine({ update_state: 'deferred', update_reason: 'waiting_for_idle', available_release: NEXT });
      await renderPage();
      await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('waitingForIdle'));
      // The owner installs it once Model Hub is idle; nobody asked this page to look.
      engineRow = engine({ active_release: NEXT, update_state: 'up_to_date', last_result: 'applied' });
      await vi.advanceTimersByTimeAsync(30_000);
      await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('current'));
      expect(screen.getByText('settings.dependencies.statusReady · v8.0.70-1')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('turns automatic updates off in one click', async () => {
    api.setEngineAutoUpdate.mockImplementation(async (enabled: boolean) => {
      engineRow = engine({ auto_update: enabled });
      return { ok: true, auto_update: enabled };
    });
    await renderPage();
    await waitFor(() => expect(strip()).not.toBeNull());
    const toggle = within(strip()!).getByRole('switch', { name: `${KEY}.autoUpdate` });
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    await userEvent.click(toggle);
    await waitFor(() => expect(api.setEngineAutoUpdate).toHaveBeenCalledWith(false));
    await waitFor(() => expect(within(strip()!).getByRole('switch').getAttribute('aria-checked')).toBe('false'));
  });

  it('says why a stopped engine stopped in place of the install failure', async () => {
    engineRow = engine({
      status: 'error', action_class: 'operator_only', reason: 'model_hub_engine_install_inspection_failed',
      active_release: null, update_state: 'blocked', update_reason: 'engine_withdrawn',
    });
    await renderPage();
    await waitFor(() => expect(strip()?.dataset.engineUpdates).toBe('stopped'));
    expect(within(strip()!).getByRole('alert').textContent).toBe(`${KEY}.stopped`);
    expect(within(card()).queryByText('settings.dependencies.installFailed')).toBeNull();
    expect(within(card()).queryByRole('button', { name: 'settings.dependencies.repair' })).toBeNull();
  });

  it('leaves a broken engine to repair before it talks about updates', async () => {
    engineRow = engine({ status: 'error', reason: 'engine_start_failed', update_state: 'up_to_date' });
    await renderPage();
    await waitFor(() => expect(within(card()).getByRole('button', { name: 'settings.dependencies.repair' })).toBeTruthy());
    expect(strip()).toBeNull();
  });

  it('credits the open-source project beside the title in every build', async () => {
    engineRow = engine({ update_state: 'uncommissioned', active_release: null });
    await renderPage();
    // A tap: a mouse would already have opened it on the way in.
    fireEvent.click(await screen.findByRole('button', { name: 'settings.dependencies.items.model-hub-engine.attributionLabel' }));
    const link = await waitFor(() => {
      const found = screen.getAllByRole('link').find((candidate) => candidate.getAttribute('target') === '_blank');
      expect(found).toBeTruthy();
      return found!;
    });
    expect(link.getAttribute('href')).toBe('https://github.com/router-for-me/CLIProxyAPI');
    expect(link.getAttribute('rel')).toBe('noreferrer');
  });
});
