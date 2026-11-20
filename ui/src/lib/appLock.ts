// The App Lock setting's access to whichever shell owns the lock: the iPhone App (`appLock`
// capability) or the Desktop shell (`__AVIBE_DESKTOP_APP_LOCK__`). Shapes: appLockContract.ts.
import {
  parseAppLockSetResult,
  parseAppLockStatus,
  type AppLockPolicy,
  type AppLockSetResult,
  type AppLockStatus,
} from './appLockContract';
import { desktopAppLockInvoke } from './desktopShell';
import { hasNativeCapability, nativeAppLockSetPolicy, nativeAppLockStatus } from './native';

/** Which device's words the setting uses. */
export type AppLockDevice = 'iphone' | 'mac';

export type AppLockActions = {
  device: AppLockDevice;
  status(): Promise<AppLockStatus>;
  setPolicy(policy: AppLockPolicy): Promise<AppLockSetResult>;
};

function decoded<T>(value: T | null): T {
  if (value === null) throw new Error('App Lock answer is outside the contract');
  return value;
}

/** The shell's App Lock, or null in a browser or a shell without it. */
export function appLockActions(): AppLockActions | null {
  if (hasNativeCapability('appLock')) {
    return {
      device: 'iphone',
      status: async () => decoded(parseAppLockStatus(await nativeAppLockStatus())),
      setPolicy: async (policy) => decoded(parseAppLockSetResult(await nativeAppLockSetPolicy(policy))),
    };
  }
  const invoke = desktopAppLockInvoke();
  if (invoke) {
    return {
      device: 'mac',
      status: async () => decoded(parseAppLockStatus(await invoke('app_lock_status'))),
      setPolicy: async (policy) => decoded(parseAppLockSetResult(await invoke('app_lock_set_policy', { policy }))),
    };
  }
  return null;
}
