import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { computerUseStatusCopy } from './computerUseStatusCopy';

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
    ['controller_unavailable', 'controllerUnavailableDetail'],
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

  it('keeps Controller-unavailable remediation aligned in both locales', () => {
    expect(en.workbench.home.computerUse.controllerUnavailableDetail).toContain('Controller');
    expect(zh.workbench.home.computerUse.controllerUnavailableDetail).toContain('Controller');
  });
});
