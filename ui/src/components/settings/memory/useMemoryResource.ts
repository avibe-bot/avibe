import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { classifyMemoryResult, memoryErrorMessage } from '../../../lib/memoryRead';

export type MemoryResource<T, A extends unknown[] = []> = {
  /** Last accepted payload. Kept across a failure unless `resetDataOnError` is set. */
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
} & Partial<MemoryRetryPolicy>;

/**
 * How a resource treats the previous attempt's outcome while a new attempt runs.
 * The panels genuinely differ here: an explicit Refresh/Search reports its own
 * attempt from a clean slate, while a polled resource must keep the last good
 * payload and its banner so neither blinks every tick.
 */
export type MemoryRetryPolicy = {
  /** Drop the visible error when a request starts, so a retry is not narrated by the old failure. */
  clearErrorOnReload: boolean;
  /** Drop the last payload when a request fails, so a retry does not render stale content. */
  resetDataOnError: boolean;
};

const KEEP_PREVIOUS_OUTCOME: MemoryRetryPolicy = {
  clearErrorOnReload: false,
  resetDataOnError: false,
};

export type MemoryResourceState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  loaded: boolean;
  forbidden: boolean;
};

/** Resolved outcome of one request, with the error already mapped to a message. */
export type MemorySettlement<T> =
  | { kind: 'ok'; value: T }
  | { kind: 'error'; message: string; forbidden: boolean };

export const INITIAL_MEMORY_RESOURCE_STATE: MemoryResourceState<never> = {
  data: null,
  error: null,
  loading: false,
  loaded: false,
  forbidden: false,
};

/** Transition applied when a request starts. */
export function memoryRequestStarted<T>(
  prev: MemoryResourceState<T>,
  policy: MemoryRetryPolicy,
): MemoryResourceState<T> {
  return { ...prev, loading: true, error: policy.clearErrorOnReload ? null : prev.error };
}

/** Transition applied when the newest request settles. */
export function memoryRequestSettled<T>(
  prev: MemoryResourceState<T>,
  settlement: MemorySettlement<T>,
  policy: MemoryRetryPolicy,
): MemoryResourceState<T> {
  const applied: MemoryResourceState<T> =
    settlement.kind === 'ok'
      ? { ...prev, data: settlement.value, error: null }
      : {
          ...prev,
          data: policy.resetDataOnError ? null : prev.data,
          error: settlement.message,
          // Forbidden is sticky: a later success must not flicker the page out of
          // its "available on this device only" state.
          forbidden: prev.forbidden || settlement.forbidden,
        };
  return { ...applied, loading: false, loaded: true };
}

/**
 * One Memory read: fetch, discriminate the closed result envelope, map the
 * error code to a message, and track loading. Every Memory panel needs the same
 * steps, and each one used to spell them out again — with its own subtly
 * different idea of what "loading" and "failed" mean. The one difference that is
 * real, not accidental, stays configurable: see `MemoryRetryPolicy`.
 */
export function useMemoryResource<T, A extends unknown[] = []>({
  read,
  accept,
  failureMessageKey,
  enabled = true,
  clearErrorOnReload = KEEP_PREVIOUS_OUTCOME.clearErrorOnReload,
  resetDataOnError = KEEP_PREVIOUS_OUTCOME.resetDataOnError,
}: UseMemoryResourceOptions<A>): MemoryResource<T, A> {
  const { t } = useTranslation();
  const [state, setState] = useState<MemoryResourceState<T>>(INITIAL_MEMORY_RESOURCE_STATE);

  const reload = useCallback(
    async (...args: A) => {
      if (!enabled) return;
      const policy: MemoryRetryPolicy = { clearErrorOnReload, resetDataOnError };
      setState((prev) => memoryRequestStarted(prev, policy));
      let settlement: MemorySettlement<T>;
      try {
        const outcome = classifyMemoryResult<T>(await read(...args), accept);
        settlement =
          outcome.kind === 'ok'
            ? { kind: 'ok', value: outcome.value }
            : {
                kind: 'error',
                message: memoryErrorMessage(t, outcome.code),
                forbidden: outcome.forbidden,
              };
      } catch {
        settlement = { kind: 'error', message: t(failureMessageKey), forbidden: false };
      }
      setState((prev) => memoryRequestSettled(prev, settlement, policy));
    },
    [accept, clearErrorOnReload, enabled, failureMessageKey, read, resetDataOnError, t],
  );

  const setData = useCallback((value: T) => {
    setState((prev) => ({ ...prev, data: value, error: null }));
  }, []);

  return { ...state, reload, setData };
}
