/* @vitest-environment jsdom */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { ComputerUseStatusLine } from './ComputerUseStatusLine';
import { computerUseStatusCopy } from './computerUseStatusCopy';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../../lib/apiFetch', () => ({ apiFetch }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(() => {
  cleanup();
  apiFetch.mockReset();
});

describe('computerUseStatusCopy', () => {
  it('maps permission reasons to localized copy without exposing raw reason codes', () => {
    const copy = computerUseStatusCopy(
      { status: 'needs_permission', reason: 'screen_recording' },
      (key) => key,
    );
    expect(copy.label).toBe('workbench.home.computerUse.needsPermission');
    expect(copy.detail).toBe('workbench.home.computerUse.screenRecordingDetail');
    expect(copy.detail).not.toContain('screen_recording');
  });

  it('keeps the Screen Recording relaunch guidance in both consumed locales', () => {
    expect(en.workbench.home.computerUse.screenRecordingDetail).toContain('quit and reopen');
    expect(zh.workbench.home.computerUse.screenRecordingDetail).toContain('退出并重新打开');
  });

  it('keeps unknown permission reasons localized', () => {
    const copy = computerUseStatusCopy(
      { status: 'needs_permission', reason: 'future_permission' },
      (key) => key,
    );
    expect(copy.detail).toBe('workbench.home.computerUse.unknownPermissionDetail');
    expect(copy.detail).not.toContain('future_permission');
  });

  it('distinguishes unavailable runtime from an unsupported old runtime', () => {
    expect(computerUseStatusCopy(
      { status: 'needs_runtime', reason: 'runtime_unavailable' },
      (key) => key,
    ).detail).toContain('runtimeUnavailableDetail');
    expect(computerUseStatusCopy(
      { status: 'needs_runtime', reason: 'runtime_too_old' },
      (key) => key,
    ).detail).toContain('runtimeTooOldDetail');
  });

  it.each([
    ['state_unwritable', 'stateUnwritableDetail'],
    ['assets_invalid', 'assetsInvalidDetail'],
    ['spawn_failed', 'spawnFailedDetail'],
    ['daemon_exited', 'daemonExitedDetail'],
    ['health_timeout', 'healthTimeoutDetail'],
    ['bundle_identity', 'bundleIdentityDetail'],
    ['ax_capability', 'axCapabilityDetail'],
    ['capture_failed', 'captureFailedDetail'],
    ['driver_health_failed', 'driverHealthFailedDetail'],
    ['endpoint_busy', 'endpointBusyDetail'],
    ['endpoint_unremovable', 'endpointUnremovableDetail'],
    ['socket_unreachable', 'socketUnreachableDetail'],
    ['socket_path_too_long', 'socketPathTooLongDetail'],
  ])('maps error reason %s to dedicated localized remediation', (reason, key) => {
    const detail = computerUseStatusCopy(
      { status: 'error', reason },
      (copyKey) => copyKey,
    ).detail;
    expect(detail).toContain(key);
    expect(detail).not.toContain(reason);
  });

  it.each([
    ['invalid_state_file', 'invalidStateFileDetail'],
    ['snapshot_invalid', 'snapshotInvalidDetail'],
    ['shell_not_running', 'shellNotRunningDetail'],
    ['daemon_unreachable', 'daemonUnreachableDetail'],
  ])('maps unavailable reason %s to dedicated localized remediation', (reason, key) => {
    const detail = computerUseStatusCopy(
      { status: 'unavailable', reason },
      (copyKey) => copyKey,
    ).detail;
    expect(detail).toContain(key);
    expect(detail).not.toContain(reason);
  });

  it('keeps unknown error and unavailable reasons localized', () => {
    expect(computerUseStatusCopy(
      { status: 'error', reason: 'future_error' },
      (key) => key,
    ).detail).toBe('workbench.home.computerUse.errorDetail');
    expect(computerUseStatusCopy(
      { status: 'unavailable', reason: 'future_unavailable' },
      (key) => key,
    ).detail).toBe('workbench.home.computerUse.unavailableDetail');
  });
});

describe('ComputerUseStatusLine', () => {
  it('renders one read-only line per observed status and refreshes without cache', async () => {
    apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'needs_permission', reason: 'screen_recording' }),
    });

    render(<ComputerUseStatusLine />);

    await waitFor(() => {
      expect(screen.getByTestId('computer-use-status')).toBeTruthy();
    });
    expect(screen.getAllByTestId('computer-use-status')).toHaveLength(1);
    expect(screen.getByTestId('computer-use-status').getAttribute('data-computer-use-status')).toBe(
      'needs_permission',
    );
    expect(screen.getByText('workbench.home.computerUse.screenRecordingDetail')).toBeTruthy();
    expect(screen.queryByText('screen_recording')).toBeNull();
    expect(apiFetch).toHaveBeenCalledWith(
      '/api/desktop/computer-use/status',
      { cache: 'no-store' },
    );
  });

  it('does not render the macOS line on an unsupported host', async () => {
    apiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ supported: false, status: 'unavailable', reason: 'unsupported_host' }),
    });

    render(<ComputerUseStatusLine />);

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalled();
    });
    expect(screen.queryByTestId('computer-use-status')).toBeNull();
  });
});
