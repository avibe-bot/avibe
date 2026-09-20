import type { DependenciesResult, DependencyItem, MemoryStatusResult } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set([
  'askill',
  'avault',
  'model-hub-engine',
  'show-runtime',
  'memory-package',
  'memory-runtime',
  'tmux',
]);

/** Source-only repair policy is not a verdict on runtime health. */
export const memoryPackageIsSourceManaged = (
  dependency: Pick<DependencyItem, 'id' | 'status' | 'action_class' | 'reason'>,
): boolean => (
  dependency.id === 'memory-package'
  && dependency.status === 'error'
  && dependency.action_class === 'operator_only'
  && dependency.reason === 'memory_package_source_build'
);

/** One Memory entry surfaces the prerequisite that currently needs attention. */
export const memoryDependencyForDisplay = (
  memoryPackage: DependencyItem | null,
  memoryRuntime: DependencyItem | null,
): DependencyItem | null => {
  if (memoryPackage && !memoryPackageIsSourceManaged(memoryPackage)
    && !['ready', 'not_required'].includes(memoryPackage.status)) {
    return memoryPackage;
  }
  if (memoryRuntime && !['not_required', 'unknown'].includes(memoryRuntime.status)) {
    return memoryRuntime;
  }
  return memoryPackage ?? memoryRuntime;
};

const STARTUP_MANAGED_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'model-hub-engine', 'tmux']);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status' | 'action_class'>,
): boolean => {
  const optionalMemoryPackageRepair = (
    dependency.id === 'memory-package'
    && dependency.status === 'not_required'
    && dependency.action_class === 'repairable'
  );
  return (
    dependency.status !== 'unsupported'
    && (dependency.status !== 'not_required' || optionalMemoryPackageRepair)
    && dependency.action_class !== 'none'
    && dependency.action_class !== 'operator_only'
    && INSTALLABLE_DEPENDENCIES.has(dependency.id)
  );
};

/** Treat any active or uncertain runtime as unsafe for dependency replacement. */
export const memoryRuntimeSidecarRunning = (
  status: MemoryStatusResult | null | undefined,
): boolean => (
  !!status
  && typeof status === 'object'
  && 'status' in status
  && status.status === 'ok'
  && ['starting', 'running', 'degraded'].includes(status.state)
);

export const dependencyIsStartupManaged = (dependency: Pick<DependencyItem, 'id'>): boolean =>
  STARTUP_MANAGED_DEPENDENCIES.has(dependency.id);

export const dependencyIsStartupRepairing = (
  dependency: Pick<DependencyItem, 'id' | 'installed' | 'status'>,
  activeIds?: ReadonlySet<string>,
): boolean =>
  dependencyIsStartupManaged(dependency) &&
  (activeIds === undefined || activeIds.has(dependency.id)) &&
  (!dependency.installed || dependency.status === 'upgrade_required' || dependency.status === 'error');

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
