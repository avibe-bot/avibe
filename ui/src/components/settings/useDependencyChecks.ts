import { useCallback, useEffect, useRef, useState } from 'react';

import type { DependenciesResult, DependencyItem, DependencyReadOptions } from '@/context/ApiContext';
import { isApiFetchDeadlineAbort } from '@/lib/apiFetch';

// Coupled rows use one inspection, including its failure evidence.
export const DEPENDENCY_CHECK_GROUPS = [
  ['askill'],
  ['avault'],
  ['show-runtime', 'node'],
  ['model-hub-engine'],
  ['memory-package', 'memory-runtime'],
  ['tmux'],
] as const;

export type DependencyCheck = {
  data: DependencyItem | null;
  checking: boolean;
  error: 'failed' | 'timeout' | null;
};

type ReadDependencies = (options?: DependencyReadOptions) => Promise<DependenciesResult>;

export function useDependencyChecks(read: ReadDependencies) {
  const [checks, setChecks] = useState<Record<string, DependencyCheck>>(() => Object.fromEntries(
    DEPENDENCY_CHECK_GROUPS.flatMap((ids) => ids.map((id) => [
      id, { data: null, checking: true, error: null },
    ])),
  ));
  const requests = useRef(new Map<string, AbortController>());

  useEffect(() => {
    const active = requests.current;
    return () => {
      for (const request of active.values()) request.abort();
      active.clear();
    };
  }, []);

  const refresh = useCallback(async (dependencyId?: string) => {
    const groups = DEPENDENCY_CHECK_GROUPS.filter((ids) => (
      dependencyId === undefined || ids.some((id) => id === dependencyId)
    ));
    await Promise.all(groups.map(async (ids) => {
      const key = ids[0];
      requests.current.get(key)?.abort();
      const controller = new AbortController();
      requests.current.set(key, controller);
      const update = (change: (previous: DependencyCheck, id: string) => DependencyCheck) => {
        setChecks((previous) => {
          if (requests.current.get(key) !== controller || controller.signal.aborted) return previous;
          return { ...previous, ...Object.fromEntries(ids.map((id) => [id, change(previous[id], id)])) };
        });
      };
      update((previous) => ({ ...previous, checking: true }));
      try {
        const result = await read({ ids, signal: controller.signal });
        if (!result.ok || !Array.isArray(result.deps) || ids.some((id) => !result.deps.some((dep) => dep.id === id))) {
          throw new Error('Incomplete dependency inspection');
        }
        const byId = new Map(result.deps.map((dep) => [dep.id, dep]));
        update((_previous, id) => ({ data: byId.get(id)!, checking: false, error: null }));
      } catch (error) {
        update((previous) => ({
          ...previous,
          checking: false,
          error: isApiFetchDeadlineAbort(error) ? 'timeout' : 'failed',
        }));
      }
    }));
  }, [read]);

  return { checks, refresh, checking: Object.values(checks).some((check) => check.checking) };
}
