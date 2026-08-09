import type { DependenciesResult, DependencyItem } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'memory-runtime', 'tmux']);
const STARTUP_MANAGED_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'tmux']);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status'>,
): boolean => dependency.status !== 'unsupported' && INSTALLABLE_DEPENDENCIES.has(dependency.id);

export const dependencyIsStartupManaged = (dependency: Pick<DependencyItem, 'id'>): boolean =>
  STARTUP_MANAGED_DEPENDENCIES.has(dependency.id);

export const dependenciesNeedAutomaticRefresh = (
  result: DependenciesResult,
  allowInitialRetry = false,
): boolean =>
  Boolean(result.reconciling) ||
  (allowInitialRetry && result.deps.some((dependency) => dependencyIsStartupManaged(dependency) && !dependency.installed));
