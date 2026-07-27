import type { TFunction } from 'i18next';

// Backend forbidden path (`_memory_forbidden_response`) returns exactly this closed shape for
// every Memory route when the request isn't direct-loopback (e.g. opened via Avibe Cloud). It is
// otherwise never produced by a settings/status/profile/search/clear success or config-disabled
// path, so it's a safe signal to render the "available on this device only" static state instead
// of a generic error.
export const MEMORY_FORBIDDEN_ERROR = 'memory_disabled';

// Every Memory route body is discriminated: `status: 'ok'` on success, `status: 'failed'` with a
// closed error code otherwise. A dependency-missing failure from the internal handler carries only
// `error`, so require the tag rather than merely rejecting 'failed'.
export const isMemoryOk = <T,>(value: T): value is Extract<T, { status: 'ok' }> =>
  !!value && typeof value === 'object' && (value as { status?: unknown }).status === 'ok';

export const isMemoryForbidden = (value: unknown): boolean =>
  !!value &&
  typeof value === 'object' &&
  (value as { status?: string; error?: string }).status === 'failed' &&
  (value as { status?: string; error?: string }).error === MEMORY_FORBIDDEN_ERROR;

/** One Memory read verdict: the accepted payload, or a closed failure code. */
export type MemoryReadOutcome<T> =
  | { kind: 'ok'; value: T }
  | { kind: 'failed'; code: string | undefined; forbidden: boolean };

/**
 * Discriminate one Memory route body.
 *
 * `accept` exists because not every route tags success the same way: the
 * failure log answers with a bare `{ items, retention_days }` while the rest
 * carry `status: 'ok'`. A forbidden body satisfies no `accept`, so the
 * forbidden verdict rides along on the failure rather than pre-empting it —
 * callers that don't distinguish it still surface its error code.
 */
export function classifyMemoryResult<T>(
  result: unknown,
  accept: (value: unknown) => boolean = isMemoryOk,
): MemoryReadOutcome<T> {
  if (accept(result)) return { kind: 'ok', value: result as T };
  return {
    kind: 'failed',
    code: (result as { error?: string } | null | undefined)?.error,
    forbidden: isMemoryForbidden(result),
  };
}

/** Map a closed backend error code to its localized message. */
export const memoryErrorMessage = (t: TFunction, code: string | null | undefined): string =>
  code ? t(`errors.${code}`, { defaultValue: code }) : t('common.unknown');
