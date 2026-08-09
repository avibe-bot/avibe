import { describe, expect, it } from 'vitest';

import {
  dependenciesNeedAutomaticRefresh,
  dependencyHasInstallAction,
  dependencyIsStartupManaged,
} from './SettingsDependenciesPage.logic';

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
});
