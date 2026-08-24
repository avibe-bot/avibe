import type { DependencyItem, MemoryStatusResult } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'memory-runtime', 'tmux']);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status'>,
): boolean => dependency.status !== 'unsupported' && INSTALLABLE_DEPENDENCIES.has(dependency.id);

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
