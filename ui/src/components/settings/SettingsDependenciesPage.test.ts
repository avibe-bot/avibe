import { describe, expect, it } from 'vitest';

import { dependencyHasInstallAction, memoryRuntimeSidecarRunning } from './SettingsDependenciesPage.logic';

describe('dependencyHasInstallAction', () => {
  it('hides install and repair actions for unsupported dependencies', () => {
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'unsupported' })).toBe(false);
  });

  it('keeps supported dependency actions unchanged', () => {
    expect(dependencyHasInstallAction({ id: 'memory-package', status: 'missing' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'missing' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'ready' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'node', status: 'missing' })).toBe(false);
  });

  it('does not offer a healthy Memory Python package reinstall', () => {
    expect(dependencyHasInstallAction({ id: 'memory-package', status: 'ready' })).toBe(false);
  });
});

describe('memoryRuntimeSidecarRunning', () => {
  it.each(['starting', 'running', 'degraded'] as const)(
    'fails closed for the active or uncertain %s state',
    (state) => {
      expect(memoryRuntimeSidecarRunning({
        status: 'ok',
        state,
        reason: null,
        source: { status: 'unavailable', observed_at: null, reason: null },
        health: null,
      })).toBe(true);
    },
  );
  it('stays false when Memory is enabled but the sidecar is not reachable', () => {
    expect(memoryRuntimeSidecarRunning(null)).toBe(false);
    expect(memoryRuntimeSidecarRunning({ status: 'failed', error: 'memory_sidecar_unavailable' })).toBe(false);
    expect(memoryRuntimeSidecarRunning({
      status: 'ok',
      state: 'disabled',
      reason: 'memory_disabled',
      source: { status: 'unavailable', observed_at: null, reason: 'memory_sidecar_unavailable' },
      health: null,
    })).toBe(false);
  });
});
