import { describe, expect, it } from 'vitest';

import { dependencyHasInstallAction, memoryRuntimeSidecarRunning } from './SettingsDependenciesPage.logic';

describe('dependencyHasInstallAction', () => {
  it('hides install and repair actions for unsupported dependencies', () => {
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'unsupported' })).toBe(false);
  });

  it('keeps supported dependency actions unchanged', () => {
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'missing' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'ready' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'node', status: 'missing' })).toBe(false);
  });
});

describe('memoryRuntimeSidecarRunning', () => {
  it('is true only for a successful status whose sidecar source is available', () => {
    expect(memoryRuntimeSidecarRunning({
      status: 'ok',
      source: { status: 'available', observed_at: '2026-08-18T14:44:35.331Z', reason: null },
      health: null,
    })).toBe(true);
  });

  it('stays false when Memory is enabled but the sidecar is not reachable', () => {
    expect(memoryRuntimeSidecarRunning(null)).toBe(false);
    expect(memoryRuntimeSidecarRunning({ status: 'failed', error: 'memory_sidecar_unavailable' })).toBe(false);
    expect(memoryRuntimeSidecarRunning({
      status: 'ok',
      source: { status: 'unavailable', observed_at: null, reason: 'memory_sidecar_unavailable' },
      health: null,
    })).toBe(false);
  });
});
