import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { classifyMemoryResult, memoryErrorMessage } from '../../../lib/memoryRead';

export type MemoryResource<T, A extends unknown[] = []> = {
  /** Last accepted payload. Kept across a failure — every panel renders `error` first. */
  data: T | null;
  error: string | null;
  /** A request is in flight. */
  loading: boolean;
  /** At least one request has settled; `!loaded` is the first-paint state. */
  loaded: boolean;
  /**
   * The backend answered with the non-loopback forbidden body at least once.
   * Sticky: the page turns it into the "available on this device only" state,
   * which must not flicker back on a later request that happens to succeed.
   */
  forbidden: boolean;
  reload: (...args: A) => Promise<void>;
  /** Adopt a payload obtained elsewhere (a settings PATCH response) without a re-read. */
  setData: (value: T) => void;
};

export type UseMemoryResourceOptions<A extends unknown[]> = {
  /** Must be referentially stable — it is a `reload` dependency. */
  read: (...args: A) => Promise<unknown>;
  /** Success predicate; defaults to the `status: 'ok'` envelope. Must be stable. */
  accept?: (value: unknown) => boolean;
  /** i18n key used when the request itself throws, so there is no closed code. */
  failureMessageKey: string;
  /** When false, `reload` is a no-op — Memory being off is not a failure. */
  enabled?: boolean;
};

type ResourceState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  loaded: boolean;
  forbidden: boolean;
};

const INITIAL_STATE = { data: null, error: null, loading: false, loaded: false, forbidden: false };

/**
 * One Memory read: fetch, discriminate the closed result envelope, map the
 * error code to a message, and track loading. Every Memory panel needs the
 * same four steps, and each one used to spell them out again — with its own
 * subtly different idea of what "loading" and "failed" mean.
 */
export function useMemoryResource<T, A extends unknown[] = []>({
  read,
  accept,
  failureMessageKey,
  enabled = true,
}: UseMemoryResourceOptions<A>): MemoryResource<T, A> {
  const { t } = useTranslation();
  const [state, setState] = useState<ResourceState<T>>(INITIAL_STATE);

  const reload = useCallback(
    async (...args: A) => {
      if (!enabled) return;
      setState((prev) => ({ ...prev, loading: true }));
      let settle: (prev: ResourceState<T>) => ResourceState<T>;
      try {
        const outcome = classifyMemoryResult<T>(await read(...args), accept);
        settle =
          outcome.kind === 'ok'
            ? (prev) => ({ ...prev, data: outcome.value, error: null })
            : (prev) => ({
                ...prev,
                error: memoryErrorMessage(t, outcome.code),
                forbidden: prev.forbidden || outcome.forbidden,
              });
      } catch {
        settle = (prev) => ({ ...prev, error: t(failureMessageKey) });
      }
      setState((prev) => ({ ...settle(prev), loading: false, loaded: true }));
    },
    [accept, enabled, failureMessageKey, read, t],
  );

  const setData = useCallback((value: T) => {
    setState((prev) => ({ ...prev, data: value, error: null }));
  }, []);

  return { ...state, reload, setData };
}
