import type { DependenciesResult, DependencyItem } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set([
  'askill',
  'avault',
  'model-hub-engine',
  'show-runtime',
  'tmux',
]);

const STARTUP_MANAGED_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'model-hub-engine', 'tmux']);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status' | 'action_class'>,
): boolean => (
  dependency.status !== 'unsupported'
  && dependency.status !== 'not_required'
  && dependency.action_class !== 'none'
  && dependency.action_class !== 'operator_only'
  && INSTALLABLE_DEPENDENCIES.has(dependency.id)
);

export const dependencyIsStartupManaged = (dependency: Pick<DependencyItem, 'id'>): boolean =>
  STARTUP_MANAGED_DEPENDENCIES.has(dependency.id);

/**
 * Upgrade required, or an installed avault below its managed pin. avault's
 * readiness floor stays lower than the pin so run/inject keep working, but
 * fetch needs the pinned release's TLS trust, and startup upgrades to it.
 */
export const dependencyNeedsUpgrade = (
  dependency: Pick<DependencyItem, 'id' | 'status'> & Partial<Pick<DependencyItem, 'installed' | 'has_update'>>,
): boolean =>
  dependency.status === 'upgrade_required' ||
  (dependency.id === 'avault' && dependency.status === 'ready' && dependency.installed === true && dependency.has_update === true);

/** Manual repair must wait for the startup pass, even before this step starts. */
export const dependencyNeedsStartupRepair = (
  dependency: Pick<DependencyItem, 'id' | 'status'> & Partial<Pick<DependencyItem, 'installed' | 'has_update'>>,
): boolean =>
  dependencyIsStartupManaged(dependency) &&
  (dependency.status === 'missing' || dependency.status === 'error' || dependencyNeedsUpgrade(dependency));

export const dependencyIsStartupRepairing = (
  dependency: Pick<DependencyItem, 'id' | 'installed' | 'status'> & Partial<Pick<DependencyItem, 'has_update'>>,
  activeIds?: ReadonlySet<string>,
): boolean =>
  dependencyIsStartupManaged(dependency) &&
  (activeIds === undefined || activeIds.has(dependency.id)) &&
  (!dependency.installed || dependency.status === 'error' || dependencyNeedsUpgrade(dependency));

export const dependenciesNeedAutomaticRefresh = (
  result: DependenciesResult,
  allowInitialRetry = false,
): boolean => {
  const activeIds = Array.isArray(result.reconciling_dependencies)
    ? new Set(result.reconciling_dependencies)
    : undefined;
  return Boolean(result.reconciling) ||
    (allowInitialRetry && result.deps.some((dependency) => dependencyIsStartupRepairing(dependency, activeIds)));
};
