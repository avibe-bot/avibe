import type { DependencyItem, MemoryStatusResult } from '@/context/ApiContext';

const INSTALLABLE_DEPENDENCIES = new Set(['askill', 'avault', 'show-runtime', 'memory-runtime', 'tmux']);

export const dependencyHasInstallAction = (
  dependency: Pick<DependencyItem, 'id' | 'status'>,
): boolean => dependency.status !== 'unsupported' && INSTALLABLE_DEPENDENCIES.has(dependency.id);

/** A reachable sidecar is the live process Repair must not replace. */
export const memoryRuntimeSidecarRunning = (
  status: MemoryStatusResult | null | undefined,
): boolean => (
  !!status
  && typeof status === 'object'
  && 'status' in status
  && status.status === 'ok'
  && status.source?.status === 'available'
);
