import { describe, expect, it } from 'vitest';
import { dependencyHasInstallAction } from './SettingsDependenciesPage.logic';

describe('dependencyHasInstallAction', () => {
  it('keeps generic dependency evidence and admission distinct', () => {
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'error', action_class: 'operator_only' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'missing', action_class: 'none' })).toBe(false);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'ready' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'show-runtime', status: 'missing', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'model-hub-engine', status: 'upgrade_required', action_class: 'repairable' })).toBe(true);
    expect(dependencyHasInstallAction({ id: 'node', status: 'missing' })).toBe(false);
  });
});
