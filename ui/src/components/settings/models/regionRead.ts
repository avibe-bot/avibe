const REGION_VALUE: unique symbol = Symbol('model-hub-region-value');

export type RegionReadCause = 'refreshing' | 'read_failed';

export type RegionRead<T> =
  | { kind: 'loading' }
  | { kind: 'ready'; readonly [REGION_VALUE]: T }
  | { kind: 'unread'; retryable: boolean }
  | {
      kind: 'degraded';
      cause: RegionReadCause;
      retryable: boolean;
      readonly [REGION_VALUE]: T;
    };

type RegionReadFold<T, R> = {
  loading: () => R;
  ready: (data: T) => R;
  unread: (retryable: boolean) => R;
  degraded: (staleData: T, cause: RegionReadCause, retryable: boolean) => R;
};

export const loadingRegion = <T>(): RegionRead<T> => ({ kind: 'loading' });

export const readyRegion = <T>(data: T): RegionRead<T> => ({ kind: 'ready', [REGION_VALUE]: data });

export const unreadRegion = <T>(retryable = true): RegionRead<T> => ({ kind: 'unread', retryable });

export const degradedRegion = <T>(
  staleData: T,
  cause: RegionReadCause,
  retryable: boolean,
): RegionRead<T> => ({ kind: 'degraded', cause, retryable, [REGION_VALUE]: staleData });

/** The exhaustive projection path for consumers that intentionally render stale data. */
export const foldRegionRead = <T, R>(read: RegionRead<T>, cases: RegionReadFold<T, R>): R => {
  switch (read.kind) {
    case 'loading': return cases.loading();
    case 'ready': return cases.ready(read[REGION_VALUE]);
    case 'unread': return cases.unread(read.retryable);
    case 'degraded': return cases.degraded(read[REGION_VALUE], read.cause, read.retryable);
  }
};

/** A retry may keep the previous projection visible, but it never makes that
 *  projection authoritative again. */
export const beginRegionRead = <T>(previous: RegionRead<T>): RegionRead<T> => {
  return foldRegionRead(previous, {
    loading: () => loadingRegion<T>(),
    ready: (data) => degradedRegion(data, 'refreshing', false),
    unread: () => loadingRegion<T>(),
    degraded: (data) => degradedRegion(data, 'refreshing', false),
  });
};

/** A failed first read is explicitly unread. A later failure retains the last
 *  good projection under an error tag, so consumers cannot mistake it for a
 *  fresh domain value. */
export const failRegionRead = <T>(previous: RegionRead<T>, retryable = true): RegionRead<T> => {
  return foldRegionRead(previous, {
    loading: () => unreadRegion<T>(retryable),
    ready: (data) => degradedRegion(data, 'read_failed', retryable),
    unread: () => unreadRegion<T>(retryable),
    degraded: (data) => degradedRegion(data, 'read_failed', retryable),
  });
};

export const settleRegionRead = <T>(
  previous: RegionRead<T>,
  next: RegionRead<T>,
): RegionRead<T> => next.kind === 'ready'
  ? next
  : next.kind === 'unread'
    ? failRegionRead(previous, next.retryable)
    : next;

export async function readRegion<T>(read: () => Promise<T>): Promise<RegionRead<T>> {
  try {
    return readyRegion(await read());
  } catch {
    return unreadRegion<T>();
  }
}

export const regionFailed = (read: RegionRead<unknown>): boolean =>
  read.kind === 'unread' || (read.kind === 'degraded' && read.cause === 'read_failed');
