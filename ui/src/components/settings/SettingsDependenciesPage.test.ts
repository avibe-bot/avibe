import { describe, expect, it } from 'vitest';

import type { DependencyItem } from '@/context/ApiContext';
import {
  dependencyHasInstallAction,
  dependencyIsStartupManaged,
  dependencyIsStartupRepairing,
  dependenciesNeedAutomaticRefresh,
  memoryPackageIsSourceManaged,
  memoryRuntimeSidecarRunning,
} from './SettingsDependenciesPage.logic';

describe('memoryPackageIsSourceManaged', () => {
  const sourcePolicy = Object.freeze({
    id: 'memory-package',
    status: 'error',
    action_class: 'operator_only',
    reason: 'memory_package_source_build',
  } as const);

  it('recognizes source policy without changing dependency evidence or repair admission', () => {
    expect(memoryPackageIsSourceManaged(sourcePolicy)).toBe(true);
    expect(dependencyHasInstallAction(sourcePolicy)).toBe(false);
    expect(sourcePolicy).toEqual({
      id: 'memory-package',
      status: 'error',
      action_class: 'operator_only',
      reason: 'memory_package_source_build',
    });
  });

  it.each([
    { id: 'memory-runtime' },
    { status: 'missing' },
    { status: 'not_required' },
    { action_class: 'repairable' },
    { action_class: 'none' },
    { action_class: undefined },
    { reason: 'memory_package_missing' },
    { reason: 'memory_package_runtime_unavailable' },
    { reason: 'memory_package_unpublished_build' },
    { reason: null },
  ] satisfies Partial<DependencyItem>[])(
    'requires the complete source-only policy, not %j',
    (differentEvidence) => {
      expect(memoryPackageIsSourceManaged({ ...sourcePolicy, ...differentEvidence })).toBe(false);
    },
  );
});

describe('dependencyHasInstallAction', () => {
  it('hides install and repair actions for unsupported dependencies', () => {
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'unsupported' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'not_required' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'error', action_class: 'operator_only' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'missing', action_class: 'none' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'error', action_class: 'operator_only' })).toBe(false);
  });

  it('keeps supported dependency actions unchanged', () => {
    expect(dependencyHasInstallAction({ id: 'memory-package', status: 'missing', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'memory-package', status: 'error', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'memory-package', status: 'not_required', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'memory-package', status: 'not_required', action_class: 'none' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'not_required', action_class: 'repairable' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'missing' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'memory-runtime', status: 'ready', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'ready' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'missing', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'model-hub-engine', status: 'upgrade_required', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'node', status: 'missing' })).toBe(false);
  });
});

describe('startup dependency refresh', () => {
  const dependency = (id: string, installed: boolean) => ({
    id,
    kind: 'tool' as const,
    required: true,
    installed,
    version: null,
    status: installed ? ('ready' as const) : ('missing' as const),
  });

  it('tracks every dependency repaired by the startup reconciler', () => {
    for (const id of ['askill', 'avault', 'show-runtime', 'tmux']) {
      expect(dependencyIsStartupManaged({ id })).toBe(true);
    }
    expect(dependencyIsStartupManaged({ id: 'node' })).toBe(false);
    expect(dependencyIsStartupManaged({ id: 'memory-runtime' })).toBe(false);
  });

  it('allows one initial retry when startup reconciliation has not acquired its lock yet', () => {
    expect(
      dependenciesNeedAutomaticRefresh({
        ok: true,
        reconciling: false,
        deps: [dependency('show-runtime', false)],
      }, true),
    ).toBe(true);
    expect(
      dependenciesNeedAutomaticRefresh({
        ok: true,
        reconciling: false,
        deps: [dependency('show-runtime', false)],
      }),
    ).toBe(false);
  });

  it('keeps polling while startup reconciliation is active', () => {
    expect(
      dependenciesNeedAutomaticRefresh({
        ok: true,
        reconciling: true,
        deps: [dependency('show-runtime', true)],
      }),
    ).toBe(true);
    expect(
      dependenciesNeedAutomaticRefresh({
        ok: true,
        reconciling: false,
        deps: [dependency('show-runtime', true), dependency('memory-runtime', false)],
      }),
    ).toBe(false);
  });

  it('treats installed dependencies requiring an upgrade as startup repairs', () => {
    expect(
      dependencyIsStartupRepairing({ id: 'avault', installed: true, status: 'upgrade_required' }),
    ).toBe(true);
    expect(
      dependenciesNeedAutomaticRefresh({
        ok: true,
        reconciling: false,
        deps: [{ ...dependency('avault', true), status: 'upgrade_required' }],
      }, true),
    ).toBe(true);
  });

  it('limits startup repair status to dependencies currently active in the backend', () => {
    const showRuntime = dependency('show-runtime', false);
    expect(dependencyIsStartupRepairing(showRuntime, new Set(['tmux']))).toBe(false);
    expect(dependencyIsStartupRepairing(showRuntime, new Set(['show-runtime']))).toBe(true);
    expect(
      dependenciesNeedAutomaticRefresh({
        ok: true,
        reconciling: false,
        reconciling_dependencies: [],
        deps: [showRuntime],
      }, true),
    ).toBe(false);
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
