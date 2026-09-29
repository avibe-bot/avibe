/* @vitest-environment jsdom */

import { useState } from 'react';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BackendLifecycleChip } from './BackendLifecycleChip';

const api = vi.hoisted(() => ({
  getBackendRuntime: vi.fn(),
  installAgent: vi.fn(),
  mutateConfig: vi.fn(),
  restartBackend: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('@/context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('@/context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('@/context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) => (options ? `${key} ${JSON.stringify(options)}` : key),
  }),
}));

const codexPath = '/usr/local/bin/codex';
const updateAvailable = {
  ok: true,
  name: 'codex',
  enabled: true,
  cli_path: codexPath,
  resolved_path: codexPath,
  installed: true,
  current_version: '1.0.0',
  latest_version: '1.1.0',
  has_update: true,
  supports_restart: true,
  process_status: 'running',
};

// What ``install_agent`` returns for a failed upgrade: the headline and hint are
// the server's, already localized; the UI's job is to keep them in view.
const npmLeftover = {
  ok: false,
  message: 'Could not upgrade Codex.',
  code: 'npm_leftover_directory',
  hint: 'npm left a temporary folder behind from an earlier interrupted update: /usr/local/lib/node_modules/@openai/.codex-lD3lp9Ti. Delete that folder, then try again.',
  exit_code: 190,
  output: 'npm error code ENOTEMPTY\nnpm error dest /usr/local/lib/node_modules/@openai/.codex-lD3lp9Ti',
};
const unrecognized = {
  ok: false,
  message: 'Could not upgrade Codex.',
  code: 'install_failed',
  hint: null,
  exit_code: 1,
  output: 'Error: something unexpected happened',
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
};

beforeEach(() => {
  api.getBackendRuntime.mockResolvedValue(updateAvailable);
  api.installAgent.mockResolvedValue({ ok: true, message: '', output: null });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('BackendLifecycleChip', () => {
  it('saves the backend auto-update switch to its own agent config', async () => {
    api.getBackendRuntime.mockResolvedValue({ ...updateAvailable, auto_update: true });
    api.mutateConfig.mockResolvedValue({ ok: true });
    const user = userEvent.setup();

    render(<BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath={codexPath} />);
    await user.click(await screen.findByRole('button', { name: 'backendLifecycle.statusUpdateAvailable' }));
    const toggle = await screen.findByRole('switch', { name: 'backendLifecycle.autoUpdate' });
    expect(toggle.getAttribute('aria-checked')).toBe('true');

    await user.click(toggle);

    expect(api.mutateConfig).toHaveBeenCalledWith([
      { kind: 'set', path: ['agents', 'codex', 'auto_update'], value: false },
    ]);
    await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('false'));
  });

  it('keeps an active upgrade visible across popover dismissal and stale runtime refreshes', async () => {
    const install = deferred<{ ok: boolean; message: string; output: null }>();
    api.installAgent.mockReturnValue(install.promise);
    const user = userEvent.setup();

    render(<BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath={codexPath} />);

    const chip = await screen.findByRole('button', {
      name: 'backendLifecycle.statusUpdateAvailable',
    });
    await user.click(chip);
    await user.click(await screen.findByRole('button', { name: 'backendLifecycle.upgradeNow' }));
    expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusUpdating');

    const probesBeforeDismiss = api.getBackendRuntime.mock.calls.length;
    await user.click(document.body);
    expect(screen.queryByText('backendLifecycle.title')).toBeNull();
    expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusUpdating');
    expect(api.getBackendRuntime).toHaveBeenCalledTimes(probesBeforeDismiss);

    const probesBeforeReopen = api.getBackendRuntime.mock.calls.length;
    await user.click(chip);
    await waitFor(() => {
      expect(api.getBackendRuntime.mock.calls.length).toBeGreaterThan(probesBeforeReopen);
    });
    expect(await screen.findByText('backendLifecycle.upgrading')).toBeTruthy();
    expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusUpdating');
    expect(screen.queryByRole('button', { name: 'backendLifecycle.upgradeNow' })).toBeNull();

    install.resolve({ ok: true, message: '', output: null });
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      'backendLifecycle.upgradeSuccess',
      'success',
    ));
    await waitFor(() => expect(chip.getAttribute('aria-label')).toBe(
      'backendLifecycle.statusUpdateAvailable',
    ));
  });

  it.each([
    ['a recognized failure with its hint', npmLeftover, [npmLeftover.message, npmLeftover.hint]],
    ['an unrecognized failure with the generic message only', unrecognized, [unrecognized.message]],
  ])('keeps %s and the installer output in the popover until the next attempt', async (_case, failure, lines) => {
    api.installAgent.mockResolvedValue(failure);
    const user = userEvent.setup();

    render(<BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath={codexPath} />);
    const chip = await screen.findByRole('button', { name: 'backendLifecycle.statusUpdateAvailable' });
    await user.click(chip);
    await user.click(await screen.findByRole('button', { name: 'backendLifecycle.upgradeNow' }));

    await waitFor(() => expect(showToast).toHaveBeenCalledWith('Could not upgrade Codex.', 'error'));
    // Dismissing the popover is when the toast would have been the only record.
    await user.click(document.body);
    await user.click(chip);
    const outcome = await screen.findByRole('alert');
    expect([...outcome.querySelectorAll(':scope > p')].map((line) => line.textContent)).toEqual(lines);
    expect(within(outcome).getByText('agentDetection.showOutput')).toBeTruthy();
    expect(within(outcome).getByText(`agentDetection.exitCode {"code":${failure.exit_code}}`)).toBeTruthy();
    expect(outcome.querySelector('pre')?.textContent).toBe(failure.output);

    const retry = deferred<{ ok: boolean; message: string; output: null }>();
    api.installAgent.mockReturnValue(retry.promise);
    await user.click(screen.getByRole('button', { name: 'backendLifecycle.upgradeNow' }));
    expect(screen.queryByRole('alert')).toBeNull();
    retry.resolve({ ok: true, message: '', output: null });
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('backendLifecycle.upgradeSuccess', 'success'));
  });

  it('retires a failure once the host detects a different executable', async () => {
    api.installAgent.mockResolvedValue(npmLeftover);
    const user = userEvent.setup();

    // A Settings card has no refresh generation; only the path says which CLI it shows.
    const { rerender } = render(<BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath={codexPath} />);
    const chip = await screen.findByRole('button', { name: 'backendLifecycle.statusUpdateAvailable' });
    await user.click(chip);
    await user.click(await screen.findByRole('button', { name: 'backendLifecycle.upgradeNow' }));
    expect((await screen.findByRole('alert')).textContent).toContain(npmLeftover.hint);

    rerender(<BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath="/opt/homebrew/bin/codex" />);

    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('backendLifecycle.title')).toBeTruthy();
  });

  it('has the host re-detect a CLI a failed upgrade removed, so it offers a reinstall', async () => {
    const disk = { present: true };
    api.installAgent.mockImplementation(async () => {
      disk.present = false;
      api.getBackendRuntime.mockResolvedValue({ ...updateAvailable, resolved_path: null, installed: false, current_version: null, has_update: false });
      return npmLeftover;
    });
    // The host owns detection: it reads the disk again whenever the chip reports a change.
    const Host = () => {
      const [cliStatus, setCliStatus] = useState<'ok' | 'missing'>('ok');
      return (
        <BackendLifecycleChip name="codex" enabled cliStatus={cliStatus} cliPath={codexPath}
          onChanged={() => setCliStatus(disk.present ? 'ok' : 'missing')} />
      );
    };
    const user = userEvent.setup();

    render(<Host />);
    const chip = await screen.findByRole('button', { name: 'backendLifecycle.statusUpdateAvailable' });
    await user.click(chip);
    await user.click(await screen.findByRole('button', { name: 'backendLifecycle.upgradeNow' }));

    await waitFor(() => expect(chip.getAttribute('aria-label')).toBe('backendLifecycle.statusError'));
    expect(screen.getByRole('alert').textContent).toContain(npmLeftover.hint);
    expect(screen.getByRole('button', { name: 'backendLifecycle.reinstall' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'backendLifecycle.restart' })).toBeNull();
  });

  // Every notice the popover can show, most urgent first. Each state below overlaps
  // with something reassuring — a desktop-managed install, a stale "latest" — and
  // the popover must say what is wrong rather than that all is well.
  const NOTICES = [
    'backendLifecycle.upgrading',
    'backendLifecycle.errorHint',
    'backendLifecycle.updateHint',
    'backendLifecycle.desktopManagedHint',
    'backendLifecycle.readyHint',
  ];
  const latest = { ...updateAvailable, current_version: '1.1.0', has_update: false };
  const managed = { managed_by: 'desktop' as const };
  it.each([
    { state: 'ready', runtime: latest, notice: 'backendLifecycle.readyHint' },
    { state: 'desktop-managed', runtime: { ...latest, ...managed }, notice: 'backendLifecycle.desktopManagedHint' },
    { state: 'desktop-managed with an update', runtime: { ...updateAvailable, ...managed }, notice: 'backendLifecycle.updateHint' },
    { state: 'desktop-managed and not found', runtime: { ...latest, ...managed }, cliStatus: 'missing' as const, notice: 'backendLifecycle.errorHint {"name":"Codex"}' },
    { state: 'desktop-managed after a failed upgrade', runtime: { ...updateAvailable, ...managed }, after: { ...latest, ...managed }, notice: null },
    { state: 'reading as latest after a failed upgrade', runtime: updateAvailable, after: latest, notice: null },
    { state: 'still behind after a failed upgrade', runtime: updateAvailable, after: updateAvailable, notice: 'backendLifecycle.updateHint' },
  ])('says what matters most when $state', async ({ runtime, cliStatus = 'ok', after, notice }) => {
    api.getBackendRuntime.mockResolvedValue(runtime);
    api.installAgent.mockImplementation(async () => {
      api.getBackendRuntime.mockResolvedValue(after);
      return unrecognized;
    });
    const user = userEvent.setup();

    render(<BackendLifecycleChip name="codex" enabled cliStatus={cliStatus} cliPath={codexPath} />);
    await user.click(screen.getByRole('button'));
    const popover = (await screen.findByText('backendLifecycle.title')).closest('div.z-50') as HTMLElement;
    const refresh = within(popover).getByRole('button', { name: 'common.refresh' });
    const settled = () => waitFor(() => expect(refresh.hasAttribute('disabled')).toBe(false));
    await settled();
    if (after) {
      await user.click(within(popover).getByRole('button', { name: 'backendLifecycle.upgradeNow' }));
      await settled();
      expect(within(popover).getByRole('alert').textContent).toContain(unrecognized.message);
    }

    expect(NOTICES.filter((key) => popover.textContent?.includes(key))).toEqual(notice ? [notice.split(' ')[0]] : []);
    if (notice) expect(popover.textContent).toContain(notice);
  });
});
