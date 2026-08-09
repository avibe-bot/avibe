import type { DependenciesResult, DependencyItem } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'memory-runtime', 'tmux']);
const STARTUP_MANAGED_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'tmux']);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status'>,
): boolean => dependency.status !== 'unsupported' && INSTALLABLE_DEPENDENCIES.has(dependency.id);

export const dependencyIsStartupManaged = (dependency: Pick<DependencyItem, 'id'>): boolean =>
  STARTUP_MANAGED_DEPENDENCIES.has(dependency.id);

export const dependencyIsStartupRepairing = (
  dependency: Pick<DependencyItem, 'id' | 'installed' | 'status'>,
): boolean =>
  dependencyIsStartupManaged(dependency) &&
  (!dependency.installed || dependency.status === 'upgrade_required' || dependency.status === 'error');

export const dependenciesNeedAutomaticRefresh = (
  result: DependenciesResult,
  allowInitialRetry = false,
): boolean =>
  Boolean(result.reconciling) ||
  (allowInitialRetry && result.deps.some((dependency) => dependencyIsStartupRepairing(dependency)));
