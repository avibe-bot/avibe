import { isApplePlatform } from '../lib/platform';

const renameMac = (value: unknown): unknown => {
  if (typeof value === 'string') return value.replace(/\bMac\b/g, 'PC');
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, renameMac(item)]));
  return value;
};

/**
 * Desktop's copy (under `desktopClient` and `desktopAccount`) names the local machine "Mac". On any
 * other desktop OS (Windows) that machine is a "PC". Applied to the copy itself,
 * before interpolation, so a user-supplied name such as "Amy's Mac" stays intact.
 */
export function forDevicePlatform<T extends object>(bundle: T): T {
  if (isApplePlatform()) return bundle;
  const patched = { ...bundle } as Record<string, unknown>;
  for (const key of ['desktopClient', 'desktopAccount']) if (key in patched) patched[key] = renameMac(patched[key]);
  return patched as T;
}
