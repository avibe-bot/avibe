export type RegionRead<T> =
  | { kind: 'loading'; data?: T }
  | { kind: 'ready'; data: T }
  | { kind: 'unread'; retryable: boolean }
  | { kind: 'error'; data: T; retryable: boolean };

export const loadingRegion = <T>(): RegionRead<T> => ({ kind: 'loading' });

export const readyRegion = <T>(data: T): RegionRead<T> => ({ kind: 'ready', data });

export const unreadRegion = <T>(retryable = true): RegionRead<T> => ({ kind: 'unread', retryable });

export const regionData = <T>(read: RegionRead<T>): T | undefined => (
  read.kind === 'ready' || read.kind === 'error' || (read.kind === 'loading' && 'data' in read)
    ? read.data
    : undefined
);

/** A retry may keep the previous projection visible, but it never makes that
 *  projection authoritative again. */
export const beginRegionRead = <T>(previous: RegionRead<T>): RegionRead<T> => {
  const data = regionData(previous);
  return data === undefined ? loadingRegion<T>() : { kind: 'loading', data };
};

/** A failed first read is explicitly unread. A later failure retains the last
 *  good projection under an error tag, so consumers cannot mistake it for a
 *  fresh domain value. */
export const failRegionRead = <T>(previous: RegionRead<T>, retryable = true): RegionRead<T> => {
  const data = regionData(previous);
  return data === undefined ? unreadRegion<T>(retryable) : { kind: 'error', data, retryable };
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
  read.kind === 'unread' || read.kind === 'error';
