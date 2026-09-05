import type { DependencyItem, MemoryStatusResult } from '@/context/ApiContext';

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
